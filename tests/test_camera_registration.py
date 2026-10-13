"""
Registering a classroom camera, its heartbeat, and who may do which.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; no camera is opened and data/classvision.db is never touched.

WHAT THIS VERIFIES
    That registering a device records WHAT and WHERE it is and no health at
    all. A camera that has just been registered has never been heard from, so
    its status, frame rate, latency, heartbeat and pipeline state are null and
    the page reads "No data" - not 0 fps, which is the single most misleading
    thing this table could print about a camera nobody has heard from.

    That correcting a device's details leaves its health alone. Fixing a typo in
    a room name must not report a working camera as offline.

    That a heartbeat is what fills those fields in, with exactly the values the
    device sent.

    That a teacher may LOOK at every device and change none of them, enforced
    by the server rather than by the dashboard hiding a button.

    And that a registered device connects to the live pipeline by its own
    camera index, without breaking the plain-webcam path this project started
    from.

WHAT THIS DOES NOT VERIFY
    That any camera works. No device is opened here; a heartbeat is an HTTP
    request, and whether the hardware behind it is real is not a question this
    suite can ask.

Run with:
    python tests/test_camera_registration.py
"""

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
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

ADMIN = {"username": "estates", "display_name": "The Estates Office",
         "password": "a-long-enough-passphrase"}
TEACHER = {"username": "tsmith", "display_name": "T Smith",
           "password": "another-long-passphrase", "role": "TEACHER"}

DEVICE = {
    "label": "Front-of-room camera",
    "classroom": "Block C, Room 10A",
    "camera_index": 2,
}

# The health fields. A registered device has none of them until it reports.
HEALTH_FIELDS = ("last_heartbeat", "last_fps", "last_latency_ms",
                 "last_error", "pipeline_state")


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_cam_"))
    state.configure(directory / "cameras.db")
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


def setup_school(client):
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201
    assert client.post("/api/users", json=TEACHER).status_code == 201


def register(client, camera_id="CAM-10A", **overrides):
    body = dict(DEVICE)
    body.update(overrides)
    return client.put("/api/cameras/" + camera_id, json=body)


def device(client, camera_id="CAM-10A"):
    listing = client.get("/api/cameras")
    assert listing.status_code == 200, listing.text
    for item in listing.json()["items"]:
        if item["camera_id"] == camera_id:
            return item
    return None


# -- registering -------------------------------------------------------------

def test_registering_records_what_and_where_a_device_is():
    client, directory = make_client()
    try:
        setup_school(client)

        created = register(client)
        assert created.status_code == 200, created.text

        row = device(client)
        assert row is not None, "the device is not in the register"

        assert row["camera_id"] == "CAM-10A"
        assert row["label"] == DEVICE["label"]
        assert row["classroom"] == DEVICE["classroom"]
        assert row["camera_index"] == 2

        # And when, and by whom.
        assert row["created_at"], "the registration time was not recorded"
        assert row["registered_by"] == ADMIN["username"], row

        print("PASS  registering records the device id, name, index, "
              "classroom, registration time and who did it")
    finally:
        teardown(client, directory)


def test_a_newly_registered_device_has_no_health_at_all():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        row = device(client)

        # The point of the whole feature. Nothing has been heard from this
        # device, so it has no health - and a plausible-looking zero would be a
        # reading nobody measured.
        for field in HEALTH_FIELDS:
            value = row.get(field)
            assert value in (None, ""), (
                "a device that has never reported has %s = %r" % (field, value))

        assert row["status"] == "unknown", row["status"]
        assert row["last_fps"] != 0, "a silent camera was credited with 0 fps"

        print("PASS  a registered device that has never reported has no "
              "status, frame rate, latency, heartbeat or pipeline state")
    finally:
        teardown(client, directory)


def test_correcting_a_devices_details_leaves_its_health_alone():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        beat = client.post("/api/cameras/CAM-10A/heartbeat", json={
            "status": "online", "fps": 24.5, "latency_ms": 41,
            "pipeline_state": "RUNNING"})
        assert beat.status_code == 200, beat.text
        before = device(client)

        # A typo in the room name. This must not report a working camera as
        # offline.
        fixed = register(client, classroom="Block C, Room 10B")
        assert fixed.status_code == 200, fixed.text

        after = device(client)
        assert after["classroom"] == "Block C, Room 10B"

        for field in HEALTH_FIELDS + ("status",):
            assert after[field] == before[field], (
                "correcting the room name changed %s from %r to %r"
                % (field, before[field], after[field]))

        print("PASS  correcting a device's details changes its details and "
              "none of its reported health")
    finally:
        teardown(client, directory)


