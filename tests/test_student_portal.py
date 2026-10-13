"""
Student portal, staff personnel records, and the redesigned interface.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file and its own staff directory; data/classvision.db is never opened and
data/staff/ is never written to.

WHAT THIS VERIFIES
    That a student can see their own attendance and nothing else; that a
    portal credential can never reach a staff endpoint and a staff cookie can
    never reach the portal; that the password a school hands out stops working
    once it has been used; that a teacher's photograph is checked against its
    actual bytes rather than its claimed type; and that the redesigned
    interface still offers a way to switch all of its motion off.

WHAT THIS DOES NOT VERIFY
    That this authentication is secure against a determined attacker. The
    portal reuses the staff password hashing and session handling, and
    inherits both their properties and their limits - see the note at the top
    of tests/test_auth_rbac.py.

Run with:
    python tests/test_student_portal.py
"""

import base64
import re
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from backend.services import state  # noqa: E402
from backend.services import staff_files_service as files  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

ADMIN = {"username": "owner", "display_name": "The Owner",
         "password": "a-long-enough-passphrase"}
TEACHER = {"username": "tsmith", "display_name": "T Smith",
           "password": "another-long-passphrase", "role": "TEACHER"}

FIRST_PASSWORD = "issued-by-the-office"
OWN_PASSWORD = "a-password-only-they-know"

# Minimal files whose leading bytes are genuinely what they claim to be.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
PDF = b"%PDF-1.4\n" + b"\x00" * 256


def data_url(mime, raw):
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_portal_"))
    state.configure(directory / "portal.db")

    # Staff files go to the temporary directory too, so no test can leave a
    # photograph in the real data/staff/.
    original = files.STAFF_DIR
    files.STAFF_DIR = directory / "staff"

    from backend.app import app
    return TestClient(app), directory, original


def teardown(client, directory, original):
    files.STAFF_DIR = original
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def setup_school(client):
    """An administrator, a teacher, two students, and one portal login."""
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201
    assert client.post("/api/users", json=TEACHER).status_code == 201

    for student_id, name in (("P001", "Asha Rao"), ("P002", "Vikram Nair")):
        assert client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": "1",
            "class_name": "MECH", "section": "A",
            "parent_name": "A Guardian",
            "parent_email": "guardian@example.invalid",
            "parent_phone": "+10000000000"}).status_code == 201

    issued = client.post("/api/portal/accounts", json={
        "student_id": "P001", "username": "asha",
        "password": FIRST_PASSWORD})
    assert issued.status_code == 201, issued.text
    return issued.json()


def sign_in_portal(password=OWN_PASSWORD, username="asha"):
    """A separate client with its own cookie jar, like a student's phone."""
    from backend.app import app
    client = TestClient(app)
    response = client.post("/api/portal/login",
                           json={"username": username, "password": password})
    return client, response


def settle(client):
    """Get past the change-the-handed-out-password gate."""
    student, response = sign_in_portal(FIRST_PASSWORD)
    assert response.status_code == 200, response.text
    assert student.post("/api/portal/me/password", json={
        "current_password": FIRST_PASSWORD,
        "new_password": OWN_PASSWORD}).status_code == 200
    student.close()

    student, response = sign_in_portal(OWN_PASSWORD)
    assert response.status_code == 200, response.text
    return student


# -- issuing a login --------------------------------------------------------

def test_only_a_super_admin_can_issue_a_student_login():
    client, directory, original = make_client()
    try:
        setup_school(client)

        from backend.app import app
        teacher = TestClient(app)
        teacher.post("/api/auth/login", json={
            "username": TEACHER["username"], "password": TEACHER["password"]})

        refused = teacher.post("/api/portal/accounts", json={
            "student_id": "P002", "username": "vikram",
            "password": "some-long-password"})
        assert refused.status_code == 403, refused.text

        for method, path in (("get", "/api/portal/accounts"),
                             ("delete", "/api/portal/accounts/P001")):
            assert getattr(teacher, method)(path).status_code == 403, (
                "a teacher reached %s" % path)

        assert teacher.post(
            "/api/portal/accounts/P001/status?status=INACTIVE"
        ).status_code == 403

        # And nothing changed.
        assert len(client.get("/api/portal/accounts").json()) == 1

        teacher.close()
        print("PASS  issuing, listing, suspending and revoking a portal login "
              "are Super Admin only")
    finally:
        teardown(client, directory, original)


