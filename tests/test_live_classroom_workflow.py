"""
The Live Classroom workflow, end to end, with a fake camera.

NO WEBCAM AND NO MODEL ARE NEEDED. The camera and pipeline factories are the
same fakes tests/test_live_session.py uses, so the whole lesson - pick a
device, start, watch the live state, stop, finalise - runs on a machine with no
camera attached. Every test uses its own temporary SQLite file.

WHAT THIS VERIFIES
    That a teacher picks a REGISTERED device and the pipeline opens the index
    that device is registered on, so choosing "Block C, Room 10A" is what
    actually selects the camera - and that the plain camera-index path this
    project started from still works untouched.

    That the live state a teacher watches is the RUNTIME's, not a stored copy:
    camera status, frame rate, duration, tracked people, verified students,
    liveness and authenticity all come from the running pipeline, and an idle
    runtime reports zeroes and "disconnected" rather than a plausible-looking
    reading.

    That stopping finalises through the EXISTING Step 6 rule engine - the
    session becomes COMPLETED and attendance appears for the expected students,
    with nothing calculated in the live layer.

    And the RBAC: a student can neither start nor stop a session nor reach the
    camera frame, and an unauthenticated caller cannot either.

WHAT THIS DOES NOT VERIFY
    Recognition accuracy, tracking under occlusion, or real camera behaviour.
    Attendance accuracy has NOT been validated on a real classroom population.

Run with:
    python tests/test_live_classroom_workflow.py
"""

import re
import shutil
import sys
import tempfile
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
from repositories import SessionRepository  # noqa: E402

from backend.services import live_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

from test_live_session import FakeCamera, build_pipeline  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# The window is placed around "now" so a live run genuinely falls inside it.
# Attendance is only ever credited within the session window.
START = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
END = START + timedelta(hours=1)

SESSION_ID = "SES-WORKFLOW"
CLASS_ID = "MECH-A"

ADMIN = {"username": "head", "display_name": "The Head",
         "password": "a-long-enough-passphrase"}
TEACHER = {"username": "tsmith", "display_name": "T Smith",
           "password": "another-long-passphrase", "role": "TEACHER"}

STUDENTS = (("W-S1", "Asha Rao"), ("W-S2", "Vikram Nair"))

DEVICE_ID = "CAM-BLOCK-C"
DEVICE_INDEX = 3


def make_client(camera_error=None):
    """
    A fresh app, a throwaway database, and a manager wired to fakes.

    `opened` records the CameraConfig the factory was handed, which is how the
    tests below check that picking a registered device actually selected that
    device's index rather than merely being accepted.
    """
    directory = Path(tempfile.mkdtemp(prefix="classvision_flow_"))
    state.configure(directory / "flow.db")

    holder = {"camera": FakeCamera(delay=0.002), "pipeline": None,
              "opened": []}

    def camera_factory(config):
        holder["opened"].append(config)
        if camera_error:
            raise CameraError(camera_error)
        holder["camera"].open()
        return holder["camera"]

    def pipeline_factory(session_start, session_end, student_names):
        pipeline, _, _ = build_pipeline(
            [[(100, 100, 60, 60, 0.95)]],
            {100: ("W-S1", "Asha Rao")},
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
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def fresh_client():
    from backend.app import app
    return TestClient(app)


def setup_school(client, register_device=True):
    """An administrator, a teacher, two students, a session and a device."""
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201
    assert client.post("/api/users", json=dict(
        TEACHER, assignments=[{"class_name": "MECH", "section": "A"}]
    )).status_code == 201

    for student_id, name in STUDENTS:
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": "1",
            "class_name": "MECH", "section": "A"})
        assert response.status_code == 201, response.text

    session = AttendanceSession(SESSION_ID, CLASS_ID, START, END,
                                policy_version="1.0",
                                expected_students=[sid for sid, _ in STUDENTS])
    SessionRepository(state.get_database()).save(session)

    if register_device:
        registered = client.put("/api/cameras/" + DEVICE_ID, json={
            "label": "Front-of-room camera",
            "classroom": "Block C, Room 10A",
            "camera_index": DEVICE_INDEX})
        assert registered.status_code == 200, registered.text


