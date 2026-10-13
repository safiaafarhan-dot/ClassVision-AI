"""
Every registered student gets a Student ID and a password, and reaches only
their own attendance with it.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That a login can be issued without an administrator inventing a username
    and a password per child - the username defaults to the STUDENT ID and the
    password is generated - and that "issue to every student who has not got
    one" is idempotent, so pressing it twice cannot reset a credential a class
    is already using.

    Then the part that matters more: that a signed-in student can read their
    own attendance and has no way to ask for anybody else's. The portal takes
    no student identifier at all, which is checked here by reading the routing
    table rather than by trying a handful of URLs and concluding from three
    404s that a fourth does not exist.

    And that a generated password appears in exactly one response and nowhere
    else - not in a later read of the account, not in the audit log, and not
    in the database file.

WHAT THIS DOES NOT VERIFY
    That this authentication is secure against a determined attacker. The
    portal reuses the staff password hashing and session handling and inherits
    both their properties and their limits - see the note at the top of
    tests/test_auth_rbac.py.

Run with:
    python tests/test_student_login.py
"""

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
from backend.services.portal_service import (  # noqa: E402
    FIRST_PASSWORD_ALPHABET,
    FIRST_PASSWORD_LENGTH,
    generate_first_password,
)
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

ADMIN = {"username": "registrar", "display_name": "The Registrar",
         "password": "a-long-enough-passphrase"}
TEACHER = {"username": "tmiller", "display_name": "T Miller",
           "password": "another-long-passphrase", "role": "TEACHER"}

ROSTER = (
    ("STU-0001", "Asha Rao", "MECH", "A"),
    ("STU-0002", "Vikram Nair", "MECH", "A"),
    ("STU-0003", "Leena Bose", "CIVIL", "B"),
)

OWN_PASSWORD = "a-password-only-they-know"


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_login_"))
    state.configure(directory / "login.db")
    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def fresh_client():
    """A separate client with its own cookie jar, like a student's phone."""
    from backend.app import app
    return TestClient(app)


def setup_school(client):
    """An administrator, a teacher, and three registered students."""
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201
    assert client.post("/api/users", json=TEACHER).status_code == 201

    for student_id, name, class_name, section in ROSTER:
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": "1",
            "class_name": class_name, "section": section})
        assert response.status_code == 201, response.text


def completed_session(session_id, class_id, student_ids, day=0):
    database = state.get_database()
    start = START + timedelta(days=day)
    end = start + timedelta(hours=1)

    session = AttendanceSession(session_id, class_id, start, end,
                                policy_version="1.0",
                                expected_students=list(student_ids))
    session.start()
    session.complete(completed_at=end)
    SessionRepository(database).save(session)


def record(session_id, student_id, status):
    decision = AttendanceDecision(student_id, session_id, status,
                                  "synthetic test record", "1.0")
    decision.present_seconds = 3240.0
    decision.session_seconds = 3600.0
    decision.presence_ratio = 0.9
    AttendanceRepository(state.get_database()).save_decision(decision)


def sign_in(username, password):
    client = fresh_client()
    response = client.post("/api/portal/login",
                           json={"username": username, "password": password})
    return client, response


def settled(username, first_password):
    """Get past the change-the-handed-out-password gate."""
    client, response = sign_in(username, first_password)
    assert response.status_code == 200, response.text
    assert client.post("/api/portal/me/password", json={
        "current_password": first_password,
        "new_password": OWN_PASSWORD}).status_code == 200
    client.close()

    client, response = sign_in(username, OWN_PASSWORD)
    assert response.status_code == 200, response.text
    return client


# -- generated credentials ---------------------------------------------------

def test_a_generated_password_avoids_the_characters_people_mistype():
    for _ in range(40):
        password = generate_first_password()
        assert len(password) == FIRST_PASSWORD_LENGTH
        assert set(password) <= set(FIRST_PASSWORD_ALPHABET)

    # These are read off a printed slip and typed into a phone by somebody who
    # did not choose them.
    for character in "lI1O0":
        assert character not in FIRST_PASSWORD_ALPHABET, (
            "%r is in the generated-password alphabet" % character)

    # And two of them are not the same password.
    assert len({generate_first_password() for _ in range(50)}) == 50

    print("PASS  a generated first password is long, random and free of the "
          "characters people mistype")


