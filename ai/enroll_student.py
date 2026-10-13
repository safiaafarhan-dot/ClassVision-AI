"""
ClassVision AI - Student Enrollment (Step 3)
--------------------------------------------
Registers one student by capturing several good views of their face, turning
each into an embedding, and storing only those embeddings.

    Step 3 creates and stores biometric embeddings but does not perform
    classroom attendance, recognition, or tracking.

What happens to the video
    Frames are used to compute an embedding and are then discarded. No
    photograph, face crop, or video of the student is ever written to disk.

Why several samples
    A single frame captures one pose under one lighting condition. If that one
    frame happens to be a bad one, it becomes the permanent reference for that
    student. Taking a few views - front, slightly left, slightly right -
    records the natural variation of a real face.

Usage
    python ai/enroll_student.py
    python ai/enroll_student.py --samples 5
    python ai/enroll_student.py --student-id S101 --name "Asha R" --roll 23 \\
        --class-name "10" --section "A"
"""

import argparse
import sys
import time

import cv2
import numpy as np

from camera_test import open_camera
from embedding_store import EmbeddingStore, check_student_id
from face_detector import YuNetFaceDetector
from face_embedding import SFaceEmbedder, cosine_similarity
from enrollment_rules import (
    COOLDOWN_SECONDS,
    DEFAULT_SAMPLES,
    STABLE_FRAMES_REQUIRED,
)
from enrollment_session import SampleCollector, persist_enrollment
from face_quality import check_frame_for_enrollment
from paths import enable_safe_console

WINDOW_TITLE = "ClassVision AI - Student Enrollment"

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

# STABLE_FRAMES_REQUIRED, COOLDOWN_SECONDS and DEFAULT_SAMPLES now come from
# ai/enrollment_rules.py, so the dashboard enrollment path holds students to
# exactly the same standard as this one. They are imported above.

# Shown one per sample. If more samples are requested than there are prompts,
# the list simply repeats.
POSE_PROMPTS = (
    "Look straight at the camera.",
    "Turn your head slightly LEFT.",
    "Turn your head slightly RIGHT.",
    "Look at the camera with a natural expression.",
    "Tilt your head very slightly.",
)


def pose_prompt(index):
    return POSE_PROMPTS[index % len(POSE_PROMPTS)]


def ask(prompt, existing=None, required=True):
    """Ask for one field, unless it was already given on the command line."""
    if existing:
        print("%s %s" % (prompt, existing))
        return existing

    while True:
        value = input(prompt + " ").strip()
        if value or not required:
            return value
        print("  This field is required.")


def collect_student_details(args):
    """Gather the metadata for the student being enrolled."""
    print("Student details")
    print("-" * 40)

    while True:
        student_id = ask("Student ID:", args.student_id)
        try:
            check_student_id(student_id)
            break
        except ValueError as error:
            print("  %s" % error)
            args.student_id = None

    return {
        "student_id": student_id,
        "name": ask("Name:", args.name),
        "roll_number": ask("Roll number:", args.roll),
        "class_name": ask("Class:", args.class_name),
        "section": ask("Section:", args.section),
    }


