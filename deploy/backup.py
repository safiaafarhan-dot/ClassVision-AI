"""
ClassVision AI - backup (Step 13)
---------------------------------
A consistent copy of the database and the face embeddings.

WHY NOT JUST COPY THE FILE

    The database runs in WAL mode. At any moment some committed transactions
    live in `classvision.db-wal` and not yet in `classvision.db`, so copying
    only the .db file - which is what every "backup" that uses the file
    explorer does - produces a file that opens cleanly and is missing the most
    recent lessons. That is the worst possible failure: it looks like a
    backup.

    This uses SQLite's own backup API, which takes a consistent snapshot of a
    live database including everything in the WAL, without stopping the
    server.

WHAT IS COPIED

    data/classvision.db   through the backup API, as one consistent file
    data/embeddings/      the face vectors, copied as files

WHAT IS NOT COPIED, AND WHY

    data/staff/ - personnel documents. They are copied only with --staff,
    because a backup of identity documents is a different decision from a
    backup of attendance and should be made deliberately.

    Nothing is uploaded anywhere. This writes to a directory you name on a
    disk you chose. It makes no network connection, and no module here can.

VERIFYING

    The copy is opened and queried afterwards, and the row counts of the main
    tables are printed next to the source's. A backup nobody has opened is a
    hypothesis.

Usage:
    python deploy/backup.py                      into backups/<timestamp>/
    python deploy/backup.py --into D:/backups
    python deploy/backup.py --staff              include personnel documents
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

DATABASE = PROJECT_ROOT / "data" / "classvision.db"
EMBEDDINGS = PROJECT_ROOT / "data" / "embeddings"
STAFF = PROJECT_ROOT / "data" / "staff"

# The tables whose counts are printed as a receipt. Not every table - these
# are the ones whose loss would mean losing a term.
COUNTED = ("students", "attendance_sessions", "attendance_records",
           "presence_segments", "users")


def snapshot(source, destination):
    """
    One consistent copy of a live SQLite database, WAL included.

    sqlite3's backup() holds a read lock for the duration and copies pages,
    so a session being written while this runs produces a backup that either
    has that transaction or does not - never half of it.
    """
    origin = sqlite3.connect(str(source))
    try:
        copy = sqlite3.connect(str(destination))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()


def counts(path):
    """Row counts for the tables worth counting, or why they could not be."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    result = {}

    try:
        existing = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}

        for table in COUNTED:
            if table not in existing:
                result[table] = None
                continue
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM %s" % table).fetchone()
            result[table] = row["total"]
    finally:
        connection.close()

    return result


def copy_tree(source, destination):
    """Copy a directory, reporting what was copied rather than assuming."""
    if not source.is_dir():
        return 0, 0

    files = 0
    total_bytes = 0

    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        target = destination / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        files += 1
        total_bytes += item.stat().st_size

    return files, total_bytes


def run(into=None, staff=False, database=DATABASE, embeddings=EMBEDDINGS,
        staff_directory=STAFF, stamp=None):
    """
    Make the backup and return what was made. Raises rather than half-doing.

    Separated from main() so the tests can run a real backup against a real
    temporary database and open the result, rather than asserting about
    printed text.
    """
    if not Path(database).is_file():
        raise FileNotFoundError("There is no database at %s." % database)

    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(into) if into else (PROJECT_ROOT / "backups")
    destination = root / stamp

    if destination.exists():
        raise FileExistsError(
            "%s already exists. Backups are never overwritten." % destination)

    destination.mkdir(parents=True)

    database_copy = destination / "classvision.db"
    snapshot(database, database_copy)

    report = {
        "destination": str(destination),
        "database": str(database_copy),
        "source_counts": counts(database),
        # Read back from the COPY, which is the only way to know the copy is
        # readable at all.
        "backup_counts": counts(database_copy),
        "embedding_files": 0,
        "embedding_bytes": 0,
        "staff_files": 0,
        "staff_included": bool(staff),
    }

    files, size = copy_tree(Path(embeddings), destination / "embeddings")
    report["embedding_files"] = files
    report["embedding_bytes"] = size

    if staff:
        staff_files, _ = copy_tree(Path(staff_directory),
                                   destination / "staff")
        report["staff_files"] = staff_files

    report["matches"] = report["source_counts"] == report["backup_counts"]
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Back up the database and the face embeddings.")
    parser.add_argument("--into", default=None,
                        help="directory to write the backup into "
                             "(default: backups/)")
    parser.add_argument("--staff", action="store_true",
                        help="also copy data/staff/ personnel documents")
    arguments = parser.parse_args()

    try:
        from paths import enable_safe_console
        enable_safe_console()
    except Exception:                                     # noqa: BLE001
        pass

    try:
        report = run(into=arguments.into, staff=arguments.staff)
    except Exception as error:                            # noqa: BLE001
        print("Backup FAILED: %s: %s" % (type(error).__name__, error))
        return 1

    print("ClassVision AI - backup")
    print("=" * 70)
    print("Written to: %s" % report["destination"])
    print("")
    print("%-24s %10s %10s" % ("table", "live", "backup"))

    for table, live in report["source_counts"].items():
        backed = report["backup_counts"].get(table)
        print("%-24s %10s %10s"
              % (table,
                 "absent" if live is None else live,
                 "absent" if backed is None else backed))

    print("")
    print("Embeddings: %d file(s), %d bytes."
          % (report["embedding_files"], report["embedding_bytes"]))

    if report["staff_included"]:
        print("Personnel documents: %d file(s)." % report["staff_files"])
    else:
        print("Personnel documents: NOT included. Pass --staff to copy them.")

    print("=" * 70)

    if not report["matches"]:
        # Counted on both sides precisely so this can be said rather than
        # assumed. A backup whose counts disagree is not a backup.
        print("THE COUNTS DO NOT MATCH. Do not rely on this backup.")
        return 1

    print("The backup opens, and its row counts match the live database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
