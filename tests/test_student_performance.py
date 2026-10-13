"""
The student performance profile, and who is allowed to read one.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test builds its own temporary
SQLite file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That a student's profile carries the registry detail a teacher needs in
    order to read a rate (class, section, roll number, enrolment) alongside the
    rates themselves; that the totals reconcile with the history printed under
    them; that the attendance trend refuses to name a direction it has too few
    sessions to support; and - the part that is a permission rather than a
    feature - that /api/analytics is scoped to the caller's classes in every
    one of its three per-object endpoints.

    That last one was a real hole. The Students page has always narrowed a
    teacher to their own classes, but the analytics endpoint behind the profile
    accepted any student id from any signed-in account, which made the scoping
    on the page a presentational detail. A teacher who could not see class
    MECH-A could read every child in it by asking for them by id.

WHAT THIS DOES NOT VERIFY
    Any accuracy claim. Every number here is arithmetic over rows this file
    wrote, and the trend is deliberately the simplest split that can be
    recounted by hand - see analytics_service._attendance_trend.

Run with:
    python tests/test_student_performance.py
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

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

ADMIN = {"username": "head", "display_name": "The Head",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

# Two classes, so "the caller's own classes" is a question with a wrong answer
# available. MECH-A is the teacher's; CIVIL-B is not.
MINE = ("S-MINE", "Asha Rao", "MECH", "A")
THEIRS = ("S-THEIRS", "Vikram Nair", "CIVIL", "B")


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_perf_"))
    state.configure(directory / "performance.db")
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
    """A separate client with its own cookie jar."""
    from backend.app import app
    client = TestClient(app)
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def setup_school(client, assignments=None):
    """An administrator, one teacher, and a student in each of two classes."""
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

    created = client.post("/api/users", json={
        "username": "tsmith", "display_name": "T Smith",
        "password": TEACHER_PASSWORD, "role": "TEACHER",
        "assignments": assignments if assignments is not None
        else [{"class_name": "MECH", "section": "A"}]})
    assert created.status_code == 201, created.text

    for student_id, name, class_name, section in (MINE, THEIRS):
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": "7",
            "class_name": class_name, "section": section})
        assert response.status_code == 201, response.text

    return created.json()


def completed_session(session_id, class_id, student_ids, day=0):
    """
    A session that ran and finished, written through the Step 6 repository.

    Built from the same objects the rule engine produces, so nothing here
    depends on a shape only this test knows about.
    """
    database = state.get_database()
    start = START + timedelta(days=day)
    end = start + timedelta(hours=1)

    session = AttendanceSession(session_id, class_id, start, end,
                                policy_version="1.0",
                                expected_students=list(student_ids))
    session.start()
    session.complete(completed_at=end)
    SessionRepository(database).save(session)
    return session


def record(session_id, student_id, status, ratio=0.9, seconds=3240.0):
    decision = AttendanceDecision(student_id, session_id, status,
                                  "synthetic test record", "1.0")
    decision.present_seconds = seconds
    decision.session_seconds = 3600.0
    decision.presence_ratio = ratio
    AttendanceRepository(state.get_database()).save_decision(decision)


def history_for(student_id, statuses, class_id="MECH-A"):
    """One completed session per status, oldest first."""
    for index, status in enumerate(statuses):
        session_id = "%s-SES-%d" % (student_id, index)
        completed_session(session_id, class_id, [student_id], day=index)
        if status is not None:
            record(session_id, student_id, status)


# -- what a profile contains -------------------------------------------------

def test_the_profile_names_the_student_before_it_rates_them():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT", "ABSENT", "LATE"])

        profile = client.get("/api/analytics/students/" + MINE[0])
        assert profile.status_code == 200, profile.text
        body = profile.json()

        assert body["student_id"] == MINE[0]
        assert body["name"] == MINE[1]
        assert body["class_name"] == "MECH", body
        assert body["section"] == "A", body
        assert body["roll_number"] == "7", body
        assert body["status"] == "ACTIVE"

        print("PASS  a profile carries the class, section and roll number the "
              "rate was earned in")
    finally:
        teardown(client, directory)


def test_the_totals_add_up_to_the_history_underneath_them():
    client, directory = make_client()
    try:
        setup_school(client)

        # Three satisfying (PRESENT, LATE, LEFT_EARLY), one PARTIAL which is
        # not satisfying, one ABSENT, and one session that completed without
        # ever being evaluated.
        history_for(MINE[0],
                    ["PRESENT", "LATE", "LEFT_EARLY", "PARTIAL", "ABSENT",
                     None])

        body = client.get("/api/analytics/students/" + MINE[0]).json()

        assert body["expected_sessions"] == 6, body["expected_sessions"]
        assert body["evaluated_sessions"] == 5
        assert body["sessions_without_record"] == 1
        assert body["satisfying_count"] == 3, body["counts"]

        # ABSENT plus the session that was never evaluated: from a student's
        # point of view both are sessions they got no credit for.
        assert body["absent_sessions"] == 2, body["absent_sessions"]

        # And the rate is the satisfying share of what they were EXPECTED at,
        # not of what happened to be recorded.
        assert abs(body["attendance_rate"] - 0.5) < 1e-9, \
            body["attendance_rate"]

        assert len(body["history"]) == 6, "the history is missing a session"
        unevaluated = [entry for entry in body["history"]
                       if entry["status"] is None]
        assert len(unevaluated) == 1, "the unevaluated session was hidden"

        print("PASS  total, attended and absent counts reconcile with the "
              "session history and with the expected denominator")
    finally:
        teardown(client, directory)


def test_every_session_carries_its_date_and_its_status():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT", "ABSENT", "LATE"])

        history = client.get("/api/analytics/students/" + MINE[0]) \
                        .json()["history"]

        assert [entry["status"] for entry in history] == \
            ["PRESENT", "ABSENT", "LATE"], "sessions came back out of order"

        for entry in history:
            assert entry["session_date"], "a session has no date"
            assert entry["session_id"], "a session has no identifier"
            assert entry["class_id"] == "MECH-A"

        print("PASS  the history is one row per session, in date order, each "
              "with its own date and recorded status")
    finally:
        teardown(client, directory)


# -- the trend ---------------------------------------------------------------

def test_a_trend_is_not_named_until_there_are_sessions_to_name_it_from():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT", "ABSENT"])

        trend = client.get("/api/analytics/students/" + MINE[0]) \
                      .json()["trend"]

        assert trend["direction"] == "NO_DATA", trend
        assert trend["earlier_rate"] is None
        assert trend["recent_rate"] is None
        assert trend["change"] is None
        assert trend["sample_size"] == 2

        print("PASS  two sessions produce no trend direction and no invented "
              "rate")
    finally:
        teardown(client, directory)


def test_a_trend_reports_the_direction_the_sessions_actually_moved():
    client, directory = make_client()
    try:
        setup_school(client)

        # Four evaluated sessions: absent, absent, present, present. The
        # earlier half is 0% and the recent half is 100%.
        history_for(MINE[0], ["ABSENT", "ABSENT", "PRESENT", "PRESENT"])

        trend = client.get("/api/analytics/students/" + MINE[0]) \
                      .json()["trend"]

        assert trend["direction"] == "IMPROVING", trend
        assert trend["earlier_rate"] == 0.0, trend
        assert trend["recent_rate"] == 1.0, trend
        assert trend["change"] == 1.0, trend
        assert trend["sample_size"] == 4

        print("PASS  a student whose recent sessions are better is reported "
              "as improving, with both halves shown")
    finally:
        teardown(client, directory)


def test_the_other_two_directions_are_reachable():
    client, directory = make_client()
    try:
        setup_school(client)

        history_for(MINE[0], ["PRESENT", "PRESENT", "ABSENT", "ABSENT"])
        declining = client.get("/api/analytics/students/" + MINE[0]) \
                          .json()["trend"]
        assert declining["direction"] == "DECLINING", declining

        history_for(THEIRS[0], ["PRESENT", "PRESENT", "PRESENT", "PRESENT"],
                    class_id="CIVIL-B")
        steady = client.get("/api/analytics/students/" + THEIRS[0]) \
                       .json()["trend"]
        assert steady["direction"] == "STEADY", steady
        assert steady["change"] == 0.0

        print("PASS  declining and steady are both reachable, and unchanged "
              "attendance is not reported as a movement")
    finally:
        teardown(client, directory)


# -- who may read one --------------------------------------------------------

def test_a_teacher_cannot_read_a_profile_outside_their_classes():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT"])
        history_for(THEIRS[0], ["PRESENT"], class_id="CIVIL-B")

        teacher = signed_in("tsmith", TEACHER_PASSWORD)

        mine = teacher.get("/api/analytics/students/" + MINE[0])
        assert mine.status_code == 200, mine.text

        theirs = teacher.get("/api/analytics/students/" + THEIRS[0])
        assert theirs.status_code == 404, (
            "a teacher read a profile from a class they are not assigned to: "
            "%s" % theirs.text)

        # 404 and not 403, so the status code cannot be used to discover which
        # student ids exist in a class the caller may not see.
        invented = teacher.get("/api/analytics/students/NO-SUCH-STUDENT")
        assert invented.status_code == 404
        assert theirs.json()["detail"] == invented.json()["detail"], (
            "a forbidden student is distinguishable from a missing one")

        teacher.close()
        print("PASS  a profile outside a teacher's classes answers 404, and "
              "identically to a student who does not exist")
    finally:
        teardown(client, directory)


def test_the_view_all_students_grant_opens_reading_and_nothing_else():
    client, directory = make_client()
    try:
        account = setup_school(client)
        history_for(THEIRS[0], ["PRESENT"], class_id="CIVIL-B")

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        assert teacher.get(
            "/api/analytics/students/" + THEIRS[0]).status_code == 404

        granted = client.patch("/api/users/%d" % account["user_id"],
                               json={"can_view_all_students": True})
        assert granted.status_code == 200, granted.text

        # Changing what an account may see signs that account out - see
        # auth_service.update_user, which deletes the tokens deliberately so a
        # permission change takes effect now rather than whenever a tab is next
        # reloaded. Assert that, then sign back in.
        assert teacher.get(
            "/api/analytics/students/" + MINE[0]).status_code == 401, (
            "a permission change left an open session running on the old "
            "permissions")
        teacher.close()

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        assert teacher.get(
            "/api/analytics/students/" + THEIRS[0]).status_code == 200, (
            "the grant did not open reading")

        # Reading only. Editing that student is still refused.
        refused = teacher.patch("/api/students/" + THEIRS[0],
                                json={"name": "Renamed By Somebody Else"})
        assert refused.status_code == 404, (
            "a view-all grant also allowed a write: %s" % refused.text)

        teacher.close()
        print("PASS  can_view_all_students opens every profile to reading and "
              "still refuses the write")
    finally:
        teardown(client, directory)


def test_a_super_admin_reads_every_profile():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT"])
        history_for(THEIRS[0], ["ABSENT"], class_id="CIVIL-B")

        for student_id, _, _, _ in (MINE, THEIRS):
            response = client.get("/api/analytics/students/" + student_id)
            assert response.status_code == 200, response.text

        print("PASS  a Super Admin reads every student's profile")
    finally:
        teardown(client, directory)


def test_session_and_class_analytics_are_scoped_too():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT"])
        history_for(THEIRS[0], ["PRESENT"], class_id="CIVIL-B")

        teacher = signed_in("tsmith", TEACHER_PASSWORD)

        assert teacher.get(
            "/api/analytics/sessions/%s-SES-0" % MINE[0]).status_code == 200

        outside = teacher.get("/api/analytics/sessions/%s-SES-0" % THEIRS[0])
        assert outside.status_code == 404, (
            "a teacher read another class's session register through "
            "analytics: %s" % outside.text)

        assert teacher.get("/api/analytics/classes/MECH-A").status_code == 200
        blocked = teacher.get("/api/analytics/classes/CIVIL-B")
        assert blocked.status_code == 404, (
            "a teacher read another class's analytics: %s" % blocked.text)

        teacher.close()
        print("PASS  session and class analytics are scoped to the caller's "
              "classes, not only the student endpoint")
    finally:
        teardown(client, directory)


def test_an_unauthenticated_caller_reaches_no_profile():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT"])

        from backend.app import app
        stranger = TestClient(app)

        for path in ("/api/analytics/students/" + MINE[0],
                     "/api/analytics/sessions/%s-SES-0" % MINE[0],
                     "/api/analytics/classes/MECH-A"):
            response = stranger.get(path)
            assert response.status_code == 401, (
                "%s answered %d to a caller with no session"
                % (path, response.status_code))

        stranger.close()
        print("PASS  every per-object analytics endpoint refuses a caller "
              "with no session")
    finally:
        teardown(client, directory)


def test_a_student_portal_token_reaches_no_staff_profile():
    client, directory = make_client()
    try:
        setup_school(client)
        history_for(MINE[0], ["PRESENT"])

        issued = client.post("/api/portal/accounts", json={
            "student_id": MINE[0], "username": "asha",
            "password": "issued-by-the-office"})
        assert issued.status_code == 201, issued.text

        from backend.app import app
        student = TestClient(app)
        assert student.post("/api/portal/login", json={
            "username": "asha",
            "password": "issued-by-the-office"}).status_code == 200

        # Their own id included. The portal is the only door a student has, and
        # it takes no identifier at all.
        for path in ("/api/analytics/students/" + MINE[0],
                     "/api/analytics/students/" + THEIRS[0],
                     "/api/students/" + MINE[0]):
            response = student.get(path)
            assert response.status_code == 401, (
                "a portal cookie reached %s (%d)"
                % (path, response.status_code))

        student.close()
        print("PASS  a portal cookie reaches no staff profile endpoint, not "
              "even the student's own")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_students_page_opens_a_profile_from_the_row_itself():
    assert "data-row-student" in JS, (
        "no row-level handler: a student row is not clickable")
    assert "tr.student-row" in CSS, (
        "a clickable row carries no affordance in the stylesheet")
    assert "cursor: pointer" in CSS

    # The action buttons must survive. A click on Remove must not also open
    # the profile behind the confirmation dialog.
    opener = JS[JS.index("data-row-student"):]
    assert 'closest("button' in opener, (
        "the row handler does not exclude clicks that landed on a control")
    assert "data-view-student" in JS, (
        "the explicit View button was removed, leaving no keyboard route in")

    print("PASS  a student row opens the profile, keeps its buttons, and the "
          "row handler ignores clicks on them")


def test_the_profile_shows_the_figures_the_page_promises():
    assert 'id="student-trend"' in HTML, "the trend has nowhere to render"
    assert ".trend-label" in CSS

    for label in ("Total sessions", "Sessions attended", "Sessions absent",
                  "Roll number", "Face enrollment"):
        assert '"%s"' % label in JS, "the profile never renders %r" % label

    # Rule 1 of the stylesheet: a direction is a word, and colour only agrees
    # with it.
    for word in ("Improving", "Declining", "Steady", "Not enough sessions"):
        assert word in JS, "the trend has no written form of %r" % word

    print("PASS  the profile renders totals, enrolment and a trend stated in "
          "words rather than in colour alone")


def test_every_element_the_profile_uses_exists_in_the_page():
    """The ids app.js reaches for on this page must be in index.html."""
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
        history_for(MINE[0], ["PRESENT", "ABSENT", "LATE"])
        client.get("/api/analytics/students/" + MINE[0])
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_the_profile_names_the_student_before_it_rates_them,
        test_the_totals_add_up_to_the_history_underneath_them,
        test_every_session_carries_its_date_and_its_status,
        test_a_trend_is_not_named_until_there_are_sessions_to_name_it_from,
        test_a_trend_reports_the_direction_the_sessions_actually_moved,
        test_the_other_two_directions_are_reachable,
        test_a_teacher_cannot_read_a_profile_outside_their_classes,
        test_the_view_all_students_grant_opens_reading_and_nothing_else,
        test_a_super_admin_reads_every_profile,
        test_session_and_class_analytics_are_scoped_too,
        test_an_unauthenticated_caller_reaches_no_profile,
        test_a_student_portal_token_reaches_no_staff_profile,
        test_the_students_page_opens_a_profile_from_the_row_itself,
        test_the_profile_shows_the_figures_the_page_promises,
        test_every_element_the_profile_uses_exists_in_the_page,
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
