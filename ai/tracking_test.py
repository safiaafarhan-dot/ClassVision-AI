"""
ClassVision AI - Live Tracking + Presence Timeline (Step 5)
-----------------------------------------------------------
Follows each person across frames, recognises them only when it is worth
doing, and prints a presence timeline.

    Step 5 does not calculate attendance.

    The timeline below records who was visible and when. Attendance will be
    derived from it in a later step by applying configurable rules.

Pipeline
    camera -> detect (every frame)
           -> track  (every frame, cheap)
           -> recognise (only when the identity manager asks for it)
           -> presence timeline

Controls
    Q or ESC   quit
    H          hide / show the info panel
    T          show / hide the live timeline in the console
    R          reload enrolled students

Examples
    python ai/tracking_test.py
    python ai/tracking_test.py --recognition-interval 15
    python ai/tracking_test.py --timeline
    python ai/tracking_test.py --benchmark --seconds 20
"""

import argparse
import sys
import time
from collections import deque

import cv2

from camera_test import open_camera
from face_detector import YuNetFaceDetector
from face_recognizer import FaceRecognizer
from person_tracker import (
    DEFAULT_MAX_LOST_FRAMES,
    TRACK_CONFIRMED,
    PersonTracker,
)
from presence_timeline import (
    STATE_PRESENT,
    STATE_TEMPORARILY_LOST,
    PresenceTimeline,
)
from track_identity import DEFAULT_RECOGNITION_INTERVAL, IdentityManager
from paths import enable_safe_console

WINDOW_TITLE = "ClassVision AI - Tracking Test"

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

COLOR_PRESENT = (0, 220, 120)
COLOR_UNKNOWN = (0, 140, 255)
COLOR_LOST = (140, 140, 140)


def percentile(sorted_values, fraction):
    if not sorted_values:
        return 0.0
    index = int(round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


class TrackingSession:
    """
    Wires detection, tracking, recognition and the timeline together.

    Kept separate from the display code so the same pipeline can be driven by
    a benchmark, by tests, or by a future component.
    """

    def __init__(self, detector, recognizer, tracker=None,
                 identity_manager=None, timeline=None):
        self.detector = detector
        self.recognizer = recognizer
        self.tracker = tracker or PersonTracker()
        self.identities = identity_manager or IdentityManager()
        self.timeline = timeline or PresenceTimeline()

        self.frame_index = -1
        self.detect_times = []
        self.recognize_times = []
        self.track_times = []
        self.total_times = []

    def process(self, frame):
        """
        Run one frame through the whole pipeline.

        Returns (tracks, events). The frame is never stored.
        """
        self.frame_index += 1
        started = time.perf_counter()

        detect_started = time.perf_counter()
        detections = self.detector.detect(frame)
        detect_ms = (time.perf_counter() - detect_started) * 1000.0

        track_started = time.perf_counter()
        self.tracker.update(detections, self.frame_index)
        track_ms = (time.perf_counter() - track_started) * 1000.0

        # Recognition runs only for tracks that actually need it.
        recognize_started = time.perf_counter()
        outcomes = {}
        visible = 0

        for track in self.tracker.tracks:
            if not track.is_visible:
                continue
            visible += 1

            if not self.identities.should_recognize(track, self.frame_index):
                continue

            detection = self._detection_for(track, detections)
            if detection is None or detection.landmarks is None:
                continue

            result = self.recognizer.recognize(frame, detection)
            _, outcome = self.identities.update(track, result, self.frame_index)
            outcomes[track.track_id] = outcome

        recognize_ms = (time.perf_counter() - recognize_started) * 1000.0

        self.identities.note_frame(visible_faces=visible)

        removed = self.tracker.take_removed()
        events = self.timeline.update(
            self.tracker.tracks, self.identities, self.frame_index,
            removed_tracks=removed, outcomes=outcomes)

        for track in removed:
            self.identities.forget(track.track_id)

        total_ms = (time.perf_counter() - started) * 1000.0

        self.detect_times.append(detect_ms)
        self.track_times.append(track_ms)
        self.recognize_times.append(recognize_ms)
        self.total_times.append(total_ms)

        return self.tracker.tracks, events

    def _detection_for(self, track, detections):
        """
        Find the detection that produced this track's current box.

        The tracker stores the box but not the landmarks, and recognition
        needs the landmarks to align the face.
        """
        for detection in detections:
            if detection.as_tuple() == track.box:
                return detection
        return None

    def close(self):
        """End every open presence at the end of a session."""
        return self.timeline.close_all(self.frame_index)


def label_for(track, identity, record):
    """Build the on-screen label: Track 7 | Safia | MATCH 0.82 | PRESENT."""
    parts = ["Track %d" % track.track_id]

    if identity is not None and identity.is_confirmed:
        parts.append(identity.name or identity.student_id)
        parts.append("MATCH %.2f" % identity.similarity)
    else:
        parts.append("UNKNOWN")
        if identity is not None and identity.similarity:
            parts.append("%.2f" % identity.similarity)

    state = record.state if record is not None else "NEW"
    parts.append(state)

    return " | ".join(parts)


def draw_track(frame, track, identity, record):
    x, y, width, height = track.box

    if not track.is_visible:
        color = COLOR_LOST
    elif identity is not None and identity.is_confirmed:
        color = COLOR_PRESENT
    else:
        color = COLOR_UNKNOWN

    thickness = 2 if track.state == TRACK_CONFIRMED else 1
    cv2.rectangle(frame, (x, y), (x + width, y + height), color, thickness)

    text = label_for(track, identity, record)
    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_width, text_height), _ = cv2.getTextSize(text, font, 0.45, 1)

    label_bottom = y - 4
    if label_bottom - text_height < 0:
        label_bottom = y + height + text_height + 8

    cv2.rectangle(frame, (x, label_bottom - text_height - 6),
                  (x + text_width + 10, label_bottom + 4), color, -1)
    cv2.putText(frame, text, (x + 5, label_bottom - 2), font, 0.45,
                (0, 0, 0), 1, cv2.LINE_AA)


