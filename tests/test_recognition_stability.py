"""
Tests for ai/recognition_stability.py (Step 4).

No webcam, no model, no network. Results are fed in by hand so the
confirmation behaviour can be asserted frame by frame.

This module is a placeholder for real tracking in Step 5. These tests pin down
what it promises today, including where it is deliberately weak.

Run with:
    python tests/test_recognition_stability.py
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
from recognition_stability import (  # noqa: E402
    StabilisedIdentity,
    StabilitySet,
    box_center,
)
from paths import enable_safe_console  # noqa: E402

FRAME_WIDTH = 640


def match(identity="S001", name="Rahul", similarity=0.9, box=(100, 100, 80, 80)):
    return RecognitionResult(identity=identity, name=name, similarity=similarity,
                             status=STATUS_MATCH, box=box)


def unknown(similarity=0.3, box=(100, 100, 80, 80)):
    return RecognitionResult(status=STATUS_UNKNOWN, similarity=similarity, box=box)


# -- single slot -----------------------------------------------------------

def test_confirmation_requires_consecutive_frames():
    slot = StabilisedIdentity(frames_to_confirm=3)

    first = slot.update(match(similarity=0.81))
    second = slot.update(match(similarity=0.88))
    assert first[2] == STATUS_UNKNOWN, "one frame should not confirm"
    assert second[2] == STATUS_UNKNOWN, "two frames should not confirm"

    third = slot.update(match(similarity=0.91))
    assert third[2] == STATUS_MATCH, "three agreeing frames should confirm"
    assert third[0] == "S001", "the confirmed identity is wrong"

    fourth = slot.update(match(similarity=0.92))
    assert fourth[2] == STATUS_MATCH, "confirmation should persist"
    print("PASS  identity confirms only after 3 consecutive agreeing frames")


def test_confirmation_count_is_configurable():
    slot = StabilisedIdentity(frames_to_confirm=1)
    assert slot.update(match())[2] == STATUS_MATCH, (
        "with frames_to_confirm=1 a single frame should confirm")

    slow = StabilisedIdentity(frames_to_confirm=5)
    for index in range(4):
        assert slow.update(match())[2] != STATUS_MATCH, (
            "confirmed too early at frame %d" % (index + 1))
    assert slow.update(match())[2] == STATUS_MATCH, "should confirm at frame 5"
    print("PASS  the confirmation count is configurable (1 and 5 both work)")


def test_disagreement_resets_the_run():
    slot = StabilisedIdentity(frames_to_confirm=3)
    slot.update(match(identity="S001"))
    slot.update(match(identity="S001"))
    slot.update(match(identity="S002", name="Priya"))   # a different person
    status = slot.update(match(identity="S001"))[2]

    assert status == STATUS_UNKNOWN, (
        "a disagreeing frame should reset the run, not confirm")
    print("PASS  a disagreeing frame resets the confirmation run")


def test_single_unknown_frame_does_not_drop_a_confirmed_identity():
    """Recognition flickers; one bad frame should not erase a settled name."""
    slot = StabilisedIdentity(frames_to_confirm=3)
    for _ in range(3):
        slot.update(match())
    assert slot.is_confirmed, "test setup failed: identity not confirmed"

    after_one_miss = slot.update(unknown())
    assert after_one_miss[2] == STATUS_MATCH, (
        "a single unknown frame should not drop the identity")
    print("PASS  one unknown frame does not drop a confirmed identity")


def test_sustained_unknown_drops_the_identity():
    slot = StabilisedIdentity(frames_to_confirm=3)
    for _ in range(3):
        slot.update(match())

    statuses = [slot.update(unknown())[2] for _ in range(3)]
    assert statuses[-1] == STATUS_UNKNOWN, (
        "a full window of unknown frames should drop the identity, got %s"
        % statuses)
    print("PASS  a sustained run of unknown frames does drop the identity")


def test_score_smoothing():
    slot = StabilisedIdentity(frames_to_confirm=1, smoothing_window=4)
    for score in (0.80, 0.90, 1.00, 0.90):
        result = slot.update(match(similarity=score))

    expected = (0.80 + 0.90 + 1.00 + 0.90) / 4.0
    assert abs(result[3] - expected) < 1e-6, (
        "smoothed score should be %.4f, got %.4f" % (expected, result[3]))
    print("PASS  the displayed score is smoothed over recent frames (%.4f)"
          % result[3])


def test_no_enrollments_passes_through():
    slot = StabilisedIdentity(frames_to_confirm=2)
    result = slot.update(RecognitionResult(status=STATUS_NO_ENROLLMENTS,
                                           box=(0, 0, 10, 10)))
    assert result[2] == STATUS_NO_ENROLLMENTS, (
        "NO_ENROLLMENTS should not be reported as UNKNOWN")
    print("PASS  NO_ENROLLMENTS passes through the stability filter unchanged")


# -- multiple faces --------------------------------------------------------

def test_two_faces_get_separate_slots():
    stability = StabilitySet(frames_to_confirm=2)

    left = match(identity="S001", name="Rahul", box=(50, 100, 80, 80))
    right = match(identity="S002", name="Priya", box=(450, 100, 80, 80))

    stability.update([left, right], FRAME_WIDTH)
    output = stability.update([left, right], FRAME_WIDTH)

    assert stability.slot_count == 2, (
        "two separated faces should get two slots, got %d" % stability.slot_count)
    assert output[0][0] == "S001", "the left face resolved to %r" % output[0][0]
    assert output[1][0] == "S002", "the right face resolved to %r" % output[1][0]
    print("PASS  two faces are confirmed independently in separate slots")


def test_one_face_does_not_leak_into_another():
    """A confirmed identity must not be handed to a different face."""
    stability = StabilitySet(frames_to_confirm=2)

    known = match(identity="S001", box=(50, 100, 80, 80))
    for _ in range(3):
        stability.update([known], FRAME_WIDTH)

    stranger = unknown(box=(500, 300, 80, 80))
    output = stability.update([known, stranger], FRAME_WIDTH)

    assert output[0][0] == "S001", "the known face lost its identity"
    assert output[1][0] is None, (
        "the stranger inherited an identity: %r" % (output[1][0],))
    print("PASS  a confirmed identity does not leak into another face's slot")


def test_slot_follows_a_moving_face():
    stability = StabilitySet(frames_to_confirm=2)

    for x in (100, 120, 140, 160):
        output = stability.update(
            [match(identity="S001", box=(x, 100, 80, 80))], FRAME_WIDTH)

    assert stability.slot_count == 1, (
        "a face moving slowly should keep one slot, got %d" % stability.slot_count)
    assert output[0][2] == STATUS_MATCH, "the moving face should stay confirmed"
    print("PASS  one slot follows a face moving gradually across the frame")


def test_distant_face_gets_a_new_slot():
    stability = StabilitySet(frames_to_confirm=2, match_radius_fraction=0.1)

    stability.update([match(box=(10, 100, 80, 80))], FRAME_WIDTH)
    stability.update([match(box=(600, 100, 80, 80))], FRAME_WIDTH)

    assert stability.slot_count == 2, (
        "a face appearing far away should get a new slot, got %d"
        % stability.slot_count)
    print("PASS  a face far from any known slot starts a new one")


def test_slots_expire():
    stability = StabilitySet(frames_to_confirm=1, timeout_seconds=1.0)

    stability.update([match()], FRAME_WIDTH, now=1000.0)
    assert stability.slot_count == 1, "test setup failed"

    stability.expire(now=1000.5)
    assert stability.slot_count == 1, "a recent slot should survive"

    stability.expire(now=1002.0)
    assert stability.slot_count == 0, "a stale slot should be forgotten"
    print("PASS  slots expire after the timeout")


def test_reset_clears_everything():
    stability = StabilitySet(frames_to_confirm=1)
    stability.update([match(), match(box=(400, 100, 80, 80))], FRAME_WIDTH)
    stability.reset()
    assert stability.slot_count == 0, "reset should clear all slots"
    print("PASS  reset clears all slots")


def test_empty_frame_is_safe():
    stability = StabilitySet()
    assert stability.update([], FRAME_WIDTH) == [], (
        "a frame with no faces should produce no results")
    print("PASS  a frame with no faces is handled safely")


def test_result_without_a_box_is_safe():
    stability = StabilitySet(frames_to_confirm=1)
    result = RecognitionResult(identity="S001", name="R", similarity=0.9,
                               status=STATUS_MATCH, box=None)
    output = stability.update([result], FRAME_WIDTH)
    assert len(output) == 1, "a boxless result should still produce output"
    print("PASS  a result with no box does not crash the stability filter")


def test_box_center():
    assert box_center((0, 0, 100, 50)) == (50.0, 25.0), "box centre is wrong"
    print("PASS  box centre is computed correctly")


def main():
    enable_safe_console()

    tests = [
        test_confirmation_requires_consecutive_frames,
        test_confirmation_count_is_configurable,
        test_disagreement_resets_the_run,
        test_single_unknown_frame_does_not_drop_a_confirmed_identity,
        test_sustained_unknown_drops_the_identity,
        test_score_smoothing,
        test_no_enrollments_passes_through,
        test_two_faces_get_separate_slots,
        test_one_face_does_not_leak_into_another,
        test_slot_follows_a_moving_face,
        test_distant_face_gets_a_new_slot,
        test_slots_expire,
        test_reset_clears_everything,
        test_empty_frame_is_safe,
        test_result_without_a_box_is_safe,
        test_box_center,
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
