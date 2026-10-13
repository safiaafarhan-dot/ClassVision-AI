"""
Live session runtime and pipeline tests (Step 8).

NO WEBCAM AND NO MODEL ARE NEEDED. The camera is a fake that hands out
pre-built frames, and the detector and recogniser are fakes that return
scripted results. That is deliberate: a test that needs a person to stand in
front of a webcam is a test that never runs.

WHAT THIS VERIFIES
    The state machine, the worker's cleanup guarantees, the safety of the
    runtime snapshot, and that the live presence view is derived by the same
    Step 6 function that attendance is.

WHAT THIS DOES NOT VERIFY
    Real recognition accuracy, real tracking under occlusion, or real camera
    behaviour. Those are unchanged from Steps 2-5 and remain unvalidated on a
    real classroom population.

Run with:
    python tests/test_live_session.py
"""

import sys
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import numpy as np  # noqa: E402

from attendance_rules import segments_from_events  # noqa: E402
from face_detector import Detection  # noqa: E402
from face_recognizer import (  # noqa: E402
    STATUS_MATCH,
    STATUS_UNKNOWN,
    RecognitionResult,
)
from live_camera import (  # noqa: E402
    CAMERA_RELEASED,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    CameraConfig,
    CameraError,
    OpenCVCamera,
)
from live_pipeline import (  # noqa: E402
    LIVE_PRESENT,
    LIVE_SEEN_EARLIER,
    LIVE_TEMPORARILY_LOST,
    LivePipeline,
)
from live_session import (  # noqa: E402
    LIVE_COMPLETED,
    LIVE_FAILED,
    LIVE_IDLE,
    LIVE_RUNNING,
    LIVE_STARTING,
    LIVE_STOPPING,
    LiveRuntime,
    LiveSessionWorker,
    LiveStateError,
)
from person_tracker import PersonTracker  # noqa: E402
from presence_timeline import PresenceTimeline  # noqa: E402
from track_identity import IdentityManager  # noqa: E402
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)

# The Step 5 timeline stamps each frame with datetime.now(). On Windows that
# clock only moves in ~15.6 ms steps, so a test that pushes a hundred frames
# through in a millisecond would give every event the SAME timestamp and
# produce zero-length presence. A real session at the measured 15 FPS is 66 ms
# per frame and never hits this.
#
# So the tests drive the timeline with an explicit clock that advances one
# frame at a time. That makes durations deterministic rather than dependent on
# how fast the machine running the tests happens to be.
FRAME_SECONDS = 1.0 / 15.0


class FrameClock:
    """A clock that moves one frame per timeline update."""

    def __init__(self, start=START, step_seconds=FRAME_SECONDS):
        self.moment = start
        self.step = timedelta(seconds=step_seconds)

    def tick(self):
        self.moment += self.step
        return self.moment

    def now(self):
        return self.moment


import presence_timeline as _timeline_module  # noqa: E402

_CLOCK = FrameClock()
_timeline_module.utc_now = _CLOCK.tick

# Anything that would be biometric data, media, a path or a secret.
FORBIDDEN_KEYS = (
    "embedding", "vector", "descriptor", "feature", "face", "image", "photo",
    "crop", "video", "thumbnail", "similarity", "password", "secret", "token",
    "credential", "api_key", "path", "model",
)


# -- fakes ------------------------------------------------------------------

class FakeFrame:
    """
    Stands in for a camera frame.

    It is not a numpy array on purpose: nothing in the live layer may inspect,
    copy or store a frame, so anything that tried would fail loudly here.
    """

    def __init__(self, index):
        self.index = index