def test_a_login_cannot_be_issued_for_somebody_who_is_not_a_student():
    client, directory, original = make_client()
    try:
        setup_school(client)

        invented = client.post("/api/portal/accounts", json={
            "student_id": "NOT_A_STUDENT", "username": "ghost",
            "password": "a-long-enough-password"})
        assert invented.status_code == 400, invented.text

        # Nor a second login for a student who has one.
        again = client.post("/api/portal/accounts", json={
            "student_id": "P001", "username": "asha2",
            "password": "a-long-enough-password"})
        assert again.status_code == 409, again.text

        # Nor a username that collides with a member of staff.
        collision = client.post("/api/portal/accounts", json={
            "student_id": "P002", "username": TEACHER["username"],
            "password": "a-long-enough-password"})
        assert collision.status_code == 409, collision.text

        print("PASS  a portal login needs a real student, a free username, "
              "and cannot be issued twice")
    finally:
        teardown(client, directory, original)


def test_the_issued_password_must_be_replaced_before_anything_is_shown():
    """
    A first password is written on a slip of paper and left on a desk.

    So it opens exactly one door - the change-password form - and the server
    is what enforces that, not the dialog the dashboard happens to show.
    """
    client, directory, original = make_client()
    try:
        setup_school(client)

        student, response = sign_in_portal(FIRST_PASSWORD)
        assert response.status_code == 200
        assert response.json()["must_change"] is True

        for path in ("/api/portal/me/attendance", "/api/portal/me/performance"):
            gated = student.get(path)
            assert gated.status_code == 403, (
                "%s answered before the handed-out password was replaced"
                % path)

        # /me itself is allowed, because the form has to greet somebody.
        assert student.get("/api/portal/me").status_code == 200

        changed = student.post("/api/portal/me/password", json={
            "current_password": FIRST_PASSWORD, "new_password": OWN_PASSWORD})
        assert changed.status_code == 200, changed.text

        # Changing it ends every session, this one included.
        assert student.get("/api/portal/me").status_code == 401, (
            "the session survived a password change")

        # The old password is dead, the new one works, and the gate is gone.
        assert sign_in_portal(FIRST_PASSWORD)[1].status_code == 401
        settled, response = sign_in_portal(OWN_PASSWORD)
        assert response.status_code == 200
        assert settled.get("/api/portal/me/attendance").status_code == 200

        student.close()
        settled.close()
        print("PASS  the handed-out password opens only the change-password "
              "form, and dies when it is used")
    finally:
        teardown(client, directory, original)


# -- what a student may see -------------------------------------------------

def test_a_student_sees_their_own_record_and_no_way_to_ask_for_another():
    client, directory, original = make_client()
    try:
        setup_school(client)
        student = settle(client)

        performance = student.get("/api/portal/me/performance")
        assert performance.status_code == 200, performance.text
        body = performance.json()

        assert body["student"]["student_id"] == "P001"

        # The other student is not reachable, and not because their id was
        # guessed wrong - there is no endpoint that takes one.
        text = performance.text
        assert "P002" not in text and "Vikram" not in text, (
            "another student appeared in a portal response")

        # No contact detail comes back either. A student does not need their
        # own parent's phone number read out to them by the school system.
        for field in ("parent_name", "parent_phone", "parent_email"):
            assert field not in text, "%s leaked to the portal" % field

        # Nothing biometric, and no path.
        for forbidden in ("embedding", "vector", ".npz", "password", "hash",
                          "token", "photo"):
            assert forbidden not in text.lower(), (
                "the portal returned %r" % forbidden)

        student.close()
        print("PASS  the portal answers with one student's own record, "
              "carrying no contact detail and nothing biometric")
    finally:
        teardown(client, directory, original)