def test_a_device_id_is_required_and_an_index_must_be_a_number():
    client, directory = make_client()
    try:
        setup_school(client)

        bad = client.put("/api/cameras/CAM-X", json={"camera_index": "two"})
        assert bad.status_code == 400, bad.text

        out_of_range = client.put("/api/cameras/CAM-X",
                                  json={"camera_index": 999})
        assert out_of_range.status_code == 400, out_of_range.text

        # Neither attempt registered anything.
        assert device(client, "CAM-X") is None

        # And an index is optional: a device whose index is not known yet is a
        # real state.
        fine = client.put("/api/cameras/CAM-X", json={"classroom": "Hall"})
        assert fine.status_code == 200, fine.text
        assert device(client, "CAM-X")["camera_index"] is None

        print("PASS  an unusable camera index is refused and registers "
              "nothing, while an absent one is allowed")
    finally:
        teardown(client, directory)


# -- heartbeats --------------------------------------------------------------

def test_a_heartbeat_fills_in_exactly_what_the_device_reported():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        beat = client.post("/api/cameras/CAM-10A/heartbeat", json={
            "status": "online", "fps": 29.97, "latency_ms": 38.5,
            "pipeline_state": "RUNNING"})
        assert beat.status_code == 200, beat.text

        row = device(client)
        assert row["status"] == "online"
        assert abs(row["last_fps"] - 29.97) < 1e-9, row["last_fps"]
        assert abs(row["last_latency_ms"] - 38.5) < 1e-9
        assert row["pipeline_state"] == "RUNNING"
        assert row["last_heartbeat"], "no heartbeat time was recorded"
        assert not row["last_error"]

        print("PASS  a heartbeat records the online status, frame rate, "
              "latency, pipeline state and time the device reported")
    finally:
        teardown(client, directory)


def test_a_device_can_report_a_fault_and_a_stopped_pipeline_separately():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        # A camera that is answering and feeding a pipeline that has stopped.
        # These are two different facts, and a school debugging a blank
        # classroom view needs to tell them apart - which is exactly why
        # pipeline_state is its own column and not folded into status.
        beat = client.post("/api/cameras/CAM-10A/heartbeat", json={
            "status": "online", "fps": 25, "latency_ms": 40,
            "pipeline_state": "FAILED", "error": "decoder stalled"})
        assert beat.status_code == 200, beat.text

        row = device(client)
        assert row["status"] == "online", row["status"]
        assert row["pipeline_state"] == "FAILED", row
        assert row["last_error"] == "decoder stalled"

        print("PASS  a camera that is online with a stopped pipeline reports "
              "both facts separately")
    finally:
        teardown(client, directory)


def test_a_heartbeat_for_an_unregistered_device_is_refused():
    client, directory = make_client()
    try:
        setup_school(client)

        orphan = client.post("/api/cameras/NEVER-REGISTERED/heartbeat",
                             json={"status": "online", "fps": 30})
        assert orphan.status_code == 404, orphan.text

        # And it did not quietly create a device row.
        assert device(client, "NEVER-REGISTERED") is None, (
            "a heartbeat registered a device nobody registered")

        print("PASS  a heartbeat from an unregistered device is refused and "
              "creates no device row")
    finally:
        teardown(client, directory)


def test_a_partial_heartbeat_does_not_invent_the_fields_it_omits():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        # A device that can report that it is alive but cannot measure latency.
        beat = client.post("/api/cameras/CAM-10A/heartbeat",
                           json={"status": "online", "fps": 15})
        assert beat.status_code == 200, beat.text

        row = device(client)
        assert row["status"] == "online"
        assert row["last_fps"] == 15
        assert row["last_latency_ms"] is None, (
            "a latency nobody measured was recorded as %r"
            % row["last_latency_ms"])
        assert row["last_heartbeat"], "the heartbeat time was not recorded"

        print("PASS  a device that reports no latency is left with none, "
              "rather than being credited with zero")
    finally:
        teardown(client, directory)


# -- who may do what ---------------------------------------------------------

def test_a_teacher_sees_every_device_and_can_change_none():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)
        client.post("/api/cameras/CAM-10A/heartbeat",
                    json={"status": "online", "fps": 30, "latency_ms": 20})

        teacher = signed_in(TEACHER["username"], TEACHER["password"])

        # Looking is allowed, and the health comes through.
        listing = teacher.get("/api/cameras")
        assert listing.status_code == 200, listing.text
        row = listing.json()["items"][0]
        assert row["camera_id"] == "CAM-10A"
        assert row["last_fps"] == 30

        # Registering and removing are not.
        refused = teacher.put("/api/cameras/CAM-SNEAK",
                              json={"classroom": "Somewhere"})
        assert refused.status_code == 403, refused.text

        removal = teacher.delete("/api/cameras/CAM-10A")
        assert removal.status_code == 403, removal.text

        # And nothing changed either way.
        after = client.get("/api/cameras").json()["items"]
        assert [item["camera_id"] for item in after] == ["CAM-10A"], after

        teacher.close()
        print("PASS  a teacher reads every device and its health, and can "
              "neither register nor remove one")
    finally:
        teardown(client, directory)


