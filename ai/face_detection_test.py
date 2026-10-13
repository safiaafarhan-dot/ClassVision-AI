"""
ClassVision AI - Live Face Detection Test (Step 2)
--------------------------------------------------
Opens the webcam and draws a box around every face it finds.

It shows WHERE faces are. It does not show WHO anyone is - there is no
recognition in Step 2, so no names are ever displayed.

The camera helpers are imported from camera_test.py (Step 1) rather than
copied, so there is only one place that knows how to open a camera.

Controls
    Q or ESC   quit
    H          hide / show the info panel
    C          hide / show per-face confidence

Examples
    python ai/face_detection_test.py
    python ai/face_detection_test.py --width 1280 --height 720
    python ai/face_detection_test.py --threshold 0.6
    python ai/face_detection_test.py --benchmark
"""

import argparse
import sys
import time
from collections import deque

import cv2

from camera_test import draw_info_panel, open_camera
from face_detector import DEFAULT_SCORE_THRESHOLD, YuNetFaceDetector, draw_detections
from paths import enable_safe_console

WINDOW_TITLE = "ClassVision AI - Face Detection Test"

# Step 1 measured ~15.5 FPS at 640x480 versus ~8 FPS at 1280x720 on this
# webcam, so 640x480 leaves roughly double the time budget for detection.
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480


