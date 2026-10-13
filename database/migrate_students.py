"""
ClassVision AI - Student Registry Migration
--------------------------------------------
Fills the schema-version-2 registry columns in for students who were enrolled
before those columns existed.

    python database/migrate_students.py
    python database/migrate_students.py --dry-run
    python database/migrate_students.py --path data/somewhere-else.db

What this closes
    Until schema version 2, a student row held an id, a name and a status.
    Their roll number, class and section lived ONLY in the Step 3 embedding
    store, so opening classvision.db and running

        SELECT * FROM students;

    answered "who is this person?" with half an answer, and anybody inspecting
    the database had to know that a second store existed and where it was.

    Version 2 adds roll_number, class_name and section to the registry.
    database.py's migrate() creates those columns; this file puts the values
    in, for students who already existed. New students never need it - both
    write paths populate the columns from the start.

Two rules, and both of them are about not destroying anything
    1. SQLite wins. A value already in the registry is never overwritten, even
       if the embedding store disagrees. The registry is what a teacher edits,
       and an old enrollment record must not be able to undo that edit.
    2. Only blanks are filled. A column is written only when it is empty, so
       running this twice does nothing the second time - and running it after
       somebody has corrected a class in the dashboard does nothing either.

    Nothing here deletes a row, a column, a table, an attendance record or an
    embedding. The only statements are INSERT and a narrow UPDATE.

Orphan enrollments
    A face enrollment with no registry row is imported, not invented: the
    store already holds that student's name, roll, class and section, because
    the enrollment asked for them. Importing it is how a student who was
    enrolled from the command line stops being invisible to the dashboard.
    Nothing is guessed - a store record with no name is skipped and reported.

Privacy
    This reads the embedding store's METADATA file only. It never opens a
    .npz, never touches a vector, and writes no biometric data into SQLite.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT / "ai") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "ai"))
if str(PROJECT_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "database"))

from database import DEFAULT_DATABASE_PATH, Database  # noqa: E402
from repositories import StudentRepository  # noqa: E402

# The columns this migration is responsible for. Deliberately not "everything
# the store happens to contain": model names, sample counts and embedding ids
# belong to the enrollment, not to the registry, and copying them into SQLite
# would create a second version of a fact that already has an owner.
MIGRATED_FIELDS = ("roll_number", "class_name", "section")


def load_store_metadata(store=None):
    """
    Student metadata from the embedding store, keyed by student_id.

    Returns an empty dict if the store is missing or unreadable, because
    "nobody has been enrolled yet" is a normal state for a fresh checkout and
    must not stop the application from starting.
    """
    try:
        if store is None:
            from embedding_store import EmbeddingStore
            store = EmbeddingStore()
        records = store.list_students()
    except Exception:                                    # noqa: BLE001
        return {}

    metadata = {}
    for record in records:
        student_id = record.get("student_id")
        if not student_id:
            continue
        metadata[student_id] = {
            "name": (record.get("name") or "").strip(),
            "roll_number": (record.get("roll_number") or "").strip(),
            "class_name": (record.get("class_name") or "").strip(),
            "section": (record.get("section") or "").strip(),
        }
    return metadata


def synchronise_registry(database, store=None, create_missing=True,
                         dry_run=False):
    """
    Bring the registry into step with the embedding store. Additive only.

    Returns a report describing exactly what happened, so a caller can log it
    and a test can assert on it:

        {"created": [...], "filled": [{"student_id", "fields"}, ...],
         "skipped": [...], "unchanged": n, "store_students": n}

    Safe to call on every application start. On a database that is already in
    step it performs no write at all.
    """
    report = {"created": [], "filled": [], "skipped": [], "unchanged": 0,
              "store_students": 0}

    metadata = load_store_metadata(store)
    report["store_students"] = len(metadata)

    if not metadata:
        return report

    students = StudentRepository(database)

    for student_id, record in sorted(metadata.items()):
        existing = students.get(student_id)

        if existing is None:
            if not create_missing:
                report["skipped"].append(student_id)
                continue

            # A store record with no name tells us nothing we could register.
            # Reported rather than filled in with a placeholder - inventing a
            # name is worse than saying the data is incomplete.
            if not record["name"]:
                report["skipped"].append(student_id)
                continue

            if not dry_run:
                students.upsert(student_id, record["name"],
                                roll_number=record["roll_number"],
                                class_name=record["class_name"],
                                section=record["section"])
            report["created"].append(student_id)
            continue

        # Fill blanks only. A value the registry already holds is the one a
        # teacher can see and edit, and it wins.
        missing = {field: record[field] for field in MIGRATED_FIELDS
                   if not (existing.get(field) or "").strip()
                   and record[field]}

        if not missing:
            report["unchanged"] += 1
            continue

        if not dry_run:
            students.update(student_id, **missing)
        report["filled"].append({"student_id": student_id,
                                 "fields": sorted(missing)})

    return report


def describe(report):
    lines = ["  embedding store  : %d enrolled student(s)"
             % report["store_students"]]

    if report["created"]:
        lines.append("  registered       : %d (%s)"
                     % (len(report["created"]), ", ".join(report["created"])))
    if report["filled"]:
        lines.append("  metadata filled  : %d" % len(report["filled"]))
        for entry in report["filled"]:
            lines.append("      %-16s %s" % (entry["student_id"],
                                             ", ".join(entry["fields"])))
    if report["skipped"]:
        lines.append("  skipped          : %d (%s)"
                     % (len(report["skipped"]), ", ".join(report["skipped"])))
        lines.append("      no name in the enrollment record - nothing was "
                     "invented for them")

    lines.append("  already correct  : %d" % report["unchanged"])
    return "\n".join(lines)


def main():
    from paths import enable_safe_console, relative
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Fill the student registry's roll number, class and "
                    "section from existing face enrollments. Never deletes "
                    "or overwrites anything.")
    parser.add_argument("--path", default=None,
                        help="database file (default: data/classvision.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args()

    path = Path(args.path) if args.path else DEFAULT_DATABASE_PATH

    if not path.exists():
        print("No database at %s." % relative(path))
        print("Create it with:  python database/init_db.py")
        return 1

    database = Database(path)

    try:
        database.connect()

        missing = [column for column in MIGRATED_FIELDS
                   if column not in database.column_names("students")]

        if args.dry_run:
            # A dry run must not change the schema either. It says what the
            # real run would add and stops there.
            print("Schema: would add column(s) %s to students."
                  % ", ".join(missing) if missing
                  else "Schema: students already has the registry columns.")
        else:
            added = database.migrate()
            if added:
                print("Schema: added column(s) %s to students."
                      % ", ".join(added))
            else:
                print("Schema: students already has the registry columns.")

        before = database.query_one(
            "SELECT COUNT(*) AS total FROM students")["total"]

        if args.dry_run:
            report = synchronise_registry(database, dry_run=True)
            print("\nDRY RUN - nothing was written.\n")
        else:
            with database.transaction():
                report = synchronise_registry(database)
            print("")

        print(describe(report))

        after = database.query_one(
            "SELECT COUNT(*) AS total FROM students")["total"]
        print("\n  students before  : %d" % before)
        print("  students after   : %d" % after)

        attendance = database.query_one(
            "SELECT COUNT(*) AS total FROM attendance_records")["total"]
        segments = database.query_one(
            "SELECT COUNT(*) AS total FROM presence_segments")["total"]
        print("  attendance rows  : %d (untouched)" % attendance)
        print("  presence rows    : %d (untouched)" % segments)

        return 0

    except Exception as error:                           # noqa: BLE001
        print("ERROR: %s: %s" % (type(error).__name__, error))
        return 1

    finally:
        database.close()


if __name__ == "__main__":
    sys.exit(main())