def test_an_unauthenticated_caller_reaches_no_camera_endpoint():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        from backend.app import app
        stranger = TestClient(app)

        assert stranger.get("/api/cameras").status_code == 401
        assert stranger.put("/api/cameras/CAM-X",
                            json={"classroom": "X"}).status_code == 401
        assert stranger.delete("/api/cameras/CAM-10A").status_code == 401
        assert stranger.post("/api/cameras/CAM-10A/heartbeat",
                             json={"status": "online"}).status_code == 401

        stranger.close()
        print("PASS  every camera endpoint refuses a caller with no session, "
              "the heartbeat included")
    finally:
        teardown(client, directory)


def test_removing_a_device_is_recorded_and_destroys_no_attendance():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        removed = client.delete("/api/cameras/CAM-10A")
        assert removed.status_code == 200, removed.text
        assert device(client) is None

        # Removing it twice is a 404, not a silent success.
        assert client.delete("/api/cameras/CAM-10A").status_code == 404

        # The audit log names who did it.
        audit = client.get("/api/users/audit")
        assert audit.status_code == 200, audit.text
        actions = [row["action"] for row in audit.json()]
        assert "camera.register" in actions, actions
        assert "camera.remove" in actions, actions

        print("PASS  removing a device is audited, and removing it again is a "
              "404 rather than a silent success")
    finally:
        teardown(client, directory)


# -- connecting to the live pipeline -----------------------------------------

def test_a_registered_device_is_what_the_live_session_is_started_against():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        schema = client.get("/openapi.json").json()
        body = schema["paths"]["/api/live/start"]["post"]["requestBody"]
        ref = body["content"]["application/json"]["schema"]["$ref"]
        model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]

        # A session is started against a DEVICE, so a teacher picks a room
        # rather than a capture index...
        assert "camera_id" in model["properties"], model["properties"].keys()
        # ...and the index is still accepted, because a machine with a webcam
        # and no registered device is the case this project started from and
        # must keep working.
        assert "camera_index" in model["properties"]

        print("PASS  a live session can be started against a registered "
              "device, and the plain camera index still works")
    finally:
        teardown(client, directory)


def test_starting_on_an_unknown_device_does_not_silently_use_another_camera():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        # There is no session, so this cannot succeed - the point is that it
        # fails on the SESSION rather than quietly opening some other camera.
        response = client.post("/api/live/start", json={
            "session_id": "NO-SUCH-SESSION", "camera_id": "NO-SUCH-DEVICE"})
        assert response.status_code in (404, 409, 503), response.text

        # And nothing was recorded against the device that does exist.
        row = device(client)
        assert row["last_heartbeat"] is None, (
            "a failed start wrote health onto a registered device")
        assert row["last_session_id"] in (None, ""), row

        print("PASS  a start that cannot run records no health against any "
              "registered device")
    finally:
        teardown(client, directory)


def test_the_camera_listing_reports_the_live_runtime_separately():
    client, directory = make_client()
    try:
        setup_school(client)
        register(client)

        body = client.get("/api/cameras").json()

        # The runtime is reported apart from the register, so the page can show
        # what is actually processing frames rather than what was last written
        # down.
        assert "live" in body, body.keys()
        live = body["live"]

        # With nothing running it says so, and names no device. "disconnected"
        # is the runtime's own word for having no camera open - a state, not a
        # reading, which is why it is asserted rather than rejected.
        assert live["state"] == "IDLE", live
        assert live["camera"] == "disconnected", live
        assert live["camera_index"] is None, live

        # And it has measured nothing. The frame COUNTERS are genuinely zero -
        # no frame has been processed - which is a count and not a rate.
        assert live["processed"] == 0, live
        assert live["dropped"] == 0, live
        assert live["last_event_at"] is None, live

        # Crucially, the idle runtime is NOT written onto any registered
        # device: the register still shows the device as never having reported.
        assert device(client)["last_heartbeat"] is None, (
            "an idle runtime was recorded as a device heartbeat")
        assert device(client)["status"] == "unknown"

        assert "note" in body
        assert "actually reported" in body["note"], body["note"]

        print("PASS  the live runtime is reported beside the register, says "
              "IDLE while nothing runs, and is never written onto a device")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_dashboard_offers_registration_and_the_device_list():
    assert 'id="camera-register"' in HTML, (
        "the Camera health page has no Register camera button")
    assert 'id="camera-dialog"' in HTML, "there is no registration dialog"

    for element in ("camera-id", "camera-label", "camera-classroom",
                    "camera-index", "camera-save"):
        assert 'id="%s"' % element in HTML, "the dialog has no %s" % element

    assert "Registered devices" in HTML, (
        "the registered device list is not labelled")
    assert "Register camera" in HTML

    # Every field the requirement names, on the page.
    for label in ("Device ID", "Device name", "Camera index",
                  "Classroom / location"):
        assert label in HTML, "the dialog does not ask for %r" % label

    # And the list shows registration time and pipeline state.
    table = HTML[HTML.index('id="cameras-table"'):]
    table = table[:table.index("</table>")]
    for column in ("Pipeline", "Registered", "Status", "Last heartbeat",
                   "FPS", "Latency"):
        assert ">%s<" % column in table, (
            "the device table has no %s column" % column)

    print("PASS  the dashboard offers Register camera, a dialog for every "
          "stored field, and a device list showing health and registration")