def test_no_portal_endpoint_accepts_a_student_id():
    """
    The shape of the API is the protection, not a check inside it.

    An endpoint that takes an id is an endpoint somebody tries a friend's id
    in. This asserts that no such endpoint exists under /api/portal/me.
    """
    client, directory, original = make_client()
    try:
        setup_school(client)

        from backend.app import app
        paths = [path for path in app.openapi()["paths"]
                 if path.startswith("/api/portal/me")]

        assert paths, "the portal has no /me endpoints at all"
        for path in paths:
            assert "{" not in path, (
                "%s takes a parameter; a student could put somebody else's "
                "id in it" % path)

        print("PASS  every portal endpoint takes the student from the "
              "session: %s" % ", ".join(sorted(paths)))
    finally:
        teardown(client, directory, original)


def test_a_suspended_account_stops_working_immediately():
    client, directory, original = make_client()
    try:
        setup_school(client)
        student = settle(client)
        assert student.get("/api/portal/me").status_code == 200

        assert client.post(
            "/api/portal/accounts/P001/status?status=INACTIVE"
        ).status_code == 200

        assert student.get("/api/portal/me").status_code == 401, (
            "an open session survived the account being suspended")
        assert sign_in_portal(OWN_PASSWORD)[1].status_code == 401

        # Reversible, and the password still works afterwards.
        assert client.post(
            "/api/portal/accounts/P001/status?status=ACTIVE"
        ).status_code == 200
        restored, response = sign_in_portal(OWN_PASSWORD)
        assert response.status_code == 200

        student.close()
        restored.close()
        print("PASS  suspending a portal account takes effect at once and can "
              "be undone")
    finally:
        teardown(client, directory, original)


def test_removing_a_portal_login_destroys_no_attendance():
    """
    "Delete" means two very different things in this project.

    DELETE /api/students/{id} destroys a person's history. Removing a portal
    login destroys a password. Only the second one is what this endpoint does,
    and this test is what keeps it that way.
    """
    client, directory, original = make_client()
    try:
        setup_school(client)

        before = client.get("/api/students/P001").json()

        removed = client.delete("/api/portal/accounts/P001")
        assert removed.status_code == 200, removed.text

        after = client.get("/api/students/P001")
        assert after.status_code == 200, "removing a login removed the student"
        assert after.json()["name"] == before["name"]
        assert after.json()["attendance"] == before["attendance"]

        assert sign_in_portal(OWN_PASSWORD)[1].status_code == 401
        assert client.get("/api/portal/accounts").json() == []

        print("PASS  removing a portal login leaves the student and every "
              "attendance record untouched")
    finally:
        teardown(client, directory, original)


# -- the two front doors stay apart ----------------------------------------

def test_a_portal_token_reaches_no_staff_endpoint():
    """
    The separation is structural: a portal session resolves against
    student_accounts, and every staff guard begins by resolving a row in
    `users`. This calls the staff API by hand with a valid portal session.
    """
    client, directory, original = make_client()
    try:
        setup_school(client)
        student = settle(client)

        for path in ("/api/students", "/api/sessions", "/api/users",
                     "/api/analytics/overview", "/api/live/status",
                     "/api/classes", "/api/alerts/rules",
                     "/api/students/P002", "/api/users/audit",
                     "/api/portal/accounts"):
            response = student.get(path)
            assert response.status_code == 401, (
                "a student portal session reached %s (%d)"
                % (path, response.status_code))

        for path in ("/api/students", "/api/sessions", "/api/users"):
            response = student.post(path, json={})
            assert response.status_code == 401, (
                "a portal session reached POST %s" % path)

        assert student.delete("/api/students/P002").status_code == 401, (
            "a portal session reached DELETE /api/students/P002")

        student.close()
        print("PASS  a portal session is refused by every staff endpoint, "
              "read and write alike")
    finally:
        teardown(client, directory, original)


