"""
Cancelling, deleting and correcting a classroom session.

NO WEBCAM AND NO MODEL ARE NEEDED. Every test runs against its own temporary
SQLite file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That a session which has produced attendance cannot be deleted at all,
    whatever is typed; that a session nothing references can be, but only
    against its own id typed out; that cancelling keeps every row and is
    idempotent; that a finished session can be neither cancelled nor deleted;
    that deletion removes exactly the rows that cascade from the session and
    leaves no orphan and no unrelated row touched; that a teacher outside the
    class cannot reach any of it; and that the action list the dashboard
    renders is the server's own.

    This suite exists because the whole session lifecycle - cancel, edit,
    correct, delete, conflicts - had no test of any kind while being the most
    destructive surface in the project.

WHAT THIS DOES NOT VERIFY
    The browser. The dialog is asserted at source level in the UI section at
    the end; no DOM is built and nobody clicks anything.

Run with:
    python tests/test_session_lifecycle.py
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
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from attendance_rules import STATUS_PRESENT, AttendanceDecision  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

CLASS_ID = "CSE(AI)-C"
OTHER_CLASS = "MECH-A"

STUDENTS = {"STU_TEST_001": "Aarav Menon", "STU_TEST_002": "Brigid O'Hare"}

ADMIN = {"username": "owner", "display_name": "The Owner",
         "password": "a-long-enough-passphrase"}

TEACHER = {"username": "tsmith", "display_name": "T Smith",
           "password": "another-long-passphrase", "role": "TEACHER"}


# -- fixtures ---------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_lifecycle_"))
    state.configure(directory / "lifecycle.db")

    from backend.app import app
    client = TestClient(app)

    students = StudentRepository(state.get_database())
    for student_id, name in STUDENTS.items():
        students.upsert(student_id, name, class_name="CSE(AI)", section="C")

    return client, directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def schedule(session_id, class_id=CLASS_ID, expected=None, status="SCHEDULED",
             start=None):
    """
    A session written through the repository the API uses.

    Built here rather than through POST /api/sessions so a test can create an
    ACTIVE or COMPLETED session directly - the endpoint only ever creates
    SCHEDULED ones, which is correct and unhelpful for testing the rest.
    """
    start = start or (datetime.now(timezone.utc).replace(microsecond=0)
                      - timedelta(hours=2))

    session = AttendanceSession(session_id, class_id, start,
                                start + timedelta(hours=1),
                                policy_version="1.0",
                                expected_students=list(
                                    expected if expected is not None
                                    else STUDENTS))
    if status in ("ACTIVE", "COMPLETED"):
        session.start()
    if status == "COMPLETED":
        session.complete()

    SessionRepository(state.get_database()).save(session)
    return session


def record_attendance(session_id, student_id="STU_TEST_001"):
    """One attendance record, so the session has evidence against it."""
    AttendanceRepository(state.get_database()).save_decision(
        AttendanceDecision(
            student_id=student_id, session_id=session_id,
            status=STATUS_PRESENT, reason="fixture", policy_version="1.0",
            session_seconds=3600.0, presence_ratio=1.0))


def counts(session_id):
    database = state.get_database()
    return {
        "session": len(database.query(
            "SELECT 1 FROM attendance_sessions WHERE session_id = ?",
            (session_id,))),
        "expected": len(database.query(
            "SELECT 1 FROM session_expected_students WHERE session_id = ?",
            (session_id,))),
        "attendance": len(database.query(
            "SELECT 1 FROM attendance_records WHERE session_id = ?",
            (session_id,))),
        "presence": len(database.query(
            "SELECT 1 FROM presence_segments WHERE session_id = ?",
            (session_id,))),
    }


def sign_in_teacher(client, assignments=None):
    """A second client holding a teacher's cookie."""
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201
    created = client.post("/api/users", json=dict(
        TEACHER, assignments=assignments or []))
    assert created.status_code in (200, 201), created.text

    from backend.app import app
    teacher = TestClient(app)
    response = teacher.post("/api/auth/login", json={
        "username": TEACHER["username"], "password": TEACHER["password"]})
    assert response.status_code == 200, response.text
    return teacher


