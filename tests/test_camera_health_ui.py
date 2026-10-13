"""
The Camera Health dashboard page.

NO WEBCAM AND NO BROWSER ARE NEEDED. The API half runs against a temporary
SQLite file; the UI half is asserted against the dashboard's own source files.

WHAT THIS VERIFIES
    That the page exists in the existing navigation, that it reads the
    endpoint that already exists rather than a new one, that it has a real
    loading state and a real error state with a retry, that it renders an
    offline camera as offline with the backend's own reason, that it invents no
    field the API does not return, that its polling timer cannot be duplicated
    and stops when the page is left, and that it opens no camera of its own.

WHAT THIS DOES NOT VERIFY
    That a human clicking the tab sees what is described here. No browser is
    driven - the DOM is never built, only the source that would build it. That
    limit is stated in the report rather than glossed over.

Run with:
    python tests/test_camera_health_ui.py
"""

import json
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
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from backend.services import state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# The endpoint that already existed. The page must use this one and add none.
ENDPOINT = "/api/cameras"

# Every field GET /api/cameras actually returns for a device. The page may show
# these and nothing else - see test_the_page_invents_no_field.
ITEM_FIELDS = ("camera_id", "label", "classroom", "camera_index", "status",
               "last_heartbeat", "last_fps", "last_latency_ms", "last_error",
               "created_at", "updated_at",
               # Device registration (Step 14). Each is REPORTED rather than
               # derived: pipeline_state and last_session_id are what the
               # device last said about the processing it is feeding, and
               # registered_by is who added it. A device that has never sent
               # a heartbeat carries "" and NULL for these, which the page
               # renders as "No data" - the same rule as last_fps.
               "pipeline_state", "registered_by", "last_session_id")

LIVE_FIELDS = ("state", "camera", "camera_index", "fps", "processed",
               "dropped", "last_event_at")

# Fields a monitoring page might plausibly want and this API does NOT provide.
# Showing any of them would mean inventing a number about real hardware.
NOT_PROVIDED = ("resolution", "detection_available", "recognition_available",
                "liveness_available", "frame_width", "frame_height",
                "uptime", "temperature", "bitrate")


# -- fixtures ---------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_camhealth_"))
    state.configure(directory / "camhealth.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def camera_health_source():
    """The JavaScript between the camera-health banner and the next section."""
    start = JS.index("/* ---------- camera health ---------- */")
    end = JS.index("/* ---------- administration ---------- */", start)
    return JS[start:end]


# -- navigation -------------------------------------------------------------

def test_camera_health_is_a_tab_in_the_existing_navigation():
    assert 'data-view="cameras"' in HTML, "there is no Camera health tab"
    assert ">Camera health<" in HTML, "the tab has no readable label"
    assert 'id="view-cameras"' in HTML, "there is no Camera health view"

    # It uses the dashboard's own switching, not a second mechanism.
    assert '$("view-cameras").hidden = view !== "cameras"' in JS, (
        "the view is not driven by the existing switchView()")
    assert 'state.view === "cameras"' in JS, (
        "refresh() does not know about the camera view")

    print("PASS  Camera health is a tab in the existing navigation and view "
          "switcher")


def test_the_view_has_semantic_headings_and_a_labelled_refresh():
    assert 'id="cameras-heading"' in HTML
    assert 'aria-labelledby="cameras-heading"' in HTML, (
        "the view is not associated with its heading")

    section = HTML[HTML.index('id="view-cameras"'):
                   HTML.index('ADMINISTRATION')]

    for heading in ("Current device", "Live pipeline", "Registered devices"):
        assert ">%s<" % heading in section, (
            "no %r section heading" % heading)

    assert '>Refresh<' in section, "there is no Refresh button"
    assert '>Try again<' in section, "the error state has no retry button"

    # A table needs a caption for anybody reading it without sighted layout.
    assert 'class="sr-only">Registered camera devices' in section, (
        "the devices table has no accessible caption")

    print("PASS  the view has three headed sections, a caption, a Refresh and "
          "a Try again")


# -- the API contract -------------------------------------------------------

def test_the_page_calls_the_endpoint_that_already_existed():
    source = camera_health_source()

    assert 'api("%s")' % ENDPOINT in source, (
        "the page does not call %s" % ENDPOINT)

    # No invented endpoint, however plausible.
    for guess in ("/api/camera/health", "/api/cameras/health",
                  "/api/health/camera", "/api/camera-health"):
        assert guess not in JS, "the page invents the endpoint %s" % guess

    # It goes through the shared api() helper, so 401 handling and the session
    # cookie are the same as everywhere else.
    assert "fetch(" not in source, (
        "the camera page calls fetch() directly instead of using api()")

    print("PASS  the page calls %s through the shared api() helper" % ENDPOINT)


def test_the_endpoint_returns_exactly_the_fields_the_page_reads():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={
            "label": "Classroom Camera 01", "classroom": "Lab 2",
            "camera_index": 0})

        body = client.get(ENDPOINT).json()

        assert sorted(body) == ["items", "live", "note"], (
            "the response shape changed: %s" % sorted(body))
        assert sorted(body["items"][0]) == sorted(ITEM_FIELDS), (
            "a device field changed: %s" % sorted(body["items"][0]))
        assert sorted(body["live"]) == sorted(LIVE_FIELDS), (
            "a runtime field changed: %s" % sorted(body["live"]))

        print("PASS  %d device fields and %d runtime fields, exactly as the "
              "page reads them" % (len(ITEM_FIELDS), len(LIVE_FIELDS)))
    finally:
        teardown(client, directory)


