"""
Tests for ai/face_detector.py (Step 2).

None of these need a webcam. They use generated frames, so they run the same
way on any machine and in CI.

They check behaviour and output format, NOT accuracy. Measuring accuracy
honestly needs a labelled dataset, which this project does not have.

Run with:
    python tests/test_face_detector.py
"""

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

import face_detector  # noqa: E402
from paths import enable_safe_console  # noqa: E402
from face_detector import (  # noqa: E402
    Detection,
    YuNetFaceDetector,
    check_frame,
    draw_detections,
)

MODEL_PATH = PROJECT_ROOT / "models" / face_detector.YUNET_MODEL_NAME


def blank_frame(width=640, height=480):
    """A plain black colour frame, the shape a webcam gives us."""
    return np.zeros((height, width, 3), dtype=np.uint8)


def noise_frame(width=640, height=480, seed=1):
    """Random static. Should not contain a face."""
    generator = np.random.default_rng(seed)
    return generator.integers(0, 256, (height, width, 3), dtype=np.uint8)


# --------------------------------------------------------------------------
# Detection value object
# --------------------------------------------------------------------------

def test_detection_geometry():
    detection = Detection(10, 20, 30, 40, 0.95)
    assert detection.right == 40, "right should be x + width"
    assert detection.bottom == 60, "bottom should be y + height"
    assert detection.area == 1200, "area should be width * height"
    assert detection.center == (25, 40), "center is wrong"
    assert detection.as_tuple() == (10, 20, 30, 40), "as_tuple is wrong"
    print("PASS  Detection geometry is correct")


def test_detection_carries_no_identity():
    """Step 2 must not know who anyone is."""
    detection = Detection(0, 0, 10, 10, 0.5)
    for forbidden in ("name", "student_id", "identity", "label", "embedding"):
        assert not hasattr(detection, forbidden), (
            "Detection must not carry identity, found: " + forbidden)
    print("PASS  Detection carries no identity fields")


def test_detection_coerces_types():
    detection = Detection(1.7, 2.2, 3.9, 4.1, "0.5")
    assert isinstance(detection.x, int), "x should be an int"
    assert isinstance(detection.confidence, float), "confidence should be a float"
    print("PASS  Detection converts its inputs to sensible types")


# --------------------------------------------------------------------------
# Frame validation
# --------------------------------------------------------------------------

def test_check_frame_accepts_valid_frame():
    check_frame(blank_frame())
    print("PASS  check_frame accepts a valid frame")


def test_check_frame_rejects_bad_input():
    bad_inputs = [
        (None, "None"),
        ("not a frame", "a string"),
        (np.zeros((480, 640), dtype=np.uint8), "a greyscale 2D array"),
        (np.zeros((0, 640, 3), dtype=np.uint8), "a zero-height frame"),
        (np.zeros((480, 640, 3), dtype=np.float32), "a float frame"),
    ]

    for value, description in bad_inputs:
        try:
            check_frame(value)
        except ValueError:
            continue
        raise AssertionError("check_frame should have rejected " + description)

    print("PASS  check_frame rejects all %d bad inputs" % len(bad_inputs))


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------

def test_detector_initializes():
    detector = YuNetFaceDetector()
    assert detector.model_path.exists(), "model file should exist"
    print("PASS  detector initializes from the downloaded model")


def test_detector_missing_model_raises_clear_error():
    try:
        YuNetFaceDetector(model_path=PROJECT_ROOT / "models" / "does_not_exist.onnx")
    except FileNotFoundError as error:
        assert "download_models" in str(error), (
            "error should tell the user how to fix it")
        print("PASS  missing model raises a clear, actionable error")
        return
    raise AssertionError("a missing model file should raise FileNotFoundError")


def test_blank_frame_finds_no_faces():
    detector = YuNetFaceDetector()
    faces = detector.detect(blank_frame())
    assert isinstance(faces, list), "detect must return a list"
    assert faces == [], "a black frame should contain no faces, got %d" % len(faces)
    print("PASS  blank frame returns an empty list (no crash, no false faces)")


def test_noise_frame_handled_cleanly():
    detector = YuNetFaceDetector()
    faces = detector.detect(noise_frame())
    assert isinstance(faces, list), "detect must return a list even on noise"
    for face in faces:
        assert isinstance(face, Detection), "every item must be a Detection"
    print("PASS  random noise handled cleanly (%d detections)" % len(faces))


