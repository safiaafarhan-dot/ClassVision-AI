"""
ClassVision AI - Face Detection (Step 2)
----------------------------------------
Answers one question only:

    "Where are the faces in this frame?"

It does NOT answer "who is this person?". There is no recognition, no
identity, no embeddings, and no attendance here. Those come in later steps.

Why YuNet
    OpenCV 5.0 REMOVED cv2.CascadeClassifier, so the classic Haar cascade
    approach is not available in this environment at all. YuNet is built into
    the OpenCV we already have, needs no extra dependency, and its model file
    is only 227 KB. It reports a confidence score per face and copes with
    small and slightly angled faces far better than Haar did, which matters
    for a classroom full of people sitting at a distance.

Structure
    BaseFaceDetector defines the interface. Everything else in the project
    should depend on that interface, not on YuNet directly, so the detector
    can be swapped later without rewriting callers.

Usage
    from face_detector import YuNetFaceDetector, draw_detections

    detector = YuNetFaceDetector()
    faces = detector.detect(frame)        # list of Detection, possibly empty
    draw_detections(frame, faces)
"""

from abc import ABC, abstractmethod

import cv2
import numpy as np

from paths import MODELS_DIR, relative

YUNET_MODEL_NAME = "face_detection_yunet_2023mar.onnx"

# A face is reported only if the model is at least this confident.
# Lower  -> finds more distant/angled faces, but more false positives.
# Higher -> fewer false positives, but misses harder faces.
#
# Measured on three OpenCV sample photos plus three non-face control images
# (see README, "Threshold tuning"). Non-face images produced zero detections
# at every threshold tested, while multi-person photos needed 0.5 or lower to
# pick up the smaller background faces. 0.6 is a middle setting; a real
# classroom should be re-tuned on real classroom footage.
DEFAULT_SCORE_THRESHOLD = 0.6

# Non-maximum suppression: merges overlapping boxes for the same face.
DEFAULT_NMS_THRESHOLD = 0.3

# Safety cap on how many candidate boxes are considered before filtering.
DEFAULT_TOP_K = 5000

# Colours in OpenCV are (Blue, Green, Red), not RGB.
BOX_COLOR = (0, 255, 170)
TEXT_COLOR = (0, 0, 0)


