"""
ClassVision AI - Live Recognition Test (Step 4)
-----------------------------------------------
Opens the webcam, finds faces, and says which enrolled student each one is -
or says Unknown.

    Step 4 performs identity verification only. It does not mark attendance
    or calculate presence duration.

No attendance status is shown anywhere in this tool, on purpose.

Controls
    Q or ESC   quit
    H          hide / show the info panel
    S          turn the stability filter on / off
    R          reload the enrolled students from disk

Examples
    python ai/recognition_test.py
    python ai/recognition_test.py --threshold 0.5
    python ai/recognition_test.py --no-stability
    python ai/recognition_test.py --benchmark
"""

import argparse
import sys
import time
from collections import deque

import cv2

from camera_test import open_camera
from face_detector import YuNetFaceDetector
from face_recognizer import (
    DEFAULT_RECOGNITION_THRESHOLD,
    STATUS_MATCH,
    STATUS_NO_ENROLLMENTS,
    FaceRecognizer,
)
from recognition_stability import DEFAULT_FRAMES_TO_CONFIRM, StabilitySet
from paths import enable_safe_console

WINDOW_TITLE = "ClassVision AI - Recognition Test"

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

MATCH_COLOR = (0, 220, 120)      # green
UNKNOWN_COLOR = (0, 140, 255)    # orange
PENDING_COLOR = (0, 200, 220)    # yellow


