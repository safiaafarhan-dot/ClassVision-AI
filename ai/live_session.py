"""
ClassVision AI - Live Session Runtime (Steps 8-10)
--------------------------------------------------
The state machine and background worker behind a running classroom session.

    Step 8 runs the existing pipeline against a live camera. It makes no
    attendance decision of its own. Attendance is still produced by the Step 6
    rule engine, once, after the session has been explicitly stopped.

Two different lifecycles, deliberately not merged
    AttendanceSession (Step 6)  SCHEDULED -> ACTIVE -> COMPLETED / CANCELLED
        A claim about the lesson. It is what attendance is decided against,
        and it lives in the database.

    LiveRuntime (this file)     IDLE -> STARTING -> RUNNING -> STOPPING
                                     -> COMPLETED / FAILED
        A claim about the camera loop. It lives in memory and dies with the
        process.

    They are separate because "the camera crashed" and "the lesson ended" are
    different facts. A FAILED camera must never quietly complete a lesson, and
    a completed lesson must never depend on a process staying alive.

What the runtime may hold
    Counters, states, timestamps, and student ids that the EXISTING
    recognition-stability logic has already confirmed.

What it must never hold
    Frames, images, face crops, embeddings, similarity vectors, model paths or
    file paths. snapshot() is the only thing the API can see, and it is built
    field by field for exactly that reason.

Threading
    One worker thread owns the camera and the pipeline. It never touches
    SQLite. The request thread reads only published snapshots, under a lock,
    so the API stays responsive and no pipeline object is ever read from two
    threads at once.
"""

import threading
import time
from datetime import datetime, timezone

from live_overlay import PREVIEW_EVERY_N_FRAMES, render_preview
from live_camera import (
    CAMERA_CONNECTED,
    CAMERA_DISCONNECTED,
    CAMERA_ERROR,
    CAMERA_RELEASED,
    CameraError,
)

# -- runtime states ---------------------------------------------------------

LIVE_IDLE = "IDLE"
LIVE_STARTING = "STARTING"
LIVE_RUNNING = "RUNNING"
LIVE_STOPPING = "STOPPING"
LIVE_COMPLETED = "COMPLETED"
LIVE_FAILED = "FAILED"

ALL_LIVE_STATES = (LIVE_IDLE, LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING,
                   LIVE_COMPLETED, LIVE_FAILED)

# Anything not listed here is refused. FAILED is reachable from every state
# that can own a resource, because a failure can happen at any of them.
ALLOWED_LIVE_TRANSITIONS = {
    LIVE_IDLE: (LIVE_STARTING,),
    LIVE_STARTING: (LIVE_RUNNING, LIVE_STOPPING, LIVE_FAILED),
    LIVE_RUNNING: (LIVE_STOPPING, LIVE_FAILED),
    LIVE_STOPPING: (LIVE_COMPLETED, LIVE_FAILED),
    LIVE_COMPLETED: (LIVE_STARTING,),
    LIVE_FAILED: (LIVE_STARTING, LIVE_STOPPING),
}

# States in which a camera may be held. Used to refuse a second session.
BUSY_STATES = (LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING)

# How often the worker republishes the presence view, in frames. At the
# measured ~15 FPS this is roughly once a second, which matches the dashboard
# poll. Rebuilding it every frame would spend the frame budget on a number
# nobody reads that often.
DEFAULT_PRESENCE_INTERVAL_FRAMES = 15

# How many consecutive failed reads mean the camera is gone rather than busy.
# At ~15 FPS this is about two seconds of silence.
DEFAULT_MAX_READ_FAILURES = 30

# How many recent frames the reported FPS is averaged over.
FPS_WINDOW = 30


class LiveStateError(RuntimeError):
    """Raised on an illegal live-runtime transition."""


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds") if moment else None


# What the liveness view says before a worker has published anything. Every
# count is zero and nothing is LIVE, which is the only safe starting point.
_EMPTY_LIVENESS = {
    "liveness_mode": "off",
    "liveness_available": True,
    "baseline_version": None,
    "tracks": 0,
    "live": 0,
    "spoof": 0,
    "uncertain": 0,
    "unknown": 0,
    "checks": 0,
}

# The same, for the Step 10 authenticity layer. Every count is zero and
# nothing is AUTHENTIC, which is the only safe starting point.
_EMPTY_AUTHENTICITY = {
    "authenticity_mode": "off",
    "authenticity_available": True,
    "authenticity_baseline_version": None,
    "tracks": 0,
    "authentic": 0,
    "suspicious": 0,
    "uncertain": 0,
    "unknown": 0,
    "checks": 0,
}


