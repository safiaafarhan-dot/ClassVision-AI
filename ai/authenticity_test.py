"""
ClassVision AI - Authenticity Calibration, Benchmark and Live Demo (Step 10)
----------------------------------------------------------------------------
Where the Step 10 thresholds come from, what they cost, and what they look
like on a real camera.

    STEP 10 IS A RESEARCH BASELINE. No real manipulated video has ever been
    shown to this system. Accuracy, FAR, FRR, APCER, BPCER and ACER are all
    "Not evaluated".

Three modes

    --calibrate   measure the three cues on real webcam video and on
                  controlled synthetic sequences, and report whether the
                  thresholds separate them. This is the reproducible source
                  for every number quoted in the README.

    --benchmark   measure what authenticity analysis costs per frame.

    (default)     live webcam demo, drawing identity, liveness and
                  authenticity side by side so the three can be seen to be
                  independent.

What the synthetic sequences honestly represent

    "coupled"     a face region whose brightness tracks its surroundings,
                  as one camera image does. This is a faithful model of a
                  real capture, and the real-camera run confirms it.

    "decoupled"   a face region driven independently of its surroundings.
                  That is a faithful model of a region being GENERATED
                  per-frame, and an unfaithful model of almost everything
                  else - notably, it does not model a real face swap's
                  texture, its blending, or its compression history.

    The coupling cue is anchored on real video: a real webcam genuinely does
    couple face and background, and --calibrate measures that rather than
    assuming it.

    The texture and chroma cues are NOT anchored on real manipulated video,
    because none was available. They are models of generator behaviour. That
    is exactly why the decision rule refuses to let either of them decide
    anything on its own.

Run it:
    python ai/authenticity_test.py --calibrate
    python ai/authenticity_test.py --benchmark
    python ai/authenticity_test.py                 # needs a webcam
"""

import argparse
import statistics
import sys
import time
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

import numpy as np  # noqa: E402

from authenticity_detector import (  # noqa: E402
    AUTHENTICITY_AUTHENTIC,
    AUTHENTICITY_SUSPICIOUS,
    AUTHENTICITY_UNCERTAIN,
    CHROMA_SUSPICIOUS_MIN,
    COUPLING_AUTHENTIC_MIN,
    COUPLING_SUSPICIOUS_MAX,
    SHARPNESS_AUTHENTIC_MIN,
    SHARPNESS_SUSPICIOUS_MAX,
    AuthenticityDetector,
)
from authenticity_stability import AuthenticityManager  # noqa: E402
from face_detector import Detection  # noqa: E402
from paths import enable_safe_console  # noqa: E402

warnings.filterwarnings("ignore")

FRAME_WIDTH = 640
FRAME_HEIGHT = 480

FACE_BOX = (250, 170, 130, 130)

LANDMARK_TEMPLATE = np.array([
    [0.31, 0.36], [0.69, 0.36], [0.50, 0.57],
    [0.35, 0.75], [0.65, 0.75]], dtype=np.float32)


def _face_patch(size, seed, detail=1.0, brightness=0.0):
    """Face-like: smooth shading plus fine grain, at a given brightness."""
    rng = np.random.default_rng(seed)
    axis = np.linspace(-1.0, 1.0, size, dtype=np.float32)
    grid_y, grid_x = np.meshgrid(axis, axis, indexing="ij")

    smooth = (138.0
              + 52.0 * np.exp(-(grid_x ** 2 * 1.6 + grid_y ** 2 * 1.1))
              - 22.0 * grid_y)
    grain = rng.normal(0.0, 5.0 * detail, (size, size)).astype(np.float32)

    return np.clip(smooth + grain + brightness, 0, 255).astype(np.float32)


