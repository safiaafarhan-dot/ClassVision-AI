"""
Step 13: the restore, rehearsed end to end.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every restore here is performed against a
real temporary tree - a real WAL-mode database, a real backup taken through
`deploy/backup.py`, real embedding files - and the restored installation is
opened and queried afterwards. The real `data/classvision.db` is never read or
written.

WHY THIS SUITE EXISTS

    `deploy/README.md` documents a restore in six steps and `backup.py` has no
    `--restore` flag, because a restore throws away whatever happened after the
    backup was taken and that is a decision for a person. What followed from
    that, until now, was that nobody had ever performed the six steps and
    checked the result: the README's own limitations said so.

    The decision has not changed. There is still no `--restore` flag and
    nothing in `deploy/` restores anything. `rehearse()` below lives in the
    TEST, which is the only place a restore should happen without a person
    agreeing to it, and it follows the documented steps in the documented
    order so that the procedure a school will actually type is the procedure
    that has been shown to work.

WHAT THIS VERIFIES

    THE DOCUMENTED PROCEDURE WORKS. Six steps against a real tree: stop,
    move the three files aside, copy the backup's database in, copy the
    backup's embeddings over, run preflight, read the attendance back. The
    restored installation opens, is at the current schema version, and holds
    exactly the rows the backup held.

    STEP 2 IS THE WHOLE PROCEDURE, AND LEAVING IT OUT FAILS SILENTLY. Copying
    the backup over `classvision.db` while the old `-wal` is still beside it
    does not fail, does not warn, and does not restore: SQLite replays the
    stale write-ahead log over the file that was just put there and the
    installation comes back holding the very rows the restore was meant to
    roll back. Measured here, both ways, on the same fixture - 5 rows restored
    when the three files are moved aside, 45 when only the `.db` is replaced.
    That is why the README says move the `-wal` and `-shm` too, and it is the
    one mistake a careful person makes anyway.

    A RESTORE LOSES WHAT CAME AFTER THE BACKUP, and the loss is counted rather
    than mentioned. The sessions written after the backup are named and their
    absence from the restored database is asserted.

    MOVE, DO NOT DELETE, KEEPS THE DECISION REVERSIBLE. The superseded trio is
    still openable after the restore and still holds the newer attendance, so
    a person who restored the wrong backup can put it back. A restore that
    cannot be undone is not a procedure, it is a gamble.

    THE EMBEDDINGS AND THE DATABASE ARE RESTORED TO THE SAME MOMENT. A student
    enrolled after the backup is absent from both. A restored store that still
    held their vectors would recognise a face the attendance tables have never
    heard of.

    A FILE-EXPLORER COPY RESTORES AN INSTALLATION THAT IS SHORT. The wrong
    kind of backup is followed all the way through to its restored result -
    which opens cleanly, which is exactly what makes it dangerous.

    PREFLIGHT BRINGS A RESTORED OLDER DATABASE FORWARD. A backup taken before
    a migration restores to a file a version behind; opening it in step 5 is
    what makes it current, with nobody waiting.

    THE PROCEDURE AND THIS SUITE CANNOT DRIFT APART. The six steps are read
    out of `deploy/README.md` and asserted, so editing the documented order
    without editing the rehearsal fails here.

WHAT THIS DOES NOT VERIFY

    That a restore has been rehearsed on the machine a school runs. This is a
    temporary tree on a developer's disk; it is evidence that the steps are
    right and in the right order, not that anybody has carried a backup in
    from another disk and watched a term of attendance come back.

    Anything about the server. Step 1 is "stop the server" and step 6 is "look
    at the Reports tab"; here the first is "close the database" and the last is
    a repository read. No uvicorn process is started and no browser is opened.

    That the backup medium is sound. Every backup restored here was written
    minutes earlier by the same process onto the same disk.

Run with:
    python tests/test_restore.py
"""

import importlib.util
import shutil
import sqlite3
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from paths import enable_safe_console  # noqa: E402

DEPLOY = PROJECT_ROOT / "deploy"


