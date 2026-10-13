"""
ClassVision AI - Headless Enrollment Session
--------------------------------------------
The enrollment capture loop, without a window.

Why this file exists
    ai/enroll_student.py works, and keeps working. But its capture loop is
    welded to an OpenCV window: it calls imshow, waitKey and
    getWindowProperty every frame. That is exactly right for a person sitting
    at the machine, and impossible to drive from a web request.

    So the DECISION half of that loop - is this frame usable, has it been
    stable long enough, are we still cooling down after the last sample - is
    lifted into SampleCollector here, and BOTH the CLI and the dashboard use
    it. There is one set of rules for what counts as a good enrollment sample,
    and one place to change it.

    Nothing about the models moves. Detection is still YuNet, embedding is
    still SFace, the quality gate is still ai/face_quality.py, and the store
    is still ai/embedding_store.py. This file coordinates them; it implements
    none of them.

The two stores, and why enrollment has to write both

    data/embeddings/          student_id -> roll, class, section, vectors
                              read by RECOGNITION (Step 4's gallery)

    data/classvision.db       student_id -> name, status, attendance
                              read by the DASHBOARD, sessions and attendance

    They have always shared a key - student_id - and never been linked. That
    is the whole reason a student enrolled from the command line was invisible
    to the dashboard: the CLI wrote vectors and nobody wrote a row.

    Enrollment now writes both, in that order. The embedding store first,
    because it is the one recognition needs; the registry second, because a
    student row with no vectors behind it is a student the camera can never
    recognise.

Privacy
    Unchanged, and it is the reason this file holds a camera at all rather
    than accepting uploads. Frames are read, turned into embeddings, and
    dropped. No frame, crop or photograph is written anywhere, and the
    browser never sees an image or a vector - only counters and a state.
"""

import threading
import time
from datetime import datetime, timezone

import numpy as np

from embedding_store import EmbeddingStore
from face_quality import check_frame_for_enrollment
from live_overlay import render_preview

# Capture tuning. These are the CLI's values, imported rather than repeated so
# the two paths cannot drift apart.
from enrollment_rules import (
    COOLDOWN_SECONDS,
    DEFAULT_SAMPLES,
    STABLE_FRAMES_REQUIRED,
)

# -- states -----------------------------------------------------------------

ENROLL_IDLE = "IDLE"
ENROLL_STARTING = "STARTING"
ENROLL_CAPTURING = "CAPTURING"
ENROLL_SAVING = "SAVING"
ENROLL_COMPLETED = "COMPLETED"
ENROLL_FAILED = "FAILED"
ENROLL_CANCELLED = "CANCELLED"

ALL_ENROLL_STATES = (ENROLL_IDLE, ENROLL_STARTING, ENROLL_CAPTURING,
                     ENROLL_SAVING, ENROLL_COMPLETED, ENROLL_FAILED,
                     ENROLL_CANCELLED)

BUSY_STATES = (ENROLL_STARTING, ENROLL_CAPTURING, ENROLL_SAVING)

# How long a capture may run before it is abandoned. A student who cannot get
# five good samples in three minutes is having a problem the camera will not
# solve by waiting longer.
DEFAULT_TIMEOUT_SECONDS = 180.0

# Consecutive failed reads before the camera is called gone.
DEFAULT_MAX_READ_FAILURES = 30


class EnrollmentError(RuntimeError):
    """Every message here is written to be shown to a teacher."""


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds") if moment else None


class SampleCollector:
    """
    Decides, frame by frame, whether a usable enrollment sample was captured.

    Display-free on purpose: the CLI draws its own overlay from the same
    state, and the server draws nothing at all. Feed it frames; it returns a
    short report per frame and accumulates embeddings.
    """

    def __init__(self, detector, embedder, total_samples=DEFAULT_SAMPLES,
                 stable_frames=STABLE_FRAMES_REQUIRED,
                 cooldown=COOLDOWN_SECONDS, clock=time.time):
        self.detector = detector
        self.embedder = embedder
        self.total_samples = int(total_samples)
        self.stable_frames_required = int(stable_frames)
        self.cooldown = float(cooldown)
        self.clock = clock

        self.embeddings = []
        self.stable_frames = 0
        self.cooldown_until = 0.0
        self.rejected = 0

        # The last frame's working state. The CLI reads these to draw its
        # overlay; the server ignores them. Exposing them here keeps offer()
        # to a small return value without the CLI having to re-run detection.
        self.last_detections = []
        self.last_ok = False
        self.last_cooling = False

    @property
    def captured(self):
        return len(self.embeddings)

    @property
    def complete(self):
        return len(self.embeddings) >= self.total_samples

    def offer(self, frame):
        """
        Offer one frame.

        Returns (captured_this_frame, reason, detection). The detection is
        handed back so a caller that wants to draw a box can - the CLI does,
        the server ignores it.
        """
        height, width = frame.shape[:2]
        detections = self.detector.detect(frame)
        result, detection = check_frame_for_enrollment(detections, width, height)

        cooling = self.clock() < self.cooldown_until

        self.last_detections = detections
        self.last_ok = bool(result.ok)
        self.last_cooling = cooling

        if result.ok and not cooling:
            self.stable_frames += 1
        else:
            self.stable_frames = 0

        if cooling:
            return False, "hold still", detection

        if not (self.stable_frames >= self.stable_frames_required
                and detection is not None):
            return False, result.reason, detection

        try:
            embedding = self.embedder.embed_detection(frame, detection)
        except ValueError as error:
            # A bad sample is skipped, never stored.
            self.stable_frames = 0
            self.rejected += 1
            return False, "sample rejected: %s" % error, detection

        self.embeddings.append(embedding)
        self.stable_frames = 0
        self.cooldown_until = self.clock() + self.cooldown

        # The frame goes out of scope in the caller. Nothing is written.
        return True, "sample %d of %d captured" % (self.captured,
                                                   self.total_samples), detection