def test_detector_rejects_invalid_frame():
    detector = YuNetFaceDetector()
    for bad in (None, np.zeros((480, 640), dtype=np.uint8)):
        try:
            detector.detect(bad)
        except ValueError:
            continue
        raise AssertionError("detect should reject invalid frames")
    print("PASS  detect rejects invalid frames with ValueError")


def test_output_format_and_bounds():
    """
    Whatever is detected must be inside the frame and well formed.

    Uses a low threshold on noise to make detections more likely, so the
    format is actually exercised. If nothing is found, the test still passes
    on the empty-list contract.
    """
    width, height = 640, 480
    detector = YuNetFaceDetector(score_threshold=0.1)
    faces = detector.detect(noise_frame(width, height, seed=7))

    for face in faces:
        assert face.x >= 0 and face.y >= 0, "box starts outside the frame: %r" % face
        assert face.right <= width, "box extends past the right edge: %r" % face
        assert face.bottom <= height, "box extends past the bottom edge: %r" % face
        assert face.width > 0 and face.height > 0, "box has no size: %r" % face
        assert 0.0 <= face.confidence <= 1.0, (
            "confidence must be between 0 and 1, got %r" % face.confidence)

    print("PASS  output format and bounds valid (%d detections checked)" % len(faces))


def test_threshold_is_respected():
    """A higher threshold must never return more faces than a lower one."""
    frame = noise_frame(seed=3)
    low = YuNetFaceDetector(score_threshold=0.1).detect(frame)
    high = YuNetFaceDetector(score_threshold=0.99).detect(frame)
    assert len(high) <= len(low), (
        "raising the threshold increased the face count (%d -> %d)"
        % (len(low), len(high)))
    print("PASS  higher threshold never yields more faces (%d -> %d)"
          % (len(low), len(high)))


def test_detector_handles_changing_frame_size():
    """The model input size must follow the frame size, not go stale."""
    detector = YuNetFaceDetector()
    for width, height in [(640, 480), (1280, 720), (320, 240), (640, 480)]:
        faces = detector.detect(blank_frame(width, height))
        assert isinstance(faces, list), "detect failed at %dx%d" % (width, height)
    print("PASS  detector handles frame size changes")


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def test_draw_with_no_detections_changes_nothing():
    frame = blank_frame()
    draw_detections(frame, [])
    assert frame.sum() == 0, "drawing zero faces should not alter the frame"
    print("PASS  drawing an empty detection list is a no-op")


def test_draw_marks_the_frame():
    frame = blank_frame()
    draw_detections(frame, [Detection(100, 100, 80, 80, 0.93)])
    assert frame.sum() > 0, "drawing a detection should mark the frame"
    assert frame.shape == (480, 640, 3), "drawing must not resize the frame"
    print("PASS  drawing a detection marks the frame without resizing it")


def test_draw_handles_box_at_frame_edge():
    """A face at the very top has no room for a label above it."""
    frame = blank_frame()
    draw_detections(frame, [Detection(0, 0, 60, 60, 0.9)])
    print("PASS  drawing a box at the frame edge does not crash")


def test_draw_without_confidence():
    frame_with = blank_frame()
    frame_without = blank_frame()
    detection = Detection(200, 200, 90, 90, 0.88)
    draw_detections(frame_with, [detection], show_confidence=True)
    draw_detections(frame_without, [detection], show_confidence=False)
    assert frame_with.sum() != frame_without.sum(), (
        "show_confidence=False should draw less than show_confidence=True")
    print("PASS  show_confidence toggles the label")


# --------------------------------------------------------------------------

def main():
    enable_safe_console()
    if not MODEL_PATH.exists():
        print("MODEL MISSING: %s" % face_detector.YUNET_MODEL_NAME)
        print("Run this first:  python ai/download_models.py")
        return 1

    tests = [
        test_detection_geometry,
        test_detection_carries_no_identity,
        test_detection_coerces_types,
        test_check_frame_accepts_valid_frame,
        test_check_frame_rejects_bad_input,
        test_detector_initializes,
        test_detector_missing_model_raises_clear_error,
        test_blank_frame_finds_no_faces,
        test_noise_frame_handled_cleanly,
        test_detector_rejects_invalid_frame,
        test_output_format_and_bounds,
        test_threshold_is_respected,
        test_detector_handles_changing_frame_size,
        test_draw_with_no_detections_changes_nothing,
        test_draw_marks_the_frame,
        test_draw_handles_box_at_frame_edge,
        test_draw_without_confidence,
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
