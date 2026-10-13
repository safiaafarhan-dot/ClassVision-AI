"""
Authentication and role-based access control tests.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That passwords are hashed and never stored, logged or returned; that a
    signed-out caller gets nothing; that a teacher cannot reach administration
    by calling the endpoint directly; that a teacher's class assignments are
    applied on the SERVER; and that the last Super Admin cannot be locked out
    of their own system.

WHAT THIS DOES NOT VERIFY
    That this authentication is secure against a determined attacker. It has
    not been penetration-tested or independently reviewed, there is no rate
    limiting beyond a per-attempt delay, and no test here claims otherwise.

Run with:
    python tests/test_auth_rbac.py
"""

import shutil
import re
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
from backend.services.auth_service import (  # noqa: E402
    SESSION_COOKIE,
    AuthFailed,
    AuthService,
    hash_password,
    verify_password,
)
from paths import enable_safe_console  # noqa: E402

ADMIN = {"username": "owner", "display_name": "The Owner",
         "password": "a-long-enough-passphrase"}

TEACHER = {"username": "tsmith", "display_name": "T Smith",
           "password": "another-long-passphrase", "role": "TEACHER"}


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_auth_"))
    state.configure(directory / "auth.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def setup_admin(client):
    response = client.post("/api/auth/setup", json=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def add_teacher(client, assignments=None, **overrides):
    payload = dict(TEACHER)
    payload["assignments"] = assignments or []
    payload.update(overrides)
    return client.post("/api/users", json=payload)


def sign_in(directory, credentials):
    """A SECOND client, with its own cookie jar, against the same database."""
    from backend.app import app
    client = TestClient(app)
    response = client.post("/api/auth/login", json={
        "username": credentials["username"],
        "password": credentials["password"]})
    assert response.status_code == 200, response.text
    return client


# -- password hashing -------------------------------------------------------

def test_a_password_is_hashed_with_a_salt_and_never_stored():
    first = hash_password("the same password")
    second = hash_password("the same password")

    assert first != second, (
        "two hashes of one password are identical - there is no salt")
    assert "the same password" not in first
    assert first.startswith("pbkdf2_sha256$"), first.split("$")[0]

    assert verify_password("the same password", first)
    assert verify_password("the same password", second)
    assert not verify_password("the wrong password", first)

    # A corrupt stored value must fail the sign-in, not crash it.
    for broken in ("", "nonsense", "pbkdf2_sha256$x$y$z", None):
        assert verify_password("anything", broken) is False

    print("PASS  passwords are salted, hashed, verifiable, and a corrupt hash "
          "fails safely")


def test_the_database_holds_no_plaintext_password():
    client, directory = make_client()
    try:
        setup_admin(client)
        add_teacher(client)

        rows = state.get_database().query("SELECT * FROM users")
        assert len(rows) == 2

        for row in rows:
            record = dict(row)
            assert record["password_hash"].startswith("pbkdf2_sha256$")
            for value in record.values():
                assert ADMIN["password"] not in str(value)
                assert TEACHER["password"] not in str(value)

        # And not in the audit trail either.
        audit = state.get_database().query("SELECT * FROM audit_log")
        for row in audit:
            for value in dict(row).values():
                assert ADMIN["password"] not in str(value)
                assert TEACHER["password"] not in str(value)

        print("PASS  no plaintext password reaches the users table or the "
              "audit log")
    finally:
        teardown(client, directory)


def test_a_weak_password_is_refused():
    client, directory = make_client()
    try:
        for weak in ("short", "password", "12345"):
            response = client.post("/api/auth/setup", json={
                "username": "owner", "display_name": "Owner",
                "password": weak})
            assert response.status_code in (400, 422), (
                "%r was accepted (%d)" % (weak, response.status_code))

        assert client.get("/api/auth/state").json()["configured"] is False
        print("PASS  a short or obvious password is refused, and no account "
              "is created")
    finally:
        teardown(client, directory)


# -- first run --------------------------------------------------------------

def test_an_empty_installation_asks_for_the_first_administrator():
    client, directory = make_client()
    try:
        auth = client.get("/api/auth/state").json()
        assert auth["configured"] is False
        assert auth["authenticated"] is False

        print("PASS  an installation with no accounts reports itself "
              "unconfigured")
    finally:
        teardown(client, directory)


def test_the_setup_endpoint_works_once_and_then_never_again():
    client, directory = make_client()
    try:
        first = setup_admin(client)
        assert first["role"] == "SUPER_ADMIN"
        assert "password" not in first
        assert "password_hash" not in first

        second = client.post("/api/auth/setup", json={
            "username": "sneaky", "display_name": "Second Admin",
            "password": "yet-another-long-pass"})
        assert second.status_code == 409, second.text

        users = client.get("/api/users").json()
        assert len(users) == 1, "a second administrator was created"

        print("PASS  the first-run endpoint creates exactly one administrator "
              "and then refuses")
    finally:
        teardown(client, directory)


def test_there_is_no_default_account_anywhere():
    """
    A shipped default password would make every installation identical.

    The only way an account comes into being is somebody creating one.
    """
    client, directory = make_client()
    try:
        assert state.get_database().query_one(
            "SELECT COUNT(*) AS total FROM users")["total"] == 0

        for guess in ("admin", "administrator", "classvision", "root"):
            response = client.post("/api/auth/login", json={
                "username": guess, "password": "admin"})
            assert response.status_code == 401, (
                "%r signed in on a fresh installation" % guess)

        print("PASS  a fresh installation has no accounts and no default "
              "credentials")
    finally:
        teardown(client, directory)


# -- sign in ----------------------------------------------------------------

def test_signing_in_and_out():
    client, directory = make_client()
    try:
        setup_admin(client)
        client.post("/api/auth/logout")

        assert client.get("/api/students").status_code == 401

        good = client.post("/api/auth/login", json={
            "username": ADMIN["username"], "password": ADMIN["password"]})
        assert good.status_code == 200, good.text
        assert SESSION_COOKIE in client.cookies

        assert client.get("/api/students").status_code == 200

        client.post("/api/auth/logout")
        assert client.get("/api/students").status_code == 401

        print("PASS  sign in, use the API, sign out, and the API closes again")
    finally:
        teardown(client, directory)


def test_a_wrong_password_and_an_unknown_user_are_refused_alike():
    client, directory = make_client()
    try:
        setup_admin(client)
        client.post("/api/auth/logout")

        wrong = client.post("/api/auth/login", json={
            "username": ADMIN["username"], "password": "not-the-password"})
        unknown = client.post("/api/auth/login", json={
            "username": "nobody-at-all", "password": "not-the-password"})

        assert wrong.status_code == 401 and unknown.status_code == 401
        assert wrong.json()["detail"] == unknown.json()["detail"], (
            "the refusal says which half was wrong, which enumerates accounts")

        print("PASS  a wrong password and an unknown username are refused "
              "identically")
    finally:
        teardown(client, directory)


def test_the_session_cookie_is_httponly_and_carries_no_identity():
    client, directory = make_client()
    try:
        response = client.post("/api/auth/setup", json=ADMIN)
        header = response.headers.get("set-cookie", "")

        assert "httponly" in header.lower(), (
            "the session cookie is readable by script: %s" % header)
        assert "samesite=lax" in header.lower().replace(" ", "")

        token = client.cookies.get(SESSION_COOKIE)
        assert ADMIN["username"] not in token
        assert ADMIN["password"] not in token

        # Only the hash is stored, so the token cannot be read back out.
        stored = state.get_database().query_one(
            "SELECT token_hash FROM user_sessions")["token_hash"]
        assert stored != token, "the raw session token is stored in the database"

        print("PASS  the session cookie is HttpOnly, SameSite, opaque, and "
              "stored only as a hash")
    finally:
        teardown(client, directory)


def test_a_deactivated_account_stops_working_immediately():
    client, directory = make_client()
    try:
        setup_admin(client)
        created = add_teacher(client).json()

        teacher = sign_in(directory, TEACHER)
        assert teacher.get("/api/students").status_code == 200

        client.patch("/api/users/%d" % created["user_id"],
                     json={"status": "INACTIVE"})

        assert teacher.get("/api/students").status_code == 401, (
            "a deactivated teacher's open session still works")

        again = client.post("/api/auth/login", json={
            "username": TEACHER["username"], "password": TEACHER["password"]})
        assert again.status_code == 401

        teacher.close()
        print("PASS  deactivating an account ends its open sessions at once")
    finally:
        teardown(client, directory)


# -- authorization ----------------------------------------------------------

def test_every_data_endpoint_refuses_an_unauthenticated_caller():
    client, directory = make_client()
    try:
        setup_admin(client)
        client.post("/api/auth/logout")

        for url in ("/api/students", "/api/sessions", "/api/classes",
                    "/api/analytics/overview", "/api/live/status",
                    "/api/users", "/api/students/enrollment/status"):
            assert client.get(url).status_code == 401, (
                "%s answered a signed-out caller" % url)

        for url, method in (("/api/students", "post"),
                            ("/api/sessions", "post"),
                            ("/api/live/start", "post"),
                            ("/api/students/enrollment/start", "post")):
            response = getattr(client, method)(url, json={})
            assert response.status_code == 401, (
                "%s %s answered a signed-out caller (%d)"
                % (method.upper(), url, response.status_code))

        # Health and the auth state stay open, because the dashboard has to
        # ask something before it can know to show a login screen.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/auth/state").status_code == 200

        print("PASS  every data endpoint refuses a signed-out caller; health "
              "and auth state stay open")
    finally:
        teardown(client, directory)


def test_a_teacher_cannot_reach_administration_by_calling_it_directly():
    """
    The permission is the server's, not the navigation's.

    Every administration endpoint is called by hand here, with a valid
    teacher session - exactly what hiding the tab would not prevent.
    """
    client, directory = make_client()
    try:
        setup_admin(client)
        created = add_teacher(client).json()

        teacher = sign_in(directory, TEACHER)

        assert teacher.get("/api/users").status_code == 403
        assert teacher.get("/api/users/audit").status_code == 403
        assert teacher.get("/api/users/%d" % created["user_id"]).status_code \
            == 403

        promotion = teacher.patch("/api/users/%d" % created["user_id"],
                                  json={"role": "SUPER_ADMIN"})
        assert promotion.status_code == 403, (
            "a teacher promoted themselves to Super Admin")

        assert teacher.post("/api/users", json={
            "username": "invented", "display_name": "Invented",
            "password": "a-long-enough-passphrase", "role": "SUPER_ADMIN"
        }).status_code == 403

        assert teacher.delete("/api/users/1").status_code == 403

        # And the attempt changed nothing.
        assert client.get("/api/users/%d" % created["user_id"]).json()["role"] \
            == "TEACHER"

        teacher.close()
        print("PASS  a teacher calling administration endpoints by hand gets "
              "403 and changes nothing")
    finally:
        teardown(client, directory)


def test_only_a_super_admin_can_permanently_delete_a_student():
    """
    The one student operation that destroys data, narrowed to one role.

    Every table pointing at students(student_id) cascades - attendance,
    presence segments, expected-student rows, academic history, notifications
    - and the face samples go too. DELETE /api/sessions/{id} has always been
    Super Admin only for that reason; a student deletion cascades wider, so
    leaving it open to any signed-in account was an asymmetry.

    Called by hand with a valid teacher session, which is exactly what hiding
    the button would not prevent.
    """
    client, directory = make_client()
    try:
        setup_admin(client)
        add_teacher(client)

        for student_id, name in (("DEL_BY_ADMIN", "Deleted By Admin"),
                                 ("DEL_BY_TEACHER", "Survives A Teacher")):
            assert client.post("/api/students", json={
                "student_id": student_id, "name": name, "roll_number": "1",
                "class_name": "MECH", "section": "A"}).status_code == 201

        teacher = sign_in(directory, TEACHER)

        # A teacher with no assignment is unrestricted WITHIN their role, and
        # this is outside it. Being unrestricted is not being an administrator.
        refused = teacher.delete("/api/students/DEL_BY_TEACHER")
        assert refused.status_code == 403, (
            "a teacher permanently deleted a student (%d)"
            % refused.status_code)
        assert "Super Admin" in refused.json()["detail"], refused.text

        # Typing the id does not buy the permission either.
        assert teacher.delete(
            "/api/students/DEL_BY_TEACHER?confirm=DEL_BY_TEACHER")             .status_code == 403, "the typed confirmation bypassed the role"

        # And nothing was destroyed by the attempt.
        assert client.get("/api/students/DEL_BY_TEACHER").status_code == 200

        # A Super Admin still can.
        allowed = client.delete("/api/students/DEL_BY_ADMIN")
        assert allowed.status_code == 200, allowed.text
        assert client.get("/api/students/DEL_BY_ADMIN").status_code == 404

        # A student who does not exist answers the same 404 it always did -
        # for a Super Admin, who is the only caller that gets far enough to
        # find out.
        assert client.delete("/api/students/NEVER_EXISTED").status_code == 404
        assert teacher.delete("/api/students/NEVER_EXISTED").status_code == 403, (
            "the role guard ran after the lookup, so a 403/404 difference "
            "tells a teacher which student ids exist")

        teacher.close()
        print("PASS  permanent student deletion is Super Admin only, and a "
              "teacher's attempt destroys nothing")
    finally:
        teardown(client, directory)


def test_a_signed_out_caller_cannot_delete_a_student():
    """401 before 403: not signed in is a different answer from not allowed."""
    client, directory = make_client()
    try:
        setup_admin(client)
        assert client.post("/api/students", json={
            "student_id": "DEL_ANON", "name": "Still Here", "roll_number": "1",
            "class_name": "MECH", "section": "A"}).status_code == 201

        client.post("/api/auth/logout")

        response = client.delete("/api/students/DEL_ANON")
        assert response.status_code == 401, (
            "a signed-out caller reached student deletion (%d)"
            % response.status_code)

        assert client.delete("/api/students/DEL_ANON?confirm=DEL_ANON")                      .status_code == 401

        # Sign back in and the student is untouched.
        admin = sign_in(directory, ADMIN)
        assert admin.get("/api/students/DEL_ANON").status_code == 200
        admin.close()

        print("PASS  a signed-out caller gets 401 from student deletion, and "
              "the student survives")
    finally:
        teardown(client, directory)


def test_a_teacher_can_still_deactivate_a_student():
    """
    Narrowing deletion must not cost a class teacher the safe alternative.

    Deactivate is the operation they actually need when somebody leaves: it
    keeps every attendance record and it is reversible.
    """
    client, directory = make_client()
    try:
        setup_admin(client)
        add_teacher(client, assignments=[{"class_name": "MECH",
                                          "section": "A"}])

        assert client.post("/api/students", json={
            "student_id": "DEACT_OK", "name": "Left The School",
            "roll_number": "1", "class_name": "MECH",
            "section": "A"}).status_code == 201

        teacher = sign_in(directory, TEACHER)

        retired = teacher.post("/api/students/DEACT_OK/status?status=INACTIVE")
        assert retired.status_code == 200, retired.text
        assert retired.json()["status"] == "INACTIVE"

        # Reversible, and the registry row is still theirs to edit.
        assert teacher.post("/api/students/DEACT_OK/status?status=ACTIVE")                       .json()["status"] == "ACTIVE"
        assert teacher.patch("/api/students/DEACT_OK",
                             json={"roll_number": "2"}).status_code == 200
        assert teacher.post("/api/students", json={
            "student_id": "DEACT_NEW", "name": "Newly Registered",
            "roll_number": "3", "class_name": "MECH",
            "section": "A"}).status_code == 201

        teacher.close()
        print("PASS  a teacher can still register, edit, deactivate and "
              "reactivate a student")
    finally:
        teardown(client, directory)


def test_a_teacher_keeps_the_classroom_work_they_need():
    client, directory = make_client()
    try:
        setup_admin(client)
        add_teacher(client)

        teacher = sign_in(directory, TEACHER)

        assert teacher.get("/api/students").status_code == 200
        assert teacher.get("/api/sessions").status_code == 200
        assert teacher.get("/api/analytics/overview").status_code == 200
        assert teacher.get("/api/live/status").status_code == 200
        assert teacher.get("/api/students/enrollment/status").status_code == 200

        created = teacher.post("/api/students", json={
            "student_id": "AUTH_TEST_1", "name": "Registered By Teacher",
            "roll_number": "1", "class_name": "MECH", "section": "A"})
        assert created.status_code == 201, created.text

        teacher.close()
        print("PASS  a teacher can register students, run sessions and see "
              "analytics")
    finally:
        teardown(client, directory)


# -- class scope ------------------------------------------------------------

def register_across_classes(client):
    for student_id, name, class_name, section in [
            ("SCOPE_A1", "Alpha One", "MECH", "A"),
            ("SCOPE_A2", "Alpha Two", "MECH", "B"),
            ("SCOPE_B1", "Beta One", "CIVIL", "A")]:
        assert client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": "1",
            "class_name": class_name, "section": section
        }).status_code == 201


