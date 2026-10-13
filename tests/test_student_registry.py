"""
Student registry tests - the registry as a GENERAL system.

NO WEBCAM AND NO MODEL ARE NEEDED. Every test uses its own temporary SQLite
file and its own temporary embedding store; neither data/classvision.db nor
data/embeddings is ever opened.

WHAT THIS VERIFIES
    That SQLite is the single source of truth for who a student is, that an
    older database upgrades to the current schema - from version 1 and from
    version 2 - without destroying anything, that existing enrollments are
    filled in rather than reinvented, and that all of it works for ARBITRARY
    students - created here with
    generated ids and unrelated names, so a fix that only worked for one
    particular student would fail every test in this file.

    No test here mentions a real student. The fixtures build STU_TEST_001,
    STU_TEST_002 and STU_TEST_003 across two classes and three sections, and
    a helper generates more on demand.

WHAT THIS DOES NOT VERIFY
    Recognition accuracy, or that a face enrollment identifies anybody. That
    is Step 4's question and is unchanged.

Run with:
    python tests/test_student_registry.py
"""

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

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from database import SCHEMA_VERSION, Database  # noqa: E402
from embedding_store import EmbeddingStore  # noqa: E402
from migrate_students import synchronise_registry  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import enrollment_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

# Three test students, deliberately unlike each other: two classes, three
# sections, different roll formats. Nothing in the implementation may depend
# on any particular one of them.
FIXTURES = [
    ("STU_TEST_001", "Aarav Menon", "R-11", "MECH", "A"),
    ("STU_TEST_002", "Brigid O'Hare", "0002", "MECH", "B"),
    ("STU_TEST_003", "Chen Wei", "c3", "CIVIL", "A"),
]


# -- fixtures ---------------------------------------------------------------

