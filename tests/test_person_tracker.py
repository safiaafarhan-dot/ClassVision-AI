"""
Synthetic tracker tests for ai/person_tracker.py (Step 5).

SYNTHETIC TRACKER TESTS. Detections are constructed by hand so every
scenario - crossing, disappearing, re-entering - is exactly reproducible.

These verify TRACKING LOGIC only. They are not real-world validation and say
nothing about recognition accuracy.

Run with:
    python tests/test_person_tracker.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from person_tracker import (  # noqa: E402
    TRACK_CONFIRMED,
    TRACK_LOST,
    TRACK_TENTATIVE,
    PersonTracker,
    Track,
    iou,
)
from paths import enable_safe_console  # noqa: E402


class FakeDetection:
    """Stands in for a YuNet Detection. Same interface the tracker uses."""

    def __init__(self, x, y, width=80, height=80, confidence=0.95):
        self.x, self.y = int(x), int(y)
        self.width, self.height = int(width), int(height)
        self.confidence = float(confidence)
        self.landmarks = None

    def as_tuple(self):
        return (self.x, self.y, self.width, self.height)

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height


def run_frames(tracker, sequence):
    """Feed a list of per-frame detection lists. Returns per-frame results."""
    return [tracker.update(detections) for detections in sequence]


# -- geometry --------------------------------------------------------------

def test_iou():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0, "identical boxes = 1.0"
    assert iou((0, 0, 10, 10), (100, 100, 10, 10)) == 0.0, "no overlap = 0.0"
    assert iou((0, 0, 10, 10), (0, 0, 0, 0)) == 0.0, "empty box = 0.0"

    partial = iou((0, 0, 10, 10), (5, 0, 10, 10))
    assert abs(partial - (50 / 150.0)) < 1e-6, "half overlap wrong: %.4f" % partial
    print("PASS  IoU is correct for identical, disjoint, empty and partial boxes")


# -- basic tracking --------------------------------------------------------

def test_one_detection_creates_one_track():
    tracker = PersonTracker()
    tracks = tracker.update([FakeDetection(100, 100)])

    assert len(tracks) == 1, "expected 1 track, got %d" % len(tracks)
    assert tracks[0].track_id == 1, "first track should be id 1"
    assert tracks[0].state == TRACK_TENTATIVE, "a brand new track is tentative"
    print("PASS  one detection creates one tentative track")


def test_multiple_detections_create_independent_tracks():
    tracker = PersonTracker()
    tracks = tracker.update([FakeDetection(50, 50),
                             FakeDetection(300, 50),
                             FakeDetection(500, 300)])

    ids = sorted(t.track_id for t in tracks)
    assert len(tracks) == 3, "expected 3 tracks, got %d" % len(tracks)
    assert ids == [1, 2, 3], "track ids should be distinct: %s" % ids
    print("PASS  three separated detections create three independent tracks")


def test_track_id_is_stable_across_frames():
    tracker = PersonTracker()
    for x in (100, 104, 108, 112, 116, 120):
        tracks = tracker.update([FakeDetection(x, 100)])

    assert len(tracks) == 1, "a single moving person should stay one track"
    assert tracks[0].track_id == 1, "the track id changed to %d" % tracks[0].track_id
    assert tracks[0].hits == 6, "expected 6 hits, got %d" % tracks[0].hits
    print("PASS  a moving person keeps the same track id across 6 frames")


def test_track_becomes_confirmed_after_min_hits():
    tracker = PersonTracker(min_hits=3)

    first = tracker.update([FakeDetection(100, 100)])[0]
    assert first.state == TRACK_TENTATIVE, "1 hit should be tentative"

    tracker.update([FakeDetection(102, 100)])
    third = tracker.update([FakeDetection(104, 100)])[0]

    assert third.state == TRACK_CONFIRMED, (
        "3 hits should confirm, got %s" % third.state)
    print("PASS  a track is confirmed once it reaches min_hits")


def test_two_people_stay_separate():
    tracker = PersonTracker()
    for step in range(6):
        tracks = tracker.update([FakeDetection(50 + step * 3, 100),
                                 FakeDetection(400 + step * 3, 100)])

    assert len(tracks) == 2, "expected 2 tracks, got %d" % len(tracks)
    assert {t.track_id for t in tracks} == {1, 2}, "track ids drifted"
    assert tracker._next_track_id == 3, "extra tracks were created"
    print("PASS  two people moving in parallel keep two stable tracks")


# -- ordering independence -------------------------------------------------

def test_detection_order_does_not_change_association():
    """
    The detector may return faces in any order. Track assignment must not
    depend on it - otherwise identities would shuffle between frames.
    """
    left = FakeDetection(50, 100)
    right = FakeDetection(400, 100)

    forward = PersonTracker()
    forward.update([left, right])
    forward_result = {t.track_id: t.box for t in
                      forward.update([FakeDetection(53, 100),
                                      FakeDetection(403, 100)])}

    backward = PersonTracker()
    backward.update([left, right])
    backward_result = {t.track_id: t.box for t in
                       backward.update([FakeDetection(403, 100),
                                        FakeDetection(53, 100)])}

    assert forward_result == backward_result, (
        "reversing detection order changed the tracks:\n  %s\n  %s"
        % (forward_result, backward_result))
    print("PASS  reversing detection order produces identical tracks")


def test_creation_order_is_deterministic():
    """New tracks must be numbered the same way regardless of input order."""
    first = PersonTracker()
    first.update([FakeDetection(50, 50), FakeDetection(400, 300)])
    first_boxes = sorted(t.box for t in first.tracks)

    second = PersonTracker()
    second.update([FakeDetection(400, 300), FakeDetection(50, 50)])
    second_boxes = sorted(t.box for t in second.tracks)

    assert first_boxes == second_boxes, "different boxes were tracked"
    assert first.track_count == second.track_count == 2, "track count differs"
    print("PASS  track creation is deterministic under input reordering")


# -- disappearing and returning -------------------------------------------

def test_missing_detection_becomes_lost_not_removed():
    tracker = PersonTracker(max_lost_frames=10)
    tracker.update([FakeDetection(100, 100)])
    tracker.update([])

    assert tracker.track_count == 1, "the track should still exist"
    track = tracker.get(1)
    assert track.state == TRACK_LOST, "expected LOST, got %s" % track.state
    assert not track.is_visible, "a lost track is not visible"
    assert tracker.active_tracks == [], "a lost track is not active"
    print("PASS  a missing detection becomes LOST, not removed")


def test_reacquisition_within_grace_period():
    tracker = PersonTracker(max_lost_frames=10)
    tracker.update([FakeDetection(100, 100)])
    tracker.update([FakeDetection(102, 100)])

    for _ in range(4):
        tracker.update([])

    tracks = tracker.update([FakeDetection(106, 100)])

    assert len(tracks) == 1, "the person should be tracked again"
    assert tracks[0].track_id == 1, (
        "reacquisition created a new id %d instead of reusing 1"
        % tracks[0].track_id)
    assert tracks[0].reacquired, "the track should be flagged as reacquired"
    print("PASS  a person returning within the grace period keeps track id 1")


def test_track_expires_after_grace_period():
    tracker = PersonTracker(max_lost_frames=5)
    tracker.update([FakeDetection(100, 100)])

    for _ in range(6):
        tracker.update([])

    assert tracker.track_count == 0, "the track should have been retired"
    removed = tracker.take_removed()
    assert len(removed) == 1, "the retired track should be reported once"
    assert removed[0].track_id == 1, "the wrong track was retired"
    assert tracker.take_removed() == [], "removed tracks should only report once"
    print("PASS  a track expires after the grace period and reports once")


def test_grace_period_is_configurable():
    short = PersonTracker(max_lost_frames=2)
    short.update([FakeDetection(100, 100)])
    for _ in range(3):
        short.update([])
    assert short.track_count == 0, "short grace period should expire the track"

    long = PersonTracker(max_lost_frames=30)
    long.update([FakeDetection(100, 100)])
    for _ in range(3):
        long.update([])
    assert long.track_count == 1, "long grace period should keep the track"
    print("PASS  the grace period is configurable (2 expires, 30 keeps)")


def test_person_leaves_and_a_new_person_arrives():
    """A genuinely new person must get a NEW track id, not inherit one."""
    tracker = PersonTracker(max_lost_frames=3)

    for _ in range(4):
        tracker.update([FakeDetection(100, 100)])
    for _ in range(5):
        tracker.update([])

    tracks = tracker.update([FakeDetection(500, 300)])

    assert len(tracks) == 1, "the new person should be tracked"
    assert tracks[0].track_id != 1, (
        "a new person inherited the old track id - identities would leak")
    print("PASS  a new person after an exit gets a fresh track id (%d)"
          % tracks[0].track_id)


# -- ByteTrack-style two-stage association ---------------------------------

def test_low_confidence_continues_but_never_creates():
    tracker = PersonTracker(high_confidence=0.7)

    tracker.update([FakeDetection(100, 100, confidence=0.95)])
    tracks = tracker.update([FakeDetection(102, 100, confidence=0.4)])

    assert len(tracks) == 1, "a weak detection should continue the track"
    assert tracks[0].track_id == 1, "the track id changed"
    assert tracks[0].hits == 2, "the weak detection should count as a hit"

    fresh = PersonTracker(high_confidence=0.7)
    assert fresh.update([FakeDetection(50, 50, confidence=0.4)]) == [], (
        "a weak detection alone must not create a track")
    print("PASS  weak detections continue tracks but never create them")


def test_high_confidence_threshold_is_configurable():
    lenient = PersonTracker(high_confidence=0.3)
    assert len(lenient.update([FakeDetection(50, 50, confidence=0.5)])) == 1, (
        "0.5 should create a track when the bar is 0.3")

    strict = PersonTracker(high_confidence=0.9)
    assert strict.update([FakeDetection(50, 50, confidence=0.5)]) == [], (
        "0.5 should not create a track when the bar is 0.9")
    print("PASS  the new-track confidence bar is configurable")


# -- movement and crossing -------------------------------------------------

def test_prediction_helps_a_fast_mover():
    """Velocity carries the box forward so a fast walker is not lost."""
    tracker = PersonTracker(iou_threshold=0.3)

    for x in (100, 130, 160, 190):
        tracks = tracker.update([FakeDetection(x, 100)])

    assert len(tracks) == 1, "a steadily moving person should stay one track"
    assert tracks[0].track_id == 1, "the fast mover lost its id"
    assert tracks[0].velocity[0] > 0, "velocity should be positive, got %r" % (
        tracks[0].velocity,)
    print("PASS  a fast mover keeps its track (velocity %.1f px/frame)"
          % tracks[0].velocity[0])


def test_two_people_approaching_keep_distinct_ids():
    tracker = PersonTracker()
    tracker.update([FakeDetection(100, 100), FakeDetection(400, 100)])

    for step in range(1, 5):
        tracks = tracker.update([FakeDetection(100 + step * 10, 100),
                                 FakeDetection(400 - step * 10, 100)])

    assert len(tracks) == 2, "expected 2 tracks, got %d" % len(tracks)
    assert {t.track_id for t in tracks} == {1, 2}, "track ids changed"
    print("PASS  two people approaching each other keep distinct ids")


def test_three_plus_tracks():
    tracker = PersonTracker()
    positions = [(50, 50), (250, 50), (450, 50), (50, 300), (450, 300)]

    for step in range(4):
        tracks = tracker.update([FakeDetection(x + step * 2, y)
                                 for x, y in positions])

    assert len(tracks) == 5, "expected 5 tracks, got %d" % len(tracks)
    assert len({t.track_id for t in tracks}) == 5, "track ids collided"
    assert all(t.state == TRACK_CONFIRMED for t in tracks), (
        "all five should be confirmed after 4 frames")
    print("PASS  five simultaneous people are tracked independently")


# -- housekeeping ----------------------------------------------------------

def test_empty_frames_are_safe():
    tracker = PersonTracker()
    assert tracker.update([]) == [], "an empty frame should produce no tracks"
    assert tracker.update(None) == [], "None should be treated as no detections"
    print("PASS  empty and None detection lists are handled safely")


def test_track_reports_center_and_age():
    track = Track(7, (100, 200, 50, 60), frame_index=3, confidence=0.9)
    assert track.center == (125.0, 230.0), "centre is wrong: %r" % (track.center,)
    assert track.first_seen_frame == 3, "first_seen_frame not recorded"
    assert track.last_seen_frame == 3, "last_seen_frame not recorded"
    assert track.age == 0, "a new track should have age 0"
    print("PASS  a track reports centre, age and frame bookkeeping")


def test_reset():
    tracker = PersonTracker()
    tracker.update([FakeDetection(100, 100)])
    tracker.reset()

    assert tracker.track_count == 0, "reset should clear tracks"
    assert tracker.update([FakeDetection(100, 100)])[0].track_id == 1, (
        "reset should restart track numbering")
    print("PASS  reset clears tracks and restarts numbering")


def main():
    enable_safe_console()

    tests = [
        test_iou,
        test_one_detection_creates_one_track,
        test_multiple_detections_create_independent_tracks,
        test_track_id_is_stable_across_frames,
        test_track_becomes_confirmed_after_min_hits,
        test_two_people_stay_separate,
        test_detection_order_does_not_change_association,
        test_creation_order_is_deterministic,
        test_missing_detection_becomes_lost_not_removed,
        test_reacquisition_within_grace_period,
        test_track_expires_after_grace_period,
        test_grace_period_is_configurable,
        test_person_leaves_and_a_new_person_arrives,
        test_low_confidence_continues_but_never_creates,
        test_high_confidence_threshold_is_configurable,
        test_prediction_helps_a_fast_mover,
        test_two_people_approaching_keep_distinct_ids,
        test_three_plus_tracks,
        test_empty_frames_are_safe,
        test_track_reports_center_and_age,
        test_reset,
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
