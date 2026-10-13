"""
End-to-end attendance test (Step 6).

SYNTHETIC. A hand-built presence timeline is pushed through the whole chain:

    presence events -> segments -> policy -> rule engine -> decisions
                    -> SQLite -> read back -> verified

Nothing here is a real-world result. These are constructed timelines with
constructed students, used to prove the pipeline is wired together correctly.
Real classroom attendance accuracy has NOT been established.

Run with:
    python tests/test_attendance_end_to_end.py
"""

import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

from attendance_rules import (  # noqa: E402
    STATUS_ABSENT,
    STATUS_LATE,
    STATUS_PARTIAL,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    AttendancePolicy,
    AttendanceRuleEngine,
    segments_from_events,
)
from attendance_session import AttendanceSession  # noqa: E402
from database import Database  # noqa: E402
from presence_timeline import (  # noqa: E402
    EVENT_IDENTITY_CONFIRMED,
    EVENT_TRACK_EXITED,
    EVENT_TRACK_REACQUIRED,
    EVENT_TRACK_STARTED,
    EVENT_TRACK_TEMPORARILY_LOST,
    EVENT_UNKNOWN_PERSON,
    PresenceEvent,
)
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)

# A synthetic class. These are not real people.
CLASS = {
    "STU001": "Student One",
    "STU002": "Student Two",
    "STU003": "Student Three",
    "STU004": "Student Four",
}


def at(minutes, seconds=0):
    return START + timedelta(minutes=minutes, seconds=seconds)


def event(event_type, minute, track_id, student_id=None, second=0, frame=0):
    return PresenceEvent(event_type, track_id, frame, at(minute, second),
                         student_id=student_id)


def build_timeline():
    """
    A deliberately varied synthetic session:

      STU001  arrives on time, briefly lost, stays to the end   -> PRESENT
      STU002  arrives at 09:14, stays to the end                -> LATE
      STU003  present for 15 minutes only                       -> PARTIAL
      STU004  never appears                                     -> ABSENT
      UNKNOWN a visible but unidentified person                 -> no record
    """
    return [
        # STU001: on time, momentary loss inside the grace period.
        event(EVENT_TRACK_STARTED, 0, 1),
        event(EVENT_IDENTITY_CONFIRMED, 0, 1, "STU001", second=4),
        event(EVENT_TRACK_TEMPORARILY_LOST, 25, 1, "STU001"),
        event(EVENT_TRACK_REACQUIRED, 25, 1, "STU001", second=10),
        event(EVENT_TRACK_EXITED, 60, 1, "STU001"),

        # STU002: late arrival, then present to the end.
        event(EVENT_TRACK_STARTED, 14, 2),
        event(EVENT_IDENTITY_CONFIRMED, 14, 2, "STU002", second=6),
        event(EVENT_TRACK_EXITED, 60, 2, "STU002"),

        # STU003: a short visit only.
        event(EVENT_TRACK_STARTED, 5, 3),
        event(EVENT_IDENTITY_CONFIRMED, 5, 3, "STU003"),
        event(EVENT_TRACK_EXITED, 20, 3, "STU003"),

        # An unidentified person, visible for a long time.
        event(EVENT_TRACK_STARTED, 10, 4),
        event(EVENT_UNKNOWN_PERSON, 10, 4, second=30),
        event(EVENT_TRACK_EXITED, 55, 4),
    ]


def run_pipeline(database_path, policy=None, complete_session=True):
    """Run the whole chain and return everything needed to assert on it."""
    policy = policy or AttendancePolicy(
        version="1.0", minimum_presence_ratio=0.75,
        late_after_seconds=600, early_leave_before_seconds=300,
        temporary_loss_grace_seconds=30)

    session = AttendanceSession("SES-2026-03-02-10A", "CLASS-10A", START, END,
                                policy_version=policy.version,
                                expected_students=list(CLASS))
    session.start()
    if complete_session:
        session.complete(completed_at=END)

    events = build_timeline()
    segments = segments_from_events(
        events, grace_seconds=policy.temporary_loss_grace_seconds,
        session_end=END)

    engine = AttendanceRuleEngine(policy)
    decisions = engine.evaluate_session(session, segments,
                                        session.expected_students)

    database = Database(database_path)
    database.connect()
    database.initialize()

    students = StudentRepository(database)
    sessions = SessionRepository(database)
    attendance = AttendanceRepository(database)

    for student_id, name in CLASS.items():
        students.upsert(student_id, name)

    sessions.save(session)
    attendance.save_segments(session.session_id, segments)
    attendance.save_decisions(decisions)

    return database, session, segments, decisions, attendance


def temp_path():
    directory = Path(tempfile.mkdtemp(prefix="classvision_e2e_"))
    return directory / "e2e.db", directory


# -- the end-to-end test ---------------------------------------------------

