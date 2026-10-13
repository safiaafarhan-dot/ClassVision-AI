"""
API tests for the Step 7 dashboard backend.

Every test runs against its OWN temporary SQLite file. The real
data/classvision.db is never opened.

Includes the boundary checks that matter most: that no endpoint can return a
biometric field, an image, a file path or a credential, and that GET requests
never change data.

Run with:
    python tests/test_dashboard_api.py
"""

import json
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

from attendance_rules import AttendanceDecision, PresenceSegment  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from database import DEFAULT_DATABASE_PATH  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

# Anything resembling biometric data, media, a path or a secret.
FORBIDDEN_KEYS = (
    "embedding", "embeddings", "vector", "vectors", "descriptor", "feature",
    "face", "image", "photo", "crop", "frame", "video", "thumbnail",
    "password", "secret", "token", "credential", "api_key", "db_path",
    "database_path", "filepath", "file_path", "connection_string",
)

STUDENTS = {"STU001": "Student One", "STU002": "Student Two",
            "STU003": "Student Three", "STU004": "Student Four"}


def make_client(seed=True):
    """A fresh app pointed at a throwaway database."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_api_"))
    state.configure(directory / "api.db")

    from backend.app import app
    client = TestClient(app)

    if seed:
        seed_database(state.get_database())

    return client, directory


def teardown(client, directory):
    client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def seed_database(database):
    """A small known dataset: one completed session, four students."""
    students = StudentRepository(database)
    sessions = SessionRepository(database)
    attendance = AttendanceRepository(database)

    for student_id, name in STUDENTS.items():
        students.upsert(student_id, name)

    end = START + timedelta(hours=1)
    session = AttendanceSession("SES-1", "CLASS-10A", START, end,
                                expected_students=list(STUDENTS))
    session.start()
    session.complete(completed_at=end)
    sessions.save(session)

    plan = [("STU001", "PRESENT", 0.95, 3420.0),
            ("STU002", "LATE", 0.77, 2760.0),
            ("STU003", "PARTIAL", 0.25, 900.0),
            ("STU004", "ABSENT", 0.0, 0.0)]

    for student_id, status, ratio, seconds in plan:
        decision = AttendanceDecision(student_id, "SES-1", status,
                                      "test fixture record", "1.0")
        decision.present_seconds = seconds
        decision.session_seconds = 3600.0
        decision.presence_ratio = ratio
        if seconds:
            decision.first_seen = START
            decision.last_seen = START + timedelta(seconds=seconds)
        attendance.save_decision(decision)

    attendance.save_segments("SES-1", [
        PresenceSegment("STU001", START, START + timedelta(minutes=57), 1),
        PresenceSegment("STU002", START + timedelta(minutes=14), end, 2),
        PresenceSegment("STU003", START, START + timedelta(minutes=15), 3),
    ])


def walk(value, visit, path=""):
    """Visit every key and value in a nested JSON structure."""
    if isinstance(value, dict):
        for key, item in value.items():
            visit(key, item, path + "/" + str(key))
            walk(item, visit, path + "/" + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, visit, path + "[%d]" % index)


# -- health ----------------------------------------------------------------

def test_health():
    client, directory = make_client()
    try:
        response = client.get("/api/health")
        assert response.status_code == 200, "health should return 200"

        body = response.json()
        assert body["status"] == "ok", "status should be ok, got %r" % body["status"]
        assert body["database"] == "ok", "database should be ok"
        assert body["schema"] == "ready", "schema should be ready"

        text = json.dumps(body).lower()
        for banned in ("path", "\\", "/users", "sqlite", "secret"):
            assert banned not in text, (
                "health leaked something it should not: %r" % banned)
        print("PASS  /api/health reports state without revealing configuration")
    finally:
        teardown(client, directory)


def test_health_on_empty_database():
    client, directory = make_client(seed=False)
    try:
        assert client.get("/api/health").json()["status"] == "ok", (
            "an empty but valid database is healthy")
        print("PASS  health is ok on an empty database")
    finally:
        teardown(client, directory)


# -- empty database --------------------------------------------------------

def test_empty_database_endpoints():
    client, directory = make_client(seed=False)
    try:
        sessions = client.get("/api/sessions").json()
        assert sessions["items"] == [] and sessions["total"] == 0, (
            "an empty database should return an empty page")

        students = client.get("/api/students").json()
        assert students["total"] == 0, "no students expected"

        overview = client.get("/api/analytics/overview").json()
        assert overview["has_data"] is False, "has_data should be False"
        assert overview["attendance_rate"] is None, (
            "an empty database must not report 0%% - got %r"
            % overview["attendance_rate"])
        print("PASS  every endpoint handles an empty database without inventing data")
    finally:
        teardown(client, directory)


# -- sessions --------------------------------------------------------------

def test_list_sessions():
    client, directory = make_client()
    try:
        body = client.get("/api/sessions").json()
        assert body["total"] == 1, "one session expected, got %d" % body["total"]

        session = body["items"][0]
        assert session["session_id"] == "SES-1", "wrong session"
        assert session["status"] == "COMPLETED", "wrong status"
        assert session["expected_students"] == 4, (
            "expected student count wrong: %r" % session["expected_students"])
        print("PASS  /api/sessions lists sessions with their expected counts")
    finally:
        teardown(client, directory)


def test_get_session_and_404():
    client, directory = make_client()
    try:
        ok = client.get("/api/sessions/SES-1")
        assert ok.status_code == 200, "existing session should return 200"
        assert ok.json()["expected_students"] == list(STUDENTS), (
            "expected students should be listed")

        missing = client.get("/api/sessions/NOPE")
        assert missing.status_code == 404, (
            "unknown session should be 404, got %d" % missing.status_code)
        assert "not found" in missing.json()["detail"].lower(), "unhelpful detail"
        print("PASS  session detail returns 200 and a clean 404")
    finally:
        teardown(client, directory)


def test_session_attendance_and_filters():
    client, directory = make_client()
    try:
        body = client.get("/api/sessions/SES-1/attendance").json()
        assert body["total"] == 4, "four records expected, got %d" % body["total"]

        statuses = {item["student_id"]: item["status"] for item in body["items"]}
        assert statuses == {"STU001": "PRESENT", "STU002": "LATE",
                            "STU003": "PARTIAL", "STU004": "ABSENT"}, (
            "wrong statuses: %s" % statuses)

        filtered = client.get("/api/sessions/SES-1/attendance?status=LATE").json()
        assert filtered["total"] == 1, "one LATE record expected"
        assert filtered["items"][0]["student_id"] == "STU002", "wrong student"

        print("PASS  attendance list and status filter return correct rows")
    finally:
        teardown(client, directory)


def test_attendance_sorting():
    client, directory = make_client()
    try:
        ascending = client.get(
            "/api/sessions/SES-1/attendance?sort=present_seconds").json()
        values = [item["present_seconds"] for item in ascending["items"]]
        assert values == sorted(values), "ascending sort failed: %s" % values

        descending = client.get(
            "/api/sessions/SES-1/attendance?sort=present_seconds&descending=true").json()
        values = [item["present_seconds"] for item in descending["items"]]
        assert values == sorted(values, reverse=True), (
            "descending sort failed: %s" % values)
        print("PASS  attendance sorting works in both directions")
    finally:
        teardown(client, directory)


def test_invalid_filters_and_sorts():
    client, directory = make_client()
    try:
        cases = [
            ("/api/sessions?status=BANANA", 400, "invalid session status"),
            ("/api/sessions/SES-1/attendance?status=NOPE", 400, "invalid status"),
            ("/api/sessions/SES-1/attendance?sort=; DROP TABLE students",
             400, "an injection attempt in the sort field"),
            ("/api/sessions?limit=0", 422, "limit below range"),
            ("/api/sessions?limit=9999", 422, "limit above range"),
            ("/api/sessions?offset=-1", 422, "negative offset"),
            ("/api/sessions?session_date=notadate", 422, "malformed date"),
        ]
        for url, expected, description in cases:
            response = client.get(url)
            assert response.status_code == expected, (
                "%s should give %d, got %d" % (description, expected,
                                               response.status_code))

        # The injection attempt must not have done anything.
        assert client.get("/api/students").json()["total"] == 4, (
            "the students table was affected by a crafted sort parameter")
        print("PASS  %d invalid queries rejected; injection attempt had no effect"
              % len(cases))
    finally:
        teardown(client, directory)


def test_pagination():
    client, directory = make_client()
    try:
        first = client.get("/api/sessions/SES-1/attendance?limit=2&offset=0").json()
        second = client.get("/api/sessions/SES-1/attendance?limit=2&offset=2").json()

        assert len(first["items"]) == 2, "first page should hold 2"
        assert len(second["items"]) == 2, "second page should hold 2"
        assert first["total"] == second["total"] == 4, "total should stay 4"

        ids = {item["student_id"] for item in first["items"]} | \
              {item["student_id"] for item in second["items"]}
        assert len(ids) == 4, "pages overlapped: %s" % ids
        print("PASS  pagination returns distinct pages with a stable total")
    finally:
        teardown(client, directory)


def test_presence_endpoint():
    client, directory = make_client()
    try:
        body = client.get("/api/sessions/SES-1/presence").json()

        assert len(body["students"]) == 3, (
            "three students had segments, got %d" % len(body["students"]))
        assert body["session_start"], "session window should be reported"

        student = [s for s in body["students"] if s["student_id"] == "STU001"][0]
        assert student["segment_count"] == 1, "one segment expected"
        assert student["segments"][0]["track_id"] == 1, "track id should be present"
        assert "not a student identity" in body["note"], (
            "the response should say what a track id is")

        text = json.dumps(body).lower()
        for banned in ("embedding", "image", "crop", "frame_data"):
            assert banned not in text, "presence leaked %r" % banned
        print("PASS  presence returns time segments only, with track ids labelled")
    finally:
        teardown(client, directory)


# -- students --------------------------------------------------------------

def test_students_endpoints():
    client, directory = make_client()
    try:
        body = client.get("/api/students").json()
        assert body["total"] == 4, "four students expected"

        one = client.get("/api/students/STU001")
        assert one.status_code == 200, "existing student should return 200"
        assert one.json()["name"] == "Student One", "wrong name"

        missing = client.get("/api/students/GHOST")
        assert missing.status_code == 404, "unknown student should be 404"

        history = client.get("/api/students/STU001/attendance").json()
        assert history["total"] == 1, "one attendance record expected"
        assert history["items"][0]["session_date"], "session date should be joined in"
        print("PASS  student list, detail, 404 and history all behave")
    finally:
        teardown(client, directory)


def test_student_search():
    client, directory = make_client()
    try:
        by_id = client.get("/api/students?search=STU002").json()
        assert by_id["total"] == 1, "search by id should find one"

        by_name = client.get("/api/students?search=Three").json()
        assert by_name["total"] == 1, "search by name should find one"
        assert by_name["items"][0]["student_id"] == "STU003", "wrong match"

        none = client.get("/api/students?search=zzzzz").json()
        assert none["total"] == 0, "a non-matching search should return nothing"
        print("PASS  student search matches id and name")
    finally:
        teardown(client, directory)


# -- analytics -------------------------------------------------------------

def test_analytics_endpoints():
    client, directory = make_client()
    try:
        overview = client.get("/api/analytics/overview").json()
        assert overview["has_data"] is True, "seeded data should be detected"
        assert overview["completed_sessions"] == 1, "one completed session"
        assert overview["attendance_rate"] == 0.5, (
            "PRESENT + LATE of 4 expected = 0.5, got %r"
            % overview["attendance_rate"])

        session = client.get("/api/analytics/sessions/SES-1").json()
        assert session["counts"]["PRESENT"] == 1, "one present"
        assert session["satisfying_count"] == 2, "two satisfying"

        student = client.get("/api/analytics/students/STU001").json()
        assert student["attendance_rate"] == 1.0, "STU001 attended the one session"

        klass = client.get("/api/analytics/classes/CLASS-10A").json()
        assert klass["completed_sessions"] == 1, "one completed session for the class"

        print("PASS  overview, session, student and class analytics all correct")
    finally:
        teardown(client, directory)


def test_analytics_404s():
    client, directory = make_client()
    try:
        for url in ("/api/analytics/sessions/NOPE",
                    "/api/analytics/students/GHOST",
                    "/api/analytics/classes/NOCLASS"):
            response = client.get(url)
            assert response.status_code == 404, (
                "%s should be 404, got %d" % (url, response.status_code))
        print("PASS  analytics endpoints return 404 for unknown ids")
    finally:
        teardown(client, directory)


def test_class_and_date_filtering():
    client, directory = make_client()
    try:
        matching = client.get(
            "/api/analytics/overview?class_id=CLASS-10A").json()
        assert matching["completed_sessions"] == 1, "class filter should match"

        other = client.get("/api/analytics/overview?class_id=NOPE").json()
        assert other["completed_sessions"] == 0, "unknown class should match none"
        assert other["attendance_rate"] is None, (
            "a filter matching nothing must give null, not 0.0")

        by_date = client.get(
            "/api/analytics/overview?session_date=2026-03-02").json()
        assert by_date["completed_sessions"] == 1, "date filter should match"

        wrong_date = client.get(
            "/api/analytics/overview?session_date=2020-01-01").json()
        assert wrong_date["completed_sessions"] == 0, "wrong date should match none"
        print("PASS  class and date filters work and report null when empty")
    finally:
        teardown(client, directory)


# -- override --------------------------------------------------------------

def test_override_endpoint():
    client, directory = make_client()
    try:
        response = client.post(
            "/api/sessions/SES-1/attendance/STU004/override",
            json={"status": "PRESENT",
                  "reason": "Student was in an approved alternate classroom",
                  "actor": "teacher:demo"})

        assert response.status_code == 200, (
            "override should succeed, got %d" % response.status_code)

        record = response.json()
        assert record["status"] == "PRESENT", "new status not applied"
        assert record["original_status"] == "ABSENT", (
            "the computed status must be preserved")
        assert record["is_override"] is True, "override flag not set"
        assert record["overridden_by"] == "teacher:demo", "actor not recorded"
        assert record["overridden_at"], "timestamp not recorded"
        print("PASS  override applies and preserves the full audit trail")
    finally:
        teardown(client, directory)


def test_override_validation_and_404():
    client, directory = make_client()
    try:
        bad_status = client.post(
            "/api/sessions/SES-1/attendance/STU001/override",
            json={"status": "PROBABLY", "reason": "because"})
        assert bad_status.status_code == 400, (
            "an invalid status should be 400, got %d" % bad_status.status_code)

        short_reason = client.post(
            "/api/sessions/SES-1/attendance/STU001/override",
            json={"status": "PRESENT", "reason": "x"})
        assert short_reason.status_code == 422, (
            "a too-short reason should be 422, got %d" % short_reason.status_code)

        missing = client.post(
            "/api/sessions/SES-1/attendance/GHOST/override",
            json={"status": "PRESENT", "reason": "valid reason here"})
        assert missing.status_code == 404, (
            "an unknown record should be 404, got %d" % missing.status_code)
        print("PASS  override rejects bad status, missing reason and unknown records")
    finally:
        teardown(client, directory)


def test_clear_override():
    client, directory = make_client()
    try:
        client.post("/api/sessions/SES-1/attendance/STU004/override",
                    json={"status": "PRESENT", "reason": "correcting a mistake"})

        response = client.delete(
            "/api/sessions/SES-1/attendance/STU004/override?policy_version=1.0")
        assert response.status_code == 200, "clearing should succeed"

        record = response.json()
        assert record["status"] == "ABSENT", "the computed status should return"
        assert record["is_override"] is False, "the override flag should clear"
        print("PASS  an override can be undone, restoring the computed status")
    finally:
        teardown(client, directory)


# -- data integrity --------------------------------------------------------

def test_get_requests_never_modify_data():
    """Viewing the dashboard must not change attendance."""
    client, directory = make_client()
    try:
        database = state.get_database()

        def snapshot():
            rows = database.query(
                "SELECT attendance_id, status, updated_at FROM attendance_records "
                "ORDER BY attendance_id")
            return [tuple(row) for row in rows]

        before = snapshot()

        for url in ("/api/health", "/api/classes", "/api/sessions",
                    "/api/sessions/SES-1", "/api/sessions/SES-1/attendance",
                    "/api/sessions/SES-1/presence", "/api/students",
                    "/api/students/STU001", "/api/students/STU001/attendance",
                    "/api/analytics/overview", "/api/analytics/sessions/SES-1",
                    "/api/analytics/students/STU001",
                    "/api/analytics/classes/CLASS-10A",
                    "/api/analytics/attention"):
            assert client.get(url).status_code == 200, "%s failed" % url

        assert snapshot() == before, (
            "GET requests modified attendance records")
        print("PASS  14 GET endpoints changed no attendance row")
    finally:
        teardown(client, directory)


# -- privacy boundary ------------------------------------------------------

def test_no_endpoint_exposes_biometric_or_secret_data():
    """The boundary that matters most in this step."""
    client, directory = make_client()
    try:
        urls = ("/api/health", "/api/classes", "/api/sessions",
                "/api/sessions/SES-1", "/api/sessions/SES-1/attendance",
                "/api/sessions/SES-1/presence", "/api/students",
                "/api/students/STU001", "/api/students/STU001/attendance",
                "/api/analytics/overview", "/api/analytics/sessions/SES-1",
                "/api/analytics/students/STU001",
                "/api/analytics/classes/CLASS-10A",
                "/api/analytics/attention")

        offenders = []

        for url in urls:
            body = client.get(url).json()

            def inspect(key, value, path, url=url):
                lowered = str(key).lower()
                for banned in FORBIDDEN_KEYS:
                    # "first_seen"/"last_seen" are times, not media; and
                    # "segment_count" is a number. Match whole-ish keys only.
                    if banned in lowered and lowered not in (
                            "first_seen", "last_seen"):
                        offenders.append("%s -> %s" % (url, path))

            walk(body, inspect)

            text = json.dumps(body)
            for magic in ("data:image", "base64,", "\\x89PNG", "\\xff\\xd8"):
                assert magic not in text, "%s returned embedded media" % url

        assert not offenders, (
            "endpoints exposed forbidden fields:\n  " + "\n  ".join(offenders))
        print("PASS  %d endpoints checked - no biometric, media, path or secret "
              "field in any response" % len(urls))
    finally:
        teardown(client, directory)


def test_openapi_schema_declares_no_biometric_field():
    """
    Check declared FIELD NAMES, not prose.

    The descriptions legitimately mention embeddings in order to say that none
    are returned. What must not exist is a declared property or parameter that
    could carry one.
    """
    client, directory = make_client()
    try:
        schema = client.get("/openapi.json").json()

        models = schema.get("components", {}).get("schemas", {})

        # Sign-in has to ACCEPT a password. These five request bodies are the
        # only places a password field may be declared, they are named one by
        # one rather than matched by pattern, and none of them is ever used as
        # a response model - a fact the loop below then proves rather than
        # assumes.
        PASSWORD_REQUESTS = ("LoginRequest", "BootstrapRequest",
                             "UserCreateRequest", "UserUpdateRequest",
                             "PasswordChangeRequest")

        response_models = set()
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for response in operation.get("responses", {}).values():
                    content = (response or {}).get("content", {})
                    for media in content.values():
                        reference = (media.get("schema") or {}).get("$ref", "")
                        if reference:
                            response_models.add(reference.rsplit("/", 1)[-1])

        for name in PASSWORD_REQUESTS:
            assert name not in response_models, (
                "%s carries a password and is returned by an endpoint" % name)

        declared = set()
        for name, model in models.items():
            properties = set(model.get("properties", {}).keys())
            if name in PASSWORD_REQUESTS:
                # Still checked against every OTHER forbidden key, and a
                # password HASH is never allowed anywhere.
                assert "password_hash" not in properties, (
                    "%s declares a password hash" % name)
                properties -= {"password", "current_password", "new_password"}
            declared.update(properties)

        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for parameter in operation.get("parameters", []):
                    declared.add(parameter.get("name", ""))

        # Two names contain a banned word and carry no biometric data.
        #
        # first_seen / last_seen are attendance timestamps.
        #
        # photo_name is the FILENAME of a member of staff's personnel
        # photograph - a string like "photo-3f2a....jpg", never bytes, never a
        # path, and never a student. Nothing computes an embedding from the
        # file it names; backend/services/staff_files_service.py imports
        # nothing from ai/ so that it cannot start. It is listed by its exact
        # name rather than by loosening FORBIDDEN_KEYS, so any OTHER field
        # that acquires one of these words still fails this test.
        ALLOWED_DESPITE_NAME = ("first_seen", "last_seen", "photo_name")

        offenders = [name for name in declared
                     if any(banned in str(name).lower()
                            for banned in FORBIDDEN_KEYS)
                     and str(name).lower() not in ALLOWED_DESPITE_NAME]

        assert not offenders, (
            "the API declares forbidden field(s): %s" % offenders)
        assert declared, "no schema fields were found - the check is not working"

        # Belt and braces: a hash must not appear in ANY declared field, in
        # any model, request or response.
        every_field = {field
                       for model in models.values()
                       for field in model.get("properties", {})}
        assert not [field for field in every_field if "hash" in field.lower()], (
            "a password hash is declared somewhere in the API")

        print("PASS  %d declared API fields, none biometric, media or secret "
              "(passwords only on the %d sign-in request bodies, never in a "
              "response)" % (len(declared), len(PASSWORD_REQUESTS)))
    finally:
        teardown(client, directory)


def test_server_errors_do_not_leak_internals():
    client, directory = make_client()
    try:
        response = client.get("/api/sessions/%s" % ("x" * 500))
        assert response.status_code in (404, 422), (
            "an over-long id should be rejected cleanly, got %d"
            % response.status_code)

        body = json.dumps(response.json()).lower()
        for leak in ("traceback", "select ", "sqlite", "c:\\", "/users/"):
            assert leak not in body, "the error response leaked %r" % leak
        print("PASS  error responses contain no traceback, SQL or file path")
    finally:
        teardown(client, directory)


def test_tests_never_touch_the_real_database():
    client, directory = make_client()
    try:
        assert state.current_path() != DEFAULT_DATABASE_PATH, (
            "the test client is pointed at the real database")
        assert str(directory) in str(state.current_path()), (
            "the test database is outside its temporary directory")
        print("PASS  API tests run against an isolated temporary database")
    finally:
        teardown(client, directory)


# -- frontend --------------------------------------------------------------

def test_dashboard_page_is_served():
    client, directory = make_client()
    try:
        page = client.get("/")
        assert page.status_code == 200, "the dashboard should be served at /"

        html = page.text
        assert "ClassVision AI" in html, "the page title is missing"
        assert 'id="main"' in html, "the main landmark is missing"
        assert "skip-link" in html, "the skip link is missing"

        # No external requests: the dashboard must work offline.
        for external in ("http://", "https://cdn", "unpkg.com", "jsdelivr",
                         "googleapis", "cdnjs"):
            assert external not in html.replace(
                'http://www.w3.org/2000/svg', ''), (
                "the page references an external resource: %r" % external)
        print("PASS  the dashboard is served and loads no external resource")
    finally:
        teardown(client, directory)


def test_static_assets_are_served():
    client, directory = make_client()
    try:
        css = client.get("/static/styles.css")
        assert css.status_code == 200, "stylesheet should be served"

        script = client.get("/static/app.js")
        assert script.status_code == 200, "script should be served"

        # Colour must never be the only signal.
        assert "STATUS_SYMBOL" in script.text, (
            "statuses should carry a symbol, not colour alone")
        # And the script must not call out to anything.
        for external in ("https://", "cdn.", "unpkg", "jsdelivr"):
            assert external not in script.text.replace(
                "http://www.w3.org/2000/svg", ""), (
                "app.js references an external resource: %r" % external)
        print("PASS  CSS and JS are served locally with symbol-based statuses")
    finally:
        teardown(client, directory)


def main():
    enable_safe_console()

    tests = [
        test_health,
        test_health_on_empty_database,
        test_empty_database_endpoints,
        test_list_sessions,
        test_get_session_and_404,
        test_session_attendance_and_filters,
        test_attendance_sorting,
        test_invalid_filters_and_sorts,
        test_pagination,
        test_presence_endpoint,
        test_students_endpoints,
        test_student_search,
        test_analytics_endpoints,
        test_analytics_404s,
        test_class_and_date_filtering,
        test_override_endpoint,
        test_override_validation_and_404,
        test_clear_override,
        test_get_requests_never_modify_data,
        test_no_endpoint_exposes_biometric_or_secret_data,
        test_openapi_schema_declares_no_biometric_field,
        test_server_errors_do_not_leak_internals,
        test_tests_never_touch_the_real_database,
        test_dashboard_page_is_served,
        test_static_assets_are_served,
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
