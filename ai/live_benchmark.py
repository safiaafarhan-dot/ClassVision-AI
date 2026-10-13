"""
ClassVision AI - Live Session Benchmark (Steps 8-9)
---------------------------------------------------
Measures what Step 8 ADDS, and nothing else.

    These are SYNTHETIC numbers from a controlled machine. They are not a
    claim about classroom performance, recognition accuracy, or how this
    behaves with real people in a real room.

What is measured, and why each one

    1. Detection on synthetic frames
       The Step 5 pipeline driven by real YuNet over generated frames. This
       is the frame budget, and it is directly comparable to the Step 2 and
       Step 5 figures (640x480, ~13 ms detection, ~15 FPS).

       Generated frames contain no faces, so recognition never runs. That is
       stated rather than hidden: this measures the detection floor, not a
       populated classroom.

    2. Live orchestration overhead
       The same loop with fake detection and recognition, so the only cost
       left is what Step 8 introduced: the worker loop, the runtime lock, the
       counters, and the periodic presence republish. If this number is not
       small, the live layer is in the way.

    3. Presence view cost as a session ages
       presence_view() rebuilds from the whole event list, so it grows with
       the session. Measured rather than assumed.

    4. API status latency while the camera runs
       The claim being tested is that the camera loop does not block request
       handling.

    5. What Step 9 liveness costs
       The same load with liveness off and on, back to back in one process,
       so the difference is the overhead and not a change of conditions. It
       also checks that recognition scheduling is IDENTICAL in both columns -
       Step 9 was not allowed to change it.

Run it:
    python ai/live_benchmark.py
    python ai/live_benchmark.py --seconds 5 --faces 4
"""

import argparse
import statistics
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import numpy as np  # noqa: E402

from face_detector import Detection  # noqa: E402
from face_recognizer import (  # noqa: E402
    STATUS_MATCH,
    RecognitionResult,
)
from live_pipeline import LivePipeline  # noqa: E402
from paths import enable_safe_console  # noqa: E402
from person_tracker import PersonTracker  # noqa: E402
from presence_timeline import PresenceTimeline  # noqa: E402
from track_identity import IdentityManager  # noqa: E402

WIDTH = 640
HEIGHT = 480

warnings.filterwarnings("ignore")

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def report(title, times_ms, extra=None):
    print("  %-34s %8.2f ms avg   %8.2f ms p95   %6.1f FPS"
          % (title, statistics.fmean(times_ms), percentile(times_ms, 0.95),
             1000.0 / statistics.fmean(times_ms)))
    if extra:
        print("  %-34s %s" % ("", extra))


# -- fakes, shared with the shape the tests use -----------------------------

class SyntheticFrames:
    """
    Generated frames, so the benchmark needs no camera and no photographs.

    Structured noise rather than flat grey: a uniform frame is unrealistically
    cheap for a detector to reject.
    """

    def __init__(self, width=WIDTH, height=HEIGHT, count=8):
        rng = np.random.default_rng(seed=20260302)
        self.frames = [rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
                       for _ in range(count)]
        self.index = -1

    def next(self):
        self.index += 1
        return self.frames[self.index % len(self.frames)]


# The five YuNet landmarks as offsets within a face box: both eyes, the nose
# tip, and both mouth corners.
LANDMARK_TEMPLATE = np.array([
    [0.31, 0.36], [0.69, 0.36], [0.50, 0.57],
    [0.35, 0.75], [0.65, 0.75]], dtype=np.float32)


class FakeDetector:
    """
    N faces, drifting slowly, so tracking has real association work to do.

    The landmarks are REAL positions with a little jitter, not zeros. Zeros
    would be degenerate geometry, and the Step 9 liveness measurement would
    reject every sample before doing any work - making liveness look free by
    measuring only its failure path.
    """

    def __init__(self, faces=4, size=96):
        self.faces = faces
        self.size = size
        self.calls = 0
        self.rng = np.random.default_rng(20260909)

    def detect(self, frame):
        self.calls += 1
        drift = (self.calls % 20) - 10

        detections = []
        for index in range(self.faces):
            x = 40 + index * 140 + drift
            y = 120 + (index % 2) * 40
            points = LANDMARK_TEMPLATE + self.rng.normal(
                0.0, 0.02, LANDMARK_TEMPLATE.shape).astype(np.float32)
            landmarks = np.column_stack([x + points[:, 0] * self.size,
                                         y + points[:, 1] * self.size
                                         ]).astype(np.float32)
            detections.append(Detection(x, y, self.size, self.size, 0.94,
                                        landmarks=landmarks))
        return detections