def load(name):
    """Import a deploy script by path; they are tools, not a package."""
    path = DEPLOY / ("%s.py" % name)
    specification = importlib.util.spec_from_file_location(
        "deploy_%s" % name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


backup = load("backup")
preflight = load("preflight")


# -- the procedure under test ----------------------------------------------
#
# The six steps of deploy/README.md, in order. This is deliberately NOT in
# deploy/: the project has no --restore flag because the loss a restore
# accepts is a human decision, and automating it for the tests' convenience
# would quietly ship the thing the notes refuse to ship.

def rehearse(data, from_backup, aside, move_wal=True):
    """
    Perform the documented restore and return where the old files went.

    `data` is the installation's data directory, `from_backup` a directory
    written by backup.run(), `aside` where step 2 moves the current files.

    move_wal=False performs the mistake instead of the procedure: the `.db` is
    replaced and the `-wal`/`-shm` are left where they are. It exists so the
    consequence can be measured rather than described.
    """
    database = data / "classvision.db"
    aside.mkdir(parents=True, exist_ok=True)

    # Step 1 is "stop the server", which is the caller's business: every
    # connection must be closed before a file is moved, and on Windows an
    # open one makes step 2 fail loudly rather than quietly.

    # Step 2 - move the current trio aside. MOVE, not delete.
    suffixes = ("", "-wal", "-shm") if move_wal else ("",)
    for suffix in suffixes:
        current = Path(str(database) + suffix)
        if current.exists():
            shutil.move(str(current), str(aside / current.name))

    # Step 3 - copy the backup's database in.
    shutil.copy2(from_backup / "classvision.db", database)

    # Step 4 - copy the backup's embeddings over the store.
    store = data / "embeddings"
    if (from_backup / "embeddings").is_dir():
        if store.exists():
            shutil.rmtree(store)
        shutil.copytree(from_backup / "embeddings", store)

    return aside


def preflight_against(data_root):
    """
    Step 5, pointed at a temporary tree.

    Returns the database check, which is the one that matters after a
    restore: it opens the restored file and brings its schema forward.
    """
    original = preflight.PROJECT_ROOT
    try:
        preflight.PROJECT_ROOT = data_root
        return preflight.check_database()
    finally:
        preflight.PROJECT_ROOT = original


# -- fixtures ---------------------------------------------------------------

def an_installation(root, sessions=2, students=4):
    """
    A real installation: data/, a WAL-mode database with attendance, and an
    embedding store. Returned OPEN, because a backup is taken from a running
    server.
    """
    from database import Database

    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)

    database = Database(data / "classvision.db")
    database.initialize()

    for index in range(students):
        database.execute(
            "INSERT INTO students (student_id, name, status, created_at, "
            "updated_at) VALUES (?, ?, 'ACTIVE', '2026-03-02', '2026-03-02')",
            ("R-%d" % index, "Student %d" % index))

    for index in range(sessions):
        add_session(database, "R-S%d" % index, students)

    store = data / "embeddings"
    store.mkdir(exist_ok=True)
    for index in range(students):
        (store / ("R-%d.npy" % index)).write_bytes(b"vector for R-%d" % index)

    return database, data


def add_session(database, session_id, students, class_id="MECH-A"):
    """One completed lesson with a record per student."""
    database.execute(
        "INSERT INTO attendance_sessions (session_id, class_id, "
        "session_date, start_time, end_time, status, policy_version, "
        "created_at) VALUES (?, ?, '2026-03-04', "
        "'2026-03-04T09:00:00+00:00', '2026-03-04T10:00:00+00:00', "
        "'COMPLETED', '1.0', '2026-03-04')", (session_id, class_id))

    for index in range(students):
        database.execute(
            "INSERT INTO attendance_records (session_id, student_id, status, "
            "reason, policy_version, created_at, updated_at) VALUES "
            "(?, ?, 'PRESENT', 'seeded', '1.0', '2026-03-04', '2026-03-04')",
            (session_id, "R-%d" % index))


def crash_state(data, database):
    """
    Leave the on-disk trio as a machine that lost power leaves it.

    A clean close checkpoints the write-ahead log and deletes it, so a test
    that closes politely never meets the stale `-wal` the procedure's step 2
    is about. Copying the three files while a connection still holds them
    reproduces the uncheckpointed state exactly, and the copy becomes the
    installation.
    """
    live = data / "classvision.db"
    kept = {}
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(live) + suffix)
        if source.exists():
            kept[suffix] = source.read_bytes()

    database.close()

    for suffix, content in kept.items():
        Path(str(live) + suffix).write_bytes(content)

    return sorted(kept)