def percentile(sorted_values, fraction):
    if not sorted_values:
        return 0.0
    index = int(round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


def draw_result(frame, result, identity, name, status, similarity):
    """Draw one face's box and label. Never draws attendance information."""
    if result.box is None:
        return

    x, y, width, height = result.box

    if status == STATUS_MATCH:
        color = MATCH_COLOR
        label = name or identity
    elif result.status == STATUS_MATCH:
        # Recognised this frame, but not yet confirmed by the stability filter.
        color = PENDING_COLOR
        label = "%s?" % (result.name or result.identity)
    else:
        color = UNKNOWN_COLOR
        label = "Unknown"

    cv2.rectangle(frame, (x, y), (x + width, y + height), color, 2)

    font = cv2.FONT_HERSHEY_SIMPLEX
    text = "%s  %.2f" % (label, similarity)
    (text_width, text_height), _ = cv2.getTextSize(text, font, 0.55, 1)

    label_bottom = y - 4
    if label_bottom - text_height < 0:
        label_bottom = y + height + text_height + 8

    cv2.rectangle(frame, (x, label_bottom - text_height - 6),
                  (x + text_width + 10, label_bottom + 4), color, -1)
    cv2.putText(frame, text, (x + 5, label_bottom - 2), font, 0.55,
                (0, 0, 0), 1, cv2.LINE_AA)


def draw_panel(frame, lines):
    height, width = frame.shape[:2]
    panel_height = 24 * len(lines) + 16

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (width, panel_height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    font = cv2.FONT_HERSHEY_SIMPLEX
    y = 28
    for line in lines:
        cv2.putText(frame, line, (14, y), font, 0.55, (0, 255, 170), 1, cv2.LINE_AA)
        y += 24


def build(args):
    """Create the detector and recogniser, explaining any failure."""
    try:
        detector = YuNetFaceDetector()
        recognizer = FaceRecognizer(threshold=args.threshold)
    except FileNotFoundError as error:
        print("ERROR: %s" % error)
        return None, None
    except ValueError as error:
        print("ERROR: %s" % error)
        return None, None

    for warning in recognizer.warnings:
        print("WARNING: %s" % warning)

    if recognizer.gallery.is_empty:
        print("\nNo students are enrolled, so every face will be UNKNOWN.")
        print("Enroll someone first:  python ai/enroll_student.py\n")
    else:
        print("Enrolled: %d student(s), %d embeddings."
              % (recognizer.gallery.student_count,
                 recognizer.gallery.embedding_count))

    return detector, recognizer


def run_live(args):
    detector, recognizer = build(args)
    if detector is None:
        return 1

    camera = open_camera(args.camera, args.width, args.height)
    if camera is None:
        print("ERROR: could not open camera %d." % args.camera)
        print("Close any app using the webcam, or run:")
        print("    python ai/camera_test.py --list")
        return 1

    actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print("Camera %d opened at %dx%d." % (args.camera, actual_width, actual_height))
    print("Recognition threshold: %.3f (provisional development value)"
          % recognizer.threshold)
    print("Controls:  Q quit   H panel   S stability   R reload students")

    stability = StabilitySet(frames_to_confirm=args.confirm_frames)
    use_stability = not args.no_stability

    frame_times = deque(maxlen=30)
    detect_times = []
    recognize_times = []
    face_counts = []
    frame_count = 0
    dropped = 0
    show_panel = True
    started_at = time.time()
    last_time = started_at

    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                dropped += 1
                if dropped > 30:
                    print("ERROR: camera stopped delivering frames.")
                    break
                continue

            frame_count += 1
            height, width = frame.shape[:2]

            detect_started = time.perf_counter()
            detections = detector.detect(frame)
            detect_ms = (time.perf_counter() - detect_started) * 1000.0

            recognize_started = time.perf_counter()
            results = recognizer.recognize_all(frame, detections)
            recognize_ms = (time.perf_counter() - recognize_started) * 1000.0

            detect_times.append(detect_ms)
            recognize_times.append(recognize_ms)
            face_counts.append(len(detections))

            if use_stability:
                stabilised = stability.update(results, width)
            else:
                stabilised = [(r.identity, r.name, r.status, r.similarity)
                              for r in results]

            for result, (identity, name, status, similarity) in zip(results, stabilised):
                draw_result(frame, result, identity, name, status, similarity)

            now = time.time()
            frame_times.append(now - last_time)
            last_time = now
            average_gap = sum(frame_times) / len(frame_times)
            fps = 1.0 / average_gap if average_gap > 0 else 0.0

            confirmed = sum(1 for _, _, status, _ in stabilised
                            if status == STATUS_MATCH)

            if show_panel:
                draw_panel(frame, [
                    "ClassVision AI - Recognition (Step 4)",
                    "Faces: %d   Matched: %d   FPS: %.1f" % (
                        len(detections), confirmed, fps),
                    "detect %.1f ms   recognise %.1f ms   total %.1f ms" % (
                        detect_ms, recognize_ms, detect_ms + recognize_ms),
                    "threshold %.2f   stability %s   identity only" % (
                        recognizer.threshold, "on" if use_stability else "off"),
                ])

            cv2.imshow(WINDOW_TITLE, frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                print("Quit requested.")
                break
            if key in (ord("h"), ord("H")):
                show_panel = not show_panel
            if key in (ord("s"), ord("S")):
                use_stability = not use_stability
                stability.reset()
                print("Stability filter %s." % ("on" if use_stability else "off"))
            if key in (ord("r"), ord("R")):
                recognizer.reload()
                stability.reset()
                print("Reloaded: %d student(s)." % recognizer.gallery.student_count)

            if cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                print("Window closed.")
                break

    except KeyboardInterrupt:
        print("\nInterrupted by user (Ctrl+C).")

    finally:
        camera.release()
        cv2.destroyAllWindows()
        print_summary(actual_width, actual_height, time.time() - started_at,
                      frame_count, dropped, detect_times, recognize_times,
                      face_counts)

    return 0


def print_summary(width, height, duration, frames, dropped,
                  detect_times, recognize_times, face_counts):
    average_fps = frames / duration if duration > 0 else 0.0
    detect_avg = sum(detect_times) / len(detect_times) if detect_times else 0.0
    recognize_avg = (sum(recognize_times) / len(recognize_times)
                     if recognize_times else 0.0)

    print("\n--- Recognition summary ---")
    print("  resolution        : %dx%d" % (width, height))
    print("  duration          : %.1fs" % duration)
    print("  frames processed  : %d" % frames)
    print("  dropped frames    : %d" % dropped)
    print("  average FPS       : %.1f" % average_fps)
    print("  detect avg        : %.1f ms" % detect_avg)
    print("  recognise avg     : %.1f ms" % recognize_avg)
    print("  total per frame   : %.1f ms" % (detect_avg + recognize_avg))
    print("  detect p95        : %.1f ms" % percentile(sorted(detect_times), 0.95))
    print("  recognise p95     : %.1f ms" % percentile(sorted(recognize_times), 0.95))
    print("  frames with faces : %d of %d"
          % (sum(1 for c in face_counts if c > 0), frames))
    print("  most faces at once: %d" % (max(face_counts) if face_counts else 0))
    print("Camera released and windows destroyed.")


def run_benchmark(args):
    """Measure detection and recognition cost with no window."""
    detector, recognizer = build(args)
    if detector is None:
        return 1

    print("\nBenchmarking (%.0fs per resolution). OpenCV runs on the CPU here -"
          % args.seconds)
    print("no GPU acceleration is configured or claimed.\n")
    print("  %-11s %7s %11s %12s %10s %7s" %
          ("resolution", "FPS", "detect avg", "recognise", "total", "faces"))
    print("  " + "-" * 64)

    for width, height in [(640, 480), (1280, 720)]:
        camera = open_camera(args.camera, width, height)
        if camera is None:
            print("  %-11s could not open camera" % ("%dx%d" % (width, height)))
            continue

        actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))

        for _ in range(5):
            camera.read()

        detect_times = []
        recognize_times = []
        face_counts = []
        started = time.time()

        while time.time() - started < args.seconds:
            ok, frame = camera.read()
            if not ok:
                continue

            t0 = time.perf_counter()
            detections = detector.detect(frame)
            t1 = time.perf_counter()
            recognizer.recognize_all(frame, detections)
            t2 = time.perf_counter()

            detect_times.append((t1 - t0) * 1000.0)
            recognize_times.append((t2 - t1) * 1000.0)
            face_counts.append(len(detections))

        duration = time.time() - started
        camera.release()

        frames = len(detect_times)
        fps = frames / duration if duration > 0 else 0.0
        detect_avg = sum(detect_times) / frames if frames else 0.0
        recognize_avg = sum(recognize_times) / frames if frames else 0.0

        print("  %-11s %7.1f %10.1fms %11.1fms %9.1fms %7d" % (
            "%dx%d" % (actual_width, actual_height), fps, detect_avg,
            recognize_avg, detect_avg + recognize_avg,
            max(face_counts) if face_counts else 0))

    print("\nNote: 'faces' is the most faces seen at once during the run.")
    print("Recognition cost scales with the number of faces in the frame.")
    return 0


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="ClassVision AI - live recognition test (Step 4). "
                    "Identity verification only; no attendance.")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--threshold", type=float, default=None,
                        help="recognition threshold (default: %.2f, provisional)"
                             % DEFAULT_RECOGNITION_THRESHOLD)
    parser.add_argument("--confirm-frames", type=int,
                        default=DEFAULT_FRAMES_TO_CONFIRM,
                        help="frames that must agree before confirming "
                             "(default: %d)" % DEFAULT_FRAMES_TO_CONFIRM)
    parser.add_argument("--no-stability", action="store_true",
                        help="show the raw per-frame result")
    parser.add_argument("--benchmark", action="store_true",
                        help="measure performance and exit")
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    if args.benchmark:
        return run_benchmark(args)

    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
