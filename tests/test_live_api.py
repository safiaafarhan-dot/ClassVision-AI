"""
Live session API tests (Step 8).

NO WEBCAM AND NO MODEL ARE NEEDED. The manager's camera and pipeline factories
are replaced with fakes, so the whole lifecycle - start, status, presence,
stop, finalise - runs in CI on a machine with no camera attached.

Every test runs against its OWN temporary SQLite file. The real
data/classvision.db is never opened.

WHAT THIS VERIFIES
    The lifecycle and its refusals, that finalisation goes through the Step 6
    rule engine, that a manual correction survives it, and that no live
    response can carry a biometric field, an image, a path or a secret.

WHAT THIS DOES NOT VERIFY
    Recognition accuracy, tracking under occlusion, or real camera behaviour.
    Attendance accuracy has NOT been validated on a real classroom population.

Run with:
    python tests/test_live_api.py
"""

import json
import re
import shutil
import sys
import tempfile
import threading
import time
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

from attendance_session import AttendanceSession  # noqa: E402
from live_camera import CameraError  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import live_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

from test_live_session import (  # noqa: E402
    FakeCamera,
    FakeFrame,
    build_pipeline,
)

# The session window is placed around "now" so a live run genuinely falls
# inside it. Attendance is only ever credited within the session window.
START = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
END = START + timedelta(hours=1)

SESSION_ID = "SES-LIVE-TEST"
CLASS_ID = "CLASS-LIVE"

STUDENTS = {"STU001": "Student One", "STU002": "Student Two"}

# Anything resembling biometric data, media, a path or a secret. "frame" is
# absent on purpose and handled separately: a COUNT of processed captures is
# not a capture, so the check below tests what a field could CARRY rather than
# banning an English word.
FORBIDDEN_KEYS = (
    "embedding", "embeddings", "vector", "vectors", "descriptor", "feature",
    "face", "image", "photo", "crop", "video", "thumbnail", "similarity",
    "score", "password", "secret", "token", "credential", "api_key",
    "db_path", "database_path", "filepath", "file_path", "connection_string",
    "model_path", "onnx",
)

LIVE_URLS = ("/api/live/status", "/api/live/presence")


# -- harness ----------------------------------------------------------------

def seed(database, status="SCHEDULED"):
    students = StudentRepository(database)
    sessions = SessionRepository(database)

    for student_id, name in STUDENTS.items():
        students.upsert(student_id, name)

    session = AttendanceSession(SESSION_ID, CLASS_ID, START, END,
                                policy_version="1.0",
                                expected_students=list(STUDENTS))
    if status != "SCHEDULED":
        session.start()
    if status == "COMPLETED":
        session.complete(completed_at=END)

    sessions.save(session)
    return session


def make_client(session_status="SCHEDULED", script=None, by_x=None,
                camera=None, camera_error=None, seed_session=True):
    """
    A fresh app, a throwaway database, and a manager wired to fakes.

    The manager is process-wide in production; here it is replaced per test so
    one test's runtime can never leak into the next.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_live_"))
    state.configure(directory / "live.db")

    if seed_session:
        seed(state.get_database(), session_status)

    holder = {"camera": camera or FakeCamera(delay=0.002), "pipeline": None}

    def camera_factory(config):
        if camera_error:
            raise CameraError(camera_error)
        holder["camera"].open()
        return holder["camera"]

    def pipeline_factory(session_start, session_end, student_names):
        pipeline, _, _ = build_pipeline(
            script or [[(100, 100, 60, 60, 0.95)]],
            by_x if by_x is not None else {100: ("STU001", "Student One")},
            names=student_names, start=session_start, end=session_end)
        holder["pipeline"] = pipeline
        return pipeline

    manager = live_service.LiveSessionManager(camera_factory=camera_factory,
                                              pipeline_factory=pipeline_factory)
    live_service.reset_live_manager(manager)

    from backend.app import app
    return TestClient(app), directory, holder


def teardown(client, directory):
    live_service.reset_live_manager(None)
    client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def wait_for_frames(holder, count, timeout=5.0):
    """Let the worker actually process some frames before asserting on it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pipeline = holder.get("pipeline")
        if pipeline is not None and pipeline.frame_index + 1 >= count:
            return True
        time.sleep(0.01)
    return False