def draw_panel(frame, lines):
    height, width = frame.shape[:2]
    panel_height = 22 * len(lines) + 14

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (width, panel_height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    font = cv2.FONT_HERSHEY_SIMPLEX
    y = 24
    for line in lines:
        cv2.putText(frame, line, (12, y), font, 0.5, (0, 255, 170), 1, cv2.LINE_AA)
        y += 22


def build(args):
    try:
        detector = YuNetFaceDetector()
        recognizer = FaceRecognizer(threshold=args.threshold)
    except (FileNotFoundError, ValueError) as error:
        print("ERROR: %s" % error)
        return None

    for warning in recognizer.warnings:
        print("WARNING: %s" % warning)

    if recognizer.gallery.is_empty:
        print("\nNo students are enrolled, so every track will be UNKNOWN.")
        print("Tracking and the presence timeline still work.")
        print("Enroll someone first:  python ai/enroll_student.py\n")
    else:
        print("Enrolled: %d student(s), %d embeddings."
              % (recognizer.gallery.student_count,
                 recognizer.gallery.embedding_count))

    return TrackingSession(
        detector, recognizer,
        tracker=PersonTracker(max_lost_frames=args.max_lost_frames),
        identity_manager=IdentityManager(
            recognition_interval=args.recognition_interval))


def print_efficiency(session):
    stats = session.identities.efficiency()

    print("\n--- Recognition efficiency ---")
    print("  frames processed      : %d" % stats["frames_processed"])
    print("  face-frames seen      : %d" % stats["face_frames_seen"])
    print("  recognition calls     : %d" % stats["recognition_calls"])
    print("  calls per frame       : %.3f" % stats["calls_per_frame"])
    print("  a Step 4 pipeline would have made %d calls"
          % stats["naive_recognition_calls"])

    if stats["face_frames_seen"]:
        print("  calls avoided         : %d (%.1f%%)"
              % (stats["calls_avoided"], stats["reduction_percent"]))
    else:
        print("  calls avoided         : not measurable - no faces were seen")


def print_timing(session, duration, dropped, width, height):
    frames = len(session.total_times)
    average_fps = frames / duration if duration > 0 else 0.0

    def mean(values):
        return sum(values) / len(values) if values else 0.0

    print("\n--- Timing ---")
    print("  resolution            : %dx%d" % (width, height))
    print("  duration              : %.1fs" % duration)
    print("  frames processed      : %d" % frames)
    print("  dropped frames        : %d" % dropped)
    print("  average FPS           : %.1f" % average_fps)
    print("  detect avg            : %.1f ms" % mean(session.detect_times))
    print("  track avg             : %.3f ms" % mean(session.track_times))
    print("  recognise avg         : %.1f ms" % mean(session.recognize_times))
    print("  total avg latency     : %.1f ms" % mean(session.total_times))
    print("  total p95 latency     : %.1f ms"
          % percentile(sorted(session.total_times), 0.95))
    print("  total max latency     : %.1f ms"
          % (max(session.total_times) if session.total_times else 0.0))


def print_timeline_summary(session):
    summary = session.timeline.summary()

    print("\n--- Presence summary (NOT attendance) ---")
    if not summary:
        print("  No tracks were created.")
        return

    print("  %-6s %-10s %-18s %-8s %-8s %s"
          % ("track", "student", "state", "frames", "lost", "duration"))
    print("  " + "-" * 64)
    for row in summary:
        print("  %-6d %-10s %-18s %-8d %-8d %.1fs" % (
            row["track_id"], row["student_id"] or "-", row["state"],
            row["visible_frames"], row["lost_episodes"],
            row["duration_seconds"]))

    print("\n  This is presence, not attendance. Attendance rules come later.")


def run_live(args):
    session = build(args)
    if session is None:
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
    print("Recognition every %d frames per settled track; grace period %d frames."
          % (args.recognition_interval, args.max_lost_frames))
    print("Controls:  Q quit   H panel   T timeline   R reload\n")

    show_timeline = args.timeline
    show_panel = True
    frame_times = deque(maxlen=30)
    dropped = 0
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

            tracks, events = session.process(frame)

            if show_timeline:
                for event in events:
                    print(event.format_line())

            for track in tracks:
                identity = session.identities.get(track.track_id)
                record = session.timeline.records.get(track.track_id)
                draw_track(frame, track, identity, record)

            now = time.time()
            frame_times.append(now - last_time)
            last_time = now
            gap = sum(frame_times) / len(frame_times)
            fps = 1.0 / gap if gap > 0 else 0.0

            visible = len([t for t in tracks if t.is_visible])
            lost = len([t for t in tracks if not t.is_visible])
            stats = session.identities.efficiency()

            if show_panel:
                draw_panel(frame, [
                    "ClassVision AI - Tracking + Presence (Step 5)",
                    "Tracks: %d visible, %d lost   FPS: %.1f" % (visible, lost, fps),
                    "recognition calls: %d (%.2f/frame)" % (
                        stats["recognition_calls"], stats["calls_per_frame"]),
                    "detect %.1f ms  track %.2f ms  total %.1f ms" % (
                        session.detect_times[-1], session.track_times[-1],
                        session.total_times[-1]),
                    "dropped: %d   identity only - no attendance" % dropped,
                ])

            cv2.imshow(WINDOW_TITLE, frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                print("Quit requested.")
                break
            if key in (ord("h"), ord("H")):
                show_panel = not show_panel
            if key in (ord("t"), ord("T")):
                show_timeline = not show_timeline
                print("Timeline %s." % ("on" if show_timeline else "off"))
            if key in (ord("r"), ord("R")):
                session.recognizer.reload()
                print("Reloaded: %d student(s)."
                      % session.recognizer.gallery.student_count)

            if cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                print("Window closed.")
                break

    except KeyboardInterrupt:
        print("\nInterrupted by user (Ctrl+C).")

    finally:
        duration = time.time() - started_at
        camera.release()
        cv2.destroyAllWindows()

        for event in session.close():
            if show_timeline:
                print(event.format_line())

        print_timing(session, duration, dropped, actual_width, actual_height)
        print_efficiency(session)
        print_timeline_summary(session)
        print("\nCamera released and windows destroyed. No frames were saved.")

    return 0


def run_benchmark(args):
    """Measure the pipeline with no window."""
    session = build(args)
    if session is None:
        return 1

    camera = open_camera(args.camera, args.width, args.height)
    if camera is None:
        print("ERROR: could not open camera %d." % args.camera)
        return 1

    actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))

    for _ in range(5):
        camera.read()

    print("Benchmarking for %.0fs at %dx%d. OpenCV runs on the CPU here -"
          % (args.seconds, actual_width, actual_height))
    print("no GPU acceleration is configured or claimed.\n")

    dropped = 0
    started = time.time()
    while time.time() - started < args.seconds:
        ok, frame = camera.read()
        if not ok:
            dropped += 1
            continue
        session.process(frame)

    duration = time.time() - started
    camera.release()
    session.close()

    print_timing(session, duration, dropped, actual_width, actual_height)
    print_efficiency(session)
    print_timeline_summary(session)
    return 0


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="ClassVision AI - tracking and presence timeline (Step 5). "
                    "Produces presence events; does NOT calculate attendance.")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--threshold", type=float, default=None,
                        help="recognition threshold (provisional default 0.45)")
    parser.add_argument("--recognition-interval", type=int,
                        default=DEFAULT_RECOGNITION_INTERVAL,
                        help="frames between re-checks of a settled track "
                             "(default: %d)" % DEFAULT_RECOGNITION_INTERVAL)
    parser.add_argument("--max-lost-frames", type=int,
                        default=DEFAULT_MAX_LOST_FRAMES,
                        help="grace period before a track is treated as gone "
                             "(default: %d)" % DEFAULT_MAX_LOST_FRAMES)
    parser.add_argument("--timeline", action="store_true",
                        help="print presence events to the console as they happen")
    parser.add_argument("--benchmark", action="store_true",
                        help="measure performance without a window")
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()

    if args.benchmark:
        return run_benchmark(args)

    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