def test_an_assigned_teacher_sees_only_their_own_classes():
    client, directory = make_client()
    try:
        setup_admin(client)
        register_across_classes(client)
        add_teacher(client, assignments=[{"class_name": "MECH",
                                          "section": "A"}])

        teacher = sign_in(directory, TEACHER)

        listing = teacher.get("/api/students").json()
        assert listing["total"] == 1, (
            "an assigned teacher saw %d students" % listing["total"])
        assert listing["items"][0]["student_id"] == "SCOPE_A1"

        # A student outside the scope is a 404, not a 403: a status code that
        # changed would tell a teacher which ids exist in a class they may
        # not see.
        assert teacher.get("/api/students/SCOPE_A2").status_code == 404
        assert teacher.get("/api/students/SCOPE_B1").status_code == 404
        assert teacher.get("/api/students/SCOPE_A1").status_code == 200

        assert teacher.patch("/api/students/SCOPE_B1",
                             json={"name": "Renamed"}).status_code == 404

        # Permanent deletion is Super Admin only, and that guard runs BEFORE
        # the row is read - so a teacher gets 403 for a student in another
        # class and 403 for one of their own. The scope 404 above is what
        # hides existence; this 403 hides nothing, because it is identical
        # either way.
        assert teacher.delete("/api/students/SCOPE_B1").status_code == 403
        assert teacher.delete("/api/students/SCOPE_A1").status_code == 403

        # The admin still sees everything, and nothing was changed.
        assert client.get("/api/students").json()["total"] == 3
        assert client.get("/api/students/SCOPE_B1").json()["name"] == "Beta One"

        teacher.close()
        print("PASS  an assigned teacher sees one class and cannot read or "
              "edit outside it")
    finally:
        teardown(client, directory)