def walk(value, visit, trail="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            visit(key, "%s.%s" % (trail, key))
            walk(item, visit, "%s.%s" % (trail, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, visit, "%s[%d]" % (trail, index))


# -- lifecycle --------------------------------------------------------------

def test_status_answers_before_anything_has_started():
    client, directory, _ = make_client()
    try:
        body = client.get("/api/live/status").json()
        assert body["state"] == "IDLE", "a fresh runtime should be IDLE"
        assert body["camera"] == "disconnected"
        assert body["session_id"] is None
        assert body["fps"] == 0.0 and body["processed_count"] == 0
        print("PASS  status answers IDLE before anything starts - not an error")
    finally:
        teardown(client, directory)


def test_start_status_presence_stop_is_a_complete_lifecycle():
    client, directory, holder = make_client()
    try:
        started = client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "RUNNING"
        assert started.json()["session_id"] == SESSION_ID

        assert wait_for_frames(holder, 20), "the worker never processed frames"

        status = client.get("/api/live/status").json()
        assert status["state"] == "RUNNING"
        assert status["camera"] == "connected"
        assert status["processed_count"] > 0
        assert status["fps"] > 0

        presence = client.get("/api/live/presence").json()
        assert presence["state"] == "RUNNING"
        assert any(row["student_id"] == "STU001"
                   for row in presence["students"]), (
            "the verified student never reached the presence view")

        stopped = client.post("/api/live/stop")
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        assert body["finalized"] is True
        assert body["session_status"] == "COMPLETED"
        assert body["already_finalized"] is False

        final = client.get("/api/live/status").json()
        assert final["state"] == "COMPLETED"
        assert final["camera"] == "released", (
            "the camera was not released, it is %s" % final["camera"])
        assert holder["camera"].released >= 1

        print("PASS  start -> status -> presence -> stop, %d captures, camera "
              "released" % body["processed_count"])
    finally:
        teardown(client, directory)


def test_the_api_stays_responsive_while_the_camera_runs():
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        wait_for_frames(holder, 10)

        started = time.time()
        codes = [client.get("/api/live/status").status_code for _ in range(20)]
        codes.append(client.get("/api/health").status_code)
        codes.append(client.get("/api/sessions").status_code)
        elapsed = time.time() - started

        assert set(codes) == {200}, "a request failed while the camera ran"
        assert elapsed < 5.0, (
            "22 requests took %.2fs - the camera loop is blocking the API"
            % elapsed)

        client.post("/api/live/stop")
        print("PASS  22 requests answered in %.0f ms while the camera ran"
              % (elapsed * 1000))
    finally:
        teardown(client, directory)


def test_a_second_start_is_refused_with_a_conflict():
    client, directory, holder = make_client()
    try:
        assert client.post("/api/live/start",
                           json={"session_id": SESSION_ID}).status_code == 200
        wait_for_frames(holder, 5)

        again = client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert again.status_code == 409, (
            "a second start returned %d instead of 409" % again.status_code)
        assert "already" in again.json()["detail"].lower()

        status = client.get("/api/live/status").json()
        assert status["state"] == "RUNNING", (
            "the refused start disturbed the running session")

        client.post("/api/live/stop")
        print("PASS  a second start is refused with 409 and the first keeps "
              "running")
    finally:
        teardown(client, directory)


def test_stopping_twice_is_safe_and_writes_nothing_twice():
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        wait_for_frames(holder, 20)

        first = client.post("/api/live/stop").json()
        assert first["already_finalized"] is False

        attendance = AttendanceRepository(state.get_database())
        after_first = attendance.count_for_session(SESSION_ID)
        rows_first = {row["student_id"]: row["status"]
                      for row in attendance.list_for_session(SESSION_ID)}

        second = client.post("/api/live/stop")
        assert second.status_code == 200, (
            "a second stop returned %d" % second.status_code)
        body = second.json()
        assert body["already_finalized"] is True
        assert body["session_id"] == first["session_id"]

        rows_second = {row["student_id"]: row["status"]
                       for row in attendance.list_for_session(SESSION_ID)}
        assert attendance.count_for_session(SESSION_ID) == after_first, (
            "stopping twice created extra attendance rows")
        assert rows_first == rows_second, "stopping twice changed a result"

        print("PASS  a second stop is idempotent - %d records, unchanged"
              % after_first)
    finally:
        teardown(client, directory)


def test_stopping_when_nothing_is_running_is_refused_cleanly():
    client, directory, _ = make_client()
    try:
        response = client.post("/api/live/stop")
        assert response.status_code == 409
        assert "no live session" in response.json()["detail"].lower()

        body = json.dumps(response.json()).lower()
        for leak in ("traceback", "sqlite", "c:\\", "/users/"):
            assert leak not in body, "the refusal leaked %r" % leak

        print("PASS  stopping nothing returns 409 with a plain explanation")
    finally:
        teardown(client, directory)


# -- refusals ---------------------------------------------------------------

def test_an_unknown_session_is_a_404_and_starts_nothing():
    client, directory, holder = make_client()
    try:
        response = client.post("/api/live/start",
                               json={"session_id": "NO-SUCH-SESSION"})
        assert response.status_code == 404

        status = client.get("/api/live/status").json()
        assert status["state"] == "IDLE", (
            "a bad session left the runtime in %s" % status["state"])
        assert holder["camera"].opened is False, (
            "the camera was opened for a session that does not exist")

        print("PASS  an unknown session is 404, the camera is never opened")
    finally:
        teardown(client, directory)


def test_a_completed_session_cannot_be_run_live():
    client, directory, _ = make_client(session_status="COMPLETED")
    try:
        response = client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert response.status_code == 409, (
            "a COMPLETED session returned %d" % response.status_code)
        assert "COMPLETED" in response.json()["detail"]
        print("PASS  a COMPLETED session is refused with 409")
    finally:
        teardown(client, directory)


def test_an_unavailable_camera_does_not_leave_the_session_running():
    client, directory, _ = make_client(
        camera_error="Camera 3 could not be opened. It may be in use by "
                     "another application.")
    try:
        response = client.post("/api/live/start",
                               json={"session_id": SESSION_ID,
                                     "camera_index": 3})
        assert response.status_code == 503, (
            "a missing camera returned %d" % response.status_code)

        detail = response.json()["detail"]
        assert "Camera 3" in detail
        for leak in ("Traceback", "File \"", "sqlite", "C:\\"):
            assert leak not in detail, "the camera error leaked %r" % leak

        status = client.get("/api/live/status").json()
        assert status["state"] == "FAILED", (
            "a missing camera left the runtime %s" % status["state"])

        # The lesson must not have been marked ACTIVE by a start that failed.
        session = SessionRepository(state.get_database()).get(SESSION_ID)
        assert session["status"] == "SCHEDULED", (
            "a failed start moved the stored session to %s" % session["status"])

        assert AttendanceRepository(
            state.get_database()).count_for_session(SESSION_ID) == 0, (
            "a failed start wrote attendance")

        print("PASS  an unavailable camera is 503, FAILED, and changes nothing "
              "in the database")
    finally:
        teardown(client, directory)


def test_a_camera_that_dies_mid_session_fails_and_releases():
    camera = FakeCamera(fail_after=10, delay=0.002)
    client, directory, holder = make_client(camera=camera)
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if client.get("/api/live/status").json()["state"] == "FAILED":
                break
            time.sleep(0.02)

        status = client.get("/api/live/status").json()
        assert status["state"] == "FAILED", (
            "a dead camera left the runtime %s" % status["state"])
        assert status["camera"] == "released"
        assert camera.released >= 1
        assert "Traceback" not in (status["error"] or "")

        # The teacher can still stop, and the evidence gathered is not lost.
        stopped = client.post("/api/live/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["finalized"] is True

        print("PASS  a camera that dies mid-session fails, releases, and can "
              "still be finalised")
    finally:
        teardown(client, directory)


def test_a_session_with_no_faces_finalises_without_inventing_attendance():
    client, directory, holder = make_client(script=[[]], by_x={})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 20)

        presence = client.get("/api/live/presence").json()
        assert presence["students"] == []
        assert presence["unknown_tracks"] == 0

        body = client.post("/api/live/stop").json()
        assert body["segments_saved"] == 0

        records = AttendanceRepository(
            state.get_database()).list_for_session(SESSION_ID)
        assert len(records) == len(STUDENTS), (
            "expected one record per expected student, got %d" % len(records))
        assert {row["status"] for row in records} == {"ABSENT"}, (
            "an empty session produced %s"
            % {row["status"] for row in records})

        print("PASS  an empty session finalises to ABSENT for expected "
              "students only - nothing invented")
    finally:
        teardown(client, directory)


def test_an_unknown_person_never_becomes_attendance():
    """The hard rule, at the API boundary."""
    client, directory, holder = make_client(by_x={})   # nobody matches
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 30)

        presence = client.get("/api/live/presence").json()
        assert presence["students"] == [], (
            "an unverified person appeared as a student")
        assert presence["unknown_tracks"] >= 1, (
            "the unverified person should be visible as a count")

        body = client.post("/api/live/stop").json()
        assert body["segments_saved"] == 0, (
            "an unverified person produced presence evidence")

        attendance = AttendanceRepository(state.get_database())
        records = attendance.list_for_session(SESSION_ID)
        assert {row["student_id"] for row in records} == set(STUDENTS), (
            "somebody who is not an expected student got a record")
        assert all(row["present_seconds"] == 0 for row in records), (
            "an unverified person contributed presence to somebody")

        print("PASS  an unverified person is a count only - 0 segments, 0 "
              "seconds credited")
    finally:
        teardown(client, directory)


# -- finalisation -----------------------------------------------------------

def test_finalisation_runs_the_step_6_rule_engine_and_stores_evidence():
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 60)

        body = client.post("/api/live/stop").json()

        assert body["policy_version"] == "1.0"
        assert body["students_evaluated"] == len(STUDENTS)
        assert body["segments_saved"] >= 1, "no presence evidence was stored"

        database = state.get_database()
        attendance = AttendanceRepository(database)

        segments = attendance.get_segments(SESSION_ID)
        assert segments, "the presence segments were not persisted"
        assert all(segment["student_id"] in STUDENTS for segment in segments)

        records = {row["student_id"]: row
                   for row in attendance.list_for_session(SESSION_ID)}

        seen = records["STU001"]
        assert seen["present_seconds"] > 0, "the verified student got no time"
        assert seen["reason"], "the decision carries no explanation"
        assert seen["policy_version"] == "1.0"

        unseen = records["STU002"]
        assert unseen["status"] == "ABSENT", (
            "a student who never appeared in a COMPLETED session is %s"
            % unseen["status"])

        session = SessionRepository(database).get(SESSION_ID)
        assert session["status"] == "COMPLETED"
        assert session["completed_at"], "the session has no completion time"

        print("PASS  finalisation stored %d segments and %d decisions through "
              "the Step 6 engine"
              % (len(segments), len(records)))
    finally:
        teardown(client, directory)


