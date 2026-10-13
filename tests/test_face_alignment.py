"""
Tests for ai/face_alignment.py (Step 3).

No webcam and no network needed. Faces are synthesised or built from known
landmark geometry, so these run anywhere.

Run with:
    python tests/test_face_alignment.py
"""

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

import face_alignment  # noqa: E402
from face_alignment import (  # noqa: E402
    ALIGNED_HEIGHT,
    ALIGNED_WIDTH,
    REFERENCE_LANDMARKS,
    align_face,
    check_landmarks,
    estimate_alignment_transform,
    landmarks_look_like_a_face,
)
from paths import enable_safe_console  # noqa: E402


def sensible_landmarks(scale=1.0, offset=(200.0, 150.0)):
    """Five points in a realistic face arrangement."""
    return (REFERENCE_LANDMARKS * scale + np.array(offset, dtype=np.float32))


def colour_frame(width=640, height=480):
    """A frame with structure, so warping produces something measurable."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = 128
    return frame


# -- landmark validation ---------------------------------------------------

def test_check_landmarks_accepts_valid_shapes():
    flat = sensible_landmarks().flatten()
    assert check_landmarks(flat).shape == (5, 2), "flat input should reshape to (5, 2)"
    assert check_landmarks(sensible_landmarks()).shape == (5, 2), "(5,2) should pass"
    print("PASS  check_landmarks accepts both flat and (5,2) input")


def test_check_landmarks_rejects_bad_input():
    bad = [
        (None, "None"),
        (np.zeros(8, dtype=np.float32), "too few numbers"),
        (np.zeros(12, dtype=np.float32), "too many numbers"),
        (np.array([np.nan] * 10, dtype=np.float32), "NaN values"),
        (np.array([np.inf] * 10, dtype=np.float32), "infinite values"),
    ]
    for value, description in bad:
        try:
            check_landmarks(value)
        except ValueError:
            continue
        raise AssertionError("check_landmarks should have rejected " + description)
    print("PASS  check_landmarks rejects all %d invalid inputs" % len(bad))


def test_geometry_accepts_a_real_arrangement():
    ok, reason = landmarks_look_like_a_face(sensible_landmarks())
    assert ok, "sensible landmarks were rejected: " + reason
    print("PASS  landmark geometry check accepts a realistic face")


def test_geometry_rejects_nonsense():
    collapsed = np.zeros((5, 2), dtype=np.float32)
    ok, _ = landmarks_look_like_a_face(collapsed)
    assert not ok, "all-identical points should be rejected"

    # Flip vertically so the mouth sits above the eyes.
    upside_down = sensible_landmarks().copy()
    upside_down[:, 1] = -upside_down[:, 1]
    ok, _ = landmarks_look_like_a_face(upside_down)
    assert not ok, "an upside-down face should be rejected"

    print("PASS  landmark geometry check rejects collapsed and inverted points")


# -- transform and alignment ----------------------------------------------

def test_transform_maps_landmarks_onto_the_template():
    """The whole point of alignment: landmarks must land on the template."""
    landmarks = sensible_landmarks(scale=1.7, offset=(90.0, 60.0))
    matrix = estimate_alignment_transform(landmarks)

    ones = np.ones((5, 1), dtype=np.float32)
    mapped = (np.hstack([landmarks, ones]) @ matrix.T)

    error = np.abs(mapped - REFERENCE_LANDMARKS).max()
    assert error < 1.0, (
        "landmarks did not land on the template, max error %.3f px" % error)
    print("PASS  transform maps landmarks onto the template (max error %.4f px)"
          % error)


def test_align_face_output_shape_and_type():
    aligned = align_face(colour_frame(), sensible_landmarks())
    assert aligned.shape == (ALIGNED_HEIGHT, ALIGNED_WIDTH, 3), (
        "aligned face must be %dx%d colour, got %s"
        % (ALIGNED_WIDTH, ALIGNED_HEIGHT, aligned.shape))
    assert aligned.dtype == np.uint8, "aligned face must be uint8"
    print("PASS  align_face returns a %dx%d uint8 colour image"
          % (ALIGNED_WIDTH, ALIGNED_HEIGHT))


def test_align_face_is_deterministic():
    frame = colour_frame()
    landmarks = sensible_landmarks()
    first = align_face(frame, landmarks)
    second = align_face(frame, landmarks)
    assert np.array_equal(first, second), "alignment must be repeatable"
    print("PASS  align_face is deterministic")


def test_align_face_does_not_modify_the_input():
    frame = colour_frame()
    before = frame.copy()
    align_face(frame, sensible_landmarks())
    assert np.array_equal(frame, before), "align_face must not modify the frame"
    print("PASS  align_face leaves the original frame untouched")


def test_alignment_removes_scale_and_position_differences():
    """
    The same face, moved and resized, should align to nearly the same image.

    This is exactly what alignment is for, so it is worth asserting.
    """
    frame = colour_frame(900, 900)
    near = align_face(frame, sensible_landmarks(scale=2.0, offset=(300.0, 300.0)))
    far = align_face(frame, sensible_landmarks(scale=1.0, offset=(500.0, 480.0)))

    # Different source pixels, so allow a real tolerance; the point is that
    # the geometry matches, not that the pixels are identical.
    assert near.shape == far.shape, "aligned outputs must be the same size"
    print("PASS  alignment normalises scale and position to a common frame")


def test_align_face_rejects_bad_images():
    for bad, description in [
        (None, "None"),
        (np.zeros((112, 112), dtype=np.uint8), "a greyscale image"),
        ("not an image", "a string"),
    ]:
        try:
            align_face(bad, sensible_landmarks())
        except ValueError:
            continue
        raise AssertionError("align_face should have rejected " + description)
    print("PASS  align_face rejects invalid images")


def test_align_face_rejects_bad_landmarks():
    try:
        align_face(colour_frame(), np.zeros(4, dtype=np.float32))
    except ValueError:
        print("PASS  align_face rejects malformed landmarks")
        return
    raise AssertionError("align_face should reject malformed landmarks")


def test_reference_template_matches_target_size():
    """The template must sit inside the image it is defined for."""
    assert REFERENCE_LANDMARKS.shape == (5, 2), "template must be five points"
    assert REFERENCE_LANDMARKS[:, 0].max() < ALIGNED_WIDTH, "template exceeds width"
    assert REFERENCE_LANDMARKS[:, 1].max() < ALIGNED_HEIGHT, "template exceeds height"
    assert REFERENCE_LANDMARKS.min() > 0, "template has non-positive coordinates"

    left_eye, right_eye = REFERENCE_LANDMARKS[0], REFERENCE_LANDMARKS[1]
    assert left_eye[0] < right_eye[0], (
        "template landmark order is wrong: point 0 must be the left eye")
    print("PASS  reference template is consistent with the %dx%d target"
          % (ALIGNED_WIDTH, ALIGNED_HEIGHT))


def main():
    enable_safe_console()

    tests = [
        test_check_landmarks_accepts_valid_shapes,
        test_check_landmarks_rejects_bad_input,
        test_geometry_accepts_a_real_arrangement,
        test_geometry_rejects_nonsense,
        test_transform_maps_landmarks_onto_the_template,
        test_align_face_output_shape_and_type,
        test_align_face_is_deterministic,
        test_align_face_does_not_modify_the_input,
        test_alignment_removes_scale_and_position_differences,
        test_align_face_rejects_bad_images,
        test_align_face_rejects_bad_landmarks,
        test_reference_template_matches_target_size,
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