def test_a_login_needs_neither_a_username_nor_a_password_to_be_invented():
    client, directory = make_client()
    try:
        setup_school(client)

        issued = client.post("/api/portal/accounts",
                             json={"student_id": "STU-0001"})
        assert issued.status_code == 201, issued.text
        account = issued.json()

        # The username IS the student id, which is the thing a student can be
        # told once and remember.
        assert account["username"] == "STU-0001", account
        assert account["student_id"] == "STU-0001"
        assert account["password_is_generated"] is True
        assert len(account["password"]) == FIRST_PASSWORD_LENGTH

        # And it works.
        student, response = sign_in("STU-0001", account["password"])
        assert response.status_code == 200, response.text
        student.close()

        print("PASS  a login issued with no username and no password becomes "
              "the Student ID with a generated password, and it signs in")
    finally:
        teardown(client, directory)


def test_an_explicit_username_and_password_still_work_unchanged():
    client, directory = make_client()
    try:
        setup_school(client)

        issued = client.post("/api/portal/accounts", json={
            "student_id": "STU-0001", "username": "asha",
            "password": "issued-by-the-office"})
        assert issued.status_code == 201, issued.text
        account = issued.json()

        assert account["username"] == "asha"

        # A password the administrator chose is NOT echoed back: they already
        # know it, and repeating a secret buys nothing.
        assert "password" not in account, account

        student, response = sign_in("asha", "issued-by-the-office")
        assert response.status_code == 200
        student.close()

        print("PASS  a chosen username and password are used as given, and a "
              "chosen password is not echoed back")
    finally:
        teardown(client, directory)


# -- every student ----------------------------------------------------------

def test_every_registered_student_can_be_given_a_login_at_once():
    client, directory = make_client()
    try:
        setup_school(client)

        result = client.post("/api/portal/accounts/issue-missing")
        assert result.status_code == 201, result.text
        body = result.json()

        assert len(body["issued"]) == len(ROSTER), body
        assert body["skipped"] == [], body["skipped"]

        by_id = {account["student_id"]: account for account in body["issued"]}
        assert set(by_id) == {student_id for student_id, _, _, _ in ROSTER}

        # Each is the student's own id, with its own password.
        passwords = set()
        for student_id, name, _, _ in ROSTER:
            account = by_id[student_id]
            assert account["username"] == student_id
            assert account["name"] == name
            passwords.add(account["password"])

        assert len(passwords) == len(ROSTER), "two students share a password"

        # And every one of them signs in.
        for student_id, _, _, _ in ROSTER:
            student, response = sign_in(student_id,
                                        by_id[student_id]["password"])
            assert response.status_code == 200, (
                "%s could not sign in: %s" % (student_id, response.text))
            student.close()

        print("PASS  every registered student is given a unique Student ID "
              "and password in one operation, and all of them sign in")
    finally:
        teardown(client, directory)


def test_issuing_to_everybody_twice_changes_nothing_the_second_time():
    client, directory = make_client()
    try:
        setup_school(client)

        first = client.post("/api/portal/accounts/issue-missing").json()
        known = {account["student_id"]: account["password"]
                 for account in first["issued"]}

        second = client.post("/api/portal/accounts/issue-missing")
        assert second.status_code == 201, second.text
        body = second.json()

        assert body["issued"] == [], "a second run issued a duplicate login"
        assert len(body["skipped"]) == len(ROSTER)
        for entry in body["skipped"]:
            assert "already has" in entry["reason"], entry

        # The crucial part: the passwords from the FIRST run still work. A bulk
        # button that silently resets credentials a class is using is the kind
        # of button somebody presses twice.
        for student_id, password in known.items():
            student, response = sign_in(student_id, password)
            assert response.status_code == 200, (
                "the second run invalidated %s's password" % student_id)
            student.close()

        print("PASS  issuing to every student twice creates nothing and "
              "resets no existing password")
    finally:
        teardown(client, directory)


def test_a_student_registered_afterwards_is_picked_up_by_the_next_run():
    client, directory = make_client()
    try:
        setup_school(client)
        client.post("/api/portal/accounts/issue-missing")

        assert client.post("/api/students", json={
            "student_id": "STU-0004", "name": "Newly Arrived",
            "class_name": "MECH", "section": "A"}).status_code == 201

        body = client.post("/api/portal/accounts/issue-missing").json()

        assert [account["student_id"] for account in body["issued"]] == \
            ["STU-0004"], body["issued"]
        assert len(body["skipped"]) == len(ROSTER)

        print("PASS  a student registered after the first run gets a login "
              "from the next one, and nobody else is touched")
    finally:
        teardown(client, directory)