def test_nobody_is_marked_absent_before_the_session_is_finalised():
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 20)

        attendance = AttendanceRepository(state.get_database())
        assert attendance.count_for_session(SESSION_ID) == 0, (
            "attendance was written while the session was still running")

        presence = client.get("/api/live/presence").json()
        states = {row["presence_state"] for row in presence["students"]}
        assert "ABSENT" not in states, (
            "the live view reported somebody as absent")
        assert "absent" in presence["note"].lower(), (
            "the response should say why nobody can be absent yet")

        session = SessionRepository(state.get_database()).get(SESSION_ID)
        assert session["status"] == "ACTIVE", (
            "a running session should be ACTIVE, it is %s" % session["status"])

        client.post("/api/live/stop")
        print("PASS  no attendance row exists until the session is stopped")
    finally:
        teardown(client, directory)


def test_a_manual_correction_survives_finalisation():
    """
    A teacher's correction outranks a recomputation. Step 6 guarantees it;
    this proves the live path does not go around it.
    """
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 30)
        client.post("/api/live/stop")

        override = client.post(
            "/api/sessions/%s/attendance/%s/override" % (SESSION_ID, "STU002"),
            json={"status": "PRESENT",
                  "reason": "Student was in an approved alternate room",
                  "actor": "Test Teacher"})
        assert override.status_code == 200, override.text
        assert override.json()["is_override"] is True
        assert override.json()["original_status"] == "ABSENT"

        # A second stop must change nothing at all.
        again = client.post("/api/live/stop").json()
        assert again["already_finalized"] is True

        # And the write path itself must preserve the correction. This reaches
        # past the idempotency guard deliberately, so the test proves the
        # WRITE is safe rather than merely that stop() refuses to run twice.
        live_service.get_live_manager()._finalize(state.get_database())

        record = AttendanceRepository(state.get_database()).get(
            SESSION_ID, "STU002")
        assert record["status"] == "PRESENT", (
            "re-finalisation overwrote the correction with %s"
            % record["status"])
        assert record["is_override"] == 1
        assert record["override_reason"].startswith("Student was in an")

        print("PASS  a manual correction survives a second finalisation, with "
              "its audit trail intact")
    finally:
        teardown(client, directory)