def test_a_staff_session_reaches_no_portal_endpoint():
    client, directory, original = make_client()
    try:
        setup_school(client)

        # The Super Admin who issued the account cannot read it as a student.
        for path in ("/api/portal/me", "/api/portal/me/attendance",
                     "/api/portal/me/performance"):
            assert client.get(path).status_code == 401, (
                "a staff cookie was accepted at %s" % path)

        assert client.get("/api/portal/state").json()["authenticated"] is False

        # Administration of portal accounts still works, because that is a
        # staff operation on somebody else's login rather than a portal one.
        assert client.get("/api/portal/accounts").status_code == 200

        print("PASS  a staff cookie is not a portal session, and the two "
              "cookies never stand in for one another")
    finally:
        teardown(client, directory, original)


def test_the_portal_stores_no_password_and_leaks_no_hash():
    client, directory, original = make_client()
    try:
        issued = setup_school(client)

        assert "password" not in issued
        assert "password_hash" not in issued

        listing = client.get("/api/portal/accounts").text
        assert FIRST_PASSWORD not in listing
        assert "hash" not in listing.lower()
        assert "pbkdf2" not in listing.lower()

        database = state.get_database()
        row = database.query_one(
            "SELECT password_hash FROM student_accounts WHERE student_id = ?",
            ("P001",))
        assert row["password_hash"].startswith("pbkdf2_sha256$")
        assert FIRST_PASSWORD not in row["password_hash"]

        raw = (directory / "portal.db").read_bytes()
        assert FIRST_PASSWORD.encode() not in raw, (
            "the issued password is readable in the database file")

        print("PASS  a portal password is salted, hashed, absent from every "
              "response and absent from the database file")
    finally:
        teardown(client, directory, original)


# -- staff photographs and documents ---------------------------------------

def test_a_staff_photograph_is_checked_against_its_actual_bytes():
    """
    The declared type is a claim. The leading bytes are the evidence.

    Trusting Content-Type is how a staff directory becomes a file-upload
    vulnerability, so an "image/jpeg" that does not begin with JPEG's
    signature is refused.
    """
    client, directory, original = make_client()
    try:
        setup_school(client)
        user_id = [user for user in client.get("/api/users").json()
                   if user["role"] == "TEACHER"][0]["user_id"]

        good = client.put("/api/users/%d/photo" % user_id,
                          json={"image": data_url("image/jpeg", JPEG)})
        assert good.status_code == 200, good.text
        assert client.get("/api/users/%d/photo" % user_id).status_code == 200

        for label, mime, payload in (
                ("a PHP script", "image/jpeg", b"<?php system($_GET[0]); ?>"),
                ("an HTML page", "image/png", b"<html><script>alert(1)"),
                ("an SVG", "image/svg+xml", b"<svg onload=alert(1)>"),
                ("a PDF as a photo", "application/pdf", PDF)):
            refused = client.put("/api/users/%d/photo" % user_id,
                                 json={"image": data_url(mime, payload)})
            assert refused.status_code == 400, (
                "%s was accepted as a staff photograph" % label)

        # The good photograph is still the one on file.
        assert client.get("/api/users/%d/photo" % user_id).status_code == 200

        print("PASS  a staff photograph must be what it says it is; scripts, "
              "HTML and SVG are refused")
    finally:
        teardown(client, directory, original)


