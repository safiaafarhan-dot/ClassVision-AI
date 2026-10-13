"""
Synthetic attendance-rule tests for ai/attendance_rules.py (Step 6).

SYNTHETIC TESTS. Every timeline here is constructed by hand so the arithmetic
and the state decisions can be asserted exactly. No camera, no model, no
database, no network.

These verify RULE LOGIC. They say nothing about real classroom accuracy, which
has not been established.

Run with:
    python tests/test_attendance_rules.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from attendance_rules import (  # noqa: E402
    STATUS_ABSENT,
    STATUS_LATE,
    STATUS_LEFT_EARLY,
    STATUS_PARTIAL,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    AttendanceDecision,
    AttendancePolicy,
    AttendancePolicyError,
    AttendanceRuleEngine,
    PresenceSegment,
    merge_segments,
    segments_from_events,
    summarize_presence,
)
from attendance_session import (  # noqa: E402
    SESSION_ACTIVE,
    SESSION_CANCELLED,
    SESSION_COMPLETED,
    SESSION_SCHEDULED,
    AttendanceSession,
    SessionStateError,
)
from presence_timeline import (  # noqa: E402
    EVENT_IDENTITY_CONFIRMED,
    EVENT_TRACK_EXITED,
    EVENT_TRACK_REACQUIRED,
    EVENT_TRACK_STARTED,
    EVENT_TRACK_TEMPORARILY_LOST,
    EVENT_UNKNOWN_PERSON,
    PresenceEvent,
)
from paths import enable_safe_console  # noqa: E402

# A one-hour session starting at 09:00 UTC.
START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def at(minutes, seconds=0):
    return START + timedelta(minutes=minutes, seconds=seconds)


def a_session(status=SESSION_COMPLETED, expected=("STU001",),
              start=START, end=END):
    session = AttendanceSession("SES-1", "CLASS-10A", start, end,
                                expected_students=list(expected))
    if status in (SESSION_ACTIVE, SESSION_COMPLETED):
        session.start()
    if status == SESSION_COMPLETED:
        session.complete(completed_at=end)
    if status == SESSION_CANCELLED:
        session.cancel()
    return session


def segment(student_id, from_minute, to_minute, track_id=1):
    return PresenceSegment(student_id, at(from_minute), at(to_minute), track_id)


def a_policy(**overrides):
    defaults = dict(minimum_presence_ratio=0.75, minimum_presence_seconds=0,
                    late_after_seconds=600, early_leave_before_seconds=300)
    defaults.update(overrides)
    return AttendancePolicy(**defaults)


def event(event_type, minute, second=0, track_id=1, student_id=None, frame=0):
    return PresenceEvent(event_type, track_id, frame, at(minute, second),
                         student_id=student_id)


# -- policy ----------------------------------------------------------------

def test_policy_defaults_are_documented_as_development_values():
    policy = AttendancePolicy()
    assert policy.version == "1.0", "a policy must carry a version"
    data = policy.to_dict()
    for field in ("minimum_presence_ratio", "late_after_seconds",
                  "early_leave_before_seconds", "version"):
        assert field in data, "policy is missing %r" % field
    print("PASS  a policy exposes its version and every threshold")


def test_invalid_policies_are_rejected():
    bad = [
        (dict(minimum_presence_ratio=1.5), "ratio above 1"),
        (dict(minimum_presence_ratio=-0.1), "negative ratio"),
        (dict(minimum_presence_seconds=-1), "negative duration"),
        (dict(late_after_seconds=-5), "negative late threshold"),
        (dict(early_leave_before_seconds=-5), "negative early-leave threshold"),
        (dict(temporary_loss_grace_seconds=-1), "negative grace"),
        (dict(version=""), "empty version"),
        (dict(count_unverified_presence=True), "counting unverified presence"),
    ]
    for kwargs, description in bad:
        try:
            AttendancePolicy(**kwargs)
        except AttendancePolicyError:
            continue
        raise AssertionError("policy should have been rejected: " + description)
    print("PASS  %d invalid policies are rejected" % len(bad))


def test_unverified_presence_can_never_be_counted():
    """Guard on the rule that an unknown face is never somebody's attendance."""
    try:
        AttendancePolicy(count_unverified_presence=True)
    except AttendancePolicyError as error:
        assert "unverified" in str(error), "the refusal should explain itself"
        print("PASS  a policy cannot be configured to count unverified presence")
        return
    raise AssertionError("count_unverified_presence must be refused")