class FakeCamera:
    """A camera that yields a fixed number of frames, then whatever was asked."""

    def __init__(self, frames=None, fail_after=None, never_opens=False,
                 delay=0.0):
        self.frames = frames
        self.fail_after = fail_after
        self.never_opens = never_opens
        # A real camera paces the loop at its frame rate. Some tests need that
        # pacing back, because a fake that returns instantly finishes hundreds
        # of frames before another thread is scheduled at all.
        self.delay = delay
        self.reads = 0
        self.released = 0
        self.opened = False

    def open(self):
        if self.never_opens:
            raise CameraError("Camera 7 could not be opened.")
        self.opened = True
        return self

    def read(self):
        self.reads += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_after is not None and self.reads > self.fail_after:
            return None
        if self.frames is not None and self.reads > self.frames:
            return None
        return FakeFrame(self.reads)

    def release(self):
        self.released += 1
        return True


class ExplodingCamera(FakeCamera):
    def read(self):
        raise RuntimeError("the driver fell over")


class ScriptedDetector:
    """
    Returns the detections written for each frame number.

    A script entry is a list of (x, y, width, height, confidence) tuples; an
    empty list means nobody is in shot for that frame.
    """

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        boxes = self.script[min(self.calls - 1, len(self.script) - 1)]
        return [Detection(x, y, w, h, confidence,
                          landmarks=np.zeros((5, 2), dtype=np.float32))
                for (x, y, w, h, confidence) in boxes]


class ScriptedRecognizer:
    """Returns a fixed answer per box position. No model, no embedding."""

    def __init__(self, by_x=None, default_unknown=True):
        self.by_x = by_x or {}
        self.default_unknown = default_unknown
        self.calls = 0

    def recognize(self, frame, detection):
        self.calls += 1
        match = self.by_x.get(detection.x)

        if match is None:
            return RecognitionResult(status=STATUS_UNKNOWN, similarity=0.21,
                                     box=detection.as_tuple())

        student_id, name = match
        return RecognitionResult(identity=student_id, name=name,
                                 similarity=0.83, status=STATUS_MATCH,
                                 box=detection.as_tuple())


def build_pipeline(script, by_x, names=None, start=START, end=END):
    """A LivePipeline wired to fakes, with the real Step 5 components inside."""
    from tracking_test import TrackingSession

    # Each pipeline gets a fresh clock so tests cannot drift into each other.
    _CLOCK.moment = start

    detector = ScriptedDetector(script)
    recognizer = ScriptedRecognizer(by_x)

    session = TrackingSession(detector, recognizer, tracker=PersonTracker(),
                              identity_manager=IdentityManager(),
                              timeline=PresenceTimeline(heartbeat_frames=5))

    pipeline = LivePipeline(detector, recognizer, session_start=start,
                            session_end=end, student_names=names or {},
                            tracking_session=session, clock=_CLOCK.now)
    return pipeline, detector, recognizer