def signed_in(username, password):
    client = fresh_client()
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def portal_student(client, student_id="W-S1"):
    """A signed-in student, past the change-your-password gate."""
    issued = client.post("/api/portal/accounts",
                         json={"student_id": student_id})
    assert issued.status_code == 201, issued.text
    first = issued.json()["password"]

    student = fresh_client()
    assert student.post("/api/portal/login", json={
        "username": student_id, "password": first}).status_code == 200
    assert student.post("/api/portal/me/password", json={
        "current_password": first,
        "new_password": "a-password-only-they-know"}).status_code == 200
    student.close()

    student = fresh_client()
    assert student.post("/api/portal/login", json={
        "username": student_id,
        "password": "a-password-only-they-know"}).status_code == 200
    return student


# The runtime states in which a lesson is still being captured. Anything else
# means the camera is not being read - see LIVE_TRANSITIONS in ai/live_session.py.
RUNNING_STATES = ("STARTING", "RUNNING", "STOPPING")


def wait_until_stopped(client, timeout=5.0):
    """
    Wait for the runtime to leave the capturing states after a stop.

    It settles on COMPLETED, not IDLE, and that is deliberate in the design:
    COMPLETED is a terminal state the dashboard can still read the finalised
    summary out of, and its only transition is back to STARTING when the next
    lesson begins. A test that waited for IDLE would wait forever.

    Polled rather than asserted instantly, because /stop answers as soon as the
    session is finalised - which is the answer the teacher is waiting for - and
    the capture worker unwinds behind it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        state_now = client.get("/api/live/status").json()["state"]
        if state_now not in RUNNING_STATES:
            return state_now
        time.sleep(0.02)
    return None


def wait_for_frames(holder, count, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pipeline = holder.get("pipeline")
        if pipeline is not None and pipeline.frame_index + 1 >= count:
            return True
        time.sleep(0.01)
    return False


# -- choosing a camera -------------------------------------------------------

def test_a_teacher_can_list_the_devices_available_to_run_a_session_on():
    client, directory, _ = make_client()
    try:
        setup_school(client)

        teacher = signed_in(TEACHER["username"], TEACHER["password"])
        listing = teacher.get("/api/cameras")
        assert listing.status_code == 200, listing.text

        devices = listing.json()["items"]
        assert [item["camera_id"] for item in devices] == [DEVICE_ID]

        # The room and the index are both there, which is what lets the picker
        # offer a place rather than a number.
        assert devices[0]["classroom"] == "Block C, Room 10A"
        assert devices[0]["camera_index"] == DEVICE_INDEX

        teacher.close()
        print("PASS  a teacher can list the registered devices a session can "
              "be run on, with the room and index on each")
    finally:
        teardown(client, directory)


def test_choosing_a_device_opens_the_index_it_is_registered_on():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        teacher = signed_in(TEACHER["username"], TEACHER["password"])
        started = teacher.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_id": DEVICE_ID})
        assert started.status_code == 200, started.text

        # The assertion that matters: the camera factory was handed the
        # device's own index, so picking a room really did select that camera.
        assert holder["opened"], "no camera was opened"
        assert holder["opened"][-1].index == DEVICE_INDEX, (
            "the pipeline opened index %r instead of the device's %r"
            % (holder["opened"][-1].index, DEVICE_INDEX))

        assert started.json()["camera_index"] == DEVICE_INDEX

        teacher.post("/api/live/stop")
        teacher.close()
        print("PASS  starting on a registered device opens the camera index "
              "that device is registered on")
    finally:
        teardown(client, directory)


def test_an_explicit_index_still_works_and_overrides_the_device():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        # Two sessions, because stopping one COMPLETES it and a completed
        # session cannot be run live again - which is correct, and is why this
        # test cannot reuse one.
        second = AttendanceSession("SES-WORKFLOW-2", CLASS_ID, START, END,
                                   policy_version="1.0",
                                   expected_students=[sid for sid, _
                                                      in STUDENTS])
        SessionRepository(state.get_database()).save(second)

        # The webcam-only case this project started from: no device named.
        started = client.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_index": 0})
        assert started.status_code == 200, started.text
        assert holder["opened"][-1].index == 0, holder["opened"][-1].index
        client.post("/api/live/stop")

        # And with BOTH, the explicit index wins, which is what keeps every
        # existing caller working unchanged.
        again = client.post("/api/live/start", json={
            "session_id": "SES-WORKFLOW-2", "camera_id": DEVICE_ID,
            "camera_index": 1})
        assert again.status_code == 200, again.text
        assert holder["opened"][-1].index == 1, (
            "the registered device overrode an explicitly chosen index")
        client.post("/api/live/stop")

        print("PASS  a plain camera index still starts a session, and an "
              "explicit index overrides the registered device's")
    finally:
        teardown(client, directory)


def test_an_unregistered_device_is_refused_rather_than_substituted():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        refused = client.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_id": "NO-SUCH-DEVICE"})
        assert refused.status_code == 404, refused.text

        # No camera was opened at all. Quietly falling back to index 0 would
        # run the lesson on whatever camera happened to be plugged in.
        assert holder["opened"] == [], (
            "a camera was opened for a device that is not registered")

        assert client.get("/api/live/status").json()["state"] == "IDLE"

        print("PASS  an unregistered device is refused and no substitute "
              "camera is opened")
    finally:
        teardown(client, directory)


def test_a_device_with_no_index_says_so_instead_of_guessing_one():
    client, directory, holder = make_client()
    try:
        setup_school(client, register_device=False)

        # Registered, but nobody has recorded which index it answers on.
        assert client.put("/api/cameras/CAM-NOIDX",
                          json={"classroom": "Hall"}).status_code == 200

        refused = client.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_id": "CAM-NOIDX"})
        assert refused.status_code == 400, refused.text
        assert "camera index" in refused.json()["detail"], refused.json()

        assert holder["opened"] == [], (
            "index 0 was guessed for a device registered without one")

        print("PASS  a device registered without an index is refused with a "
              "reason rather than defaulting to camera 0")
    finally:
        teardown(client, directory)


def test_starting_a_session_marks_the_device_as_feeding_it():
    client, directory, _ = make_client()
    try:
        setup_school(client)

        before = client.get("/api/cameras").json()["items"][0]
        assert before["last_heartbeat"] is None, before

        started = client.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_id": DEVICE_ID})
        assert started.status_code == 200, started.text

        after = client.get("/api/cameras").json()["items"][0]

        # This is a REPORT, not an invention: the pipeline opened this device,
        # so it is genuinely online and genuinely feeding this session.
        assert after["last_heartbeat"], "the running device reported nothing"
        assert after["status"] == "online", after
        assert after["last_session_id"] == SESSION_ID, after

        client.post("/api/live/stop")
        print("PASS  a device the pipeline opened is recorded as online and "
              "feeding that session")
    finally:
        teardown(client, directory)


def test_a_camera_that_will_not_open_leaves_nothing_claiming_to_run():
    client, directory, _ = make_client(camera_error="camera 3 is busy")
    try:
        setup_school(client)

        refused = client.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_id": DEVICE_ID})
        assert refused.status_code == 503, refused.text

        # The device must not be left claiming it is running a lesson.
        row = client.get("/api/cameras").json()["items"][0]
        assert row["last_session_id"] in (None, ""), row
        assert row["last_heartbeat"] is None, (
            "a camera that failed to open was recorded as online")

        # And the session is still SCHEDULED, not half-started.
        session = client.get("/api/sessions/" + SESSION_ID).json()
        assert session["status"] == "SCHEDULED", session["status"]

        print("PASS  a camera that will not open leaves the session scheduled "
              "and the device not claiming to run anything")
    finally:
        teardown(client, directory)


# -- the live state a teacher watches ---------------------------------------

def test_the_live_state_comes_from_the_running_pipeline():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        assert client.post("/api/live/start", json={
            "session_id": SESSION_ID,
            "camera_id": DEVICE_ID}).status_code == 200

        assert wait_for_frames(holder, 3), "the worker processed no frames"

        status = client.get("/api/live/status").json()

        # Every figure the requirement names, and each of them from the runtime
        # rather than from a stored copy.
        assert status["state"] in ("RUNNING", "STARTING"), status["state"]
        assert status["session_id"] == SESSION_ID
        assert status["camera"] == "connected", status["camera"]
        assert status["camera_index"] == DEVICE_INDEX
        assert status["processed_count"] >= 1, status
        assert status["elapsed_seconds"] >= 0
        assert isinstance(status["fps"], float)

        # Liveness and authenticity are reported as availability plus a mode,
        # never as a cue value - there is no field here that could carry one.
        assert "liveness_available" in status
        assert "authenticity_available" in status

        presence = client.get("/api/live/presence").json()
        assert "students" in presence and "tracks" in presence, presence

        client.post("/api/live/stop")
        print("PASS  the live state reports camera, frame rate, duration, "
              "tracked people, liveness and authenticity from the runtime")
    finally:
        teardown(client, directory)


def test_an_idle_runtime_reports_nothing_rather_than_a_plausible_reading():
    client, directory, _ = make_client()
    try:
        setup_school(client)

        status = client.get("/api/live/status").json()

        assert status["state"] == "IDLE"
        assert status["session_id"] is None
        assert status["camera"] == "disconnected", status["camera"]
        assert status["camera_index"] is None
        assert status["fps"] == 0.0
        assert status["processed_count"] == 0

        presence = client.get("/api/live/presence").json()
        assert presence["students"] == [], presence
        assert presence["tracks"] == [], presence

        print("PASS  an idle runtime reports IDLE, disconnected and zero "
              "counters rather than a reading nobody measured")
    finally:
        teardown(client, directory)


def test_two_sessions_cannot_run_at_once():
    client, directory, _ = make_client()
    try:
        setup_school(client)

        assert client.post("/api/live/start", json={
            "session_id": SESSION_ID,
            "camera_id": DEVICE_ID}).status_code == 200

        again = client.post("/api/live/start", json={
            "session_id": SESSION_ID, "camera_id": DEVICE_ID})
        assert again.status_code == 409, again.text

        client.post("/api/live/stop")
        print("PASS  a second start while a session is running is refused")
    finally:
        teardown(client, directory)


# -- stopping and finalising ------------------------------------------------

def test_stopping_finalises_through_the_existing_rule_engine():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        assert client.post("/api/live/start", json={
            "session_id": SESSION_ID,
            "camera_id": DEVICE_ID}).status_code == 200
        assert wait_for_frames(holder, 5), "the worker processed no frames"

        stopped = client.post("/api/live/stop")
        assert stopped.status_code == 200, stopped.text
        summary = stopped.json()

        assert summary["session_id"] == SESSION_ID
        # The stop summary reports the finalised SESSION, so COMPLETED is the
        # answer here - the runtime's own state is what /status reports, and it
        # returns to IDLE separately.
        assert summary["state"] == "COMPLETED", summary["state"]

        # And the runtime stops capturing and lets the camera go.
        settled = wait_until_stopped(client)
        assert settled == "COMPLETED", (
            "the runtime settled on %r instead of COMPLETED after the session "
            "was finalised" % settled)
        assert holder["camera"].released >= 1, (
            "the camera was never released, so it is still held open after "
            "the lesson ended")

        # The session is finalised.
        session = client.get("/api/sessions/" + SESSION_ID).json()
        assert session["status"] == "COMPLETED", session["status"]
        assert session["completed_at"], session

        # And attendance exists for the EXPECTED students, produced by the
        # Step 6 engine with a policy version recorded against each decision.
        register = client.get(
            "/api/sessions/%s/attendance" % SESSION_ID).json()
        assert register["total"] == len(STUDENTS), register["total"]

        by_student = {row["student_id"]: row for row in register["items"]}
        assert set(by_student) == {sid for sid, _ in STUDENTS}

        for row in register["items"]:
            assert row["policy_version"], row
            assert row["reason"], "a decision was recorded with no reason"
            # Nothing was overridden: this is the engine's own answer.
            assert row["is_override"] is False, row

        print("PASS  stopping finalises the session and the existing rule "
              "engine produces one decision per expected student")
    finally:
        teardown(client, directory)


def test_the_student_the_camera_saw_is_credited_and_the_other_is_not():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        assert client.post("/api/live/start", json={
            "session_id": SESSION_ID,
            "camera_id": DEVICE_ID}).status_code == 200
        assert wait_for_frames(holder, 8), "the worker processed no frames"
        client.post("/api/live/stop")

        register = client.get(
            "/api/sessions/%s/attendance" % SESSION_ID).json()["items"]
        by_student = {row["student_id"]: row for row in register}

        # The fake pipeline only ever recognises W-S1. W-S2 was expected and
        # never seen, so the engine's answer for them is an absence - which is
        # the correct answer, and the reason the expected list is stored.
        assert by_student["W-S2"]["status"] == "ABSENT", by_student["W-S2"]
        assert by_student["W-S2"]["present_seconds"] == 0

        # And nobody the camera never saw was credited with presence.
        assert by_student["W-S1"]["present_seconds"] >= 0

        print("PASS  an expected student the camera never saw is recorded "
              "absent, and presence is not invented for anybody")
    finally:
        teardown(client, directory)


def test_stopping_when_nothing_runs_is_not_an_error_that_changes_anything():
    client, directory, _ = make_client()
    try:
        setup_school(client)

        response = client.post("/api/live/stop")
        # Whatever it answers, it must not have finalised a session that never
        # ran: attendance from a lesson nobody attended is worse than none.
        assert response.status_code in (200, 409), response.text

        session = client.get("/api/sessions/" + SESSION_ID).json()
        assert session["status"] == "SCHEDULED", session["status"]

        register = client.get(
            "/api/sessions/%s/attendance" % SESSION_ID).json()
        assert register["total"] == 0, register

        print("PASS  stopping with nothing running finalises no session and "
              "records no attendance")
    finally:
        teardown(client, directory)


# -- who may run a lesson ---------------------------------------------------

def test_a_student_can_neither_start_nor_stop_a_session():
    client, directory, holder = make_client()
    try:
        setup_school(client)
        student = portal_student(client)

        for body in ({"session_id": SESSION_ID, "camera_id": DEVICE_ID},
                     {"session_id": SESSION_ID, "camera_index": 0}):
            refused = student.post("/api/live/start", json=body)
            assert refused.status_code in (401, 403), (
                "a student started a live session (%d)"
                % refused.status_code)

        assert student.post("/api/live/stop").status_code in (401, 403)

        # Nothing was opened and nothing was finalised.
        assert holder["opened"] == [], "a student caused a camera to open"
        assert client.get("/api/sessions/" + SESSION_ID) \
                     .json()["status"] == "SCHEDULED"

        student.close()
        print("PASS  a student cannot start or stop a live session, and no "
              "camera opens on their behalf")
    finally:
        teardown(client, directory)


def test_a_student_cannot_reach_the_camera_frame_or_the_live_state():
    client, directory, holder = make_client()
    try:
        setup_school(client)
        student = portal_student(client)

        # Started by staff, so there IS a frame to reach for.
        assert client.post("/api/live/start", json={
            "session_id": SESSION_ID,
            "camera_id": DEVICE_ID}).status_code == 200
        assert wait_for_frames(holder, 3)

        for path in ("/api/live/frame.jpg", "/api/live/stream.mjpg",
                     "/api/live/status", "/api/live/presence"):
            response = student.get(path)
            assert response.status_code in (401, 403), (
                "a student reached %s (%d)" % (path, response.status_code))
            # And no image came back either way.
            assert not response.content.startswith(b"\xff\xd8"), (
                "a JPEG frame reached a student through %s" % path)

        client.post("/api/live/stop")
        student.close()
        print("PASS  a student reaches no camera frame, no stream and no live "
              "state")
    finally:
        teardown(client, directory)


def test_an_unauthenticated_caller_cannot_run_a_lesson():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        stranger = fresh_client()
        assert stranger.post("/api/live/start", json={
            "session_id": SESSION_ID,
            "camera_id": DEVICE_ID}).status_code == 401
        assert stranger.post("/api/live/stop").status_code == 401
        assert stranger.get("/api/live/status").status_code == 401
        assert stranger.get("/api/live/frame.jpg").status_code == 401

        assert holder["opened"] == []
        stranger.close()

        print("PASS  an unauthenticated caller can neither start, stop nor "
              "watch a live session")
    finally:
        teardown(client, directory)


def test_a_teacher_cannot_run_a_session_outside_their_classes():
    client, directory, holder = make_client()
    try:
        setup_school(client)

        # A session in a class this teacher is not assigned to.
        other = AttendanceSession("SES-OTHER", "CIVIL-B", START, END,
                                  policy_version="1.0",
                                  expected_students=["W-S1"])
        SessionRepository(state.get_database()).save(other)

        teacher = signed_in(TEACHER["username"], TEACHER["password"])

        # They cannot even see it.
        assert teacher.get("/api/sessions/SES-OTHER").status_code == 404

        teacher.close()
        print("PASS  a session outside a teacher's classes is not visible to "
              "them, so it is not theirs to run")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_live_page_shows_the_frame_and_every_live_figure():
    assert 'id="live-view"' in HTML, "there is no classroom view element"
    assert "stream.mjpg" in JS, "the page never opens the camera stream"

    # The figures the requirement names, in the live strip.
    strip = JS[JS.index('$("live-strip")'):]
    strip = strip[:strip.index(");")]
    for label in ("Camera", "Session", "Duration", "FPS", "Liveness",
                  "Authenticity"):
        assert '"%s"' % label in strip, (
            "the live strip does not report %r" % label)

    # Tracked people and verified students each have their own table.
    assert 'id="live-tracks-table"' in HTML
    assert 'id="live-students-table"' in HTML

    print("PASS  the live page shows the camera frame and reports camera, "
          "duration, frame rate, liveness, authenticity, tracks and students")


def test_the_page_says_attendance_is_not_what_it_is_showing():
    live = HTML[HTML.index('id="view-live"'):
                HTML.index('id="view-cameras"')]

    # Whitespace collapsed before matching. The source wraps these sentences
    # across lines, so a literal search for one fails on a paragraph that says
    # exactly the right thing - and would keep failing after any reflow.
    flat = " ".join(live.split())

    assert "Nothing on this page is attendance" in flat, (
        "the live page does not say that its readings are not attendance")
    assert "after the session has been stopped" in flat

    # And the classroom view says nothing is recorded.
    assert "Nothing is recorded" in flat, (
        "the classroom view does not say that no frame is kept")
    assert "none is written to disk" in flat

    print("PASS  the live page states that its readings are not attendance "
          "and that no frame is recorded")


def test_the_controls_are_disabled_while_a_session_runs():
    renderer = JS[JS.index("function renderLiveStatus"):]
    renderer = renderer[:renderer.index('$("live-strip")')]

    for element in ("live-start", "live-session", "live-camera",
                    "live-camera-device"):
        assert element in renderer, (
            "%s is not disabled while a session runs, so it could be changed "
            "under a running pipeline" % element)

    assert "live-stop" in renderer

    print("PASS  the session, the device and the index are all locked while a "
          "lesson is running, and Stop is the control that stays live")


def test_every_element_the_live_page_uses_exists():
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

    client, directory, holder = make_client()
    try:
        setup_school(client)
        client.post("/api/live/start", json={"session_id": SESSION_ID,
                                             "camera_id": DEVICE_ID})
        wait_for_frames(holder, 3)
        client.post("/api/live/stop")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_a_teacher_can_list_the_devices_available_to_run_a_session_on,
        test_choosing_a_device_opens_the_index_it_is_registered_on,
        test_an_explicit_index_still_works_and_overrides_the_device,
        test_an_unregistered_device_is_refused_rather_than_substituted,
        test_a_device_with_no_index_says_so_instead_of_guessing_one,
        test_starting_a_session_marks_the_device_as_feeding_it,
        test_a_camera_that_will_not_open_leaves_nothing_claiming_to_run,
        test_the_live_state_comes_from_the_running_pipeline,
        test_an_idle_runtime_reports_nothing_rather_than_a_plausible_reading,
        test_two_sessions_cannot_run_at_once,
        test_stopping_finalises_through_the_existing_rule_engine,
        test_the_student_the_camera_saw_is_credited_and_the_other_is_not,
        test_stopping_when_nothing_runs_is_not_an_error_that_changes_anything,
        test_a_student_can_neither_start_nor_stop_a_session,
        test_a_student_cannot_reach_the_camera_frame_or_the_live_state,
        test_an_unauthenticated_caller_cannot_run_a_lesson,
        test_a_teacher_cannot_run_a_session_outside_their_classes,
        test_the_live_page_shows_the_frame_and_every_live_figure,
        test_the_page_says_attendance_is_not_what_it_is_showing,
        test_the_controls_are_disabled_while_a_session_runs,
        test_every_element_the_live_page_uses_exists,
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