def test_a_class_assignment_without_a_section_covers_every_section():
    client, directory = make_client()
    try:
        setup_admin(client)
        register_across_classes(client)
        add_teacher(client, assignments=[{"class_name": "MECH",
                                          "section": ""}])

        teacher = sign_in(directory, TEACHER)
        listing = teacher.get("/api/students").json()

        assert listing["total"] == 2, (
            "a section-less assignment saw %d students" % listing["total"])
        assert sorted(item["student_id"] for item in listing["items"]) == \
            ["SCOPE_A1", "SCOPE_A2"]

        teacher.close()
        print("PASS  an assignment with no section covers every section of "
              "that class")
    finally:
        teardown(client, directory)


def test_a_teacher_with_no_assignment_is_unrestricted_within_their_role():
    client, directory = make_client()
    try:
        setup_admin(client)
        register_across_classes(client)
        add_teacher(client, assignments=[])

        teacher = sign_in(directory, TEACHER)

        assert teacher.get("/api/students").json()["total"] == 3, (
            "a teacher with no timetable was shown an empty dashboard")
        assert teacher.get("/api/users").status_code == 403, (
            "unrestricted was read as administrative")

        teacher.close()
        print("PASS  a teacher with no assignment sees every class, and still "
              "no administration")
    finally:
        teardown(client, directory)