def walk(value, visit, trail=""):
    if isinstance(value, dict):
        for key, item in value.items():
            visit(key, "%s.%s" % (trail, key))
            walk(item, visit, "%s.%s" % (trail, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, visit, "%s[%d]" % (trail, index))


# -- state machine ----------------------------------------------------------

def test_state_transitions_follow_the_declared_machine():
    runtime = LiveRuntime()
    assert runtime.state == LIVE_IDLE

    runtime.begin("SES-1", class_id="10A", camera_index=0)
    assert runtime.state == LIVE_STARTING

    runtime.mark_running()
    assert runtime.state == LIVE_RUNNING

    runtime.mark_stopping()
    assert runtime.state == LIVE_STOPPING

    runtime.mark_completed()
    assert runtime.state == LIVE_COMPLETED

    # A completed run may be followed by a new one, and nothing else.
    runtime.begin("SES-2")
    assert runtime.state == LIVE_STARTING
    print("PASS  IDLE -> STARTING -> RUNNING -> STOPPING -> COMPLETED -> "
          "STARTING")


def test_illegal_transitions_are_refused():
    runtime = LiveRuntime()

    for bad in (runtime.mark_running, runtime.mark_stopping,
                runtime.mark_completed):
        try:
            bad()
        except LiveStateError:
            continue
        raise AssertionError("%s was allowed from IDLE" % bad.__name__)

    runtime.begin("SES-1")
    runtime.mark_running()
    try:
        runtime.mark_completed()
        raise AssertionError("RUNNING -> COMPLETED skipped STOPPING")
    except LiveStateError:
        pass

    print("PASS  illegal transitions raise LiveStateError instead of "
          "half-running")


def test_failure_is_reachable_from_every_busy_state_and_keeps_a_safe_message():
    for enter in ("STARTING", "RUNNING", "STOPPING"):
        runtime = LiveRuntime()
        runtime.begin("SES-1")
        if enter in ("RUNNING", "STOPPING"):
            runtime.mark_running()
        if enter == "STOPPING":
            runtime.mark_stopping()

        runtime.fail("Camera 0 could not be opened.")
        assert runtime.state == LIVE_FAILED, (
            "a failure in %s did not reach FAILED" % enter)
        assert "could not be opened" in runtime.error

    print("PASS  a failure in STARTING, RUNNING or STOPPING reaches FAILED")


def test_runtime_snapshot_is_safe_and_complete():
    runtime = LiveRuntime()
    runtime.begin("SES-1", class_id="10A", camera_index=2)
    runtime.mark_running()
    runtime.note_frame(fps=14.8, active_tracks=4, recognized_students=3,
                       unknown_tracks=1)

    snapshot = runtime.snapshot()

    required = ("state", "session_id", "elapsed_seconds", "fps",
                "active_tracks", "recognized_students", "unknown_tracks",
                "camera", "started_at", "last_event_at")
    for field in required:
        assert field in snapshot, "the status is missing %s" % field

    offenders = []
    walk(snapshot, lambda key, trail: offenders.extend(
        [trail] for banned in FORBIDDEN_KEYS
        if banned in key.lower()))
    assert not offenders, "the runtime snapshot exposes %s" % offenders

    for value in snapshot.values():
        assert isinstance(value, (str, int, float, bool, type(None))), (
            "the snapshot carries a %s, which could hold anything"
            % type(value).__name__)

    print("PASS  runtime snapshot carries %d plain, non-biometric fields"
          % len(snapshot))


def test_camera_config_defaults_to_the_measured_resolution():
    config = CameraConfig()
    assert (config.width, config.height) == (DEFAULT_WIDTH, DEFAULT_HEIGHT)
    assert (config.width, config.height) == (640, 480), (
        "the default must stay at the resolution Step 1 and Step 5 measured")

    explicit = CameraConfig.from_environment(index=3, width=1280, height=720)
    assert explicit.index == 3 and explicit.width == 1280

    for bad in ({"index": -1}, {"width": 0}, {"height": -10}):
        try:
            CameraConfig(**bad)
            raise AssertionError("CameraConfig accepted %s" % bad)
        except ValueError:
            pass

    print("PASS  camera defaults to 640x480 and refuses impossible settings")


def test_real_camera_release_is_idempotent():
    camera = OpenCVCamera(CameraConfig(index=0))
    assert camera.is_open is False
    assert camera.release() is True
    assert camera.release() is True, "a second release must not raise"
    assert camera.read() is None, "reading an unopened camera must not raise"
    print("PASS  an unopened camera reads as None and releases twice safely")


# -- the worker -------------------------------------------------------------

def run_worker(camera, pipeline, runtime=None, presence_interval=5,
               max_read_failures=5, frames_to_wait=None, timeout=5.0):
    runtime = runtime or LiveRuntime()
    runtime.begin("SES-1")
    runtime.mark_running()

    worker = LiveSessionWorker(runtime, camera, pipeline,
                               presence_interval=presence_interval,
                               max_read_failures=max_read_failures)
    worker.start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if frames_to_wait is not None and worker.frames >= frames_to_wait:
            break
        if not worker.is_alive():
            break
        time.sleep(0.01)

    worker.request_stop()
    worker.join(timeout=timeout)
    return runtime, worker


def test_worker_processes_frames_and_stops_cleanly():
    pipeline, detector, _ = build_pipeline(
        [[(100, 100, 60, 60, 0.95)]], {100: ("STU001", "Student One")})
    camera = FakeCamera()

    runtime, worker = run_worker(camera, pipeline, frames_to_wait=30)

    assert worker.frames >= 30, "the worker processed only %d frames" % worker.frames
    assert not worker.is_alive(), "the worker did not stop"
    assert camera.released >= 1, "the camera was not released"
    assert runtime.processed_count >= 30
    assert runtime.fps > 0, "no frame rate was measured"
    print("PASS  worker processed %d frames, then stopped and released the "
          "camera" % worker.frames)


def test_camera_failure_during_a_session_fails_and_releases():
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]],
                                    {100: ("STU001", "Student One")})
    camera = FakeCamera(fail_after=10)

    runtime, worker = run_worker(camera, pipeline, max_read_failures=5)

    assert runtime.state == LIVE_FAILED, (
        "a dead camera left the runtime in %s" % runtime.state)
    assert runtime.camera_status == CAMERA_RELEASED
    assert camera.released >= 1, "the camera was not released after a failure"
    assert "camera" in (runtime.error or "").lower()
    assert "Traceback" not in (runtime.error or "")
    print("PASS  a camera that stops delivering fails the run and releases it")