def test_the_page_invents_no_field():
    """
    A monitoring page that shows a resolution nobody reported is worse than one
    that shows nothing: the number looks measured.
    """
    source = camera_health_source()

    for field in NOT_PROVIDED:
        assert field not in source, (
            "the page displays %r, which GET %s does not return"
            % (field, ENDPOINT))

    print("PASS  none of %d fields the API does not provide appear on the page"
          % len(NOT_PROVIDED))


# -- states -----------------------------------------------------------------

def test_there_is_a_loading_state_and_it_is_not_left_showing():
    assert 'id="cameras-loading"' in HTML
    assert "Checking camera health" in HTML, (
        "there is no loading message")

    source = camera_health_source()

    # One function owns all three states, so they cannot both be visible.
    assert "function camerasState(" in source
    assert 'camerasState("ready")' in source
    assert 'camerasState("loading")' in source
    assert 'camerasState("error"' in source

    # The spinner is suppressed on a poll, or it would flash every 7 seconds.
    assert 'if ($("cameras-body").hidden && $("cameras-error").hidden)' in \
        source, "the loading state is shown on every poll, not just the first"

    print("PASS  a loading state exists, is exclusive, and does not flash on "
          "every poll")


def test_a_failed_request_shows_an_error_with_a_retry():
    assert 'id="cameras-error"' in HTML
    assert "Unable to retrieve camera health" in HTML, (
        "there is no error message")
    assert 'id="cameras-retry"' in HTML

    source = camera_health_source()
    assert 'camerasState("error"' in source

    # 403 gets its own sentence rather than a raw status code.
    assert "failure.status === 403" in source, (
        "a forbidden response is not handled distinctly")
    assert "does not have access to camera health" in source

    # And the buttons are wired.
    assert '$("cameras-retry").addEventListener("click", refreshCameras)' in JS
    assert '$("cameras-refresh").addEventListener("click", refreshCameras)' in JS

    print("PASS  a failure renders an error state, names a 403, and offers a "
          "working retry")


def test_an_unauthenticated_caller_is_refused_by_the_backend():
    """The page shows nothing the backend did not authorise."""
    client, directory = make_client()
    try:
        # An account exists, so the installation is configured and closed.
        setup = client.post("/api/auth/setup", json={
            "username": "owner", "display_name": "Owner",
            "password": "a-long-enough-passphrase"})
        assert setup.status_code == 201, setup.text

        from backend.app import app
        anonymous = TestClient(app)
        try:
            assert anonymous.get(ENDPOINT).status_code == 401, (
                "camera health is readable without signing in")
        finally:
            anonymous.close()

        print("PASS  %s is 401 for an unauthenticated caller" % ENDPOINT)
    finally:
        teardown(client, directory)


