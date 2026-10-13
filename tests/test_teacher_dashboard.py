"""
The teacher's own day, and the quick actions beside it.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That /api/analytics/today answers with the caller's OWN classes, own
    students and own sessions - a teacher assigned to one class does not learn
    the shape of another - and that the narrowing happens in the service rather
    than by the browser hiding rows.

    That it invents nothing. A morning in which no session has been finalised
    reports has_data false and a null attendance rate, not 0%, because
    attendance is produced by the rule engine after a session is stopped and a
    lesson still running has none. `active_session` is null when nothing is
    running rather than the next scheduled lesson dressed up as a current one.

    And the scoping fixes underneath it: /api/sessions, /api/sessions/{id},
    its register, its presence evidence and /api/classes were all readable by
    any signed-in teacher for any class in the school. Those are checked here
    because the day dashboard is built on top of them, and a scoped dashboard
    over unscoped endpoints is decoration.

WHAT THIS DOES NOT VERIFY
    Any accuracy claim. Every figure is arithmetic over rows this file wrote.

Run with:
    python tests/test_teacher_dashboard.py
"""

import re
import shutil
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from attendance_rules import AttendanceDecision  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from repositories import AttendanceRepository, SessionRepository  # noqa: E402

from backend.services import state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

ADMIN = {"username": "principal", "display_name": "The Principal",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

# Two classes. MECH-A belongs to the teacher; CIVIL-B does not.
MINE = [("D-M1", "Asha Rao"), ("D-M2", "Vikram Nair")]
THEIRS = [("D-C1", "Leena Bose")]


def today_iso():
    """
    The server's date, which is what /api/analytics/today defaults to.

    Taken from the same clock the endpoint uses rather than hard-coded, so this
    suite does not start failing at midnight.
    """
    from datetime import date
    return date.today().isoformat()


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_day_"))
    state.configure(directory / "day.db")
    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def signed_in(username, password):
    from backend.app import app
    client = TestClient(app)
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def setup_school(client, assignments=None):
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

    created = client.post("/api/users", json={
        "username": "tsmith", "display_name": "T Smith",
        "password": TEACHER_PASSWORD, "role": "TEACHER",
        "assignments": assignments if assignments is not None
        else [{"class_name": "MECH", "section": "A"}]})
    assert created.status_code == 201, created.text

    for students, class_name, section in ((MINE, "MECH", "A"),
                                          (THEIRS, "CIVIL", "B")):
        for student_id, name in students:
            response = client.post("/api/students", json={
                "student_id": student_id, "name": name, "roll_number": "1",
                "class_name": class_name, "section": section})
            assert response.status_code == 201, response.text

    return created.json()


def session_on(session_id, class_id, student_ids, day_iso, status="COMPLETED",
               hour=9):
    """A session dated on a given day, in whichever state is asked for."""
    start = datetime.fromisoformat(day_iso + "T%02d:00:00+00:00" % hour)
    end = start + timedelta(hours=1)

    session = AttendanceSession(session_id, class_id, start, end,
                                policy_version="1.0",
                                expected_students=list(student_ids))
    if status in ("ACTIVE", "COMPLETED"):
        session.start()
    if status == "COMPLETED":
        session.complete(completed_at=end)
    if status == "CANCELLED":
        session.cancel()

    SessionRepository(state.get_database()).save(session)


def record(session_id, student_id, status):
    decision = AttendanceDecision(student_id, session_id, status,
                                  "synthetic test record", "1.0")
    decision.present_seconds = 3240.0
    decision.session_seconds = 3600.0
    decision.presence_ratio = 0.9
    AttendanceRepository(state.get_database()).save_decision(decision)


# -- the day, for the right person -------------------------------------------

def test_a_teacher_sees_only_their_own_classes_in_their_day():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()

        session_on("DAY-MINE", "MECH-A", [sid for sid, _ in MINE], day)
        session_on("DAY-THEIRS", "CIVIL-B", [sid for sid, _ in THEIRS], day)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        body = teacher.get("/api/analytics/today")
        assert body.status_code == 200, body.text
        mine = body.json()

        ids = [session["session_id"] for session in mine["sessions"]]
        assert ids == ["DAY-MINE"], ids
        assert mine["session_counts"]["total"] == 1

        # And their student count is their own class, not the school.
        assert mine["students"]["total"] == len(MINE), mine["students"]
        assert mine["students"]["active"] == len(MINE)

        # The Super Admin sees both.
        everything = client.get("/api/analytics/today").json()
        assert set(session["session_id"]
                   for session in everything["sessions"]) == \
            {"DAY-MINE", "DAY-THEIRS"}
        assert everything["students"]["total"] == len(MINE) + len(THEIRS)

        teacher.close()
        print("PASS  a teacher's day holds their own sessions and their own "
              "student count; a Super Admin's holds the school's")
    finally:
        teardown(client, directory)


def test_the_day_states_the_scope_it_was_narrowed_to():
    client, directory = make_client()
    try:
        setup_school(client)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        scope = teacher.get("/api/analytics/today").json()["scope"]

        assert scope["unrestricted"] is False
        assert scope["is_super_admin"] is False
        assert [(item["class_name"], item["section"])
                for item in scope["user"]["assignments"]] == [("MECH", "A")]

        admin_scope = client.get("/api/analytics/today").json()["scope"]
        assert admin_scope["unrestricted"] is True
        assert admin_scope["is_super_admin"] is True

        teacher.close()
        print("PASS  the day reports which scope produced it, so a narrowed "
              "dashboard can say so rather than looking like an empty school")
    finally:
        teardown(client, directory)


def test_a_teacher_with_no_assignment_is_not_narrowed():
    client, directory = make_client()
    try:
        setup_school(client, assignments=[])
        day = today_iso()
        session_on("DAY-MINE", "MECH-A", [sid for sid, _ in MINE], day)
        session_on("DAY-THEIRS", "CIVIL-B", [sid for sid, _ in THEIRS], day)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        body = teacher.get("/api/analytics/today").json()

        # A teacher who has an account but no timetable yet should be able to
        # work rather than stare at an empty dashboard and assume it is broken.
        assert len(body["sessions"]) == 2, body["sessions"]
        assert body["scope"]["unrestricted"] is True

        teacher.close()
        print("PASS  a teacher with no class assignment sees everything, "
              "rather than an empty dashboard that looks broken")
    finally:
        teardown(client, directory)


# -- what it refuses to invent ----------------------------------------------

def test_a_morning_with_nothing_finalised_reports_no_attendance():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()

        # Scheduled and running, but nothing completed.
        session_on("DAY-SCHED", "MECH-A", [sid for sid, _ in MINE], day,
                   status="SCHEDULED", hour=9)
        session_on("DAY-RUN", "MECH-A", [sid for sid, _ in MINE], day,
                   status="ACTIVE", hour=11)

        body = client.get("/api/analytics/today").json()

        assert body["session_counts"]["total"] == 2
        assert body["session_counts"]["scheduled"] == 1
        assert body["session_counts"]["active"] == 1
        assert body["session_counts"]["completed"] == 0

        attendance = body["attendance"]
        assert attendance["has_data"] is False, attendance
        assert attendance["attendance_rate"] is None, (
            "an unfinalised day was given an attendance rate")
        assert attendance["completed_sessions"] == 0
        assert attendance["expected_students"] == 0, (
            "a running session's expectations were counted as absences")

        print("PASS  a day with no finalised session has no attendance rate "
              "and counts no absences")
    finally:
        teardown(client, directory)


def test_attendance_is_summed_over_finalised_sessions_only():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()

        session_on("DAY-DONE", "MECH-A", [sid for sid, _ in MINE], day,
                   status="COMPLETED", hour=9)
        record("DAY-DONE", "D-M1", "PRESENT")
        record("DAY-DONE", "D-M2", "ABSENT")

        # A second session still running, with no records at all.
        session_on("DAY-RUN", "MECH-A", [sid for sid, _ in MINE], day,
                   status="ACTIVE", hour=11)

        attendance = client.get("/api/analytics/today").json()["attendance"]

        assert attendance["has_data"] is True
        assert attendance["completed_sessions"] == 1
        # Two expectations, from the completed session only - not four.
        assert attendance["expected_students"] == 2, attendance
        assert attendance["satisfying_count"] == 1
        assert abs(attendance["attendance_rate"] - 0.5) < 1e-9
        assert attendance["counts"]["PRESENT"] == 1
        assert attendance["counts"]["ABSENT"] == 1

        print("PASS  today's attendance sums the finalised sessions only, so "
              "a running lesson adds no expectations and no absences")
    finally:
        teardown(client, directory)


def test_nothing_running_means_a_null_active_session():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()
        session_on("DAY-SCHED", "MECH-A", [sid for sid, _ in MINE], day,
                   status="SCHEDULED")

        body = client.get("/api/analytics/today").json()

        assert body["live"] is None, body["live"]
        assert body["active_session"] is None, (
            "a scheduled session was reported as the active one")

        # An ACTIVE session IS reported, because that one really is running.
        session_on("DAY-RUN", "MECH-A", [sid for sid, _ in MINE], day,
                   status="ACTIVE", hour=11)
        again = client.get("/api/analytics/today").json()
        assert again["active_session"]["session_id"] == "DAY-RUN"
        # Still no live camera: the runtime is what answers that, and no
        # pipeline has been started.
        assert again["live"] is None

        print("PASS  a scheduled lesson is not reported as active, and an "
              "active session is not reported as a running camera")
    finally:
        teardown(client, directory)


def test_yesterdays_sessions_are_not_in_todays_dashboard():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()
        yesterday = (datetime.fromisoformat(day) - timedelta(days=1)).date() \
                            .isoformat()

        session_on("DAY-TODAY", "MECH-A", [sid for sid, _ in MINE], day)
        session_on("DAY-BEFORE", "MECH-A", [sid for sid, _ in MINE],
                   yesterday)

        ids = [session["session_id"]
               for session in client.get("/api/analytics/today")
                                    .json()["sessions"]]
        assert ids == ["DAY-TODAY"], ids

        # And an explicit date reaches the other one.
        asked = client.get("/api/analytics/today?session_date=" + yesterday)
        assert asked.status_code == 200
        assert [session["session_id"]
                for session in asked.json()["sessions"]] == ["DAY-BEFORE"]
        assert asked.json()["session_date"] == yesterday

        print("PASS  the day panel holds one day, and another date can be "
              "asked for explicitly")
    finally:
        teardown(client, directory)


def test_an_unauthenticated_caller_gets_no_day():
    client, directory = make_client()
    try:
        setup_school(client)
        from backend.app import app

        stranger = TestClient(app)
        assert stranger.get("/api/analytics/today").status_code == 401
        stranger.close()

        print("PASS  the day dashboard refuses a caller with no session")
    finally:
        teardown(client, directory)


# -- the endpoints it is built on --------------------------------------------

def test_the_sessions_list_is_scoped_to_the_callers_classes():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()
        session_on("DAY-MINE", "MECH-A", [sid for sid, _ in MINE], day)
        session_on("DAY-THEIRS", "CIVIL-B", [sid for sid, _ in THEIRS], day)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        page = teacher.get("/api/sessions").json()

        assert [item["session_id"] for item in page["items"]] == ["DAY-MINE"]
        # The total must be the count of what this teacher may see, not of the
        # table - a filtered page with an unfiltered total is a leak and a
        # broken pager at once.
        assert page["total"] == 1, page["total"]

        teacher.close()
        print("PASS  the sessions list and its total are both narrowed to the "
              "caller's classes")
    finally:
        teardown(client, directory)


def test_one_session_its_register_and_its_evidence_are_all_scoped():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()
        session_on("DAY-THEIRS", "CIVIL-B", [sid for sid, _ in THEIRS], day)
        record("DAY-THEIRS", "D-C1", "PRESENT")

        teacher = signed_in("tsmith", TEACHER_PASSWORD)

        for path in ("/api/sessions/DAY-THEIRS",
                     "/api/sessions/DAY-THEIRS/attendance",
                     "/api/sessions/DAY-THEIRS/presence"):
            response = teacher.get(path)
            assert response.status_code == 404, (
                "a teacher read %s for a class they are not assigned to (%d)"
                % (path, response.status_code))

        # And a session that does not exist answers identically, so the status
        # code does not map the timetable.
        missing = teacher.get("/api/sessions/NO-SUCH-SESSION")
        assert missing.status_code == 404

        teacher.close()
        print("PASS  a session outside a teacher's classes, its register and "
              "its presence evidence all answer 404")
    finally:
        teardown(client, directory)


def test_the_class_list_names_only_the_callers_classes():
    client, directory = make_client()
    try:
        setup_school(client)
        day = today_iso()
        session_on("DAY-MINE", "MECH-A", [sid for sid, _ in MINE], day)
        session_on("DAY-THEIRS", "CIVIL-B", [sid for sid, _ in THEIRS], day)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        classes = [item["class_id"] for item in
                   teacher.get("/api/classes").json()]
        assert classes == ["MECH-A"], classes

        everything = [item["class_id"] for item in
                      client.get("/api/classes").json()]
        assert set(everything) == {"MECH-A", "CIVIL-B"}

        teacher.close()
        print("PASS  the class filter offers a teacher their own classes "
              "rather than naming every class in the school")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_day_panel_is_outside_the_block_that_hides_on_an_empty_database():
    """
    #overview-body is hidden until some attendance exists. The Today panel must
    not be inside it, or a school's first morning shows a blank page.
    """
    body_start = HTML.index('id="overview-body"')
    today_start = HTML.index('id="today-heading"')

    assert today_start < body_start, (
        "the Today panel is inside #overview-body and would be hidden on a "
        "database with no completed sessions")

    quick_start = HTML.index('id="quick-heading"')
    assert quick_start < body_start, (
        "the quick actions are inside #overview-body and would be hidden "
        "before the first session is finalised")

    print("PASS  Today and the quick actions render before any attendance "
          "exists, rather than being hidden with the summary")


def test_the_day_panel_says_no_data_rather_than_nothing():
    assert "loadToday" in JS
    assert 'api("/api/analytics/today")' in JS

    panel = JS[JS.index("async function loadToday"):
               JS.index("function renderTodayLive")]

    # card() renders null as the words "No data" - see the helper. The panel
    # must pass null rather than 0 when nothing has been finalised.
    assert "has_data" in panel, (
        "the panel does not distinguish a finalised day from an empty one")
    assert "no session has been finalised today" in panel

    print("PASS  the day panel passes null, not zero, when no session has "
          "been finalised")


def test_the_quick_actions_are_all_present_and_reuse_one_code_path():
    for element in ("quick-add-student", "quick-add-teacher",
                    "quick-create-session", "quick-start-live",
                    "quick-view-reports"):
        assert 'id="%s"' % element in HTML, "no %s action" % element

    for label in ("Add Student", "Add Teacher", "Create Session",
                  "Start Live Class", "View Reports"):
        assert label in HTML, "the quick actions do not offer %r" % label

    # Each opens the SAME thing its own page's button opens, rather than a
    # second implementation that drifts.
    binder = JS[JS.index("function bindQuickActions"):]
    binder = binder[:binder.index("\n}\n")]
    for call in ("openStudentDialog()", "openUserDialog(null)",
                 "openSessionDialog()", 'switchView("live")',
                 'switchView("reports")'):
        assert call in binder, "a quick action does not reuse %s" % call

    # Adding a teacher is Super Admin only on the server, and hidden here.
    assert "renderQuickActions" in JS
    gate = JS[JS.index("function renderQuickActions"):]
    gate = gate[:gate.index("\n}\n")]
    assert "quick-add-teacher" in gate and "is_super_admin" in gate, (
        "Add Teacher is offered to a teacher as well as an administrator")

    print("PASS  all five quick actions exist, each reuses the existing "
          "dialog, and Add Teacher is gated on the role")


def test_the_live_indicator_can_have_its_motion_switched_off():
    assert ".live-dot" in CSS
    assert "live-pulse" in CSS

    reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".live-dot { animation: none" in reduced, (
        "the live indicator keeps pulsing for somebody who asked for less "
        "motion")

    print("PASS  the live indicator stops pulsing under "
          "prefers-reduced-motion and stays visible")


def test_every_element_the_day_panel_uses_exists_in_the_page():
    used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', JS))
    present = set(re.findall(r'id="([a-z0-9-]+)"', HTML))

    missing = sorted(used - present)
    assert not missing, "app.js reaches for ids the page does not have: %s" \
        % missing

    print("PASS  all %d element ids the script uses exist in the page"
          % len(used))


def test_tests_never_touch_the_real_database():
    from database import DEFAULT_DATABASE_PATH

    real = Path(DEFAULT_DATABASE_PATH)
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        setup_school(client)
        session_on("DAY-MINE", "MECH-A", [sid for sid, _ in MINE],
                   today_iso())
        client.get("/api/analytics/today")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_a_teacher_sees_only_their_own_classes_in_their_day,
        test_the_day_states_the_scope_it_was_narrowed_to,
        test_a_teacher_with_no_assignment_is_not_narrowed,
        test_a_morning_with_nothing_finalised_reports_no_attendance,
        test_attendance_is_summed_over_finalised_sessions_only,
        test_nothing_running_means_a_null_active_session,
        test_yesterdays_sessions_are_not_in_todays_dashboard,
        test_an_unauthenticated_caller_gets_no_day,
        test_the_sessions_list_is_scoped_to_the_callers_classes,
        test_one_session_its_register_and_its_evidence_are_all_scoped,
        test_the_class_list_names_only_the_callers_classes,
        test_the_day_panel_is_outside_the_block_that_hides_on_an_empty_database,
        test_the_day_panel_says_no_data_rather_than_nothing,
        test_the_quick_actions_are_all_present_and_reuse_one_code_path,
        test_the_live_indicator_can_have_its_motion_switched_off,
        test_every_element_the_day_panel_uses_exists_in_the_page,
        test_tests_never_touch_the_real_database,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                       # noqa: BLE001
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__,
                                        error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
