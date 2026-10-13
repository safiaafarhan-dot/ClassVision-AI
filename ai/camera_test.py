"""
ClassVision AI - Camera Diagnostics (Step 1)
--------------------------------------------
Confirms that the camera pipeline this project will depend on actually works,
and reports the numbers that matter later: resolution, real frame rate, and
dropped frames.

This file does NOT detect or recognise faces and does NOT record attendance.
It is a hardware check only.

Controls
    Q or ESC   quit
    S          save a snapshot into datasets/snapshots/
    H          hide / show the on-screen info panel

Examples
    python ai/camera_test.py
    python ai/camera_test.py --list
    python ai/camera_test.py --camera 1 --width 1280 --height 720
"""

import argparse
import sys
import time
from collections import deque
from pathlib import Path

import cv2

WINDOW_TITLE = "ClassVision AI - Camera Test"

# datasets/ is gitignored, so snapshots never get committed by accident.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "datasets" / "snapshots"

# On Windows the DirectShow backend opens webcams faster and more reliably
# than the default one.
BACKEND = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY


def probe_cameras(max_index=5):
    """
    Which camera indexes actually work on this machine, as data.

    One entry per index tried, so a caller can tell "nothing is there" from
    "something is there and would not give a frame" - which are different
    faults with different fixes. A device that OPENS but delivers no frame is
    almost always held by another application, and that is the single most
    common cause of the camera failing mid-lesson.

    This is the probe; list_cameras() below prints it. The split exists so the
    dashboard and the command line cannot disagree about what a working camera
    is.
    """
    findings = []

    # Probing an empty index makes OpenCV print a scary-looking warning.
    # It is harmless here, so keep the output clean while we scan.
    previous_log_level = cv2.utils.logging.getLogLevel()
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)

    try:
        for index in range(max_index):
            camera = cv2.VideoCapture(index, BACKEND)
            entry = {"index": index, "available": False, "opened": False,
                     "width": None, "height": None, "detail": "nothing on "
                     "this index"}

            try:
                if camera.isOpened():
                    entry["opened"] = True
                    ok, frame = camera.read()
                    if ok and frame is not None:
                        height, width = frame.shape[:2]
                        entry.update(available=True, width=int(width),
                                     height=int(height),
                                     detail="delivered a frame")
                    else:
                        entry["detail"] = ("opened but delivered no frame - "
                                           "usually held by another "
                                           "application")
            finally:
                camera.release()

            findings.append(entry)
    finally:
        cv2.utils.logging.setLogLevel(previous_log_level)

    return findings


def list_cameras(max_index=5):
    """Print which camera indexes actually work on this machine."""
    print("Scanning for cameras...")

    findings = probe_cameras(max_index)
    found = [entry["index"] for entry in findings if entry["available"]]

    for entry in findings:
        if entry["available"]:
            print("  [%d] available  (%dx%d)"
                  % (entry["index"], entry["width"], entry["height"]))
        elif entry["opened"]:
            print("  [%d] %s" % (entry["index"], entry["detail"]))

    if not found:
        print("  No working cameras found.")
    else:
        print("\nUse one with:  python ai/camera_test.py --camera %d" % found[0])

    return found


def open_camera(index, width, height):
    """Open a camera and ask it for a resolution. Returns None on failure."""
    camera = cv2.VideoCapture(index, BACKEND)

    if not camera.isOpened():
        return None

    # These are requests, not commands. The camera may ignore them and give
    # us the closest size it supports, which is why we measure it afterwards.
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    return camera


def draw_info_panel(frame, lines):
    """Draw readable text on top of the frame with a dark strip behind it."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    panel_height = 24 * len(lines) + 16

    # A semi-transparent strip keeps the text readable over any background.
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (frame.shape[1], panel_height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    y = 28
    for line in lines:
        cv2.putText(frame, line, (14, y), font, 0.55, (0, 255, 170), 1, cv2.LINE_AA)
        y += 24


def save_image(path, frame):
    """
    Write an image to disk, safely.

    cv2.imwrite() silently fails when the path contains non-English
    characters (this project lives under a folder named "文档", for example).
    So we encode the image in memory and let Python write the bytes, which
    handles any path correctly.
    """
    extension = path.suffix or ".jpg"
    ok, buffer = cv2.imencode(extension, frame)

    if not ok:
        return False

    path.write_bytes(buffer.tobytes())
    return True


def save_snapshot(frame, count):
    """Save the current frame to datasets/snapshots/ and return the path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / ("snapshot_%03d.jpg" % count)
    save_image(path, frame)
    return path