def test_a_registered_camera_nobody_has_heard_from_reads_unknown():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={
            "label": "Classroom Camera 01", "classroom": "Lab 2",
            "camera_index": 0})

        item = client.get(ENDPOINT).json()["items"][0]

        assert item["status"] == "unknown", (
            "a camera nobody has heard from reads %r" % item["status"])
        assert item["last_heartbeat"] is None
        assert item["last_fps"] is None

        # The page must have a neutral presentation for exactly this.
        source = camera_health_source()
        assert "unknown:" in source, "there is no UNKNOWN indicator"
        # The class is built from the reported status, and an unrecognised
        # status falls back to unknown rather than to nothing.
        assert '"pill pill-cam-" + (known ? key : "unknown")' in source, (
            "the status class is not derived from the reported status")
        assert ".pill-cam-unknown" in CSS, "UNKNOWN has no style"

        print("PASS  an unheard-from camera is UNKNOWN, with a neutral "
              "indicator, not a green light")
    finally:
        teardown(client, directory)


def test_an_offline_camera_renders_as_offline_with_the_backend_s_reason():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={"label": "Front"})
        client.post("/api/cameras/CAM-01/heartbeat", json={"fps": 12.5})
        client.post("/api/cameras/CAM-01/heartbeat", json={
            "status": "offline", "error": "the device stopped responding"})

        item = client.get(ENDPOINT).json()["items"][0]
        assert item["status"] == "offline"
        assert item["last_error"] == "the device stopped responding"
        assert item["last_fps"] == 12.5, (
            "the last measured frame rate was discarded")

        source = camera_health_source()
        assert '"pill pill-cam-" + (known ? key : "unknown")' in source, (
            "the status class is not derived from the reported status")
        assert "offline:" in source, "there is no OFFLINE indicator"
        assert ".pill-cam-offline" in CSS, "OFFLINE has no style"
        # The reason is shown, and shown as the device's own words.
        assert "Reported error: " in source, (
            "the page does not surface the backend's reason")
        assert "last_error" in source

        print("PASS  an offline camera renders OFFLINE, keeps its last "
              "measurement, and shows the reported reason")
    finally:
        teardown(client, directory)


def test_no_camera_registered_is_shown_as_such_rather_than_as_healthy():
    client, directory = make_client()
    try:
        body = client.get(ENDPOINT).json()
        assert body["items"] == []

        assert 'id="cameras-empty"' in HTML
        assert "No cameras registered" in HTML

        source = camera_health_source()
        assert "there is no health to report" in source, (
            "an installation with no camera is not told so")

        print("PASS  no registered device is reported as such, not as healthy")
    finally:
        teardown(client, directory)


# -- status is not colour alone ---------------------------------------------

def test_status_is_carried_by_a_symbol_as_well_as_a_colour():
    source = camera_health_source()

    assert "CAMERA_SYMBOL" in source, "there are no status symbols"
    for status in ("online", "offline", "degraded", "unknown"):
        assert status in source, "no symbol entry for %r" % status

    # The text of the status is rendered too, not only the swatch.
    assert "raw.toUpperCase()" in source, (
        "the status value itself is not rendered as text")
    assert 'title: "Reported status: "' in source, (
        "the pill carries no accessible title")
    assert '"aria-hidden": "true"' in source, (
        "the decorative symbol is not hidden from assistive technology")

    print("PASS  status is a symbol plus text plus colour, never colour alone")


def test_the_status_styles_reuse_the_existing_palette():
    """A healthy camera should read like a present student, in both themes."""
    for status in ("online", "offline", "degraded", "unknown"):
        assert ".pill-cam-%s" % status in CSS, "no style for %r" % status

    block = CSS[CSS.index(".pill-cam-online"):CSS.index(".pill-cam-unknown")]
    for token in ("var(--present)", "var(--absent)", "var(--late)"):
        assert token in block, (
            "%s is hard-coded rather than reusing %s" % ("a colour", token))

    assert "#" not in block, (
        "the camera status styles contain a hard-coded hex colour, so they "
        "will not follow the dark palette")

    print("PASS  the four indicators reuse the existing status tokens, no hex "
          "colours")


