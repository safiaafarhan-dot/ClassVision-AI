"""
Session scheduling tests (dashboard, added after Step 9).

Every test runs against its OWN temporary SQLite file. The real
data/classvision.db is never opened.

WHAT THIS VERIFIES
    That a teacher can schedule a session through the dashboard API, that the
    validation refuses everything it should, that a scheduled session is
    immediately selectable by the Live Classroom tab, and that scheduling
    calculates no attendance and creates no second session model.

WHAT THIS DOES NOT VERIFY
    Anything about recognition, liveness or attendance accuracy. Scheduling
    records an intention; it runs no camera and decides nothing.

Run with:
    python tests/test_session_creation.py
"""

import json
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
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from attendance_session import SESSION_SCHEDULED  # noqa: E402
from database import SCHEMA  # noqa: E402
from repositories import AttendanceRepository, StudentRepository  # noqa: E402
from schema_baseline import EXPECTED_TABLES  # noqa: E402

from backend.services import state  # noqa: E402
from backend.services.data_service import generate_session_id  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STUDENTS = {"STU001": "Student One", "STU002": "Student Two",
            "STU003": "Student Three"}

CLASS_ID = "CLASS-10A"

# Anything resembling biometric data, media, a path or a secret.
FORBIDDEN_KEYS = (
    "embedding", "vector", "descriptor", "feature", "face", "image", "photo",
    "crop", "frame", "video", "thumbnail", "similarity", "password", "secret",
    "token", "credential", "api_key", "db_path", "file_path", "model_path",
)

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def make_client(seed=True):
    directory = Path(tempfile.mkdtemp(prefix="classvision_sched_"))
    state.configure(directory / "sched.db")

    if seed:
        students = StudentRepository(state.get_database())
        for student_id, name in STUDENTS.items():
            students.upsert(student_id, name)

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def at(hour, minute=0, day_offset=1):
    """A timezone-aware ISO timestamp, tomorrow by default."""
    moment = (datetime.now(timezone.utc) + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return moment.isoformat()


def payload(**overrides):
    body = {
        "class_id": CLASS_ID,
        "start_time": at(9),
        "end_time": at(10),
        "expected_students": ["STU001", "STU002"],
    }
    body.update(overrides)
    return body


def walk(value, visit, trail="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            visit(key, "%s.%s" % (trail, key))
            walk(item, visit, "%s.%s" % (trail, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, visit, "%s[%d]" % (trail, index))


# -- the happy path --------------------------------------------------------

def test_a_valid_session_is_created_scheduled():
    client, directory = make_client()
    try:
        response = client.post("/api/sessions", json=payload())
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["status"] == SESSION_SCHEDULED, (
            "a new session was created %s" % body["status"])
        assert body["class_id"] == CLASS_ID
        assert body["expected_students"] == ["STU001", "STU002"]
        assert body["completed_at"] is None
        assert body["policy_version"], "no policy version was recorded"

        print("PASS  a valid request creates %s, SCHEDULED, with %d expected "
              "students" % (body["session_id"], len(body["expected_students"])))
    finally:
        teardown(client, directory)


def test_the_generated_identifier_follows_the_project_convention():
    client, directory = make_client()
    try:
        body = client.post("/api/sessions", json=payload()).json()

        assert re.match(r"^SES-\d{4}-\d{2}-\d{2}-\d{4}-[A-Z0-9-]+$",
                        body["session_id"]), (
            "unexpected identifier shape: %s" % body["session_id"])

        # An awkward class name still produces a usable id.
        odd = client.post("/api/sessions", json=payload(
            class_id="Physics 2 / Lab", start_time=at(14), end_time=at(15)))
        assert odd.status_code == 201, odd.text
        assert " " not in odd.json()["session_id"]
        assert "/" not in odd.json()["session_id"]

        print("PASS  identifiers follow the SES-date-time-class convention "
              "(%s, %s)" % (body["session_id"], odd.json()["session_id"]))
    finally:
        teardown(client, directory)


def test_an_explicit_identifier_is_honoured():
    client, directory = make_client()
    try:
        body = client.post("/api/sessions",
                           json=payload(session_id="SES-CUSTOM-1")).json()
        assert body["session_id"] == "SES-CUSTOM-1"
        print("PASS  a caller may supply its own session identifier")
    finally:
        teardown(client, directory)


def test_the_same_class_may_meet_twice_in_one_day():
    """
    A morning and an afternoon lesson are two sessions, not a collision.

    This is why the start time is part of the generated identifier.
    """
    client, directory = make_client()
    try:
        morning = client.post("/api/sessions",
                              json=payload(start_time=at(9), end_time=at(10)))
        afternoon = client.post("/api/sessions",
                                json=payload(start_time=at(14), end_time=at(15)))

        assert morning.status_code == 201, morning.text
        assert afternoon.status_code == 201, afternoon.text
        assert morning.json()["session_id"] != afternoon.json()["session_id"]

        print("PASS  two sessions for one class on one day: %s and %s"
              % (morning.json()["session_id"], afternoon.json()["session_id"]))
    finally:
        teardown(client, directory)


# -- refusals --------------------------------------------------------------

def test_invalid_times_are_refused():
    client, directory = make_client()
    try:
        cases = [
            (payload(start_time=at(10), end_time=at(9)), "end before start"),
            (payload(start_time=at(9), end_time=at(9)), "zero length"),
            (payload(end_time=at(9, 0)), "identical instants"),
            (payload(start_time="not a time"), "unparseable start"),
            (payload(end_time="2026-13-45T99:00:00+00:00"), "impossible date"),
            (payload(start_time="2026-09-24T09:00:00"), "start with no offset"),
            (payload(end_time="2026-09-24T10:00:00"), "end with no offset"),
            (payload(start_time=at(9), end_time=at(9, 0, 4)), "several days long"),
        ]

        for body, description in cases:
            response = client.post("/api/sessions", json=body)
            assert response.status_code in (400, 422), (
                "%s returned %d" % (description, response.status_code))

            detail = json.dumps(response.json()).lower()
            for leak in ("traceback", "sqlite", "c:\\", "/users/", "select "):
                assert leak not in detail, (
                    "%s leaked %r" % (description, leak))

        print("PASS  %d invalid time combinations refused cleanly, none "
              "leaking internals" % len(cases))
    finally:
        teardown(client, directory)


def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc():
    """
    "09:00" means nothing without an offset.

    Guessing one is how a session ends up an hour away from the lesson it is
    supposed to describe, so the API refuses instead.
    """
    client, directory = make_client()
    try:
        response = client.post("/api/sessions",
                               json=payload(start_time="2026-09-24T09:00:00"))
        assert response.status_code == 400
        assert "offset" in response.json()["detail"].lower(), (
            "the refusal does not explain what is missing: %s"
            % response.json()["detail"])

        assert client.get("/api/sessions").json()["total"] == 0, (
            "a refused request still created a session")

        print("PASS  a timestamp with no offset is refused, and explains why")
    finally:
        teardown(client, directory)


def test_unknown_student_ids_are_refused_and_named():
    client, directory = make_client()
    try:
        response = client.post("/api/sessions", json=payload(
            expected_students=["STU001", "GHOST-1", "GHOST-2"]))

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "GHOST-1" in detail and "GHOST-2" in detail, (
            "the refusal does not name the unknown ids: %s" % detail)
        assert "STU001" not in detail, (
            "the refusal blamed a student who does exist")

        assert client.get("/api/sessions").json()["total"] == 0, (
            "a session was created despite an unknown student")

        print("PASS  unknown student ids are refused and named individually")
    finally:
        teardown(client, directory)


def test_an_empty_expected_list_is_refused():
    client, directory = make_client()
    try:
        response = client.post("/api/sessions",
                               json=payload(expected_students=[]))
        assert response.status_code == 400
        assert "absence" in response.json()["detail"].lower(), (
            "the refusal does not say why an empty list is useless: %s"
            % response.json()["detail"])

        blank = client.post("/api/sessions",
                            json=payload(expected_students=["", "   "]))
        assert blank.status_code == 400

        print("PASS  a session with nobody expected is refused, with the "
              "reason")
    finally:
        teardown(client, directory)


def test_missing_required_fields_are_refused():
    client, directory = make_client()
    try:
        cases = [
            ({}, "nothing at all"),
            ({"class_id": CLASS_ID}, "no times"),
            (payload(class_id=""), "empty class"),
            (payload(class_id="   "), "whitespace class"),
            ({k: v for k, v in payload().items() if k != "start_time"},
             "no start time"),
            ({k: v for k, v in payload().items() if k != "end_time"},
             "no end time"),
            ({k: v for k, v in payload().items() if k != "expected_students"},
             "no expected students"),
            (payload(class_id="x" * 500), "over-long class"),
        ]

        for body, description in cases:
            response = client.post("/api/sessions", json=body)
            assert response.status_code in (400, 422), (
                "%s returned %d" % (description, response.status_code))

        assert client.get("/api/sessions").json()["total"] == 0
        print("PASS  %d incomplete requests refused, nothing created"
              % len(cases))
    finally:
        teardown(client, directory)


def test_a_duplicate_identifier_is_a_conflict_not_an_overwrite():
    """
    The repository upserts, which is right for re-saving a session and wrong
    for creating one. Without this guard a second POST would silently rewrite
    an existing session - including one that had already been run.
    """
    client, directory = make_client()
    try:
        first = client.post("/api/sessions",
                            json=payload(session_id="SES-FIXED"))
        assert first.status_code == 201

        second = client.post("/api/sessions", json=payload(
            session_id="SES-FIXED", class_id="CLASS-DIFFERENT",
            expected_students=["STU003"]))
        assert second.status_code == 409, (
            "a duplicate returned %d" % second.status_code)

        stored = client.get("/api/sessions/SES-FIXED").json()
        assert stored["class_id"] == CLASS_ID, (
            "the duplicate overwrote the original class")
        assert stored["expected_students"] == ["STU001", "STU002"], (
            "the duplicate overwrote the expected students")

        print("PASS  a duplicate identifier is 409 and changes nothing")
    finally:
        teardown(client, directory)


def test_a_session_cannot_be_created_already_completed():
    """
    Attendance comes from evidence, and a session that never ran has none.

    The request model has no status field at all, so this is enforced by
    there being nothing to set - which is stronger than validating it.
    """
    client, directory = make_client()
    try:
        response = client.post("/api/sessions", json=payload(
            status="COMPLETED", completed_at=at(11)))

        assert response.status_code in (201, 422), response.text
        if response.status_code == 201:
            assert response.json()["status"] == SESSION_SCHEDULED, (
                "a status was smuggled in: %s" % response.json()["status"])
            assert response.json()["completed_at"] is None

        from backend.schemas.models import SessionCreateRequest
        assert "status" not in SessionCreateRequest.model_fields
        assert "completed_at" not in SessionCreateRequest.model_fields

        print("PASS  status and completion cannot be set at creation")
    finally:
        teardown(client, directory)


# -- it reaches the rest of the system -------------------------------------

def test_a_created_session_appears_in_the_sessions_list():
    client, directory = make_client()
    try:
        created = client.post("/api/sessions", json=payload()).json()

        listing = client.get("/api/sessions").json()
        assert listing["total"] == 1
        assert listing["items"][0]["session_id"] == created["session_id"]
        assert listing["items"][0]["expected_students"] == 2, (
            "the list does not show the expected-student count")

        filtered = client.get("/api/sessions?class_id=%s" % CLASS_ID).json()
        assert filtered["total"] == 1

        by_date = client.get("/api/sessions?session_date=%s"
                             % created["session_date"]).json()
        assert by_date["total"] == 1

        print("PASS  the new session appears in the list, and under the class "
              "and date filters")
    finally:
        teardown(client, directory)


def test_a_created_session_is_selectable_by_live_classroom():
    """
    The whole point of the feature.

    This uses the EXACT query the Live Classroom tab issues, so a change to
    either side that broke the connection would fail here.
    """
    client, directory = make_client()
    try:
        created = client.post("/api/sessions", json=payload()).json()

        runnable = []
        for status in ("ACTIVE", "SCHEDULED"):
            page = client.get("/api/sessions?status=%s&limit=50&offset=0"
                              % status).json()
            runnable.extend(page["items"])

        assert any(s["session_id"] == created["session_id"] for s in runnable), (
            "the new session is not offered to Live Classroom")

        # And the live API accepts it: a start attempt gets past session
        # validation and fails only on the camera, which no test machine has.
        response = client.post("/api/live/start",
                               json={"session_id": created["session_id"]})
        assert response.status_code in (200, 503), (
            "the live API rejected a freshly scheduled session with %d: %s"
            % (response.status_code, response.text))
        assert response.status_code != 404, "the live API could not find it"
        assert response.status_code != 409, (
            "the live API refused its state: %s" % response.text)

        if response.status_code == 200:
            client.post("/api/live/stop")

        print("PASS  a scheduled session is offered to Live Classroom and "
              "accepted by /api/live/start")
    finally:
        teardown(client, directory)


def test_scheduling_calculates_no_attendance():
    client, directory = make_client()
    try:
        created = client.post("/api/sessions", json=payload()).json()

        attendance = AttendanceRepository(state.get_database())
        assert attendance.count_for_session(created["session_id"]) == 0, (
            "scheduling produced attendance records")
        assert attendance.get_segments(created["session_id"]) == [], (
            "scheduling produced presence evidence")

        api_view = client.get("/api/sessions/%s/attendance"
                              % created["session_id"]).json()
        assert api_view["total"] == 0

        analytics = client.get("/api/analytics/sessions/%s"
                               % created["session_id"]).json()
        assert analytics["is_final"] is False, (
            "a scheduled session is being treated as final")
        assert analytics["evaluated_students"] == 0

        print("PASS  scheduling records an intention and calculates nothing")
    finally:
        teardown(client, directory)


def test_scheduling_uses_the_existing_tables_only():
    client, directory = make_client()
    try:
        client.post("/api/sessions", json=payload())

        tables = sorted(state.get_database().table_names())
        # Every table the project is allowed to have, listed by hand in
        # tests/schema_baseline.py. Scheduling contributes none of them,
        # which is what this test proves.
        expected = EXPECTED_TABLES
        assert tables == expected, (
            "the table list changed - expected %s, found %s"
            % (expected, tables))

        # A substring check would match the SCHEDULED status in the CHECK
        # constraint, so look for CREATE TABLE statements instead.
        created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
        assert sorted(created) == expected, (
            "the schema declares %s" % sorted(created))

        rows = state.get_database().query(
            "SELECT student_id FROM session_expected_students")
        assert len(rows) == 2, (
            "expected students were not written to the existing table")

        print("PASS  scheduling writes to attendance_sessions and "
              "session_expected_students only - the same %d tables"
              % len(tables))
    finally:
        teardown(client, directory)


def test_creating_a_session_with_no_students_registered():
    client, directory = make_client(seed=False)
    try:
        response = client.post("/api/sessions", json=payload())
        assert response.status_code == 400
        assert "not registered" in response.json()["detail"].lower()
        print("PASS  scheduling against an empty roster fails with a clear "
              "reason, not a database error")
    finally:
        teardown(client, directory)


# -- privacy ---------------------------------------------------------------

def test_the_creation_endpoint_exposes_nothing_sensitive():
    client, directory = make_client()
    try:
        bodies = [client.post("/api/sessions", json=payload()).json(),
                  client.post("/api/sessions", json=payload()).json(),
                  client.post("/api/sessions",
                              json=payload(expected_students=["NOPE"])).json()]

        offenders = []
        for body in bodies:
            def inspect(key, trail):
                lowered = key.lower()
                if lowered in ("first_seen", "last_seen"):
                    return
                for banned in FORBIDDEN_KEYS:
                    if banned in lowered:
                        offenders.append(trail)

            walk(body, inspect)

            text = json.dumps(body)
            for magic in ("data:image", "base64,", "\\x89PNG"):
                assert magic not in text

        assert not offenders, "creation responses exposed %s" % offenders

        from backend.schemas.models import SessionCreateRequest
        declared = set(SessionCreateRequest.model_fields)
        bad = [f for f in declared
               if any(b in f.lower() for b in FORBIDDEN_KEYS)]
        assert not bad, "the request model declares %s" % bad

        print("PASS  %d creation responses and %d request fields carry nothing "
              "biometric, media, path or secret"
              % (len(bodies), len(declared)))
    finally:
        teardown(client, directory)


def test_tests_never_touch_the_real_database():
    from database import DEFAULT_DATABASE_PATH

    client, directory = make_client()
    try:
        assert state.get_database().path != DEFAULT_DATABASE_PATH
        assert str(directory) in str(state.get_database().path)
        print("PASS  scheduling tests run against an isolated temporary "
              "database")
    finally:
        teardown(client, directory)


# -- the dashboard ---------------------------------------------------------

def test_the_sessions_page_has_a_visible_add_session_control():
    assert 'id="add-session"' in HTML, "there is no Add Session button"
    assert "+ Add Session" in HTML, "the button is not labelled recognisably"

    button = HTML[HTML.index('id="add-session"') - 200:
                  HTML.index('id="add-session"') + 60]
    assert "view-sessions" in HTML[:HTML.index('id="add-session"')], (
        "the button is not on the Sessions page")
    assert "btn-primary" in button, "the button is not styled as a primary action"

    print("PASS  the Sessions page carries a primary + Add Session button")


def test_the_scheduling_dialog_is_accessible_and_labelled():
    assert 'id="session-dialog"' in HTML, "there is no scheduling dialog"
    assert "<dialog" in HTML and "aria-labelledby" in HTML

    for control in ("new-session-class", "new-session-date",
                    "new-session-start", "new-session-end"):
        assert 'id="%s"' % control in HTML, "%s is missing" % control
        assert 'for="%s"' % control in HTML, "%s has no label" % control

    assert 'id="new-session-error"' in HTML
    assert 'role="alert"' in HTML, "validation errors are not announced"
    assert "<fieldset" in HTML and "<legend" in HTML, (
        "the student picker is not grouped with a legend")

    print("PASS  the dialog is a labelled native modal with an alert region")


def test_students_are_chosen_from_the_roster_not_typed():
    assert "/api/students" in JS, "the dialog never loads the roster"
    assert 'type: "checkbox"' in JS, "students are not selectable as a list"
    assert 'id="new-session-students"' in HTML

    # No free-text field for student ids anywhere in the dialog.
    #
    # Asserted as "every text field is one of the KNOWN non-student fields"
    # rather than by counting them. The count used to be 1 and the dialog has
    # since gained a registered-class and a section field, both of which choose
    # a GROUP rather than naming a person - so a count would fail on a change
    # that does not touch what this test is about. Named fields fail only when
    # a new text input actually appears, which is exactly when somebody should
    # look.
    dialog = HTML[HTML.index('id="session-dialog"'):
                  HTML.index("evidence / override dialog")]

    assert "student_id" not in dialog

    allowed = {
        "new-session-class",        # the session's free-text identifier
        "new-session-class-name",   # the registered class to expect from
        "new-session-section",      # narrows that class to one section
    }

    import re as _re
    text_fields = set(_re.findall(
        r'<input[^>]*type="text"[^>]*id="([a-z0-9-]+)"', dialog))
    text_fields |= set(_re.findall(
        r'<input[^>]*id="([a-z0-9-]+)"[^>]*type="text"', dialog))

    unexpected = sorted(text_fields - allowed)
    assert not unexpected, (
        "the dialog has a free-text field that is not a class or section, so "
        "a student id may now be typeable: %s" % unexpected)

    # And the roster is still checkboxes, so a student is picked and not typed.
    assert 'id="new-session-students"' in HTML

    print("PASS  students are picked from the registered roster, never typed")


def test_the_dialog_posts_to_the_existing_sessions_endpoint():
    """No second session system: the same URL the Sessions table reads."""
    assert '"/api/sessions", "POST"' in JS, (
        "the dialog does not post to /api/sessions")

    # Checked as COMPLETE url literals rather than as substrings.
    #
    # "/api/live/sessions" - the real endpoint that lists running sessions -
    # contains "/api/live/session", so a bare substring test reports the
    # genuine endpoint as an invented one. Matching the closing quote still
    # catches api("/api/live/session"), which is what this guards against,
    # without false-matching a different endpoint whose name starts the same.
    for invented in ("/api/schedule", "/api/new-session",
                     "/api/sessions/create", "/api/live/session"):
        assert ('"%s"' % invented) not in JS, (
            "the dialog invented a second endpoint: %s" % invented)

    assert "loadLiveSessions" in JS, (
        "the new session is not offered to Live Classroom after creation")
    assert "toISOString" in JS, (
        "local times are not converted before sending")

    print("PASS  the dialog posts to /api/sessions and refreshes the live "
          "session list")


def test_the_dialog_adds_no_animation_or_external_request():
    """
    Scheduling pulls in no library and reaches no external host.

    The animation half of this assertion moved to
    tests/test_dashboard_frontend.py, which now requires every animation in
    the dashboard to be switched off by prefers-reduced-motion rather than
    requiring that none exist. What is kept here is the part that has not
    changed: no framework, no CDN, no build step.
    """
    combined = (HTML + CSS + JS).replace("http://www.w3.org/2000/svg", "")
    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr"):
        assert external not in combined

    # A ceiling, not a target. It exists to catch a framework or a bundled
    # library arriving by the back door, and the checks above are what
    # actually enforce that. Raised from 120 KB when the dashboard gained a
    # sign-in screen, an administration view and the full student registry
    # table - all of it hand-written HTML, CSS and JavaScript with no build
    # step, which is the property this test protects.
    total = len(HTML) + len(CSS) + len(JS)
    # See test_dashboard_frontend.test_frontend_is_lightweight for why
    # this ceiling moves and what it is actually guarding against.
    assert total < 620_000, "the frontend has grown to %d bytes" % total

    print("PASS  scheduling adds no animation and no external request "
          "(%.1f KB total)" % (total / 1024.0))


def main():
    enable_safe_console()

    tests = [
        test_a_valid_session_is_created_scheduled,
        test_the_generated_identifier_follows_the_project_convention,
        test_an_explicit_identifier_is_honoured,
        test_the_same_class_may_meet_twice_in_one_day,
        test_invalid_times_are_refused,
        test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc,
        test_unknown_student_ids_are_refused_and_named,
        test_an_empty_expected_list_is_refused,
        test_missing_required_fields_are_refused,
        test_a_duplicate_identifier_is_a_conflict_not_an_overwrite,
        test_a_session_cannot_be_created_already_completed,
        test_a_created_session_appears_in_the_sessions_list,
        test_a_created_session_is_selectable_by_live_classroom,
        test_scheduling_calculates_no_attendance,
        test_scheduling_uses_the_existing_tables_only,
        test_creating_a_session_with_no_students_registered,
        test_the_creation_endpoint_exposes_nothing_sensitive,
        test_tests_never_touch_the_real_database,
        test_the_sessions_page_has_a_visible_add_session_control,
        test_the_scheduling_dialog_is_accessible_and_labelled,
        test_students_are_chosen_from_the_roster_not_typed,
        test_the_dialog_posts_to_the_existing_sessions_endpoint,
        test_the_dialog_adds_no_animation_or_external_request,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__, error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