def percentile(sorted_values, fraction):
    """Simple percentile for an already-sorted list. Avoids a numpy import."""
    if not sorted_values:
        return 0.0
    index = int(round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


def build_detector(threshold):
    """Create the detector, explaining clearly if the model is missing."""
    try:
        return YuNetFaceDetector(score_threshold=threshold)
    except FileNotFoundError as error:
        print("ERROR: %s" % error)
        return None


def run_live(camera_index, width, height, threshold):
    """Live preview with face boxes. Returns an exit code."""
    detector = build_detector(threshold)
    if detector is None:
        return 1

    camera = open_camera(camera_index, width, height)
    if camera is None:
        print("ERROR: Could not open camera %d." % camera_index)
        print("Close any app using the webcam, or run:")
        print("    python ai/camera_test.py --list")
        return 1

    actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print("Camera %d opened at %dx%d." % (camera_index, actual_width, actual_height))
    print("Confidence threshold: %.2f" % threshold)
    print("Controls:  Q / ESC = quit   H = info panel   C = confidence labels")

    frame_times = deque(maxlen=30)
    detect_times = []
    face_counts = []
    frame_count = 0
    dropped_frames = 0
    show_panel = True
    show_confidence = True
    started_at = time.time()
    last_time = started_at

    try:
        while True:
            ok, frame = camera.read()

            if not ok:
                dropped_frames += 1
                if dropped_frames > 30:
                    print("ERROR: Camera stopped delivering frames. Stopping.")
                    break
                continue

            frame_count += 1

            # Time only the detection, so the number is not polluted by the
            # cost of drawing and displaying the frame.
            detect_started = time.perf_counter()
            faces = detector.detect(frame)
            detect_ms = (time.perf_counter() - detect_started) * 1000.0

            detect_times.append(detect_ms)
            face_counts.append(len(faces))

            draw_detections(frame, faces, show_confidence=show_confidence)

            now = time.time()
            frame_times.append(now - last_time)
            last_time = now

            average_gap = sum(frame_times) / len(frame_times)
            fps = 1.0 / average_gap if average_gap > 0 else 0.0

            if show_panel:
                draw_info_panel(frame, [
                    "ClassVision AI - Face Detection (Step 2)",
                    "Faces: %d" % len(faces),
                    "FPS: %.1f   detect: %.1f ms" % (fps, detect_ms),
                    "%dx%d   no identification" % (actual_width, actual_height),
                ])

            cv2.imshow(WINDOW_TITLE, frame)

            key = cv2.waitKey(1) & 0xFF

            if key in (ord("q"), ord("Q"), 27):  # 27 = ESC
                print("Quit requested.")
                break
            if key in (ord("h"), ord("H")):
                show_panel = not show_panel
            if key in (ord("c"), ord("C")):
                show_confidence = not show_confidence

            if cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                print("Window closed.")
                break

    except KeyboardInterrupt:
        print("\nInterrupted by user (Ctrl+C).")

    finally:
        camera.release()
        cv2.destroyAllWindows()
        print_summary(actual_width, actual_height, time.time() - started_at,
                      frame_count, dropped_frames, detect_times, face_counts)

    return 0


def print_summary(width, height, duration, frame_count, dropped, detect_times, face_counts):
    """Print the measured numbers for a run."""
    ordered = sorted(detect_times)
    average_fps = frame_count / duration if duration > 0 else 0.0
    average_detect = sum(detect_times) / len(detect_times) if detect_times else 0.0
    max_faces = max(face_counts) if face_counts else 0
    frames_with_faces = sum(1 for count in face_counts if count > 0)

    print("\n--- Face detection summary ---")
    print("  resolution        : %dx%d" % (width, height))
    print("  duration          : %.1fs" % duration)
    print("  frames processed  : %d" % frame_count)
    print("  dropped frames    : %d" % dropped)
    print("  average FPS       : %.1f" % average_fps)
    print("  detect avg        : %.1f ms" % average_detect)
    print("  detect p50 / p95  : %.1f ms / %.1f ms"
          % (percentile(ordered, 0.50), percentile(ordered, 0.95)))
    print("  frames with faces : %d of %d" % (frames_with_faces, frame_count))
    print("  most faces at once: %d" % max_faces)
    print("Camera released and windows destroyed.")


def run_benchmark(camera_index, threshold, seconds, resolutions):
    """
    Measure detection cost at several resolutions, with no window.

    Useful for recording a real baseline on this machine without needing
    anyone to sit and watch a preview window.
    """
    detector = build_detector(threshold)
    if detector is None:
        return 1

    print("Benchmarking face detection (%.0fs per resolution)...\n" % seconds)
    print("  %-11s %7s %10s %10s %10s %8s" %
          ("resolution", "FPS", "detect avg", "p95", "frames", "faces"))
    print("  " + "-" * 62)

    for width, height in resolutions:
        camera = open_camera(camera_index, width, height)
        if camera is None:
            print("  %-11s  could not open camera" % ("%dx%d" % (width, height)))
            continue

        actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Discard the first few frames: cameras need a moment to settle
        # after a resolution change, and those frames are not representative.
        for _ in range(5):
            camera.read()

        detect_times = []
        face_counts = []
        dropped = 0
        started = time.time()

        while time.time() - started < seconds:
            ok, frame = camera.read()
            if not ok:
                dropped += 1
                continue

            detect_started = time.perf_counter()
            faces = detector.detect(frame)
            detect_times.append((time.perf_counter() - detect_started) * 1000.0)
            face_counts.append(len(faces))

        duration = time.time() - started
        camera.release()

        frames = len(detect_times)
        fps = frames / duration if duration > 0 else 0.0
        average = sum(detect_times) / frames if frames else 0.0
        ordered = sorted(detect_times)
        max_faces = max(face_counts) if face_counts else 0

        print("  %-11s %7.1f %9.1fms %9.1fms %10d %8d" % (
            "%dx%d" % (actual_width, actual_height),
            fps, average, percentile(ordered, 0.95), frames, max_faces))

        if dropped:
            print("      (%d dropped frames)" % dropped)

    print("\nNote: 'faces' is the most faces seen at once during the run.")
    print("It reflects whoever was in front of the camera, not accuracy.")
    return 0


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="ClassVision AI - live face detection test (Step 2). "
                    "Detects where faces are; does not identify anyone."
    )
    parser.add_argument("--camera", type=int, default=0,
                        help="camera index (default: 0)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="frame width (default: %d)" % DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                        help="frame height (default: %d)" % DEFAULT_HEIGHT)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCORE_THRESHOLD,
                        help="confidence threshold 0-1 (default: %.2f)"
                             % DEFAULT_SCORE_THRESHOLD)
    parser.add_argument("--benchmark", action="store_true",
                        help="measure performance at several resolutions and exit")
    parser.add_argument("--seconds", type=float, default=6.0,
                        help="seconds per resolution when benchmarking (default: 6)")
    args = parser.parse_args()

    if args.benchmark:
        return run_benchmark(args.camera, args.threshold, args.seconds,
                             [(640, 480), (1280, 720)])

    return run_live(args.camera, args.width, args.height, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