def synthetic_sequence(kind, frames=40, box=FACE_BOX, seed=17):
    """
    Yield (frame, detection, index) for a controlled sequence.

    kind "coupled"    face brightness tracks the background, as one camera
                      image does
    kind "decoupled"  face brightness is driven independently of the
                      background - a model of per-frame generation
    kind "inverted"   face brightness moves OPPOSITE to the background - a
                      model of a composite whose exposure compensation runs
                      the wrong way, and the only case this cue can catch
    kind "smooth"     coupled, but the face carries far less detail than its
                      surroundings
    kind "tiny"       a face too small to compare against its context
    """
    rng = np.random.default_rng(seed)
    x, y, width, height = box

    for index in range(frames):
        # One global illumination signal for the whole scene. A real camera's
        # auto-exposure and the room's lighting move everything together.
        scene = 26.0 * np.sin(index * 0.42) + rng.normal(0.0, 1.2)

        background = rng.normal(96.0 + scene, 16.0, (FRAME_HEIGHT, FRAME_WIDTH))
        frame = np.repeat(
            np.clip(background, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)

        if kind == "decoupled":
            # The face follows its OWN illumination, unrelated to the scene.
            face_light = 26.0 * np.sin(index * 1.93 + 2.1)
            detail = 1.0
        elif kind == "inverted":
            # The face moves opposite to the scene. No single camera produces
            # this; a badly composited region can.
            face_light = -scene
            detail = 0.12
        elif kind == "smooth":
            face_light = scene
            detail = 0.12
        else:
            face_light = scene
            detail = 1.0

        box_now = (x, y, width, height) if kind != "tiny" else (x, y, 30, 30)

        patch = _face_patch(160, seed + index, detail=detail,
                            brightness=face_light)
        resized = patch[
            np.minimum((np.arange(box_now[3]) * 160) // box_now[3], 159)
        ][:, np.minimum((np.arange(box_now[2]) * 160) // box_now[2], 159)]

        frame[box_now[1]:box_now[1] + box_now[3],
              box_now[0]:box_now[0] + box_now[2]] = np.repeat(
            np.clip(resized, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)

        points = LANDMARK_TEMPLATE + rng.normal(0.0, 0.02,
                                                LANDMARK_TEMPLATE.shape)
        landmarks = np.column_stack([
            box_now[0] + points[:, 0] * box_now[2],
            box_now[1] + points[:, 1] * box_now[3]]).astype(np.float32)

        yield (frame,
               Detection(box_now[0], box_now[1], box_now[2], box_now[3], 0.95,
                         landmarks=landmarks),
               index)


def measure(kind, frames=40, seed=17):
    detector = AuthenticityDetector()
    for frame, detection, _ in synthetic_sequence(kind, frames, seed=seed):
        detector.observe(1, frame, detection)
    return detector.measurements(1), detector.assess(1)


# -- real camera anchor ----------------------------------------------------

def measure_real_camera(seconds=8.0, camera_index=0):
    """
    Measure the cues on REAL webcam video, using the real face detector.

    This is the anchor for the coupling threshold. "Face and background move
    together under one camera" is a property of a real capture, and it can
    simply be observed rather than assumed.

    Returns None when no camera or no face is available - which is not a
    failure, just an absence, and is reported as such.
    """
    try:
        from camera_test import open_camera
        from face_detector import YuNetFaceDetector
    except Exception as error:
        print("  real camera unavailable (%s)" % type(error).__name__)
        return None

    camera = open_camera(camera_index, FRAME_WIDTH, FRAME_HEIGHT)
    if camera is None:
        print("  no camera at index %d" % camera_index)
        return None

    detector = YuNetFaceDetector()
    analyser = AuthenticityDetector()
    seen = 0
    deadline = time.time() + seconds

    try:
        while time.time() < deadline:
            ok, frame = camera.read()
            if not ok:
                continue
            for detection in detector.detect(frame):
                analyser.observe(1, frame, detection)
                seen += 1
                break
    finally:
        camera.release()

    if seen < 8:
        print("  a real camera was open but no face was in front of it "
              "(%d usable observations) - the coupling anchor needs a face"
              % seen)
        return None

    return analyser.measurements(1), analyser.assess(1), seen


# -- calibration -----------------------------------------------------------

def calibrate(repeats=10, frames=40, camera_index=0, seconds=8.0):
    print("=" * 74)
    print("Step 10 authenticity - cue calibration")
    print("=" * 74)
    print("The coupling cue is anchored on REAL webcam video where a face is")
    print("available. The texture and chroma cues are NOT anchored on real")
    print("manipulated video, because none was available - they are models of")
    print("generator behaviour, and no accuracy figure is derived from them.\n")

    print("1. REAL webcam (the coupling anchor)")
    real = measure_real_camera(seconds, camera_index)
    if real:
        cues, verdict, seen = real
        print("     coupling %.4f   sharpness %.4f   chroma %.2f   (%d obs)"
              % (cues["coupling"], cues["sharpness_ratio"],
                 cues["chroma_offset"], seen))
        print("     verdict: %s" % verdict.state)
    print("")

    print("2. SYNTHETIC sequences")
    results = {}
    for kind in ("coupled", "decoupled", "inverted", "smooth"):
        couplings, sharpnesses, chromas, states = [], [], [], []

        for repeat in range(repeats):
            cues, verdict = measure(kind, frames, seed=17 + repeat * 11)
            if cues is None or cues["coupling"] is None:
                continue
            couplings.append(cues["coupling"])
            sharpnesses.append(cues["sharpness_ratio"])
            chromas.append(cues["chroma_offset"])
            states.append(verdict.state)

        results[kind] = {"coupling": couplings, "sharpness": sharpnesses,
                         "states": states}

        if not couplings:
            print("     %-10s no usable measurement" % kind)
            continue

        print("     %-10s coupling %+.4f..%+.4f   sharpness %.4f..%.4f"
              % (kind, min(couplings), max(couplings),
                 min(sharpnesses), max(sharpnesses)))
        print("     %-10s verdicts: %s" % ("", _counts(states)))

    print("\n3. Thresholds in ai/authenticity_detector.py")
    print("     COUPLING_AUTHENTIC_MIN    %+.4f" % COUPLING_AUTHENTIC_MIN)
    print("     COUPLING_SUSPICIOUS_MAX   %+.4f" % COUPLING_SUSPICIOUS_MAX)
    print("     SHARPNESS_AUTHENTIC_MIN    %.4f" % SHARPNESS_AUTHENTIC_MIN)
    print("     SHARPNESS_SUSPICIOUS_MAX   %.4f" % SHARPNESS_SUSPICIOUS_MAX)
    print("     CHROMA_SUSPICIOUS_MIN      %.2f" % CHROMA_SUSPICIOUS_MIN)

    coupled = results.get("coupled", {}).get("coupling") or []
    decoupled = results.get("decoupled", {}).get("coupling") or []
    inverted = results.get("inverted", {}).get("coupling") or []

    separated = False
    if coupled and inverted:
        print("\n4. What the coupling cue CAN separate")
        print("     weakest coupled sequence    %+.4f  (needs >= %+.4f)"
              % (min(coupled), COUPLING_AUTHENTIC_MIN))
        print("     strongest inverted          %+.4f  (needs <= %+.4f)"
              % (max(inverted), COUPLING_SUSPICIOUS_MAX))
        separated = (min(coupled) > COUPLING_AUTHENTIC_MIN
                     and max(inverted) < COUPLING_SUSPICIOUS_MAX)
        print("     separated with margin: %s" % ("yes" if separated else "NO"))

    if decoupled:
        print("\n5. What it CANNOT separate - stated, not hidden")
        print("     decoupled sequences ran %+.4f .. %+.4f, which sits inside"
              % (min(decoupled), max(decoupled)))
        print("     the UNCERTAIN band between %+.4f and %+.4f."
              % (COUPLING_SUSPICIOUS_MAX, COUPLING_AUTHENTIC_MIN))
        print("     A merely INDEPENDENT face region is NOT flagged; only an")
        print("     anti-correlated one is. A static, evenly lit REAL scene")
        print("     also lands near zero, which is why the suspicious")
        print("     threshold cannot be raised to catch the decoupled case")
        print("     without flagging ordinary still students.")

    print("\nWhat this does NOT establish: behaviour on a real face swap, a")
    print("real neural reenactment, a real replayed video, or any manipulation")
    print("produced by somebody trying to defeat it. Not evaluated.")

    return 0 if separated else 1


def _counts(states):
    tally = {}
    for state in states:
        tally[state] = tally.get(state, 0) + 1
    return ", ".join("%s %d" % (k, tally[k]) for k in sorted(tally))


# -- benchmark -------------------------------------------------------------

def benchmark(frames=400):
    print("=" * 74)
    print("Step 10 authenticity - cost per observation (SYNTHETIC)")
    print("=" * 74)

    detector = AuthenticityDetector()
    manager = AuthenticityManager()
    sequence = list(synthetic_sequence("coupled", frames=min(frames, 60)))

    observe_times, assess_times = [], []

    for index in range(frames):
        frame, detection, _ = sequence[index % len(sequence)]

        started = time.perf_counter()
        detector.observe(1, frame, detection)
        observe_times.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        verdict = detector.assess(1)
        assess_times.append((time.perf_counter() - started) * 1000.0)

        manager.update(1, verdict, index)

    total = [a + b for a, b in zip(observe_times, assess_times)]

    print("  observe (measure one frame)   %.3f ms avg   %.3f ms p95"
          % (statistics.fmean(observe_times), _p95(observe_times)))
    print("  assess  (verdict from window) %.3f ms avg   %.3f ms p95"
          % (statistics.fmean(assess_times), _p95(assess_times)))
    print("  combined per checked face     %.3f ms avg   %.3f ms p95"
          % (statistics.fmean(total), _p95(total)))
    print("")
    print("  A face is checked every 3rd frame, so the per-FRAME cost of one")
    print("  tracked face is about %.3f ms." % (statistics.fmean(total) / 3.0))
    print("  Step 5 measured detection at ~13 ms and the frame budget at")
    print("  ~65 ms, which is the comparison that matters.")
    return 0


def _p95(values):
    ordered = sorted(values)
    return ordered[int(round(0.95 * (len(ordered) - 1)))]


# -- live demo -------------------------------------------------------------

def live_demo(camera_index, seconds):
    """Identity, liveness and authenticity drawn side by side on real video."""
    import cv2

    from camera_test import open_camera
    from face_detector import YuNetFaceDetector
    from liveness_detector import LivenessDetector
    from liveness_stability import LivenessManager
    from person_tracker import PersonTracker

    detector = YuNetFaceDetector()
    tracker = PersonTracker()
    liveness, liveness_manager = LivenessDetector(), LivenessManager()
    authenticity = AuthenticityDetector()
    authenticity_manager = AuthenticityManager()

    camera = open_camera(camera_index, FRAME_WIDTH, FRAME_HEIGHT)
    if camera is None:
        print("ERROR: could not open camera %d. Try "
              "python ai/camera_test.py --list" % camera_index)
        return 1

    colours = {
        AUTHENTICITY_AUTHENTIC: (0, 220, 120),
        AUTHENTICITY_SUSPICIOUS: (0, 80, 255),
        AUTHENTICITY_UNCERTAIN: (0, 190, 240),
    }

    print("Authenticity demo. Q or ESC to quit.")
    print("RESEARCH BASELINE - not a validated deepfake detector.")

    deadline = time.time() + seconds if seconds else None
    frame_index = -1

    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                break

            frame_index += 1
            detections = detector.detect(frame)
            tracker.update(detections, frame_index)

            for track in tracker.tracks:
                if not track.is_visible:
                    continue

                detection = None
                for candidate in detections:
                    if candidate.as_tuple() == track.box:
                        detection = candidate
                        break

                if detection is not None:
                    if liveness_manager.should_check(track, frame_index):
                        liveness.observe(track.track_id, frame, detection)
                        liveness_manager.update(
                            track.track_id, liveness.assess(track.track_id),
                            frame_index)
                    if authenticity_manager.should_check(track, frame_index):
                        authenticity.observe(track.track_id, frame, detection)
                        authenticity_manager.update(
                            track.track_id,
                            authenticity.assess(track.track_id), frame_index)

                live_record = liveness_manager.get(track.track_id)
                source_record = authenticity_manager.get(track.track_id)
                source_state = (source_record.effective_state
                                if source_record else "UNKNOWN")
                colour = colours.get(source_state, (170, 170, 170))

                x, y, width, height = track.box
                cv2.rectangle(frame, (x, y), (x + width, y + height), colour, 2)
                cv2.putText(
                    frame,
                    "T%d | liveness %s | authenticity %s"
                    % (track.track_id,
                       live_record.effective_state if live_record else "UNKNOWN",
                       source_state),
                    (x, max(y - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    colour, 1, cv2.LINE_AA)

            for retired in tracker.take_removed():
                liveness.forget(retired.track_id)
                liveness_manager.forget(retired.track_id)
                authenticity.forget(retired.track_id)
                authenticity_manager.forget(retired.track_id)

            cv2.putText(frame,
                        "RESEARCH BASELINE - authenticity is not validated",
                        (10, FRAME_HEIGHT - 14), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 190, 240), 1, cv2.LINE_AA)
            cv2.imshow("ClassVision AI - Authenticity Test", frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if deadline and time.time() > deadline:
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()
        print("Camera released.")

    print(authenticity_manager.summary())
    return 0


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Calibrate, benchmark or demonstrate the Step 10 "
                    "authenticity baseline.")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--frames", type=int, default=40)
    arguments = parser.parse_args()

    if arguments.calibrate:
        return calibrate(frames=arguments.frames,
                         camera_index=arguments.camera,
                         seconds=arguments.seconds or 8.0)

    if arguments.benchmark:
        return benchmark()

    return live_demo(arguments.camera, arguments.seconds)


if __name__ == "__main__":
    sys.exit(main())
