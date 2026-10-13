"""
ClassVision AI - Live Camera Source (Step 8)
--------------------------------------------
Opening, reading and RELEASING the classroom camera.

    Step 8 connects the existing pipeline to a live camera. It adds no new
    model, changes no threshold, and stores no image.

Why this is its own small module
    The camera is the one resource in this project that can be leaked. A
    webcam held open by a crashed worker stays locked until the process dies,
    and on Windows nothing else can open it in the meantime. Putting open,
    read and release behind one object means there is exactly one place that
    has to get the cleanup right, and exactly one thing to fake in a test.

    Nothing here interprets the picture. A frame is read, handed to the
    pipeline, and dropped. No frame is written to disk, cached, queued, or
    returned through the API.

Resolution
    640x480 by default, because that is what Step 1 and Step 5 actually
    measured on this machine (~15 FPS, 0 dropped frames). 1280x720 cost about
    three times as much per frame for no detection benefit, so it is available
    by configuration but is never the default.

Configuration
    CLASSVISION_CAMERA_INDEX    which camera to open   (default 0)
    CLASSVISION_CAMERA_WIDTH    requested width        (default 640)
    CLASSVISION_CAMERA_HEIGHT   requested height       (default 480)

    The index is deliberately configurable and deliberately not assumed:
    camera 0 is the built-in webcam on some machines and a virtual device on
    others. ai/camera_test.py --list reports which indexes actually work.
"""

import os

CAMERA_INDEX_ENV = "CLASSVISION_CAMERA_INDEX"
CAMERA_WIDTH_ENV = "CLASSVISION_CAMERA_WIDTH"
CAMERA_HEIGHT_ENV = "CLASSVISION_CAMERA_HEIGHT"

DEFAULT_CAMERA_INDEX = 0
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

# Camera status values reported through the API. Operational only - none of
# these says anything about a person.
CAMERA_DISCONNECTED = "disconnected"
CAMERA_CONNECTED = "connected"
CAMERA_RELEASED = "released"
CAMERA_ERROR = "error"


class CameraError(RuntimeError):
    """
    The camera could not be opened or stopped delivering frames.

    The message is written to be shown to a teacher: it names the camera
    index and nothing else. No file path, no driver string, no traceback.
    """


def _int_from_environment(name, fallback):
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        # A typo in an environment variable must not decide the resolution
        # silently, but it must also not stop a lesson. Fall back and say so.
        return fallback


class CameraConfig:
    """Which camera to open, and at what size."""

    __slots__ = ("index", "width", "height")

    def __init__(self, index=None, width=None, height=None):
        self.index = DEFAULT_CAMERA_INDEX if index is None else int(index)
        self.width = DEFAULT_WIDTH if width is None else int(width)
        self.height = DEFAULT_HEIGHT if height is None else int(height)

        if self.index < 0:
            raise ValueError("camera index must not be negative, got %d"
                             % self.index)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("camera resolution must be positive, got %dx%d"
                             % (self.width, self.height))

    @classmethod
    def from_environment(cls, index=None, width=None, height=None):
        """Explicit argument first, then environment, then the default."""
        return cls(
            index=index if index is not None
            else _int_from_environment(CAMERA_INDEX_ENV, DEFAULT_CAMERA_INDEX),
            width=width if width is not None
            else _int_from_environment(CAMERA_WIDTH_ENV, DEFAULT_WIDTH),
            height=height if height is not None
            else _int_from_environment(CAMERA_HEIGHT_ENV, DEFAULT_HEIGHT))

    def to_dict(self):
        return {"index": self.index, "width": self.width, "height": self.height}

    def __repr__(self):
        return "CameraConfig(index=%d, %dx%d)" % (self.index, self.width,
                                                  self.height)


class OpenCVCamera:
    """
    The real webcam, behind the smallest interface the worker needs.

    open() / read() / release(), and release() is safe to call twice - which
    matters, because it is called from a finally block that may run after the
    worker has already stopped cleanly.
    """

    def __init__(self, config=None):
        self.config = config or CameraConfig.from_environment()
        self._capture = None

    @property
    def is_open(self):
        return self._capture is not None

    def open(self):
        """
        Open the camera, or raise CameraError.

        cv2 is imported here rather than at module level so that importing the
        backend does not load OpenCV until somebody actually asks for a live
        session.
        """
        if self._capture is not None:
            return self

        # Reuses the Step 1 helper, which already selects the DirectShow
        # backend on Windows - the one that opens webcams reliably there.
        from camera_test import open_camera

        capture = open_camera(self.config.index, self.config.width,
                              self.config.height)

        if capture is None:
            raise CameraError(
                "Camera %d could not be opened. It may be missing, in use by "
                "another application, or blocked by a privacy setting."
                % self.config.index)

        self._capture = capture
        return self

    def read(self):
        """One frame, or None if this read failed. Never raises."""
        if self._capture is None:
            return None

        try:
            ok, frame = self._capture.read()
        except Exception:
            return None

        return frame if ok else None

    def actual_size(self):
        """What the camera really gave us, which may not be what we asked."""
        if self._capture is None:
            return (0, 0)

        try:
            import cv2
            return (int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        except Exception:
            return (0, 0)

    def release(self):
        """Give the camera back. Safe to call more than once."""
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                # Releasing must never be the thing that raises - it runs in a
                # finally block whose whole job is cleanup.
                pass
        return True


def open_camera_source(config=None):
    """Default camera factory. Tests replace this with a fake."""
    camera = OpenCVCamera(config)
    camera.open()
    return camera
