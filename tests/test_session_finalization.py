"""
Session finalisation tests - ending a lesson early must not erase attendance.

NO WEBCAM AND NO MODEL ARE NEEDED. Finalisation is driven directly, with
controlled presence segments standing in for what a camera would have seen.
Every test uses its own temporary SQLite file.

THE BUG THIS EXISTS TO PREVENT
    Attendance is a RATIO: presence divided by the length of the session. A
    lesson scheduled 10:00-11:00 and stopped by the teacher at 10:35 used to
    be divided by the full hour, so a student who sat through every second the
    camera was running scored 58% - and against a 75% policy that is PARTIAL,
    not PRESENT. They had done nothing wrong. Nobody was looking for the last
    25 minutes.

    Finalisation now measures over the window that was actually OBSERVED, and
    separately refuses to downgrade any attendance that was already recorded.

WHAT THIS DOES NOT VERIFY
    Recognition, liveness or tracking. Those decide WHO was present; these
    tests are about what happens to that answer afterwards.

Run with:
    python tests/test_session_finalization.py
"""

import shutil
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

from attendance_rules import PresenceSegment  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from liveness_detector import LIVENESS_MODE_OFF  # noqa: E402
from authenticity_detector import AUTHENTICITY_MODE_OFF  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import live_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

SESSION_ID = "SES-FINALIZE-TEST"
CLASS_ID = "CLASS-FINALIZE"

# A lesson in the past, so "now" is always after it and the test does not
# depend on how long it takes to run.
START = datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc)
SCHEDULED_END = START + timedelta(hours=1)


# -- harness ----------------------------------------------------------------

class StubPipeline:
    """
    Stands in for the live pipeline, and hands back fixed segments.

    Deliberately NOT a fake camera or a fake detector: what is under test is
    what finalisation does with presence evidence, so the evidence is given
    directly and the AI layers are left out of it entirely.
    """

    def __init__(self, segments):
        self._segments = segments
        self.closed = False
        self.liveness_manager = None
        self.liveness_mode = LIVENESS_MODE_OFF
        self.authenticity_mode = AUTHENTICITY_MODE_OFF

    def close(self):
        self.closed = True

    def segments(self, session_end=None):
        return list(self._segments)

    def authenticity_states(self):
        return {}


def student_ids(count):
    return ["STU_FIN_%03d" % index for index in range(1, count + 1)]


