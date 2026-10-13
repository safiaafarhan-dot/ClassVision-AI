"""
Analytics tests for backend/services/analytics_service.py (Step 7).

SYNTHETIC. Every database is built by hand in a temporary file so each metric
can be asserted against a known answer. No real classroom data is involved and
no accuracy claim is made.

Run with:
    python tests/test_dashboard_analytics.py
"""

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

from attendance_rules import (  # noqa: E402
    AttendanceDecision,
    PresenceSegment,
)
from attendance_session import AttendanceSession  # noqa: E402
from database import Database  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services.analytics_service import (  # noqa: E402
    SATISFYING_STATUSES,
    AnalyticsService,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def temp_database():
    directory = Path(tempfile.mkdtemp(prefix="classvision_an_"))
    database = Database(directory / "analytics.db")
    database.connect()
    database.initialize()
    return database, directory


def cleanup(database, directory):
    database.close()
    shutil.rmtree(directory, ignore_errors=True)


class Builder:
    """Builds a known database, so every expected number can be written down."""

    def __init__(self, database):
        self.database = database
        self.students = StudentRepository(database)
        self.sessions = SessionRepository(database)
        self.attendance = AttendanceRepository(database)

    def student(self, student_id, name=None):
        self.students.upsert(student_id, name or ("Name " + student_id))
        return self

    def session(self, session_id, class_id="C1", day=0, status="COMPLETED",
                expected=(), policy_version="1.0"):
        start = START + timedelta(days=day)
        end = start + timedelta(hours=1)

        session = AttendanceSession(session_id, class_id, start, end,
                                    policy_version=policy_version,
                                    expected_students=list(expected))
        if status in ("ACTIVE", "COMPLETED"):
            session.start()
        if status == "COMPLETED":
            session.complete(completed_at=end)
        if status == "CANCELLED":
            session.cancel()

        self.sessions.save(session)
        return self

    def record(self, session_id, student_id, status, ratio=0.9,
               present_seconds=3240.0, policy_version="1.0", day=0,
               late=False, early=False):
        decision = AttendanceDecision(student_id, session_id, status,
                                      "synthetic test record", policy_version)
        decision.present_seconds = present_seconds
        decision.session_seconds = 3600.0
        decision.presence_ratio = ratio
        decision.first_seen = START + timedelta(days=day)
        decision.last_seen = decision.first_seen + timedelta(hours=1)
        decision.was_late = late
        decision.left_early = early
        self.attendance.save_decision(decision)
        return self

    def segments(self, session_id, student_id, count=1, day=0):
        start = START + timedelta(days=day)
        items = [PresenceSegment(student_id,
                                 start + timedelta(minutes=index * 20),
                                 start + timedelta(minutes=index * 20 + 15),
                                 track_id=index + 1)
                 for index in range(count)]
        self.attendance.save_segments(session_id, items)
        return self


# -- definitions -----------------------------------------------------------

def test_satisfying_statuses_are_explicit():
    """LATE and LEFT_EARLY met the presence requirement; PARTIAL did not."""
    assert set(SATISFYING_STATUSES) == {"PRESENT", "LATE", "LEFT_EARLY"}, (
        "the satisfying set changed: %s" % (SATISFYING_STATUSES,))
    assert "PARTIAL" not in SATISFYING_STATUSES, "PARTIAL must not count as met"
    assert "ABSENT" not in SATISFYING_STATUSES, "ABSENT must not count as met"
    print("PASS  satisfying statuses are exactly PRESENT, LATE, LEFT_EARLY")


# -- empty database --------------------------------------------------------

def test_overview_on_empty_database():
    """No data is not the same fact as zero."""
    database, directory = temp_database()
    try:
        overview = AnalyticsService(database).overview()

        assert overview["has_data"] is False, "an empty database should say so"
        assert overview["attendance_rate"] is None, (
            "an empty database must not report a 0%% rate - got %r"
            % overview["attendance_rate"])
        assert overview["sample_size"] == 0, "sample size should be zero"
        assert overview["completed_sessions"] == 0, "no sessions expected"
        print("PASS  empty database returns has_data=False and a null rate, "
              "never a fabricated 0%")
    finally:
        cleanup(database, directory)


def test_real_zero_is_distinct_from_no_data():
    """A student who attended nothing has a REAL 0.0 rate, not null."""
    database, directory = temp_database()
    try:
        Builder(database).student("STU001") \
            .session("S1", expected=["STU001"]) \
            .record("S1", "STU001", "ABSENT", ratio=0.0, present_seconds=0.0)

        analytics = AnalyticsService(database).student_analytics("STU001")

        assert analytics["attendance_rate"] == 0.0, (
            "a genuinely absent student should have a real 0.0, got %r"
            % analytics["attendance_rate"])
        assert analytics["sample_size"] == 1, "one expectation was recorded"
        print("PASS  a real zero is 0.0 while no data is null - the two are "
              "distinguishable")
    finally:
        cleanup(database, directory)


# -- session analytics -----------------------------------------------------

def test_session_analytics_counts():
    database, directory = temp_database()
    try:
        builder = Builder(database)
        for student_id in ("STU001", "STU002", "STU003", "STU004"):
            builder.student(student_id)

        builder.session("S1", expected=["STU001", "STU002", "STU003", "STU004"])
        builder.record("S1", "STU001", "PRESENT", ratio=0.95)
        builder.record("S1", "STU002", "LATE", ratio=0.80, late=True)
        builder.record("S1", "STU003", "PARTIAL", ratio=0.30)
        builder.record("S1", "STU004", "ABSENT", ratio=0.0, present_seconds=0.0)

        analytics = AnalyticsService(database).session_analytics("S1")

        assert analytics["expected_students"] == 4, "expected count wrong"
        assert analytics["counts"]["PRESENT"] == 1, "present count wrong"
        assert analytics["counts"]["LATE"] == 1, "late count wrong"
        assert analytics["counts"]["PARTIAL"] == 1, "partial count wrong"
        assert analytics["counts"]["ABSENT"] == 1, "absent count wrong"
        assert analytics["satisfying_count"] == 2, (
            "PRESENT + LATE should satisfy, got %d" % analytics["satisfying_count"])
        assert analytics["attendance_rate"] == 0.5, (
            "2 of 4 should be 0.5, got %r" % analytics["attendance_rate"])
        assert analytics["is_final"] is True, "a completed session is final"
        print("PASS  session analytics: 2 of 4 satisfying = 0.500 attendance rate")
    finally:
        cleanup(database, directory)


def test_session_analytics_unknown_session():
    database, directory = temp_database()
    try:
        assert AnalyticsService(database).session_analytics("nope") is None, (
            "an unknown session should return None, not an empty result")
        print("PASS  analytics for an unknown session returns None")
    finally:
        cleanup(database, directory)


def test_active_and_cancelled_sessions_excluded():
    """Attendance is not final until a session is completed."""
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        builder.session("DONE", day=0, status="COMPLETED", expected=["STU001"])
        builder.session("LIVE", day=1, status="ACTIVE", expected=["STU001"])
        builder.session("GONE", day=2, status="CANCELLED", expected=["STU001"])
        builder.session("SOON", day=3, status="SCHEDULED", expected=["STU001"])

        builder.record("DONE", "STU001", "PRESENT")
        builder.record("LIVE", "STU001", "PRESENT")
        builder.record("GONE", "STU001", "PRESENT")

        overview = AnalyticsService(database).overview()
        assert overview["completed_sessions"] == 1, (
            "only the completed session should count, got %d"
            % overview["completed_sessions"])

        student = AnalyticsService(database).student_analytics("STU001")
        assert student["expected_sessions"] == 1, (
            "only completed sessions belong in the denominator, got %d"
            % student["expected_sessions"])
        print("PASS  active, cancelled and scheduled sessions are excluded")
    finally:
        cleanup(database, directory)


# -- student analytics -----------------------------------------------------

def test_student_attendance_rate_over_multiple_sessions():
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        for day in range(4):
            builder.session("S%d" % day, day=day, expected=["STU001"])

        builder.record("S0", "STU001", "PRESENT", ratio=1.0, day=0)
        builder.record("S1", "STU001", "LATE", ratio=0.8, day=1, late=True)
        builder.record("S2", "STU001", "PARTIAL", ratio=0.4, day=2)
        builder.record("S3", "STU001", "ABSENT", ratio=0.0,
                       present_seconds=0.0, day=3)

        analytics = AnalyticsService(database).student_analytics("STU001")

        assert analytics["expected_sessions"] == 4, "denominator wrong"
        assert analytics["satisfying_count"] == 2, "PRESENT + LATE satisfy"
        assert analytics["attendance_rate"] == 0.5, (
            "2 of 4 = 0.5, got %r" % analytics["attendance_rate"])
        assert analytics["late_rate"] == 0.25, "1 of 4 late"
        assert analytics["partial_rate"] == 0.25, "1 of 4 partial"
        assert analytics["absence_rate"] == 0.25, "1 of 4 absent"
        assert abs(analytics["average_presence_ratio"] - 0.55) < 1e-6, (
            "average of 1.0, 0.8, 0.4, 0.0 is 0.55, got %r"
            % analytics["average_presence_ratio"])
        assert len(analytics["history"]) == 4, "history should hold 4 entries"
        print("PASS  student rates over 4 sessions: attendance 0.50, late 0.25, "
              "partial 0.25, absence 0.25, avg presence 0.550")
    finally:
        cleanup(database, directory)


def test_expected_but_unevaluated_counts_in_denominator():
    """
    Expected, session completed, but never evaluated.

    It must count against the student rather than vanish - and be visible.
    """
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        builder.session("S0", day=0, expected=["STU001"])
        builder.session("S1", day=1, expected=["STU001"])
        builder.record("S0", "STU001", "PRESENT")

        analytics = AnalyticsService(database).student_analytics("STU001")

        assert analytics["expected_sessions"] == 2, "denominator should be 2"
        assert analytics["evaluated_sessions"] == 1, "only one was evaluated"
        assert analytics["sessions_without_record"] == 1, (
            "the missing evaluation should be reported")
        assert analytics["attendance_rate"] == 0.5, (
            "1 satisfying of 2 expected = 0.5, got %r" % analytics["attendance_rate"])
        print("PASS  an expected-but-unevaluated session stays in the denominator "
              "and is reported")
    finally:
        cleanup(database, directory)


def test_student_not_expected_is_not_counted():
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001").student("STU002")
        builder.session("S1", expected=["STU001"])
        builder.record("S1", "STU001", "PRESENT")

        analytics = AnalyticsService(database).student_analytics("STU002")

        assert analytics["expected_sessions"] == 0, (
            "a student never expected should have no denominator")
        assert analytics["attendance_rate"] is None, (
            "with no expectations the rate is undefined, not 0.0")
        print("PASS  a student who was never expected has a null rate, not 0%")
    finally:
        cleanup(database, directory)


def test_unknown_student_returns_none():
    database, directory = temp_database()
    try:
        assert AnalyticsService(database).student_analytics("GHOST") is None, (
            "an unknown student should return None")
        print("PASS  analytics for an unknown student returns None")
    finally:
        cleanup(database, directory)


# -- class analytics -------------------------------------------------------

def test_class_analytics_aggregates_sessions():
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001").student("STU002")
        for day in range(3):
            builder.session("S%d" % day, class_id="C1", day=day,
                            expected=["STU001", "STU002"])
            builder.record("S%d" % day, "STU001", "PRESENT", ratio=0.9, day=day)
            builder.record("S%d" % day, "STU002", "ABSENT", ratio=0.0,
                           present_seconds=0.0, day=day)

        analytics = AnalyticsService(database).class_analytics("C1")

        assert analytics["completed_sessions"] == 3, "three sessions expected"
        assert analytics["student_count"] == 2, "two students expected"
        assert analytics["attendance_rate"] == 0.5, (
            "3 satisfying of 6 expectations = 0.5, got %r"
            % analytics["attendance_rate"])
        assert analytics["absence_rate"] == 0.5, "half were absent"
        assert len(analytics["session_history"]) == 3, "history should hold 3"
        print("PASS  class analytics aggregate to 0.500 across 3 sessions")
    finally:
        cleanup(database, directory)


def test_trend_thresholds():
    """One session is not a trend, and the API must not pretend otherwise."""
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        builder.session("S0", class_id="C1", day=0, expected=["STU001"])
        builder.record("S0", "STU001", "PRESENT")

        one = AnalyticsService(database).class_analytics("C1")
        assert one["comparison_available"] is False, "1 session is no comparison"
        assert one["has_trend_data"] is False, "1 session is no trend"

        builder.session("S1", class_id="C1", day=1, expected=["STU001"])
        builder.record("S1", "STU001", "PRESENT", day=1)
        two = AnalyticsService(database).class_analytics("C1")
        assert two["comparison_available"] is True, "2 sessions allow comparison"
        assert two["has_trend_data"] is False, "2 sessions are still not a trend"

        builder.session("S2", class_id="C1", day=2, expected=["STU001"])
        builder.record("S2", "STU001", "PRESENT", day=2)
        three = AnalyticsService(database).class_analytics("C1")
        assert three["has_trend_data"] is True, "3 sessions support a trend"

        print("PASS  1 session = no comparison, 2 = comparison, 3 = trend")
    finally:
        cleanup(database, directory)


def test_class_with_no_completed_sessions():
    database, directory = temp_database()
    try:
        analytics = AnalyticsService(database).class_analytics("NOPE")
        assert analytics["completed_sessions"] == 0, "no sessions expected"
        assert analytics["attendance_rate"] is None, (
            "with no sessions the rate must be null, not 0.0")
        print("PASS  a class with no completed sessions reports null, not 0%")
    finally:
        cleanup(database, directory)


# -- policy versioning -----------------------------------------------------

def test_records_stay_with_their_policy_version():
    """Historical results must not be silently reinterpreted."""
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        builder.session("S1", expected=["STU001"], policy_version="1.0")
        builder.record("S1", "STU001", "PRESENT", policy_version="1.0")
        builder.record("S1", "STU001", "PARTIAL", policy_version="2.0")

        analytics = AnalyticsService(database).session_analytics("S1")

        assert analytics["has_multiple_policy_versions"] is True, (
            "a mixed dataset must be visible")
        assert analytics["policy_versions_present"] == ["1.0", "2.0"], (
            "both versions should be listed: %s"
            % analytics["policy_versions_present"])
        # The session declares policy 1.0, so the 1.0 record is authoritative.
        assert analytics["counts"]["PRESENT"] == 1, (
            "the session's own policy version should be used")
        assert analytics["counts"]["PARTIAL"] == 0, (
            "the other policy's record must not also be counted")
        assert analytics["evaluated_students"] == 1, (
            "one student must not be counted twice")
        print("PASS  with two policy versions the session's own policy is used "
              "and the mix is reported")
    finally:
        cleanup(database, directory)


# -- overrides -------------------------------------------------------------

def test_override_uses_effective_status_and_is_counted():
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        builder.session("S1", expected=["STU001"])
        builder.record("S1", "STU001", "ABSENT", ratio=0.0, present_seconds=0.0)

        AttendanceRepository(database).apply_override(
            "S1", "STU001", "PRESENT", reason="verified manually",
            actor="teacher")

        analytics = AnalyticsService(database).session_analytics("S1")

        assert analytics["counts"]["PRESENT"] == 1, (
            "the corrected status should be used")
        assert analytics["counts"]["ABSENT"] == 0, "the original should not count"
        assert analytics["override_count"] == 1, (
            "the override must be reported so it is not mistaken for measurement")
        print("PASS  an overridden record counts by its effective status and the "
              "override is reported")
    finally:
        cleanup(database, directory)


# -- attention indicators --------------------------------------------------

def test_attention_indicators_use_explicit_thresholds():
    database, directory = temp_database()
    try:
        builder = Builder(database).student("GOOD").student("POOR")
        for day in range(4):
            builder.session("S%d" % day, day=day, expected=["GOOD", "POOR"])
            builder.record("S%d" % day, "GOOD", "PRESENT", day=day)
            builder.record("S%d" % day, "POOR", "ABSENT", ratio=0.0,
                           present_seconds=0.0, day=day)

        result = AnalyticsService(database).attention_indicators(minimum_sessions=3)
        flagged = {item["student_id"] for item in result["indicators"]}

        assert "POOR" in flagged, "a 0% attendance student should be flagged"
        assert "GOOD" not in flagged, "a 100% attendance student should not be"
        assert result["rule"]["attendance_rate_below"] == 0.75, (
            "the rule must be reported so it can be checked")
        assert "not anomalies" in result["note"], (
            "the response should state that this is not anomaly detection")

        item = [i for i in result["indicators"] if i["student_id"] == "POOR"][0]
        assert item["reasons"], "the reason should be stated"
        print("PASS  attention list uses stated thresholds and explains itself")
    finally:
        cleanup(database, directory)


def test_attention_respects_minimum_sessions():
    database, directory = temp_database()
    try:
        builder = Builder(database).student("POOR")
        builder.session("S0", day=0, expected=["POOR"])
        builder.record("S0", "POOR", "ABSENT", ratio=0.0, present_seconds=0.0)

        result = AnalyticsService(database).attention_indicators(minimum_sessions=3)
        assert result["indicators"] == [], (
            "one session is too little evidence to flag anyone")
        print("PASS  a student with too few sessions is not flagged")
    finally:
        cleanup(database, directory)


# -- read-only -------------------------------------------------------------

def test_analytics_never_modify_data():
    """Viewing analytics must not change a single row."""
    database, directory = temp_database()
    try:
        builder = Builder(database).student("STU001")
        builder.session("S1", expected=["STU001"])
        builder.record("S1", "STU001", "PRESENT")
        builder.segments("S1", "STU001", count=2)

        def snapshot():
            return {table: database.query_one(
                        "SELECT COUNT(*) AS n FROM %s" % table)["n"]
                    for table in database.table_names()}

        before = snapshot()
        service = AnalyticsService(database)
        service.overview()
        service.session_analytics("S1")
        service.student_analytics("STU001")
        service.class_analytics("C1")
        service.attention_indicators()
        after = snapshot()

        assert before == after, (
            "analytics modified the database:\n  before %s\n  after  %s"
            % (before, after))

        record = database.query_one(
            "SELECT status, reason FROM attendance_records WHERE student_id = 'STU001'")
        assert record["status"] == "PRESENT", "a record was altered by reading"
        print("PASS  running every analytics function changed no row")
    finally:
        cleanup(database, directory)


def main():
    enable_safe_console()

    tests = [
        test_satisfying_statuses_are_explicit,
        test_overview_on_empty_database,
        test_real_zero_is_distinct_from_no_data,
        test_session_analytics_counts,
        test_session_analytics_unknown_session,
        test_active_and_cancelled_sessions_excluded,
        test_student_attendance_rate_over_multiple_sessions,
        test_expected_but_unevaluated_counts_in_denominator,
        test_student_not_expected_is_not_counted,
        test_unknown_student_returns_none,
        test_class_analytics_aggregates_sessions,
        test_trend_thresholds,
        test_class_with_no_completed_sessions,
        test_records_stay_with_their_policy_version,
        test_override_uses_effective_status_and_is_counted,
        test_attention_indicators_use_explicit_thresholds,
        test_attention_respects_minimum_sessions,
        test_analytics_never_modify_data,
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