class FakeRecognizer:
    """A match with no model behind it. Cost is measured separately in Step 4."""

    def __init__(self):
        self.calls = 0

    def recognize(self, frame, detection):
        self.calls += 1
        student = "STU%03d" % (detection.x // 130 + 1)
        return RecognitionResult(identity=student, name=student,
                                 similarity=0.81, status=STATUS_MATCH,
                                 box=detection.as_tuple())


def make_pipeline(detector, recognizer, liveness_mode="off"):
    """
    A pipeline for benchmarking.

    Liveness defaults to OFF here so sections 1-4 keep measuring what Step 8
    measures, and stay comparable with the figures Step 8 published. Section 5
    turns it on and off deliberately, which is the only place the comparison
    means anything.
    """
    from tracking_test import TrackingSession

    session = TrackingSession(detector, recognizer, tracker=PersonTracker(),
                              identity_manager=IdentityManager(),
                              timeline=PresenceTimeline())
    return LivePipeline(detector, recognizer, session_start=START,
                        session_end=END, tracking_session=session,
                        liveness_mode=liveness_mode)


# -- 1. detection floor -----------------------------------------------------

def benchmark_detection(seconds):
    from face_detector import YuNetFaceDetector

    print("\n1. Detection floor - real YuNet over synthetic frames")
    print("   Generated frames contain no faces, so recognition never runs.")
    print("   This is the frame budget, not a populated classroom.\n")

    detector = YuNetFaceDetector()
    pipeline = make_pipeline(detector, FakeRecognizer())
    frames = SyntheticFrames()

    times = []
    deadline = time.perf_counter() + seconds

    while time.perf_counter() < deadline:
        started = time.perf_counter()
        pipeline.process(frames.next())
        times.append((time.perf_counter() - started) * 1000.0)

    stage = pipeline.timings()
    report("whole frame", times,
           "detect %.2f ms, track %.3f ms, recognise %.3f ms"
           % (statistics.fmean(stage["detect_ms"]),
              statistics.fmean(stage["track_ms"]),
              statistics.fmean(stage["recognize_ms"])))
    print("  %-34s %d frames, %d recognition calls"
          % ("", len(times), pipeline.identities.recognition_calls))
    return times


# -- 2. live orchestration overhead ----------------------------------------

def benchmark_orchestration(seconds, faces):
    from live_session import LiveRuntime, LiveSessionWorker

    print("\n2. Live orchestration - fake detection and recognition")
    print("   Everything expensive removed, so what remains is the cost Step 8")
    print("   itself adds: the worker loop, the lock, the counters and the")
    print("   periodic presence republish.\n")

    detector = FakeDetector(faces)
    recognizer = FakeRecognizer()
    pipeline = make_pipeline(detector, recognizer)

    times = []
    deadline = time.perf_counter() + seconds
    frames = SyntheticFrames(count=2)
    runtime = LiveRuntime()
    runtime.begin("SES-BENCH")
    runtime.mark_running()

    republished = 0
    index = 0

    while time.perf_counter() < deadline:
        started = time.perf_counter()
        pipeline.process(frames.next())
        counts = pipeline.counts()
        runtime.note_frame(fps=15.0, active_tracks=counts["active_tracks"],
                           recognized_students=counts["recognized_students"],
                           unknown_tracks=counts["unknown_tracks"])
        index += 1
        if index % 15 == 0:
            runtime.publish_presence(pipeline.presence_view())
            republished += 1
        times.append((time.perf_counter() - started) * 1000.0)

    report("whole frame (%d faces)" % faces, times,
           "%d frames, %d presence republishes, %d recognition calls"
           % (len(times), republished, recognizer.calls))

    efficiency = pipeline.efficiency()
    print("  %-34s %d of %d possible recognitions (%.1f%% avoided)"
          % ("", efficiency["recognition_calls"],
             efficiency["naive_recognition_calls"],
             efficiency["reduction_percent"]))
    return times


# -- 3. presence view cost as a session ages -------------------------------

def benchmark_presence_view(faces):
    print("\n3. Presence view cost as the event list grows")
    print("   presence_view() rebuilds from the whole timeline, so it gets")
    print("   slower as a session runs. Measured, not assumed.\n")

    detector = FakeDetector(faces)
    pipeline = make_pipeline(detector, FakeRecognizer())
    frames = SyntheticFrames(count=2)

    for checkpoint in (500, 2000, 5000):
        while pipeline.frame_index + 1 < checkpoint:
            pipeline.process(frames.next())

        samples = []
        for _ in range(20):
            started = time.perf_counter()
            pipeline.presence_view()
            samples.append((time.perf_counter() - started) * 1000.0)

        print("  %-34s %8.2f ms   (%d frames, %d events)"
              % ("after %d frames" % checkpoint, statistics.fmean(samples),
                 checkpoint, len(pipeline.timeline.events)))


# -- 4. API latency while the camera runs ----------------------------------

def benchmark_api_latency(requests=50):
    print("\n4. API latency while a live session runs")
    print("   The claim under test: the camera loop does not block requests.\n")

    import shutil
    import tempfile

    import backend                                   # noqa: F401
    from fastapi.testclient import TestClient

    from attendance_session import AttendanceSession
    from backend.services import live_service, state
    from repositories import SessionRepository, StudentRepository

    directory = Path(tempfile.mkdtemp(prefix="classvision_bench_"))
    state.configure(directory / "bench.db")
    database = state.get_database()

    window_start = datetime.now(timezone.utc) - timedelta(minutes=1)
    session = AttendanceSession("SES-BENCH", "CLASS-BENCH", window_start,
                                window_start + timedelta(hours=1),
                                expected_students=["STU001"])
    StudentRepository(database).upsert("STU001", "Benchmark Student")
    SessionRepository(database).save(session)

    class PacedCamera:
        """Paced at 15 FPS, like the camera Step 1 measured."""

        def __init__(self):
            self.frames = SyntheticFrames(count=2)
            self.released = 0

        def read(self):
            time.sleep(1.0 / 15.0)
            return self.frames.next()

        def release(self):
            self.released += 1

    detector = FakeDetector(4)

    manager = live_service.LiveSessionManager(
        camera_factory=lambda config: PacedCamera(),
        pipeline_factory=lambda start, end, names: make_pipeline(
            detector, FakeRecognizer()))
    live_service.reset_live_manager(manager)

    from backend.app import app
    client = TestClient(app)

    try:
        client.post("/api/live/start", json={"session_id": "SES-BENCH"})
        time.sleep(0.5)

        latencies = []
        for _ in range(requests):
            started = time.perf_counter()
            client.get("/api/live/status")
            latencies.append((time.perf_counter() - started) * 1000.0)

        presence = []
        for _ in range(20):
            started = time.perf_counter()
            client.get("/api/live/presence")
            presence.append((time.perf_counter() - started) * 1000.0)

        print("  %-34s %8.2f ms avg   %8.2f ms p95   (%d requests)"
              % ("/api/live/status", statistics.fmean(latencies),
                 percentile(latencies, 0.95), len(latencies)))
        print("  %-34s %8.2f ms avg   %8.2f ms p95   (%d requests)"
              % ("/api/live/presence", statistics.fmean(presence),
                 percentile(presence, 0.95), len(presence)))

        before_stop = client.get("/api/live/status").json()
        stopped = client.post("/api/live/stop").json()
        print("  %-34s %d captures processed, %d dropped, %.1f FPS, %d "
              "segments stored"
              % ("finalised", stopped["processed_count"],
                 before_stop["dropped_count"], before_stop["fps"],
                 stopped["segments_saved"]))
    finally:
        live_service.reset_live_manager(None)
        client.close()
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


# -- 5. what liveness costs (Step 9) ---------------------------------------

def benchmark_liveness(seconds, faces, frames=20000):
    """
    The same synthetic load, twice: liveness off, then liveness on.

    Run back to back on the same machine in the same process, so the
    difference between the two columns is the Step 9 overhead and not a
    difference in conditions.

    A FIXED NUMBER OF FRAMES, not a fixed duration. Time-boxing the two runs
    would let the slower column process fewer frames and therefore make fewer
    recognition calls, which looks exactly like liveness having changed
    recognition scheduling when it has done no such thing. Same frames in,
    same work to compare.
    """
    from liveness_detector import LIVENESS_MODE_OFF, LIVENESS_MODE_REPORT

    print("\n5. Step 9 - what liveness costs")
    print("   The same synthetic load with liveness off and on. The right-hand")
    print("   column is what Step 9 added, measured rather than estimated.\n")

    results = {}

    for label, mode in (("liveness off", LIVENESS_MODE_OFF),
                        ("liveness on", LIVENESS_MODE_REPORT)):
        detector = FakeDetector(faces)
        recognizer = FakeRecognizer()

        from tracking_test import TrackingSession

        session = TrackingSession(detector, recognizer,
                                  tracker=PersonTracker(),
                                  identity_manager=IdentityManager(),
                                  timeline=PresenceTimeline())
        pipeline = LivePipeline(detector, recognizer, session_start=START,
                                session_end=END, tracking_session=session,
                                liveness_mode=mode)

        source = SyntheticFrames(count=4)
        times = []

        for _ in range(frames):
            started = time.perf_counter()
            pipeline.process(source.next())
            times.append((time.perf_counter() - started) * 1000.0)

        stage = pipeline.timings()
        results[label] = {
            "times": times,
            "detect": statistics.fmean(stage["detect_ms"]),
            "track": statistics.fmean(stage["track_ms"]),
            "recognition_calls": pipeline.identities.recognition_calls,
            "liveness_checks": pipeline.liveness_summary()["checks"],
            "frames": len(times),
        }

    print("  %-26s %14s %14s" % ("", "liveness off", "liveness on"))
    print("  " + "-" * 56)

    off, on = results["liveness off"], results["liveness on"]

    rows = [
        ("frame latency avg (ms)",
         statistics.fmean(off["times"]), statistics.fmean(on["times"])),
        ("frame latency p95 (ms)",
         percentile(off["times"], 0.95), percentile(on["times"], 0.95)),
        ("effective FPS",
         1000.0 / statistics.fmean(off["times"]),
         1000.0 / statistics.fmean(on["times"])),
        ("detection (ms)", off["detect"], on["detect"]),
        ("tracking (ms)", off["track"], on["track"]),
    ]

    for name, left, right in rows:
        print("  %-26s %14.3f %14.3f" % (name, left, right))

    print("  %-26s %14d %14d" % ("recognition calls",
                                 off["recognition_calls"],
                                 on["recognition_calls"]))
    print("  %-26s %14.4f %14.4f" % ("recognition calls / frame",
                                     off["recognition_calls"] / off["frames"],
                                     on["recognition_calls"] / on["frames"]))
    print("  %-26s %14d %14d" % ("liveness checks",
                                 off["liveness_checks"],
                                 on["liveness_checks"]))
    print("  %-26s %14d %14d" % ("frames processed",
                                 off["frames"], on["frames"]))

    overhead = statistics.fmean(on["times"]) - statistics.fmean(off["times"])
    print("")
    print("  Liveness overhead per frame with %d faces: %.3f ms"
          % (faces, overhead))
    print("  Per face per frame: %.3f ms" % (overhead / max(faces, 1)))
    print("  Against the 65 ms frame budget measured in Step 1: %.1f%%"
          % (overhead / 65.0 * 100.0))
    print("")
    print("  Recognition calls must be IDENTICAL in both columns. Step 9 was")
    print("  not allowed to change recognition scheduling, and a difference")
    print("  here would mean it had.")

    if off["recognition_calls"] == on["recognition_calls"]:
        print("  Identical: %d in both columns over %d frames."
              % (off["recognition_calls"], off["frames"]))
        return 0

    print("  WARNING: they differ (%d vs %d) - liveness has changed "
          "recognition." % (off["recognition_calls"], on["recognition_calls"]))
    return 1


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Measure the Step 8 live session layer (synthetic).")
    parser.add_argument("--seconds", type=float, default=4.0,
                        help="how long each timed loop runs")
    parser.add_argument("--faces", type=int, default=4,
                        help="synthetic faces per frame in the fake detector")
    parser.add_argument("--skip-detection", action="store_true",
                        help="skip the real-YuNet section (no model needed)")
    arguments = parser.parse_args()

    print("=" * 72)
    print("ClassVision AI - Steps 8-9 live session benchmark")
    print("=" * 72)
    print("SYNTHETIC. Generated frames, invented students, one machine.")
    print("This says nothing about classroom accuracy or real-world behaviour.")

    if not arguments.skip_detection:
        try:
            benchmark_detection(arguments.seconds)
        except Exception as error:
            print("\n1. Detection floor - SKIPPED (%s). Run "
                  "python ai/download_models.py first."
                  % type(error).__name__)

    benchmark_orchestration(arguments.seconds, arguments.faces)
    benchmark_presence_view(arguments.faces)
    benchmark_api_latency()
    benchmark_liveness(arguments.seconds, arguments.faces)

    print("\nDone. Every number above is synthetic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