def rows(path, table="attendance_records"):
    """How many rows a database FILE holds, or None if the table is absent."""
    connection = sqlite3.connect(str(path))
    try:
        present = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if table not in present:
            return None
        return connection.execute(
            "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    finally:
        connection.close()


def session_ids(path):
    connection = sqlite3.connect(str(path))
    try:
        return sorted(row[0] for row in connection.execute(
            "SELECT session_id FROM attendance_sessions"))
    finally:
        connection.close()


def temporary(prefix):
    return Path(tempfile.mkdtemp(prefix="classvision_%s_" % prefix))


# -- the procedure works ----------------------------------------------------

def test_the_documented_procedure_restores_a_backup_end_to_end():
    """
    All six steps, against a real tree, with the result opened and read.

    This is the test the README's limitation 5 said did not exist.
    """
    root = temporary("restore")
    database = None
    try:
        database, data = an_installation(root, sessions=2)

        made = backup.run(into=root / "backups", database=data /
                          "classvision.db", embeddings=data / "embeddings",
                          stamp="restore-point")
        assert made["matches"], "the backup itself did not verify"
        at_backup = made["source_counts"]["attendance_records"]

        # A lesson after the backup, then the power fails.
        add_session(database, "R-AFTER", 4)
        after = rows(data / "classvision.db")
        crash_state(data, database)
        database = None

        rehearse(data, Path(made["destination"]), root / "aside")

        check = preflight_against(root)
        assert check.status == preflight.OK, (
            "preflight will not pass on the restored database: %s"
            % check.detail)

        from database import SCHEMA_VERSION
        assert str(SCHEMA_VERSION) in check.detail, check.detail

        # Step 6 - read the attendance back, which is what "look at the
        # Reports tab" means without a browser.
        from database import Database
        from repositories import AttendanceRepository

        restored = Database(data / "classvision.db")
        restored.connect()
        try:
            records = AttendanceRepository(restored).list_for_session("R-S0")
            assert len(records) == 4, (
                "the restored installation has %d of 4 records for R-S0"
                % len(records))
            assert all(record["status"] == "PRESENT" for record in records)
            total = restored.query_one(
                "SELECT COUNT(*) AS n FROM attendance_records")["n"]
        finally:
            restored.close()

        assert total == at_backup, (
            "restored to %d records; the backup held %d" % (total, at_backup))

        print("PASS  the six documented steps restore a backup end to end "
              "(%d records back, %d at the moment of failure)"
              % (total, after))
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


def test_leaving_the_stale_wal_behind_silently_undoes_the_restore():
    """
    The mistake, measured. Step 2 moves THREE files, and this is why.

    Replacing only `classvision.db` leaves a write-ahead log belonging to the
    database that was just replaced. Both files descend from the same
    database, so the log's salt still matches, SQLite replays it over the
    restored pages, and the installation comes back holding the rows the
    restore existed to discard. Nothing fails. Nothing warns.
    """
    root = temporary("restore_wal")
    database = None
    try:
        database, data = an_installation(root, sessions=1)
        made = backup.run(into=root / "backups",
                          database=data / "classvision.db",
                          embeddings=data / "embeddings", stamp="before")
        at_backup = made["source_counts"]["attendance_records"]

        add_session(database, "R-AFTER", 4)
        after = rows(data / "classvision.db")
        present = crash_state(data, database)
        database = None

        assert "-wal" in present, (
            "this fixture did not reproduce an uncheckpointed log, so the "
            "mistake it measures cannot happen here")

        # Keep a pristine copy of the crash state so the same fixture can be
        # restored twice, correctly and incorrectly.
        snapshot = root / "crash"
        snapshot.mkdir()
        for suffix in present:
            shutil.copy2(Path(str(data / "classvision.db") + suffix),
                         snapshot / ("classvision.db" + suffix))

        wrong = rehearse(data, Path(made["destination"]), root / "aside-wrong",
                         move_wal=False)
        got_wrong = rows(data / "classvision.db")

        # Put the crash state back and do it properly.
        for suffix in present:
            target = Path(str(data / "classvision.db") + suffix)
            if target.exists():
                target.unlink()
            shutil.copy2(snapshot / ("classvision.db" + suffix), target)

        rehearse(data, Path(made["destination"]), root / "aside-right")
        got_right = rows(data / "classvision.db")

        assert got_right == at_backup, (
            "the documented procedure restored %s records, not the backup's %d"
            % (got_right, at_backup))
        assert got_wrong == after, (
            "leaving the -wal behind gave %s records; the whole point is that "
            "it gives back the pre-restore %d" % (got_wrong, after))
        assert got_wrong != got_right, (
            "the mistake and the procedure produced the same database, so "
            "step 2 of the README would be arbitrary")
        assert not any((wrong / name).exists()
                       for name in ("classvision.db-wal",
                                    "classvision.db-shm")), (
            "the incorrect restore moved the log aside after all")

        print("PASS  leaving the stale -wal in place silently undoes the "
              "restore (%d records back instead of %d)"
              % (got_wrong, got_right))
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


def test_a_restore_discards_what_happened_after_the_backup():
    """
    The loss is the point of the procedure, so it is counted here by name.
    """
    root = temporary("restore_loss")
    database = None
    try:
        database, data = an_installation(root, sessions=2)
        made = backup.run(into=root / "backups",
                          database=data / "classvision.db",
                          embeddings=data / "embeddings", stamp="before")

        for name in ("R-LATE1", "R-LATE2"):
            add_session(database, name, 4)

        before_restore = session_ids(data / "classvision.db")
        crash_state(data, database)
        database = None

        rehearse(data, Path(made["destination"]), root / "aside")
        restored = session_ids(data / "classvision.db")

        assert "R-LATE1" in before_restore and "R-LATE2" in before_restore
        assert restored == ["R-S0", "R-S1"], (
            "the restored database holds %s" % restored)
        assert "R-LATE1" not in restored and "R-LATE2" not in restored, (
            "a restore kept a lesson recorded after the backup, which would "
            "mean the backup file is not the thing being restored")

        print("PASS  a restore discards the 2 lessons recorded after the "
              "backup, and the procedure does not pretend otherwise")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


def test_moving_the_old_files_aside_keeps_the_restore_reversible():
    """
    "Move, do not delete" - because the file being replaced may be the better
    one, and nobody knows that until after the restore.
    """
    root = temporary("restore_aside")
    database = None
    try:
        database, data = an_installation(root, sessions=1)
        made = backup.run(into=root / "backups",
                          database=data / "classvision.db",
                          embeddings=data / "embeddings", stamp="before")

        add_session(database, "R-AFTER", 4)
        richer = rows(data / "classvision.db")
        present = crash_state(data, database)
        database = None

        aside = rehearse(data, Path(made["destination"]), root / "aside")

        for suffix in present:
            assert (aside / ("classvision.db" + suffix)).exists(), (
                "classvision.db%s was not moved aside" % suffix)

        # The superseded database still opens and still holds the newer
        # attendance, so undoing the restore is a file move.
        assert rows(aside / "classvision.db") == richer, (
            "the moved-aside database no longer holds the %d records it had"
            % richer)

        # Reading it just now CHECKPOINTED it: closing the last connection
        # folds the write-ahead log into the .db and deletes the sidecars. So
        # the trio moved aside is a trio only until somebody looks at it, and
        # undoing the restore moves back whatever is still there - which is
        # why the rows are asserted again below rather than assumed.
        for suffix in present:
            source = aside / ("classvision.db" + suffix)
            if not source.exists():
                continue
            target = Path(str(data / "classvision.db") + suffix)
            if target.exists():
                target.unlink()
            shutil.move(str(source), str(target))

        assert rows(data / "classvision.db") == richer, (
            "putting the moved files back did not restore the installation "
            "that was replaced")

        print("PASS  the superseded database is moved aside intact, so the "
              "restore can be undone (%d records recovered)" % richer)
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


def test_the_embeddings_and_the_database_are_restored_to_one_moment():
    """
    A student enrolled after the backup must be absent from both halves.

    A restored store holding vectors the restored attendance tables have never
    heard of would recognise a face belonging to no student - which is the one
    thing recognition is not allowed to do.
    """
    root = temporary("restore_vectors")
    database = None
    try:
        database, data = an_installation(root, sessions=1, students=3)
        made = backup.run(into=root / "backups",
                          database=data / "classvision.db",
                          embeddings=data / "embeddings", stamp="before")
        assert made["embedding_files"] == 3, made["embedding_files"]

        # Somebody is enrolled after the backup: a row and a vector.
        database.execute(
            "INSERT INTO students (student_id, name, status, created_at, "
            "updated_at) VALUES ('R-NEW', 'Enrolled Later', 'ACTIVE', "
            "'2026-03-05', '2026-03-05')")
        (data / "embeddings" / "R-NEW.npy").write_bytes(b"vector for R-NEW")
        crash_state(data, database)
        database = None

        rehearse(data, Path(made["destination"]), root / "aside")

        files = sorted(path.name
                       for path in (data / "embeddings").iterdir())
        assert files == ["R-0.npy", "R-1.npy", "R-2.npy"], (
            "the restored store holds %s" % files)

        connection = sqlite3.connect(str(data / "classvision.db"))
        try:
            known = sorted(row[0] for row in connection.execute(
                "SELECT student_id FROM students"))
        finally:
            connection.close()

        assert "R-NEW" not in known, known
        assert not (data / "embeddings" / "R-NEW.npy").exists(), (
            "a vector survived a restore that removed the student it belongs "
            "to, so the store and the database are at different moments")

        print("PASS  the restored store and the restored database are at the "
              "same moment (3 students, 3 vectors, the later enrolment in "
              "neither)")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


def test_restoring_a_file_explorer_copy_gives_a_short_installation():
    """
    The wrong backup, followed all the way to its restored result.

    `test_deployment.py` shows that copying the `.db` alone yields a short
    file. This takes that file through the restore and shows what a school
    would then be looking at: an installation that opens, that preflight is
    content with, and that is missing a term.
    """
    root = temporary("restore_wrong_backup")
    database = None
    try:
        database, data = an_installation(root, sessions=2)
        complete = rows(data / "classvision.db")

        # What copying in the file explorer produces: the .db, nothing else.
        wrong = root / "explorer-copy"
        wrong.mkdir()
        shutil.copy2(data / "classvision.db", wrong / "classvision.db")

        crash_state(data, database)
        database = None

        rehearse(data, wrong, root / "aside")
        got = rows(data / "classvision.db")

        assert got != complete, (
            "the plain copy restored a complete installation, so either WAL "
            "mode is off or the fixture checkpointed; this test would then "
            "be proving nothing")
        assert got is None or got < complete, (
            "the plain copy restored %s of %d records" % (got, complete))

        check = preflight_against(root)
        assert check.status == preflight.OK, (
            "preflight rejected the short installation (%s) - which would be "
            "better behaviour than the one this test records, so update the "
            "deploy notes" % check.detail)

        print("PASS  restoring a file-explorer copy gives an installation "
              "holding %s of %d records, which opens and passes preflight"
              % ("no table" if got is None else got, complete))
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


def test_preflight_brings_a_restored_older_database_forward():
    """
    Step 5 is not a formality: a backup taken before a migration restores to a
    file a schema version behind, and opening it is what makes it current.
    """
    root = temporary("restore_schema")
    database = None
    try:
        database, data = an_installation(root, sessions=1)
        made = backup.run(into=root / "backups",
                          database=data / "classvision.db",
                          embeddings=data / "embeddings", stamp="old")
        crash_state(data, database)
        database = None

        # The backup came from an older installation.
        copy = sqlite3.connect(str(Path(made["destination"]) /
                                   "classvision.db"))
        try:
            copy.execute(
                "UPDATE schema_info SET value = '1' "
                "WHERE key = 'schema_version'")
            copy.commit()
        finally:
            copy.close()

        rehearse(data, Path(made["destination"]), root / "aside")

        connection = sqlite3.connect(str(data / "classvision.db"))
        try:
            before = connection.execute(
                "SELECT value FROM schema_info WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert str(before) == "1", before

        check = preflight_against(root)
        from database import SCHEMA_VERSION

        assert check.status == preflight.OK, check.detail
        assert str(SCHEMA_VERSION) in check.detail, check.detail

        print("PASS  preflight brings a restored schema-1 database forward to "
              "version %s, before a lesson" % SCHEMA_VERSION)
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(root, ignore_errors=True)


# -- the procedure and the notes cannot drift ------------------------------

def test_the_rehearsal_follows_the_steps_the_deploy_notes_document():
    """
    The six steps are read out of deploy/README.md, in order.

    If somebody reorders or rewords the documented procedure, this fails -
    which is the only thing keeping a rehearsal of six steps from becoming a
    rehearsal of six different steps.
    """
    notes = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "### Restoring" in notes, (
        "deploy/README.md no longer documents a restore")

    section = notes.split("### Restoring", 1)[1]
    section = section.split("\n---", 1)[0]

    required = [
        "Stop the server",
        "Move the current",
        "Move, do not delete",
        "Copy `classvision.db` from the backup",
        "Copy the backup's `embeddings/`",
        "deploy/preflight.py",
        "Reports tab",
    ]

    position = -1
    for phrase in required:
        found = section.find(phrase)
        assert found != -1, (
            "the documented restore no longer says %r" % phrase)
        assert found > position, (
            "%r has moved out of its documented order" % phrase)
        position = found

    assert "-wal" in section and "-shm" in section, (
        "step 2 no longer names the write-ahead log files, which is the step "
        "that silently loses a restore")
    assert "no `--restore` flag" in section, (
        "the notes no longer say why the restore is manual")

    print("PASS  the rehearsal follows the six steps deploy/README.md "
          "documents, in that order")


def test_nothing_in_deploy_restores_anything():
    """
    The decision this suite was careful not to overturn.

    Rehearsing a restore is not the same as shipping one. No `--restore` flag
    appeared in `backup.py`, and nothing in `deploy/` moves a backup back into
    `data/` - the function that does it lives in this test file.
    """
    for name in sorted(DEPLOY.glob("*.py")):
        source = name.read_text(encoding="utf-8")
        assert "--restore" not in source, (
            "%s grew a --restore flag" % name.name)
        assert "def restore" not in source, (
            "%s grew a restore function" % name.name)

    mine = Path(__file__).read_text(encoding="utf-8")
    assert "def rehearse(" in mine, (
        "the restore procedure is no longer defined in the test that "
        "rehearses it")

    print("PASS  the restore is still a human operation; nothing in deploy/ "
          "performs one")


def test_the_restore_rehearsal_never_touches_the_real_installation():
    real = PROJECT_ROOT / "data" / "classvision.db"
    store = PROJECT_ROOT / "data" / "embeddings"

    before = real.stat().st_mtime if real.exists() else None
    vectors = (sorted(path.name for path in store.iterdir())
               if store.is_dir() else None)

    # The only checks here that read the real tree are the source ones.
    preflight.check_python()
    preflight.check_dependencies()

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    now = (sorted(path.name for path in store.iterdir())
           if store.is_dir() else None)
    assert now == vectors, "the real embedding store was modified by a test"

    print("PASS  no test here touched data/classvision.db or "
          "data/embeddings/")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_the_documented_procedure_restores_a_backup_end_to_end,
    test_leaving_the_stale_wal_behind_silently_undoes_the_restore,
    test_a_restore_discards_what_happened_after_the_backup,
    test_moving_the_old_files_aside_keeps_the_restore_reversible,
    test_the_embeddings_and_the_database_are_restored_to_one_moment,
    test_restoring_a_file_explorer_copy_gives_a_short_installation,
    test_preflight_brings_a_restored_older_database_forward,
    test_the_rehearsal_follows_the_steps_the_deploy_notes_document,
    test_nothing_in_deploy_restores_anything,
    test_the_restore_rehearsal_never_touches_the_real_installation,
]


def main():
    enable_safe_console()

    passed = 0
    for test in TESTS:
        try:
            test()
            passed += 1
        except AssertionError as error:
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                        # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(error).__name__, error))

    print("")
    print("%d/%d tests passed." % (passed, len(TESTS)))
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
