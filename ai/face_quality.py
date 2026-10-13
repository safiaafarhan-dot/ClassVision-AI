"""
ClassVision AI - Enrollment Quality Checks (Step 3)
---------------------------------------------------
Decides whether a detected face is good enough to enroll.

Enrollment is the one moment where a bad sample causes lasting damage: a
blurry or badly angled face becomes the stored reference that every future
comparison is measured against. It is far better to reject a sample and ask
the student to try again than to store a weak one.

Every check returns a short reason the person in front of the camera can act
on - "move closer", not "quality score 0.34".

These checks are deliberately simple and explainable. They are NOT liveness or
anti-spoofing checks; a photograph held up to the camera would pass. Liveness
belongs to a later hardening step.
"""

from face_alignment import landmarks_look_like_a_face

# A face smaller than this is too low-detail to make a reliable reference.
# 80 px across at 640x480 means the student is reasonably close to the camera.
MIN_FACE_WIDTH = 80
MIN_FACE_HEIGHT = 80

# Enrollment demands more certainty than live detection does, so this sits
# above the detector's own default of 0.6.
MIN_ENROLLMENT_CONFIDENCE = 0.85

# A real face is roughly as tall as it is wide, within reason. A wildly
# different ratio usually means a bad box rather than an unusual face.
MIN_ASPECT_RATIO = 0.5
MAX_ASPECT_RATIO = 2.0

# Faces touching the frame edge are usually partly outside it.
EDGE_MARGIN = 2


class QualityResult:
    """The outcome of checking one face, with a message worth showing."""

    __slots__ = ("ok", "reason")

    def __init__(self, ok, reason):
        self.ok = bool(ok)
        self.reason = reason

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "QualityResult(ok=%r, reason=%r)" % (self.ok, self.reason)


def check_single_face(detections):
    """
    Enrollment needs exactly one face: zero is nothing to enroll, and more
    than one makes it ambiguous whose identity is being stored.
    """
    count = len(detections)

    if count == 0:
        return QualityResult(False, "No face detected - look at the camera.")

    if count > 1:
        return QualityResult(
            False,
            "Multiple faces detected (%d) - only one person should be visible."
            % count)

    return QualityResult(True, "One face detected.")


def check_face(detection, frame_width, frame_height):
    """
    Check a single detected face against every enrollment rule.

    Returns the first failure found, so the student is given one clear thing
    to fix rather than a list.
    """
    if detection.width <= 0 or detection.height <= 0:
        return QualityResult(False, "Invalid face box - please try again.")

    if detection.confidence < MIN_ENROLLMENT_CONFIDENCE:
        return QualityResult(
            False,
            "Face confidence too low (%.0f%%) - improve lighting and face the camera."
            % (detection.confidence * 100))

    if detection.width < MIN_FACE_WIDTH or detection.height < MIN_FACE_HEIGHT:
        return QualityResult(
            False,
            "Face too small (%dx%d, need %dx%d) - move closer to the camera."
            % (detection.width, detection.height, MIN_FACE_WIDTH, MIN_FACE_HEIGHT))

    aspect_ratio = detection.height / float(detection.width)
    if not (MIN_ASPECT_RATIO <= aspect_ratio <= MAX_ASPECT_RATIO):
        return QualityResult(
            False, "Face box looks distorted - please try again.")

    touches_edge = (
        detection.x <= EDGE_MARGIN
        or detection.y <= EDGE_MARGIN
        or detection.right >= frame_width - EDGE_MARGIN
        or detection.bottom >= frame_height - EDGE_MARGIN
    )
    if touches_edge:
        return QualityResult(
            False, "Face is at the edge of the frame - center yourself.")

    if detection.landmarks is None:
        return QualityResult(
            False, "Facial landmarks missing - please try again.")

    geometry_ok, geometry_reason = landmarks_look_like_a_face(detection.landmarks)
    if not geometry_ok:
        return QualityResult(False, "Face not clearly visible - %s." % geometry_reason)

    return QualityResult(True, "Face quality is good.")


def check_frame_for_enrollment(detections, frame_width, frame_height):
    """
    The whole check for one frame: exactly one face, and that face usable.

    Returns (QualityResult, detection or None).
    """
    single = check_single_face(detections)
    if not single.ok:
        return single, None

    detection = detections[0]
    result = check_face(detection, frame_width, frame_height)

    return result, (detection if result.ok else None)