def test_class_names_are_matched_however_they_were_typed():
    """
    Sessions carry free-text class_id; students carry class and section.

    Both spellings already exist in the data, so both are normalised before
    comparison - in one place, so it can be replaced by a real class table
    later without touching a route.
    """
    from backend.services.authz import Principal

    principal = Principal(
        user={"role": "TEACHER",
              "assignments": [{"class_name": "CSE(AI)", "section": "C"}]},
        configured=True)

    assert principal.may_see_student({"class_name": "CSE(AI)", "section": "C"})
    assert principal.may_see_student({"class_name": "cse(ai)", "section": "c"})
    assert not principal.may_see_student({"class_name": "CSE(AI)",
                                          "section": "B"})
    assert not principal.may_see_student({"class_name": "CIVIL",
                                          "section": "C"})

    for class_id in ("CSE(AI)-C", "cse(ai) c", "CSE_AI_C", "CSE(AI)"):
        assert principal.may_see_class_id(class_id), class_id
    assert not principal.may_see_class_id("CIVIL-A")

    print("PASS  class scope survives the spellings the data already uses")


# -- lockout guards ---------------------------------------------------------

def test_the_last_super_admin_cannot_be_deactivated_demoted_or_deleted():
    client, directory = make_client()
    try:
        admin = setup_admin(client)
        user_id = admin["user_id"]

        deactivate = client.patch("/api/users/%d" % user_id,
                                  json={"status": "INACTIVE"})
        assert deactivate.status_code == 403, deactivate.text
        assert "last active Super Admin" in deactivate.json()["detail"]

        demote = client.patch("/api/users/%d" % user_id,
                              json={"role": "TEACHER"})
        assert demote.status_code == 403, demote.text

        remove = client.delete("/api/users/%d" % user_id)
        assert remove.status_code == 403, remove.text

        assert client.get("/api/users/%d" % user_id).json()["status"] == "ACTIVE"

        print("PASS  the last administrator cannot deactivate, demote or "
              "delete themselves")
    finally:
        teardown(client, directory)