def test_end_to_end_attendance():
    path, directory = temp_path()
    try:
        database, session, segments, decisions, attendance = run_pipeline(path)

        stored = {row["student_id"]: row
                  for row in attendance.list_for_session(session.session_id)}

        expected = {
            "STU001": STATUS_PRESENT,
            "STU002": STATUS_LATE,
            "STU003": STATUS_PARTIAL,
            "STU004": STATUS_ABSENT,
        }

        for student_id, want in expected.items():
            assert student_id in stored, "no record stored for %s" % student_id
            got = stored[student_id]["status"]
            assert got == want, (
                "%s should be %s but is %s (%s)"
                % (student_id, want, got, stored[student_id]["reason"]))

        print("PASS  end-to-end: " + ", ".join(
            "%s=%s" % (s, stored[s]["status"]) for s in sorted(expected)))
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_unknown_person_produces_no_attendance_record():
    """The hard rule: a visible stranger is never somebody's attendance."""
    path, directory = temp_path()
    try:
        database, session, segments, _, attendance = run_pipeline(path)

        records = attendance.list_for_session(session.session_id)
        assert len(records) == 4, (
            "expected 4 records for 4 expected students, got %d" % len(records))

        ids = {row["student_id"] for row in records}
        assert ids == set(CLASS), "unexpected students appeared: %s" % ids

        # The unknown person's track produced no segment at all.
        assert all(s.track_id != 4 for s in segments), (
            "the unidentified track produced a presence segment")

        print("PASS  the unidentified person produced no attendance record and "
              "no presence segment")
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_temporary_loss_did_not_break_presence():
    path, directory = temp_path()
    try:
        database, session, segments, _, attendance = run_pipeline(path)

        mine = [s for s in segments if s.student_id == "STU001"]
        assert len(mine) == 1, (
            "STU001's 10-second loss split their presence into %d segments"
            % len(mine))

        record = attendance.get(session.session_id, "STU001")
        assert record["segment_count"] == 1, "segment count should be 1"
        assert record["presence_ratio"] > 0.98, (
            "the brief loss reduced the ratio to %.3f" % record["presence_ratio"])

        print("PASS  a 10-second loss inside the grace period left presence "
              "continuous (ratio %.3f)" % record["presence_ratio"])
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_absence_requires_a_completed_session():
    path, directory = temp_path()
    try:
        database, session, _, _, attendance = run_pipeline(
            path, complete_session=False)

        record = attendance.get(session.session_id, "STU004")
        assert record["status"] == STATUS_UNKNOWN, (
            "an active session marked STU004 %s instead of UNKNOWN"
            % record["status"])
        assert "cannot be determined" in record["reason"], (
            "the reason should explain why absence is undecided")

        print("PASS  while the session is ACTIVE, the missing student is "
              "UNKNOWN, not ABSENT")
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_evidence_is_stored_for_re_evaluation():
    path, directory = temp_path()
    try:
        database, session, segments, _, attendance = run_pipeline(path)

        stored = attendance.get_segments(session.session_id)
        assert len(stored) == len(segments), (
            "expected %d stored segments, got %d" % (len(segments), len(stored)))

        for row in stored:
            assert row["student_id"] in CLASS, "a segment has an unknown student"
            assert row["duration_seconds"] > 0, "a zero-length segment was stored"

        print("PASS  %d presence segments were persisted as re-usable evidence"
              % len(stored))
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_re_evaluation_with_a_new_policy():
    """Same stored evidence, stricter policy, a different answer - both kept."""
    path, directory = temp_path()
    try:
        database, session, segments, _, attendance = run_pipeline(path)

        before = attendance.get(session.session_id, "STU002", "1.0")
        assert before["status"] == STATUS_LATE, "setup failed"

        strict = AttendancePolicy(version="2.0", minimum_presence_ratio=0.95,
                                  late_after_seconds=600,
                                  early_leave_before_seconds=300)
        decisions = AttendanceRuleEngine(strict).evaluate_session(
            session, segments, session.expected_students)
        attendance.save_decisions(decisions)

        after = attendance.get(session.session_id, "STU002", "2.0")
        assert after["status"] == STATUS_PARTIAL, (
            "under the strict policy STU002 should be PARTIAL, got %s"
            % after["status"])

        still_there = attendance.get(session.session_id, "STU002", "1.0")
        assert still_there["status"] == STATUS_LATE, (
            "the version 1.0 result was overwritten")

        versions = attendance.policy_versions_for_session(session.session_id)
        assert versions == ["1.0", "2.0"], "both versions should exist: %s" % versions

        print("PASS  the same evidence under policy 2.0 gives PARTIAL while the "
              "1.0 result (LATE) is preserved")
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_running_the_whole_pipeline_twice_is_idempotent():
    path, directory = temp_path()
    try:
        database, session, _, _, attendance = run_pipeline(path)
        first = attendance.count_for_session(session.session_id)
        first_segments = len(attendance.get_segments(session.session_id))
        database.close()

        database, session, _, _, attendance = run_pipeline(path)
        second = attendance.count_for_session(session.session_id)
        second_segments = len(attendance.get_segments(session.session_id))

        assert first == second == 4, (
            "running twice changed the record count: %d then %d" % (first, second))
        assert first_segments == second_segments, (
            "running twice duplicated evidence: %d then %d"
            % (first_segments, second_segments))

        print("PASS  running the entire pipeline twice produced no duplicates "
              "(%d records, %d segments)" % (second, second_segments))
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_decisions_explain_themselves_end_to_end():
    path, directory = temp_path()
    try:
        database, session, _, _, attendance = run_pipeline(path)

        for row in attendance.list_for_session(session.session_id):
            assert row["reason"], "%s has no reason" % row["student_id"]
            assert row["policy_version"] == "1.0", "policy version missing"
            assert row["session_seconds"] == 3600.0, (
                "session length wrong: %s" % row["session_seconds"])

        print("PASS  every stored record carries a reason and a policy version")
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_database_contains_no_biometric_data():
    path, directory = temp_path()
    try:
        database, _, _, _, _ = run_pipeline(path)

        banned = ("embedding", "vector", "descriptor", "face", "image",
                  "photo", "crop", "frame_data")

        # One column names a FILE and holds no bytes: users.photo_name is the
        # filename of a teacher's personnel photograph stored under
        # data/staff/. It is not a biometric record - nothing computes an
        # embedding from it, the recognition pipeline never opens it, and it
        # describes a member of staff rather than a student being identified.
        #
        # It is listed here by its exact name rather than by loosening the
        # word list, so any OTHER column that acquires one of these words
        # still fails this test.
        allowed = {"users.photo_name"}

        for table in database.table_names():
            for column in database.query("PRAGMA table_info(%s)" % table):
                name = column["name"].lower()
                qualified = "%s.%s" % (table, name)

                # Whatever it is called, no column may be declared to hold
                # binary. This is stricter than the name check and is what
                # actually stops an image being written into SQLite.
                assert "BLOB" not in (column["type"] or "").upper(), (
                    "%s is a BLOB - the database must hold no binary data"
                    % qualified)

                if qualified in allowed:
                    continue

                for word in banned:
                    assert word not in name, (
                        "%s looks like biometric storage" % qualified)

        database.close()

        # The file itself must not contain image magic bytes.
        blob = path.read_bytes()
        for magic, label in ((b"\xff\xd8\xff", "JPEG"), (b"\x89PNG", "PNG")):
            assert magic not in blob, "the database file contains %s data" % label

        print("PASS  no biometric column and no image data in the database file")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- performance -----------------------------------------------------------

