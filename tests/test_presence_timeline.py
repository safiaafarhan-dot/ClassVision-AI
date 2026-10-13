"""
Synthetic presence-timeline tests for ai/presence_timeline.py (Step 5).

SYNTHETIC TESTS. Tracks and identities are driven by hand so the exact event
sequence can be asserted. No camera, no model, no network.

These verify TIMELINE LOGIC. They are not attendance, and nothing here
measures recognition accuracy.

Run with:
    python tests/test_presence_timeline.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from face_recognizer import STATUS_MATCH, RecognitionResult  # noqa: E402
from person_tracker import Track  # noqa: E402
from presence_timeline import (  # noqa: E402
    EVENT_IDENTITY_CHANGED_REJECTED,
    EVENT_IDENTITY_CONFIRMED,
    EVENT_PRESENCE_CONTINUED,
    EVENT_TRACK_EXITED,
    EVENT_TRACK_REACQUIRED,
    EVENT_TRACK_STARTED,
    EVENT_TRACK_TEMPORARILY_LOST,
    EVENT_UNKNOWN_PERSON,
    STATE_EXITED,
    STATE_PRESENT,
    STATE_TEMPORARILY_LOST,
    PresenceEvent,
    PresenceTimeline,
)
from track_identity import IdentityManager  # noqa: E402
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(frame):
    """A deterministic UTC timestamp for a frame, at 15 FPS."""
    return START + timedelta(seconds=frame / 15.0)


def a_track(track_id=1, frame=0, visible=True):
    track = Track(track_id, (100, 100, 80, 80), frame, 0.95)
    if not visible:
        track.mark_missed()
    return track


def match(student_id="STU001", name="Safia", similarity=0.82):
    return RecognitionResult(identity=student_id, name=name,
                             similarity=similarity, status=STATUS_MATCH)


def types_of(events):
    return [e.event_type for e in events]


def confirmed_manager(track, student_id="STU001"):
    manager = IdentityManager(confirmations_required=1)
    manager.update(track, match(student_id), 0)
    return manager


# -- event structure -------------------------------------------------------

def test_event_has_time_and_frame():
    event = PresenceEvent(EVENT_TRACK_STARTED, 7, 42, at(42),
                          student_id="STU001", similarity=0.8)

    assert event.frame_index == 42, "frame index missing"
    assert event.timestamp.tzinfo is not None, "timestamp must be timezone aware"
    assert event.timestamp.utcoffset().total_seconds() == 0, (
        "timestamps must be UTC, not local time")
    assert "2026-01-01T12:00:02" in event.timestamp_iso, (
        "ISO timestamp wrong: %s" % event.timestamp_iso)

    data = event.to_dict()
    for field in ("event_type", "track_id", "student_id", "frame_index",
                  "timestamp_utc", "similarity"):
        assert field in data, "to_dict missing %r" % field
    print("PASS  events carry UTC timestamps and frame indexes")


def test_event_formats_readably():
    line = PresenceEvent(EVENT_IDENTITY_CONFIRMED, 7, 10, at(10),
                         student_id="STU001", similarity=0.82).format_line()

    assert "IDENTITY_CONFIRMED" in line, "event type missing from line"
    assert "track=7" in line, "track id missing from line"
    assert "STU001" in line, "student missing from line"
    print("PASS  events format into a readable console line")


def test_event_carries_no_vector():
    event = PresenceEvent(EVENT_TRACK_STARTED, 1, 0, at(0))
    for forbidden in ("embedding", "vector", "features", "crop", "frame_image"):
        assert not hasattr(event, forbidden), "event exposes %r" % forbidden
    print("PASS  presence events carry no biometric vector")


# -- basic lifecycle -------------------------------------------------------

def test_track_started_emitted_once():
    timeline = PresenceTimeline()
    manager = IdentityManager()
    track = a_track()

    first = timeline.update([track], manager, 0, timestamp=at(0))
    second = timeline.update([track], manager, 1, timestamp=at(1))

    assert EVENT_TRACK_STARTED in types_of(first), "no TRACK_STARTED on frame 0"
    assert EVENT_TRACK_STARTED not in types_of(second), (
        "TRACK_STARTED emitted twice")
    print("PASS  TRACK_STARTED is emitted exactly once per track")


def test_identity_confirmed_emitted_once():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)

    first = timeline.update([track], manager, 1, timestamp=at(1))
    second = timeline.update([track], manager, 2, timestamp=at(2))

    assert EVENT_IDENTITY_CONFIRMED in types_of(first), "identity never confirmed"
    assert EVENT_IDENTITY_CONFIRMED not in types_of(second), (
        "IDENTITY_CONFIRMED emitted twice")

    event = [e for e in first if e.event_type == EVENT_IDENTITY_CONFIRMED][0]
    assert event.student_id == "STU001", "wrong student on the event"
    print("PASS  IDENTITY_CONFIRMED is emitted exactly once")


def test_state_becomes_present():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)

    timeline.update([track], manager, 1, timestamp=at(1))
    timeline.update([track], manager, 2, timestamp=at(2))

    assert timeline.records[1].state == STATE_PRESENT, (
        "expected PRESENT, got %s" % timeline.records[1].state)
    print("PASS  a confirmed, visible track settles into PRESENT")


def test_unknown_person_event():
    """
    A stranger is announced only after a fair number of attempts.

    A genuine student is still unconfirmed after their FIRST recognition, so
    announcing UNKNOWN_PERSON then would label every student a stranger one
    frame before naming them.
    """
    timeline = PresenceTimeline(unknown_after_attempts=2)
    manager = IdentityManager(confirmations_required=3)
    track = a_track()

    manager.update(track, RecognitionResult(similarity=0.2), 0)
    after_one = timeline.update([track], manager, 1, timestamp=at(1))
    assert EVENT_UNKNOWN_PERSON not in types_of(after_one), (
        "UNKNOWN_PERSON fired after a single attempt: %s" % types_of(after_one))

    manager.update(track, RecognitionResult(similarity=0.2), 1)
    after_two = timeline.update([track], manager, 2, timestamp=at(2))

    assert EVENT_UNKNOWN_PERSON in types_of(after_two), (
        "UNKNOWN_PERSON should fire after %d attempts: %s"
        % (2, types_of(after_two)))
    assert timeline.records[1].student_id is None, (
        "an unknown person must not be given a student id")
    print("PASS  UNKNOWN_PERSON waits for a second attempt, then fires once")


# -- temporary loss vs exit ------------------------------------------------

def test_temporary_loss_is_not_an_exit():
    """The distinction the whole timeline depends on."""
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)

    timeline.update([track], manager, 1, timestamp=at(1))

    track.mark_missed()
    events = timeline.update([track], manager, 2, timestamp=at(2))

    assert EVENT_TRACK_TEMPORARILY_LOST in types_of(events), (
        "expected TEMPORARILY_LOST, got %s" % types_of(events))
    assert EVENT_TRACK_EXITED not in types_of(events), (
        "a brief disappearance produced an EXIT - this is the bug the grace "
        "period exists to prevent")
    assert timeline.records[1].state == STATE_TEMPORARILY_LOST, "wrong state"
    print("PASS  a brief disappearance is TEMPORARILY_LOST, never an exit")


def test_temporarily_lost_emitted_once_per_episode():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)
    timeline.update([track], manager, 1, timestamp=at(1))

    emitted = 0
    for frame in range(2, 8):
        track.mark_missed()
        emitted += types_of(timeline.update([track], manager, frame,
                                            timestamp=at(frame))
                            ).count(EVENT_TRACK_TEMPORARILY_LOST)

    assert emitted == 1, (
        "TEMPORARILY_LOST should fire once per episode, fired %d times" % emitted)
    print("PASS  TEMPORARILY_LOST fires once per episode, not every frame")


def test_reacquisition_does_not_create_a_second_person():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)

    timeline.update([track], manager, 1, timestamp=at(1))
    track.mark_missed()
    timeline.update([track], manager, 2, timestamp=at(2))

    track.mark_seen((104, 100, 80, 80), 3, 0.95, min_hits=3)
    events = timeline.update([track], manager, 3, timestamp=at(3))

    assert EVENT_TRACK_REACQUIRED in types_of(events), (
        "expected REACQUIRED, got %s" % types_of(events))
    assert EVENT_TRACK_STARTED not in types_of(events), (
        "reacquisition created a second person")
    assert len(timeline.records) == 1, (
        "expected 1 presence record, got %d" % len(timeline.records))
    assert timeline.records[1].lost_episodes == 1, "lost episode not counted"
    print("PASS  reacquisition reuses the record and creates no second person")


def test_exit_only_when_the_tracker_gives_up():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)
    timeline.update([track], manager, 1, timestamp=at(1))

    track.mark_missed()
    timeline.update([track], manager, 2, timestamp=at(2))

    events = timeline.update([], manager, 20, removed_tracks=[track],
                             timestamp=at(20))

    assert EVENT_TRACK_EXITED in types_of(events), (
        "expected EXITED once the tracker retired the track")
    exit_event = [e for e in events if e.event_type == EVENT_TRACK_EXITED][0]
    assert exit_event.student_id == "STU001", "the exit lost the student id"
    assert timeline.records[1].state == STATE_EXITED, "state should be EXITED"
    print("PASS  EXITED is emitted only when the tracker retires the track")


def test_no_duplicate_exit():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)
    timeline.update([track], manager, 1, timestamp=at(1))

    first = timeline.update([], manager, 10, removed_tracks=[track],
                            timestamp=at(10))
    second = timeline.update([], manager, 11, removed_tracks=[track],
                             timestamp=at(11))

    assert types_of(first).count(EVENT_TRACK_EXITED) == 1, "expected one exit"
    assert EVENT_TRACK_EXITED not in types_of(second), "exit emitted twice"
    print("PASS  a track exits exactly once")


def test_full_lifecycle_order():
    """The whole sequence, in order, for one person."""
    timeline = PresenceTimeline(heartbeat_frames=1000)
    track = a_track()
    manager = confirmed_manager(track)

    timeline.update([track], manager, 1, timestamp=at(1))
    timeline.update([track], manager, 2, timestamp=at(2))

    track.mark_missed()
    timeline.update([track], manager, 3, timestamp=at(3))

    track.mark_seen((104, 100, 80, 80), 4, 0.95, min_hits=3)
    timeline.update([track], manager, 4, timestamp=at(4))

    timeline.update([], manager, 20, removed_tracks=[track], timestamp=at(20))

    order = types_of(timeline.events)
    expected = [EVENT_TRACK_STARTED, EVENT_IDENTITY_CONFIRMED,
                EVENT_TRACK_TEMPORARILY_LOST, EVENT_TRACK_REACQUIRED,
                EVENT_TRACK_EXITED]

    assert order == expected, "wrong lifecycle order:\n  got      %s\n  expected %s" % (
        order, expected)

    frames = [e.frame_index for e in timeline.events]
    assert frames == sorted(frames), "events are not in frame order: %s" % frames
    print("PASS  full lifecycle emits the correct events in the correct order")


# -- heartbeat -------------------------------------------------------------

def test_presence_heartbeat():
    timeline = PresenceTimeline(heartbeat_frames=10)
    track = a_track()
    manager = confirmed_manager(track)

    beats = []
    for frame in range(1, 31):
        if EVENT_PRESENCE_CONTINUED in types_of(
                timeline.update([track], manager, frame, timestamp=at(frame))):
            beats.append(frame)

    # The record starts at frame 1, so beats land at 11 and 21. Frame 31 would
    # be the third, which is past the end of this loop.
    assert beats == [11, 21], "expected heartbeats at frames 11 and 21, got %s" % beats
    print("PASS  PRESENCE_CONTINUED fires on the configured heartbeat, not "
          "every frame")


# -- identity switching ----------------------------------------------------

def test_rejected_switch_is_recorded():
    timeline = PresenceTimeline()
    track = a_track()
    manager = IdentityManager(confirmations_required=1, switch_confirmations=4)
    manager.update(track, match("STU001"), 0)
    timeline.update([track], manager, 1, timestamp=at(1))

    _, outcome = manager.update(track, match("STU002", "Rahul", 0.95), 2)
    events = timeline.update([track], manager, 2, timestamp=at(2),
                             outcomes={1: outcome})

    assert EVENT_IDENTITY_CHANGED_REJECTED in types_of(events), (
        "a refused switch should be recorded: %s" % types_of(events))
    assert timeline.records[1].student_id == "STU001", (
        "the record's identity changed despite the rejection")
    print("PASS  a rejected identity switch is recorded and the identity holds")


# -- multiple people -------------------------------------------------------

def test_multiple_tracks_are_independent():
    timeline = PresenceTimeline()
    first, second = a_track(1), a_track(2, frame=0)
    manager = IdentityManager(confirmations_required=1)
    manager.update(first, match("STU001", "Safia"), 0)
    manager.update(second, match("STU002", "Rahul"), 0)

    timeline.update([first, second], manager, 1, timestamp=at(1))

    first.mark_missed()
    events = timeline.update([first, second], manager, 2, timestamp=at(2))

    lost = [e for e in events if e.event_type == EVENT_TRACK_TEMPORARILY_LOST]
    assert len(lost) == 1 and lost[0].track_id == 1, (
        "only track 1 should be lost")
    assert timeline.records[2].state == STATE_PRESENT, (
        "track 2 was affected by track 1 disappearing")
    assert timeline.records[1].student_id == "STU001", "track 1 identity wrong"
    assert timeline.records[2].student_id == "STU002", "track 2 identity wrong"
    print("PASS  two tracks keep independent states and identities")


def test_track_order_does_not_change_events():
    forward = PresenceTimeline()
    backward = PresenceTimeline()
    manager = IdentityManager(confirmations_required=1)

    first, second = a_track(1), a_track(2)
    manager.update(first, match("STU001"), 0)
    manager.update(second, match("STU002"), 0)

    forward_events = forward.update([first, second], manager, 1, timestamp=at(1))
    backward_events = backward.update([second, first], manager, 1, timestamp=at(1))

    forward_pairs = sorted((e.event_type, e.track_id) for e in forward_events)
    backward_pairs = sorted((e.event_type, e.track_id) for e in backward_events)

    assert forward_pairs == backward_pairs, (
        "track order changed the events:\n  %s\n  %s"
        % (forward_pairs, backward_pairs))
    print("PASS  reordering tracks does not change the events produced")


# -- session close and summary --------------------------------------------

def test_close_all_ends_open_presences():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)
    timeline.update([track], manager, 1, timestamp=at(1))

    closing = timeline.close_all(frame_index=50, timestamp=at(50))

    assert len(closing) == 1, "the open presence should be closed"
    assert closing[0].event_type == EVENT_TRACK_EXITED, "should close with EXITED"
    assert timeline.close_all(51, at(51)) == [], "closing twice should be a no-op"
    print("PASS  close_all ends open presences exactly once")


def test_summary_reports_presence_not_attendance():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)

    for frame in range(1, 6):
        timeline.update([track], manager, frame, timestamp=at(frame))
    timeline.update([], manager, 10, removed_tracks=[track], timestamp=at(10))

    summary = timeline.summary()
    assert len(summary) == 1, "expected one record"

    row = summary[0]
    assert row["student_id"] == "STU001", "student missing from summary"
    assert row["visible_frames"] == 5, (
        "expected 5 visible frames, got %s" % row["visible_frames"])
    assert row["duration_seconds"] > 0, "duration should be positive"

    for banned in ("attendance", "present", "status", "is_present"):
        assert banned not in row, (
            "the summary contains an attendance-like field %r - Step 5 must "
            "not decide attendance" % banned)
    print("PASS  the summary reports presence duration, not attendance")


def test_events_can_be_filtered():
    timeline = PresenceTimeline()
    track = a_track()
    manager = confirmed_manager(track)
    timeline.update([track], manager, 1, timestamp=at(1))

    assert len(timeline.events_for_track(1)) >= 2, "track filter failed"
    assert len(timeline.events_for_student("STU001")) >= 1, "student filter failed"
    assert timeline.events_for_student("NOBODY") == [], "unknown student filter"
    print("PASS  events can be filtered by track and by student")


def main():
    enable_safe_console()

    tests = [
        test_event_has_time_and_frame,
        test_event_formats_readably,
        test_event_carries_no_vector,
        test_track_started_emitted_once,
        test_identity_confirmed_emitted_once,
        test_state_becomes_present,
        test_unknown_person_event,
        test_temporary_loss_is_not_an_exit,
        test_temporarily_lost_emitted_once_per_episode,
        test_reacquisition_does_not_create_a_second_person,
        test_exit_only_when_the_tracker_gives_up,
        test_no_duplicate_exit,
        test_full_lifecycle_order,
        test_presence_heartbeat,
        test_rejected_switch_is_recorded,
        test_multiple_tracks_are_independent,
        test_track_order_does_not_change_events,
        test_close_all_ends_open_presences,
        test_summary_reports_presence_not_attendance,
        test_events_can_be_filtered,
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