class LiveRuntime:
    """
    The in-memory state of the live session, safe to read from any thread.

    Every mutation and every read goes through the same lock. The lock is held
    only for plain attribute work - never across a camera read or a pipeline
    call - so the worker is never blocked by a dashboard poll.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self):
        with self._lock:
            self.state = LIVE_IDLE
            self.session_id = None
            self.class_id = None
            self.started_at = None
            self.stopped_at = None
            self.camera_status = CAMERA_DISCONNECTED
            self.camera_index = None

            self.processed_count = 0
            self.dropped_count = 0
            self.recognition_calls = 0
            self.fps = 0.0

            self.active_tracks = 0
            self.recognized_students = 0
            self.unknown_tracks = 0
            self.last_event_at = None

            self.error = None
            self._presence = []

            # -- liveness (Step 9) ----------------------------------------
            #
            # Published by the worker exactly like the presence view: the
            # request thread reads a snapshot and never touches the pipeline.
            self._tracks = []
            self._liveness = _EMPTY_LIVENESS.copy()
            self._authenticity = _EMPTY_AUTHENTICITY.copy()

            # -- the classroom preview ------------------------------------
            #
            # ONE slot, holding the most recent annotated frame as JPEG
            # bytes. Not a queue: a dashboard a second behind wants the
            # current picture of the room, not the backlog. Never written to
            # disk, and cleared the moment the camera is released.
            self._frame = None
            self._frame_index = 0
            self._frame_at = None
        return self

    def begin(self, session_id, class_id=None, camera_index=None):
        """Move IDLE / COMPLETED / FAILED to STARTING, clearing the last run."""
        with self._lock:
            self._require(LIVE_STARTING)
            self.reset()
            self.state = LIVE_STARTING
            self.session_id = session_id
            self.class_id = class_id
            self.camera_index = camera_index
            self.started_at = utc_now()
            return self.state

    def _require(self, new_state):
        allowed = ALLOWED_LIVE_TRANSITIONS.get(self.state, ())
        if new_state not in allowed:
            raise LiveStateError(
                "cannot move the live runtime from %s to %s (allowed: %s)"
                % (self.state, new_state,
                   ", ".join(allowed) if allowed else "nothing"))

    def transition(self, new_state):
        with self._lock:
            self._require(new_state)
            self.state = new_state
            return self.state

    def mark_running(self):
        with self._lock:
            self.transition(LIVE_RUNNING)
            self.camera_status = CAMERA_CONNECTED
            self.error = None

    def mark_stopping(self):
        with self._lock:
            self.transition(LIVE_STOPPING)

    def mark_completed(self):
        with self._lock:
            self.transition(LIVE_COMPLETED)
            self.stopped_at = utc_now()

    def fail(self, message, camera_status=CAMERA_ERROR):
        """
        Record a failure in a form that is safe to return to a browser.

        The caller passes a short sentence, never an exception repr - a
        traceback or a file path must not reach the API.
        """
        with self._lock:
            if self.state in BUSY_STATES:
                self.state = LIVE_FAILED
            self.error = str(message)[:300]
            self.camera_status = camera_status
            self.stopped_at = self.stopped_at or utc_now()

    def mark_camera_released(self):
        with self._lock:
            self.camera_status = CAMERA_RELEASED
            # Nothing is watching the room any more, so stop showing it.
            self._frame = None
            self._frame_at = None

    # -- per-frame updates -------------------------------------------------

    def note_frame(self, fps, active_tracks, recognized_students,
                   unknown_tracks, last_event_at=None, recognition_calls=None):
        with self._lock:
            self.processed_count += 1
            self.fps = round(float(fps), 2)
            self.active_tracks = int(active_tracks)
            self.recognized_students = int(recognized_students)
            self.unknown_tracks = int(unknown_tracks)
            if recognition_calls is not None:
                self.recognition_calls = int(recognition_calls)
            if last_event_at is not None:
                self.last_event_at = last_event_at

    def note_dropped(self):
        with self._lock:
            self.dropped_count += 1

    def publish_presence(self, rows):
        """Replace the presence view the API serves."""
        with self._lock:
            self._presence = list(rows)

    def presence(self):
        with self._lock:
            return list(self._presence)

    def publish_liveness(self, summary, tracks):
        """Replace the liveness view the API serves."""
        with self._lock:
            self._liveness = dict(summary)
            self._tracks = list(tracks)

    def publish_authenticity(self, summary):
        """Replace the authenticity view the API serves."""
        with self._lock:
            self._authenticity = dict(summary)

    def authenticity_snapshot(self):
        """
        Counts and configuration only.

        Built field by field like every other view, so there is no field here
        for a cue value, an image or an identity.
        """
        with self._lock:
            published = self._authenticity or _EMPTY_AUTHENTICITY
            return {
                "authenticity_mode": published.get("authenticity_mode", "off"),
                "authenticity_available": bool(
                    published.get("authenticity_available", True)),
                "authenticity_baseline_version": published.get(
                    "authenticity_baseline_version"),
                "authentic_tracks": int(published.get("authentic", 0)),
                "suspicious_tracks": int(published.get("suspicious", 0)),
                "unverified_source_tracks": int(published.get("uncertain", 0)),
                "authenticity_checks": int(published.get("checks", 0)),
            }

    def tracks(self):
        with self._lock:
            return list(self._tracks)

    def liveness_snapshot(self):
        """
        Counts and configuration only.

        Built field by field like every other view in this project. There is
        no field here for a cue value, an image or an identity, so none can
        appear by accident.
        """
        with self._lock:
            published = self._liveness or _EMPTY_LIVENESS
            return {
                "liveness_mode": published.get("liveness_mode", "off"),
                "liveness_available": bool(
                    published.get("liveness_available", True)),
                "baseline_version": published.get("baseline_version"),
                "live_tracks": int(published.get("live", 0)),
                "spoof_tracks": int(published.get("spoof", 0)),
                "uncertain_tracks": int(published.get("uncertain", 0)),
                "liveness_checks": int(published.get("checks", 0)),
            }

    # -- reading -----------------------------------------------------------

    @property
    def is_busy(self):
        with self._lock:
            return self.state in BUSY_STATES

    def elapsed_seconds(self):
        with self._lock:
            if self.started_at is None:
                return 0.0
            end = self.stopped_at or utc_now()
            return max((end - self.started_at).total_seconds(), 0.0)

    def publish_frame(self, jpeg):
        """
        Replace the single preview slot.

        One slot, not a queue: a dashboard that is a second behind wants the
        CURRENT picture of the room, not the backlog. Replacing rather than
        appending is also what stops a slow reader from growing memory
        without limit.

        The bytes are a JPEG of the classroom with boxes drawn on it. They
        are never written to disk and are dropped the moment the next frame
        arrives, or when the camera is released.
        """
        with self._lock:
            self._frame = jpeg
            self._frame_index += 1
            self._frame_at = utc_now()

    def latest_frame(self):
        """The current preview, or None. Returns (bytes, index, taken_at)."""
        with self._lock:
            return self._frame, self._frame_index, self._frame_at

    def clear_frame(self):
        """
        Drop the preview.

        Called when the camera is released, so the dashboard cannot go on
        showing a classroom that nothing is watching any more.
        """
        with self._lock:
            self._frame = None
            self._frame_at = None

    def snapshot(self):
        """
        The ONLY runtime view the API is allowed to serve.

        Built field by field, like every other response in this project, so a
        new internal attribute can never leak into a response by accident.
        """
        with self._lock:
            liveness = self._liveness or _EMPTY_LIVENESS
            authenticity = self._authenticity or _EMPTY_AUTHENTICITY

            return {
                "state": self.state,
                "session_id": self.session_id,
                "class_id": self.class_id,
                "camera": self.camera_status,
                "camera_index": self.camera_index,
                "started_at": _iso(self.started_at),
                "stopped_at": _iso(self.stopped_at),
                "elapsed_seconds": round(self.elapsed_seconds(), 1),
                "fps": self.fps,
                "processed_count": self.processed_count,
                "dropped_count": self.dropped_count,
                "recognition_calls": self.recognition_calls,
                "active_tracks": self.active_tracks,
                "recognized_students": self.recognized_students,
                "unknown_tracks": self.unknown_tracks,
                "last_event_at": _iso(self.last_event_at),
                "error": self.error,
                # -- liveness (Step 9), counts only -----------------------
                "liveness_mode": liveness.get("liveness_mode", "off"),
                "liveness_available": bool(
                    liveness.get("liveness_available", True)),
                "baseline_version": liveness.get("baseline_version"),
                "live_tracks": int(liveness.get("live", 0)),
                "spoof_tracks": int(liveness.get("spoof", 0)),
                "uncertain_tracks": int(liveness.get("uncertain", 0)),
                "liveness_checks": int(liveness.get("checks", 0)),
                # -- authenticity (Step 10), counts only ------------------
                "authenticity_mode": authenticity.get("authenticity_mode",
                                                      "off"),
                "authenticity_available": bool(
                    authenticity.get("authenticity_available", True)),
                "authenticity_baseline_version": authenticity.get(
                    "authenticity_baseline_version"),
                "authentic_tracks": int(authenticity.get("authentic", 0)),
                "suspicious_tracks": int(authenticity.get("suspicious", 0)),
                "unverified_source_tracks": int(
                    authenticity.get("uncertain", 0)),
                "authenticity_checks": int(authenticity.get("checks", 0)),
            }


class LiveSessionWorker(threading.Thread):
    """
    The camera loop, on its own thread.

    Its contract is short and worth stating plainly:

      - it owns the camera and is the only thing allowed to read it;
      - it never opens a database connection;
      - it never writes an image anywhere;
      - it releases the camera in a finally block, whatever happened;
      - any exception becomes runtime state FAILED with a safe message, not a
        traceback on somebody's screen.
    """

    def __init__(self, runtime, camera, pipeline, stop_event=None,
                 presence_interval=DEFAULT_PRESENCE_INTERVAL_FRAMES,
                 max_read_failures=DEFAULT_MAX_READ_FAILURES,
                 clock=None, preview=True,
                 preview_interval=None):
        super().__init__(name="classvision-live", daemon=True)

        self.runtime = runtime
        self.camera = camera
        self.pipeline = pipeline
        self.stop_event = stop_event or threading.Event()
        self.presence_interval = max(int(presence_interval), 1)
        self.max_read_failures = max(int(max_read_failures), 1)
        self.clock = clock or time.perf_counter

        # The dashboard's classroom view. On by default because a teacher
        # watching a live lesson needs to see the room; switchable off for a
        # headless deployment, and for the tests, which hand this worker fake
        # frames that are not images.
        self.preview = bool(preview)
        self.preview_interval = max(
            int(preview_interval if preview_interval is not None
                else PREVIEW_EVERY_N_FRAMES), 1)
        self.preview_failures = 0

        self.frames = 0
        self.read_failures = 0
        self._recent = []

    def request_stop(self):
        self.stop_event.set()

    def _publish_preview(self, frame):
        """
        Encode one preview. A failure here must never stop the lesson.

        The camera, the detector and attendance are the job; a picture on a
        dashboard is not, so an encoder that fails is counted and ignored
        rather than allowed to kill the worker.
        """
        try:
            jpeg = render_preview(frame, self.pipeline.overlay_view())
            if jpeg:
                self.runtime.publish_frame(jpeg)
        except Exception:                                # noqa: BLE001
            self.preview_failures += 1

    def _fps(self, elapsed):
        self._recent.append(elapsed)
        if len(self._recent) > FPS_WINDOW:
            del self._recent[0]
        total = sum(self._recent)
        return (len(self._recent) / total) if total > 0 else 0.0

    def run(self):
        try:
            self._loop()
        except CameraError as error:
            self.runtime.fail(str(error))
        except Exception as error:                      # noqa: BLE001
            # Deliberately broad: a worker that died silently would leave the
            # dashboard showing RUNNING forever. The exception TYPE is kept
            # because it is useful and carries no data; the message is not.
            self.runtime.fail(
                "The live session stopped unexpectedly (%s). The camera has "
                "been released and no attendance has been written."
                % type(error).__name__)
        finally:
            self.camera.release()
            self.runtime.mark_camera_released()

    def _loop(self):
        while not self.stop_event.is_set():
            started = self.clock()
            frame = self.camera.read()

            if frame is None:
                self.read_failures += 1
                self.runtime.note_dropped()

                if self.read_failures >= self.max_read_failures:
                    raise CameraError(
                        "The camera stopped delivering frames after %d "
                        "attempts. It may have been unplugged or taken by "
                        "another application." % self.read_failures)
                continue

            self.read_failures = 0
            self.frames += 1

            events = self.pipeline.process(frame)

            # The classroom view, drawn from what process() just worked out.
            # Runs no model: it copies the frame, draws boxes and labels on
            # the copy, encodes a JPEG, and replaces the single slot the API
            # reads from. Publishing frame N+1 drops frame N - that is the
            # entire retention policy, and nothing reaches disk.
            if self.preview and self.frames % self.preview_interval == 0:
                self._publish_preview(frame)

            # The frame is not stored, copied, queued or referenced past this
            # point. Dropping the reference here is the whole retention policy.
            frame = None

            elapsed = max(self.clock() - started, 1e-9)
            counts = self.pipeline.counts()

            self.runtime.note_frame(
                fps=self._fps(elapsed),
                active_tracks=counts["active_tracks"],
                recognized_students=counts["recognized_students"],
                unknown_tracks=counts["unknown_tracks"],
                last_event_at=events[-1].timestamp if events else None,
                recognition_calls=counts["recognition_calls"])

            if self.frames % self.presence_interval == 0:
                self.runtime.publish_presence(self.pipeline.presence_view())
                self.runtime.publish_liveness(
                    self.pipeline.liveness_summary(),
                    self.pipeline.tracks_view())
                self.runtime.publish_authenticity(
                    self.pipeline.authenticity_summary())