# -- polling ----------------------------------------------------------------

def test_polling_starts_on_the_page_and_stops_when_it_is_left():
    assert 'if (view === "cameras") startCameraPolling();' in JS, (
        "polling does not start when the page is opened")
    assert "else stopCameraPolling();" in JS, (
        "polling does not stop when the page is left")

    # Leaving the dashboard entirely stops it too.
    assert "stopCameraPolling();" in JS[JS.index("function handleUnauthorized"):
                                        JS.index("function handleUnauthorized")
                                        + 700], (
        "a signed-out session leaves the camera timer running")

    print("PASS  polling starts on the page, stops when it is left, and stops "
          "on sign-out")


def test_polling_cannot_be_duplicated():
    source = camera_health_source()

    start = source[source.index("function startCameraPolling"):
                   source.index("function stopCameraPolling")]

    assert "stopCameraPolling();" in start, (
        "startCameraPolling does not clear an existing timer first, so "
        "navigating away and back would leave two running")

    stop = source[source.index("function stopCameraPolling"):]
    assert "clearInterval" in stop
    assert "state.cameraTimer = null" in stop, (
        "the timer handle is not cleared, so it cannot be stopped twice safely")

    assert "cameraTimer: null" in JS, (
        "there is no single timer slot on the shared state object")

    print("PASS  the timer is cleared before it is set, and nulled when "
          "stopped")


def test_returning_to_the_page_fetches_fresh_data():
    """switchView calls refresh(), which dispatches to loadCameras()."""
    switch = JS[JS.index("function switchView"):
                JS.index("async function refresh()")]

    assert 'if (view === "cameras") startCameraPolling();' in switch
    assert "refresh();" in switch, (
        "switchView does not refresh, so returning would show stale data "
        "until the first poll")

    assert 'else if (state.view === "cameras") await loadCameras();' in JS, (
        "refresh() does not load camera health")

    print("PASS  returning to the page refreshes immediately, then resumes "
          "polling")


def test_the_poll_interval_is_reasonable():
    source = camera_health_source()
    match = re.search(r"const CAMERA_POLL_MS = (\d+);", source)
    assert match, "the poll interval is not a named constant"

    interval = int(match.group(1))
    assert 5000 <= interval <= 10000, (
        "the camera page polls every %dms, outside the intended 5-10s"
        % interval)

    print("PASS  the page polls every %.0fs, from a named constant"
          % (interval / 1000))


# -- what the page must not do ---------------------------------------------

def test_the_page_opens_no_camera_and_shows_no_picture():
    source = camera_health_source()

    for banned in ("getUserMedia", "mediaDevices", "<canvas", "canvas",
                   "frame.jpg", "stream.mjpg", "createElement(\"img\")"):
        assert banned not in source, (
            "the camera health page uses %r - it must display the backend's "
            "health data, not open a camera or show a frame" % banned)

    # The camera health view, and only it.
    #
    # This slice used to end at the word ADMINISTRATION, which is five views
    # further down the document: reports, assembly, the assistant and academic
    # terms were all inside the bound, and the assertion below was quietly
    # being made about every one of them. It passed for as long as none of
    # them held a picture, and stopped passing the moment a view with a camera
    # panel was declared there - which is a true fact about that view and
    # nothing at all about this page. The bound is now the next view section,
    # whichever view that is.
    start = HTML.index('id="view-cameras"')
    section = HTML[start:HTML.index('<section id="view-', start)]
    assert "<img" not in section, (
        "the camera health page contains an image element")
    assert "<video" not in section

    print("PASS  the page opens no camera, holds no image element, and "
          "requests no frame")