def test_a_second_administrator_releases_the_guard():
    client, directory = make_client()
    try:
        admin = setup_admin(client)
        second = client.post("/api/users", json={
            "username": "deputy", "display_name": "Deputy",
            "password": "a-second-long-passphrase", "role": "SUPER_ADMIN"})
        assert second.status_code == 201, second.text

        # Now there are two, so the first may step down.
        demote = client.patch("/api/users/%d" % admin["user_id"],
                              json={"role": "TEACHER"})
        assert demote.status_code == 200, demote.text
        assert demote.json()["role"] == "TEACHER"

        print("PASS  the guard releases as soon as a second administrator "
              "exists")
    finally:
        teardown(client, directory)


def test_nobody_can_delete_the_account_they_are_using():
    client, directory = make_client()
    try:
        admin = setup_admin(client)
        client.post("/api/users", json={
            "username": "deputy", "display_name": "Deputy",
            "password": "a-second-long-passphrase", "role": "SUPER_ADMIN"})

        response = client.delete("/api/users/%d" % admin["user_id"])
        assert response.status_code == 403, response.text
        assert "signed in" in response.json()["detail"]

        print("PASS  you cannot delete the account you are signed in with")
    finally:
        teardown(client, directory)


# -- account management -----------------------------------------------------

def test_creating_activating_and_deactivating_a_teacher():
    client, directory = make_client()
    try:
        setup_admin(client)

        created = add_teacher(client, assignments=[
            {"class_name": "MECH", "section": "A"}])
        assert created.status_code == 201, created.text

        account = created.json()
        assert account["role"] == "TEACHER"
        assert account["status"] == "ACTIVE"
        assert account["assignments"] == [{"class_name": "MECH",
                                           "section": "A"}]

        user_id = account["user_id"]

        assert client.patch("/api/users/%d" % user_id,
                            json={"status": "INACTIVE"}).json()["status"] \
            == "INACTIVE"
        assert client.patch("/api/users/%d" % user_id,
                            json={"status": "ACTIVE"}).json()["status"] \
            == "ACTIVE"

        reassigned = client.patch("/api/users/%d" % user_id, json={
            "assignments": [{"class_name": "CIVIL", "section": ""}]})
        assert reassigned.json()["assignments"] == [{"class_name": "CIVIL",
                                                     "section": ""}]

        assert client.delete("/api/users/%d" % user_id).status_code == 200
        assert client.get("/api/users/%d" % user_id).status_code == 404

        print("PASS  a teacher can be created, deactivated, reactivated, "
              "reassigned and deleted")
    finally:
        teardown(client, directory)


def test_a_duplicate_username_is_refused():
    client, directory = make_client()
    try:
        setup_admin(client)
        assert add_teacher(client).status_code == 201
        assert add_teacher(client).status_code == 409

        assert len(client.get("/api/users").json()) == 2
        print("PASS  a duplicate username is refused")
    finally:
        teardown(client, directory)


def test_changing_your_own_password_needs_the_old_one():
    client, directory = make_client()
    try:
        setup_admin(client)

        wrong = client.post("/api/auth/password", json={
            "current_password": "not-it", "new_password": "a-brand-new-pass"})
        assert wrong.status_code == 401, wrong.text

        right = client.post("/api/auth/password", json={
            "current_password": ADMIN["password"],
            "new_password": "a-brand-new-passphrase"})
        assert right.status_code == 200, right.text

        # Every session ended, including this one.
        assert client.get("/api/students").status_code == 401

        again = client.post("/api/auth/login", json={
            "username": ADMIN["username"],
            "password": "a-brand-new-passphrase"})
        assert again.status_code == 200
        assert client.post("/api/auth/login", json={
            "username": ADMIN["username"],
            "password": ADMIN["password"]}).status_code == 401

        print("PASS  changing your password needs the old one and ends every "
              "session")
    finally:
        teardown(client, directory)