def test_performance_is_measured():
    """
    Measure the rule engine and the database separately.

    The point is to know whether attendance calculation is a bottleneck, not
    to claim a benchmark.
    """
    path, directory = temp_path()
    try:
        students = ["STU%03d" % index for index in range(1, 31)]
        events = []
        for index, student_id in enumerate(students, start=1):
            events.append(event(EVENT_IDENTITY_CONFIRMED, 0, index, student_id))
            events.append(event(EVENT_TRACK_TEMPORARILY_LOST, 30, index, student_id))
            events.append(event(EVENT_TRACK_REACQUIRED, 30, index, student_id,
                                second=10))
            events.append(event(EVENT_TRACK_EXITED, 58, index, student_id))

        policy = AttendancePolicy(version="1.0", minimum_presence_ratio=0.75)
        session = AttendanceSession("PERF-1", "CLASS-PERF", START, END,
                                    expected_students=students)
        session.start()
        session.complete(completed_at=END)

        started = time.perf_counter()
        segments = segments_from_events(events, session_end=END)
        decisions = AttendanceRuleEngine(policy).evaluate_session(
            session, segments, students)
        rules_ms = (time.perf_counter() - started) * 1000.0

        database = Database(path)
        database.connect()
        database.initialize()
        student_repository = StudentRepository(database)
        for student_id in students:
            student_repository.upsert(student_id, "Name " + student_id)
        SessionRepository(database).save(session)
        attendance = AttendanceRepository(database)

        started = time.perf_counter()
        attendance.save_segments(session.session_id, segments)
        attendance.save_decisions(decisions)
        write_ms = (time.perf_counter() - started) * 1000.0

        assert len(decisions) == 30, "expected 30 decisions"
        assert attendance.count_for_session("PERF-1") == 30, "not all rows stored"

        print("PASS  measured: %d events, %d students, %d segments | "
              "rules %.2f ms, database write %.2f ms, total %.2f ms"
              % (len(events), len(students), len(segments), rules_ms,
                 write_ms, rules_ms + write_ms))
        database.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main():
    enable_safe_console()

    tests = [
        test_end_to_end_attendance,
        test_unknown_person_produces_no_attendance_record,
        test_temporary_loss_did_not_break_presence,
        test_absence_requires_a_completed_session,
        test_evidence_is_stored_for_re_evaluation,
        test_re_evaluation_with_a_new_policy,
        test_running_the_whole_pipeline_twice_is_idempotent,
        test_decisions_explain_themselves_end_to_end,
        test_database_contains_no_biometric_data,
        test_performance_is_measured,
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