def persist_enrollment(details, embeddings, embedder, store=None,
                       registry_sync=None):
    """
    Save an enrollment: vectors first, then the registry row.

    This is the half of the CLI's enroll() that has nothing to do with a
    window, and both paths call it so a dashboard enrollment and a command
    line enrollment produce byte-identical records.

    registry_sync is a callable taking the whole details dict - id, name,
    roll number, class and section. It is passed in rather than imported so
    this module keeps no dependency on SQLite: the CLI supplies a best-effort
    one, the backend supplies the real repository.

    It takes the DICT rather than an id and a name because the registry is now
    the source of truth for who a student is. Handing it two of the five
    fields would leave the other three reachable only through the embedding
    store, which is the exact split this work exists to close.
    """
    store = store or EmbeddingStore()
    model_info = embedder.model_info()

    record = store.save_student(
        student_id=details["student_id"],
        name=details["name"],
        roll_number=details["roll_number"],
        class_name=details["class_name"],
        section=details["section"],
        model_name=model_info["model_name"],
        model_version=model_info["model_version"],
        extra={
            "model_file": model_info["model_file"],
            "model_sha256": model_info["model_sha256"],
            "embedding_dim": model_info["embedding_dim"],
            "normalization": model_info["normalization"],
            "similarity": model_info["similarity"],
        },
    )

    store.save_embedding(details["student_id"], np.array(embeddings),
                         model_info)

    synced = False
    if registry_sync is not None:
        # A failure here must not lose the enrollment that has already been
        # written. The caller is told, and can retry the row.
        synced = bool(registry_sync(details))

    return {
        "student_id": details["student_id"],
        "name": details["name"],
        "samples": len(embeddings),
        "model_name": model_info["model_name"],
        "model_version": model_info["model_version"],
        "registry_synced": synced,
        "sample_count": record.get("sample_count", len(embeddings)),
    }