# -- what the server offers -------------------------------------------------

def test_the_action_list_comes_from_the_server_and_follows_the_status():
    client, directory = make_client()
    try:
        expected = {
            "SCHEDULED": ["edit", "start", "cancel", "delete"],
            "ACTIVE": ["view_live", "end", "cancel"],
            "COMPLETED": ["view_attendance", "view_analytics", "correct"],
        }

        for status, actions in expected.items():
            session_id = "SES-ACTIONS-%s" % status
            schedule(session_id, status=status)

            details = client.get("/api/sessions/%s/details" % session_id).json()
            assert details["available_actions"] == actions, (
                "%s offers %s, expected %s"
                % (status, details["available_actions"], actions))

        # And a cancelled session offers neither cancel nor delete.
        schedule("SES-ACTIONS-CANCELLED")
        client.post("/api/sessions/SES-ACTIONS-CANCELLED/cancel",
                    json={"reason": "wrong class"})
        details = client.get(
            "/api/sessions/SES-ACTIONS-CANCELLED/details").json()
        assert details["available_actions"] == ["view_details", "view_audit"]
        assert "delete" not in details["available_actions"]
        assert "cancel" not in details["available_actions"]

        print("PASS  4 statuses each offer the actions the API will accept, "
              "and no others")
    finally:
        teardown(client, directory)


def test_the_details_response_carries_the_impact_the_dialog_shows():
    client, directory = make_client()
    try:
        schedule("SES-IMPACT", status="COMPLETED")
        record_attendance("SES-IMPACT")

        details = client.get("/api/sessions/SES-IMPACT/details").json()
        evidence = details["evidence"]

        assert evidence["attendance_records"] == 1
        assert evidence["expected_students"] == 2
        assert evidence["presence_segments"] == 0
        assert evidence["has_attendance"] is True

        # The fields the dialog renders in its header.
        for field in ("session_id", "session_date", "class_id", "section",
                      "teacher", "status", "start_time", "end_time"):
            assert field in details, "details has no %r" % field

        print("PASS  details carries the counts and the header fields the "
              "dialog renders")
    finally:
        teardown(client, directory)


# -- deleting -------------------------------------------------------------

def test_a_session_with_attendance_cannot_be_deleted_at_all():
    """
    The refusal that matters. Three tables cascade from this row, so deleting
    a session that ran takes the lesson's whole register with it - and no
    confirmation makes that a reasonable thing to do by accident.
    """
    client, directory = make_client()
    try:
        schedule("SES-HASDATA", status="COMPLETED")
        record_attendance("SES-HASDATA")
        before = counts("SES-HASDATA")

        # Unconfirmed.
        refused = client.delete("/api/sessions/SES-HASDATA")
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "attendance record" in detail, (
            "the refusal does not say what would be lost: %r" % detail)
        assert "Cancel it instead" in detail, (
            "the refusal does not offer the safe alternative")

        # Confirmed with the correct id - still refused. The typed id is not a
        # way past this; it only exists for a session with nothing to lose.
        forced = client.delete(
            "/api/sessions/SES-HASDATA?confirm=SES-HASDATA")
        assert forced.status_code == 409, (
            "a typed id deleted a session that had a register: %s"
            % forced.text)

        assert counts("SES-HASDATA") == before, "the refusal still changed rows"
        assert before["attendance"] == 1

        print("PASS  a session with a register is 409 twice over, and nothing "
              "changed")
    finally:
        teardown(client, directory)


def test_presence_alone_is_enough_to_refuse_deletion():
    """Presence segments are evidence too, even with no decision recorded."""
    client, directory = make_client()
    try:
        session = schedule("SES-PRESENCE", status="COMPLETED")
        database = state.get_database()
        database.execute(
            "INSERT INTO presence_segments (session_id, student_id, "
            "start_time, end_time, duration_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("SES-PRESENCE", "STU_TEST_001",
             session.start_time.isoformat(), session.end_time.isoformat(),
             3600.0, "then"))

        refused = client.delete("/api/sessions/SES-PRESENCE?confirm=SES-PRESENCE")
        assert refused.status_code == 409, refused.text
        assert "presence segment" in refused.json()["detail"]

        assert counts("SES-PRESENCE")["presence"] == 1

        print("PASS  a presence segment alone refuses the deletion")
    finally:
        teardown(client, directory)