def test_finalisation_keeps_the_sessions_policy_version():
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 20)
        body = client.post("/api/live/stop").json()

        stored = SessionRepository(state.get_database()).get(SESSION_ID)
        assert body["policy_version"] == stored["policy_version"], (
            "finalisation used policy %s for a session recorded under %s"
            % (body["policy_version"], stored["policy_version"]))

        versions = AttendanceRepository(
            state.get_database()).policy_versions_for_session(SESSION_ID)
        assert versions == [stored["policy_version"]]

        print("PASS  attendance is written under the session's own policy "
              "version (%s)" % body["policy_version"])
    finally:
        teardown(client, directory)


# -- validation and privacy -------------------------------------------------

def test_start_rejects_a_malformed_request():
    client, directory, _ = make_client()
    try:
        cases = [
            ({}, "no session id"),
            ({"session_id": ""}, "an empty session id"),
            ({"session_id": SESSION_ID, "camera_index": -1}, "a negative camera"),
            ({"session_id": SESSION_ID, "camera_index": 9999}, "an absurd camera"),
            ({"session_id": "x" * 500}, "an over-long session id"),
            ({"session_id": SESSION_ID, "width": 10}, "an impossible width"),
        ]

        for payload, description in cases:
            response = client.post("/api/live/start", json=payload)
            assert response.status_code in (400, 404, 422), (
                "%s returned %d" % (description, response.status_code))

        assert client.get("/api/live/status").json()["state"] == "IDLE", (
            "a rejected request still started something")
        print("PASS  %d malformed start requests rejected, nothing started"
              % len(cases))
    finally:
        teardown(client, directory)


