"""
Synthetic identity tests for ai/track_identity.py (Step 5).

SYNTHETIC TESTS. Recognition results are constructed by hand so identity
behaviour - especially refusing to switch identities - can be asserted
exactly. No model, no camera, no network.

These test identity POLICY, not recognition accuracy.

Run with:
    python tests/test_track_identity.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from face_recognizer import (  # noqa: E402
    STATUS_MATCH,
    STATUS_NO_ENROLLMENTS,
    STATUS_UNKNOWN,
    RecognitionResult,
)
from person_tracker import Track  # noqa: E402
from track_identity import (  # noqa: E402
    IDENTITY_CONFIRMED,
    IDENTITY_NO_ENROLLMENTS,
    IDENTITY_UNKNOWN,
    IdentityManager,
    TrackIdentity,
)
from paths import enable_safe_console  # noqa: E402


def a_track(track_id=1, frame=0, visible=True, reacquired=False):
    track = Track(track_id, (100, 100, 80, 80), frame, 0.95)
    if not visible:
        track.mark_missed()
    track.reacquired = reacquired
    return track


def match(student_id="STU001", name="Safia", similarity=0.82):
    return RecognitionResult(identity=student_id, name=name,
                             similarity=similarity, status=STATUS_MATCH)


def unknown(similarity=0.31):
    return RecognitionResult(status=STATUS_UNKNOWN, similarity=similarity)


def confirm(manager, track, student_id="STU001", similarity=0.82, frame=0):
    """Push a student through to confirmed."""
    for step in range(manager.confirmations_required):
        manager.update(track, match(student_id, similarity=similarity),
                       frame + step)
    return manager.get(track.track_id)


# -- identity confirmation -------------------------------------------------

def test_identity_needs_repeated_evidence():
    manager = IdentityManager(confirmations_required=2)
    track = a_track()

    identity, outcome = manager.update(track, match(), 0)
    assert outcome == "pending", "one result should not confirm, got %r" % outcome
    assert not identity.is_confirmed, "identity confirmed too early"
    assert identity.student_id is None, "an unconfirmed identity must stay None"

    identity, outcome = manager.update(track, match(), 1)
    assert outcome == "confirmed", "two results should confirm, got %r" % outcome
    assert identity.student_id == "STU001", "wrong student attached"
    assert identity.name == "Safia", "display name not attached"
    print("PASS  an identity is confirmed only after repeated evidence")


def test_identity_attaches_to_the_correct_track():
    manager = IdentityManager(confirmations_required=1)
    first, second = a_track(1), a_track(2)

    manager.update(first, match("STU001", "Safia"), 0)
    manager.update(second, match("STU002", "Rahul"), 0)

    assert manager.get(1).student_id == "STU001", "track 1 got the wrong student"
    assert manager.get(2).student_id == "STU002", "track 2 got the wrong student"
    print("PASS  identities attach to their own tracks independently")


def test_unknown_stays_unknown():
    manager = IdentityManager()
    track = a_track()

    for frame in range(5):
        identity, outcome = manager.update(track, unknown(), frame)

    assert outcome == "unknown", "expected unknown, got %r" % outcome
    assert identity.student_id is None, "UNKNOWN must not carry a student"
    assert identity.status == IDENTITY_UNKNOWN, "status should be UNKNOWN"
    print("PASS  repeated non-matches stay UNKNOWN and never invent a student")


def test_no_enrollments_is_distinct_from_unknown():
    manager = IdentityManager()
    identity, outcome = manager.update(
        a_track(), RecognitionResult(status=STATUS_NO_ENROLLMENTS), 0)

    assert outcome == "no_enrollments", "expected no_enrollments, got %r" % outcome
    assert identity.status == IDENTITY_NO_ENROLLMENTS, (
        "an empty gallery should not look like an unrecognised person")
    print("PASS  NO_ENROLLMENTS is kept distinct from UNKNOWN")


def test_alternating_candidates_never_confirm():
    """Flip-flopping recognition must not confirm anybody."""
    manager = IdentityManager(confirmations_required=2)
    track = a_track()

    for frame in range(6):
        student = "STU001" if frame % 2 == 0 else "STU002"
        identity, _ = manager.update(track, match(student), frame)

    assert not identity.is_confirmed, (
        "alternating candidates confirmed %r" % identity.student_id)
    print("PASS  alternating candidates never reach confirmation")


# -- identity-switch protection -------------------------------------------

def test_identity_cannot_switch_immediately():
    """The core safeguard: one contradicting frame must not steal a track."""
    manager = IdentityManager(confirmations_required=2, switch_confirmations=4)
    track = a_track()
    confirm(manager, track, "STU001", similarity=0.82)

    identity, outcome = manager.update(track, match("STU002", "Rahul", 0.95), 5)

    assert outcome == "switch_rejected", (
        "a single contradicting result switched identity, got %r" % outcome)
    assert identity.student_id == "STU001", (
        "identity was stolen by %r" % identity.student_id)
    assert identity.rejected_switches == 1, "the rejection was not counted"
    print("PASS  a single contradicting recognition cannot switch identity")


def test_switch_requires_repeated_and_stronger_evidence():
    manager = IdentityManager(confirmations_required=2, switch_confirmations=4,
                              switch_margin=0.08)
    track = a_track()
    confirm(manager, track, "STU001", similarity=0.82)

    outcomes = []
    for frame in range(4):
        _, outcome = manager.update(track, match("STU002", "Rahul", 0.95),
                                    10 + frame)
        outcomes.append(outcome)

    assert outcomes[:3] == ["switch_rejected"] * 3, (
        "the switch was accepted too early: %s" % outcomes)
    assert outcomes[3] == "switched", (
        "sustained stronger evidence should eventually switch, got %s" % outcomes)
    assert manager.get(1).student_id == "STU002", "the switch did not apply"
    print("PASS  a switch needs %d repeats AND a stronger score" % 4)


def test_switch_refused_without_a_confidence_margin():
    """Repetition alone is not enough - the newcomer must clearly win."""
    manager = IdentityManager(confirmations_required=2, switch_confirmations=3,
                              switch_margin=0.08)
    track = a_track()
    confirm(manager, track, "STU001", similarity=0.90)

    for frame in range(6):
        identity, outcome = manager.update(track, match("STU002", "R", 0.92),
                                           10 + frame)

    assert outcome == "switch_rejected", (
        "a switch was accepted without the margin, got %r" % outcome)
    assert identity.student_id == "STU001", "identity changed without a margin"
    assert identity.rejected_switches >= 6, "rejections were not counted"
    print("PASS  a switch is refused when the score margin is too small")


def test_same_identity_reinforces():
    manager = IdentityManager(confirmations_required=2)
    track = a_track()
    confirm(manager, track, "STU001")

    identity, outcome = manager.update(track, match("STU001", similarity=0.9), 9)

    assert outcome == "unchanged", "expected unchanged, got %r" % outcome
    assert identity.student_id == "STU001", "identity drifted"
    assert identity.rejected_switches == 0, "no rejection should be recorded"
    print("PASS  agreeing recognition reinforces the existing identity")


def test_confirmed_identity_survives_one_weak_frame():
    """A student turning away briefly must not lose their identity."""
    manager = IdentityManager(confirmations_required=2)
    track = a_track()
    confirm(manager, track, "STU001")

    identity, outcome = manager.update(track, unknown(0.2), 9)

    assert outcome == "unchanged", "a weak frame dropped the identity"
    assert identity.student_id == "STU001", "identity lost on one weak frame"
    print("PASS  a confirmed identity survives an isolated weak frame")


def test_malformed_results_are_safe():
    manager = IdentityManager()
    track = a_track()

    identity, outcome = manager.update(track, None, 0)
    assert outcome == "unknown", "None should be handled safely, got %r" % outcome
    assert identity.student_id is None, "None must not set an identity"

    broken = RecognitionResult(identity=None, status=STATUS_MATCH, similarity=0.9)
    identity, outcome = manager.update(track, broken, 1)
    assert identity.student_id is None, (
        "a MATCH with no identity must not set a student")
    print("PASS  malformed recognition results are handled safely")


# -- recognition scheduling -----------------------------------------------

def test_new_track_is_recognized():
    manager = IdentityManager()
    assert manager.should_recognize(a_track(), 0), (
        "a brand new track must be recognised")
    print("PASS  a new track always triggers recognition")


def test_confirmed_track_is_not_recognized_every_frame():
    """The entire performance argument for Step 5."""
    manager = IdentityManager(confirmations_required=1, recognition_interval=30)
    track = a_track()
    manager.update(track, match(), 0)

    asked = sum(1 for frame in range(1, 30)
                if manager.should_recognize(track, frame))

    assert asked == 0, (
        "a settled track asked for recognition %d times in 29 frames" % asked)
    assert manager.should_recognize(track, 30), (
        "recognition should refresh once the interval elapses")
    print("PASS  a confirmed track skips 29 frames, then refreshes at 30")


def test_unconfirmed_track_keeps_asking():
    manager = IdentityManager(confirmations_required=3)
    track = a_track()
    manager.update(track, match(), 0)

    assert manager.should_recognize(track, 1), (
        "an unconfirmed identity should keep asking")
    print("PASS  an unconfirmed track keeps requesting recognition")


def test_reacquired_track_is_recognized_again():
    manager = IdentityManager(confirmations_required=1, recognition_interval=100)
    track = a_track()
    manager.update(track, match(), 0)

    assert not manager.should_recognize(track, 5), "setup failed"

    track.reacquired = True
    assert manager.should_recognize(track, 6), (
        "a reacquired track must be re-checked - it may be a different person")
    print("PASS  a reacquired track is re-recognised immediately")


def test_invisible_track_is_not_recognized():
    manager = IdentityManager()
    assert not manager.should_recognize(a_track(visible=False), 5), (
        "there is no fresh image for an invisible track")
    print("PASS  an invisible track is never sent for recognition")


def test_recognition_interval_is_configurable():
    fast = IdentityManager(confirmations_required=1, recognition_interval=5)
    track = a_track()
    fast.update(track, match(), 0)

    assert not fast.should_recognize(track, 4), "should wait until the interval"
    assert fast.should_recognize(track, 5), "should refresh at interval 5"
    print("PASS  the recognition interval is configurable")


# -- efficiency accounting -------------------------------------------------

def test_efficiency_accounting():
    manager = IdentityManager(confirmations_required=1, recognition_interval=30)
    track = a_track()

    for frame in range(60):
        manager.note_frame(visible_faces=1)
        if manager.should_recognize(track, frame):
            manager.update(track, match(), frame)

    stats = manager.efficiency()

    assert stats["frames_processed"] == 60, "frame count wrong"
    assert stats["naive_recognition_calls"] == 60, "naive count wrong"
    assert stats["recognition_calls"] < 60, (
        "tracking did not reduce recognition calls: %d" % stats["recognition_calls"])
    assert stats["calls_avoided"] == 60 - stats["recognition_calls"], (
        "calls_avoided is inconsistent")
    assert 0 < stats["reduction_percent"] < 100, "reduction percent out of range"
    print("PASS  efficiency accounting is consistent (%d calls in 60 frames, "
          "%.0f%% avoided)" % (stats["recognition_calls"],
                               stats["reduction_percent"]))


def test_efficiency_with_no_frames():
    stats = IdentityManager().efficiency()
    assert stats["calls_per_frame"] == 0.0, "should not divide by zero"
    assert stats["reduction_percent"] == 0.0, "should not divide by zero"
    print("PASS  efficiency accounting handles a zero-frame session")


# -- housekeeping ----------------------------------------------------------

def test_prune_removes_dead_tracks():
    manager = IdentityManager(confirmations_required=1)
    manager.update(a_track(1), match("STU001"), 0)
    manager.update(a_track(2), match("STU002"), 0)

    manager.prune(live_track_ids=[1])

    assert manager.get(1) is not None, "a live track was pruned"
    assert manager.get(2) is None, "a dead track was not pruned"
    print("PASS  prune removes identity state for dead tracks only")


def test_identity_dict_carries_no_vector():
    identity = TrackIdentity(1)
    identity.student_id = "STU001"

    for forbidden in ("embedding", "embeddings", "vector", "features"):
        assert not hasattr(identity, forbidden), (
            "TrackIdentity exposes %r" % forbidden)

    for key, value in identity.to_dict().items():
        assert not isinstance(value, (list, tuple)), (
            "to_dict field %r looks like a vector" % key)
    print("PASS  track identity state carries no biometric vector")


def main():
    enable_safe_console()

    tests = [
        test_identity_needs_repeated_evidence,
        test_identity_attaches_to_the_correct_track,
        test_unknown_stays_unknown,
        test_no_enrollments_is_distinct_from_unknown,
        test_alternating_candidates_never_confirm,
        test_identity_cannot_switch_immediately,
        test_switch_requires_repeated_and_stronger_evidence,
        test_switch_refused_without_a_confidence_margin,
        test_same_identity_reinforces,
        test_confirmed_identity_survives_one_weak_frame,
        test_malformed_results_are_safe,
        test_new_track_is_recognized,
        test_confirmed_track_is_not_recognized_every_frame,
        test_unconfirmed_track_keeps_asking,
        test_reacquired_track_is_recognized_again,
        test_invisible_track_is_not_recognized,
        test_recognition_interval_is_configurable,
        test_efficiency_accounting,
        test_efficiency_with_no_frames,
        test_prune_removes_dead_tracks,
        test_identity_dict_carries_no_vector,
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
