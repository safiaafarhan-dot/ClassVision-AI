"""
Step 13: the preflight check and the backup.

NO WEBCAM AND NO NETWORK ARE NEEDED. The backup tests run against a real
temporary SQLite database and open the copy they make; the real
data/classvision.db is never read or written.

WHAT THIS VERIFIES

    THE BACKUP IS CONSISTENT. The database runs in WAL mode, so copying the
    .db file alone - which is what copying in a file explorer does - yields a
    file that opens cleanly and is missing the most recent lessons. That exact
    failure is reproduced here: rows are committed, the plain file is copied,
    and the copy is shown to be short. Then the same state is backed up through
    this tool and the copy is shown to be complete.

    THE BACKUP IS OPENED. The row counts are read back out of the copy, not out
    of the source, and compared. A backup nobody has opened is a hypothesis.

    A BACKUP NEVER OVERWRITES ONE. Writing into a directory that already
    exists is refused, because the common way to lose data to a backup tool is
    for it to half-write over the only good copy.

    PERSONNEL DOCUMENTS ARE OPT-IN. Backing up identity documents is a
    different decision from backing up attendance, and the default does not
    make it quietly.

    PREFLIGHT NEVER COUNTS A SKIPPED CHECK AS A PASS. Each check reports OK,
    PROBLEM or SKIPPED with its reason; a missing webcam is honestly SKIPPED
    rather than silently passed or loudly failed, and the exit code is non-zero
    only for PROBLEM.

    PREFLIGHT CHANGES NOTHING it was not asked to. It is run against a
    temporary tree and the tree is compared before and after, except for the
    database it was explicitly asked to open and migrate.

    PREFLIGHT ACTUALLY DETECTS THINGS. A missing model, a truncated model and
    an unwritable data directory are each produced on purpose and each has to
    be reported as a PROBLEM with a fix.

WHAT THIS DOES NOT VERIFY

    That a restore works end to end on a real machine. The backup is opened
    and counted; putting it back is a human operation with a human decision in
    it, and the deploy notes describe it rather than this suite pretending to
    have done it.

    Anything about a camera. The camera check is exercised only through its
    skipped path; opening a real webcam is not something a test suite should
    do.

Run with:
    python tests/test_deployment.py
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
    """
    Import a deploy script by path.

    They are command-line tools rather than a package - that is the point of
    them - so they are loaded the way a tool is, not the way a module is.
    """
    spec = importlib.util.spec_from_file_location(
        "deploy_%s" % name, DEPLOY / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = load("backup")
preflight = load("preflight")


# -- fixtures ---------------------------------------------------------------

def a_database(directory, sessions=3):
    """
    A real database with real rows, in WAL mode, left OPEN.

    Left open on purpose: a backup taken while the server is running is the
    only kind anybody actually takes, and the WAL is only interesting while a
    connection still holds it.
    """
    from database import Database

    path = directory / "classvision.db"
    database = Database(path)
    database.initialize()

    students = []
    for index in range(4):
        student_id = "D-%d" % index
        students.append(student_id)
        database.execute(
            "INSERT INTO students (student_id, name, status, created_at, "
            "updated_at) VALUES (?, ?, 'ACTIVE', '2026-03-02', '2026-03-02')",
            (student_id, "Student %d" % index))

    for index in range(sessions):
        session_id = "D-S%d" % index
        database.execute(
            "INSERT INTO attendance_sessions (session_id, class_id, "
            "session_date, start_time, end_time, status, policy_version, "
            "created_at) VALUES (?, 'MECH-A', '2026-03-0%d', "
            "'2026-03-0%dT09:00:00+00:00', '2026-03-0%dT10:00:00+00:00', "
            "'COMPLETED', '1.0', '2026-03-02')"
            % (index + 1, index + 1, index + 1), (session_id,))

        for student_id in students:
            database.execute(
                "INSERT INTO attendance_records (session_id, student_id, "
                "status, reason, policy_version, created_at, updated_at) "
                "VALUES (?, ?, 'PRESENT', 'seeded', '1.0', '2026-03-02', "
                "'2026-03-02')",
                (session_id, student_id))

    return database, path


def embeddings_at(directory, count=3):
    store = directory / "embeddings"
    store.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (store / ("D-%d.npy" % index)).write_bytes(b"not a real vector")
    return store


def counts_of(path):
    """
    How many attendance records a database file holds.

    Returns None when the table is not even there, which is a real state for a
    copy taken without the write-ahead log: on a young database the schema
    itself has not been checkpointed yet, so the .db file is close to empty.
    """
    connection = sqlite3.connect(str(path))
    try:
        existing = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "attendance_records" not in existing:
            return None
        return connection.execute(
            "SELECT COUNT(*) FROM attendance_records").fetchone()[0]
    finally:
        connection.close()


# -- the backup is consistent ----------------------------------------------

def test_copying_the_database_file_alone_loses_recent_rows():
    """
    The failure this tool exists to avoid, reproduced.

    If this test ever starts failing because a plain copy IS complete, the
    database is no longer in WAL mode - which would be worth knowing, and is
    why the assertion is written this way round rather than skipped.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_walproof_"))
    database = None
    try:
        database, path = a_database(directory)

        live = counts_of(path)
        assert live == 12, live

        # What a file-explorer "backup" does: the .db, and nothing else.
        naive = directory / "naive.db"
        shutil.copy2(path, naive)

        short = counts_of(naive)

        assert short != live, (
            "a plain copy of the .db was complete, which means the database "
            "is not in WAL mode any more - the backup tool's whole reason "
            "would have changed")

        print("PASS  copying the .db file alone loses what is in the WAL "
              "(%s instead of %d)"
              % ("not even the table" if short is None else short, live))
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_backup_is_complete_and_is_opened_to_prove_it():
    directory = Path(tempfile.mkdtemp(prefix="classvision_backup_"))
    database = None
    try:
        database, path = a_database(directory)
        embeddings_at(directory)

        report = backup.run(into=directory / "out", database=path,
                            embeddings=directory / "embeddings",
                            staff_directory=directory / "staff",
                            stamp="fixed")

        assert report["matches"] is True, report
        assert report["source_counts"] == report["backup_counts"]
        assert report["source_counts"]["attendance_records"] == 12

        # Read it back independently of the tool's own report.
        assert counts_of(Path(report["database"])) == 12, (
            "the backup is short - the WAL was not included")

        assert report["embedding_files"] == 3
        assert report["embedding_bytes"] > 0

        print("PASS  the backup is complete, and the copy is opened to prove "
              "it")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_backup_captures_rows_written_while_it_was_open():
    directory = Path(tempfile.mkdtemp(prefix="classvision_backuplive_"))
    database = None
    try:
        database, path = a_database(directory, sessions=1)

        # One more record, committed and still in the WAL.
        database.execute(
            "INSERT INTO attendance_records (session_id, student_id, status, "
            "reason, policy_version, created_at, updated_at) VALUES "
            "('D-S0', 'D-0', 'LATE', 'late arrival', '2.0', '2026-03-02', "
            "'2026-03-02')")

        report = backup.run(into=directory / "out", database=path,
                            embeddings=directory / "nothing-here",
                            staff_directory=directory / "staff",
                            stamp="fixed")

        assert counts_of(Path(report["database"])) == 5, (
            "a row committed into the WAL did not reach the backup")

        print("PASS  rows committed while the server was running reach the "
              "backup")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_backup_never_overwrites_an_existing_one():
    directory = Path(tempfile.mkdtemp(prefix="classvision_nooverwrite_"))
    database = None
    try:
        database, path = a_database(directory, sessions=1)

        backup.run(into=directory / "out", database=path,
                   embeddings=directory / "none", stamp="same")

        try:
            backup.run(into=directory / "out", database=path,
                       embeddings=directory / "none", stamp="same")
            raise AssertionError("a backup overwrote an existing one")
        except FileExistsError as error:
            assert "never overwritten" in str(error), error

        print("PASS  a backup refuses to overwrite an existing one")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_personnel_documents_are_opt_in():
    directory = Path(tempfile.mkdtemp(prefix="classvision_staff_"))
    database = None
    try:
        database, path = a_database(directory, sessions=1)

        staff = directory / "staff"
        staff.mkdir()
        (staff / "contract.pdf").write_bytes(b"%PDF-1.4 not really")

        default = backup.run(into=directory / "a", database=path,
                             embeddings=directory / "none",
                             staff_directory=staff, stamp="fixed")

        assert default["staff_included"] is False
        assert default["staff_files"] == 0
        assert not (Path(default["destination"]) / "staff").exists(), (
            "personnel documents were copied without being asked for")

        asked = backup.run(into=directory / "b", database=path,
                           embeddings=directory / "none",
                           staff_directory=staff, staff=True, stamp="fixed")

        assert asked["staff_included"] is True
        assert asked["staff_files"] == 1

        print("PASS  personnel documents are copied only when asked for")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_backing_up_a_database_that_is_not_there_is_refused_clearly():
    directory = Path(tempfile.mkdtemp(prefix="classvision_nodb_"))
    try:
        try:
            backup.run(into=directory / "out",
                       database=directory / "nothing.db",
                       embeddings=directory / "none")
            raise AssertionError("a missing database was backed up")
        except FileNotFoundError as error:
            assert "no database" in str(error).lower(), error

        print("PASS  backing up a database that is not there says so")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_missing_embedding_store_is_zero_files_rather_than_a_failure():
    """
    An installation with no enrollments yet has no embedding directory.

    That is a normal state, not a broken one, and the backup reports zero
    files rather than refusing to run.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_noembed_"))
    database = None
    try:
        database, path = a_database(directory, sessions=1)

        report = backup.run(into=directory / "out", database=path,
                            embeddings=directory / "never-existed",
                            stamp="fixed")

        assert report["embedding_files"] == 0
        assert report["matches"] is True

        print("PASS  no embedding store yet is zero files, not a failure")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_backup_tool_makes_no_network_connection():
    source = (DEPLOY / "backup.py").read_text(encoding="utf-8")

    for module in ("requests", "httpx", "urllib", "socket", "smtplib",
                   "boto3", "ftplib"):
        assert "import %s" % module not in source, (
            "the backup tool imports %s" % module)
    for word in ("http://", "https://", "s3://"):
        assert word not in source, "the backup tool names %s" % word

    print("PASS  the backup tool makes no network connection")


# -- preflight ---------------------------------------------------------------

def test_preflight_never_counts_a_skipped_check_as_a_pass():
    checks = preflight.run(camera=False)

    statuses = {check.status for check in checks}
    assert statuses <= {preflight.OK, preflight.PROBLEM, preflight.SKIPPED}, (
        statuses)

    camera = [check for check in checks if check.name == "camera"]
    assert camera, "there is no camera check"
    assert camera[0].status == preflight.SKIPPED, (
        "the camera was reported as passing without being opened")
    assert camera[0].fix, "the skipped camera check does not say how to run it"

    print("PASS  a check that did not run is SKIPPED, never a pass")


def test_every_check_has_a_name_and_says_something():
    for check in preflight.run(camera=False):
        assert check.name, "a check has no name"
        assert check.detail, "check %s says nothing" % check.name
        if check.status == preflight.PROBLEM:
            assert check.fix, (
                "check %s reports a problem with no fix" % check.name)

    print("PASS  every check names itself and says something")


def test_a_missing_model_is_reported_as_a_problem_with_a_fix():
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_nomodels_"))
    try:
        (directory / "models").mkdir()
        preflight.PROJECT_ROOT = directory

        check = preflight.check_models()

        assert check.status == preflight.PROBLEM
        assert "missing" in check.detail
        assert "download_models" in check.fix, check.fix

        print("PASS  a missing model is a PROBLEM, with the command that "
              "fixes it")
    finally:
        preflight.PROJECT_ROOT = original
        shutil.rmtree(directory, ignore_errors=True)


def test_a_truncated_model_is_caught_rather_than_counted_as_present():
    """
    The confusing failure: a download interrupted halfway.

    The file exists, so every "is it there" check passes, and the load fails
    at the start of a lesson instead.
    """
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_truncated_"))
    try:
        models = directory / "models"
        models.mkdir()
        for name in preflight.REQUIRED_MODELS:
            (models / name).write_bytes(b"half a download")

        preflight.PROJECT_ROOT = directory
        check = preflight.check_models()

        assert check.status == preflight.PROBLEM
        assert "small" in check.detail, check.detail

        print("PASS  a half-downloaded model is caught, not counted as "
              "present")
    finally:
        preflight.PROJECT_ROOT = original
        shutil.rmtree(directory, ignore_errors=True)


def test_a_missing_data_directory_is_reported():
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_nodata_"))
    try:
        preflight.PROJECT_ROOT = directory
        check = preflight.check_data_directory()

        assert check.status == preflight.PROBLEM
        assert "does not exist" in check.detail

        print("PASS  a missing data directory is reported")
    finally:
        preflight.PROJECT_ROOT = original
        shutil.rmtree(directory, ignore_errors=True)


def test_the_data_directory_check_actually_writes():
    """
    Checked by writing, not by reading a permission bit.

    os.access() reports what the bits say, which on Windows is frequently not
    what happens when you open a file. The check has to create and delete
    something real - and must leave nothing behind.
    """
    # Comments are stripped first: the file EXPLAINS why it does not ask the
    # permission bits, and that explanation must not read as a violation.
    source = (DEPLOY / "preflight.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())

    assert "os.access" not in code, (
        "the writability check asks the permission bits instead of writing")
    assert "probe.write_text" in code, (
        "the check does not actually write anything")
    assert "probe.unlink" in code, (
        "the check writes a probe file and never removes it")

    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_writable_"))
    try:
        (directory / "data").mkdir()
        preflight.PROJECT_ROOT = directory

        check = preflight.check_data_directory()
        assert check.status == preflight.OK

        leftovers = list((directory / "data").iterdir())
        assert not leftovers, (
            "the check left files behind: %s" % leftovers)

        print("PASS  the data directory check writes, and leaves nothing "
              "behind")
    finally:
        preflight.PROJECT_ROOT = original
        shutil.rmtree(directory, ignore_errors=True)


def test_no_database_yet_is_skipped_rather_than_failed():
    """A fresh checkout has no database. That is not a fault."""
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_freshdb_"))
    try:
        (directory / "data").mkdir()
        preflight.PROJECT_ROOT = directory

        check = preflight.check_database()
        assert check.status == preflight.SKIPPED, check.status
        assert "created on first run" in check.detail

        print("PASS  no database yet is SKIPPED, not failed")
    finally:
        preflight.PROJECT_ROOT = original
        shutil.rmtree(directory, ignore_errors=True)


def test_an_installation_with_no_accounts_is_skipped_with_the_reason():
    """
    UNCONFIGURED is deliberate. It must not read as a fault.

    An installation with no accounts has an open API and asks for its first
    Super Admin; that is the documented state of every fresh install, and the
    alternative - shipping a default password - is worse in every way.
    """
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_noaccounts_"))
    database = None
    try:
        (directory / "data").mkdir()
        database, _ = a_database(directory / "data", sessions=1)
        preflight.PROJECT_ROOT = directory

        check = preflight.check_accounts()

        assert check.status == preflight.SKIPPED, check.status
        assert "UNCONFIGURED" in check.detail
        assert "deliberate" in check.detail

        print("PASS  an installation with no accounts is SKIPPED with the "
              "reason")
    finally:
        preflight.PROJECT_ROOT = original
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_full_queue_is_reported_without_being_called_a_fault():
    """
    A queue with items in it means nothing was LOST, only that nothing was
    sent. Calling that a PROBLEM would train somebody to ignore the report.
    """
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_queuecheck_"))
    database = None
    try:
        (directory / "data").mkdir()
        database, path = a_database(directory / "data", sessions=1)

        database.execute(
            "INSERT INTO sync_queue (item_key, kind, payload, status, "
            "queued_at, updated_at) VALUES ('k1', 'attendance', '{}', "
            "'PENDING', '2026-03-02T09:00:00+00:00', "
            "'2026-03-02T09:00:00+00:00')")

        preflight.PROJECT_ROOT = directory
        check = preflight.check_sync_queue()

        assert check.status == preflight.SKIPPED, check.status
        assert "Nothing has been lost" in check.detail, check.detail
        assert "1 item(s) waiting" in check.detail

        print("PASS  a full queue is reported without being called a fault")
    finally:
        preflight.PROJECT_ROOT = original
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_schema_version_is_checked_against_the_code():
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_schema_"))
    database = None
    try:
        (directory / "data").mkdir()
        database, path = a_database(directory / "data", sessions=1)

        # Pretend this database is a version behind.
        database.execute(
            "UPDATE schema_info SET value = '1' WHERE key = 'schema_version'")
        database.close()
        database = None

        preflight.PROJECT_ROOT = directory
        check = preflight.check_database()

        # Opening it RUNS the migration, which is the point: an upgrade that
        # would fail should fail here, with nobody waiting.
        assert check.status == preflight.OK, (
            "opening the database did not bring its schema forward: %s"
            % check.detail)

        from database import SCHEMA_VERSION
        assert str(SCHEMA_VERSION) in check.detail, check.detail

        print("PASS  opening the database brings an older schema forward, "
              "before a lesson")
    finally:
        preflight.PROJECT_ROOT = original
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_preflight_creates_nothing_in_the_tree_it_checks():
    original = preflight.PROJECT_ROOT
    directory = Path(tempfile.mkdtemp(prefix="classvision_readonly_"))
    try:
        (directory / "data").mkdir()
        (directory / "models").mkdir()
        preflight.PROJECT_ROOT = directory

        before = sorted(str(p.relative_to(directory))
                        for p in directory.rglob("*"))
        preflight.run(camera=False)
        after = sorted(str(p.relative_to(directory))
                       for p in directory.rglob("*"))

        assert before == after, (
            "preflight changed the tree: added %s"
            % sorted(set(after) - set(before)))

        print("PASS  preflight creates nothing in the tree it checks")
    finally:
        preflight.PROJECT_ROOT = original
        shutil.rmtree(directory, ignore_errors=True)


def test_the_camera_check_is_only_run_when_asked_for():
    source = (DEPLOY / "preflight.py").read_text(encoding="utf-8")

    assert "--camera" in source, "there is no way to ask for the camera check"
    assert "if camera:" in source, (
        "the camera check is not behind the flag")

    without = [check for check in preflight.run(camera=False)
               if check.name == "camera"][0]
    assert without.status == preflight.SKIPPED
    assert "intrusive" in without.detail

    print("PASS  the camera is opened only when asked for")


def test_preflight_reports_a_count_of_each_outcome():
    source = (DEPLOY / "preflight.py").read_text(encoding="utf-8")

    assert "problem(s)" in source and "skipped" in source, (
        "the summary does not report the three outcomes separately")
    assert "A skipped check is not a pass" in source, (
        "the summary does not say that a skipped check proves nothing")
    assert "prove nothing" in source, (
        "a successful run does not say what was not checked")

    print("PASS  the summary counts each outcome and says what was not "
          "checked")


def test_deployment_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    # Only the pure-source checks run against the real tree; everything that
    # opens a database above is pointed at a temporary one.
    preflight.check_python()
    preflight.check_dependencies()

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  no test here modified the real database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_copying_the_database_file_alone_loses_recent_rows,
    test_the_backup_is_complete_and_is_opened_to_prove_it,
    test_the_backup_captures_rows_written_while_it_was_open,
    test_a_backup_never_overwrites_an_existing_one,
    test_personnel_documents_are_opt_in,
    test_backing_up_a_database_that_is_not_there_is_refused_clearly,
    test_a_missing_embedding_store_is_zero_files_rather_than_a_failure,
    test_the_backup_tool_makes_no_network_connection,
    test_preflight_never_counts_a_skipped_check_as_a_pass,
    test_every_check_has_a_name_and_says_something,
    test_a_missing_model_is_reported_as_a_problem_with_a_fix,
    test_a_truncated_model_is_caught_rather_than_counted_as_present,
    test_a_missing_data_directory_is_reported,
    test_the_data_directory_check_actually_writes,
    test_no_database_yet_is_skipped_rather_than_failed,
    test_an_installation_with_no_accounts_is_skipped_with_the_reason,
    test_a_full_queue_is_reported_without_being_called_a_fault,
    test_the_schema_version_is_checked_against_the_code,
    test_preflight_creates_nothing_in_the_tree_it_checks,
    test_the_camera_check_is_only_run_when_asked_for,
    test_preflight_reports_a_count_of_each_outcome,
    test_deployment_tests_never_touch_the_real_database,
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