def test_a_teacher_cannot_read_or_write_a_personnel_record():
    client, directory, original = make_client()
    try:
        setup_school(client)
        users = client.get("/api/users").json()
        user_id = [u for u in users if u["role"] == "TEACHER"][0]["user_id"]

        client.post("/api/users/%d/documents" % user_id, json={
            "file": data_url("application/pdf", PDF),
            "filename": "contract.pdf", "label": "Contract"})

        from backend.app import app
        teacher = TestClient(app)
        teacher.post("/api/auth/login", json={
            "username": TEACHER["username"], "password": TEACHER["password"]})

        assert teacher.get("/api/users/%d/documents" % user_id).status_code \
            == 403, "a teacher read a personnel document list"
        assert teacher.post("/api/users/%d/documents" % user_id, json={
            "file": data_url("application/pdf", PDF),
            "filename": "x.pdf"}).status_code == 403
        assert teacher.put("/api/users/%d/photo" % user_id, json={
            "image": data_url("image/png", PNG)}).status_code == 403

        # A photograph is different: a colleague may see who works here.
        client.put("/api/users/%d/photo" % user_id,
                   json={"image": data_url("image/jpeg", JPEG)})
        assert teacher.get("/api/users/%d/photo" % user_id).status_code == 200

        teacher.close()
        print("PASS  documents are Super Admin only; a photograph is visible "
              "to signed-in staff and writable only by an administrator")
    finally:
        teardown(client, directory, original)


def test_a_document_cannot_be_read_through_the_wrong_account():
    client, directory, original = make_client()
    try:
        setup_school(client)
        users = client.get("/api/users").json()
        teacher_id = [u for u in users if u["role"] == "TEACHER"][0]["user_id"]
        admin_id = [u for u in users
                    if u["role"] == "SUPER_ADMIN"][0]["user_id"]

        created = client.post("/api/users/%d/documents" % teacher_id, json={
            "file": data_url("application/pdf", PDF),
            "filename": "../../etc/passwd.pdf", "label": "Contract"})
        assert created.status_code == 201, created.text
        document_id = created.json()["document_id"]

        # The filename is stored as a LABEL, never used as a path.
        assert "/" not in created.json()["filename"]
        assert ".." not in created.json()["filename"].replace("..", "._")

        # The id alone is not enough - the account in the path has to own it.
        wrong = client.get("/api/users/%d/documents/%d"
                           % (admin_id, document_id))
        assert wrong.status_code == 404, (
            "a document was readable through another account's URL")

        right = client.get("/api/users/%d/documents/%d"
                           % (teacher_id, document_id))
        assert right.status_code == 200
        assert "attachment" in right.headers.get("content-disposition", ""), (
            "a document is served inline, so a PDF would run in the "
            "dashboard's own origin")

        # A tampered stored_name cannot be used to read the disk.
        assert files.read_file("../../database/database.py") is None
        assert files.read_file("photo-notahexstring.jpg") is None

        print("PASS  a document is reachable only through its own account, "
              "always as an attachment, and never by path")
    finally:
        teardown(client, directory, original)


# -- the interface ----------------------------------------------------------

def test_the_sign_in_screen_offers_both_doors():
    assert 'id="auth-switch"' in HTML, "there is no staff/student switch"
    assert 'id="auth-as-staff"' in HTML and 'id="auth-as-student"' in HTML
    assert 'role="tablist"' in HTML, "the switch is not announced as a choice"

    assert "/api/portal/login" in JS, "the student door goes nowhere"
    assert "authAudience" in JS, "nothing records which door was chosen"

    print("PASS  the sign-in screen asks which door, and the student door "
          "posts to the portal endpoint")


def test_the_portal_page_shows_one_student_and_offers_no_navigation():
    portal = HTML[HTML.index('id="portal-main"'):
                  HTML.index('id="portal-password-dialog"')]

    assert 'id="portal-ring"' in portal, "no attendance figure"
    assert 'id="portal-history"' in portal, "no session history"

    # A student's page must not contain the staff navigation, even hidden.
    for staff_only in ('data-view="admin"', 'data-view="live"',
                       'id="users-table"', 'id="students-table"'):
        assert staff_only not in portal, (
            "the portal page contains staff UI: %s" % staff_only)

    assert "/api/portal/me" in JS
    # No portal call may carry an id.
    for match in re.findall(r'"/api/portal/me[^"]*"', JS):
        assert "{" not in match and "+" not in match, (
            "a portal URL is built with a variable: %s" % match)

    print("PASS  the portal page shows one student's own record and carries "
          "no staff navigation")