def test_a_session_nothing_references_needs_its_id_typed_out():
    client, directory = make_client()
    try:
        schedule("SES-TYPO")

        # No confirmation at all.
        first = client.delete("/api/sessions/SES-TYPO")
        assert first.status_code == 400, first.text
        assert "type its session id exactly" in first.json()["detail"]

        # The wrong id, including a prefix and a near-miss of the right one.
        for wrong in ("SES", "ses-typo", "SES-TYPO-X", "SESTYPO"):
            response = client.delete(
                "/api/sessions/SES-TYPO?confirm=%s" % wrong)
            assert response.status_code == 400, (
                "%r was accepted as confirmation: %s" % (wrong, response.text))

        assert counts("SES-TYPO")["session"] == 1, (
            "a failed confirmation deleted the session anyway")

        # Surrounding whitespace IS tolerated - the service strips it, exactly
        # as the student deletion does. A trailing space picked up by a
        # copy-paste should not stand between somebody and a deletion they
        # typed the id for; it is the id that must match, not the padding.
        done = client.delete("/api/sessions/SES-TYPO?confirm=%20SES-TYPO%20")
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["deleted"] is True
        assert body["session_id"] == "SES-TYPO"
        assert body["expected_students_removed"] == 2

        print("PASS  4 wrong confirmations refused; the exact id accepted, "
              "whitespace and all")
    finally:
        teardown(client, directory)


def test_deleting_removes_every_dependent_row_and_leaves_no_orphan():
    client, directory = make_client()
    try:
        schedule("SES-GONE")
        schedule("SES-KEPT", expected=["STU_TEST_002"])

        before = counts("SES-GONE")
        assert before == {"session": 1, "expected": 2, "attendance": 0,
                          "presence": 0}

        assert client.delete(
            "/api/sessions/SES-GONE?confirm=SES-GONE").status_code == 200

        after = counts("SES-GONE")
        assert after == {"session": 0, "expected": 0, "attendance": 0,
                         "presence": 0}, (
            "rows survived the deletion: %s" % after)

        # No orphan anywhere: every remaining child row points at a session
        # that still exists.
        database = state.get_database()
        for table in ("session_expected_students", "attendance_records",
                      "presence_segments"):
            orphans = database.query(
                "SELECT COUNT(*) AS n FROM %s child "
                "WHERE NOT EXISTS (SELECT 1 FROM attendance_sessions parent "
                "WHERE parent.session_id = child.session_id)" % table)
            assert orphans[0]["n"] == 0, (
                "%d orphaned row(s) in %s" % (orphans[0]["n"], table))

        # And the unrelated session is untouched.
        assert counts("SES-KEPT") == {"session": 1, "expected": 1,
                                      "attendance": 0, "presence": 0}

        # The students themselves are not deleted with the session.
        students = state.get_database().query("SELECT student_id FROM students")
        assert len(students) == len(STUDENTS), (
            "deleting a session deleted a student")

        print("PASS  the session and its 2 expected rows went, no orphan "
              "remains, the other session and both students are intact")
    finally:
        teardown(client, directory)


def test_deleting_an_unknown_session_is_a_404():
    client, directory = make_client()
    try:
        response = client.delete("/api/sessions/SES-NOPE?confirm=SES-NOPE")
        assert response.status_code == 404, response.text
        assert "does not exist" in response.json()["detail"].lower()

        print("PASS  deleting an unknown session is a 404")
    finally:
        teardown(client, directory)


def test_a_running_session_must_be_stopped_before_it_can_be_deleted():
    client, directory = make_client()
    try:
        schedule("SES-RUNNING", status="ACTIVE")

        refused = client.delete(
            "/api/sessions/SES-RUNNING?confirm=SES-RUNNING")
        assert refused.status_code == 409, refused.text
        assert "running" in refused.json()["detail"].lower()

        assert counts("SES-RUNNING")["session"] == 1

        print("PASS  a running session is refused until it is stopped")
    finally:
        teardown(client, directory)