# -- segments and duration -------------------------------------------------

def test_single_segment_duration():
    summary = summarize_presence("STU001", [segment("STU001", 0, 30)], START, END)
    assert summary.present_seconds == 1800, (
        "expected 1800s, got %s" % summary.present_seconds)
    assert summary.segment_count == 1, "expected one segment"
    print("PASS  a single 30-minute segment is 1800 seconds")


def test_multiple_segments_are_added():
    segments = [segment("STU001", 0, 20), segment("STU001", 21, 40)]
    summary = summarize_presence("STU001", segments, START, END)
    assert summary.present_seconds == (20 + 19) * 60, (
        "expected 2340s, got %s" % summary.present_seconds)
    assert summary.segment_count == 2, "expected two distinct segments"
    assert summary.loss_periods == 1, "one gap between two segments"
    assert summary.loss_seconds == 60, (
        "the gap should be 60s, got %s" % summary.loss_seconds)
    print("PASS  two separated segments add up and record the gap between them")


def test_overlapping_segments_are_not_double_counted():
    """09:00-09:20 plus 09:10-09:40 is 40 minutes, not 50."""
    segments = [segment("STU001", 0, 20, track_id=7),
                segment("STU001", 10, 40, track_id=15)]
    summary = summarize_presence("STU001", segments, START, END)

    assert summary.present_seconds == 40 * 60, (
        "overlap was double counted: %s seconds" % summary.present_seconds)
    assert summary.segment_count == 1, "overlapping segments should merge into one"
    print("PASS  overlapping segments merge - 40 minutes, not 50")


def test_adjacent_segments_merge():
    segments = [segment("STU001", 0, 20), segment("STU001", 20, 40)]
    summary = summarize_presence("STU001", segments, START, END)
    assert summary.segment_count == 1, "touching segments should merge"
    assert summary.present_seconds == 40 * 60, "duration wrong after merge"
    print("PASS  touching segments merge into one")


def test_zero_duration_segments_are_dropped():
    segments = [segment("STU001", 10, 10), segment("STU001", 0, 20)]
    summary = summarize_presence("STU001", segments, START, END)
    assert summary.present_seconds == 20 * 60, (
        "a zero-length segment changed the total: %s" % summary.present_seconds)
    print("PASS  zero-duration segments are ignored")


def test_segments_are_clipped_to_the_session():
    """Presence before or after the session must not count towards it."""
    early = PresenceSegment("STU001", START - timedelta(minutes=30), at(10))
    late = PresenceSegment("STU001", at(50), END + timedelta(minutes=30))
    summary = summarize_presence("STU001", [early, late], START, END)

    assert summary.present_seconds == (10 + 10) * 60, (
        "presence outside the session was counted: %s" % summary.present_seconds)
    print("PASS  presence outside the session window is clipped away")


def test_malformed_segments_are_rejected():
    bad = [
        (lambda: PresenceSegment("STU001", at(20), at(10)), "end before start"),
        (lambda: PresenceSegment("", at(0), at(10)), "no student id"),
        (lambda: PresenceSegment("STU001", "09:00", at(10)), "a string time"),
        (lambda: PresenceSegment("STU001", datetime(2026, 3, 2, 9, 0), at(10)),
         "a naive datetime"),
    ]
    for build, description in bad:
        try:
            build()
        except ValueError:
            continue
        raise AssertionError("segment should have been rejected: " + description)
    print("PASS  %d malformed segments are rejected" % len(bad))


def test_multiple_tracks_one_student():
    """Two track ids for one person is one student, not two."""
    segments = [segment("STU001", 0, 20, track_id=7),
                segment("STU001", 21, 45, track_id=15)]
    summary = summarize_presence("STU001", segments, START, END)

    assert summary.present_seconds == (20 + 24) * 60, (
        "expected 44 minutes, got %.0f" % (summary.present_seconds / 60))
    assert summary.track_ids == [7, 15], "both tracks should be recorded"
    print("PASS  two tracks for one student merge into 44 minutes of presence")