def test_live_responses_carry_no_biometric_media_path_or_secret():
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 30)

        bodies = {url: client.get(url).json() for url in LIVE_URLS}
        bodies["/api/live/stop"] = client.post("/api/live/stop").json()

        offenders = []
        for url, body in bodies.items():
            def inspect(key, trail, url=url):
                lowered = key.lower()
                if lowered in ("first_seen", "last_seen"):
                    return
                for banned in FORBIDDEN_KEYS:
                    if banned in lowered:
                        offenders.append("%s -> %s" % (url, trail))

            walk(body, inspect)

            text = json.dumps(body)
            for magic in ("data:image", "base64,", "\\x89PNG", "\\xff\\xd8"):
                assert magic not in text, "%s returned embedded media" % url

        assert not offenders, (
            "live endpoints exposed forbidden fields:\n  "
            + "\n  ".join(offenders))

        print("PASS  %d live responses checked - no biometric, media, path or "
              "secret field" % len(bodies))
    finally:
        teardown(client, directory)


def test_live_responses_carry_no_capture_only_counts():
    """
    "frame" is a legitimate English word and a forbidden kind of data.

    Rather than banning the word, this asserts the stronger property: every
    value in a live response is a plain scalar, and every capture-related
    field is a NUMBER. A picture cannot hide in an integer.
    """
    client, directory, holder = make_client()
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 20)

        status = client.get("/api/live/status").json()
        presence = client.get("/api/live/presence").json()

        for key, value in status.items():
            assert isinstance(value, (str, int, float, bool, type(None))), (
                "status field %s is a %s, which could carry anything"
                % (key, type(value).__name__))

        for key in ("processed_count", "dropped_count", "recognition_calls"):
            assert isinstance(status[key], int), (
                "%s is not a plain count" % key)

        for row in presence["students"]:
            for key, value in row.items():
                assert isinstance(value, (str, int, float, bool, type(None))), (
                    "presence field %s is a %s" % (key, type(value).__name__))

        text = json.dumps(status) + json.dumps(presence)
        assert len(text) < 20000, (
            "a live response is %d bytes - far too large for counters"
            % len(text))

        client.post("/api/live/stop")
        print("PASS  every live field is a scalar and every capture field is a "
              "count (%d bytes total)" % len(text))
    finally:
        teardown(client, directory)