def test_bulk_issuing_is_super_admin_only():
    client, directory = make_client()
    try:
        setup_school(client)

        teacher = fresh_client()
        assert teacher.post("/api/auth/login", json={
            "username": TEACHER["username"],
            "password": TEACHER["password"]}).status_code == 200

        refused = teacher.post("/api/portal/accounts/issue-missing")
        assert refused.status_code == 403, refused.text
        teacher.close()

        stranger = fresh_client()
        assert stranger.post(
            "/api/portal/accounts/issue-missing").status_code == 401
        stranger.close()

        # And nothing was created by either attempt.
        assert client.get("/api/portal/accounts").json() == []

        print("PASS  only a Super Admin can issue logins in bulk, and a "
              "refused attempt creates nothing")
    finally:
        teardown(client, directory)


# -- what a signed-in student can see ---------------------------------------

def test_a_student_sees_their_own_attendance_and_its_percentage():
    client, directory = make_client()
    try:
        setup_school(client)
        issued = {account["student_id"]: account["password"]
                  for account in
                  client.post("/api/portal/accounts/issue-missing")
                        .json()["issued"]}

        # Two sessions for MECH-A. STU-0001 attended one of them.
        completed_session("MECH-1", "MECH-A", ["STU-0001", "STU-0002"], day=0)
        completed_session("MECH-2", "MECH-A", ["STU-0001", "STU-0002"], day=1)
        record("MECH-1", "STU-0001", "PRESENT")
        record("MECH-1", "STU-0002", "PRESENT")
        record("MECH-2", "STU-0001", "ABSENT")
        record("MECH-2", "STU-0002", "PRESENT")

        student = settled("STU-0001", issued["STU-0001"])

        performance = student.get("/api/portal/me/performance")
        assert performance.status_code == 200, performance.text
        body = performance.json()

        assert body["student"]["student_id"] == "STU-0001"
        assert body["student"]["name"] == "Asha Rao"
        assert body["student"]["class_name"] == "MECH"

        attendance = body["attendance"]
        assert attendance["expected_sessions"] == 2, attendance
        assert attendance["sessions_attended"] == 1, attendance
        assert abs(attendance["attendance_rate"] - 0.5) < 1e-9, attendance

        history = student.get("/api/portal/me/attendance")
        assert history.status_code == 200
        items = history.json()["items"]
        assert len(items) == 2, items

        # Their own sessions, with a date and a status on each.
        assert {item["session_id"] for item in items} == {"MECH-1", "MECH-2"}
        assert {item["status"] for item in items} == {"PRESENT", "ABSENT"}
        for item in items:
            assert item["student_id"] == "STU-0001"
            assert item["session_date"], item

        student.close()
        print("PASS  a student sees their own percentage, present/absent "
              "status, history and session dates")
    finally:
        teardown(client, directory)


def test_a_student_sees_no_trace_of_another_student():
    client, directory = make_client()
    try:
        setup_school(client)
        issued = {account["student_id"]: account["password"]
                  for account in
                  client.post("/api/portal/accounts/issue-missing")
                        .json()["issued"]}

        completed_session("MECH-1", "MECH-A", ["STU-0001", "STU-0002"])
        record("MECH-1", "STU-0001", "PRESENT")
        record("MECH-1", "STU-0002", "ABSENT")

        student = settled("STU-0001", issued["STU-0001"])

        everything = "".join([
            student.get("/api/portal/me").text,
            student.get("/api/portal/me/attendance").text,
            student.get("/api/portal/me/performance").text,
        ])

        for other_id, other_name, _, _ in ROSTER[1:]:
            assert other_id not in everything, (
                "%s appears in another student's portal" % other_id)
            assert other_name not in everything, (
                "%s appears in another student's portal" % other_name)

        student.close()
        print("PASS  nothing a student can read from the portal mentions "
              "another student's id or name")
    finally:
        teardown(client, directory)


