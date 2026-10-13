"""
ClassVision AI - Face Alignment (Step 3)
----------------------------------------
Turns a detected face into a standard 112x112 image, ready for the embedding
model.

Why alignment is necessary
    An embedding model compares faces by their appearance. If one photo has a
    head tilted 15 degrees and another does not, a large part of the numeric
    difference between the two embeddings is caused by the tilt rather than by
    the person. Aligning every face the same way removes that variation, so
    the embedding describes the face instead of the camera angle.

    Without alignment the same person can look, numerically, like two
    different people.

Which landmarks are used
    YuNet returns five points per face. Their order was confirmed by
    measuring a known photo rather than trusting documentation:

        index 0  left eye           (left as it appears in the image)
        index 1  right eye
        index 2  nose tip
        index 3  left mouth corner
        index 4  right mouth corner

How it works
    Each of those five points is moved onto a fixed reference position using a
    similarity transform - rotation, uniform scale and translation only. No
    stretching or shearing, so the face keeps its proportions.

Target size
    112x112 pixels, BGR, uint8. That is what SFace expects, and the reference
    positions below are the standard ArcFace five-point template defined for
    exactly that size.
"""

import cv2
import numpy as np

# The aligned face size the embedding model expects.
ALIGNED_WIDTH = 112
ALIGNED_HEIGHT = 112
ALIGNED_SIZE = (ALIGNED_WIDTH, ALIGNED_HEIGHT)

# Where each landmark should end up in the aligned 112x112 image.
# This is the standard ArcFace/SFace five-point template. The row order must
# match the YuNet landmark order documented above.
REFERENCE_LANDMARKS = np.array([
    [38.2946, 51.6963],   # left eye
    [73.5318, 51.5014],   # right eye
    [56.0252, 71.7366],   # nose tip
    [41.5493, 92.3655],   # left mouth corner
    [70.7299, 92.2041],   # right mouth corner
], dtype=np.float32)

LANDMARK_NAMES = (
    "left eye", "right eye", "nose tip",
    "left mouth corner", "right mouth corner",
)

EXPECTED_LANDMARK_COUNT = 5


def check_landmarks(landmarks):
    """
    Make sure the landmarks are usable before trying to align with them.

    Raises ValueError with a readable message rather than letting a bad shape
    fail somewhere deep inside OpenCV.
    """
    if landmarks is None:
        raise ValueError("landmarks are None (the detector did not supply them)")

    points = np.asarray(landmarks, dtype=np.float32)

    if points.size != EXPECTED_LANDMARK_COUNT * 2:
        raise ValueError(
            "expected %d landmark points (%d numbers), got %d numbers"
            % (EXPECTED_LANDMARK_COUNT, EXPECTED_LANDMARK_COUNT * 2, points.size))

    points = points.reshape(EXPECTED_LANDMARK_COUNT, 2)

    if not np.all(np.isfinite(points)):
        raise ValueError("landmarks contain NaN or infinite values")

    return points


def landmarks_look_like_a_face(landmarks):
    """
    A cheap sanity check on the geometry of the five points.

    This is not pose estimation. It only catches landmarks that are obviously
    wrong - for example all five points collapsed onto the same spot, or the
    mouth sitting above the eyes. Returns (ok, reason).
    """
    points = check_landmarks(landmarks)

    left_eye, right_eye, nose, left_mouth, right_mouth = points

    eye_distance = float(np.linalg.norm(right_eye - left_eye))
    if eye_distance < 1.0:
        return False, "eyes are in the same place - landmarks are unusable"

    # Eyes should be above the mouth. Image y grows downwards.
    average_eye_y = (left_eye[1] + right_eye[1]) / 2.0
    average_mouth_y = (left_mouth[1] + right_mouth[1]) / 2.0
    if average_mouth_y <= average_eye_y:
        return False, "mouth is not below the eyes - face may be upside down"

    # The nose normally sits between the eye line and the mouth line.
    if not (average_eye_y <= nose[1] <= average_mouth_y):
        return False, "nose is outside the eye-to-mouth range"

    # A face squashed to near zero height is not usable.
    if (average_mouth_y - average_eye_y) < eye_distance * 0.2:
        return False, "face geometry looks flattened or distorted"

    return True, "landmark geometry looks like a face"


def estimate_alignment_transform(landmarks):
    """
    Work out the 2x3 similarity transform that maps the landmarks onto the
    reference template.
    """
    points = check_landmarks(landmarks)

    # estimateAffinePartial2D gives rotation + uniform scale + translation.
    # The full affine version would also allow shearing, which would distort
    # the face and make embeddings less comparable.
    matrix, _ = cv2.estimateAffinePartial2D(
        points, REFERENCE_LANDMARKS, method=cv2.LMEDS)

    if matrix is None:
        raise ValueError("could not compute an alignment transform from these landmarks")

    return matrix


def align_face(image, landmarks):
    """
    Produce the aligned 112x112 face image.

    image     - the full frame the face was detected in (BGR, uint8)
    landmarks - the five points from the detector

    Returns a new 112x112 BGR image. The input frame is not modified.
    """
    if image is None:
        raise ValueError("image is None")

    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("image must be a colour frame with shape (h, w, 3)")

    matrix = estimate_alignment_transform(landmarks)

    return cv2.warpAffine(
        image, matrix, ALIGNED_SIZE,
        flags=cv2.INTER_LINEAR,
        borderValue=(0, 0, 0),
    )


def align_detection(image, detection):
    """Convenience wrapper: align straight from a Detection object."""
    return align_face(image, detection.landmarks)