def temp_environment(expected, segments, session_status="ACTIVE"):
    """A database with a session, its roll, and a manager ready to finalise."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_finalize_"))
    state.configure(directory / "finalize.db")

    database = state.get_database()
    students = StudentRepository(database)
    sessions = SessionRepository(database)

    for student_id in expected:
        students.upsert(student_id, "Student %s" % student_id)

    session = AttendanceSession(SESSION_ID, CLASS_ID, START, SCHEDULED_END,
                                policy_version="1.0",
                                expected_students=list(expected))
    if session_status in ("ACTIVE", "COMPLETED"):
        session.start()
    sessions.save(session)

    manager = live_service.LiveSessionManager()
    manager._session_id = SESSION_ID
    manager._pipeline = StubPipeline(segments)
    live_service.reset_live_manager(manager)

    return {"directory": directory, "manager": manager, "database": database}


def teardown(environment):
    live_service.reset_live_manager(None)
    state.close_database()
    shutil.rmtree(environment["directory"], ignore_errors=True)


def present_segments(ids, start=START, end=None):
    """Segments for students who were in front of the camera the whole time."""
    end = end or (START + timedelta(minutes=35))
    return [PresenceSegment(student_id=student_id, track_id=index,
                            start=start, end=end)
            for index, student_id in enumerate(ids, start=1)]


def finalise(environment, stopped_at):
    """Run the real finalisation, with the clock standing still where told."""
    manager = environment["manager"]
    original = live_service._utc_now
    live_service._utc_now = lambda: stopped_at
    try:
        return manager._finalize(environment["database"])
    finally:
        live_service._utc_now = original


def statuses(database):
    rows = AttendanceRepository(database).list_for_session(SESSION_ID)
    return {row["student_id"]: row["status"] for row in rows}


def tally(database):
    counts = {}
    for status in statuses(database).values():
        counts[status] = counts.get(status, 0) + 1
    return counts


# -- the window ------------------------------------------------------------

def test_the_observed_window_is_the_one_attendance_is_measured_over():
    session = AttendanceSession(SESSION_ID, CLASS_ID, START, SCHEDULED_END,
                                policy_version="1.0",
                                expected_students=["A"])

    stopped_early = START + timedelta(minutes=35)
    shortened, early = live_service.LiveSessionManager._observed_window(
        session, stopped_early)
    assert early is True
    assert shortened.end_time == stopped_early
    assert shortened.duration_seconds == 35 * 60
    assert session.end_time == SCHEDULED_END, (
        "shortening the evaluation window rewrote the session's end time")

    # On time and late both leave the lesson exactly as scheduled.
    for stopped in (SCHEDULED_END, SCHEDULED_END + timedelta(minutes=10)):
        same, early = live_service.LiveSessionManager._observed_window(
            session, stopped)
        assert early is False
        assert same.end_time == SCHEDULED_END

    # A stop at or before the start observed nothing at all, and a zero-length
    # window cannot be divided by.
    unchanged, early = live_service.LiveSessionManager._observed_window(
        session, START)
    assert early is True
    assert unchanged.end_time == SCHEDULED_END

    print("PASS  the evaluation window shortens to what was observed, and "
          "never lengthens or rewrites the session")


# -- the eight scenarios ---------------------------------------------------

def test_seven_of_ten_present_stay_present_when_ended_early():
    """Expected 10, present 7, stopped early -> 7 present, 3 absent."""
    expected = student_ids(10)
    environment = temp_environment(expected, present_segments(expected[:7]))
    try:
        summary = finalise(environment, START + timedelta(minutes=35))

        counts = tally(environment["database"])
        assert counts.get("PRESENT") == 7, "counts were %s" % counts
        assert counts.get("ABSENT") == 3, "counts were %s" % counts
        assert summary["ended_early"] is True

        print("PASS  expected 10, present 7, ended early -> 7 present, "
              "3 absent")
    finally:
        teardown(environment)


def test_nobody_present_is_everybody_absent():
    expected = student_ids(10)
    environment = temp_environment(expected, [])
    try:
        finalise(environment, START + timedelta(minutes=35))

        counts = tally(environment["database"])
        assert counts.get("ABSENT") == 10, "counts were %s" % counts
        assert "PRESENT" not in counts

        print("PASS  expected 10, present 0, ended early -> 0 present, "
              "10 absent")
    finally:
        teardown(environment)


def test_everybody_present_stays_present():
    expected = student_ids(10)
    environment = temp_environment(expected, present_segments(expected))
    try:
        finalise(environment, START + timedelta(minutes=35))

        counts = tally(environment["database"])
        assert counts.get("PRESENT") == 10, "counts were %s" % counts
        assert "ABSENT" not in counts

        print("PASS  expected 10, present 10, ended early -> 10 present, "
              "0 absent")
    finally:
        teardown(environment)


def test_a_student_present_for_the_whole_observed_window_is_present():
    """
    The exact case that used to fail.

    One student, in front of the camera for every second it was running, in a
    lesson stopped 25 minutes early. Against the scheduled hour their ratio is
    0.58 and the 75% policy calls that PARTIAL. Against the observed window it
    is 1.0, and they are PRESENT - which is what happened.
    """
    expected = ["STU_FIN_001"]
    stopped = START + timedelta(minutes=35)
    environment = temp_environment(
        expected, present_segments(expected, end=stopped))
    try:
        finalise(environment, stopped)

        record = AttendanceRepository(environment["database"]).get(
            SESSION_ID, "STU_FIN_001")
        assert record["status"] == "PRESENT", (
            "a student present for the entire observed window was recorded "
            "as %s" % record["status"])
        assert record["presence_ratio"] >= 0.99, (
            "presence ratio was %.2f" % record["presence_ratio"])

        print("PASS  a student present for the whole observed window is "
              "PRESENT, not PARTIAL")
    finally:
        teardown(environment)


def test_a_student_never_seen_is_absent():
    expected = ["STU_FIN_001", "STU_FIN_002"]
    environment = temp_environment(expected,
                                   present_segments(["STU_FIN_001"]))
    try:
        finalise(environment, START + timedelta(minutes=35))

        recorded = statuses(environment["database"])
        assert recorded["STU_FIN_001"] == "PRESENT"
        assert recorded["STU_FIN_002"] == "ABSENT", (
            "a student who never appeared was recorded as %s"
            % recorded["STU_FIN_002"])

        print("PASS  an expected student who was never seen is ABSENT")
    finally:
        teardown(environment)


def test_finalising_twice_changes_nothing():
    """
    A double-clicked Stop, or a re-run after a crash.

    Finalising again must not create a second record and must not be able to
    turn an attendance into an absence - nothing about finalising is evidence
    that a student was not there.
    """
    expected = student_ids(10)
    environment = temp_environment(expected, present_segments(expected[:7]))
    try:
        database = environment["database"]

        finalise(environment, START + timedelta(minutes=35))
        first = statuses(database)
        rows_before = len(AttendanceRepository(database)
                          .list_for_session(SESSION_ID))

        # Second pass, with NO evidence at all - the pipeline is closed and a
        # fresh finalisation would otherwise see nobody.
        environment["manager"]._pipeline = StubPipeline([])
        summary = finalise(environment, START + timedelta(minutes=40))

        second = statuses(database)
        rows_after = len(AttendanceRepository(database)
                         .list_for_session(SESSION_ID))

        assert rows_after == rows_before, (
            "finalising twice created %d extra attendance rows"
            % (rows_after - rows_before))

        for student_id, status in first.items():
            if status == "PRESENT":
                assert second[student_id] == "PRESENT", (
                    "%s went from PRESENT to %s on a second finalisation"
                    % (student_id, second[student_id]))

        assert len(summary["attendance_preserved"]) == 7, (
            "preserved %s" % summary["attendance_preserved"])

        print("PASS  finalising twice adds no rows and never turns PRESENT "
              "into ABSENT")
    finally:
        teardown(environment)


def test_ending_exactly_on_time_uses_the_same_logic():
    expected = student_ids(4)
    environment = temp_environment(
        expected, present_segments(expected[:3], end=SCHEDULED_END))
    try:
        summary = finalise(environment, SCHEDULED_END)

        counts = tally(environment["database"])
        assert summary["ended_early"] is False
        assert counts.get("PRESENT") == 3, "counts were %s" % counts
        assert counts.get("ABSENT") == 1, "counts were %s" % counts

        print("PASS  ending exactly on time finalises the same way")
    finally:
        teardown(environment)


def test_ending_after_the_scheduled_end_uses_the_same_logic():
    """
    Stopping late does not lengthen the lesson.

    The scheduled window still divides, so a student present for all of it is
    present - and evidence recorded after the bell does not create extra
    credit.
    """
    expected = student_ids(4)
    environment = temp_environment(
        expected, present_segments(expected[:3], end=SCHEDULED_END))
    try:
        summary = finalise(environment, SCHEDULED_END + timedelta(minutes=15))

        counts = tally(environment["database"])
        assert summary["ended_early"] is False
        assert summary["observed_end"] == SCHEDULED_END.isoformat()
        assert counts.get("PRESENT") == 3, "counts were %s" % counts
        assert counts.get("ABSENT") == 1, "counts were %s" % counts

        print("PASS  ending late finalises over the scheduled window, not a "
              "longer one")
    finally:
        teardown(environment)


# -- what finalisation must never destroy ----------------------------------

def test_presence_evidence_is_stored_even_for_students_who_end_up_absent():
    """
    Blocked or partial presence is still presence that was OBSERVED.

    Storing it is what lets a teacher review a decision they disagree with.
    """
    expected = student_ids(3)
    brief = [PresenceSegment(student_id="STU_FIN_003", track_id=9,
                             start=START, end=START + timedelta(seconds=20))]
    environment = temp_environment(
        expected, present_segments(expected[:2]) + brief)
    try:
        finalise(environment, START + timedelta(minutes=35))

        database = environment["database"]
        segments = AttendanceRepository(database).get_segments(SESSION_ID)
        owners = {segment["student_id"] for segment in segments}

        assert "STU_FIN_003" in owners, (
            "the brief appearance was not stored as evidence")
        assert statuses(database)["STU_FIN_003"] in ("ABSENT", "PARTIAL"), (
            "20 seconds of an hour should not be a full attendance")

        print("PASS  presence evidence is stored even when it does not earn "
              "an attendance")
    finally:
        teardown(environment)


def test_a_manual_override_survives_finalisation():
    expected = student_ids(2)
    environment = temp_environment(expected, present_segments(expected))
    try:
        database = environment["database"]
        attendance = AttendanceRepository(database)

        finalise(environment, START + timedelta(minutes=35))

        attendance.apply_override(SESSION_ID, "STU_FIN_001", "ABSENT",
                                  "Marked absent by the teacher",
                                  actor="a teacher")

        environment["manager"]._pipeline = StubPipeline(
            present_segments(expected))
        finalise(environment, START + timedelta(minutes=40))

        record = attendance.get(SESSION_ID, "STU_FIN_001")
        assert record["is_override"] == 1, "the override was cleared"
        assert record["status"] == "ABSENT", (
            "finalising overwrote a teacher's correction with %s"
            % record["status"])
        assert record["override_reason"] == "Marked absent by the teacher"

        print("PASS  a teacher's correction survives a later finalisation")
    finally:
        teardown(environment)


def test_the_session_keeps_its_scheduled_times():
    expected = student_ids(2)
    environment = temp_environment(expected, present_segments(expected))
    try:
        finalise(environment, START + timedelta(minutes=35))

        row = SessionRepository(environment["database"]).get(SESSION_ID)
        assert row["start_time"].startswith(START.isoformat()[:19]), row
        assert row["end_time"].startswith(SCHEDULED_END.isoformat()[:19]), (
            "the scheduled end was rewritten to the early stop: %s"
            % row["end_time"])
        assert row["status"] == "COMPLETED"
        assert row["completed_at"], "no completion time was recorded"

        print("PASS  the session keeps its scheduled window and records when "
              "it actually finished")
    finally:
        teardown(environment)


def test_the_summary_explains_that_the_window_was_shortened():
    expected = student_ids(2)
    environment = temp_environment(expected, present_segments(expected))
    try:
        summary = finalise(environment, START + timedelta(minutes=35))

        assert summary["ended_early"] is True
        assert summary["scheduled_end"] == SCHEDULED_END.isoformat()
        assert summary["observed_end"] == (
            START + timedelta(minutes=35)).isoformat()
        assert "stopped before its scheduled end" in summary["note"], (
            "the summary does not say the window was shortened: %s"
            % summary["note"])

        print("PASS  the summary reports the shortened window rather than "
              "hiding it")
    finally:
        teardown(environment)


def test_preserving_attendance_only_blocks_downgrades():
    """
    The guard must not freeze a record. It blocks one direction only.
    """
    from repositories import AttendanceRepository as Repository

    expected = student_ids(2)
    environment = temp_environment(expected, [])
    try:
        database = environment["database"]

        # Everybody absent to begin with.
        finalise(environment, START + timedelta(minutes=35))
        assert tally(database).get("ABSENT") == 2

        # Now the evidence arrives - a correction upwards must go through.
        environment["manager"]._pipeline = StubPipeline(
            present_segments(expected))
        finalise(environment, START + timedelta(minutes=35))

        recorded = statuses(database)
        assert all(status == "PRESENT" for status in recorded.values()), (
            "an upgrade from ABSENT to PRESENT was blocked: %s" % recorded)
        assert len(Repository(database).list_for_session(SESSION_ID)) == 2

        print("PASS  the guard blocks downgrades only - ABSENT can still "
              "become PRESENT")
    finally:
        teardown(environment)


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_the_observed_window_is_the_one_attendance_is_measured_over,
        test_seven_of_ten_present_stay_present_when_ended_early,
        test_nobody_present_is_everybody_absent,
        test_everybody_present_stays_present,
        test_a_student_present_for_the_whole_observed_window_is_present,
        test_a_student_never_seen_is_absent,
        test_finalising_twice_changes_nothing,
        test_ending_exactly_on_time_uses_the_same_logic,
        test_ending_after_the_scheduled_end_uses_the_same_logic,
        test_presence_evidence_is_stored_even_for_students_who_end_up_absent,
        test_a_manual_override_survives_finalisation,
        test_the_session_keeps_its_scheduled_times,
        test_the_summary_explains_that_the_window_was_shortened,
        test_preserving_attendance_only_blocks_downgrades,
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