def test_no_portal_endpoint_takes_a_student_identifier_at_all():
    """
    Read from the routing table, not from a guessed list of URLs.

    Trying a handful of paths and concluding from four 404s that a fifth does
    not exist is how this kind of check passes while the hole stays open. The
    app knows every path it serves; this asks it.

    Via app.openapi() rather than app.routes, and that detail matters: this
    FastAPI wraps an included router in an _IncludedRouter object with no
    `path` of its own, so walking app.routes for "/api/portal" finds NOTHING
    and a test written that way passes without checking anything. The
    generated schema is the reliable list, and the assertion below that the
    portal surface is non-empty is what stops this silently emptying again.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        from backend.app import app

        paths = [path for path in app.openapi()["paths"]
                 if path.startswith("/api/portal")]

        assert len(paths) >= 8, (
            "the portal surface came back as %d paths, so this test is not "
            "reading the routing table: %s" % (len(paths), paths))

        student_facing = [path for path in paths
                          # Administration is Super Admin only and
                          # legitimately names a student. The student-facing
                          # surface is /me plus signing in.
                          if not path.startswith("/api/portal/accounts")]

        offenders = [path for path in student_facing if "{" in path]
        assert not offenders, (
            "a student-facing portal endpoint accepts a path parameter: %s"
            % offenders)

        # And the /me endpoints take no student id as a QUERY parameter
        # either - a filter is as good as a path segment to somebody trying
        # their friend's id.
        for path in student_facing:
            for method, operation in app.openapi()["paths"][path].items():
                names = {parameter["name"]
                         for parameter in operation.get("parameters", [])}
                assert "student_id" not in names, (
                    "%s %s accepts a student_id parameter" % (method, path))

        print("PASS  no student-facing portal route takes a student id, in "
              "the path or in the query string")
    finally:
        teardown(client, directory)


def test_a_student_cannot_change_any_attendance():
    client, directory = make_client()
    try:
        setup_school(client)
        issued = {account["student_id"]: account["password"]
                  for account in
                  client.post("/api/portal/accounts/issue-missing")
                        .json()["issued"]}

        completed_session("MECH-1", "MECH-A", ["STU-0001"])
        record("MECH-1", "STU-0001", "ABSENT")

        student = settled("STU-0001", issued["STU-0001"])

        # Their own record, and the one they would most want to change.
        attempts = [
            ("post", "/api/sessions/MECH-1/attendance/STU-0001/override",
             {"status": "PRESENT", "reason": "I was there"}),
            ("delete", "/api/sessions/MECH-1/attendance/STU-0001/override",
             None),
            ("patch", "/api/students/STU-0001", {"name": "Someone Else"}),
            ("delete", "/api/students/STU-0001", None),
            ("post", "/api/sessions", {"class_id": "MECH-A"}),
        ]

        for method, path, body in attempts:
            call = getattr(student, method)
            response = call(path, json=body) if body is not None else call(path)
            assert response.status_code in (401, 403, 405), (
                "%s %s answered %d to a student"
                % (method.upper(), path, response.status_code))

        # And the record is untouched.
        row = client.get("/api/sessions/MECH-1/attendance").json()["items"][0]
        assert row["status"] == "ABSENT", row
        assert row["is_override"] is False, row

        student.close()
        print("PASS  a student cannot override, edit or delete attendance, "
              "and the record they tried to change is unaltered")
    finally:
        teardown(client, directory)


def test_a_student_reaches_no_teacher_page_or_camera():
    client, directory = make_client()
    try:
        setup_school(client)
        issued = {account["student_id"]: account["password"]
                  for account in
                  client.post("/api/portal/accounts/issue-missing")
                        .json()["issued"]}

        student = settled("STU-0001", issued["STU-0001"])

        # Live classroom and the camera, student management, system settings,
        # and the accounts that administer the portal itself.
        for method, path in (
                ("post", "/api/live/start"),
                ("post", "/api/live/stop"),
                ("get", "/api/live/status"),
                ("get", "/api/live/presence"),
                ("get", "/api/live/frame.jpg"),
                ("get", "/api/students"),
                ("get", "/api/sessions"),
                ("get", "/api/analytics/overview"),
                ("get", "/api/cameras"),
                ("get", "/api/users"),
                ("get", "/api/users/audit"),
                ("get", "/api/portal/accounts"),
                ("post", "/api/portal/accounts/issue-missing")):
            call = getattr(student, method)
            response = call(path, json={}) if method == "post" else call(path)
            assert response.status_code in (401, 403), (
                "a student reached %s %s (%d)"
                % (method.upper(), path, response.status_code))

        student.close()
        print("PASS  a portal session reaches no live camera, no student "
              "management, no settings and no account administration")
    finally:
        teardown(client, directory)


# -- what is never stored or shown twice -------------------------------------

def test_a_generated_password_appears_once_and_is_never_stored():
    client, directory = make_client()
    try:
        setup_school(client)

        body = client.post("/api/portal/accounts/issue-missing").json()
        secrets_issued = [account["password"] for account in body["issued"]]
        assert secrets_issued

        # Not in a later read of the accounts.
        listed = client.get("/api/portal/accounts").text
        for password in secrets_issued:
            assert password not in listed, (
                "a generated password came back from a later read")
        assert "password_hash" not in listed

        # Not in the audit log. The path is /api/users/audit - asserted as a
        # 200 first, because a 404 body also fails to contain a password and
        # would make this check pass while reading nothing.
        audit_response = client.get("/api/users/audit")
        assert audit_response.status_code == 200, audit_response.text
        audit = audit_response.text
        assert "portal.issue" in audit, "the audit log recorded no issuance"
        for password in secrets_issued:
            assert password not in audit, (
                "a generated password was written to the audit log")

        # And not in the database file, in any encoding SQLite might have used.
        state.close_database()
        raw = (directory / "login.db").read_bytes()
        for password in secrets_issued:
            assert password.encode("utf-8") not in raw, (
                "a generated password is in the database file in plaintext")

        print("PASS  a generated password is returned once and appears in no "
              "later response, no audit row and not in the database file")
    finally:
        teardown(client, directory)


def test_a_reset_generates_a_password_and_signs_the_student_out():
    client, directory = make_client()
    try:
        setup_school(client)
        issued = {account["student_id"]: account["password"]
                  for account in
                  client.post("/api/portal/accounts/issue-missing")
                        .json()["issued"]}

        student = settled("STU-0001", issued["STU-0001"])
        assert student.get("/api/portal/me").status_code == 200

        reset = client.post("/api/portal/accounts/STU-0001/password")
        assert reset.status_code == 200, reset.text
        fresh = reset.json()
        assert fresh["password_is_generated"] is True
        assert len(fresh["password"]) == FIRST_PASSWORD_LENGTH

        # The open session is gone: a reset whose reason is "somebody else knew
        # the old one" has to mean signed out everywhere.
        assert student.get("/api/portal/me").status_code == 401
        student.close()

        # The old password no longer works; the new one does, and asks to be
        # replaced again.
        _, refused = sign_in("STU-0001", OWN_PASSWORD)
        assert refused.status_code == 401

        again, response = sign_in("STU-0001", fresh["password"])
        assert response.status_code == 200
        assert again.get("/api/portal/me/attendance").status_code == 403, (
            "a reset password did not re-arm the must-change gate")
        again.close()

        print("PASS  a reset with no password generates one, signs every "
              "session out and requires the student to change it again")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_dashboard_offers_the_bulk_issue_and_shows_each_password_once():
    assert 'id="portal-issue-missing"' in HTML, (
        "the Administration page has no way to issue logins to every student")
    assert 'id="portal-issued"' in HTML, (
        "generated passwords have nowhere to be shown")

    assert "/api/portal/accounts/issue-missing" in JS
    assert "showIssuedLogins" in JS
    assert "clearIssuedLogins" in JS, (
        "nothing clears a list of credentials off the screen")

    # Leaving the tab must clear them, so a class's logins are not left up on
    # a projector for the next lesson.
    switcher = JS[JS.index("function switchView"):]
    switcher = switcher[:switcher.index("\n}")]
    assert "clearIssuedLogins" in switcher, (
        "switching view leaves generated passwords on screen")

    print("PASS  the dashboard can issue a login to every student and clears "
          "the shown passwords when the tab is left")


def test_the_student_door_is_on_the_sign_in_screen():
    assert 'id="auth-as-student"' in HTML, (
        "the sign-in screen offers no student door")
    assert "/api/portal/login" in JS, (
        "the student door never posts to the portal endpoint")

    # And the portal page carries no staff navigation.
    portal = HTML[HTML.index('id="portal-main"'):]
    portal = portal[:portal.index("</main>")]
    for forbidden in ("data-view=", "Live classroom", "Administration"):
        assert forbidden not in portal, (
            "the student portal page carries %r" % forbidden)

    print("PASS  the sign-in screen has a student door and the portal page "
          "carries no staff navigation")


def test_tests_never_touch_the_real_database():
    from database import DEFAULT_DATABASE_PATH

    real = Path(DEFAULT_DATABASE_PATH)
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        setup_school(client)
        client.post("/api/portal/accounts/issue-missing")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_a_generated_password_avoids_the_characters_people_mistype,
        test_a_login_needs_neither_a_username_nor_a_password_to_be_invented,
        test_an_explicit_username_and_password_still_work_unchanged,
        test_every_registered_student_can_be_given_a_login_at_once,
        test_issuing_to_everybody_twice_changes_nothing_the_second_time,
        test_a_student_registered_afterwards_is_picked_up_by_the_next_run,
        test_bulk_issuing_is_super_admin_only,
        test_a_student_sees_their_own_attendance_and_its_percentage,
        test_a_student_sees_no_trace_of_another_student,
        test_no_portal_endpoint_takes_a_student_identifier_at_all,
        test_a_student_cannot_change_any_attendance,
        test_a_student_reaches_no_teacher_page_or_camera,
        test_a_generated_password_appears_once_and_is_never_stored,
        test_a_reset_generates_a_password_and_signs_the_student_out,
        test_the_dashboard_offers_the_bulk_issue_and_shows_each_password_once,
        test_the_student_door_is_on_the_sign_in_screen,
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