def test_administrative_actions_are_audited_without_secrets():
    client, directory = make_client()
    try:
        setup_admin(client)
        created = add_teacher(client).json()
        client.patch("/api/users/%d" % created["user_id"],
                     json={"status": "INACTIVE"})
        client.post("/api/students", json={
            "student_id": "AUDIT_1", "name": "Audited Student",
            "roll_number": "1", "class_name": "MECH", "section": "A"})

        entries = client.get("/api/users/audit").json()
        actions = {entry["action"] for entry in entries}

        for expected in ("user.bootstrap", "auth.login", "user.create",
                         "user.update", "student.create"):
            assert expected in actions, "%r was not audited: %s" % (expected,
                                                                    actions)

        text = str(entries).lower()
        for secret in ("password", "hash", "embedding", "token"):
            assert secret not in text, "the audit log mentions %r" % secret

        print("PASS  %d administrative actions audited, none of them carrying "
              "a secret" % len(entries))
    finally:
        teardown(client, directory)


# -- privacy ----------------------------------------------------------------

def test_no_account_response_ever_carries_a_password_or_a_hash():
    client, directory = make_client()
    try:
        setup_admin(client)
        created = add_teacher(client)

        urls = ["/api/users", "/api/users/%d" % created.json()["user_id"],
                "/api/auth/me", "/api/auth/state", "/api/users/audit"]

        for url in urls:
            text = client.get(url).text.lower()
            for forbidden in ("password", "hash", "pbkdf2", "token",
                              "embedding"):
                assert forbidden not in text, "%s exposes %r" % (url, forbidden)

        for text in (created.text.lower(),
                     client.patch("/api/users/%d" % created.json()["user_id"],
                                  json={"display_name": "Renamed"}).text.lower()):
            for forbidden in ("password", "hash", "pbkdf2"):
                assert forbidden not in text

        print("PASS  no account response carries a password, a hash or a token")
    finally:
        teardown(client, directory)


def test_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    stamp = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        setup_admin(client)
        assert "classvision.db" not in str(state.current_path())
    finally:
        teardown(client, directory)

    if stamp is not None:
        assert real.stat().st_mtime == stamp, (
            "the real attendance database was modified")

    print("PASS  the real attendance database was never opened")


# -- the sign-in screen -----------------------------------------------------

HTML = (PROJECT_ROOT / "backend" / "static" / "index.html").read_text(
    encoding="utf-8")
JS = (PROJECT_ROOT / "backend" / "static" / "app.js").read_text(encoding="utf-8")


def test_the_dashboard_has_a_login_screen_and_role_aware_navigation():
    assert 'id="auth-screen"' in HTML, "there is no sign-in screen"
    assert 'id="auth-username"' in HTML and 'id="auth-password"' in HTML
    assert 'type="password"' in HTML, "the password field is not masked"

    assert 'id="tab-admin"' in HTML and "hidden" in HTML
    assert 'id="view-admin"' in HTML
    assert 'id="account-logout"' in HTML, "there is no way to sign out"

    assert "/api/auth/state" in JS and "/api/auth/login" in JS
    assert "/api/auth/logout" in JS
    assert "handleUnauthorized" in JS, (
        "a 401 is not turned back into the login screen")

    print("PASS  the dashboard has a login screen, a sign-out control and "
          "role-aware navigation")


def test_the_disclaimer_describes_the_authentication_that_now_exists():
    assert "Authentication is not implemented." not in HTML, (
        "the dashboard still claims authentication is missing")
    assert "not evaluated production security" in HTML or \
           "not independently reviewed" in HTML, (
        "the footer does not qualify what the authentication is")

    # The existing research disclaimers must survive.
    assert "research prototype" in HTML
    assert "does not detect deepfakes" in HTML

    print("PASS  the disclaimer describes the authentication that exists and "
          "keeps the research warnings")