class EnrollmentRuntime:
    """
    The in-memory state of one enrollment, safe to read from any thread.

    Deliberately shaped like ai/live_session.py's LiveRuntime: the same lock
    discipline, the same "snapshot() is the only thing the API may see" rule,
    and the same refusal to hold anything that could carry an image.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        with self._lock:
            self.state = ENROLL_IDLE
            self.student_id = None
            self.name = None
            self.captured = 0
            self.required = DEFAULT_SAMPLES
            self.rejected = 0
            self.message = None
            self.error = None
            self.started_at = None
            self.finished_at = None
            self.camera_index = None

            # -- the enrollment preview -----------------------------------
            #
            # The same one-slot arrangement the live classroom uses. A person
            # being enrolled has to be able to see themselves in frame -
            # "look at the camera" is useless advice if you cannot tell
            # whether you are in shot - and the FULL frame is what shows
            # that, not a crop of your own face.
            self._frame = None
            self._frame_index = 0
        return self

    def publish_frame(self, jpeg):
        with self._lock:
            self._frame = jpeg
            self._frame_index += 1

    def latest_frame(self):
        with self._lock:
            return self._frame, self._frame_index

    def clear_frame(self):
        """Dropped when the camera goes, so no stale view is served."""
        with self._lock:
            self._frame = None

    @property
    def is_busy(self):
        with self._lock:
            return self.state in BUSY_STATES

    def begin(self, student_id, name, required, camera_index):
        with self._lock:
            self.reset()
            self.state = ENROLL_STARTING
            self.student_id = student_id
            self.name = name
            self.required = int(required)
            self.camera_index = camera_index
            self.started_at = utc_now()
            self.message = "Opening the camera"

    def mark_capturing(self):
        with self._lock:
            self.state = ENROLL_CAPTURING
            self.message = "Look at the camera"

    def note_progress(self, captured, rejected, message):
        with self._lock:
            self.captured = int(captured)
            self.rejected = int(rejected)
            self.message = message

    def mark_saving(self):
        with self._lock:
            self.state = ENROLL_SAVING
            self.message = "Saving enrollment"

    def mark_completed(self, message):
        with self._lock:
            self.state = ENROLL_COMPLETED
            self.message = message
            self.finished_at = utc_now()
            self._frame = None

    def fail(self, message):
        """Record a failure in a form that is safe to show a browser."""
        with self._lock:
            self.state = ENROLL_FAILED
            self.error = str(message)[:300]
            self.message = self.error
            self.finished_at = utc_now()
            self._frame = None

    def cancel(self, message="Enrollment cancelled."):
        with self._lock:
            self.state = ENROLL_CANCELLED
            self.message = message
            self.finished_at = utc_now()
            self._frame = None

    def snapshot(self):
        """
        The ONLY view the API is allowed to serve.

        Built field by field, so a new internal attribute cannot leak into a
        response by accident. There is no field here for a frame, a crop or a
        vector, because there is nothing of that kind to put in one.
        """
        with self._lock:
            return {
                "state": self.state,
                "student_id": self.student_id,
                "name": self.name,
                "captured": self.captured,
                "required": self.required,
                "rejected": self.rejected,
                "message": self.message,
                "error": self.error,
                "camera_index": self.camera_index,
                "started_at": _iso(self.started_at),
                "finished_at": _iso(self.finished_at),
            }


class EnrollmentWorker(threading.Thread):
    """
    The capture loop, on its own thread.

    Its contract matches the Step 8 camera worker's, for the same reasons:

      - it owns the camera and is the only thing that reads it;
      - it releases the camera in a finally block, whatever happened;
      - it writes no image anywhere;
      - any exception becomes FAILED with a safe message, not a traceback.
    """

    def __init__(self, runtime, camera, collector, details, embedder,
                 store=None, registry_sync=None, stop_event=None,
                 timeout=DEFAULT_TIMEOUT_SECONDS,
                 max_read_failures=DEFAULT_MAX_READ_FAILURES, clock=time.time,
                 preview=True):
        super().__init__(name="classvision-enrollment", daemon=True)

        self.runtime = runtime
        self.camera = camera
        self.collector = collector
        self.details = details
        self.embedder = embedder
        self.store = store
        self.registry_sync = registry_sync
        self.stop_event = stop_event or threading.Event()
        self.timeout = float(timeout)
        self.max_read_failures = int(max_read_failures)
        self.clock = clock

        # On by default: somebody being enrolled needs to see whether they
        # are actually in shot. Off for the tests, which feed this worker
        # arrays that are not photographs of anything.
        self.preview = bool(preview)
        self.preview_failures = 0

        self.result = None

    def request_stop(self):
        self.stop_event.set()

    def run(self):
        try:
            self._capture()
        except EnrollmentError as error:
            self.runtime.fail(str(error))
        except Exception as error:                       # noqa: BLE001
            self.runtime.fail(
                "Enrollment stopped unexpectedly (%s). Nothing was stored."
                % type(error).__name__)
        finally:
            self.camera.release()
            self.runtime.clear_frame()

    def _publish_preview(self, frame, detection, reason):
        """
        The complete camera frame, with the detected face boxed on it.

        The FULL frame on purpose. Enrollment fails most often because the
        person is too far away, off-centre or backlit, and every one of those
        is obvious in a picture of the room and invisible in a crop of a face.

        The box is drawn from the detection the collector just used - no
        second detection pass - and the frame is dropped immediately after
        encoding.
        """
        try:
            rows = []
            if detection is not None:
                rows.append({
                    "box": (detection.x, detection.y,
                            detection.width, detection.height),
                    "track_id": 1,
                    "student_id": None,
                    "student_name": None,
                    "liveness_state": "UNKNOWN",
                    "authenticity_state": "UNKNOWN",
                })
                # The box is labelled "Track 1" by the shared overlay, which
                # is honest: enrollment identifies nobody, it records them.

            jpeg = render_preview(frame, rows)
            if jpeg:
                self.runtime.publish_frame(jpeg)
        except Exception:                                # noqa: BLE001
            self.preview_failures += 1

    def _capture(self):
        self.runtime.mark_capturing()

        deadline = self.clock() + self.timeout
        read_failures = 0

        while not self.collector.complete:
            if self.stop_event.is_set():
                self.runtime.cancel()
                return

            if self.clock() > deadline:
                raise EnrollmentError(
                    "Enrollment timed out after %d seconds with %d of %d "
                    "samples. Nothing was stored."
                    % (int(self.timeout), self.collector.captured,
                       self.collector.total_samples))

            frame = self.camera.read()

            if frame is None:
                read_failures += 1
                if read_failures >= self.max_read_failures:
                    raise EnrollmentError(
                        "The camera stopped delivering frames. Nothing was "
                        "stored.")
                continue

            read_failures = 0

            captured, reason, detection = self.collector.offer(frame)

            if self.preview:
                self._publish_preview(frame, detection, reason)

            frame = None

            self.runtime.note_progress(self.collector.captured,
                                       self.collector.rejected, reason)

        self.runtime.mark_saving()

        self.result = persist_enrollment(self.details, self.collector.embeddings,
                                         self.embedder, store=self.store,
                                         registry_sync=self.registry_sync)

        self.runtime.mark_completed(
            "%s enrolled with %d samples."
            % (self.details["name"], self.result["samples"]))