def test_longest_segment_is_reported():
    segments = [segment("STU001", 0, 5), segment("STU001", 10, 40),
                segment("STU001", 45, 50)]
    summary = summarize_presence("STU001", segments, START, END)
    assert summary.longest_segment_seconds == 30 * 60, (
        "longest segment wrong: %s" % summary.longest_segment_seconds)
    print("PASS  the longest continuous segment is reported")


# -- timeline -> segments --------------------------------------------------

def test_unknown_person_produces_no_segment():
    """The rule that an unidentified face is never somebody's attendance."""
    events = [event(EVENT_TRACK_STARTED, 0),
              event(EVENT_UNKNOWN_PERSON, 0, 5),
              event(EVENT_TRACK_EXITED, 30)]
    assert segments_from_events(events) == [], (
        "an unidentified track produced presence")
    print("PASS  an unidentified person produces no presence segment")


def test_identity_confirmed_opens_a_segment():
    events = [event(EVENT_TRACK_STARTED, 0),
              event(EVENT_IDENTITY_CONFIRMED, 0, 10, student_id="STU001"),
              event(EVENT_TRACK_EXITED, 30)]
    segments = segments_from_events(events)

    assert len(segments) == 1, "expected one segment, got %d" % len(segments)
    assert segments[0].student_id == "STU001", "wrong student"
    assert segments[0].duration_seconds == 30 * 60 - 10, (
        "segment should start at identification: %s"
        % segments[0].duration_seconds)
    print("PASS  a segment starts when the identity is confirmed, not before")


def test_temporary_loss_inside_grace_keeps_one_segment():
    events = [event(EVENT_IDENTITY_CONFIRMED, 0, student_id="STU001"),
              event(EVENT_TRACK_TEMPORARILY_LOST, 10),
              event(EVENT_TRACK_REACQUIRED, 10, 15),
              event(EVENT_TRACK_EXITED, 30)]
    segments = segments_from_events(events, grace_seconds=30)

    assert len(segments) == 1, (
        "a brief loss split the presence into %d segments" % len(segments))
    assert segments[0].duration_seconds == 30 * 60, (
        "the gap was subtracted: %s" % segments[0].duration_seconds)
    print("PASS  a loss inside the grace period keeps one continuous segment")


def test_long_loss_splits_the_segment():
    events = [event(EVENT_IDENTITY_CONFIRMED, 0, student_id="STU001"),
              event(EVENT_TRACK_TEMPORARILY_LOST, 10),
              event(EVENT_TRACK_REACQUIRED, 20),
              event(EVENT_TRACK_EXITED, 30)]
    segments = segments_from_events(events, grace_seconds=30)

    assert len(segments) == 2, (
        "a 10-minute absence should split presence, got %d" % len(segments))
    total = sum(s.duration_seconds for s in segments)
    assert total == 20 * 60, "the absent 10 minutes were counted: %s" % total
    print("PASS  a loss beyond the grace period splits the segment and the "
          "absent time is not counted")


def test_exit_closes_at_last_seen_not_at_giving_up():
    events = [event(EVENT_IDENTITY_CONFIRMED, 0, student_id="STU001"),
              event(EVENT_TRACK_TEMPORARILY_LOST, 20),
              event(EVENT_TRACK_EXITED, 21)]
    segments = segments_from_events(events)

    assert len(segments) == 1, "expected one segment"
    assert segments[0].duration_seconds == 20 * 60, (
        "presence was credited past the last sighting: %s"
        % segments[0].duration_seconds)
    print("PASS  an exit credits presence up to the last sighting")


def test_return_after_exit_is_a_new_segment():
    events = [event(EVENT_IDENTITY_CONFIRMED, 0, track_id=7, student_id="STU001"),
              event(EVENT_TRACK_EXITED, 20, track_id=7),
              event(EVENT_IDENTITY_CONFIRMED, 25, track_id=15, student_id="STU001"),
              event(EVENT_TRACK_EXITED, 45, track_id=15)]
    segments = segments_from_events(events)

    assert len(segments) == 2, "expected two segments, got %d" % len(segments)
    assert {s.track_id for s in segments} == {7, 15}, "track ids not preserved"
    summary = summarize_presence("STU001", segments, START, END)
    assert summary.present_seconds == 40 * 60, (
        "expected 40 minutes across two visits, got %.0f"
        % (summary.present_seconds / 60))
    print("PASS  leaving and returning creates two segments for one student")