def test_the_page_adds_no_external_request():
    """Local appliance: nothing on this page may reach off the machine."""
    source = camera_health_source()

    for banned in ("http://", "https://", "//cdn", "fonts.googleapis"):
        assert banned not in source, (
            "the camera health page references %r" % banned)

    print("PASS  the camera health page makes no external request")


def test_the_page_carries_nothing_biometric():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={
            "label": "Classroom Camera 01", "classroom": "Lab 2",
            "camera_index": 0})
        client.post("/api/cameras/CAM-01/heartbeat", json={"fps": 12.0})

        body = client.get(ENDPOINT).text.lower()
        for word in ("embedding", "vector", "descriptor", "student", "face",
                     "photo", "crop", "npz", "password", "token"):
            assert word not in body, (
                "the camera response carries %r" % word)

        source = camera_health_source()
        for word in ("embedding", "vector", "student_id"):
            assert word not in source, (
                "the camera page references %r" % word)

        print("PASS  the response and the page carry no identity and nothing "
              "biometric")
    finally:
        teardown(client, directory)


def test_long_identifiers_cannot_overflow_the_page():
    assert ".cam-headline p { overflow-wrap: anywhere; }" in CSS or \
        "overflow-wrap: anywhere" in CSS, (
        "a long device id has nothing stopping it widening the page")
    assert "#cameras-table code { overflow-wrap: anywhere; }" in CSS
    assert 'class="table-scroll" id="cameras-table-wrap"' in HTML, (
        "the devices table is not in a horizontal scroll container")

    print("PASS  long ids wrap and the table scrolls rather than overflowing")


def test_the_dashboard_still_serves_its_own_source():
    """The byte-level guard the other suites rely on, checked here too."""
    client, directory = make_client()
    try:
        assert client.get("/").text == HTML, (
            "the served page differs from the source file - a line-ending "
            "change is the usual cause")
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200

        print("PASS  the served dashboard still matches its source byte for "
              "byte")
    finally:
        teardown(client, directory)


def test_the_existing_views_are_untouched():
    for view in ("view-overview", "view-sessions", "view-students",
                 "view-live", "view-admin"):
        assert 'id="%s"' % view in HTML, "the %s view disappeared" % view
        assert '$("%s").hidden = view !== ' % view in JS, (
            "the %s view is no longer switched" % view)

    for tab in ("overview", "sessions", "students", "live", "admin"):
        assert 'data-view="%s"' % tab in HTML, "the %s tab disappeared" % tab

    print("PASS  all five existing views and tabs are still present and "
          "switched")


def test_camera_health_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        assert Path(state.get_database().path).parent != real.parent
        client.put("/api/cameras/CAM-01", json={"label": "Front"})
        client.get(ENDPOINT)
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_camera_health_is_a_tab_in_the_existing_navigation,
    test_the_view_has_semantic_headings_and_a_labelled_refresh,
    test_the_page_calls_the_endpoint_that_already_existed,
    test_the_endpoint_returns_exactly_the_fields_the_page_reads,
    test_the_page_invents_no_field,
    test_there_is_a_loading_state_and_it_is_not_left_showing,
    test_a_failed_request_shows_an_error_with_a_retry,
    test_an_unauthenticated_caller_is_refused_by_the_backend,
    test_a_registered_camera_nobody_has_heard_from_reads_unknown,
    test_an_offline_camera_renders_as_offline_with_the_backend_s_reason,
    test_no_camera_registered_is_shown_as_such_rather_than_as_healthy,
    test_status_is_carried_by_a_symbol_as_well_as_a_colour,
    test_the_status_styles_reuse_the_existing_palette,
    test_polling_starts_on_the_page_and_stops_when_it_is_left,
    test_polling_cannot_be_duplicated,
    test_returning_to_the_page_fetches_fresh_data,
    test_the_poll_interval_is_reasonable,
    test_the_page_opens_no_camera_and_shows_no_picture,
    test_the_page_adds_no_external_request,
    test_the_page_carries_nothing_biometric,
    test_long_identifiers_cannot_overflow_the_page,
    test_the_dashboard_still_serves_its_own_source,
    test_the_existing_views_are_untouched,
    test_camera_health_tests_never_touch_the_real_database,
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