def test_a_deleted_session_stays_deleted():
    client, directory = make_client()
    try:
        schedule("SES-ONCE")
        assert client.delete(
            "/api/sessions/SES-ONCE?confirm=SES-ONCE").status_code == 200

        # Gone from the list, gone from details, gone on a second delete.
        listing = client.get("/api/sessions").json()
        assert all(item["session_id"] != "SES-ONCE"
                   for item in listing["items"]), (
            "a deleted session is still listed")
        assert client.get("/api/sessions/SES-ONCE").status_code == 404
        assert client.get("/api/sessions/SES-ONCE/details").status_code == 404
        assert client.delete(
            "/api/sessions/SES-ONCE?confirm=SES-ONCE").status_code == 404

        print("PASS  a deleted session is absent from the list, details and a "
              "second delete")
    finally:
        teardown(client, directory)


def test_deletion_is_audited_and_the_audit_row_outlives_the_session():
    client, directory = make_client()
    try:
        schedule("SES-AUDITED")
        client.delete("/api/sessions/SES-AUDITED?confirm=SES-AUDITED")

        rows = [dict(row) for row in state.get_database().query(
            "SELECT * FROM audit_log WHERE action = 'session.deleted'")]
        assert len(rows) == 1, "the deletion was not audited"
        assert rows[0]["subject"] == "SES-AUDITED", (
            "the audit row does not name the session")
        assert "no attendance existed" in (rows[0]["detail"] or "")

        # The audit row references the id as text, not a foreign key, so it is
        # still here after the session it describes has gone.
        assert counts("SES-AUDITED")["session"] == 0

        print("PASS  the deletion is audited and the audit row survives the "
              "session")
    finally:
        teardown(client, directory)


# -- cancelling -----------------------------------------------------------

def test_cancelling_keeps_every_row_and_records_who_and_why():
    client, directory = make_client()
    try:
        schedule("SES-CANCEL")
        before = counts("SES-CANCEL")

        response = client.post("/api/sessions/SES-CANCEL/cancel",
                               json={"reason": "created for the wrong class"})
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["status"] == "CANCELLED"
        assert body["cancellation_reason"] == "created for the wrong class"
        assert body["cancelled_at"], "no cancellation time was recorded"

        assert counts("SES-CANCEL") == before, "cancelling removed a row"

        # Still in the list, and still readable.
        assert client.get("/api/sessions/SES-CANCEL").status_code == 200

        print("PASS  cancelling keeps every row and records the time and the "
              "reason")
    finally:
        teardown(client, directory)


def test_cancelling_twice_does_not_overwrite_the_first_cancellation():
    client, directory = make_client()
    try:
        schedule("SES-TWICE")
        first = client.post("/api/sessions/SES-TWICE/cancel",
                            json={"reason": "the original reason"}).json()

        again = client.post("/api/sessions/SES-TWICE/cancel",
                            json={"reason": "a different reason"})
        assert again.status_code == 200, again.text
        body = again.json()

        assert body["cancellation_reason"] == "the original reason", (
            "the second cancellation overwrote the first one's reason")
        assert body["cancelled_at"] == first["cancelled_at"], (
            "the second cancellation moved the timestamp")

        print("PASS  cancelling twice is idempotent and keeps the first "
              "reason")
    finally:
        teardown(client, directory)


def test_a_finished_session_cannot_be_cancelled():
    client, directory = make_client()
    try:
        schedule("SES-DONE", status="COMPLETED")
        record_attendance("SES-DONE")

        refused = client.post("/api/sessions/SES-DONE/cancel", json={})
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "already finished" in detail
        assert "correction" in detail.lower(), (
            "the refusal does not point at the correction workflow")

        assert client.get("/api/sessions/SES-DONE").json()["status"] == \
            "COMPLETED", "a completed session was cancelled anyway"

        print("PASS  a finished session is 409 and is pointed at correction "
              "instead")
    finally:
        teardown(client, directory)