def test_unknown_people_are_never_named_or_guessed():
    client, directory, holder = make_client(
        script=[[(100, 100, 60, 60, 0.95), (300, 120, 60, 60, 0.95)]],
        by_x={100: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for_frames(holder, 30)

        presence = client.get("/api/live/presence").json()
        named = {row["student_id"] for row in presence["students"]}

        assert named == {"STU001"}, (
            "the presence view named %s - the second person is not enrolled"
            % named)
        assert presence["unknown_tracks"] == 1, (
            "the unverified person should be counted once, got %d"
            % presence["unknown_tracks"])

        text = json.dumps(presence)
        assert "STU002" not in text, (
            "the unverified person was guessed to be a known student")

        client.post("/api/live/stop")
        print("PASS  one verified student named, one unverified person counted "
              "and never guessed")
    finally:
        teardown(client, directory)


def test_the_openapi_document_declares_the_live_operations_safely():
    client, directory, _ = make_client()
    try:
        schema = client.get("/openapi.json").json()

        for path in ("/api/live/start", "/api/live/status",
                     "/api/live/presence", "/api/live/stop"):
            assert path in schema["paths"], "%s is not documented" % path

        declared = set()
        for name, model in schema["components"]["schemas"].items():
            if name.startswith("Live"):
                declared.update(model.get("properties", {}).keys())

        assert declared, "no live schema fields were found"

        offenders = [field for field in declared
                     if any(banned in field.lower()
                            for banned in FORBIDDEN_KEYS)
                     and field.lower() not in ("first_seen", "last_seen")]
        assert not offenders, "the live API declares %s" % offenders

        print("PASS  4 live operations documented, %d declared fields, none "
              "biometric" % len(declared))
    finally:
        teardown(client, directory)


def test_tests_never_touch_the_real_database():
    from database import DEFAULT_DATABASE_PATH

    client, directory, _ = make_client()
    try:
        assert state.get_database().path != DEFAULT_DATABASE_PATH
        assert str(directory) in str(state.get_database().path)
        print("PASS  live API tests run against an isolated temporary database")
    finally:
        teardown(client, directory)


# -- the live dashboard -----------------------------------------------------
#
# Static checks, in the style of the Step 7 frontend suite: the served HTML,
# CSS and JavaScript are inspected directly. No browser is driven, and that
# limit is stated in the README rather than glossed over.

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_the_dashboard_has_a_live_view_with_labelled_controls():
    assert 'data-view="live"' in HTML, "there is no Live tab"
    assert 'id="view-live"' in HTML, "there is no live view"
    assert 'id="live-heading"' in HTML, "the live view has no heading"

    for control in ("live-session", "live-camera"):
        assert 'for="%s"' % control in HTML, (
            "the %s control has no label" % control)

    for button in ("live-start", "live-stop"):
        assert 'id="%s"' % button in HTML, "the %s button is missing" % button

    print("PASS  the live view exists with labelled session and camera "
          "controls")


def test_the_live_view_polls_for_state_and_shows_only_the_server_s_own_view():
    """
    Step 8 banned every picture from this dashboard. That has CHANGED, once,
    deliberately, and this test records exactly how far.

    The classroom view was added because a teacher running a live lesson has
    to be able to see the room - who is at the back, whether the camera has
    drifted, whether half the class is out of shot. None of that is knowable
    from counters, and none of it is knowable from a grid of face crops.

    What that change did NOT license, and what is still asserted below:

      - the browser never opens a camera of its own (getUserMedia);
      - there is no canvas, so the page cannot lift pixels out of the stream
        and post them somewhere;
      - the only image source is this server's own overlay endpoint - no
        data: URI, no external host, no second stream;
      - STATE still arrives as polled JSON, not over a socket;
      - the page still uploads nothing.

    What is served is the COMPLETE annotated frame. The aligned crops the
    recogniser works on never leave the process.
    """
    assert "setInterval" in JS, "the live view does not poll"
    assert "LIVE_POLL_MS" in JS, "no polling interval is declared"
    assert "/api/live/status" in JS and "/api/live/presence" in JS

    # The browser must never touch a camera, hold a pixel buffer, or push
    # bytes anywhere. None of these became acceptable.
    #
    # Scoped to the LIVE section of each file rather than to the whole of it.
    # That is not a loosening: the ban is about what the classroom view may
    # do with a student's face, and it is enforced here exactly as before
    # over exactly that code. Elsewhere in the dashboard a camera is now used
    # for something categorically different - a member of staff choosing to
    # have their ID photograph taken - and a file-wide string search cannot
    # tell the two apart. The student-enrollment ban in
    # tests/test_student_enrollment.py is scoped the same way and is the one
    # that guards a student's face.
    live_js = JS[JS.index("live classroom (Step 8)"):
                 JS.index("schedule a session")]
    live_html = HTML[HTML.index('id="view-live"'):
                     HTML.index('id="view-cameras"')]

    for forbidden in ("getUserMedia", "<canvas", "MediaSource",
                      "MediaRecorder", "EventSource", "WebSocket",
                      "FormData", "FileReader", "toDataURL", "<video"):
        assert forbidden not in live_html and forbidden not in live_js, (
            "the live classroom view uses %s" % forbidden)

    # Exactly one kind of image, and it comes from this server.
    import re

    # Built from character classes rather than inline quotes, so the pattern
    # stays readable and does not need escaping inside its own literal.
    QUOTE = "[\"']"
    sources = re.findall("src\\s*=\\s*%s([^\"']*)%s" % (QUOTE, QUOTE), HTML)
    sources += re.findall("\\.src\\s*=\\s*\"([^\"]*)\"", JS)

    for source in sources:
        if not source or source.startswith("/static/"):
            continue
        assert source.startswith("/api/"), (
            "an image or script is loaded from %r, which is not this server"
            % source)

    assert "data:image" not in HTML and "data:image" not in JS, (
        "an image is embedded in the page rather than served live")

    # The endpoints that exist, named, so a THIRD one cannot appear quietly.
    assert "/api/live/stream.mjpg" in JS
    assert JS.count("/api/live/stream.mjpg") == 1, (
        "the classroom stream is referenced more than once - there should be "
        "exactly one place that opens it")

    print("PASS  the live view polls JSON for state and shows only this "
          "server's own complete classroom frame")


def test_the_live_view_never_calls_an_unverified_person_absent():
    live_js = JS[JS.index("live classroom (Step 8)"):
                 JS.index("schedule a session")]

    assert "unverified" in live_js.lower(), (
        "the live view does not distinguish unverified people")
    assert "ABSENT" not in live_js, (
        "the live rendering mentions ABSENT, which cannot be known while a "
        "session is still running")

    assert "never named" in HTML, (
        "the page does not say that unverified people are never named")
    assert "not visible right now does not mean absent" in HTML.lower(), (
        "the page does not warn against reading absence into the live view")

    print("PASS  the live view counts unverified people and never calls "
          "anybody absent")


def test_every_live_state_renders_as_a_symbol_and_text():
    for status in ("IDLE", "STARTING", "RUNNING", "STOPPING", "FAILED",
                   "RECOGNIZED", "TEMPORARILY_LOST", "SEEN_EARLIER"):
        assert re.search(r"%s:\s*\"" % status, JS), (
            "live state %s has no symbol, so it would rely on colour alone"
            % status)

    for state in ("RECOGNIZED", "TEMPORARILY_LOST", "SEEN_EARLIER"):
        assert ".pill-%s" % state in CSS, (
            "live state %s has no pill style" % state)

    print("PASS  all 8 live states render as a symbol plus their text")


def test_the_live_section_adds_no_animation_or_external_request():
    """
    The live view itself stays still, and nothing is fetched from anywhere.

    The blanket "no animation in the dashboard" assertion that used to be
    here was dropped when the project asked for a designed interface. What
    replaced it is narrower and stricter where it counts: this section must
    not animate, and tests/test_dashboard_frontend.py asserts that every
    animation elsewhere is switched off by prefers-reduced-motion.

    A moving classroom view would be actively bad - a teacher is watching it
    to judge whether the camera has drifted.
    """
    live_js = JS[JS.index("live classroom (Step 8)"):
                 JS.index("schedule a session")]
    assert "requestAnimationFrame" not in live_js, (
        "the live view animates; it is watched to judge a real camera")

    combined = (HTML + CSS + JS).replace("http://www.w3.org/2000/svg", "")
    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr"):
        assert external not in combined, (
            "the dashboard now references %r" % external)

    total = len(HTML) + len(CSS) + len(JS)
    # A ceiling, not a target. It catches a framework or a bundled library
    # arriving by the back door; the "no external request" checks are what
    # actually enforce that. Raised from 120 KB when the dashboard gained a
    # sign-in screen, an administration view and the full student registry
    # table, and again for the visual redesign and the student portal - all
    # still hand-written HTML, CSS and JavaScript, no build step.
    # See test_dashboard_frontend.test_frontend_is_lightweight for why
    # this ceiling moves and what it is actually guarding against.
    assert total < 620_000, "the frontend has grown to %d bytes" % total

    print("PASS  the live view adds no animation and no external request "
          "(%.1f KB total)" % (total / 1024.0))


def test_the_served_dashboard_matches_its_source_after_the_live_changes():
    client, directory, _ = make_client()
    try:
        assert client.get("/").text == HTML, (
            "the served page differs from the source file")
        assert client.get("/static/app.js").status_code == 200
        print("PASS  the dashboard is served unchanged, with the live view in "
              "it")
    finally:
        teardown(client, directory)


def main():
    enable_safe_console()

    tests = [
        test_status_answers_before_anything_has_started,
        test_start_status_presence_stop_is_a_complete_lifecycle,
        test_the_api_stays_responsive_while_the_camera_runs,
        test_a_second_start_is_refused_with_a_conflict,
        test_stopping_twice_is_safe_and_writes_nothing_twice,
        test_stopping_when_nothing_is_running_is_refused_cleanly,
        test_an_unknown_session_is_a_404_and_starts_nothing,
        test_a_completed_session_cannot_be_run_live,
        test_an_unavailable_camera_does_not_leave_the_session_running,
        test_a_camera_that_dies_mid_session_fails_and_releases,
        test_a_session_with_no_faces_finalises_without_inventing_attendance,
        test_an_unknown_person_never_becomes_attendance,
        test_finalisation_runs_the_step_6_rule_engine_and_stores_evidence,
        test_nobody_is_marked_absent_before_the_session_is_finalised,
        test_a_manual_correction_survives_finalisation,
        test_finalisation_keeps_the_sessions_policy_version,
        test_start_rejects_a_malformed_request,
        test_live_responses_carry_no_biometric_media_path_or_secret,
        test_live_responses_carry_no_capture_only_counts,
        test_unknown_people_are_never_named_or_guessed,
        test_the_openapi_document_declares_the_live_operations_safely,
        test_tests_never_touch_the_real_database,
        test_the_dashboard_has_a_live_view_with_labelled_controls,
        test_the_live_view_polls_for_state_and_shows_only_the_server_s_own_view,
        test_the_live_view_never_calls_an_unverified_person_absent,
        test_every_live_state_renders_as_a_symbol_and_text,
        test_the_live_section_adds_no_animation_or_external_request,
        test_the_served_dashboard_matches_its_source_after_the_live_changes,
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