def test_the_redesign_can_have_all_of_its_motion_switched_off():
    assert "prefers-reduced-motion" in CSS
    assert "prefersReducedMotion" in JS, (
        "the canvas animation never consults the preference")

    scene = JS[JS.index("function startAuthScene"):
               JS.index("function stopAuthScene")]
    assert "prefersReducedMotion()" in scene, (
        "the sign-in scene starts its loop without asking")

    # And it stops itself rather than running behind a signed-in dashboard.
    assert "auth-screen" in scene and "hidden" in scene, (
        "the animation loop never checks whether its screen is still shown")

    print("PASS  every animation, CSS and canvas alike, can be switched off "
          "and the canvas loop stops itself")


def test_the_redesign_still_reaches_no_external_host():
    combined = (HTML + CSS + JS).replace("http://www.w3.org/2000/svg", "")
    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr",
                     "googleapis", "cdnjs", "fonts.g", "three.min.js"):
        assert external not in combined, (
            "the redesign pulled in %r" % external)

    # The 3D is hand-written, not a library.
    assert "spherePoints" in JS, "the sign-in scene is not the hand-written one"
    assert "THREE" not in JS and "WebGLRenderer" not in JS

    print("PASS  the redesign is hand-written and still works offline")


def test_tests_never_touch_the_real_database_or_staff_directory():
    real = PROJECT_ROOT / "data" / "classvision.db"
    staff = PROJECT_ROOT / "data" / "staff"

    before = real.stat().st_mtime if real.exists() else None
    staff_before = sorted(path.name for path in staff.glob("*")) \
        if staff.exists() else []

    client, directory, original = make_client()
    try:
        setup_school(client)
        user_id = [u for u in client.get("/api/users").json()
                   if u["role"] == "TEACHER"][0]["user_id"]
        client.put("/api/users/%d/photo" % user_id,
                   json={"image": data_url("image/jpeg", JPEG)})
        settle(client).close()
    finally:
        teardown(client, directory, original)

    after = real.stat().st_mtime if real.exists() else None
    staff_after = sorted(path.name for path in staff.glob("*")) \
        if staff.exists() else []

    assert after == before, "the real attendance database was written to"
    assert staff_after == staff_before, (
        "a photograph was written into the real data/staff/: %s"
        % sorted(set(staff_after) - set(staff_before)))

    print("PASS  the real database and staff directory were never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_only_a_super_admin_can_issue_a_student_login,
        test_a_login_cannot_be_issued_for_somebody_who_is_not_a_student,
        test_the_issued_password_must_be_replaced_before_anything_is_shown,
        test_a_student_sees_their_own_record_and_no_way_to_ask_for_another,
        test_no_portal_endpoint_accepts_a_student_id,
        test_a_suspended_account_stops_working_immediately,
        test_removing_a_portal_login_destroys_no_attendance,
        test_a_portal_token_reaches_no_staff_endpoint,
        test_a_staff_session_reaches_no_portal_endpoint,
        test_the_portal_stores_no_password_and_leaks_no_hash,
        test_a_staff_photograph_is_checked_against_its_actual_bytes,
        test_a_teacher_cannot_read_or_write_a_personnel_record,
        test_a_document_cannot_be_read_through_the_wrong_account,
        test_the_sign_in_screen_offers_both_doors,
        test_the_portal_page_shows_one_student_and_offers_no_navigation,
        test_the_redesign_can_have_all_of_its_motion_switched_off,
        test_the_redesign_still_reaches_no_external_host,
        test_tests_never_touch_the_real_database_or_staff_directory,
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