def test_a_cancelled_session_can_still_be_deleted_if_it_left_nothing_behind():
    """
    Cancel then delete is a real sequence: somebody cancels a mistake, then
    tidies it away. It must still obey the same evidence rule.
    """
    client, directory = make_client()
    try:
        schedule("SES-TIDY")
        client.post("/api/sessions/SES-TIDY/cancel", json={"reason": "mistake"})

        assert client.delete("/api/sessions/SES-TIDY").status_code == 400, (
            "a cancelled session skipped the typed confirmation")
        assert client.delete(
            "/api/sessions/SES-TIDY?confirm=SES-TIDY").status_code == 200
        assert counts("SES-TIDY")["session"] == 0

        print("PASS  a cancelled session with nothing behind it deletes, still "
              "against its id")
    finally:
        teardown(client, directory)


def test_cancelling_an_unknown_session_is_a_404():
    client, directory = make_client()
    try:
        response = client.post("/api/sessions/SES-NOPE/cancel", json={})
        assert response.status_code == 404, response.text

        print("PASS  cancelling an unknown session is a 404")
    finally:
        teardown(client, directory)


# -- authorization --------------------------------------------------------

def test_an_unauthenticated_caller_cannot_cancel_or_delete():
    client, directory = make_client()
    try:
        schedule("SES-LOCKED")
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

        from backend.app import app
        anonymous = TestClient(app)
        try:
            assert anonymous.delete(
                "/api/sessions/SES-LOCKED?confirm=SES-LOCKED"
            ).status_code == 401
            assert anonymous.post(
                "/api/sessions/SES-LOCKED/cancel", json={}).status_code == 401
            assert anonymous.get(
                "/api/sessions/SES-LOCKED/details").status_code == 401
        finally:
            anonymous.close()

        assert counts("SES-LOCKED")["session"] == 1

        print("PASS  an unauthenticated caller gets 401 on details, cancel and "
              "delete")
    finally:
        teardown(client, directory)


def test_a_teacher_cannot_delete_a_session():
    """Deletion is Super Admin only, whatever class the teacher holds."""
    client, directory = make_client()
    try:
        schedule("SES-TEACHER", class_id=CLASS_ID)
        teacher = sign_in_teacher(client, assignments=[
            {"class_name": "CSE(AI)", "section": "C"}])
        try:
            refused = teacher.delete(
                "/api/sessions/SES-TEACHER?confirm=SES-TEACHER")
            assert refused.status_code == 403, refused.text

            # Cancel, however, is theirs to use within their own class.
            allowed = teacher.post("/api/sessions/SES-TEACHER/cancel",
                                   json={"reason": "clash"})
            assert allowed.status_code == 200, allowed.text
        finally:
            teacher.close()

        assert counts("SES-TEACHER")["session"] == 1, (
            "the teacher deleted the session after all")

        print("PASS  a teacher is 403 on delete and may cancel their own "
              "class's session")
    finally:
        teardown(client, directory)


def test_a_teacher_cannot_touch_another_classs_session():
    """
    Out of scope reads as ABSENT, not as forbidden.

    require_class_scope raises 404, not 403, on purpose: answering "403
    Forbidden" would confirm that a session with that id exists and which class
    it belongs to. A teacher probing ids learns nothing either way.
    """
    client, directory = make_client()
    try:
        schedule("SES-OTHER", class_id=OTHER_CLASS, expected=["STU_TEST_001"])
        teacher = sign_in_teacher(client, assignments=[
            {"class_name": "CSE(AI)", "section": "C"}])
        try:
            for response, what in (
                    (teacher.get("/api/sessions/SES-OTHER/details"), "details"),
                    (teacher.post("/api/sessions/SES-OTHER/cancel", json={}),
                     "cancel"),
                    (teacher.delete(
                        "/api/sessions/SES-OTHER?confirm=SES-OTHER"),
                     "delete")):
                assert response.status_code in (403, 404), (
                    "a teacher reached %s for another class: %s"
                    % (what, response.text))

            # The STATUS is the same either way, which is the substantive
            # protection: a teacher cannot tell the two apart from the code.
            absent = teacher.get("/api/sessions/SES-NOSUCH/details")
            present = teacher.get("/api/sessions/SES-OTHER/details")
            assert absent.status_code == present.status_code == 404, (
                "out-of-scope and absent sessions answer differently")

            # KNOWN GAP, asserted rather than glossed over: the two MESSAGES
            # still differ - "Session SES-X does not exist." for one that is
            # really absent, "Session not found." for one that exists in
            # another class. The wording therefore distinguishes them even
            # though the status does not, so a teacher enumerating ids can
            # learn which exist. Closing it means making require_class_scope
            # in backend/services/authz.py raise the same sentence the
            # not-found path uses; that is a change to shared authorization
            # messaging and is left for a deliberate decision rather than made
            # as a side effect of building a dialog.
            assert absent.json()["detail"] != present.json()["detail"], (
                "the messages now match - if that was deliberate, this "
                "assertion and the comment above should be replaced with an "
                "equality check")
        finally:
            teacher.close()

        assert counts("SES-OTHER")["session"] == 1, (
            "the teacher deleted another class's session")

        print("PASS  another class's session is refused on details, cancel and "
              "delete, and looks absent rather than forbidden")
    finally:
        teardown(client, directory)