def test_the_browser_never_stores_a_password():
    """
    No credential in web storage, and none in a script-readable cookie.

    This used to ban localStorage outright, which was the right proxy while the
    dashboard stored nothing at all. It now keeps a colour-theme preference
    there - see bindTheme in app.js - and a blanket ban would have to be either
    deleted or worked around, so it is replaced by the check it was standing in
    for: what may be WRITTEN to storage is an allow-list of one key, and that
    key is not a credential.

    sessionStorage and document.cookie stay banned entirely. The session cookie
    is HttpOnly and set by the server precisely so that no script can read it,
    and nothing in this dashboard has a reason to reach for either.
    """
    for forbidden in ("sessionStorage", "document.cookie"):
        assert forbidden not in JS, (
            "the dashboard uses %s - the session cookie is HttpOnly and set "
            "by the server for a reason" % forbidden)

    # Every key written to localStorage, read out of the source rather than
    # assumed. A second key appearing here is a deliberate decision somebody
    # has to come and record in this list.
    #
    # There are two now. Both are PREFERENCES ABOUT THIS SCREEN and neither is
    # data: the colour theme, and whether the navigation rail is collapsed to
    # icons. Both belong to the machine somebody is sitting at rather than to
    # their account - the same teacher wants a wide rail on a desktop and a
    # narrow one on a laptop - which is exactly why they are here and not on
    # the server.
    allowed = {"THEME_KEY", "RAIL_KEY"}

    written = set(re.findall(r'localStorage\.setItem\(\s*([A-Za-z_$][\w$]*|"[^"]*")',
                             JS))
    keys = set(re.findall(r'const\s+(?:THEME_KEY|RAIL_KEY)\s*=\s*"([^"]+)"', JS))

    assert written <= allowed, (
        "the dashboard writes something other than a screen preference to "
        "localStorage: %s" % sorted(written - allowed))
    assert keys == {"classvision-theme", "classvision-rail"}, keys

    # And nothing credential-shaped goes near storage or either preference.
    theme_code = JS[JS.index("const THEME_KEY"):
                    JS.index("function percent")]
    rail_code = JS[JS.index("const RAIL_KEY"):
                   JS.index("function bindRail")]
    for shape in ("password", "token", "secret", "credential", "session"):
        assert shape not in theme_code.lower(), (
            "the stored-preference code mentions %r" % shape)
        assert shape not in rail_code.lower(), (
            "the stored rail preference mentions %r" % shape)

    print("PASS  the dashboard keeps no credential of its own - web storage "
          "holds two screen preferences and nothing else")


# -- the first-run dead end -------------------------------------------------

def test_the_command_line_can_create_the_first_administrator():
    """
    The way in when a browser cannot help.

    A dashboard that asks for a password nobody has been given is a dead end,
    and the fix must not depend on the dashboard itself working - the server
    may be running older code, or there may be no browser to hand.
    """
    import subprocess

    directory = Path(tempfile.mkdtemp(prefix="classvision_cli_admin_"))
    try:
        path = directory / "cli.db"

        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "database" / "init_db.py"),
             "--path", str(path)],
            cwd=str(PROJECT_ROOT), capture_output=True, check=True)

        created = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "database" / "create_admin.py"),
             "--path", str(path), "--username", "cli_owner",
             "--name", "CLI Owner", "--password", "a-long-enough-passphrase"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True)

        assert created.returncode == 0, created.stdout + created.stderr
        assert "SUPER_ADMIN" in created.stdout

        # The password must not be echoed anywhere, including on success.
        assert "a-long-enough-passphrase" not in created.stdout
        assert "a-long-enough-passphrase" not in created.stderr

        state.configure(path)
        service = AuthService(state.get_database())

        assert service.is_configured()
        token, user = service.login("cli_owner", "a-long-enough-passphrase")
        assert user["role"] == "SUPER_ADMIN"
        assert token

        listed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "database" / "create_admin.py"),
             "--path", str(path), "--list"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        assert "cli_owner" in listed.stdout

        # A stored hash, not the word - the listing legitimately SAYS that
        # passwords are held as PBKDF2 hashes, and an assertion that banned
        # the word would be testing the prose instead of the behaviour.
        assert "pbkdf2_sha256$" not in listed.stdout, (
            "the account listing printed a password hash")
        assert "$" not in listed.stdout.replace("--", ""), (
            "the account listing printed something hash-shaped")

        print("PASS  the command line creates a usable first Super Admin and "
              "prints no secret")
    finally:
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_command_line_can_reset_a_forgotten_password():
    import subprocess

    directory = Path(tempfile.mkdtemp(prefix="classvision_cli_reset_"))
    try:
        path = directory / "cli.db"

        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "database" / "init_db.py"),
             "--path", str(path)],
            cwd=str(PROJECT_ROOT), capture_output=True, check=True)
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "database" / "create_admin.py"),
             "--path", str(path), "--username", "forgetful",
             "--password", "the-original-passphrase"],
            cwd=str(PROJECT_ROOT), capture_output=True, check=True)

        reset = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "database" / "create_admin.py"),
             "--path", str(path), "--reset-password", "forgetful",
             "--password", "a-replacement-passphrase"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        assert reset.returncode == 0, reset.stdout + reset.stderr

        state.configure(path)
        service = AuthService(state.get_database())

        token, user = service.login("forgetful", "a-replacement-passphrase")
        assert user["username"] == "forgetful"

        try:
            service.login("forgetful", "the-original-passphrase")
            raise AssertionError("the old password still works after a reset")
        except AuthFailed:
            pass

        print("PASS  a forgotten password can be replaced, and the old one "
              "stops working")
    finally:
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_dashboard_never_dead_ends_when_the_server_is_out_of_date():
    """
    The failure that started this: a new page against an old server.

    Static files are read from disk per request, so a browser can load the
    current index.html and app.js while uvicorn is still running the Python it
    imported at start-up. /api/auth/state then 404s, and the page must say so
    rather than leaving its own placeholder markup on screen looking like a
    login form nobody has credentials for.
    """
    assert "failure.status = response.status" in JS, (
        "the fetch helper does not carry the status, so boot cannot tell a "
        "404 from an unreachable server")
    assert "failure.status === 404" in JS, (
        "boot does not handle a missing /api/auth/state")
    assert "newer than the server running it" in JS, (
        "a stale server produces no explanation")
    assert "Cannot reach ClassVision AI" in JS, (
        "an unreachable backend produces no explanation")

    print("PASS  a stale or unreachable backend is explained instead of "
          "becoming a sign-in dead end")


