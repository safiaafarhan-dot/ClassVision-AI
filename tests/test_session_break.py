"""
The ten-minute break, and what it does to attendance.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHY THIS SUITE IS MOSTLY ARITHMETIC

    A break that is only a countdown in the browser is a lie about attendance.
    The rule engine divides present time by session length, so ten minutes of a
    sixty-minute lesson spent standing up and stretching lowers every student's
    ratio by a sixth, and a student on the PARTIAL boundary is marked PARTIAL
    for complying with the teacher.

    So most of what is checked here is the division, not the timer.

WHAT THIS VERIFIES

    THE INVARIANT FIRST: a session with no break produces byte-identical
    numbers to the ones it produced before breaks existed. The subtraction is
    skipped rather than applied as a zero, and this is asserted against the
    plain end-minus-start value.

    That a break is removed from BOTH sides of the ratio. The denominator loses
    it through duration_seconds, and the numerator loses it because presence
    inside a break is cut out of the segments - so leaving the room during a
    break costs nothing and staying in frame earns nothing. Those two are
    asserted to produce the SAME status for the same student.

    That overlapping breaks are not subtracted twice, which would shrink the
    denominator below the time that was actually taught and inflate every rate
    in the session.

    That a break is clipped to its session. A break cannot remove time the
    lesson did not contain.

    That the clock is the SERVER'S: `ends_at` is stored when the break starts,
    and a browser is never trusted to decide when ten minutes are up.

    That a double-clicked button cannot create two overlapping breaks - the
    second call answers with the break that is already running.

    That breaks are PER SESSION. A break on one class does not pause another,
    which is the whole point of the state living on a row keyed by session.

WHAT THIS DOES NOT VERIFY

    That ten minutes elapse in real time. The suite does not sleep for ten
    minutes; it checks the stored interval, the remaining time the server
    reports, and the arithmetic over intervals it constructs directly.

Run with:
    python tests/test_session_break.py
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
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from attendance_rules import (  # noqa: E402
    AttendancePolicy,
    AttendanceRuleEngine,
    PresenceSegment,
    subtract_intervals,
    summarize_presence,
)
from attendance_session import AttendanceSession, normalise_intervals  # noqa: E402
from backend.services import state  # noqa: E402
from backend.services.break_service import (  # noqa: E402
    BREAK_SECONDS,
    BreakError,
    BreakService,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=60)
BREAK = (START + timedelta(minutes=25), START + timedelta(minutes=35))

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}


# -- harness ----------------------------------------------------------------

def make_database():
    directory = Path(tempfile.mkdtemp(prefix="classvision_break_"))
    state.configure(directory / "break.db")
    return state.get_database(), directory


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_breakapi_"))
    state.configure(directory / "break.db")
    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        if client is not None:
            client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def engine():
    return AttendanceRuleEngine(AttendancePolicy())


def a_session(excluded=None, end=END, status="COMPLETED", students=("A",)):
    return AttendanceSession("S-1", "CSE-A", START, end, policy_version="1.0",
                             status=status, expected_students=list(students),
                             excluded_intervals=excluded)


def present_for(minutes, student="A"):
    return [PresenceSegment(student, START, START + timedelta(minutes=minutes))]


def store_session(database, session_id="S-1", class_id="CSE-A",
                  status="ACTIVE", start=None, end=None):
    from repositories import SessionRepository

    session = AttendanceSession(session_id, class_id,
                                start or (datetime.now(timezone.utc)
                                          - timedelta(minutes=10)),
                                end or (datetime.now(timezone.utc)
                                        + timedelta(minutes=50)),
                                policy_version="1.0")
    if status == "ACTIVE":
        session.start()
    SessionRepository(database).save(session)
    return session


# -- the invariant ----------------------------------------------------------

def test_a_session_with_no_break_is_unchanged():
    """
    The guarantee every existing number depends on.

    Not "close to" the old value - the same value, because the subtraction is
    skipped entirely rather than applied as a zero.
    """
    plain = a_session()

    assert plain.excluded_intervals == (), (
        "a session with no break has excluded intervals")
    assert plain.duration_seconds == (END - START).total_seconds()
    assert plain.duration_seconds == plain.scheduled_seconds
    assert plain.excluded_seconds == 0.0

    decision = engine().evaluate_student("A", present_for(50), plain)
    assert decision.session_seconds == 3600.0
    assert decision.present_seconds == 3000.0

    # And the presence summary is untouched when no intervals are given.
    segments = present_for(50)
    assert subtract_intervals(segments, ()) is segments, (
        "subtract_intervals copied the list when it had nothing to do")

    print("PASS  a session with no break produces exactly the numbers it "
          "always did")


def test_the_break_is_removed_from_both_sides_of_the_ratio():
    without = a_session()
    with_break = a_session(excluded=[BREAK])

    assert without.duration_seconds == 3600.0
    assert with_break.duration_seconds == 3000.0, (
        "the break was not taken out of the denominator")
    assert with_break.scheduled_seconds == 3600.0, (
        "the wall-clock length changed, which it must not")

    plain = engine().evaluate_student("A", present_for(50), without)
    broken = engine().evaluate_student("A", present_for(50), with_break)

    assert plain.present_seconds == 3000.0
    assert broken.present_seconds == 2400.0, (
        "presence inside the break was counted; the numerator was not clipped")

    print("PASS  a break is subtracted from the denominator AND the numerator "
          "(3000/3600 becomes 2400/3000)")


def test_leaving_the_room_during_a_break_costs_nothing():
    """
    The reason the numerator is clipped as well as the denominator.

    If presence during a break counted, a student who stayed in their seat
    would out-score one who went to get water, and attendance would be
    measuring willingness to sit still.
    """
    session = a_session(excluded=[BREAK], students=("B",))

    left = [PresenceSegment("B", START, BREAK[0]),
            PresenceSegment("B", BREAK[1], END)]
    stayed = [PresenceSegment("B", START, END)]

    away = engine().evaluate_student("B", left, session)
    present = engine().evaluate_student("B", stayed, session)

    assert away.present_seconds == present.present_seconds == 3000.0
    assert away.presence_ratio == present.presence_ratio == 1.0
    assert away.status == present.status, (
        "leaving the room during a break changed the outcome")

    print("PASS  leaving the room during a break scores the same as staying "
          "in frame (%s both)" % away.status)


def test_a_break_cannot_make_a_present_student_absent():
    """The failure this whole feature exists to prevent."""
    session_without = a_session()
    session_with = a_session(excluded=[BREAK])

    # Present for all the TAUGHT time: the first 25 minutes and the last 25.
    segments = [PresenceSegment("A", START, BREAK[0]),
                PresenceSegment("A", BREAK[1], END)]

    ignored_break = engine().evaluate_student("A", segments, session_without)
    with_break = engine().evaluate_student("A", segments, session_with)

    assert with_break.presence_ratio == 1.0, with_break.presence_ratio
    assert with_break.status == "PRESENT", (
        "a student present for every minute of teaching was not PRESENT")
    assert ignored_break.presence_ratio < with_break.presence_ratio, (
        "the fixture no longer demonstrates the problem - ignoring the break "
        "should give a worse ratio")

    print("PASS  a student present for all taught time is PRESENT (%.3f), "
          "where ignoring the break would give %.3f"
          % (with_break.presence_ratio, ignored_break.presence_ratio))


def test_overlapping_breaks_are_not_subtracted_twice():
    overlapping = a_session(excluded=[
        BREAK, (START + timedelta(minutes=30), START + timedelta(minutes=40))])

    assert overlapping.excluded_seconds == 900.0, (
        "overlapping breaks removed %.0fs instead of 900s"
        % overlapping.excluded_seconds)
    assert overlapping.duration_seconds == 2700.0
    assert len(overlapping.excluded_intervals) == 1, (
        "the overlapping intervals were not merged")

    print("PASS  two overlapping breaks exclude 900s, not 1200s")


def test_a_break_is_clipped_to_its_session():
    """A break may only remove time the lesson contained."""
    overrunning = a_session(excluded=[
        (START + timedelta(minutes=55), START + timedelta(minutes=75))])

    assert overrunning.excluded_seconds == 300.0, (
        "a break running past the end of the lesson removed %.0fs"
        % overrunning.excluded_seconds)

    outside = a_session(excluded=[(END + timedelta(minutes=5),
                                   END + timedelta(minutes=15))])
    assert outside.excluded_intervals == (), (
        "a break entirely outside the session was kept")
    assert outside.duration_seconds == 3600.0

    # And normalise_intervals is what does it, so it can be checked directly.
    assert normalise_intervals([(START - timedelta(hours=1), END)],
                               START, END) == ((START, END),)

    print("PASS  a break is clipped to the session it is inside")


def test_presence_is_summarised_with_the_break_removed():
    segments = [PresenceSegment("A", START, END)]

    whole = summarize_presence("A", segments, START, END)
    clipped = summarize_presence("A", segments, START, END,
                                 excluded_intervals=[BREAK])

    assert whole.present_seconds == 3600.0
    assert clipped.present_seconds == 3000.0
    assert clipped.segment_count == 2, (
        "a segment straddling the break was not split in two")

    print("PASS  a segment straddling a break is split and the break removed")


# -- the service ------------------------------------------------------------

def test_a_break_is_ten_minutes_by_the_servers_clock():
    database, directory = make_database()
    try:
        store_session(database)
        started = BreakService(database).start("S-1", actor="office")

        assert started["length_seconds"] == BREAK_SECONDS == 600
        assert 590 <= started["remaining_seconds"] <= 600, (
            "the remaining time is not about ten minutes: %s"
            % started["remaining_seconds"])
        assert started["ends_at"], "the end is not stored"
        assert started["active"] is True

        # Stored, not computed per request: the row carries the end.
        row = database.query_one(
            "SELECT ends_at, started_at FROM session_breaks WHERE break_id = ?",
            (started["break_id"],))
        assert row["ends_at"] and row["started_at"]

        print("PASS  a break is ten minutes, fixed by the server when it "
              "starts")
    finally:
        teardown(None, directory)


def test_starting_a_break_twice_does_not_create_two():
    database, directory = make_database()
    try:
        store_session(database)
        service = BreakService(database)

        first = service.start("S-1")
        second = service.start("S-1")

        assert first["break_id"] == second["break_id"], (
            "a double-clicked button created a second break")
        assert len(service.breaks_for("S-1")) == 1

        print("PASS  a second start answers with the break already running")
    finally:
        teardown(None, directory)


def test_a_break_can_be_ended_early_and_only_the_real_time_counts():
    database, directory = make_database()
    try:
        store_session(database)
        service = BreakService(database)

        service.start("S-1")
        ended = service.end("S-1", actor="office")

        assert ended["active"] is False
        assert ended["ended_early"] is True
        assert service.current("S-1") is None

        # The excluded interval is what HAPPENED, not the planned ten minutes.
        #
        # Ended in the same millisecond it started, so the honest answer is
        # that it excludes nothing at all and there is no interval to report -
        # a zero-length break removes zero time. The property being asserted is
        # the rule, not the row count.
        intervals = service.excluded_intervals("S-1")
        assert len(intervals) <= 1, intervals
        actual = sum((end - start).total_seconds() for start, end in intervals)
        assert actual < BREAK_SECONDS, (
            "a break ended early still excluded the full ten minutes")

        try:
            service.end("S-1")
            raise AssertionError("ending a finished break was allowed")
        except BreakError:
            pass

        print("PASS  a break ended early excludes only the time it actually "
              "took (%.1fs)" % actual)
    finally:
        teardown(None, directory)


def test_a_break_is_refused_when_there_is_nothing_to_interrupt():
    database, directory = make_database()
    try:
        store_session(database, status="SCHEDULED")
        service = BreakService(database)

        try:
            service.start("S-1")
            raise AssertionError("a break was allowed on a SCHEDULED session")
        except BreakError as error:
            assert "running session" in str(error), error

        try:
            service.start("NO-SUCH-SESSION")
            raise AssertionError("a break was allowed on a missing session")
        except BreakError as error:
            assert "No session" in str(error), error

        print("PASS  a break is refused unless a session is running")
    finally:
        teardown(None, directory)


def test_a_break_belongs_to_one_session():
    """Starting a break for one class must not pause another."""
    database, directory = make_database()
    try:
        store_session(database, session_id="S-A", class_id="CSE-A")
        store_session(database, session_id="S-B", class_id="CSE-B")

        service = BreakService(database)
        service.start("S-A")

        assert service.state("S-A")["active"] is True
        assert service.state("S-B")["active"] is False, (
            "a break on one session paused another")
        assert service.current("S-B") is None
        assert service.excluded_intervals("S-B") == []

        print("PASS  a break on CSE-A leaves CSE-B running")
    finally:
        teardown(None, directory)


# -- the API ----------------------------------------------------------------

def test_the_status_endpoint_reports_the_break_as_scalars():
    """
    Flat fields, not a nested object.

    test_live_api asserts that every value in a live status response is a plain
    scalar, so no field of it can carry a captured image. The break state has to
    respect that too.
    """
    client, directory = make_client()
    try:
        status = client.get("/api/live/status")
        assert status.status_code == 200, status.text
        body = status.json()

        for field in ("on_break", "break_remaining_seconds", "break_started_at",
                      "break_ends_at", "break_count",
                      "break_excluded_seconds"):
            assert field in body, "the status has no %s" % field
            assert isinstance(body[field],
                              (str, int, float, bool, type(None))), (
                "%s is a %s, which could carry anything"
                % (field, type(body[field]).__name__))

        assert body["on_break"] is False, "an idle runtime claims a break"

        print("PASS  the break state is reported as scalars and defaults to "
              "no break")
    finally:
        teardown(client, directory)


def test_the_break_endpoints_refuse_when_no_session_is_running():
    client, directory = make_client()
    try:
        assert client.post("/api/auth/setup",
                           json=ADMIN).status_code == 201

        started = client.post("/api/live/break")
        assert started.status_code == 409, started.text
        assert "nothing to interrupt" in started.json()["detail"]

        ended = client.post("/api/live/break/end")
        assert ended.status_code == 409, ended.text

        print("PASS  the break endpoints answer 409 when nothing is running")
    finally:
        teardown(client, directory)


def test_the_break_endpoints_require_a_signed_in_account():
    client, directory = make_client()
    try:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

        from backend.app import app
        anonymous = TestClient(app)
        try:
            assert anonymous.post("/api/live/break").status_code == 401
            assert anonymous.post("/api/live/break/end").status_code == 401
        finally:
            anonymous.close()

        print("PASS  the break endpoints are 401 for an unauthenticated "
              "caller")
    finally:
        teardown(client, directory)


def test_the_break_table_is_the_only_thing_the_schema_gained():
    from schema_baseline import EXPECTED_TABLES

    database, directory = make_database()
    try:
        assert sorted(database.table_names()) == EXPECTED_TABLES, (
            "the schema no longer matches the hand-written baseline")
        assert "session_breaks" in EXPECTED_TABLES

        for row in database.connection.execute(
                "PRAGMA table_info(session_breaks)").fetchall():
            assert "BLOB" not in str(row[2]).upper(), (
                "session_breaks declares a BLOB column: %s" % row[1])

        print("PASS  the break feature added one table and no BLOB column")
    finally:
        teardown(None, directory)


def test_break_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    database, directory = make_database()
    try:
        assert Path(database.path).parent != real.parent
        store_session(database)
        BreakService(database).start("S-1")
    finally:
        teardown(None, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_a_session_with_no_break_is_unchanged,
    test_the_break_is_removed_from_both_sides_of_the_ratio,
    test_leaving_the_room_during_a_break_costs_nothing,
    test_a_break_cannot_make_a_present_student_absent,
    test_overlapping_breaks_are_not_subtracted_twice,
    test_a_break_is_clipped_to_its_session,
    test_presence_is_summarised_with_the_break_removed,
    test_a_break_is_ten_minutes_by_the_servers_clock,
    test_starting_a_break_twice_does_not_create_two,
    test_a_break_can_be_ended_early_and_only_the_real_time_counts,
    test_a_break_is_refused_when_there_is_nothing_to_interrupt,
    test_a_break_belongs_to_one_session,
    test_the_status_endpoint_reports_the_break_as_scalars,
    test_the_break_endpoints_refuse_when_no_session_is_running,
    test_the_break_endpoints_require_a_signed_in_account,
    test_the_break_table_is_the_only_thing_the_schema_gained,
    test_break_tests_never_touch_the_real_database,
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