def test_worker_failure_becomes_FAILED_without_leaking_a_traceback():
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]], {})
    camera = ExplodingCamera()

    runtime, worker = run_worker(camera, pipeline)

    assert runtime.state == LIVE_FAILED
    assert camera.released >= 1, "the camera was not released after a crash"

    error = runtime.error or ""
    assert "RuntimeError" in error, "the failure type should be reported"
    assert "driver fell over" not in error, (
        "the raw exception message reached the runtime")
    assert "Traceback" not in error and "File \"" not in error
    print("PASS  a crashing worker becomes FAILED with a safe message")


def test_camera_is_released_even_when_the_pipeline_raises():
    class BrokenPipeline:
        def process(self, frame):
            raise ValueError("pipeline problem")

        def counts(self):
            return {"active_tracks": 0, "unknown_tracks": 0,
                    "recognized_students": 0, "recognition_calls": 0}

        def presence_view(self):
            return []

    camera = FakeCamera()
    runtime, _ = run_worker(camera, BrokenPipeline())

    assert runtime.state == LIVE_FAILED
    assert camera.released >= 1, "the camera leaked when the pipeline raised"
    print("PASS  a pipeline exception still releases the camera")


# -- presence ---------------------------------------------------------------

def drive(pipeline, frames):
    """Push N frames through a pipeline built on fakes."""
    for index in range(frames):
        pipeline.process(FakeFrame(index))
    return pipeline


def test_unknown_person_produces_no_presence_row():
    """The hard rule, restated for the live view."""
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]], {})
    drive(pipeline, 30)

    rows = pipeline.presence_view()
    assert rows == [], "an unverified person appeared in the presence view"
    assert pipeline.counts()["unknown_tracks"] == 1, (
        "the unverified person should still be visible as a count")

    segments = pipeline.segments(session_end=END)
    assert segments == [], "an unverified person produced a presence segment"
    print("PASS  an unverified person is a count, never a row and never a "
          "segment")


def test_a_verified_student_appears_with_a_provisional_duration():
    pipeline, _, _ = build_pipeline(
        [[(100, 100, 60, 60, 0.95)]], {100: ("STU001", "Student One")},
        names={"STU001": "Student One"})
    drive(pipeline, 30)

    rows = pipeline.presence_view()
    assert len(rows) == 1, "expected one verified student, got %d" % len(rows)

    row = rows[0]
    assert row["student_id"] == "STU001"
    assert row["student_name"] == "Student One"
    assert row["identity_state"] == "CONFIRMED"
    assert row["presence_state"] == LIVE_PRESENT
    assert row["duration_seconds"] >= 0
    assert "similarity" not in row and "embedding" not in row
    print("PASS  a verified student appears as %s / %s with no score attached"
          % (row["identity_state"], row["presence_state"]))