def test_the_sign_in_screen_offers_a_way_out_of_a_forgotten_password():
    """
    The dead-end this exists to prevent.

    A fresh installation is fine: with no accounts the screen asks for the
    first Super Admin. The trap is the OTHER case - one administrator, created
    months ago, whose password is gone. Setup is closed (correctly), the hash
    cannot be reversed (correctly), and before this the screen said only "use
    the account your administrator created for you" to the person who IS the
    administrator.

    Naming the reset command reveals nothing: running it needs write access to
    the database file, which is the same access as opening it in any SQLite
    tool.
    """
    assert 'id="auth-recover"' in HTML, (
        "the sign-in screen offers no recovery path")
    assert "Lost the only administrator password?" in HTML

    assert "create_admin.py --list" in HTML, (
        "the recovery block does not say how to find the username")
    assert "create_admin.py --reset-password" in HTML, (
        "the recovery block does not say how to set a new password")

    # It must not imply a password can be recovered, because it cannot.
    assert "cannot be recovered" in HTML

    print("PASS  the sign-in screen names the reset command and promises no "
          "recovery")


def test_the_recovery_path_it_names_actually_exists():
    """
    A documented command that does not work is worse than none.

    Checks the CLI really accepts those two flags, so this block cannot drift
    out of step with the tool it points at.
    """
    source = (PROJECT_ROOT / "database" / "create_admin.py").read_text(
        encoding="utf-8")

    for flag in ("--list", "--reset-password"):
        assert '"%s"' % flag in source, (
            "create_admin.py does not define %s, which the sign-in screen "
            "tells people to run" % flag)

    # And it must not be able to print a password.
    assert "does not print, recover or display an existing password" in source

    print("PASS  --list and --reset-password both exist in create_admin.py")


def test_the_recovery_block_is_hidden_during_first_run():
    """
    On first run there is no password to have lost.

    Offering a reset to somebody who has not created an account yet sends them
    to a command that would find nothing.
    """
    assert 'id="auth-recover" hidden' in HTML, (
        "the recovery block is not hidden by default")
    assert '$("auth-recover").hidden = first' in JS, (
        "the recovery block is not tied to the sign-in mode")

    print("PASS  the recovery block appears on sign-in only, never during "
          "first run")


def test_the_first_run_screen_says_it_is_creating_an_account():
    """The one screen a person meets knowing no credentials at all."""
    assert "Create your Super Admin account" in JS
    assert "ships no default credentials" in JS, (
        "the first-run screen does not explain why there is nothing to sign "
        "in with")
    assert "authMode" in JS and '"setup"' in JS

    print("PASS  the first-run screen asks for a new account rather than for "
          "an unknown password")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_a_password_is_hashed_with_a_salt_and_never_stored,
        test_the_database_holds_no_plaintext_password,
        test_a_weak_password_is_refused,
        test_an_empty_installation_asks_for_the_first_administrator,
        test_the_setup_endpoint_works_once_and_then_never_again,
        test_there_is_no_default_account_anywhere,
        test_signing_in_and_out,
        test_a_wrong_password_and_an_unknown_user_are_refused_alike,
        test_the_session_cookie_is_httponly_and_carries_no_identity,
        test_a_deactivated_account_stops_working_immediately,
        test_every_data_endpoint_refuses_an_unauthenticated_caller,
        test_a_teacher_cannot_reach_administration_by_calling_it_directly,
        test_a_teacher_keeps_the_classroom_work_they_need,
        test_only_a_super_admin_can_permanently_delete_a_student,
        test_a_signed_out_caller_cannot_delete_a_student,
        test_a_teacher_can_still_deactivate_a_student,
        test_an_assigned_teacher_sees_only_their_own_classes,
        test_a_class_assignment_without_a_section_covers_every_section,
        test_a_teacher_with_no_assignment_is_unrestricted_within_their_role,
        test_class_names_are_matched_however_they_were_typed,
        test_the_last_super_admin_cannot_be_deactivated_demoted_or_deleted,
        test_a_second_administrator_releases_the_guard,
        test_nobody_can_delete_the_account_they_are_using,
        test_creating_activating_and_deactivating_a_teacher,
        test_a_duplicate_username_is_refused,
        test_changing_your_own_password_needs_the_old_one,
        test_administrative_actions_are_audited_without_secrets,
        test_no_account_response_ever_carries_a_password_or_a_hash,
        test_tests_never_touch_the_real_database,
        test_the_dashboard_has_a_login_screen_and_role_aware_navigation,
        test_the_disclaimer_describes_the_authentication_that_now_exists,
        test_the_browser_never_stores_a_password,
        test_the_command_line_can_create_the_first_administrator,
        test_the_command_line_can_reset_a_forgotten_password,
        test_the_dashboard_never_dead_ends_when_the_server_is_out_of_date,
        test_the_first_run_screen_says_it_is_creating_an_account,
        test_the_sign_in_screen_offers_a_way_out_of_a_forgotten_password,
        test_the_recovery_path_it_names_actually_exists,
        test_the_recovery_block_is_hidden_during_first_run,
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