def test_open_segment_is_closed_at_session_end():
    events = [event(EVENT_IDENTITY_CONFIRMED, 0, student_id="STU001")]
    segments = segments_from_events(events, session_end=END)
    assert len(segments) == 1, "an open segment should be closed"
    assert segments[0].end == END, "it should close at the session end"
    print("PASS  a still-open presence is closed at the session end")


# -- attendance status -----------------------------------------------------

def test_present():
    engine = AttendanceRuleEngine(a_policy())
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 0, 58)], a_session())

    assert decision.status == STATUS_PRESENT, (
        "expected PRESENT, got %s (%s)" % (decision.status, decision.reason))
    assert decision.presence_ratio > 0.9, "ratio wrong: %.3f" % decision.presence_ratio
    assert "met the minimum" in decision.reason, "the reason should explain why"
    print("PASS  full attendance is PRESENT (ratio %.3f)" % decision.presence_ratio)


def test_late():
    engine = AttendanceRuleEngine(a_policy(late_after_seconds=600))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 14, 60)], a_session())

    assert decision.status == STATUS_LATE, (
        "expected LATE, got %s (%s)" % (decision.status, decision.reason))
    assert decision.was_late, "the late flag should be set"
    assert "late threshold" in decision.reason, "the reason should name the threshold"
    print("PASS  arriving at 09:14 with a 09:10 threshold is LATE")


def test_not_late_when_on_time():
    engine = AttendanceRuleEngine(a_policy(late_after_seconds=600))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 5, 60)], a_session())
    assert decision.status == STATUS_PRESENT, (
        "arriving at 09:05 should not be late, got %s" % decision.status)
    print("PASS  arriving before the late threshold is not LATE")


def test_partial():
    engine = AttendanceRuleEngine(a_policy(minimum_presence_ratio=0.75))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 0, 20)], a_session())

    assert decision.status == STATUS_PARTIAL, (
        "expected PARTIAL, got %s" % decision.status)
    assert abs(decision.presence_ratio - (1.0 / 3.0)) < 0.01, (
        "ratio wrong: %.3f" % decision.presence_ratio)
    assert "below the required" in decision.reason, "the reason should explain"
    print("PASS  20 minutes of a 60-minute session is PARTIAL (ratio %.3f)"
          % decision.presence_ratio)


def test_partial_by_minimum_seconds():
    engine = AttendanceRuleEngine(
        a_policy(minimum_presence_ratio=0.0, minimum_presence_seconds=2400))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 0, 20)], a_session())
    assert decision.status == STATUS_PARTIAL, (
        "a minimum duration should be enforced, got %s" % decision.status)
    assert "1200s is below the required 2400s" in decision.reason, (
        "the reason should quote both numbers: %s" % decision.reason)
    print("PASS  a minimum-duration rule produces PARTIAL with exact numbers")


def test_left_early():
    engine = AttendanceRuleEngine(
        a_policy(minimum_presence_ratio=0.5, early_leave_before_seconds=300))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 0, 40)], a_session())

    assert decision.status == STATUS_LEFT_EARLY, (
        "expected LEFT_EARLY, got %s (%s)" % (decision.status, decision.reason))
    assert decision.left_early, "the early-leave flag should be set"
    print("PASS  leaving at 09:40 with a 09:55 threshold is LEFT_EARLY")


def test_left_early_needs_a_completed_session():
    """A camera being stopped must never look like a student leaving early."""
    engine = AttendanceRuleEngine(
        a_policy(minimum_presence_ratio=0.5, early_leave_before_seconds=300))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 0, 40)], a_session(status=SESSION_ACTIVE))

    assert decision.status != STATUS_LEFT_EARLY, (
        "an active session must not produce LEFT_EARLY")
    assert not decision.left_early, "the early-leave flag should not be set"
    print("PASS  LEFT_EARLY is never decided while the session is still active")