def test_presence_survives_a_temporary_track_loss():
    """
    Blinking is not leaving.

    The student is detected, disappears for a few frames - inside the
    tracker's grace period - and comes back. The live view must keep them, and
    the Step 6 segment must not be cut in two.
    """
    script = ([[(100, 100, 60, 60, 0.95)]] * 12
              + [[]] * 5
              + [[(104, 100, 60, 60, 0.95)]] * 12)

    pipeline, _, _ = build_pipeline(script, {100: ("STU001", "Student One"),
                                             104: ("STU001", "Student One")})

    for index in range(12):
        pipeline.process(FakeFrame(index))

    during_loss_states = []
    for index in range(12, 17):
        pipeline.process(FakeFrame(index))
        rows = pipeline.presence_view()
        during_loss_states.append(rows[0]["presence_state"] if rows else None)

    for index in range(17, 29):
        pipeline.process(FakeFrame(index))

    rows = pipeline.presence_view()
    assert len(rows) == 1, "the student was lost entirely"
    assert rows[0]["presence_state"] == LIVE_PRESENT, (
        "the student did not return to PRESENT, but to %s"
        % rows[0]["presence_state"])
    assert LIVE_TEMPORARILY_LOST in during_loss_states, (
        "the brief loss was never reported as temporary: %s"
        % during_loss_states)
    assert None not in during_loss_states, (
        "the student vanished from the live view during a brief loss")

    assert rows[0]["segment_count"] == 1, (
        "a loss inside the grace period split the presence into %d segments"
        % rows[0]["segment_count"])
    print("PASS  a %d-frame loss stays TEMPORARILY_LOST and keeps one segment"
          % 5)


def test_a_student_who_leaves_is_seen_earlier_never_absent():
    script = [[(100, 100, 60, 60, 0.95)]] * 12 + [[]] * 40

    pipeline, _, _ = build_pipeline(script, {100: ("STU001", "Student One")})
    drive(pipeline, 52)

    rows = pipeline.presence_view()
    assert len(rows) == 1
    assert rows[0]["presence_state"] == LIVE_SEEN_EARLIER, (
        "a student who left is reported as %s" % rows[0]["presence_state"])

    states = {row["presence_state"] for row in rows}
    assert "ABSENT" not in states, (
        "the live view called somebody absent before the session ended")
    print("PASS  a student who left is SEEN_EARLIER - never ABSENT while the "
          "session runs")


def test_the_live_view_uses_the_same_segment_definition_as_attendance():
    """
    One definition, or the dashboard and the record can disagree.

    The live duration must come from the same Step 6 function the rule engine
    is fed, not from a convenient live approximation.
    """
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]],
                                    {100: ("STU001", "Student One")})
    drive(pipeline, 40)

    now = pipeline.clock()
    rows = pipeline.presence_view(now=now)

    direct = segments_from_events(pipeline.timeline.events,
                                  grace_seconds=pipeline.grace_seconds,
                                  session_end=now)
    total = sum(segment.duration_seconds for segment in direct
                if segment.student_id == "STU001")

    assert abs(rows[0]["duration_seconds"] - round(total, 1)) < 0.2, (
        "the live duration (%s) disagrees with segments_from_events (%s)"
        % (rows[0]["duration_seconds"], total))
    print("PASS  live durations come from segments_from_events, the same "
          "function attendance uses")


def test_recognition_runs_far_less_often_than_once_per_frame():
    """Step 5's scheduling must still be in charge - Step 8 adds no recogniser."""
    pipeline, _, recognizer = build_pipeline(
        [[(100, 100, 60, 60, 0.95)]], {100: ("STU001", "Student One")})
    drive(pipeline, 90)

    efficiency = pipeline.efficiency()
    assert recognizer.calls == efficiency["recognition_calls"]
    assert recognizer.calls < 20, (
        "recognition ran %d times in 90 frames - the identity manager is no "
        "longer scheduling it" % recognizer.calls)

    print("PASS  %d recognition calls over 90 frames (%.1f%% avoided)"
          % (recognizer.calls, efficiency["reduction_percent"]))