def temp_environment():
    """A private database, a private embedding store, and a client for both."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_registry_"))

    # Several service functions import EmbeddingStore lazily and construct it
    # with no argument, so the honest way to redirect them all at once is the
    # module global they end up reading.
    import embedding_store

    original = embedding_store.EMBEDDINGS_DIR
    embedding_store.EMBEDDINGS_DIR = directory / "embeddings"

    store = EmbeddingStore(directory / "embeddings")

    state.configure(directory / "registry.db")

    from backend.app import app
    client = TestClient(app)

    return {"directory": directory, "store": store, "client": client,
            "original": original}


def teardown(environment):
    import embedding_store
    embedding_store.EMBEDDINGS_DIR = environment["original"]
    try:
        environment["client"].close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(environment["directory"], ignore_errors=True)


def register(client, fixture):
    student_id, name, roll, class_name, section = fixture
    return client.post("/api/students", json={
        "student_id": student_id, "name": name, "roll_number": roll,
        "class_name": class_name, "section": section})


def register_all(client, fixtures=None):
    return [register(client, fixture) for fixture in (fixtures or FIXTURES)]


def generated(index):
    """A student nobody wrote into the implementation. Used to prove that."""
    return ("GEN%04d" % index, "Generated Person %d" % index,
            "G%d" % index, "CLASS%d" % (index % 3), chr(65 + index % 4))


# -- the migration ----------------------------------------------------------

def test_a_version_one_database_gains_the_registry_columns():
    """
    The exact shape of the schema this project shipped before, migrated.

    Built by hand rather than by calling initialize(), so the test is a real
    version-1 database and not a version-2 one with columns removed.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_v1_"))
    try:
        path = directory / "old.db"
        raw = sqlite3.connect(str(path))
        raw.executescript("""
            CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE students (
                student_id TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            INSERT INTO schema_info VALUES ('schema_version', '1');
            INSERT INTO students VALUES
                ('OLD001', 'Existing One', 'ACTIVE', 'then', 'then'),
                ('OLD002', 'Existing Two', 'INACTIVE', 'then', 'then');
        """)
        raw.commit()
        raw.close()

        database = Database(path)
        database.connect()
        try:
            assert database.column_names("students") == [
                "student_id", "name", "status", "created_at", "updated_at"], (
                "the fixture is not a version-1 students table")

            added = database.migrate()
            # Version 2 added the three registry columns; version 3 added
            # parent/guardian contact; version 7 added the academic year, so
            # the registry can be filtered by the year a student is in. A
            # version-1 file jumps straight to the current schema, so it
            # gains all eight at once. The session lifecycle columns are not
            # here because this fixture has no attendance_sessions table -
            # migrating a table that is absent is skipped, not an error.
            assert sorted(added) == [
                "academic_year", "class_name", "notification_enabled",
                "parent_email", "parent_name", "parent_phone", "roll_number",
                "section"], (
                "the migration added %s" % added)

            columns = database.column_names("students")
            for column in ("student_id", "name", "roll_number", "class_name",
                           "section", "academic_year", "status", "created_at",
                           "updated_at", "parent_name", "parent_phone",
                           "parent_email", "notification_enabled"):
                assert column in columns, "no %r column after migrating" % column

            rows = {row["student_id"]: dict(row) for row in
                    database.query("SELECT * FROM students")}
            assert len(rows) == 2, "the migration lost a student"
            assert rows["OLD001"]["name"] == "Existing One"
            assert rows["OLD002"]["status"] == "INACTIVE", (
                "the migration changed a student's status")
            assert rows["OLD001"]["roll_number"] == "", (
                "the new column should default to empty, not NULL")
            # Version 7's column, on a database with no history to backfill
            # from. '' is the honest value: nobody has recorded a term for
            # this student, which is different from their year being unknown.
            assert rows["OLD001"]["academic_year"] == "", (
                "the academic year should default to empty, not NULL")

            # Idempotent: running it again must do nothing at all.
            assert database.migrate() == [], "the migration is not idempotent"
        finally:
            database.close()

        print("PASS  a version-1 database gains eight columns and loses "
              "nothing")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_version_two_database_upgrades_to_the_current_schema():
    """
    The UPGRADE path, which a fresh-database test cannot exercise.

    initialize() runs the table script, then migrate(), then the indexes. Three
    of those indexes are on columns version 3 added, and on an existing table
    CREATE TABLE IF NOT EXISTS does nothing - so creating an index in the same
    script as the tables failed with "no such column: academic_session_id" and
    took the whole upgrade with it. Every real installation hit that and no new
    database could, which is exactly why this test builds an OLD one.

    The fixture is a genuine version-2 database with rows in it, built by hand
    rather than by calling initialize() and deleting things afterwards.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_v2_"))
    try:
        path = directory / "old.db"
        raw = sqlite3.connect(str(path))
        raw.executescript("""
            CREATE TABLE schema_info (key TEXT PRIMARY KEY,
                                      value TEXT NOT NULL);
            CREATE TABLE students (
                student_id  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                roll_number TEXT NOT NULL DEFAULT '',
                class_name  TEXT NOT NULL DEFAULT '',
                section     TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL);
            CREATE TABLE attendance_sessions (
                session_id     TEXT PRIMARY KEY,
                class_id       TEXT NOT NULL,
                session_date   TEXT NOT NULL,
                start_time     TEXT NOT NULL,
                end_time       TEXT NOT NULL,
                timezone       TEXT NOT NULL DEFAULT 'UTC',
                status         TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                completed_at   TEXT);
            CREATE TABLE session_expected_students (
                session_id TEXT NOT NULL,
                student_id TEXT NOT NULL,
                UNIQUE (session_id, student_id));
            CREATE TABLE attendance_records (
                attendance_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id     TEXT NOT NULL,
                student_id     TEXT NOT NULL,
                status         TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                evaluated_at   TEXT NOT NULL);
            CREATE TABLE presence_segments (
                segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                student_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time   TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE users (
                user_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                role     TEXT NOT NULL);
            CREATE TABLE user_class_assignments (
                assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                class_name    TEXT NOT NULL);
            CREATE TABLE user_sessions (
                session_token_hash TEXT PRIMARY KEY,
                user_id            INTEGER NOT NULL);
            CREATE TABLE audit_log (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                at       TEXT NOT NULL,
                action   TEXT NOT NULL);

            INSERT INTO schema_info VALUES ('schema_version', '2');
            INSERT INTO students VALUES
                ('OLD001', 'Existing One', '001', 'CSE(AI)', 'C', 'ACTIVE',
                 'then', 'then');
            INSERT INTO attendance_sessions VALUES
                ('SES-OLD-1', 'CSE(AI)-C', '2026-09-01',
                 '2026-09-01T09:00:00+00:00', '2026-09-01T10:00:00+00:00',
                 'UTC', 'COMPLETED', '1.0', 'then', 'then');
            INSERT INTO session_expected_students VALUES ('SES-OLD-1', 'OLD001');
            INSERT INTO attendance_records
                (session_id, student_id, status, policy_version, evaluated_at)
                VALUES ('SES-OLD-1', 'OLD001', 'PRESENT', '1.0', 'then');
            INSERT INTO presence_segments
                (session_id, student_id, start_time, end_time, created_at)
                VALUES ('SES-OLD-1', 'OLD001', '2026-09-01T09:02:00+00:00',
                        '2026-09-01T09:58:00+00:00', 'then');
            INSERT INTO users VALUES (1, 'owner', 'SUPER_ADMIN');
            INSERT INTO audit_log (at, action) VALUES ('then', 'student.create');
        """)
        raw.commit()
        raw.close()

        database = Database(path)
        database.connect()
        try:
            assert database.schema_version() == 2, (
                "the fixture is not a version-2 database")

            # The upgrade itself. Before the index ordering was fixed, this
            # line raised OperationalError and nothing below ran.
            database.initialize()

            assert database.schema_version() == SCHEMA_VERSION

            # Every version-3 table is there.
            tables = set(database.table_names())
            for table in ("academic_sessions", "student_academic_history",
                          "notification_rules", "notifications",
                          "model_registry", "cameras"):
                assert table in tables, "%r was not created" % table

            # The indexes that depend on migrated columns exist.
            indexes = {row["name"] for row in database.query(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name LIKE 'idx_%'")}
            for index in ("idx_sessions_academic", "idx_students_class",
                          "idx_history_student"):
                assert index in indexes, "%r was not created" % index

            # And every existing row came through untouched.
            student = dict(database.query_one(
                "SELECT * FROM students WHERE student_id = 'OLD001'"))
            assert student["name"] == "Existing One"
            assert student["class_name"] == "CSE(AI)"
            assert student["section"] == "C"
            assert student["parent_phone"] == "", (
                "the new contact column should default to empty, not NULL")
            assert student["notification_enabled"] == 1

            session = dict(database.query_one(
                "SELECT * FROM attendance_sessions "
                "WHERE session_id = 'SES-OLD-1'"))
            assert session["status"] == "COMPLETED"
            assert session["start_time"] == "2026-09-01T09:00:00+00:00", (
                "the migration rewrote a historical session time")
            assert session["academic_session_id"] is None, (
                "a term was invented for a session that predates terms")
            assert session["correction_state"] == "NONE"

            for table, expected in (("attendance_records", 1),
                                    ("presence_segments", 1),
                                    ("session_expected_students", 1),
                                    ("users", 1), ("audit_log", 1)):
                rows = database.query("SELECT * FROM %s" % table)
                assert len(rows) == expected, (
                    "%s has %d row(s), expected %d"
                    % (table, len(rows), expected))

            assert dict(database.query_one(
                "SELECT * FROM attendance_records"))["status"] == "PRESENT"

            # Re-running the whole thing is a no-op, twice over.
            database.initialize()
            assert database.migrate() == [], "the upgrade is not idempotent"
            assert len(database.query("SELECT * FROM students")) == 1
        finally:
            database.close()

        print("PASS  a version-2 database upgrades to version %d, gains 6 "
              "tables and loses nothing" % SCHEMA_VERSION)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_migration_preserves_attendance_sessions_and_segments():
    """
    Every row survives. This is the guarantee the whole migration rests on.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_preserve_"))
    try:
        path = directory / "full.db"
        database = Database(path)
        database.connect()
        database.initialize()

        students = StudentRepository(database)
        for fixture in FIXTURES:
            students.upsert(fixture[0], fixture[1])

        from attendance_session import AttendanceSession
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        session = AttendanceSession(
            session_id="SES-MIGRATION", class_id="MECH",
            start_time=start, end_time=start + timedelta(hours=1),
            policy_version="1.0",
            expected_students=[fixture[0] for fixture in FIXTURES])

        sessions = SessionRepository(database)
        attendance = AttendanceRepository(database)

        with database.transaction():
            sessions.save(session)

        attendance.save_decisions([
            decision("SES-MIGRATION", fixture[0], start)
            for fixture in FIXTURES])

        before = {table: database.query_one(
            "SELECT COUNT(*) AS total FROM %s" % table)["total"]
            for table in database.table_names()}

        database.migrate()
        database.initialize()

        after = {table: database.query_one(
            "SELECT COUNT(*) AS total FROM %s" % table)["total"]
            for table in database.table_names()}

        for table, count in before.items():
            assert after.get(table) == count, (
                "migrating changed %s from %d rows to %s"
                % (table, count, after.get(table)))

        assert database.schema_version() == SCHEMA_VERSION
        database.close()

        print("PASS  migrating preserves every attendance, session, expected "
              "and presence row")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_existing_enrollments_fill_the_registry_in():
    """
    Metadata that lived only in the embedding store is copied into SQLite.

    For ANY student in the store - the loop below does three at once, and
    none of them is named in the implementation.
    """
    environment = temp_environment()
    try:
        store = environment["store"]
        database = state.get_database()
        students = StudentRepository(database)

        for student_id, name, roll, class_name, section in FIXTURES:
            store.save_student(student_id=student_id, name=name,
                               roll_number=roll, class_name=class_name,
                               section=section, model_name="sface",
                               model_version="2021dec")
            # The row a pre-migration enrollment would have left: id and name
            # only, with the other three blank.
            students.upsert(student_id, name)

        report = synchronise_registry(database, store=store)

        assert report["store_students"] == len(FIXTURES)
        assert len(report["filled"]) == len(FIXTURES), (
            "only %d of %d students were filled in"
            % (len(report["filled"]), len(FIXTURES)))

        for student_id, name, roll, class_name, section in FIXTURES:
            row = students.get(student_id)
            assert row["roll_number"] == roll, (
                "%s kept roll %r" % (student_id, row["roll_number"]))
            assert row["class_name"] == class_name
            assert row["section"] == section

        # Idempotent, and not destructive on a second pass.
        again = synchronise_registry(database, store=store)
        assert again["filled"] == [], "the backfill is not idempotent"
        assert again["unchanged"] == len(FIXTURES)

        print("PASS  existing enrollments fill the registry in, once, for "
              "every student")
    finally:
        teardown(environment)


def test_the_backfill_never_overwrites_what_a_teacher_typed():
    environment = temp_environment()
    try:
        store = environment["store"]
        database = state.get_database()
        students = StudentRepository(database)

        store.save_student(student_id="STU_TEST_009", name="Store Name",
                           roll_number="store-roll", class_name="STORE-CLASS",
                           section="Z", model_name="sface",
                           model_version="2021dec")

        students.upsert("STU_TEST_009", "Registry Name",
                        roll_number="registry-roll", class_name="REAL-CLASS",
                        section="A")

        synchronise_registry(database, store=store)

        row = students.get("STU_TEST_009")
        assert row["name"] == "Registry Name", "the backfill renamed a student"
        assert row["roll_number"] == "registry-roll"
        assert row["class_name"] == "REAL-CLASS"
        assert row["section"] == "A"

        print("PASS  the backfill fills blanks only and never overwrites the "
              "registry")
    finally:
        teardown(environment)


def test_an_orphan_enrollment_is_imported_not_invented():
    """
    A face enrollment with no registry row becomes a registered student.

    Nothing is guessed: the name, roll, class and section all come from the
    enrollment record, which asked for them. A store record with NO name is
    skipped and reported rather than filled in with a placeholder.
    """
    environment = temp_environment()
    try:
        store = environment["store"]
        database = state.get_database()

        store.save_student(student_id="ORPHAN01", name="Orphaned Student",
                           roll_number="o-1", class_name="MECH", section="A",
                           model_name="sface", model_version="2021dec")
        store.save_student(student_id="ORPHAN02", name="",
                           roll_number="", class_name="", section="",
                           model_name="sface", model_version="2021dec")

        report = synchronise_registry(database, store=store)

        assert report["created"] == ["ORPHAN01"], (
            "created %s" % report["created"])
        assert report["skipped"] == ["ORPHAN02"], (
            "a nameless enrollment was not skipped: %s" % report["skipped"])

        row = StudentRepository(database).get("ORPHAN01")
        assert row["name"] == "Orphaned Student"
        assert row["class_name"] == "MECH"
        assert StudentRepository(database).get("ORPHAN02") is None, (
            "a student was invented from a nameless enrollment")

        print("PASS  an orphan enrollment is imported from its own record, "
              "and a nameless one is not")
    finally:
        teardown(environment)


# -- creation ---------------------------------------------------------------

def test_every_registered_student_carries_the_full_metadata():
    environment = temp_environment()
    try:
        client = environment["client"]

        for response, fixture in zip(register_all(client), FIXTURES):
            assert response.status_code == 201, response.text
            body = response.json()
            assert body["student_id"] == fixture[0]
            assert body["name"] == fixture[1]
            assert body["roll_number"] == fixture[2]
            assert body["class_name"] == fixture[3]
            assert body["section"] == fixture[4]
            assert body["status"] == "ACTIVE"
            assert body["enrolled"] is False
            assert body["sample_count"] == 0

        listing = client.get("/api/students").json()
        assert listing["total"] == len(FIXTURES)

        print("PASS  every registered student carries id, name, roll, class, "
              "section and status")
    finally:
        teardown(environment)


def test_a_student_nobody_wrote_into_the_code_works_identically():
    """
    Twenty generated students, none of them named anywhere in the project.

    If any student-specific logic existed, these would behave differently
    from the fixtures. They do not.
    """
    environment = temp_environment()
    try:
        client = environment["client"]

        for index in range(20):
            fixture = generated(index)
            assert register(client, fixture).status_code == 201

            body = client.get("/api/students/%s" % fixture[0]).json()
            assert body["name"] == fixture[1]
            assert body["roll_number"] == fixture[2]
            assert body["class_name"] == fixture[3]
            assert body["section"] == fixture[4]

        assert client.get("/api/students").json()["total"] == 20

        print("PASS  20 generated students behave exactly like the fixtures")
    finally:
        teardown(environment)


def test_students_reach_sqlite_in_a_form_a_plain_query_can_read():
    """
    The two queries from the brief, run against the real file.

    Not through the API - through sqlite3, the way DB Browser would.
    """
    environment = temp_environment()
    try:
        register_all(environment["client"])

        path = state.current_path()
        raw = sqlite3.connect(str(path))
        raw.row_factory = sqlite3.Row
        try:
            rows = raw.execute("""
                SELECT student_id, name, roll_number, class_name, section,
                       status
                  FROM students
                 ORDER BY student_id
            """).fetchall()

            assert len(rows) == len(FIXTURES)
            for row, fixture in zip(rows, FIXTURES):
                assert row["student_id"] == fixture[0]
                assert row["name"] == fixture[1]
                assert row["roll_number"] == fixture[2]
                assert row["class_name"] == fixture[3]
                assert row["section"] == fixture[4]
                assert row["status"] == "ACTIVE"

            summary = raw.execute("""
                SELECT s.student_id, s.name, s.roll_number, s.class_name,
                       s.section, s.status,
                       COUNT(DISTINCT ar.session_id) AS sessions_attended
                  FROM students s
                  LEFT JOIN attendance_records ar
                    ON ar.student_id = s.student_id
                 GROUP BY s.student_id, s.name, s.roll_number, s.class_name,
                          s.section, s.status
                 ORDER BY s.student_id
            """).fetchall()

            assert len(summary) == len(FIXTURES)
            assert all(row["sessions_attended"] == 0 for row in summary)
        finally:
            raw.close()

        print("PASS  both inspection queries from the brief work, for every "
              "student")
    finally:
        teardown(environment)


def test_a_duplicate_student_id_is_a_conflict_not_an_overwrite():
    environment = temp_environment()
    try:
        client = environment["client"]
        register(client, FIXTURES[0])

        again = client.post("/api/students", json={
            "student_id": FIXTURES[0][0], "name": "Someone Else",
            "roll_number": "999", "class_name": "OTHER", "section": "Z"})

        assert again.status_code == 409, again.text
        assert client.get("/api/students/%s" % FIXTURES[0][0]).json()["name"] \
            == FIXTURES[0][1], "a duplicate overwrote the original student"

        print("PASS  a duplicate id is refused and overwrites nothing")
    finally:
        teardown(environment)


def test_an_unsafe_student_id_is_refused_before_it_reaches_the_registry():
    environment = temp_environment()
    try:
        client = environment["client"]

        for bad in ("../escape", "with space", "a/b"):
            response = client.post("/api/students", json={
                "student_id": bad, "name": "Nope"})
            assert response.status_code == 400, (
                "%r was accepted (%d)" % (bad, response.status_code))

        assert client.get("/api/students").json()["total"] == 0

        print("PASS  an id the embedding store would refuse cannot be "
              "registered either")
    finally:
        teardown(environment)


# -- editing ----------------------------------------------------------------

def test_editing_changes_sqlite_and_keeps_the_enrollment():
    environment = temp_environment()
    try:
        client, store = environment["client"], environment["store"]
        student_id = FIXTURES[1][0]

        register(client, FIXTURES[1])
        store.save_student(student_id=student_id, name=FIXTURES[1][1],
                           roll_number=FIXTURES[1][2],
                           class_name=FIXTURES[1][3], section=FIXTURES[1][4],
                           model_name="sface", model_version="2021dec")
        store.save_embedding(student_id, [[0.1] * 128] * 5,
                             {"model_name": "sface", "model_version": "2021dec",
                              "embedding_dim": 128})

        response = client.patch("/api/students/%s" % student_id, json={
            "name": "Brigid O'Hare-Smith", "roll_number": "0002-A",
            "class_name": "MECH", "section": "C"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Brigid O'Hare-Smith"
        assert body["roll_number"] == "0002-A"
        assert body["section"] == "C"

        # The point of the test: an edit must not cost the student their face.
        assert body["enrolled"] is True
        assert body["sample_count"] == 5
        assert store.has_student(student_id)

        # And the store's copy of the metadata is brought into step.
        assert store.get_student(student_id)["section"] == "C", (
            "the embedding store's metadata drifted from the registry")

        print("PASS  editing updates SQLite, syncs the store, and keeps the "
              "face samples")
    finally:
        teardown(environment)


def test_the_student_id_cannot_be_edited():
    environment = temp_environment()
    try:
        client = environment["client"]
        register(client, FIXTURES[0])

        # Sent anyway, the way a hand-written request would.
        client.patch("/api/students/%s" % FIXTURES[0][0],
                     json={"student_id": "SOMETHING_ELSE", "name": "Renamed"})

        assert client.get("/api/students/%s" % FIXTURES[0][0]).status_code == 200
        assert client.get("/api/students/SOMETHING_ELSE").status_code == 404

        print("PASS  the student id is not editable, so nothing that "
              "references it is orphaned")
    finally:
        teardown(environment)


def test_editing_an_unknown_student_is_a_404():
    environment = temp_environment()
    try:
        response = environment["client"].patch("/api/students/NOBODY",
                                               json={"name": "Ghost"})
        assert response.status_code == 404, response.text
        print("PASS  editing a student who does not exist is a 404")
    finally:
        teardown(environment)


# -- status, history and removal -------------------------------------------

def decision(session_id, student_id, start):
    """
    One PRESENT decision, built the way the rule engine builds them.

    The constructor takes a presence SUMMARY rather than loose timestamps, so
    the evidence fields are filled in afterwards - which is also what makes
    this a fixture rather than a second implementation of the rules.
    """
    from datetime import timedelta

    from attendance_rules import AttendanceDecision

    made = AttendanceDecision(
        student_id=student_id, session_id=session_id, status="PRESENT",
        reason="registry test fixture", policy_version="1.0",
        session_seconds=3600.0, presence_ratio=0.83)

    made.present_seconds = 3000.0
    made.first_seen = start
    made.last_seen = start + timedelta(minutes=50)
    made.segment_count = 1
    made.loss_periods = 0
    return made


def make_attendance(client, student_ids, class_id="MECH"):
    """A completed session with one PRESENT decision per student."""
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)
    response = client.post("/api/sessions", json={
        "class_id": class_id,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
        "expected_students": list(student_ids)})
    assert response.status_code == 201, response.text
    session_id = response.json()["session_id"]

    database = state.get_database()
    database.execute(
        "UPDATE attendance_sessions SET status = 'COMPLETED' "
        "WHERE session_id = ?", (session_id,))

    AttendanceRepository(database).save_decisions([
        decision(session_id, student_id, start)
        for student_id in student_ids])

    return session_id


def test_the_attendance_summary_is_derived_for_every_student():
    environment = temp_environment()
    try:
        client = environment["client"]
        register_all(client)

        ids = [fixture[0] for fixture in FIXTURES]
        make_attendance(client, ids)

        listing = client.get("/api/students").json()["items"]
        assert len(listing) == len(FIXTURES)

        for item in listing:
            summary = item["attendance"]
            assert summary["expected_sessions"] == 1, item["student_id"]
            assert summary["recorded_sessions"] == 1
            assert summary["sessions_attended"] == 1
            assert summary["attendance_rate"] == 1.0
            assert summary["counts"]["PRESENT"] == 1
            assert summary["counts"]["ABSENT"] == 0

        # And it is DERIVED, not stored: no column on students holds it.
        columns = state.get_database().column_names("students")
        for forbidden in ("attendance", "present_count", "sessions_attended"):
            assert forbidden not in columns, (
                "attendance was duplicated into the students table: %s"
                % columns)

        print("PASS  attendance is summarised per student and stored nowhere "
              "on the student row")
    finally:
        teardown(environment)


def test_a_student_with_no_sessions_has_no_rate_rather_than_zero():
    environment = temp_environment()
    try:
        client = environment["client"]
        register(client, FIXTURES[2])

        summary = client.get("/api/students/%s" % FIXTURES[2][0]) \
                        .json()["attendance"]

        assert summary["expected_sessions"] == 0
        assert summary["attendance_rate"] is None, (
            "a student nobody has expected was given a 0%% attendance rate")

        print("PASS  no expected sessions means no rate, not 0%")
    finally:
        teardown(environment)


def test_deactivating_keeps_every_attendance_record():
    environment = temp_environment()
    try:
        client = environment["client"]
        register_all(client)

        ids = [fixture[0] for fixture in FIXTURES]
        session_id = make_attendance(client, ids)
        target = ids[0]

        before = client.get("/api/students/%s/attendance" % target).json()
        assert before["total"] == 1

        response = client.post("/api/students/%s/status?status=INACTIVE"
                               % target)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "INACTIVE"

        after = client.get("/api/students/%s/attendance" % target).json()
        assert after["total"] == 1, "deactivating destroyed attendance history"

        database = state.get_database()
        assert database.query_one(
            "SELECT COUNT(*) AS total FROM session_expected_students "
            "WHERE session_id = ?", (session_id,))["total"] == len(ids), (
            "deactivating removed an expected-student row")

        # And it can be undone.
        assert client.post("/api/students/%s/status?status=ACTIVE" % target) \
                     .json()["status"] == "ACTIVE"

        print("PASS  deactivating preserves attendance, expectations and "
              "presence, and is reversible")
    finally:
        teardown(environment)


def test_deleting_a_student_with_history_is_refused_by_default():
    environment = temp_environment()
    try:
        client = environment["client"]
        register_all(client)

        ids = [fixture[0] for fixture in FIXTURES]
        make_attendance(client, ids)

        response = client.delete("/api/students/%s" % ids[0])
        assert response.status_code == 409, response.text
        assert "history" in response.json()["detail"].lower()

        assert client.get("/api/students/%s" % ids[0]).status_code == 200, (
            "the refused delete removed the student anyway")
        assert client.get("/api/students/%s/attendance" % ids[0]) \
                     .json()["total"] == 1

        print("PASS  deleting a student with history is refused, and the "
              "refusal says what would be lost")
    finally:
        teardown(environment)


def test_deleting_a_student_without_history_removes_the_enrollment_too():
    environment = temp_environment()
    try:
        client, store = environment["client"], environment["store"]
        student_id = FIXTURES[2][0]

        register(client, FIXTURES[2])
        store.save_student(student_id=student_id, name=FIXTURES[2][1],
                           roll_number=FIXTURES[2][2],
                           class_name=FIXTURES[2][3], section=FIXTURES[2][4],
                           model_name="sface", model_version="2021dec")
        store.save_embedding(student_id, [[0.2] * 128] * 3,
                             {"model_name": "sface", "model_version": "2021dec",
                              "embedding_dim": 128})

        assert store.has_student(student_id)

        response = client.delete("/api/students/%s" % student_id)
        assert response.status_code == 200, response.text
        assert response.json()["enrollment_removed"] is True

        assert client.get("/api/students/%s" % student_id).status_code == 404
        assert not store.has_student(student_id), (
            "the face samples outlived the student - an orphaned biometric "
            "record")
        assert not (environment["directory"] / "embeddings" /
                    ("%s.npz" % student_id)).exists(), (
            "the vector file was left behind")

        print("PASS  deleting a student with no history removes their face "
              "samples too")
    finally:
        teardown(environment)


def test_a_confirmed_delete_is_the_only_way_to_destroy_history():
    """
    Destroying history takes the student's id, typed out.

    Written after exactly this happened for real: one click on a button
    labelled "Delete permanently" removed a student's roll entry, five face
    samples, three attendance records and five presence segments, because
    every one of those tables cascades from students(student_id).
    """
    environment = temp_environment()
    try:
        client = environment["client"]
        register_all(client)
        ids = [fixture[0] for fixture in FIXTURES]
        make_attendance(client, ids)

        assert client.delete("/api/students/%s" % ids[1]).status_code == 409

        # A truthy confirmation is not enough - the student's own id has to be
        # typed. This is the guard that a stray double-click cannot satisfy.
        for wrong in ("true", "yes", ids[0], ""):
            refused = client.delete("/api/students/%s?confirm=%s"
                                    % (ids[1], wrong))
            assert refused.status_code in (400, 409), (
                "confirm=%r deleted a student with history (%d)"
                % (wrong, refused.status_code))

        assert client.get("/api/students/%s" % ids[1]).status_code == 200, (
            "a refused delete removed the student anyway")

        confirmed = client.delete("/api/students/%s?confirm=%s"
                                  % (ids[1], ids[1]))
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["history_removed"]["attendance_records"] == 1

        assert client.get("/api/students/%s" % ids[1]).status_code == 404

        # The OTHER students' history is untouched.
        assert client.get("/api/students/%s/attendance" % ids[0]) \
                     .json()["total"] == 1

        print("PASS  destroying history takes an explicit confirmation and "
              "affects nobody else")
    finally:
        teardown(environment)


# -- filtering --------------------------------------------------------------

def test_the_registry_can_be_filtered_by_class_section_and_status():
    environment = temp_environment()
    try:
        client = environment["client"]
        register_all(client)
        client.post("/api/students/%s/status?status=INACTIVE" % FIXTURES[2][0])

        def total(query):
            return client.get("/api/students?" + query).json()["total"]

        assert total("class_name=MECH") == 2
        assert total("class_name=mech") == 2, "the class filter is case bound"
        assert total("class_name=CIVIL") == 1
        assert total("section=A") == 2
        assert total("class_name=MECH&section=B") == 1
        assert total("status=ACTIVE") == 2
        assert total("status=INACTIVE") == 1
        assert total("search=Chen") == 1
        assert total("search=R-11") == 1, "roll numbers are not searchable"
        assert total("enrolled=false") == 3
        assert total("enrolled=true") == 0

        print("PASS  the registry filters by class, section, status, "
              "enrollment and free text")
    finally:
        teardown(environment)


# -- both write paths -------------------------------------------------------

def test_the_cli_sync_writes_the_same_row_the_dashboard_writes():
    """
    ai/enroll_student.py's registry sync and POST /api/students must agree.

    Compared field by field on two DIFFERENT students, so a shared bug that
    wrote a constant would not pass.
    """
    environment = temp_environment()
    try:
        client = environment["client"]
        database = state.get_database()

        dashboard = FIXTURES[0]
        register(client, dashboard)

        cli = FIXTURES[1]
        StudentRepository(database).upsert(
            cli[0], cli[1], roll_number=cli[2], class_name=cli[3],
            section=cli[4])

        rows = {row["student_id"]: row
                for row in StudentRepository(database).list_all()}

        for fixture in (dashboard, cli):
            row = rows[fixture[0]]
            assert row["name"] == fixture[1]
            assert row["roll_number"] == fixture[2]
            assert row["class_name"] == fixture[3]
            assert row["section"] == fixture[4]
            assert row["status"] == "ACTIVE"

        assert sorted(rows[dashboard[0]].keys()) == \
            sorted(rows[cli[0]].keys()), (
            "the two paths produced differently shaped rows")

        print("PASS  the CLI and the dashboard write identically shaped "
              "registry rows")
    finally:
        teardown(environment)


def test_an_enrollment_sync_never_reactivates_a_deactivated_student():
    environment = temp_environment()
    try:
        client = environment["client"]
        student_id = FIXTURES[0][0]

        register(client, FIXTURES[0])
        client.post("/api/students/%s/status?status=INACTIVE" % student_id)

        # What a re-enrollment does: upsert with the details and no status.
        StudentRepository(state.get_database()).upsert(
            student_id, FIXTURES[0][1], roll_number="changed",
            class_name=FIXTURES[0][3], section=FIXTURES[0][4])

        row = client.get("/api/students/%s" % student_id).json()
        assert row["status"] == "INACTIVE", (
            "re-running the camera reactivated a deactivated student")
        assert row["roll_number"] == "changed", (
            "the enrollment did not update the metadata it was given")

        print("PASS  re-enrolling updates metadata without reactivating a "
              "deactivated student")
    finally:
        teardown(environment)


def test_partial_upserts_do_not_blank_existing_metadata():
    environment = temp_environment()
    try:
        client = environment["client"]
        register(client, FIXTURES[0])

        # A caller that knows only an id and a name - the old two-argument
        # shape, still used by the benchmark and the demo seeder.
        StudentRepository(state.get_database()).upsert(FIXTURES[0][0],
                                                       "Renamed Person")

        row = client.get("/api/students/%s" % FIXTURES[0][0]).json()
        assert row["name"] == "Renamed Person"
        assert row["roll_number"] == FIXTURES[0][2], (
            "a two-argument upsert blanked the roll number")
        assert row["class_name"] == FIXTURES[0][3]
        assert row["section"] == FIXTURES[0][4]

        print("PASS  an upsert that omits metadata leaves it alone")
    finally:
        teardown(environment)


# -- privacy ----------------------------------------------------------------

def test_no_registry_response_exposes_biometric_data():
    environment = temp_environment()
    try:
        client, store = environment["client"], environment["store"]
        register_all(client)

        store.save_student(student_id=FIXTURES[0][0], name=FIXTURES[0][1],
                           roll_number=FIXTURES[0][2],
                           class_name=FIXTURES[0][3], section=FIXTURES[0][4],
                           model_name="sface", model_version="2021dec")
        store.save_embedding(FIXTURES[0][0], [[0.3] * 128] * 5,
                             {"model_name": "sface", "model_version": "2021dec",
                              "embedding_dim": 128})

        forbidden = ("embedding", "vector", "descriptor", "image", "photo",
                     "crop", "frame", "npz", "password", "token", "path")

        for url in ("/api/students",
                    "/api/students/%s" % FIXTURES[0][0],
                    "/api/students/%s/attendance" % FIXTURES[0][0]):
            text = client.get(url).text.lower()
            for word in forbidden:
                assert word not in text, (
                    "%s mentions %r" % (url, word))

        print("PASS  no registry response carries an embedding, an image or a "
              "path")
    finally:
        teardown(environment)


def test_tests_never_touch_the_real_stores():
    real_database = PROJECT_ROOT / "data" / "classvision.db"
    real_store = PROJECT_ROOT / "data" / "embeddings"

    stamp = real_database.stat().st_mtime if real_database.exists() else None
    files = sorted(p.name for p in real_store.glob("*")) \
        if real_store.exists() else []

    environment = temp_environment()
    try:
        register_all(environment["client"])
        assert "classvision.db" not in str(state.current_path()), (
            "the tests are pointed at the real database")
    finally:
        teardown(environment)

    if stamp is not None:
        assert real_database.stat().st_mtime == stamp, (
            "the real attendance database was modified")
    if real_store.exists():
        assert sorted(p.name for p in real_store.glob("*")) == files, (
            "the real embedding store was modified")

    print("PASS  the real database and embedding store were never opened")


def test_no_student_identifier_is_hard_coded_in_the_implementation():
    """
    The fix must be general. This looks for the shape of a one-off.

    Scans the modules that make up the registry for a literal student id or
    for the names in the project's own example data. A conditional on a
    particular student would have to be written somewhere, and this is where
    it would show up.
    """
    import re

    suspects = [
        PROJECT_ROOT / "backend" / "services" / "data_service.py",
        PROJECT_ROOT / "backend" / "services" / "enrollment_service.py",
        PROJECT_ROOT / "backend" / "routes" / "students.py",
        PROJECT_ROOT / "database" / "repositories.py",
        PROJECT_ROOT / "database" / "migrate_students.py",
        PROJECT_ROOT / "ai" / "enrollment_session.py",
    ]

    # An id LITERAL used in a comparison, rather than mentioned in prose.
    pattern = re.compile(r'[=!]=\s*["\']STU[A-Z0-9_-]*["\']')

    for path in suspects:
        source = path.read_text(encoding="utf-8")
        found = pattern.findall(source)
        assert not found, "%s compares against a literal id: %s" % (path.name,
                                                                    found)
        for name in ("Safia", "Farhan"):
            assert name not in source, (
                "%s mentions %r" % (path.name, name))

    print("PASS  no registry module compares against a literal student id")


# -- the Students page ------------------------------------------------------

HTML = (PROJECT_ROOT / "backend" / "static" / "index.html").read_text(
    encoding="utf-8")
JS = (PROJECT_ROOT / "backend" / "static" / "app.js").read_text(
    encoding="utf-8")


def test_the_students_table_shows_every_registry_column():
    table = HTML[HTML.index('id="students-table"'):
                 HTML.index('id="students-pager"')]

    for heading in ("Student ID", "Name", "Roll", "Class", "Section",
                    "Status", "Enrollment", "Samples", "Attendance"):
        assert ">%s<" % heading in table, (
            "the students table has no %r column" % heading)

    print("PASS  the students table shows all nine registry columns")


def test_the_add_student_form_asks_for_every_registry_field():
    dialog = HTML[HTML.index('id="student-dialog"'):
                  HTML.index('id="student-edit-dialog"')]

    for field in ("new-student-id", "new-student-name", "new-student-roll",
                  "new-student-class", "new-student-section"):
        assert 'id="%s"' % field in dialog, "the form has no %r input" % field

    # All five are required, so a half-filled student cannot be created from
    # the dashboard at all.
    assert dialog.count("required") >= 5, (
        "not every registry field is required")

    assert "Save &amp; Enroll Face" in dialog or "Save & Enroll Face" in dialog, (
        "there is no save-and-enroll action")

    print("PASS  the add form asks for all five registry fields and offers "
          "enrollment at registration time")


def test_the_dialogs_can_all_be_closed_and_cancelled():
    """
    Every dialog has a working way out, including the one holding a camera.

    The close event matters more than the button: Escape closes a dialog
    without going through any click handler, and a capture left running
    against a webcam is what stops the live classroom from starting.
    """
    for dialog, closer in (("student-dialog", "student-close"),
                           ("student-edit-dialog", "edit-student-close"),
                           ("student-remove-dialog", "student-remove-close"),
                           ("user-dialog", "user-close"),
                           ("password-dialog", "password-close")):
        assert 'id="%s"' % dialog in HTML, "no %s" % dialog
        assert 'id="%s"' % closer in HTML, "%s has no close control" % dialog
        assert '$("%s").addEventListener("click"' % closer in JS, (
            "%s is not wired to anything" % closer)

    assert 'id="student-cancel-enroll"' in HTML
    assert 'cancelEnrollment' in JS
    assert '$("student-dialog").addEventListener("close"' in JS, (
        "closing the enrollment dialog does not release the camera")
    assert "/api/students/enrollment/cancel" in JS

    print("PASS  every dialog closes, and closing the enrollment dialog "
          "cancels the capture")


def test_the_registry_actions_are_all_wired():
    for endpoint in ("/api/students", "/api/students/enrollment/start",
                     "/api/students/enrollment/cancel"):
        assert endpoint in JS, "%s is never called" % endpoint

    for handler in ("openStudentDialog", "openStudentEdit", "openStudentRemove",
                    "enrollExistingStudent", "saveStudentEdit",
                    "deactivateStudent", "deleteStudent", "registerStudentOnly",
                    "startEnrollment"):
        assert handler in JS, "%s is missing" % handler

    assert '"PATCH"' in JS, "nothing edits a student"
    assert '"DELETE"' in JS, "nothing deletes a student"
    assert "student-delete-confirm" in JS, (
        "the delete flow has no type-the-id confirmation")
    assert "Type " in HTML and "to confirm" in HTML, (
        "the remove dialog does not ask for the id to be typed")

    print("PASS  add, edit, enroll, deactivate and delete are all wired to "
          "the API")


def test_permanent_deletion_is_offered_only_to_a_super_admin():
    """
    The button follows the role the server reports, and nothing else.

    This is UI convenience only - DELETE /api/students/{id} depends on
    require_super_admin, so a teacher who reaches the endpoint by hand is
    refused whether or not the control was drawn. What is asserted here is
    that a teacher is not shown a button that can only ever fail, and that
    narrowing it did not quietly take Deactivate away from them.
    """
    opener = JS[JS.index("async function openStudentRemove"):
                JS.index("async function deactivateStudent")]

    assert "state.auth.is_super_admin" in opener, (
        "the remove dialog does not consult the signed-in role at all")
    assert '$("student-delete").hidden' in opener, (
        "permanent deletion is shown to every role")

    # Deactivate is gated on the student's status, never on the role.
    deactivate_line = [line for line in opener.splitlines()
                       if '$("student-deactivate").hidden' in line]
    assert len(deactivate_line) == 1, "Deactivate is hidden in more than one way"
    assert "is_super_admin" not in deactivate_line[0], (
        "Deactivate was taken away from teachers along with Delete")
    assert "INACTIVE" in deactivate_line[0]

    # A teacher sees an explanation where the button was, not a gap.
    assert 'id="student-delete-note"' in HTML, (
        "nothing explains the missing Delete button to a teacher")
    assert 'id="student-delete-explainer"' in HTML
    assert '$("student-delete-note").hidden' in opener

    # The browser is not the authority: the endpoint is still called with a
    # typed confirmation and the server still decides.
    assert "require_super_admin" in (
        PROJECT_ROOT / "backend" / "routes" / "students.py").read_text(
            encoding="utf-8"), (
        "the route does not enforce what the dialog assumes")

    print("PASS  permanent deletion is offered only to a Super Admin, "
          "Deactivate stays with teachers, and the server still enforces it")


def test_save_and_enroll_registers_before_opening_the_camera():
    """
    The ordering is what makes a cancelled capture non-destructive.

    Register first, then capture: a cancelled or failed enrollment leaves a
    registered student shown as not enrolled, rather than losing everything
    the teacher typed.
    """
    enrollment = JS[JS.index("async function startEnrollment"):
                    JS.index("async function cancelEnrollment")]

    register_at = enrollment.index("ensureRegistered")
    capture_at = enrollment.index("/api/students/enrollment/start")

    assert register_at < capture_at, (
        "the camera is opened before the student is registered")

    assert "already exists" in JS, (
        "re-enrolling an existing student would fail on the registration step")

    print("PASS  save-and-enroll registers the student before opening the "
          "camera")


def test_the_students_page_renders_no_image_and_uploads_nothing():
    page = HTML[HTML.index('id="view-students"'):
                HTML.index('id="view-live"')]

    for media in ("<img", "<video", "<canvas", "<picture"):
        assert media not in page, "the Students page renders %s" % media

    print("PASS  the Students page shows no image element of any kind")


def test_every_element_the_script_reaches_for_actually_exists():
    """
    The guard that catches a dead button before a person does.

    $("...") on an id the page does not have returns null, and calling
    addEventListener on null throws during DOMContentLoaded - which silently
    abandons every handler registered AFTER the bad line. The symptom is not
    "one button is broken", it is "half the dialogs stopped responding", and
    nothing else in this project would have noticed.

    Writing this test found exactly that: the edit dialog's form was wired as
    "edit-student-form" while the markup called it "student-edit-form".
    """
    import re

    declared = set(re.findall(r'id="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([^"]+)"\)', JS))

    missing = sorted(used - declared)
    assert not missing, (
        "app.js reaches for element(s) the page does not have: %s" % missing)

    assert len(used) > 100, (
        "only %d ids were found - the pattern has stopped matching" % len(used))

    print("PASS  all %d element ids the script uses exist in the page"
          % len(used))


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_a_version_one_database_gains_the_registry_columns,
        test_a_version_two_database_upgrades_to_the_current_schema,
        test_the_migration_preserves_attendance_sessions_and_segments,
        test_existing_enrollments_fill_the_registry_in,
        test_the_backfill_never_overwrites_what_a_teacher_typed,
        test_an_orphan_enrollment_is_imported_not_invented,
        test_every_registered_student_carries_the_full_metadata,
        test_a_student_nobody_wrote_into_the_code_works_identically,
        test_students_reach_sqlite_in_a_form_a_plain_query_can_read,
        test_a_duplicate_student_id_is_a_conflict_not_an_overwrite,
        test_an_unsafe_student_id_is_refused_before_it_reaches_the_registry,
        test_editing_changes_sqlite_and_keeps_the_enrollment,
        test_the_student_id_cannot_be_edited,
        test_editing_an_unknown_student_is_a_404,
        test_the_attendance_summary_is_derived_for_every_student,
        test_a_student_with_no_sessions_has_no_rate_rather_than_zero,
        test_deactivating_keeps_every_attendance_record,
        test_deleting_a_student_with_history_is_refused_by_default,
        test_deleting_a_student_without_history_removes_the_enrollment_too,
        test_a_confirmed_delete_is_the_only_way_to_destroy_history,
        test_the_registry_can_be_filtered_by_class_section_and_status,
        test_the_cli_sync_writes_the_same_row_the_dashboard_writes,
        test_an_enrollment_sync_never_reactivates_a_deactivated_student,
        test_partial_upserts_do_not_blank_existing_metadata,
        test_no_registry_response_exposes_biometric_data,
        test_tests_never_touch_the_real_stores,
        test_no_student_identifier_is_hard_coded_in_the_implementation,
        test_the_students_table_shows_every_registry_column,
        test_the_add_student_form_asks_for_every_registry_field,
        test_the_dialogs_can_all_be_closed_and_cancelled,
        test_the_registry_actions_are_all_wired,
        test_permanent_deletion_is_offered_only_to_a_super_admin,
        test_save_and_enroll_registers_before_opening_the_camera,
        test_the_students_page_renders_no_image_and_uploads_nothing,
        test_every_element_the_script_reaches_for_actually_exists,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                       # noqa: BLE001
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__,
                                        error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