def draw_enrollment_overlay(frame, message, ok, captured, total, stable, cooling):
    """Draw the guidance panel and a progress bar for samples taken."""
    height, width = frame.shape[:2]
    good = (0, 220, 120)
    bad = (0, 140, 255)
    color = good if ok else bad

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (width, 96), (0, 0, 0), -1)
    cv2.rectangle(overlay, (0, height - 34), (width, height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(frame, "Sample %d / %d" % (min(captured + 1, total), total),
                (14, 28), font, 0.7, (255, 255, 255), 2, cv2.LINE_AA)

    if cooling:
        status = "Captured - get ready for the next pose"
    else:
        status = message
    cv2.putText(frame, status[:58], (14, 56), font, 0.55, color, 1, cv2.LINE_AA)

    if ok and not cooling:
        filled = int((stable / float(STABLE_FRAMES_REQUIRED)) * 200)
        cv2.rectangle(frame, (14, 70), (214, 82), (90, 90, 90), 1)
        cv2.rectangle(frame, (14, 70), (14 + min(filled, 200), 82), good, -1)
        cv2.putText(frame, "hold still", (222, 81), font, 0.45,
                    (200, 200, 200), 1, cv2.LINE_AA)

    # Progress dots, one per sample.
    for index in range(total):
        centre = (20 + index * 22, height - 16)
        if index < captured:
            cv2.circle(frame, centre, 8, good, -1)
        else:
            cv2.circle(frame, centre, 8, (110, 110, 110), 1)

    cv2.putText(frame, "Q = cancel", (width - 120, height - 12), font, 0.5,
                (200, 200, 200), 1, cv2.LINE_AA)


def capture_samples(camera, detector, embedder, total_samples):
    """
    Guide the student through capturing samples.

    The DECISION - is this frame usable, has it been stable long enough, are
    we cooling down - belongs to SampleCollector, which the dashboard uses
    too. What stays here is the window: the overlay, the live boxes and the
    keyboard. That split is what stops the two enrollment paths from holding
    students to different standards.

    Returns a list of embeddings, or None if cancelled. Frames are never
    stored - each becomes an embedding and is then dropped.
    """
    collector = SampleCollector(detector, embedder, total_samples)
    consecutive_failures = 0

    print("")
    print("Starting capture. Follow the on-screen instructions.")
    print("Press Q at any time to cancel.")
    print("")
    print(">>> %s" % pose_prompt(0))

    while not collector.complete:
        ok_read, frame = camera.read()

        if not ok_read:
            consecutive_failures += 1
            if consecutive_failures > 30:
                print("ERROR: the camera stopped delivering frames.")
                break
            continue
        consecutive_failures = 0

        captured, reason, detection = collector.offer(frame)

        if detection is not None:
            cv2.rectangle(frame, (detection.x, detection.y),
                          (detection.right, detection.bottom), (0, 220, 120), 2)
        else:
            for other in collector.last_detections:
                cv2.rectangle(frame, (other.x, other.y),
                              (other.right, other.bottom), (0, 140, 255), 1)

        draw_enrollment_overlay(frame, reason, collector.last_ok,
                                collector.captured, total_samples,
                                collector.stable_frames, collector.last_cooling)
        cv2.imshow(WINDOW_TITLE, frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            print("Enrollment cancelled.")
            return None

        if cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
            print("Window closed. Enrollment cancelled.")
            return None

        if captured:
            print("  Sample %d/%d captured." % (collector.captured,
                                                total_samples))
            if not collector.complete:
                print(">>> %s" % pose_prompt(collector.captured))
        elif reason.startswith("sample rejected"):
            print("  %s" % reason)

    return collector.embeddings


def report_sample_consistency(embeddings):
    """
    Development sanity check: do these samples look like the same person?

    This compares the student's samples with each other. It is a diagnostic,
    NOT a recognition threshold - choosing a "same person" cut-off is a Step 4
    decision that must be made against real data.
    """
    if len(embeddings) < 2:
        return

    similarities = []
    for first in range(len(embeddings)):
        for second in range(first + 1, len(embeddings)):
            similarities.append(
                cosine_similarity(embeddings[first], embeddings[second]))

    print("\nSample consistency (diagnostic only, not a recognition threshold)")
    print("  pairs compared : %d" % len(similarities))
    print("  cosine min     : %.4f" % min(similarities))
    print("  cosine mean    : %.4f" % (sum(similarities) / len(similarities)))
    print("  cosine max     : %.4f" % max(similarities))

    if min(similarities) < 0.5:
        print("  NOTE: one pair of samples is not very similar. If several")
        print("        people took turns in front of the camera, delete this")
        print("        enrollment and redo it.")


def sync_student_registry(details):
    """
    Add or refresh this student's row in the attendance database.

    Writes ALL of the metadata the enrollment collected - id, name, roll
    number, class and section - because the registry is the source of truth
    for who a student is. A command-line enrollment therefore produces exactly
    the same student row a dashboard registration does, and

        SELECT student_id, name, roll_number, class_name, section, status
          FROM students;

    answers the question completely whichever path created the student.

    Enrollment used to write the embedding store and no student row at all,
    which is why a student enrolled here could be recognised by the camera and
    still appear nowhere on the dashboard. Two stores, one shared key, no link
    between them.

    open_database() runs the schema migration, so enrolling against a database
    created before the registry columns existed adds them rather than failing.

    Best-effort on purpose. If the database cannot be reached, the face
    enrollment that has already been saved is still valid and recognition
    still works; the caller reports what did and did not happen rather than
    discarding a capture the student just sat through.
    """
    try:
        from pathlib import Path

        database_dir = Path(__file__).resolve().parent.parent / "database"
        if str(database_dir) not in sys.path:
            sys.path.insert(0, str(database_dir))

        from database import open_database
        from repositories import StudentRepository

        database = open_database()
        try:
            StudentRepository(database).upsert(
                details["student_id"], details["name"],
                roll_number=details.get("roll_number", ""),
                class_name=details.get("class_name", ""),
                section=details.get("section", ""))
            return True
        finally:
            database.close()
    except Exception:
        # Deliberately broad: nothing about registering a row is worth losing
        # a completed enrollment over.
        return False


def enroll(args):
    """Run the whole enrollment flow. Returns an exit code."""
    details = collect_student_details(args)
    store = EmbeddingStore()

    if store.has_student(details["student_id"]) and not args.replace:
        print("\nA student with id %r is already enrolled."
              % details["student_id"])
        print("Re-run with --replace to overwrite, or use a different id.")
        return 1

    try:
        detector = YuNetFaceDetector()
        embedder = SFaceEmbedder()
    except FileNotFoundError as error:
        print("\nERROR: %s" % error)
        return 1

    camera = open_camera(args.camera, args.width, args.height)
    if camera is None:
        print("\nERROR: could not open camera %d." % args.camera)
        print("Close any app using the webcam, or run:")
        print("    python ai/camera_test.py --list")
        return 1

    try:
        embeddings = capture_samples(camera, detector, embedder, args.samples)
    finally:
        camera.release()
        cv2.destroyAllWindows()

    if not embeddings:
        print("\nEnrollment did not complete. Nothing was stored.")
        return 1

    if len(embeddings) < args.samples:
        print("\nOnly %d of %d samples were captured. Nothing was stored."
              % (len(embeddings), args.samples))
        return 1

    report_sample_consistency(embeddings)

    # The save half is shared with the dashboard, so both paths write
    # records that are byte-identical - and both register the student in
    # the attendance database, which is what stops a command-line
    # enrollment from being invisible on the Students page.
    summary = persist_enrollment(details, embeddings, embedder,
                                 registry_sync=sync_student_registry)
    model_info = embedder.model_info()

    print("\nEnrollment completed.")
    print("  student        : %s (%s)" % (details["name"], details["student_id"]))
    print("  class          : %s-%s" % (details["class_name"], details["section"]))
    print("  samples stored : %d" % len(embeddings))
    print("  model          : %s %s (%d numbers per sample)"
          % (model_info["model_name"], model_info["model_version"],
             model_info["embedding_dim"]))
    if summary["registry_synced"]:
        print("  registry       : student record saved to the attendance "
              "database")
        print("                   (id, name, roll, class, section - visible "
              "on the dashboard)")
    else:
        print("  registry       : NOT added to the attendance database.")
        print("                   The face enrollment is saved and "
              "recognition works,")
        print("                   but this student will not appear on the "
              "dashboard until")
        print("                   the database is reachable. Re-run to retry.")

    print("\nNo photograph of this student was stored - embeddings only.")
    print("Review enrollments with:  python ai/embedding_store.py list")
    return 0


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="ClassVision AI - enroll one student (Step 3). "
                    "Stores face embeddings, never photographs. "
                    "Does not perform attendance or recognition.")
    parser.add_argument("--student-id", help="student id (asked for if omitted)")
    parser.add_argument("--name", help="student name")
    parser.add_argument("--roll", help="roll number")
    parser.add_argument("--class-name", help="class")
    parser.add_argument("--section", help="section")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help="how many samples to capture (default: %d)"
                             % DEFAULT_SAMPLES)
    parser.add_argument("--camera", type=int, default=0, help="camera index")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--replace", action="store_true",
                        help="overwrite an existing enrollment with this id")
    args = parser.parse_args()

    if args.samples < 1:
        print("--samples must be at least 1.")
        return 1

    print("ClassVision AI - Student Enrollment (Step 3)")
    print("=" * 46)
    print("This records face embeddings, not photographs.")
    print("It does not mark attendance and cannot recognise anyone yet.\n")

    try:
        return enroll(args)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was stored.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