def test_the_device_list_says_no_data_rather_than_zero():
    renderer = JS[JS.index("function renderCameraTable"):]
    renderer = renderer[:renderer.index("/* ---------- registering a camera")]

    assert "No data" in JS, (
        "an unreported health value is not labelled No data")
    assert "reported(" in renderer, (
        "health cells are not routed through the no-data helper")

    # Status must be No data too when nothing has been heard, rather than the
    # placeholder "unknown" pill reading as a measurement.
    assert "last_heartbeat" in renderer, (
        "the status cell does not check whether the device has ever reported")

    print("PASS  every unreported health value renders as the words No data "
          "rather than as a dash or a zero")


def test_registration_controls_are_hidden_from_a_teacher_as_well_as_refused():
    renderer = JS[JS.index("function renderCameraTable"):]
    renderer = renderer[:renderer.index("/* ---------- registering a camera")]

    assert "is_super_admin" in renderer, (
        "the device table offers its buttons regardless of role")
    assert "View only" in renderer, (
        "a teacher is given no indication of why they have no buttons")
    assert 'camera-register").hidden' in renderer

    print("PASS  a teacher is shown the devices as view-only, and the server "
          "refuses the writes regardless")


def test_the_live_tab_picks_a_registered_device_and_keeps_the_index():
    assert 'id="live-camera-device"' in HTML, (
        "the Live classroom tab cannot pick a registered camera")
    assert 'id="live-camera"' in HTML, (
        "the plain camera index was removed, breaking the webcam-only case")

    assert "loadLiveCameras" in JS
    picker = JS[JS.index("async function loadLiveCameras"):]
    picker = picker[:picker.index("function scheduledSession")]
    assert '/api/cameras' in picker, (
        "the live tab does not read the same device list as Camera health")
    assert "no data yet" in picker, (
        "a device that has not reported is given a health reading it does not "
        "have")

    starter = JS[JS.index("async function startLive"):]
    starter = starter[:starter.index("\n}\n")]
    assert "camera_id" in starter, (
        "starting a session does not send the chosen device")
    assert "camera_index" in starter, (
        "starting a session no longer sends an explicit index")

    print("PASS  the live tab selects a registered device, still accepts a "
          "plain index, and sends both")


def test_every_element_the_camera_pages_use_exists():
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
        register(client)
        client.post("/api/cameras/CAM-10A/heartbeat",
                    json={"status": "online", "fps": 30})
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_registering_records_what_and_where_a_device_is,
        test_a_newly_registered_device_has_no_health_at_all,
        test_correcting_a_devices_details_leaves_its_health_alone,
        test_a_device_id_is_required_and_an_index_must_be_a_number,
        test_a_heartbeat_fills_in_exactly_what_the_device_reported,
        test_a_device_can_report_a_fault_and_a_stopped_pipeline_separately,
        test_a_heartbeat_for_an_unregistered_device_is_refused,
        test_a_partial_heartbeat_does_not_invent_the_fields_it_omits,
        test_a_teacher_sees_every_device_and_can_change_none,
        test_an_unauthenticated_caller_reaches_no_camera_endpoint,
        test_removing_a_device_is_recorded_and_destroys_no_attendance,
        test_a_registered_device_is_what_the_live_session_is_started_against,
        test_starting_on_an_unknown_device_does_not_silently_use_another_camera,
        test_the_camera_listing_reports_the_live_runtime_separately,
        test_the_dashboard_offers_registration_and_the_device_list,
        test_the_device_list_says_no_data_rather_than_zero,
        test_registration_controls_are_hidden_from_a_teacher_as_well_as_refused,
        test_the_live_tab_picks_a_registered_device_and_keeps_the_index,
        test_every_element_the_camera_pages_use_exists,
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