# -- the dashboard ----------------------------------------------------------

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_the_sessions_page_offers_remove_on_every_row():
    assert 'data-remove-session' in JS, (
        "the sessions table has no Remove action")
    assert 'text: "Remove"' in JS

    assert 'openSessionRemoveDialog(button.getAttribute("data-remove-session"))'\
        in JS, "the Remove button is not wired to the dialog"

    print("PASS  every session row carries a Remove action wired to the dialog")


def test_the_dialog_shows_the_session_and_its_impact():
    dialog = HTML[HTML.index('id="session-remove-dialog"'):
                  HTML.index("<!-- create or edit an account -->")]

    for element in ("session-remove-who", "session-remove-meta",
                    "session-remove-impact", "session-remove-error"):
        assert 'id="%s"' % element in dialog, "the dialog has no %r" % element

    for button in ("session-cancel-action", "session-delete",
                   "session-remove-close"):
        assert 'id="%s"' % button in dialog, "the dialog has no %r" % button

    assert ">Cancel session<" in dialog
    assert ">Delete permanently<" in dialog
    assert ">Close<" in dialog

    # The header fields the requirement names.
    for label in ("Date", "Class", "Teacher", "Status"):
        assert 'metaRow("%s"' % label in JS, (
            "the dialog does not show %r" % label)

    # And the counts.
    for label in ("Attendance records", "Presence segments",
                  "Expected students"):
        assert 'evidenceRow("%s"' % label in JS, (
            "the dialog does not show %r" % label)

    print("PASS  the dialog shows id, date, class, teacher, status, 3 impact "
          "counts and 3 buttons")


def test_the_dialog_takes_its_actions_from_the_server():
    """The policy lives in one place, and it is not the browser."""
    assert '$("session-cancel-action").hidden = !actions.includes("cancel")' \
        in JS, "the Cancel button is not driven by available_actions"
    assert '$("session-delete").hidden = !actions.includes("delete")' in JS, (
        "the Delete button is not driven by available_actions")
    assert "details.available_actions" in JS

    # The browser must not re-derive the rule from the status.
    module = JS[JS.index("/* ---------- cancel or delete a session"):
                JS.index("/* ---------- sign in ---------- */")]
    for reimplementation in ('status === "SCHEDULED"', 'status === "COMPLETED"',
                             'status === "ACTIVE"',
                             "has_attendance >", "attendance_records >"):
        assert reimplementation not in module, (
            "the browser re-implements the deletion policy: %r"
            % reimplementation)

    print("PASS  the buttons follow available_actions and the policy is not "
          "re-derived in the browser")


