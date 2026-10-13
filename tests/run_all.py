"""
Run every test suite in this project.

No webcam and no network needed. The Step 2 and Step 3 suites do need the
downloaded models; run  python ai/download_models.py  first.

Usage:
    python tests/run_all.py
"""

import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

SUITES = [
    ("Step 1  camera",           "test_camera_test.py"),
    ("Step 2  face detection",   "test_face_detector.py"),
    ("Step 3  face alignment",   "test_face_alignment.py"),
    ("Step 3  face embedding",   "test_face_embedding.py"),
    ("Step 3  store + privacy",  "test_embedding_store.py"),
    ("Step 4  face recognition", "test_face_recognizer.py"),
    ("Step 4  stability",        "test_recognition_stability.py"),
    ("Step 5  person tracker",   "test_person_tracker.py"),
    ("Step 5  track identity",   "test_track_identity.py"),
    ("Step 5  presence timeline","test_presence_timeline.py"),
    ("Step 6  attendance rules", "test_attendance_rules.py"),
    ("Step 6  database",         "test_attendance_database.py"),
    ("Step 6  end-to-end",       "test_attendance_end_to_end.py"),
    ("Step 7  analytics",        "test_dashboard_analytics.py"),
    ("Step 7  dashboard API",    "test_dashboard_api.py"),
    ("Step 7  dashboard UI",     "test_dashboard_frontend.py"),
    ("Step 8  live runtime",     "test_live_session.py"),
    ("Step 8  live API + UI",    "test_live_api.py"),
    ("Step 9  liveness detect",  "test_liveness_detector.py"),
    ("Step 9  liveness stable",  "test_liveness_stability.py"),
    ("Step 9  liveness integ",   "test_liveness_integration.py"),
    ("Dashboard  scheduling",    "test_session_creation.py"),
    ("Step 10 authenticity",     "test_authenticity_detector.py"),
    ("Step 10 auth stability",   "test_authenticity_stability.py"),
    ("Step 10 auth integration", "test_authenticity_integration.py"),
    ("Dashboard  enrollment",    "test_student_enrollment.py"),
    ("Registry  students",       "test_student_registry.py"),
    ("Registry  auth + RBAC",    "test_auth_rbac.py"),
    ("Session  finalization",    "test_session_finalization.py"),
    ("Live  classroom view",     "test_classroom_view.py"),
    ("Academic  terms + promote", "test_academic_sessions.py"),
    ("Operations  alerts+models", "test_operations.py"),
    ("Camera health  UI",        "test_camera_health_ui.py"),
    ("Session  lifecycle",       "test_session_lifecycle.py"),
    ("Portal  students + staff", "test_student_portal.py"),
    ("Profile  performance",     "test_student_performance.py"),
    ("Portal  student login",    "test_student_login.py"),
    ("Dashboard  teacher day",   "test_teacher_dashboard.py"),
    ("Reports  attendance+CSV",  "test_attendance_reports.py"),
    ("Session  expected roster", "test_session_expected_students.py"),
    ("Camera  registration",     "test_camera_registration.py"),
    ("Live  classroom workflow", "test_live_classroom_workflow.py"),
    ("Interface  theme + depth", "test_theme_toggle.py"),
    ("Step 11 summaries+PDF",    "test_report_exports.py"),
    ("Step 11 reports UI",       "test_report_ui.py"),
    ("Academic  terms UI",       "test_academic_ui.py"),
    ("Step 12 assistant",        "test_assistant.py"),
    ("Step 13 offline queue",    "test_offline_sync.py"),
    ("Step 13 deployment",       "test_deployment.py"),
    ("Step 13 restore drill",    "test_restore.py"),
    ("Step 14 session break",    "test_session_break.py"),
    ("Step 14 multi-session",    "test_multi_session.py"),
    ("Rules  duration grace",    "test_attendance_grace.py"),
    ("Registry  identity",       "test_student_identity.py"),
    ("Registry  year filters",   "test_student_registry_filters.py"),
    ("Live  multi-class console", "test_multi_session_console.py"),
    ("Interface  glass contrast", "test_glass_contrast.py"),
    ("Interface  spatial layer",  "test_spatial_interface.py"),
    ("Assembly  mode",            "test_assembly_mode.py"),
]


# Every suite ends with a line like "23/24 tests passed.", which is where the
# project-wide test count comes from. A suite that dies before printing it
# reports no count, and the summary says so rather than guessing a number.
COUNT_LINE = re.compile(r"^(\d+)/(\d+) tests passed\.")


def run_suite(filename):
    """
    Run one suite, echoing its output as it arrives, and read its count line.

    Output is streamed rather than captured-then-printed so a slow suite still
    shows progress. Returns (ok, passed, total); passed and total are None
    when the suite printed no count line.
    """
    process = subprocess.Popen(
        [sys.executable, str(TESTS_DIR / filename)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    passed = total = None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        match = COUNT_LINE.match(line.strip())
        if match:
            passed, total = int(match.group(1)), int(match.group(2))

    process.stdout.close()
    return process.wait() == 0, passed, total


def main():
    results = []

    for title, filename in SUITES:
        print("=" * 70)
        print(title)
        print("=" * 70)

        ok, passed, total = run_suite(filename)
        results.append((title, ok, passed, total))
        print("")

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for title, ok, passed, total in results:
        count = "%d/%d" % (passed, total) if total is not None else "no count"
        print("  %-28s %-9s %s" % (title, count, "PASS" if ok else "FAIL"))

    counted = [(passed, total) for _, _, passed, total in results
               if total is not None]
    total_passed = sum(passed for passed, _ in counted)
    total_tests = sum(total for _, total in counted)
    uncounted = len(results) - len(counted)

    print("")
    print("  %d of %d tests passed across %d suites."
          % (total_passed, total_tests, len(counted)))
    if uncounted:
        print("  %d suite(s) printed no count line." % uncounted)

    failed = [title for title, ok, _, _ in results if not ok]
    if failed:
        print("\n%d of %d suites FAILED: %s"
              % (len(failed), len(results), ", ".join(failed)))
        return 1

    print("\nAll %d suites passed." % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
