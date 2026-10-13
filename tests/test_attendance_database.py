"""
Database tests for database/ (Step 6).

Every test creates its OWN temporary SQLite file and deletes it afterwards.
The real data/classvision.db is never opened, read, or written by these tests.

Run with:
    python tests/test_attendance_database.py
"""

import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

from attendance_rules import (  # noqa: E402
    STATUS_ABSENT,
    STATUS_PRESENT,
    AttendanceDecision,
    PresenceSegment,
)
from attendance_session import AttendanceSession  # noqa: E402
from database import SCHEMA_VERSION, DEFAULT_DATABASE_PATH, Database  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def temp_database():
    """An isolated database in a throwaway directory."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_db_"))
    database = Database(directory / "test.db")
    database.connect()
    database.initialize()
    return database, directory


def cleanup(database, directory):
    database.close()
    shutil.rmtree(directory, ignore_errors=True)


def a_session(session_id="SES-1", expected=("STU001",)):
    return AttendanceSession(session_id, "CLASS-10A", START, END,
                             expected_students=list(expected))


def a_decision(student_id="STU001", session_id="SES-1", status=STATUS_PRESENT,
               policy_version="1.0", ratio=0.95, present_seconds=3420.0):
    decision = AttendanceDecision(student_id, session_id, status,
                                  "test reason for %s" % student_id,
                                  policy_version)
    decision.present_seconds = present_seconds
    decision.session_seconds = 3600.0
    decision.presence_ratio = ratio
    decision.first_seen = START
    decision.last_seen = END
    return decision


def seed(database, students=("STU001",), session_id="SES-1"):
    """A database with students and a session, ready for attendance rows."""
    student_repository = StudentRepository(database)
    session_repository = SessionRepository(database)

    for student_id in students:
        student_repository.upsert(student_id, "Student " + student_id)

    session_repository.save(a_session(session_id, expected=students))
    return student_repository, session_repository, AttendanceRepository(database)


# -- schema ----------------------------------------------------------------

def test_schema_is_created():
    database, directory = temp_database()
    try:
        tables = database.table_names()
        for expected in ("students", "attendance_sessions",
                         "attendance_records", "presence_segments",
                         "session_expected_students", "schema_info"):
            assert expected in tables, "table %r was not created" % expected
        assert database.schema_version() == SCHEMA_VERSION, (
            "schema version not recorded")

        # Version 2 moved the student registry metadata into SQLite, so a
        # plain SELECT on students answers who a student is without a second
        # store having to be opened.
        columns = database.column_names("students")
        for column in ("student_id", "name", "roll_number", "class_name",
                       "section", "status", "created_at", "updated_at"):
            assert column in columns, (
                "the students table has no %r column: %s" % (column, columns))
        print("PASS  the schema creates all %d tables" % len(tables))
    finally:
        cleanup(database, directory)


def test_repeated_initialization_is_safe():
    database, directory = temp_database()
    try:
        students = StudentRepository(database)
        students.upsert("STU001", "Asha")

        database.initialize()
        database.initialize()

        assert students.get("STU001") is not None, (
            "re-initialising the database destroyed existing data")
        assert database.schema_version() == SCHEMA_VERSION, (
            "schema version changed")
        print("PASS  re-initialising is safe and preserves existing data")
    finally:
        cleanup(database, directory)


def test_foreign_keys_are_enforced():
    """SQLite leaves these OFF by default, so this is worth asserting."""
    database, directory = temp_database()
    try:
        assert database.foreign_keys_enabled(), (
            "foreign keys are not enforced - orphan rows would be possible")

        try:
            database.execute(
                "INSERT INTO session_expected_students (session_id, student_id) "
                "VALUES ('nope', 'nobody')")
        except sqlite3.IntegrityError:
            print("PASS  foreign keys are enforced (orphan insert refused)")
            return
        raise AssertionError("an orphan row was accepted")
    finally:
        cleanup(database, directory)


def test_tests_do_not_touch_the_real_database():
    database, directory = temp_database()
    try:
        assert database.path != DEFAULT_DATABASE_PATH, (
            "a test is pointing at the real database")
        assert str(directory) in str(database.path), (
            "the test database is outside its temporary directory")
        print("PASS  tests use an isolated database, never data/classvision.db")
    finally:
        cleanup(database, directory)


# -- students --------------------------------------------------------------

def test_insert_and_read_student():
    database, directory = temp_database()
    try:
        repository = StudentRepository(database)
        repository.upsert("STU001", "Asha R")

        student = repository.get("STU001")
        assert student["name"] == "Asha R", "name not stored"
        assert student["status"] == "ACTIVE", "default status wrong"
        assert student["created_at"], "created_at missing"
        print("PASS  a student can be inserted and read back")
    finally:
        cleanup(database, directory)


def test_student_upsert_does_not_duplicate():
    database, directory = temp_database()
    try:
        repository = StudentRepository(database)
        repository.upsert("STU001", "Asha")
        repository.upsert("STU001", "Asha Renamed")

        assert len(repository.list_all()) == 1, "upsert created a duplicate"
        assert repository.get("STU001")["name"] == "Asha Renamed", (
            "the update did not apply")
        print("PASS  upserting a student updates rather than duplicates")
    finally:
        cleanup(database, directory)


def test_student_table_holds_no_biometrics():
    """Biometric data stays in the Step 3 store, never in attendance tables."""
    database, directory = temp_database()
    try:
        columns = [row["name"] for row in database.query(
            "PRAGMA table_info(students)")]
        for banned in ("embedding", "embeddings", "vector", "face", "image",
                       "photo", "descriptor"):
            for column in columns:
                assert banned not in column.lower(), (
                    "students table has a biometric-looking column: %s" % column)
        print("PASS  the students table has no biometric column (%s)"
              % ", ".join(columns))
    finally:
        cleanup(database, directory)


# -- sessions --------------------------------------------------------------

def test_insert_session_with_expected_students():
    database, directory = temp_database()
    try:
        _, sessions, _ = seed(database, students=("STU001", "STU002"))

        stored = sessions.get("SES-1")
        assert stored["class_id"] == "CLASS-10A", "class not stored"
        assert stored["status"] == "SCHEDULED", "status wrong"
        assert stored["expected_students"] == ["STU001", "STU002"], (
            "expected students wrong: %s" % stored["expected_students"])
        print("PASS  a session stores its expected students")
    finally:
        cleanup(database, directory)


def test_session_completion_is_recorded():
    database, directory = temp_database()
    try:
        _, sessions, _ = seed(database)
        completed = sessions.complete("SES-1", completed_at=END)

        assert completed["status"] == "COMPLETED", "status not updated"
        assert completed["completed_at"] is not None, "completion time missing"
        print("PASS  completing a session records the status and timestamp")
    finally:
        cleanup(database, directory)


def test_invalid_session_status_is_rejected():
    database, directory = temp_database()
    try:
        seed(database)
        try:
            database.execute(
                "UPDATE attendance_sessions SET status = 'BANANA' "
                "WHERE session_id = 'SES-1'")
        except sqlite3.IntegrityError:
            print("PASS  an invalid session status is refused by the database")
            return
        raise AssertionError("an invalid session status was accepted")
    finally:
        cleanup(database, directory)


def test_session_end_must_follow_start():
    database, directory = temp_database()
    try:
        try:
            database.execute(
                """INSERT INTO attendance_sessions
                   (session_id, class_id, session_date, start_time, end_time,
                    timezone, status, policy_version, created_at)
                   VALUES ('S9','C','2026-03-02','2026-03-02T10:00:00+00:00',
                           '2026-03-02T09:00:00+00:00','UTC','SCHEDULED','1.0',
                           '2026-03-02T08:00:00+00:00')""")
        except sqlite3.IntegrityError:
            print("PASS  a session ending before it starts is refused")
            return
        raise AssertionError("a backwards session was accepted")
    finally:
        cleanup(database, directory)


# -- attendance records ----------------------------------------------------

def test_insert_and_read_attendance():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision())

        record = attendance.get("SES-1", "STU001")
        assert record["status"] == STATUS_PRESENT, "status not stored"
        assert record["policy_version"] == "1.0", "policy version not stored"
        assert record["reason"], "reason not stored"
        assert abs(record["presence_ratio"] - 0.95) < 1e-6, "ratio not stored"
        print("PASS  an attendance decision is stored and read back")
    finally:
        cleanup(database, directory)


def test_evaluating_twice_does_not_duplicate():
    """Idempotency: same session, same policy, no second row."""
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)

        attendance.save_decision(a_decision())
        attendance.save_decision(a_decision())
        attendance.save_decision(a_decision())

        assert attendance.count_for_session("SES-1") == 1, (
            "re-evaluating created duplicate rows: %d"
            % attendance.count_for_session("SES-1"))
        print("PASS  evaluating the same session three times creates one row")
    finally:
        cleanup(database, directory)


def test_new_policy_version_creates_a_separate_record():
    """Policy history must survive, not be overwritten."""
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)

        attendance.save_decision(a_decision(status=STATUS_PRESENT,
                                            policy_version="1.0"))
        attendance.save_decision(a_decision(status=STATUS_ABSENT,
                                            policy_version="2.0"))

        assert attendance.count_for_session("SES-1") == 2, (
            "a new policy version should add a row, not replace one")

        first = attendance.get("SES-1", "STU001", "1.0")
        second = attendance.get("SES-1", "STU001", "2.0")
        assert first["status"] == STATUS_PRESENT, "the v1 result was overwritten"
        assert second["status"] == STATUS_ABSENT, "the v2 result is wrong"
        assert attendance.policy_versions_for_session("SES-1") == ["1.0", "2.0"]
        print("PASS  a second policy version adds a row and preserves the first")
    finally:
        cleanup(database, directory)


def test_updating_attendance_in_place():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision(status=STATUS_PRESENT))

        updated = a_decision(status=STATUS_ABSENT, ratio=0.0,
                             present_seconds=0.0)
        attendance.save_decision(updated)

        record = attendance.get("SES-1", "STU001", "1.0")
        assert record["status"] == STATUS_ABSENT, "the record was not updated"
        assert attendance.count_for_session("SES-1") == 1, "a duplicate appeared"
        print("PASS  re-evaluating with the same policy updates in place")
    finally:
        cleanup(database, directory)


def test_invalid_attendance_status_is_rejected():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision())
        try:
            database.execute(
                "UPDATE attendance_records SET status = 'PROBABLY_THERE'")
        except sqlite3.IntegrityError:
            print("PASS  an invalid attendance status is refused")
            return
        raise AssertionError("an invalid attendance status was accepted")
    finally:
        cleanup(database, directory)


def test_invalid_numbers_are_rejected():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision())

        bad = [
            ("UPDATE attendance_records SET present_seconds = -1",
             "negative duration"),
            ("UPDATE attendance_records SET presence_ratio = 1.5",
             "a ratio above 1"),
            ("UPDATE attendance_records SET presence_ratio = -0.2",
             "a negative ratio"),
            ("UPDATE attendance_records SET segment_count = -3",
             "a negative segment count"),
        ]
        for sql, description in bad:
            try:
                database.execute(sql)
            except sqlite3.IntegrityError:
                continue
            raise AssertionError("the database accepted " + description)
        print("PASS  %d invalid numeric values are refused" % len(bad))
    finally:
        cleanup(database, directory)


def test_attendance_requires_an_existing_student():
    database, directory = temp_database()
    try:
        seed(database)
        attendance = AttendanceRepository(database)
        try:
            attendance.save_decision(a_decision(student_id="GHOST"))
        except sqlite3.IntegrityError:
            print("PASS  attendance for an unknown student is refused")
            return
        raise AssertionError("attendance was stored for a non-existent student")
    finally:
        cleanup(database, directory)


# -- presence segments -----------------------------------------------------

def test_presence_segments_persist():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        segments = [PresenceSegment("STU001", START, START + timedelta(minutes=20), 7),
                    PresenceSegment("STU001", START + timedelta(minutes=25), END, 15)]

        saved = attendance.save_segments("SES-1", segments)
        assert saved == 2, "expected 2 segments saved, got %d" % saved

        stored = attendance.get_segments("SES-1", "STU001")
        assert len(stored) == 2, "segments not read back"
        assert stored[0]["track_id"] == 7, "track id not preserved"
        assert stored[0]["duration_seconds"] == 1200, (
            "duration wrong: %s" % stored[0]["duration_seconds"])
        print("PASS  presence segments persist with their track ids")
    finally:
        cleanup(database, directory)


def test_saving_segments_twice_does_not_duplicate():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        segments = [PresenceSegment("STU001", START, END, 7)]

        attendance.save_segments("SES-1", segments)
        attendance.save_segments("SES-1", segments)

        assert len(attendance.get_segments("SES-1")) == 1, (
            "the same evidence was stored twice")
        print("PASS  saving the same evidence twice stores it once")
    finally:
        cleanup(database, directory)


def test_negative_segment_duration_is_rejected():
    database, directory = temp_database()
    try:
        seed(database)
        try:
            database.execute(
                """INSERT INTO presence_segments
                   (session_id, student_id, track_id, start_time, end_time,
                    duration_seconds, created_at)
                   VALUES ('SES-1','STU001',1,'2026-03-02T09:00:00+00:00',
                           '2026-03-02T09:10:00+00:00',-5,
                           '2026-03-02T09:00:00+00:00')""")
        except sqlite3.IntegrityError:
            print("PASS  a negative segment duration is refused")
            return
        raise AssertionError("a negative duration was accepted")
    finally:
        cleanup(database, directory)


# -- manual override -------------------------------------------------------

def test_manual_override_keeps_an_audit_trail():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision(status=STATUS_ABSENT))

        overridden = attendance.apply_override(
            "SES-1", "STU001", STATUS_PRESENT,
            reason="Student was present; camera was obstructed",
            actor="teacher:jdoe")

        assert overridden["status"] == STATUS_PRESENT, "the new status is wrong"
        assert overridden["original_status"] == STATUS_ABSENT, (
            "the computed status was lost")
        assert overridden["is_override"] == 1, "the override flag is not set"
        assert overridden["overridden_by"] == "teacher:jdoe", "actor not stored"
        assert overridden["overridden_at"], "override timestamp missing"
        assert "obstructed" in overridden["override_reason"], "reason not stored"
        print("PASS  a manual override records who, when, why, and what it was")
    finally:
        cleanup(database, directory)


def test_override_survives_re_evaluation():
    """A human correction must not be silently undone by a re-run."""
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision(status=STATUS_ABSENT))
        attendance.apply_override("SES-1", "STU001", STATUS_PRESENT,
                                  reason="verified manually", actor="teacher")

        attendance.save_decision(a_decision(status=STATUS_ABSENT))

        record = attendance.get("SES-1", "STU001", "1.0")
        assert record["status"] == STATUS_PRESENT, (
            "re-evaluation overwrote a manual override")
        assert record["is_override"] == 1, "the override flag was cleared"
        print("PASS  re-evaluation does not overwrite a manual override")
    finally:
        cleanup(database, directory)


def test_override_can_be_cleared():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision(status=STATUS_ABSENT))
        attendance.apply_override("SES-1", "STU001", STATUS_PRESENT,
                                  reason="mistake", actor="teacher")

        restored = attendance.clear_override("SES-1", "STU001")
        assert restored["status"] == STATUS_ABSENT, "the computed status was not restored"
        assert restored["is_override"] == 0, "the override flag was not cleared"
        assert restored["original_status"] is None, "the audit fields were not cleared"
        print("PASS  an override can be cleared, restoring the computed status")
    finally:
        cleanup(database, directory)


def test_override_of_missing_record_fails_loudly():
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        try:
            attendance.apply_override("SES-1", "STU001", STATUS_PRESENT,
                                      reason="x")
        except LookupError:
            print("PASS  overriding a non-existent record raises, not silently passes")
            return
        raise AssertionError("overriding a missing record should raise")
    finally:
        cleanup(database, directory)


def test_override_without_audit_trail_is_rejected():
    """The database itself refuses a half-recorded override."""
    database, directory = temp_database()
    try:
        _, _, attendance = seed(database)
        attendance.save_decision(a_decision())
        try:
            database.execute(
                "UPDATE attendance_records SET is_override = 1")
        except sqlite3.IntegrityError:
            print("PASS  an override without its audit trail is refused")
            return
        raise AssertionError("an override with no audit trail was accepted")
    finally:
        cleanup(database, directory)


# -- transactions ----------------------------------------------------------

def test_transaction_rolls_back_on_error():
    database, directory = temp_database()
    try:
        students = StudentRepository(database)
        students.upsert("STU001", "Asha")

        try:
            with database.transaction():
                students.upsert("STU002", "Ben")
                raise RuntimeError("something went wrong mid-write")
        except RuntimeError:
            pass

        assert students.get("STU002") is None, (
            "a failed transaction left data behind")
        assert students.get("STU001") is not None, (
            "the rollback removed data it should not have")
        print("PASS  a failed transaction rolls back cleanly")
    finally:
        cleanup(database, directory)


def test_transaction_commits_on_success():
    database, directory = temp_database()
    try:
        students = StudentRepository(database)
        with database.transaction():
            students.upsert("STU001", "Asha")
            students.upsert("STU002", "Ben")

        assert len(students.list_all()) == 2, "the transaction did not commit"
        print("PASS  a successful transaction commits")
    finally:
        cleanup(database, directory)


def test_errors_are_not_swallowed():
    database, directory = temp_database()
    try:
        seed(database)
        attendance = AttendanceRepository(database)
        try:
            attendance.save_decision(a_decision(student_id="NOBODY"))
        except sqlite3.IntegrityError:
            print("PASS  database integrity errors propagate instead of being hidden")
            return
        raise AssertionError("an integrity error was swallowed")
    finally:
        cleanup(database, directory)


def main():
    enable_safe_console()

    tests = [
        test_schema_is_created,
        test_repeated_initialization_is_safe,
        test_foreign_keys_are_enforced,
        test_tests_do_not_touch_the_real_database,
        test_insert_and_read_student,
        test_student_upsert_does_not_duplicate,
        test_student_table_holds_no_biometrics,
        test_insert_session_with_expected_students,
        test_session_completion_is_recorded,
        test_invalid_session_status_is_rejected,
        test_session_end_must_follow_start,
        test_insert_and_read_attendance,
        test_evaluating_twice_does_not_duplicate,
        test_new_policy_version_creates_a_separate_record,
        test_updating_attendance_in_place,
        test_invalid_attendance_status_is_rejected,
        test_invalid_numbers_are_rejected,
        test_attendance_requires_an_existing_student,
        test_presence_segments_persist,
        test_saving_segments_twice_does_not_duplicate,
        test_negative_segment_duration_is_rejected,
        test_manual_override_keeps_an_audit_trail,
        test_override_survives_re_evaluation,
        test_override_can_be_cleared,
        test_override_of_missing_record_fails_loudly,
        test_override_without_audit_trail_is_rejected,
        test_transaction_rolls_back_on_error,
        test_transaction_commits_on_success,
        test_errors_are_not_swallowed,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__, error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