class Detection:
    """
    One detected face: a box plus how confident the model is.

    Deliberately carries no name, no ID, and no identity of any kind.
    """

    __slots__ = ("x", "y", "width", "height", "confidence", "landmarks")

    def __init__(self, x, y, width, height, confidence, landmarks=None):
        self.x = int(x)
        self.y = int(y)
        self.width = int(width)
        self.height = int(height)
        self.confidence = float(confidence)

        # Five (x, y) points: both eyes, the nose tip, and both mouth corners.
        # Step 2 does not use them; Step 3 needs them to align faces before
        # producing an embedding. None when the detector did not supply them.
        self.landmarks = None if landmarks is None else np.asarray(
            landmarks, dtype=np.float32).reshape(5, 2)

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def area(self):
        return self.width * self.height

    @property
    def center(self):
        return (self.x + self.width // 2, self.y + self.height // 2)

    def as_tuple(self):
        """(x, y, width, height) - the format most OpenCV calls expect."""
        return (self.x, self.y, self.width, self.height)

    def __repr__(self):
        return "Detection(x=%d, y=%d, w=%d, h=%d, confidence=%.3f)" % (
            self.x, self.y, self.width, self.height, self.confidence
        )


def check_frame(frame):
    """
    Make sure we were handed a real camera frame.

    Raises ValueError with a clear message instead of letting OpenCV fail
    deep inside the C++ layer with something unreadable.
    """
    if frame is None:
        raise ValueError("frame is None (the camera read probably failed)")

    if not isinstance(frame, np.ndarray):
        raise ValueError("frame must be a numpy array, got %s" % type(frame).__name__)

    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(
            "frame must be a colour image with shape (height, width, 3), got %s"
            % (frame.shape,)
        )

    if frame.shape[0] == 0 or frame.shape[1] == 0:
        raise ValueError("frame is empty (zero width or height)")

    if frame.dtype != np.uint8:
        raise ValueError("frame must have dtype uint8, got %s" % frame.dtype)


class BaseFaceDetector(ABC):
    """
    The interface every face detector in this project must provide.

    Depend on this, not on a specific model, so the detector can be replaced
    later without changing the code that uses it.
    """

    @abstractmethod
    def detect(self, frame):
        """Return a list of Detection objects. An empty list means no faces."""

    def __call__(self, frame):
        return self.detect(frame)


class YuNetFaceDetector(BaseFaceDetector):
    """Face detector backed by OpenCV's built-in YuNet model."""

    def __init__(self, model_path=None, score_threshold=DEFAULT_SCORE_THRESHOLD,
                 nms_threshold=DEFAULT_NMS_THRESHOLD, top_k=DEFAULT_TOP_K):
        self.model_path = model_path or (MODELS_DIR / YUNET_MODEL_NAME)
        self.score_threshold = float(score_threshold)

        if not self.model_path.exists():
            raise FileNotFoundError(
                "Face detection model not found at %s\n"
                "Download it once with:  python ai/download_models.py"
                % relative(self.model_path)
            )

        # Load the model from BYTES, not from a path.
        #
        # OpenCV's C++ file reader cannot open paths containing non-English
        # characters on Windows, and this project lives under a folder named
        # "文档". Passing the path directly fails with "Can't read ONNX file"
        # even though the file is perfectly valid. Reading the bytes in Python
        # sidesteps the problem completely, on every platform.
        model_bytes = np.frombuffer(self.model_path.read_bytes(), dtype=np.uint8)
        empty_config = np.empty(0, dtype=np.uint8)

        # Building the model prints a harmless "Targets are not supported by
        # the new graph engine" warning. Keep it out of the project's output.
        previous_log_level = cv2.utils.logging.getLogLevel()
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        try:
            # The input size given here is a placeholder. It must match the
            # real frame size, so detect() updates it when the size changes.
            self._detector = cv2.FaceDetectorYN.create(
                "onnx",
                model_bytes,
                empty_config,
                (320, 320),
                self.score_threshold,
                float(nms_threshold),
                int(top_k),
            )
        finally:
            cv2.utils.logging.setLogLevel(previous_log_level)

        self._input_size = (320, 320)

    def detect(self, frame):
        """Find faces in one frame. Returns [] when there are none."""
        check_frame(frame)

        height, width = frame.shape[:2]

        # Telling the model the wrong frame size produces badly placed boxes,
        # so keep it in sync. Only call the setter when it actually changes.
        if self._input_size != (width, height):
            self._detector.setInputSize((width, height))
            self._input_size = (width, height)

        _, raw_faces = self._detector.detect(frame)

        # YuNet returns None rather than an empty array when nothing is found.
        if raw_faces is None or len(raw_faces) == 0:
            return []

        detections = []
        for face in raw_faces:
            # Each row is [x, y, w, h, 10 landmark values, score].
            # The 10 landmark values are five (x, y) points. Step 2 ignores
            # them; Step 3 uses them to align the face before embedding it.
            x, y, box_width, box_height = face[0:4]
            landmarks = face[4:14]
            confidence = face[-1]

            # The model can return boxes that hang slightly off the edge of
            # the frame. Clamp them so later code can crop safely.
            x = max(0, int(round(float(x))))
            y = max(0, int(round(float(y))))
            box_width = int(round(float(box_width)))
            box_height = int(round(float(box_height)))
            box_width = min(box_width, width - x)
            box_height = min(box_height, height - y)

            if box_width <= 0 or box_height <= 0:
                continue

            # Landmarks are NOT clamped to the frame. A face at the edge can
            # legitimately have a landmark just outside it, and alignment
            # copes with that better than a silently moved point would.
            detections.append(
                Detection(x, y, box_width, box_height, confidence, landmarks))

        return detections


def draw_detections(frame, detections, show_confidence=True, color=BOX_COLOR):
    """
    Draw a box around each detected face.

    No names are drawn, because Step 2 does not know who anyone is.
    Returns the same frame, modified in place.
    """
    for detection in detections:
        cv2.rectangle(
            frame,
            (detection.x, detection.y),
            (detection.right, detection.bottom),
            color,
            2,
        )

        if not show_confidence:
            continue

        label = "%.0f%%" % (detection.confidence * 100)
        font = cv2.FONT_HERSHEY_SIMPLEX
        (text_width, text_height), _ = cv2.getTextSize(label, font, 0.5, 1)

        # Put the label just above the box, or inside it if the box is at the
        # very top of the frame and there is no room above.
        label_bottom = detection.y - 4
        if label_bottom - text_height < 0:
            label_bottom = detection.y + text_height + 8

        cv2.rectangle(
            frame,
            (detection.x, label_bottom - text_height - 4),
            (detection.x + text_width + 8, label_bottom + 2),
            color,
            -1,
        )
        cv2.putText(
            frame, label, (detection.x + 4, label_bottom - 2),
            font, 0.5, TEXT_COLOR, 1, cv2.LINE_AA,
        )

    return frame