def test_identity_stability_still_owns_confirmation():
    """
    One agreeing recognition must not confirm anybody.

    Step 8 must not shortcut the two-confirmation rule, so a track that has
    only been recognised once produces no presence row at all.
    """
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]],
                                    {100: ("STU001", "Student One")})

    pipeline.process(FakeFrame(0))
    assert pipeline.presence_view() == [], (
        "a single recognition produced a presence row")

    pipeline.process(FakeFrame(1))
    identity = pipeline.identities.get(1)
    assert identity is not None and identity.is_confirmed, (
        "two agreeing recognitions should confirm the identity")
    print("PASS  one recognition confirms nobody; two do - unchanged from "
          "Step 5")


def test_closing_the_pipeline_is_idempotent():
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]],
                                    {100: ("STU001", "Student One")})
    drive(pipeline, 20)

    first = pipeline.close()
    second = pipeline.close()

    assert first, "closing an open session should emit exit events"
    assert second == [], "closing twice emitted events twice"

    segments = pipeline.segments(session_end=END)
    assert len(segments) == 1, (
        "expected one segment after closing, got %d" % len(segments))
    print("PASS  close() emits exits once and is safe to call again")


def test_the_worker_publishes_presence_without_the_reader_touching_the_pipeline():
    """
    The API reads a published snapshot, never the live objects.

    This is what keeps a dashboard poll off the worker's data structures.
    """
    pipeline, _, _ = build_pipeline([[(100, 100, 60, 60, 0.95)]],
                                    {100: ("STU001", "Student One")},
                                    names={"STU001": "Student One"})
    # Paced like a camera, so the reader genuinely overlaps the worker.
    camera = FakeCamera(delay=0.002)
    runtime = LiveRuntime()

    readings = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            readings.append(runtime.snapshot())
            runtime.presence()
            time.sleep(0.005)

    reader = threading.Thread(target=poll, daemon=True)
    reader.start()

    run_worker(camera, pipeline, runtime=runtime, presence_interval=5,
               frames_to_wait=120)

    stop.set()
    reader.join(timeout=2)

    published = runtime.presence()
    assert published, "the worker never published a presence view"
    assert published[0]["student_id"] == "STU001"
    assert len(readings) >= 5, (
        "the reader only managed %d polls while the worker ran"
        % len(readings))
    print("PASS  %d concurrent reads while the worker ran; presence published "
          "for %d student(s)" % (len(readings), len(published)))


def main():
    enable_safe_console()

    tests = [
        test_state_transitions_follow_the_declared_machine,
        test_illegal_transitions_are_refused,
        test_failure_is_reachable_from_every_busy_state_and_keeps_a_safe_message,
        test_runtime_snapshot_is_safe_and_complete,
        test_camera_config_defaults_to_the_measured_resolution,
        test_real_camera_release_is_idempotent,
        test_worker_processes_frames_and_stops_cleanly,
        test_camera_failure_during_a_session_fails_and_releases,
        test_worker_failure_becomes_FAILED_without_leaking_a_traceback,
        test_camera_is_released_even_when_the_pipeline_raises,
        test_unknown_person_produces_no_presence_row,
        test_a_verified_student_appears_with_a_provisional_duration,
        test_presence_survives_a_temporary_track_loss,
        test_a_student_who_leaves_is_seen_earlier_never_absent,
        test_the_live_view_uses_the_same_segment_definition_as_attendance,
        test_recognition_runs_far_less_often_than_once_per_frame,
        test_identity_stability_still_owns_confirmation,
        test_closing_the_pipeline_is_idempotent,
        test_the_worker_publishes_presence_without_the_reader_touching_the_pipeline,
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