def test_the_dialog_requires_the_typed_id_and_cannot_double_submit():
    module = JS[JS.index("/* ---------- cancel or delete a session"):
                JS.index("/* ---------- sign in ---------- */")]

    assert "typed !== sessionId" in module, (
        "the dialog does not check the typed id")
    assert "confirm=" in module, "the typed id is not sent to the API"

    # A second click while a delete is in flight must do nothing.
    assert "state.removingBusy" in module, (
        "there is no guard against a duplicate request")
    assert "if (!state.removingSession || state.removingBusy) return;" in module

    # The field is cleared between openings so one session's id cannot be
    # left sitting in the box for another.
    assert '$("session-delete-confirm").value = "";' in JS

    print("PASS  the typed id is required and sent, and a second click while "
          "deleting is ignored")


def test_the_dialog_refreshes_the_list_and_the_analytics():
    module = JS[JS.index("/* ---------- cancel or delete a session"):
                JS.index("/* ---------- sign in ---------- */")]

    assert "refreshAfterSessionChange" in module
    for reload in ("loadSessions()", "loadOverview()", "loadClasses()"):
        assert reload in module, (
            "%s is not reloaded after a session changes" % reload)

    print("PASS  the list, the class filter and the overview all reload after "
          "a change")


def test_session_lifecycle_tests_never_touch_the_real_database():
    """
    Checked on the CONFIGURED PATH, not on the file's timestamp.

    A modification time is the wrong thing to watch here: the project opens
    SQLite in WAL mode, so a write lands in classvision.db-wal and leaves
    classvision.db's mtime alone until a checkpoint. A test that watched the
    mtime would pass while writing to the real database - and this project's
    data directory is inside a OneDrive folder, which touches those files on
    its own schedule, so the timestamp is not even reliable in the other
    direction.

    Asserting the path is what the established suites do, and it is the fact
    that actually matters: a connection opened somewhere else cannot reach the
    real file.
    """
    real = PROJECT_ROOT / "data" / "classvision.db"

    client, directory = make_client()
    try:
        configured = Path(state.current_path())
        assert "classvision.db" not in str(configured), (
            "the suite is configured against the real database: %s" % configured)
        assert configured.parent != real.parent, (
            "the suite writes into the real data directory")

        # And a real destructive operation, to prove it lands in the temp file.
        schedule("SES-TEMP")
        assert client.delete(
            "/api/sessions/SES-TEMP?confirm=SES-TEMP").status_code == 200
        assert counts("SES-TEMP")["session"] == 0
    finally:
        teardown(client, directory)

    print("PASS  the suite is configured against a temporary database, checked "
          "by path rather than by mtime")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_the_action_list_comes_from_the_server_and_follows_the_status,
    test_the_details_response_carries_the_impact_the_dialog_shows,
    test_a_session_with_attendance_cannot_be_deleted_at_all,
    test_presence_alone_is_enough_to_refuse_deletion,
    test_a_session_nothing_references_needs_its_id_typed_out,
    test_deleting_removes_every_dependent_row_and_leaves_no_orphan,
    test_deleting_an_unknown_session_is_a_404,
    test_a_running_session_must_be_stopped_before_it_can_be_deleted,
    test_a_deleted_session_stays_deleted,
    test_deletion_is_audited_and_the_audit_row_outlives_the_session,
    test_cancelling_keeps_every_row_and_records_who_and_why,
    test_cancelling_twice_does_not_overwrite_the_first_cancellation,
    test_a_finished_session_cannot_be_cancelled,
    test_a_cancelled_session_can_still_be_deleted_if_it_left_nothing_behind,
    test_cancelling_an_unknown_session_is_a_404,
    test_an_unauthenticated_caller_cannot_cancel_or_delete,
    test_a_teacher_cannot_delete_a_session,
    test_a_teacher_cannot_touch_another_classs_session,
    test_the_sessions_page_offers_remove_on_every_row,
    test_the_dialog_shows_the_session_and_its_impact,
    test_the_dialog_takes_its_actions_from_the_server,
    test_the_dialog_requires_the_typed_id_and_cannot_double_submit,
    test_the_dialog_refreshes_the_list_and_the_analytics,
    test_session_lifecycle_tests_never_touch_the_real_database,
]


def main():
    enable_safe_console()

    passed = 0
    for test in TESTS:
        try:
            test()
            passed += 1
        except AssertionError as error:
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                        # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(error).__name__, error))

    print("")
    print("%d/%d tests passed." % (passed, len(TESTS)))
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