def run_camera_test(index, width, height):
    """Show the live feed with live diagnostics. Returns an exit code."""
    camera = open_camera(index, width, height)

    if camera is None:
        print("ERROR: Could not open camera %d." % index)
        print("Possible reasons:")
        print("  - No webcam is connected.")
        print("  - Another app (Zoom, Teams, Camera) is already using it.")
        print("  - Camera access is blocked in Windows privacy settings.")
        print("  - The camera is on a different index.")
        print("\nTry:  python ai/camera_test.py --list")
        return 1

    # Report what we actually got, which may differ from what we asked for.
    actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print("Camera %d opened successfully at %dx%d." % (index, actual_width, actual_height))
    print("Controls:  Q / ESC = quit   S = snapshot   H = hide info panel")

    # A rolling window of recent frame times gives a stable, live FPS reading.
    frame_times = deque(maxlen=30)
    frame_count = 0
    dropped_frames = 0
    snapshot_count = 0
    show_panel = True
    started_at = time.time()
    last_time = started_at

    try:
        while True:
            ok, frame = camera.read()

            if not ok:
                # One bad frame is normal; many in a row means real trouble.
                dropped_frames += 1
                if dropped_frames > 30:
                    print("ERROR: Camera stopped delivering frames. Stopping.")
                    break
                continue

            frame_count += 1

            now = time.time()
            frame_times.append(now - last_time)
            last_time = now

            average_gap = sum(frame_times) / len(frame_times)
            fps = 1.0 / average_gap if average_gap > 0 else 0.0
            elapsed = now - started_at

            if show_panel:
                draw_info_panel(frame, [
                    "ClassVision AI - Camera Test (Step 1)",
                    "%dx%d   %5.1f FPS" % (actual_width, actual_height, fps),
                    "frames %d   dropped %d   %5.1fs" % (frame_count, dropped_frames, elapsed),
                    "Q quit    S snapshot    H hide panel",
                ])

            cv2.imshow(WINDOW_TITLE, frame)

            # Wait 1 ms for a key. 0xFF keeps this portable across platforms.
            key = cv2.waitKey(1) & 0xFF

            if key in (ord("q"), ord("Q"), 27):  # 27 = ESC
                print("Quit requested.")
                break

            if key in (ord("s"), ord("S")):
                snapshot_count += 1
                path = save_snapshot(frame, snapshot_count)
                print("Snapshot saved: %s" % path)

            if key in (ord("h"), ord("H")):
                show_panel = not show_panel

            # Also exit if the user clicks the window X button.
            if cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                print("Window closed.")
                break

    except KeyboardInterrupt:
        print("\nInterrupted by user (Ctrl+C).")

    finally:
        # Always release the camera and close the windows, even after an error.
        camera.release()
        cv2.destroyAllWindows()

        total = time.time() - started_at
        average_fps = frame_count / total if total > 0 else 0.0
        print("\n--- Camera test summary ---")
        print("  resolution   : %dx%d" % (actual_width, actual_height))
        print("  frames shown : %d" % frame_count)
        print("  dropped      : %d" % dropped_frames)
        print("  duration     : %.1fs" % total)
        print("  average FPS  : %.1f" % average_fps)
        print("  snapshots    : %d" % snapshot_count)
        print("Camera released and windows destroyed.")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="ClassVision AI - camera diagnostics (Step 1)."
    )
    parser.add_argument("--camera", type=int, default=0,
                        help="camera index to open (default: 0)")
    parser.add_argument("--width", type=int, default=1280,
                        help="requested frame width (default: 1280)")
    parser.add_argument("--height", type=int, default=720,
                        help="requested frame height (default: 720)")
    parser.add_argument("--list", action="store_true",
                        help="list available cameras and exit")
    args = parser.parse_args()

    if args.list:
        return 0 if list_cameras() else 1

    return run_camera_test(args.camera, args.width, args.height)


if __name__ == "__main__":
    sys.exit(main())