def test_absent_only_after_completion():
    engine = AttendanceRuleEngine(a_policy())

    completed = engine.evaluate_student("STU004", [], a_session(
        status=SESSION_COMPLETED))
    assert completed.status == STATUS_ABSENT, (
        "a completed session with no presence should be ABSENT, got %s"
        % completed.status)
    assert "completed session" in completed.reason, "the reason should say why"

    active = engine.evaluate_student("STU004", [], a_session(status=SESSION_ACTIVE))
    assert active.status == STATUS_UNKNOWN, (
        "an active session must not mark anyone ABSENT, got %s" % active.status)

    scheduled = engine.evaluate_student("STU004", [],
                                        a_session(status=SESSION_SCHEDULED))
    assert scheduled.status == STATUS_UNKNOWN, (
        "a scheduled session must not mark anyone ABSENT, got %s"
        % scheduled.status)

    cancelled = engine.evaluate_student("STU004", [],
                                        a_session(status=SESSION_CANCELLED))
    assert cancelled.status == STATUS_UNKNOWN, (
        "a cancelled session must not mark anyone ABSENT, got %s"
        % cancelled.status)

    print("PASS  ABSENT is produced only for a COMPLETED session")


def test_partial_becomes_absent_when_policy_forbids_partial():
    engine = AttendanceRuleEngine(a_policy(allow_partial=False))
    decision = engine.evaluate_student(
        "STU001", [segment("STU001", 0, 10)], a_session())
    assert decision.status == STATUS_ABSENT, (
        "with allow_partial off, insufficient presence should be ABSENT")
    assert "does not allow partial" in decision.reason, "the reason should say so"
    print("PASS  allow_partial=False turns insufficient presence into ABSENT")


# -- explainability --------------------------------------------------------

def test_every_decision_explains_itself():
    engine = AttendanceRuleEngine(a_policy())
    session = a_session()
    cases = [
        ("STU001", [segment("STU001", 0, 58)]),
        ("STU002", [segment("STU002", 14, 60)]),
        ("STU003", [segment("STU003", 0, 20)]),
        ("STU004", []),
    ]
    for student_id, segments in cases:
        decision = engine.evaluate_student(student_id, segments, session)
        assert decision.reason and len(decision.reason) > 20, (
            "%s has no useful reason: %r" % (student_id, decision.reason))
        assert decision.policy_version == "1.0", "policy version not recorded"

        data = decision.to_dict()
        for field in ("status", "reason", "present_seconds", "session_seconds",
                      "presence_ratio", "policy_version", "was_late",
                      "left_early"):
            assert field in data, "decision is missing %r" % field
    print("PASS  every decision carries a reason and its policy version")


def test_decision_carries_no_biometric_data():
    decision = AttendanceDecision("STU001", "SES-1", STATUS_PRESENT,
                                  "reason", "1.0")
    for forbidden in ("embedding", "embeddings", "vector", "face_image",
                      "crop", "frame", "image"):
        assert not hasattr(decision, forbidden), (
            "decision exposes %r" % forbidden)
    print("PASS  an attendance decision carries no biometric data")


# -- expected students and sessions ---------------------------------------

def test_only_expected_students_get_records():
    engine = AttendanceRuleEngine(a_policy())
    session = a_session(expected=("STU001", "STU002"))
    segments = [segment("STU001", 0, 58), segment("STU999", 0, 58)]

    decisions = engine.evaluate_session(session, segments,
                                        session.expected_students)
    ids = sorted(d.student_id for d in decisions)

    assert ids == ["STU001", "STU002"], (
        "only expected students should be evaluated, got %s" % ids)
    assert "STU999" not in ids, "an unexpected person produced an attendance record"
    print("PASS  only expected students receive attendance records")


def test_session_lifecycle():
    session = AttendanceSession("S1", "C1", START, END)
    assert session.status == SESSION_SCHEDULED, "a new session is scheduled"
    assert not session.can_finalize_absence, "cannot finalise absence yet"

    session.start()
    assert session.is_active, "session should be active"
    assert not session.can_finalize_absence, "an active session cannot finalise"

    session.complete()
    assert session.is_completed, "session should be completed"
    assert session.can_finalize_absence, "a completed session can finalise"
    assert session.completed_at is not None, "completion time not recorded"
    print("PASS  the session lifecycle gates absence finalisation")


def test_illegal_session_transitions_are_refused():
    bad = [
        (lambda: AttendanceSession("S", "C", START, END).complete(),
         "SCHEDULED straight to COMPLETED"),
        (lambda: AttendanceSession("S", "C", START, END).start().start(),
         "ACTIVE to ACTIVE"),
        (lambda: AttendanceSession("S", "C", START, END).start().complete().start(),
         "COMPLETED back to ACTIVE"),
        (lambda: AttendanceSession("S", "C", START, END).cancel().start(),
         "CANCELLED to ACTIVE"),
    ]
    for build, description in bad:
        try:
            build()
        except SessionStateError:
            continue
        raise AssertionError("transition should be refused: " + description)
    print("PASS  %d illegal session transitions are refused" % len(bad))


