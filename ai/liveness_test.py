"""
ClassVision AI - Liveness Calibration, Benchmark and Live Demo (Step 9)
-----------------------------------------------------------------------
Where the Step 9 thresholds come from, what they cost, and what they look like
on a real camera.

    STEP 9 IS A RESEARCH PROTOTYPE. Everything calibrated here is calibrated
    on SYNTHETIC sequences. No real presentation attack has ever been shown to
    this system, and no accuracy, FAR, FRR, APCER, BPCER or ACER is claimed.

Three modes

    --calibrate   measure the three cues on controlled synthetic sequences and
                  report whether the thresholds separate them, with margins.
                  This is the reproducible source for every number quoted in
                  the README.

    --benchmark   measure what liveness costs per frame, so the Step 8
                  performance claims can be re-checked rather than assumed.

    (default)     live webcam demo. Draws each track's liveness state next to
                  its identity, so the two can be seen to be independent.

What the synthetic sequences honestly represent

    "live"   a face whose landmark geometry DEFORMS between observations, with
             texture that fluctuates. That is the physical property of a head:
             it is a moving 3D object.

    "spoof"  a STATIC presented image - a photograph or a paused screen - with
             optional RIGID movement of the object holding it, and texture
             detail reduced as a recapture would reduce it.

    The motion cue is modelled faithfully. A flat photograph genuinely has zero
    non-rigid landmark deformation no matter how it is waved about, and that is
    exactly what the synthetic sequence reproduces.

    The texture cue is NOT modelled faithfully. Real print grain, real display
    moire and real sensor noise are not simulated; "detail loss" is applied as
    a blur, which is a model of the effect rather than a measurement of it. The
    texture thresholds are therefore the least transferable numbers in Step 9,
    which is precisely why the decision rule refuses to let texture decide
    anything on its own.

    There is unavoidable circularity in calibrating on data you generated. It
    is stated here rather than hidden, and it is the reason the README reports
    "Not evaluated" for every accuracy metric.

Run it:
    python ai/liveness_test.py --calibrate
    python ai/liveness_test.py --benchmark
    python ai/liveness_test.py                  # needs a webcam
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

from face_detector import Detection  # noqa: E402
from liveness_detector import (  # noqa: E402
    DETAIL_LIVE_MIN,
    DETAIL_SPOOF_MAX,
    LIVENESS_LIVE,
    LIVENESS_SPOOF,
    LIVENESS_UNCERTAIN,
    MOTION_LIVE_MIN,
    MOTION_SPOOF_MAX,
    VARIATION_SPOOF_MAX,
    LivenessDetector,
)
from liveness_stability import LivenessManager  # noqa: E402
from paths import enable_safe_console  # noqa: E402

warnings.filterwarnings("ignore")

FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# Where the synthetic face sits, and how big it is. 120 px is a plausible
# classroom face at 640x480.
FACE_BOX = (250, 170, 120, 120)

# The five YuNet landmarks, as offsets within the face box: both eyes, the
# nose tip, and both mouth corners.
LANDMARK_TEMPLATE = np.array([
    [0.31, 0.36],   # right eye
    [0.69, 0.36],   # left eye
    [0.50, 0.57],   # nose tip
    [0.35, 0.75],   # right mouth corner
    [0.65, 0.75],   # left mouth corner
], dtype=np.float32)


def _blur(image, passes=1):
    """A 3x3 box blur, applied in numpy. Models recapture detail loss."""
    result = image.astype(np.float32)

    for _ in range(passes):
        padded = np.pad(result, 1, mode="edge")
        stacked = np.stack([
            padded[0:-2, 0:-2], padded[0:-2, 1:-1], padded[0:-2, 2:],
            padded[1:-1, 0:-2], padded[1:-1, 1:-1], padded[1:-1, 2:],
            padded[2:, 0:-2], padded[2:, 1:-1], padded[2:, 2:],
        ])
        result = stacked.mean(axis=0)

    return result


def synthetic_face_patch(size=160, seed=0, detail=1.0):
    """
    A face-like patch: smooth shading plus fine texture.

    Smooth gradients dominate, as they do on a real face, with a modest layer
    of fine grain on top. Pure noise would be unrealistic - it would give a
    detail figure no real face could reach.
    """
    rng = np.random.default_rng(seed)
    axis = np.linspace(-1.0, 1.0, size, dtype=np.float32)
    grid_y, grid_x = np.meshgrid(axis, axis, indexing="ij")

    # Low-frequency shading: a lit oval, roughly how a face is illuminated.
    smooth = (140.0
              + 55.0 * np.exp(-(grid_x ** 2 * 1.6 + grid_y ** 2 * 1.1))
              - 25.0 * grid_y
              + 12.0 * np.cos(grid_x * 2.2))

    # Fine grain: skin texture, eyelashes, stubble, fabric. The amplitude was
    # chosen so texture_detail() on the result lands inside the range measured
    # on this machine's REAL camera (0.684 - 2.614), rather than at some value
    # no real frame reaches.
    grain = rng.normal(0.0, 4.5 * detail, (size, size)).astype(np.float32)

    return np.clip(smooth + grain, 0, 255).astype(np.float32)


def _place(frame, patch, box):
    """Paste a grayscale patch into a 3-channel frame, resampled to the box."""
    x, y, width, height = box
    resized = patch[
        np.minimum((np.arange(height) * patch.shape[0]) // height,
                   patch.shape[0] - 1)
    ][:, np.minimum((np.arange(width) * patch.shape[1]) // width,
                    patch.shape[1] - 1)]

    frame[y:y + height, x:x + width] = np.repeat(
        np.clip(resized, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)
    return frame


def synthetic_sequence(kind, frames=40, box=FACE_BOX, seed=11, drift=True):
    """
    Yield (frame, detection) pairs for a controlled sequence.

    kind "live"   geometry deforms; texture is fine and fluctuates
    kind "spoof"  geometry is rigid; texture is blurred and constant
    kind "small"  a face too small to measure - tests the unusable path
    kind "still"  a real face that barely moves: ambiguous on purpose
    """
    rng = np.random.default_rng(seed)
    x, y, width, height = box

    # The spoof sequence is ONE image, reused. That is what a photograph is.
    # It is blurred twice, this project's model of recapture detail loss.
    frozen_patch = _blur(synthetic_face_patch(160, seed=seed, detail=1.0),
                         passes=2)

    for index in range(frames):
        frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), 70, dtype=np.uint8)
        background = rng.normal(70, 6, (FRAME_HEIGHT, FRAME_WIDTH))
        frame[:, :, 0] = np.clip(background, 0, 255).astype(np.uint8)
        frame[:, :, 1] = frame[:, :, 0]
        frame[:, :, 2] = frame[:, :, 0]

        # Rigid movement of the whole face: applied to BOTH kinds, because an
        # attacker holding a photo moves it. If rigid motion were only in the
        # live sequence, the motion cue would be measuring the wrong thing.
        shift_x = int(round(6.0 * np.sin(index * 0.35))) if drift else 0
        shift_y = int(round(4.0 * np.cos(index * 0.29))) if drift else 0

        left = max(min(x + shift_x, FRAME_WIDTH - width - 1), 0)
        top = max(min(y + shift_y, FRAME_HEIGHT - height - 1), 0)

        if kind == "spoof":
            # Sensor noise is added on top of the frozen image, because a
            # camera pointed at a photograph still produces noise. Without it
            # the texture-stability cue would separate the two sequences
            # perfectly and for the wrong reason - it would be detecting a
            # digitally identical frame, which is not the attack being
            # modelled.
            patch = frozen_patch + rng.normal(0.0, 1.6, frozen_patch.shape)
            jitter = 0.0
        else:
            # A real face's texture statistic does not hold still: focus,
            # exposure and shadow all move it. The measured frame-to-frame
            # variation on this machine's camera was 0.33 or more, so the
            # synthetic live face modulates its fine grain rather than
            # regenerating an identical amount of it every frame.
            swing = 1.0 + 0.45 * float(np.sin(index * 0.8))
            patch = synthetic_face_patch(160, seed=seed + index, detail=swing)
            jitter = 0.0015 if kind == "still" else 0.02

        gray = np.clip(patch, 0, 255)
        colour = np.stack([gray, gray, gray], axis=2).astype(np.uint8)

        if kind == "small":
            small_box = (left, top, 24, 24)
            _place(frame, colour[:, :, 0].astype(np.float32), small_box)
            box_now = small_box
        else:
            _place(frame, colour[:, :, 0].astype(np.float32),
                   (left, top, width, height))
            box_now = (left, top, width, height)

        # Landmarks. Non-rigid jitter is what separates a head from a print.
        points = LANDMARK_TEMPLATE.copy()
        if jitter:
            points = points + rng.normal(0.0, jitter, points.shape).astype(
                np.float32)

        landmarks = np.column_stack([
            box_now[0] + points[:, 0] * box_now[2],
            box_now[1] + points[:, 1] * box_now[3],
        ]).astype(np.float32)

        detection = Detection(box_now[0], box_now[1], box_now[2], box_now[3],
                              0.95, landmarks=landmarks)
        detection_frame_index = index

        yield frame, detection, detection_frame_index


# -- calibration ------------------------------------------------------------

def measure_sequence(kind, frames=40, seed=11, drift=True):
    """Run one synthetic sequence through the detector and read the cues."""
    detector = LivenessDetector()

    for frame, detection, index in synthetic_sequence(kind, frames, seed=seed,
                                                      drift=drift):
        detector.observe(1, frame, detection)

    return detector.measurements(1), detector.assess(1)


def calibrate(repeats=12, frames=40):
    print("=" * 74)
    print("Step 9 liveness - cue calibration on SYNTHETIC sequences")
    print("=" * 74)
    print("These sequences were generated by this file. Calibrating on")
    print("generated data is circular, and is reported as such: no accuracy")
    print("figure is derived from it and none is claimed.\n")

    results = {}

    for kind in ("live", "spoof", "still"):
        motions, details, variations, states = [], [], [], []

        for repeat in range(repeats):
            cues, verdict = measure_sequence(kind, frames, seed=11 + repeat * 7)
            if cues is None:
                continue
            motions.append(cues["motion"])
            details.append(cues["detail"])
            variations.append(cues["variation"])
            states.append(verdict.state)

        results[kind] = {
            "motion": motions, "detail": details,
            "variation": variations, "states": states,
        }

        print("  %-6s  motion %.5f - %.5f   detail %.4f - %.4f   "
              "variation %.4f - %.4f"
              % (kind, min(motions), max(motions), min(details), max(details),
                 min(variations), max(variations)))
        print("  %-6s  verdicts: %s" % ("", _counts(states)))

    print("\n  Thresholds in ai/liveness_detector.py")
    print("    MOTION_LIVE_MIN      %.5f" % MOTION_LIVE_MIN)
    print("    MOTION_SPOOF_MAX     %.5f" % MOTION_SPOOF_MAX)
    print("    DETAIL_LIVE_MIN      %.5f" % DETAIL_LIVE_MIN)
    print("    DETAIL_SPOOF_MAX     %.5f" % DETAIL_SPOOF_MAX)
    print("    VARIATION_SPOOF_MAX  %.5f" % VARIATION_SPOOF_MAX)

    live_motion = min(results["live"]["motion"])
    spoof_motion = max(results["spoof"]["motion"])

    print("\n  Margins on the cue that carries the decision (motion)")
    print("    quietest synthetic live sequence   %.5f  (needs >= %.5f)"
          % (live_motion, MOTION_LIVE_MIN))
    print("    liveliest synthetic spoof sequence %.5f  (needs <= %.5f)"
          % (spoof_motion, MOTION_SPOOF_MAX))
    separated = live_motion > MOTION_LIVE_MIN and spoof_motion < MOTION_SPOOF_MAX
    print("    separated with margin: %s" % ("yes" if separated else "NO"))

    print("\n  What this does NOT establish: behaviour on a real photograph,")
    print("  a real screen, a replayed video, a mask, or any attack performed")
    print("  by somebody trying to defeat it. Not evaluated.")

    return 0 if separated else 1


def _counts(states):
    tally = {}
    for state in states:
        tally[state] = tally.get(state, 0) + 1
    return ", ".join("%s %d" % (k, tally[k]) for k in sorted(tally))


# -- benchmark --------------------------------------------------------------

def benchmark(frames=300):
    print("=" * 74)
    print("Step 9 liveness - cost per observation (SYNTHETIC)")
    print("=" * 74)

    detector = LivenessDetector()
    manager = LivenessManager()

    sequence = list(synthetic_sequence("live", frames=min(frames, 60)))
    observe_times = []
    assess_times = []

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
    print("  tracked face is about %.3f ms."
          % (statistics.fmean(total) / 3.0))
    print("  Step 5 measured detection at ~13 ms and the frame budget at")
    print("  ~65 ms, so this is the comparison that matters.")
    return 0


def _p95(values):
    ordered = sorted(values)
    return ordered[int(round(0.95 * (len(ordered) - 1)))]


# -- live demo --------------------------------------------------------------

def live_demo(camera_index, seconds):
    """
    Show liveness next to identity on a real camera.

    Deliberately draws BOTH, because the point Step 9 keeps making is that
    they are different facts.
    """
    import cv2

    from camera_test import open_camera
    from face_detector import YuNetFaceDetector
    from person_tracker import PersonTracker

    detector = YuNetFaceDetector()
    tracker = PersonTracker()
    liveness = LivenessDetector()
    manager = LivenessManager()

    camera = open_camera(camera_index, FRAME_WIDTH, FRAME_HEIGHT)
    if camera is None:
        print("ERROR: could not open camera %d. Try "
              "python ai/camera_test.py --list" % camera_index)
        return 1

    colours = {
        LIVENESS_LIVE: (0, 220, 120),
        LIVENESS_SPOOF: (0, 80, 255),
        LIVENESS_UNCERTAIN: (0, 190, 240),
    }

    print("Liveness demo. Q or ESC to quit.")
    print("This is a RESEARCH PROTOTYPE. Do not read it as a security control.")

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

                if detection is not None and \
                        manager.should_check(track, frame_index):
                    liveness.observe(track.track_id, frame, detection)
                    manager.update(track.track_id,
                                   liveness.assess(track.track_id),
                                   frame_index)

                record = manager.get(track.track_id)
                state = record.effective_state if record else "UNKNOWN"
                colour = colours.get(state, (170, 170, 170))

                x, y, width, height = track.box
                cv2.rectangle(frame, (x, y), (x + width, y + height), colour, 2)
                cv2.putText(frame,
                            "Track %d | liveness %s%s"
                            % (track.track_id, state,
                               " (confirmed)" if record and record.confirmed
                               else ""),
                            (x, max(y - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, colour, 1, cv2.LINE_AA)

            for retired in tracker.take_removed():
                manager.forget(retired.track_id)
                liveness.forget(retired.track_id)

            cv2.putText(frame, "RESEARCH PROTOTYPE - liveness is not validated",
                        (10, FRAME_HEIGHT - 14), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 190, 240), 1, cv2.LINE_AA)
            cv2.imshow("ClassVision AI - Liveness Test", frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if deadline and time.time() > deadline:
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()
        print("Camera released.")

    print(manager.summary())
    return 0


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Calibrate, benchmark or demonstrate the Step 9 liveness "
                    "baseline.")
    parser.add_argument("--calibrate", action="store_true",
                        help="measure the cues on synthetic sequences")
    parser.add_argument("--benchmark", action="store_true",
                        help="measure the cost per observation")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=0,
                        help="stop the live demo after this long")
    parser.add_argument("--frames", type=int, default=40)
    arguments = parser.parse_args()

    if arguments.calibrate:
        return calibrate(frames=arguments.frames)

    if arguments.benchmark:
        return benchmark()

    return live_demo(arguments.camera, arguments.seconds)


if __name__ == "__main__":
    sys.exit(main())
