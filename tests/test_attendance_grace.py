"""
The duration-based attendance grace period.

    A longer class forgives more absence than a short one. The band table
    lives in ai/attendance_rules.py and nowhere else.

WHAT THIS VERIFIES

    The band table, on every boundary the requirement names, including the
    fractional ones either side of each edge.

    That the grace period is a QUALIFICATION and never an adjustment: the
    presence timeline, present_seconds, the ratio and the missing time are
    identical with the rule on and off, and only the status and the reason
    can differ.

    That it only ever grants, never withholds - a student the ordinary rules
    already called PRESENT is returned untouched, and ABSENT and UNKNOWN are
    not shortfalls a grace period can forgive.

    That the decision is the backend's: a session finalised through the
    service layer carries the relaxation, and the API exposes the three
    figures a teacher needs in order to question it.

WHAT THIS DOES NOT VERIFY

    Whether these particular bands are the right policy for any real school.
    They are the project's development default, like every other threshold in
    the rules engine, and they are a policy setting precisely because nobody
    here is in a position to choose them on an institution's behalf.

Run with:
    python tests/test_attendance_grace.py
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

from attendance_rules import (  # noqa: E402
    DEFAULT_GRACE_BANDS,
    STATUS_ABSENT,
    STATUS_LEFT_EARLY,
    STATUS_PARTIAL,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    AttendancePolicy,
    AttendancePolicyError,
    AttendanceRuleEngine,
    PresenceSegment,
    grace_minutes_for_duration,
    grace_seconds_for_duration,
)
from attendance_session import (  # noqa: E402
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    AttendanceSession,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


# -- helpers ---------------------------------------------------------------

def a_session(total_minutes, status=SESSION_COMPLETED, expected=("STU001",)):
    end = START + timedelta(minutes=total_minutes)
    session = AttendanceSession("SES-G", "CLASS-10A", START, end,
                                expected_students=list(expected))
    if status in (SESSION_ACTIVE, SESSION_COMPLETED):
        session.start()
    if status == SESSION_COMPLETED:
        session.complete(completed_at=end)
    return session


def present_for(minutes, from_minute=0, student_id="STU001"):
    """One continuous segment, by default starting at the bell."""
    begin = START + timedelta(minutes=from_minute)
    return [PresenceSegment(student_id, begin,
                            begin + timedelta(minutes=minutes), 1)]


def a_policy(**overrides):
    defaults = dict(minimum_presence_ratio=0.75, minimum_presence_seconds=0,
                    late_after_seconds=600, early_leave_before_seconds=300)
    defaults.update(overrides)
    return AttendancePolicy(**defaults)


def decide(total_minutes, present_minutes, from_minute=0, policy=None,
           status=SESSION_COMPLETED):
    engine = AttendanceRuleEngine(policy or a_policy())
    return engine.evaluate_student(
        "STU001", present_for(present_minutes, from_minute),
        a_session(total_minutes, status=status))


def granted_by_grace(decision):
    """Whether the relaxation is what produced this status, per the reason."""
    return "grace period" in decision.reason


# -- the band table --------------------------------------------------------

def test_every_boundary_in_the_requirement():
    """
    The table, on every edge and on both sides of every edge.

    The fractional cases are the ones that matter: an inclusive upper bound
    is one character away from an exclusive one, and only 30.1, 60.1 and 90.1
    can tell the two apart.
    """
    expected = [
        (30, 0), (30.1, 5), (31, 5), (45, 5), (60, 5),
        (60.1, 10), (61, 10), (75, 10), (90, 10),
        (90.1, 15), (91, 15), (120, 15), (180, 15),
    ]

    for minutes, grace in expected:
        actual = grace_minutes_for_duration(minutes)
        assert actual == grace, (
            "a %r-minute class should allow %d minutes of grace, got %d"
            % (minutes, grace, actual))

    print("PASS  all %d boundary cases map to the required grace period"
          % len(expected))


def test_the_bands_are_inclusive_at_the_top():
    """30 belongs to the 0-minute band, not the 5-minute one."""
    for edge, below, above in ((30, 0, 5), (60, 5, 10), (90, 10, 15)):
        assert grace_minutes_for_duration(edge) == below, (
            "%d minutes exactly should take the lower band" % edge)
        assert grace_minutes_for_duration(edge + 0.0001) == above, (
            "anything over %d minutes should take the higher band" % edge)

    print("PASS  each band includes its own upper bound and excludes the next")


def test_seconds_and_minutes_agree():
    """The two accessors are one rule, not two."""
    for minutes in (0, 1, 29.9, 30, 45, 60, 75, 90, 91, 150, 600):
        from_minutes = grace_minutes_for_duration(minutes)
        from_seconds = grace_seconds_for_duration(minutes * 60.0)
        assert from_seconds == from_minutes * 60.0, (
            "%r minutes: %r minutes of grace but %r seconds"
            % (minutes, from_minutes, from_seconds))

    print("PASS  the seconds accessor is the minutes accessor times sixty")


def test_a_class_with_no_length_allows_nothing():
    """
    Zero, negative and nonsense durations allow no grace.

    Not an edge case for its own sake: a session can be finalised over an
    observed window of zero if the camera never ran, and a grace period
    granted there would be attendance granted for a class that did not
    happen.
    """
    for duration in (0, -1, -600, None, "", "soon"):
        assert grace_minutes_for_duration(duration) == 0, (
            "a duration of %r must allow no grace" % duration)
        assert grace_seconds_for_duration(duration) == 0.0

    print("PASS  a zero, negative or unreadable duration allows no grace")


def test_the_bands_are_a_policy_setting():
    """
    The table is the default, not the law.

    An institution that wants one flat allowance, or none at all, says so in
    the policy rather than editing the engine.
    """
    flat = AttendancePolicy(duration_grace_bands=((None, 7),))
    assert flat.grace_minutes_for(60 * 60) == 7
    assert flat.grace_minutes_for(600 * 60) == 7

    off = AttendancePolicy(duration_grace_bands=())
    assert off.grace_minutes_for(120 * 60) == 0, (
        "an empty band table must switch the relaxation off entirely")
    assert off.grace_seconds_for(120 * 60) == 0.0

    standard = AttendancePolicy()
    assert standard.duration_grace_bands == DEFAULT_GRACE_BANDS, (
        "the default policy should carry the documented default bands")
    assert standard.grace_seconds_for(75 * 60) == 600.0

    print("PASS  the bands are a policy setting: overridable, and switchable "
          "off")


def test_an_incoherent_band_table_is_refused():
    broken = [
        (((60.0, 5), (30.0, 0), (None, 15)), "descending bounds"),
        (((30.0, 0), (60.0, 5)), "a bounded last band"),
        (((None, 5), (60.0, 10)), "an open band that is not last"),
        (((30.0, -1), (None, 5)), "a negative grace"),
        (((0.0, 0), (None, 5)), "a non-positive bound"),
        ((("thirty", 0), (None, 5)), "an unreadable bound"),
    ]

    for bands, why in broken:
        try:
            AttendancePolicy(duration_grace_bands=bands)
        except AttendancePolicyError:
            continue
        except ValueError:
            continue
        raise AssertionError("a band table with %s was accepted: %r"
                             % (why, bands))

    print("PASS  a band table that cannot mean anything is refused, loudly")


# -- the attendance decisions the requirement names ------------------------

def test_the_decision_table_in_the_requirement():
    """
    The six cases, as stated.

    The default policy's 75% ratio already grants PRESENT at every one of the
    "within grace" rows, which is the honest result: the relaxation is a
    safety net under the ordinary rules and not a replacement for them. What
    is asserted here is the OUTCOME the requirement names, plus the three
    figures behind it.
    """
    cases = [
        (60, 55, 5, 5, STATUS_PRESENT),
        (90, 80, 10, 10, STATUS_PRESENT),
        (120, 105, 15, 15, STATUS_PRESENT),
    ]

    for total, present, missing, grace, status in cases:
        decision = decide(total, present)
        assert decision.status == status, (
            "%d-minute class, %d present: expected %s, got %s (%s)"
            % (total, present, status, decision.status, decision.reason))
        assert round(decision.missing_seconds / 60.0, 6) == missing, (
            "missing time should be %d minutes, got %.3f"
            % (missing, decision.missing_seconds / 60.0))
        assert decision.grace_seconds / 60.0 == grace, (
            "a %d-minute class should allow %d minutes, got %.1f"
            % (total, grace, decision.grace_seconds / 60.0))
        assert decision.within_grace, (
            "%d minutes missing should be inside a %d-minute allowance"
            % (missing, grace))

    print("PASS  55/60, 80/90 and 105/120 are PRESENT, each exactly on its "
          "allowance")


def test_one_minute_past_the_allowance_falls_back_to_the_existing_rules():
    """
    Over the line, the ordinary rules decide - whatever they decide.

    This test deliberately does not assert a particular status. The
    requirement says the existing rule applies, and pinning the answer here
    would quietly make the grace suite the owner of the early-leave
    threshold's behaviour as well.
    """
    for total, present, missing in ((60, 54, 6), (90, 79, 11), (120, 104, 16)):
        decision = decide(total, present)

        assert round(decision.missing_seconds / 60.0, 6) == missing
        assert not decision.within_grace, (
            "%d minutes missing is outside the allowance for a %d-minute "
            "class" % (missing, total))
        assert not granted_by_grace(decision), (
            "the relaxation should not have decided this: %s"
            % decision.reason)

    print("PASS  6/60, 11/90 and 16/120 are outside the allowance and the "
          "existing rules decide")


def test_the_relaxation_grants_a_status_the_ordinary_rules_withheld():
    """
    The case the rule exists for.

    A policy demanding 95% presence refuses a student who missed exactly the
    allowance. The relaxation grants it - and the record says which rule
    did, because the reason text changes.
    """
    strict = a_policy(minimum_presence_ratio=0.95)

    without = AttendanceRuleEngine(
        a_policy(minimum_presence_ratio=0.95, duration_grace_bands=()))
    refused = without.evaluate_student(
        "STU001", present_for(55), a_session(60))
    assert refused.status == STATUS_PARTIAL, (
        "with no relaxation, 55 of 60 minutes under a 95%% rule should be "
        "PARTIAL, got %s" % refused.status)

    granted = decide(60, 55, policy=strict)
    assert granted.status == STATUS_PRESENT, (
        "the relaxation should have granted PRESENT, got %s (%s)"
        % (granted.status, granted.reason))
    assert granted_by_grace(granted), (
        "the reason should say the grace period decided it: %s"
        % granted.reason)
    assert "300s" in granted.reason and "60-minute" in granted.reason, (
        "the reason should quote the allowance and the class length: %s"
        % granted.reason)

    # One minute further out, the same policy still refuses.
    still_refused = decide(60, 54, policy=strict)
    assert still_refused.status == STATUS_PARTIAL, (
        "6 minutes missing is outside a 5-minute allowance, got %s"
        % still_refused.status)

    print("PASS  the relaxation turns a withheld PRESENT into a granted one, "
          "and says so in the reason")


def test_the_relaxation_never_touches_a_present_it_did_not_decide():
    """
    A student who met the ordinary rules is returned exactly as they came.

    This is why the relaxation runs second. Asked first it would claim the
    credit for every PRESENT in the register, and a student who missed two
    minutes of an hour would read as having needed grace they never used.
    """
    decision = decide(60, 58)

    assert decision.status == STATUS_PRESENT
    assert not granted_by_grace(decision), (
        "the ordinary rules decided this; the reason should not mention the "
        "grace period: %s" % decision.reason)
    assert "met the minimum" in decision.reason, (
        "the original explanation should survive: %s" % decision.reason)

    print("PASS  a PRESENT the ordinary rules granted keeps its own reason")


def test_a_full_attendance_needs_no_relaxation():
    """Nothing missed is not something forgiven."""
    decision = decide(30, 30)

    assert decision.missing_seconds == 0.0
    assert not decision.within_grace, (
        "a student who missed nothing has used no allowance")
    assert decision.status == STATUS_PRESENT
    assert not granted_by_grace(decision)

    print("PASS  a student who missed nothing is not recorded as having used "
          "grace")


def test_a_short_class_forgives_nothing():
    """
    30 minutes or less allows no grace, so the ordinary rules decide alone.

    Checked against a policy that would otherwise refuse, so the absence of
    an allowance is what is being observed rather than the ratio passing.
    """
    strict = a_policy(minimum_presence_ratio=0.99)
    decision = decide(30, 29, policy=strict)

    assert decision.grace_seconds == 0.0
    assert not decision.within_grace
    assert decision.status != STATUS_PRESENT, (
        "a 30-minute class has no allowance to grant PRESENT with, got %s"
        % decision.status)

    print("PASS  a class of 30 minutes or less grants no relaxation at all")


def test_absence_and_uncertainty_are_not_shortfalls():
    """
    A grace period cannot manufacture attendance.

    ABSENT means no verified presence at all and UNKNOWN means the session
    cannot answer yet. Neither is a student who very nearly attended, and
    neither may be relaxed into PRESENT however long the class was.
    """
    engine = AttendanceRuleEngine(a_policy())

    absent = engine.evaluate_student("STU001", [], a_session(180))
    assert absent.status == STATUS_ABSENT, (
        "no presence in a completed session is ABSENT, got %s" % absent.status)
    assert absent.grace_seconds == 900.0, (
        "the record should still state what a 180-minute class allows")
    assert not absent.within_grace

    unknown = engine.evaluate_student(
        "STU001", [], a_session(180, status=SESSION_ACTIVE))
    assert unknown.status == STATUS_UNKNOWN, (
        "no presence in a running session is UNKNOWN, got %s" % unknown.status)

    print("PASS  ABSENT and UNKNOWN are never relaxed into PRESENT")


def test_lateness_is_forgiven_but_not_hidden():
    """
    The flags survive the relaxation.

    A twelve-minute late arrival to a two-hour class is inside the
    fifteen-minute allowance, so the status is PRESENT. was_late stays true,
    because it is a measurement and the register has to go on saying what
    happened.
    """
    decision = decide(120, 108, from_minute=12)

    assert decision.status == STATUS_PRESENT, (
        "12 minutes missing is inside a 15-minute allowance, got %s (%s)"
        % (decision.status, decision.reason))
    assert granted_by_grace(decision)
    assert decision.was_late, (
        "the student arrived after the late threshold and the flag must say "
        "so even though the status was granted")

    # Four minutes further out and the ordinary rules have it back.
    beyond = decide(120, 104, from_minute=16)
    assert beyond.status != STATUS_PRESENT, (
        "16 minutes missing is outside the allowance, got %s" % beyond.status)
    assert beyond.was_late

    print("PASS  a late arrival inside the allowance is PRESENT, and still "
          "recorded as late")


# -- the timeline is not touched -------------------------------------------

def test_the_relaxation_changes_nothing_but_the_status_and_the_reason():
    """
    The test the whole feature rests on.

    The same evidence is evaluated twice - once with the bands on and once
    with them switched off - and every field except the status and the reason
    is compared. If the grace period ever adjusts present_seconds, the ratio,
    the segment count or the missing time, this fails.
    """
    strict = dict(minimum_presence_ratio=0.95, minimum_presence_seconds=0,
                  late_after_seconds=600, early_leave_before_seconds=300)

    with_grace = AttendanceRuleEngine(AttendancePolicy(**strict))
    without_grace = AttendanceRuleEngine(
        AttendancePolicy(duration_grace_bands=(), **strict))

    # 55 of 60 minutes, as two segments with a five-minute hole in the middle.
    segments = (present_for(25, from_minute=0)
                + present_for(30, from_minute=30))
    session = a_session(60)

    granted = with_grace.evaluate_student("STU001", segments, session)
    refused = without_grace.evaluate_student("STU001", segments, session)

    assert granted.status == STATUS_PRESENT
    assert refused.status == STATUS_PARTIAL, (
        "without the relaxation this should be PARTIAL, got %s"
        % refused.status)

    compared = ("present_seconds", "session_seconds", "presence_ratio",
                "missing_seconds", "first_seen", "last_seen",
                "segment_count", "longest_segment_seconds", "loss_periods",
                "loss_seconds", "was_late", "left_early")

    for field in compared:
        assert getattr(granted, field) == getattr(refused, field), (
            "the relaxation changed %s: %r with it, %r without"
            % (field, getattr(granted, field), getattr(refused, field)))

    print("PASS  with the relaxation on and off, only the status and the "
          "reason differ across %d fields" % len(compared))


def test_the_missing_time_stays_visible_and_exact():
    """
    The requirement's own example, asserted field by field.

    60-minute class, 55 minutes present, 5 minutes missing, 5 minutes
    allowed, final attendance PRESENT - and the five missing minutes are
    still five missing minutes on the record.
    """
    decision = decide(60, 55, policy=a_policy(minimum_presence_ratio=0.95))

    assert decision.present_seconds == 55 * 60.0
    assert decision.missing_seconds == 5 * 60.0, (
        "the missing time must be the measurement, got %.1fs"
        % decision.missing_seconds)
    assert decision.grace_seconds == 5 * 60.0
    assert decision.status == STATUS_PRESENT

    # And the arithmetic is the plain subtraction, not duration minus grace.
    assert (decision.session_seconds - decision.present_seconds
            == decision.missing_seconds), (
        "missing time should be session length minus present time")

    exported = decision.to_dict()
    assert exported["present_seconds"] == 3300.0
    assert exported["missing_seconds"] == 300.0
    assert exported["grace_seconds"] == 300.0
    assert exported["within_grace"] is True
    assert exported["status"] == STATUS_PRESENT

    print("PASS  55 present, 5 missing, 5 allowed, PRESENT - and the 5 "
          "missing minutes are still on the record")


def test_the_segments_behind_a_granted_present_still_show_the_gap():
    """
    The evidence keeps the hole in it.

    A student relaxed into PRESENT has a presence timeline with a gap, and
    the summary says two segments and five minutes of loss. Grace is a
    conclusion about that evidence, not an edit to it.
    """
    engine = AttendanceRuleEngine(a_policy(minimum_presence_ratio=0.95))
    segments = (present_for(25, from_minute=0)
                + present_for(30, from_minute=30))

    decision = engine.evaluate_student("STU001", segments, a_session(60))

    assert decision.status == STATUS_PRESENT
    assert decision.segment_count == 2, (
        "the two segments must survive, got %d" % decision.segment_count)
    assert decision.loss_periods == 1, (
        "the gap between them must survive, got %d" % decision.loss_periods)
    assert decision.loss_seconds == 5 * 60.0, (
        "the gap is five minutes, got %.1fs" % decision.loss_seconds)

    print("PASS  a granted PRESENT still carries two segments and a "
          "five-minute gap")


# -- the backend is the source of truth ------------------------------------

def test_finalizing_a_session_applies_the_relaxation():
    """
    Through the service layer, with a database, and with no browser involved.

    The frontend is not consulted and could not be: the status and the
    allowance are both decided here and then written to attendance_records.
    """
    from backend.services import state

    directory = Path(tempfile.mkdtemp(prefix="classvision_grace_"))
    try:
        state.configure(directory / "grace.db")
        database = state.get_database()

        from repositories import (AttendanceRepository, SessionRepository,
                                  StudentRepository)

        students = StudentRepository(database)
        sessions = SessionRepository(database)
        attendance = AttendanceRepository(database)

        with database.transaction():
            students.upsert("STU001", "A Student")

        # A two-hour class, finalised with 110 minutes of presence: ten
        # minutes missing against a fifteen-minute allowance.
        session = a_session(120)
        with database.transaction():
            sessions.save(session)

        engine = AttendanceRuleEngine(
            AttendancePolicy(version=session.policy_version,
                             minimum_presence_ratio=0.95))
        decisions = engine.evaluate_session(
            session, present_for(110), ["STU001"])

        with database.transaction():
            for decision in decisions:
                attendance.save_decision(decision)

        stored = attendance.list_for_session(session.session_id)
        assert len(stored) == 1, "one record expected, got %d" % len(stored)
        row = stored[0]

        assert row["status"] == STATUS_PRESENT, (
            "the backend should have granted PRESENT, got %s" % row["status"])
        assert row["present_seconds"] == 110 * 60.0, (
            "the stored present time must be the measurement, got %r"
            % row["present_seconds"])
        assert row["session_seconds"] == 120 * 60.0
        assert "grace period" in row["reason"], (
            "the stored reason should record that the relaxation decided it: "
            "%s" % row["reason"])

        # The two figures the API derives, re-derived from the stored row.
        missing = row["session_seconds"] - row["present_seconds"]
        assert missing == 10 * 60.0
        assert grace_seconds_for_duration(row["session_seconds"]) == 15 * 60.0
        assert missing <= grace_seconds_for_duration(row["session_seconds"])

        print("PASS  a session finalised in the backend stores the granted "
              "status and the evidence behind it")
    finally:
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_no_new_column_was_needed():
    """
    The relaxation added no schema.

    Both figures follow from session_seconds and present_seconds, which the
    table has held since Step 6, so there is no migration to run and no
    existing database to upgrade. This asserts that, so that a later change
    adding a column has to be a deliberate decision rather than a drift.
    """
    from backend.services import state

    directory = Path(tempfile.mkdtemp(prefix="classvision_grace_schema_"))
    try:
        state.configure(directory / "schema.db")
        database = state.get_database()

        columns = set(database.column_names("attendance_records"))
        for absent in ("grace_seconds", "grace_minutes", "missing_seconds",
                       "within_grace"):
            assert absent not in columns, (
                "attendance_records gained a %s column; the relaxation is "
                "meant to be derived from session_seconds and "
                "present_seconds" % absent)

        for needed in ("session_seconds", "present_seconds"):
            assert needed in columns, (
                "%s is what the relaxation is derived from and it is missing"
                % needed)

        print("PASS  the relaxation is derived from two existing columns and "
              "needed no migration")
    finally:
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_api_exposes_the_three_figures():
    """
    The attendance response carries the measurement, the allowance and the
    comparison - and the old fields are all still there.
    """
    from fastapi.testclient import TestClient

    from backend.services import state

    directory = Path(tempfile.mkdtemp(prefix="classvision_grace_api_"))
    client = None
    try:
        state.configure(directory / "api.db")
        database = state.get_database()

        from repositories import (AttendanceRepository, SessionRepository,
                                  StudentRepository)

        students = StudentRepository(database)
        sessions = SessionRepository(database)
        attendance = AttendanceRepository(database)

        with database.transaction():
            students.upsert("STU001", "A Student")

        session = a_session(90)
        with database.transaction():
            sessions.save(session)

        engine = AttendanceRuleEngine(
            AttendancePolicy(version=session.policy_version,
                             minimum_presence_ratio=0.95))
        with database.transaction():
            for decision in engine.evaluate_session(
                    session, present_for(80), ["STU001"]):
                attendance.save_decision(decision)

        from backend.services.data_service import DataService

        page = DataService(database).session_attendance(session.session_id)
        record = page["items"][0]

        assert record["status"] == STATUS_PRESENT
        assert record["present_seconds"] == 80 * 60.0
        assert record["missing_seconds"] == 10 * 60.0, (
            "missing time should be 600s, got %r" % record["missing_seconds"])
        assert record["grace_seconds"] == 10 * 60.0
        assert record["grace_minutes"] == 10
        assert record["within_grace"] is True

        # Nothing that was there before has gone.
        for field in ("presence_ratio", "segment_count", "loss_periods",
                      "was_late", "left_early", "reason", "policy_version"):
            assert field in record, "the response lost %s" % field

        # And it validates against the published schema.
        from backend.schemas.models import AttendanceRecord
        AttendanceRecord(**record)

        print("PASS  the attendance API adds the measurement, the allowance "
              "and the comparison, and drops nothing")
    finally:
        if client is not None:
            client.close()
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_frontend_holds_no_grace_arithmetic():
    """
    The decision is the backend's, and the browser only renders it.

    A band table in JavaScript would be a second source of truth that a
    student could read and that nothing would keep in step with the engine.
    """
    script = (PROJECT_ROOT / "backend" / "static" / "app.js").read_text(
        encoding="utf-8")

    for banned in ("grace_minutes_for", "<= 30", "<= 60", "<= 90"):
        assert banned not in script, (
            "app.js appears to compute a grace period itself: %r" % banned)

    # It must read the backend's figures rather than invent them.
    for field in ("missing_seconds", "grace_seconds", "within_grace"):
        assert field in script, (
            "the interface never shows the backend's %s" % field)

    print("PASS  the interface displays the backend's figures and computes no "
          "grace period of its own")


# -- runner ----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_every_boundary_in_the_requirement,
        test_the_bands_are_inclusive_at_the_top,
        test_seconds_and_minutes_agree,
        test_a_class_with_no_length_allows_nothing,
        test_the_bands_are_a_policy_setting,
        test_an_incoherent_band_table_is_refused,
        test_the_decision_table_in_the_requirement,
        test_one_minute_past_the_allowance_falls_back_to_the_existing_rules,
        test_the_relaxation_grants_a_status_the_ordinary_rules_withheld,
        test_the_relaxation_never_touches_a_present_it_did_not_decide,
        test_a_full_attendance_needs_no_relaxation,
        test_a_short_class_forgives_nothing,
        test_absence_and_uncertainty_are_not_shortfalls,
        test_lateness_is_forgiven_but_not_hidden,
        test_the_relaxation_changes_nothing_but_the_status_and_the_reason,
        test_the_missing_time_stays_visible_and_exact,
        test_the_segments_behind_a_granted_present_still_show_the_gap,
        test_finalizing_a_session_applies_the_relaxation,
        test_no_new_column_was_needed,
        test_the_api_exposes_the_three_figures,
        test_the_frontend_holds_no_grace_arithmetic,
    ]

    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as failure:
            print("FAIL  %s: %s" % (test.__name__, failure))
        except Exception as failure:                      # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(failure).__name__, failure))

    print("\n%d/%d tests passed." % (passed, len(tests)))
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