def test_invalid_sessions_are_rejected():
    bad = [
        (lambda: AttendanceSession("", "C", START, END), "no session id"),
        (lambda: AttendanceSession("S", "", START, END), "no class id"),
        (lambda: AttendanceSession("S", "C", END, START), "end before start"),
        (lambda: AttendanceSession("S", "C", START, START), "zero length"),
        (lambda: AttendanceSession("S", "C", datetime(2026, 3, 2, 9), END),
         "a naive start time"),
    ]
    for build, description in bad:
        try:
            build()
        except ValueError:
            continue
        raise AssertionError("session should be rejected: " + description)
    print("PASS  %d invalid sessions are rejected" % len(bad))


# -- policy versioning -----------------------------------------------------

def test_same_evidence_different_policy_gives_different_results():
    """The reason policy version is recorded with every decision."""
    session = a_session()
    segments = [segment("STU001", 0, 35)]

    # early_leave is switched off so this test isolates the ratio rule.
    # With it on, 35 minutes would correctly be LEFT_EARLY under both policies.
    lenient = AttendanceRuleEngine(
        AttendancePolicy(version="1.0", minimum_presence_ratio=0.5,
                         early_leave_before_seconds=None))
    strict = AttendanceRuleEngine(
        AttendancePolicy(version="2.0", minimum_presence_ratio=0.9,
                         early_leave_before_seconds=None))

    first = lenient.evaluate_student("STU001", segments, session)
    second = strict.evaluate_student("STU001", segments, session)

    assert first.status == STATUS_PRESENT, "lenient policy should pass"
    assert second.status == STATUS_PARTIAL, "strict policy should not pass"
    assert first.policy_version == "1.0", "version 1 not recorded"
    assert second.policy_version == "2.0", "version 2 not recorded"
    assert first.present_seconds == second.present_seconds, (
        "the evidence must be identical - only the policy differs")
    print("PASS  identical evidence under two policies gives two results, each "
          "tagged with its policy version")


def test_configurable_thresholds_change_the_outcome():
    session = a_session()
    segments = [segment("STU001", 12, 60)]

    strict = AttendanceRuleEngine(a_policy(late_after_seconds=600))
    relaxed = AttendanceRuleEngine(a_policy(late_after_seconds=1200))

    assert strict.evaluate_student("STU001", segments, session).status == STATUS_LATE
    assert relaxed.evaluate_student("STU001", segments, session).status == STATUS_PRESENT
    print("PASS  the late threshold is genuinely configurable")


def main():
    enable_safe_console()

    tests = [
        test_policy_defaults_are_documented_as_development_values,
        test_invalid_policies_are_rejected,
        test_unverified_presence_can_never_be_counted,
        test_single_segment_duration,
        test_multiple_segments_are_added,
        test_overlapping_segments_are_not_double_counted,
        test_adjacent_segments_merge,
        test_zero_duration_segments_are_dropped,
        test_segments_are_clipped_to_the_session,
        test_malformed_segments_are_rejected,
        test_multiple_tracks_one_student,
        test_longest_segment_is_reported,
        test_unknown_person_produces_no_segment,
        test_identity_confirmed_opens_a_segment,
        test_temporary_loss_inside_grace_keeps_one_segment,
        test_long_loss_splits_the_segment,
        test_exit_closes_at_last_seen_not_at_giving_up,
        test_return_after_exit_is_a_new_segment,
        test_open_segment_is_closed_at_session_end,
        test_present,
        test_late,
        test_not_late_when_on_time,
        test_partial,
        test_partial_by_minimum_seconds,
        test_left_early,
        test_left_early_needs_a_completed_session,
        test_absent_only_after_completion,
        test_partial_becomes_absent_when_policy_forbids_partial,
        test_every_decision_explains_itself,
        test_decision_carries_no_biometric_data,
        test_only_expected_students_get_records,
        test_session_lifecycle,
        test_illegal_session_transitions_are_refused,
        test_invalid_sessions_are_rejected,
        test_same_evidence_different_policy_gives_different_results,
        test_configurable_thresholds_change_the_outcome,
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
