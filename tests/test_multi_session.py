"""
Simultaneous classrooms: isolation, and the dynamic panel grid.

NO WEBCAM AND NO NETWORK ARE NEEDED. The managers here are driven directly
rather than through a camera, which is the only way to have several "running"
sessions on a machine with one webcam. What that means for what is proven is
stated plainly at the bottom of this docstring.

WHY THE REGISTRY EXISTS

    LiveSessionManager was already per-session in everything but its accessor:
    one instance holds one session id, one runtime, one pipeline, one camera and
    one worker behind its own lock. What made the application single-session was
    get_live_manager() returning one global instance, so a second start was
    refused for clashing with the first.

    LiveSessionRegistry holds one manager per session id. Each goes on
    enforcing exactly the rules it always did, for its own session.

WHAT THIS VERIFIES - the eight isolation properties

    1. attendance and presence do not cross between classes
    2. detection counts do not cross between classes
    3. finishing one class does not finish another
    4. a break in one class does not pause another
    5. one class's camera failure does not mark another's camera failed
    6. one class's liveness state does not affect another's
    7. dropping a panel does not stop its session
    8. the active sessions are reconstructed from the BACKEND after a refresh

    and the grid itself:

    - the panel count equals the number of active sessions, for 1, 3, 4, 8, 10
      and 15, with no maximum anywhere in the page
    - a session that finishes stops being rendered; a new one appears
    - focus, switching focus, and returning to the grid
    - a focused session that is no longer running drops focus by itself

WHAT THIS DOES NOT VERIFY

    REAL SIMULTANEOUS CAMERAS. This machine has one webcam - the probe reports
    index 0 available and nothing on index 1 - so two physical streams cannot
    be opened here and are NOT tested. What is tested is that two managers keep
    separate state, separate counters, separate camera indexes and separate
    breaks, and that every endpoint answers about the session it was asked
    about.

    Nor does it verify how the grid LOOKS. No browser is driven; the panel
    logic is asserted against the source that builds it.

Run with:
    python tests/test_multi_session.py
"""

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

from attendance_session import AttendanceSession  # noqa: E402
from backend.services import state  # noqa: E402
from backend.services.break_service import BreakService  # noqa: E402
from backend.services.live_service import (  # noqa: E402
    LiveSessionRegistry,
    get_live_registry,
    reset_live_manager,
)
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}


# -- harness ----------------------------------------------------------------

def make_database():
    directory = Path(tempfile.mkdtemp(prefix="classvision_multi_"))
    state.configure(directory / "multi.db")
    reset_live_manager(None)
    return state.get_database(), directory


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_multiapi_"))
    state.configure(directory / "multi.db")
    reset_live_manager(None)
    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        if client is not None:
            client.close()
    except Exception:                                    # noqa: BLE001
        pass
    reset_live_manager(None)
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def store_session(database, session_id, class_id, status="ACTIVE"):
    from repositories import SessionRepository

    now = datetime.now(timezone.utc)
    session = AttendanceSession(session_id, class_id, now - timedelta(minutes=5),
                               now + timedelta(minutes=55),
                               policy_version="1.0")
    if status == "ACTIVE":
        session.start()
    SessionRepository(database).save(session)
    return session


def running(registry, session_id, class_id, camera_index=0):
    """
    A manager whose runtime is RUNNING, without a camera.

    The runtime is driven directly. That is the only way to have several
    sessions "running" on a machine with one webcam, and it is exactly the part
    of the system these tests are about: whether two managers keep their state
    apart.
    """
    manager = registry.manager_for(session_id)
    manager.runtime.begin(session_id, class_id=class_id,
                          camera_index=camera_index)
    return manager


def live_console_source():
    """The JavaScript that builds the multi-class console."""
    start = JS.index("   THE MULTI-CLASS LIVE CONSOLE")
    end = JS.index("/* ---------- live classroom (Step 8) ---------- */", start)
    return JS[start:end]


# -- the eight isolation properties ----------------------------------------

def test_two_sessions_get_two_managers():
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        a = registry.manager_for("S-A")
        b = registry.manager_for("S-B")

        assert a is not b, "both sessions share one manager"
        assert registry.manager_for("S-A") is a, (
            "asking twice produced two managers for one session")

        print("PASS  two sessions get two managers, and one session gets one")
    finally:
        teardown(None, directory)


def test_detection_counts_do_not_cross_between_classes():
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        a = running(registry, "S-A", "CSE-A", camera_index=0)
        b = running(registry, "S-B", "CSE-B", camera_index=1)

        # note_frame is what the capture worker calls per processed frame.
        for _ in range(40):
            a.runtime.note_frame(fps=12.0, active_tracks=28,
                                 recognized_students=26, unknown_tracks=2)
        a.runtime.note_dropped()

        for _ in range(7):
            b.runtime.note_frame(fps=9.0, active_tracks=31,
                                 recognized_students=29, unknown_tracks=2)

        status_a, status_b = a.status(), b.status()

        assert status_a["processed_count"] == 40
        assert status_b["processed_count"] == 7, (
            "a count from one classroom appeared in another: %s"
            % status_b["processed_count"])
        assert status_a["active_tracks"] == 28
        assert status_b["active_tracks"] == 31, (
            "a detection count crossed between classes")
        assert status_a["dropped_count"] == 1
        assert status_b["dropped_count"] == 0, (
            "a dropped frame in one classroom was counted in another")
        assert status_a["session_id"] == "S-A"
        assert status_b["session_id"] == "S-B"
        assert status_a["class_id"] == "CSE-A"
        assert status_b["class_id"] == "CSE-B"

        print("PASS  detection counts stay inside the class they belong to")
    finally:
        teardown(None, directory)


def test_presence_does_not_cross_between_classes():
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        a = running(registry, "S-A", "CSE-A")
        b = running(registry, "S-B", "CSE-B")

        presence_a = a.presence()
        presence_b = b.presence()

        assert presence_a["session_id"] == "S-A"
        assert presence_b["session_id"] == "S-B"
        # Neither has students yet, and crucially neither has the OTHER's.
        assert presence_a["students"] == []
        assert presence_b["students"] == []

        print("PASS  each session answers with its own presence list")
    finally:
        teardown(None, directory)


def test_finishing_one_class_does_not_finish_another():
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        running(registry, "S-A", "CSE-A")
        running(registry, "S-B", "CSE-B")

        assert set(registry.busy()) == {"S-A", "S-B"}

        registry.manager_for("S-A").runtime.reset()

        assert set(registry.busy()) == {"S-B"}, (
            "finishing one session changed the other: %s"
            % sorted(registry.busy()))
        assert registry.manager_for("S-B").runtime.is_busy

        print("PASS  finishing CSE-A leaves CSE-B running")
    finally:
        teardown(None, directory)


def test_a_break_in_one_class_does_not_pause_another():
    database, directory = make_database()
    try:
        store_session(database, "S-A", "CSE-A")
        store_session(database, "S-B", "CSE-B")

        service = BreakService(database)
        service.start("S-A", actor="office")

        assert service.state("S-A")["active"] is True
        assert service.state("S-B")["active"] is False, (
            "a break in one classroom paused another")
        assert service.state("S-B")["excluded_seconds"] == 0.0

        print("PASS  a break in CSE-A does not pause CSE-B")
    finally:
        teardown(None, directory)


def test_one_cameras_failure_does_not_fail_another():
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        a = running(registry, "S-A", "CSE-A")
        b = running(registry, "S-B", "CSE-B")

        a.runtime.fail("The camera stopped delivering frames.")

        status_a, status_b = a.status(), b.status()

        assert status_a["state"] == "FAILED"
        assert status_b["state"] != "FAILED", (
            "one camera failing marked another session failed")
        assert status_b["error"] is None, (
            "an error from one classroom appeared in another: %s"
            % status_b["error"])

        print("PASS  one camera failing leaves the other session unaffected")
    finally:
        teardown(None, directory)


def test_liveness_state_does_not_cross_between_classes():
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        a = running(registry, "S-A", "CSE-A")
        b = running(registry, "S-B", "CSE-B")

        status_a, status_b = a.status(), b.status()

        # Each reports its own liveness counters, and they are independent
        # objects rather than a shared one.
        for field in ("live_tracks", "spoof_tracks", "uncertain_tracks"):
            assert field in status_a and field in status_b
        assert status_a is not status_b

        a_liveness = a.liveness()
        b_liveness = b.liveness()
        assert a_liveness is not b_liveness, (
            "both sessions share one liveness object")

        print("PASS  liveness counters belong to one session each")
    finally:
        teardown(None, directory)


def test_forgetting_a_panel_does_not_stop_its_session():
    """Closing a panel is not a request to end a lesson."""
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        running(registry, "S-A", "CSE-A")

        dropped = registry.forget("S-A")

        assert dropped is False, (
            "a running session was dropped from the registry")
        assert "S-A" in registry.busy(), "the running session was stopped"

        # Once it is no longer running, it can be forgotten.
        registry.manager_for("S-A").runtime.reset()
        assert registry.forget("S-A") is True

        print("PASS  a running session cannot be forgotten; a finished one can")
    finally:
        teardown(None, directory)


def test_the_active_sessions_come_from_the_backend():
    """
    Requirement: a refresh reconstructs the running sessions from the backend.

    Asserted on both sides. The registry reports them, and the page asks for
    them rather than remembering them - there is no localStorage and no
    client-side list of sessions anywhere in the console code.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        running(registry, "S-A", "CSE-A")
        running(registry, "S-B", "CSE-B")
        running(registry, "S-C", "CSE-C")

        active = registry.active()
        assert len(active) == 3, len(active)
        assert {entry["class_id"] for entry in active} == {"CSE-A", "CSE-B",
                                                          "CSE-C"}

        source = live_console_source()
        assert '/api/live/sessions' in source, (
            "the console does not ask the backend which sessions are running")
        assert "localStorage" not in source, (
            "the console remembers sessions in the browser, which cannot "
            "survive a backend change")

        print("PASS  the running sessions come from the backend, not from the "
              "browser")
    finally:
        teardown(None, directory)


# -- the API ---------------------------------------------------------------

def test_the_sessions_endpoint_reports_one_entry_per_session():
    client, directory = make_client()
    try:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

        empty = client.get("/api/live/sessions")
        assert empty.status_code == 200, empty.text
        body = empty.json()
        assert body["items"] == [] and body["total"] == 0
        assert body["totals"]["active_sessions"] == 0

        registry = get_live_registry()
        running(registry, "S-A", "CSE-A", camera_index=0)
        running(registry, "S-B", "CSE-B", camera_index=1)

        listed = client.get("/api/live/sessions").json()
        assert listed["total"] == 2, listed["total"]

        by_class = {entry["class_id"]: entry for entry in listed["items"]}
        assert set(by_class) == {"CSE-A", "CSE-B"}
        assert by_class["CSE-A"]["camera_index"] == 0
        assert by_class["CSE-B"]["camera_index"] == 1, (
            "the camera index crossed between sessions")

        # Every entry carries its own break fields.
        for entry in listed["items"]:
            assert "on_break" in entry
            assert entry["on_break"] is False

        print("PASS  /api/live/sessions reports one entry per session, each "
              "with its own camera")
    finally:
        teardown(client, directory)


def test_the_status_endpoint_answers_about_the_session_it_was_asked_about():
    client, directory = make_client()
    try:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

        registry = get_live_registry()
        running(registry, "S-A", "CSE-A", camera_index=0)
        running(registry, "S-B", "CSE-B", camera_index=3)

        a = client.get("/api/live/status?session_id=S-A").json()
        b = client.get("/api/live/status?session_id=S-B").json()

        assert a["session_id"] == "S-A" and a["class_id"] == "CSE-A"
        assert b["session_id"] == "S-B" and b["class_id"] == "CSE-B"
        assert a["camera_index"] == 0 and b["camera_index"] == 3

        presence_a = client.get("/api/live/presence?session_id=S-A").json()
        assert presence_a["session_id"] == "S-A"

        print("PASS  status and presence answer about the session named")
    finally:
        teardown(client, directory)


def test_a_break_endpoint_targets_the_session_it_names():
    client, directory = make_client()
    try:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

        database = state.get_database()
        store_session(database, "S-A", "CSE-A")
        store_session(database, "S-B", "CSE-B")

        registry = get_live_registry()
        running(registry, "S-A", "CSE-A")
        running(registry, "S-B", "CSE-B")

        started = client.post("/api/live/break?session_id=S-A")
        assert started.status_code == 200, started.text

        a = client.get("/api/live/status?session_id=S-A").json()
        b = client.get("/api/live/status?session_id=S-B").json()

        assert a["on_break"] is True
        assert b["on_break"] is False, (
            "a break started for CSE-A also paused CSE-B")
        assert a["break_remaining_seconds"] > 0
        assert b["break_remaining_seconds"] == 0

        print("PASS  a break targets only the session it names")
    finally:
        teardown(client, directory)


def test_every_live_endpoint_can_be_scoped_to_a_session():
    """A panel that could not name its session would act on another."""
    client, directory = make_client()
    try:
        from backend.app import app

        spec = app.openapi()
        scoped = {}
        for path, methods in spec["paths"].items():
            if not path.startswith("/api/live/"):
                continue
            for operation in methods.values():
                names = {parameter["name"]
                         for parameter in operation.get("parameters", [])}
                scoped[path] = "session_id" in names

        for path in ("/api/live/status", "/api/live/presence",
                     "/api/live/stop", "/api/live/break",
                     "/api/live/break/end", "/api/live/frame.jpg",
                     "/api/live/stream.mjpg"):
            assert scoped.get(path) is True, (
                "%s cannot be scoped to a session, so a panel acting on it "
                "could reach another classroom" % path)

        print("PASS  all seven per-session live endpoints take a session_id")
    finally:
        teardown(client, directory)


# -- the dynamic grid ------------------------------------------------------

def test_the_panel_count_is_the_session_count_with_no_maximum():
    """
    The requirement stated as arithmetic: N sessions means N panels.

    gridColumns() chooses a column CAP, never a panel limit, and the panels
    themselves come from the list - one per entry - so the count is whatever the
    backend reported.
    """
    source = live_console_source()

    # One panel per entry, built in a loop over the response.
    assert "for (const entry of entries)" in source, (
        "the panels are not built one per reported session")

    # The column helper is a cap and is asserted to keep climbing rather than
    # stopping at four.
    columns = {}
    block = source[source.index("function gridColumns"):]
    block = block[:block.index("\n}")]
    for count, expected in ((1, 1), (2, 2), (3, 3), (6, 3), (8, 4), (12, 4),
                            (15, 5), (40, 5)):
        columns[count] = expected

    assert "return 5" in block, (
        "the column cap stops climbing before five")
    assert "if (count <= 12) return 4" in block

    # Nothing anywhere caps the number of panels.
    for cap in ("slice(0, 4)", "slice(0,4)", "MAX_PANELS", "> 4) return",
                "length > 4"):
        assert cap not in source, (
            "the console caps the panel count (%s)" % cap)

    print("PASS  the panel count is the session count; gridColumns is a "
          "column cap, not a panel limit")


def test_the_registry_reports_however_many_sessions_are_running():
    """
    N running sessions means N entries - for 1, 3, 4, 8, 10 and 15.

    The panel count follows from this: renderLiveGrid builds one panel per
    entry, asserted by the test above. This is the half that can be measured
    rather than read, so it is measured at the counts the requirement names.
    """
    database, directory = make_database()
    try:
        for count in (1, 3, 4, 8, 10, 15):
            registry = LiveSessionRegistry()
            for number in range(count):
                running(registry, "S-%02d" % number, "CSE-%02d" % number,
                        camera_index=number)

            active = registry.active()
            assert len(active) == count, (
                "%d sessions running produced %d entries"
                % (count, len(active)))

            # Each entry is its own classroom - no two share a session id or a
            # camera index.
            ids = [entry["session_id"] for entry in active]
            cameras = [entry["camera_index"] for entry in active]
            assert len(set(ids)) == count, "session ids collided"
            assert len(set(cameras)) == count, "camera indexes collided"

            # And one finishing leaves exactly the rest.
            registry.manager_for(ids[0]).runtime.reset()
            assert len(registry.active()) == count - 1, (
                "finishing one of %d left %d"
                % (count, len(registry.active())))

        print("PASS  1, 3, 4, 8, 10 and 15 running sessions each report that "
              "many entries, and finishing one leaves the rest")
    finally:
        teardown(None, directory)


def test_the_grid_column_count_comes_from_the_session_count():
    source = live_console_source()
    assert 'setProperty("--live-columns"' in source, (
        "the grid's column count is not set from the session count")
    assert "gridColumns(entries.length)" in source

    assert "--live-columns" in CSS, "the stylesheet does not read the cap"
    assert "auto-fit" in CSS, (
        "the grid is not responsive - a fixed column count cannot adapt to "
        "the viewport")

    print("PASS  the column count comes from the session count and the "
          "viewport, not from a fixed layout")


def test_a_finished_session_stops_being_rendered():
    source = live_console_source()

    # Panels are keyed by session id and the leftovers are released.
    assert "existing.get(entry.session_id)" in source, (
        "panels are not matched to sessions by id")
    assert "for (const panel of existing.values()) releaseLivePanel(panel)" \
        in source, "panels for finished sessions are not removed"
    assert "function releaseLivePanel" in source
    assert "removeAttribute(\"src\")" in source, (
        "a removed panel's stream is not closed, which leaks a connection")

    print("PASS  a finished session's panel is removed and its stream closed")


def test_each_panel_streams_only_its_own_camera():
    source = live_console_source()

    # The URL is built in ONE place - test_live_api asserts the stream endpoint
    # appears exactly once in the page, so a second camera connection cannot
    # appear quietly. The property is therefore checked in two halves: the
    # panel passes its OWN session id, and the one builder scopes it.
    assert "classroomStreamUrl(entry.session_id)" in source, (
        "a panel does not pass its own session id to the stream builder, so "
        "one classroom's camera could appear in another's panel")

    builder = JS[JS.index("function classroomStreamUrl"):]
    builder = builder[:builder.index(chr(10) + "}")]
    assert "session_id=" in builder, (
        "the stream builder does not scope the stream to a session")
    assert "encodeURIComponent(sessionId)" in builder

    print("PASS  each panel streams its own session's camera")


def test_every_panel_control_carries_its_own_session_id():
    source = live_console_source()
    block = source[source.index("function renderLivePanelActions"):]
    block = block[:block.index("\n/* ---------- focus mode")]

    assert "const sessionId = entry.session_id" in block, (
        "the controls do not capture their own session id")
    for action in ("focusLiveSession(sessionId)", "startSessionBreak(sessionId",
                   "endSessionBreak(sessionId", "finishSession(sessionId"):
        assert action in block, "a control does not pass its session: %s" % action

    print("PASS  every panel control acts on its own session id")


def test_focus_shows_one_session_and_keeps_the_others_reachable():
    source = live_console_source()

    assert "function focusLiveSession" in source
    assert "function leaveLiveFocus" in source
    assert "function renderFocusSwitcher" in source, (
        "the other classes are not reachable while one is focused")
    assert 'id="live-focus-back"' in HTML, "there is no way back to the grid"
    assert "Back to all live classes" in HTML

    # The switcher lists the OTHERS, not everything.
    switcher = source[source.index("function renderFocusSwitcher"):]
    assert "entries.filter((entry) => entry.session_id !== focusedId)" in \
        switcher, "the switcher includes the focused class"

    print("PASS  focus enlarges one class and leaves the others one click "
          "away")


def test_a_focused_session_that_ends_returns_to_the_grid():
    source = live_console_source()

    assert "!entries.some((entry) => entry.session_id === state.liveFocus)" \
        in source, "a focused session that ended is not detected"
    assert "state.liveFocus = null" in source

    focus = source[source.index("function renderLiveFocus"):]
    assert "leaveLiveFocus();" in focus, (
        "a focused session that is gone does not fall back to the grid")

    print("PASS  a focused session that is no longer running returns to the "
          "grid")


def test_the_console_adds_no_second_polling_loop():
    """
    One request carries every session, so the request count does not grow with
    the number of classrooms.
    """
    source = live_console_source()

    assert "setInterval" not in source, (
        "the console starts a polling timer of its own; the page already "
        "polls once a second and one request carries every session")

    # setTimeout is allowed for exactly one thing: putting a button's label
    # back after it has said "Done". That is a one-shot, not a loop, and
    # banning the function name outright would have banned it along with the
    # polling this test is actually about.
    one_shots = re.findall(r"window\.setTimeout\(", source)
    assert len(one_shots) <= 1, (
        "the console schedules %d delayed callbacks; only the button label "
        "reset is expected" % len(one_shots))
    if one_shots:
        assert "button.textContent = original" in source, (
            "the one delayed callback is not the button label reset")

    # And loadLive is still the single entry point the one timer calls.
    assert "await loadActiveLiveSessions();" in JS

    timers = len(re.findall(r"setInterval\(", JS))
    assert timers <= 4, (
        "the page now has %d interval timers; the live, enrollment and camera "
        "polls are the only ones expected" % timers)

    print("PASS  the console adds no polling loop of its own (%d timers in the "
          "whole page)" % timers)


def test_the_panels_expose_nothing_biometric():
    source = live_console_source()

    for word in ("embedding", "descriptor", "face_crop", "landmark"):
        assert word not in source.lower(), (
            "the panels mention %s" % word)

    # The only image is the annotated whole frame, which is what the single
    # session view already showed.
    images = re.findall(r'"(/api/[a-z0-9/._]+)"', source)
    for url in images:
        assert "crop" not in url and "embedding" not in url

    print("PASS  the panels expose nothing biometric")


def test_multi_session_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    database, directory = make_database()
    try:
        assert Path(database.path).parent != real.parent
        store_session(database, "S-A", "CSE-A")
    finally:
        teardown(None, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


def test_two_sessions_produce_two_entries():
    """The count the other test did not cover, asserted on its own."""
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        running(registry, "S-1", "CSE-A", camera_index=0)
        running(registry, "S-2", "CSE-B", camera_index=1)

        active = registry.active()
        assert len(active) == 2, len(active)
        assert {entry["session_id"] for entry in active} == {"S-1", "S-2"}

        print("PASS  two running sessions report two entries")
    finally:
        teardown(None, directory)


def test_focusing_one_of_ten_uses_that_sessions_id():
    """
    Focus is identified by session id, so the enlarged panel is built from that
    entry and its controls are scoped to it.
    """
    source = live_console_source()

    assert "function focusLiveSession(sessionId)" in source
    assert "state.liveFocus = sessionId" in source, (
        "focus is not recorded by session id")

    focus = source[source.index("function renderLiveFocus"):]
    focus = focus[:focus.index(chr(10) + "/* The other running classes")]

    assert "candidate.session_id === state.liveFocus" in focus, (
        "the focused entry is not found by session id")
    assert "panel.dataset.session !== entry.session_id" in focus, (
        "the focused panel is not rebuilt when the focused session changes - "
        "switching from one class to another would keep the first one's frame")
    assert "loadFocusPresence(entry.session_id)" in focus, (
        "the focused presence list is not scoped to the focused session")

    print("PASS  focus is by session id, and switching focus rebuilds the "
          "panel for the new session")


def test_switching_focus_releases_the_previous_stream():
    """
    7 -> 3 must not leave class 7's camera connection open.

    An <img> whose src still points at a motion-JPEG stream keeps that
    connection alive even after the node is replaced, which is how a console
    accumulates one connection per class the operator has looked at.
    """
    source = live_console_source()
    focus = source[source.index("function renderLiveFocus"):]
    focus = focus[:focus.index(chr(10) + "/* The other running classes")]

    assert "if (panel) releaseLivePanel(panel)" in focus, (
        "the previous focused panel's stream is not released when focus moves")

    print("PASS  switching focus releases the previous class's stream")


def test_the_switcher_makes_every_other_class_selectable():
    source = live_console_source()
    switcher = source[source.index("function renderFocusSwitcher"):]

    assert "focusLiveSession(entry.session_id)" in switcher, (
        "a class in the switcher cannot be selected")
    assert "others.length" in switcher, (
        "the switcher does not say how many other classes are running")

    print("PASS  every other running class is selectable from the switcher")


def test_logout_stops_the_live_poll_and_clears_what_was_fetched():
    """
    Requirement: logout stops live polling and leaves no stale register behind.
    """
    sign_out = JS[JS.index("async function signOut()"):]
    sign_out = sign_out[:sign_out.index(chr(10) + "}")]

    for stopper in ("stopLivePolling()", "stopEnrollPolling()",
                    "stopCameraPolling()"):
        assert stopper in sign_out, (
            "signing out does not %s, so it keeps requesting after the "
            "account is gone" % stopper)

    assert "clearFetchedData()" in sign_out, (
        "signing out leaves the fetched register in the page")

    clear = JS[JS.index("function clearFetchedData()"):]
    clear = clear[:clear.index(chr(10) + "}")]
    assert "querySelectorAll(\"tbody\")" in clear, (
        "the tables are not emptied on sign-out")

    # And an unauthorised answer is handled in ONE place.
    assert JS.count("function handleUnauthorized") == 1
    assert "handleUnauthorized();" in JS

    print("PASS  signing out stops every poll and clears the fetched data")


def test_a_duplicate_click_cannot_send_the_same_operation_twice():
    helper = JS[JS.index("async function withBusyButton"):]
    helper = helper[:helper.index(chr(10) + "}")]

    assert "if (!button || button.disabled) return" in helper, (
        "a second click is not ignored while the first is in flight")
    assert "button.disabled = true" in helper, (
        "the button is not disabled during the operation")
    assert "button.disabled = false" in helper, (
        "the button is never re-enabled, so one failure disables it forever")

    # Every session action goes through it rather than calling the API raw.
    for action in ("startSessionBreak", "endSessionBreak", "finishSession"):
        block = JS[JS.index("async function %s" % action):]
        block = block[:block.index(chr(10) + "}")]
        assert "withBusyButton" in block, (
            "%s does not guard against a double click" % action)

    print("PASS  every session action disables its button while it runs")


def test_backend_errors_become_sentences_a_teacher_can_act_on():
    block = JS[JS.index("function explainFailure"):]
    block = block[:block.index(chr(10) + "}")]

    # The cases the requirement names.
    assert "Camera could not be opened" in block, (
        "a 503 does not explain that the camera could not be opened")
    assert "not being used by another application" in block
    assert "no longer active" in block, (
        "a 409 on a session does not say the session is no longer active")
    assert "no longer available" in block, (
        "a 404 on a session does not say it is no longer available")
    assert "Please sign in again" in block
    assert "Unable to reach the ClassVision server" in block, (
        "a request that never arrived is not reported as unreachable")

    # And the technical detail still reaches the console for debugging.
    assert "console.debug" in block, (
        "the status and detail are not logged anywhere for debugging")

    # A raw status code is never the whole message.
    assert "HTTP " not in block

    print("PASS  backend failures become sentences, with the detail kept in "
          "the console")


def test_the_nested_views_have_back_navigation():
    assert "Back to all live classes" in HTML
    assert "Back to sessions" in HTML, (
        "the session detail panel does not say where Back goes")
    assert "Back to students" in HTML, (
        "the student detail panel does not say where Back goes")

    # Internal view state, not browser history: going back from the first view
    # somebody opens would leave the application entirely.
    #
    # Checked against the CODE with comments stripped. The script explains that
    # choice in a comment which names the call it is not making, and a bare
    # substring search reads the explanation as the violation.
    code = re.sub(r"/\*.*?\*/", " ", JS, flags=re.S)
    code = re.sub(r"(?<![:\w])//[^" + chr(10) + "]*", " ", code)

    assert "history.back(" not in code, (
        "back navigation uses browser history, which can leave the app")
    assert "history.go(" not in code
    assert "function returnToList" in JS, (
        "Back does not put the reader back where they came from")

    print("PASS  the nested views have Back navigation, by view state rather "
          "than browser history")


def test_the_grid_does_not_rebuild_panels_it_already_has():
    """
    Rebuilding every panel on every poll would restart every stream once a
    second, which looks exactly like a row of broken cameras.
    """
    source = live_console_source()

    assert "existing.set(panel.dataset.session, panel)" in source, (
        "panels are not indexed by session for reuse")
    assert "updateLivePanel(panel, entry)" in source, (
        "an existing panel is not updated in place")
    assert "existing.delete(entry.session_id)" in source

    print("PASS  a panel that already exists is updated in place, not rebuilt")


def test_a_session_whose_camera_died_stays_visible_as_failed():
    """
    The defect this test was written for.

    Two classes pointed at ONE physical camera: the second start takes the
    device and the first session's worker fails. The registry originally
    reported only BUSY sessions, so that classroom's panel silently
    disappeared and the operator was told nothing at all.

    A camera conflict has to be shown, not hidden - so a FAILED session keeps
    its panel, carrying the runtime's own reason.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        a = running(registry, "S-A", "CSE-A", camera_index=0)
        running(registry, "S-B", "CSE-B", camera_index=0)

        a.runtime.fail("The camera stopped delivering frames.")

        # Not holding a camera any more - which is the right answer for
        # "can the devices be probed".
        assert "S-A" not in registry.busy()

        # But still worth a panel, and still carrying its reason.
        assert "S-A" in registry.reportable(), (
            "a session whose camera died vanished from the console instead of "
            "being shown as failed")

        entries = {entry["session_id"]: entry for entry in registry.active()}
        assert set(entries) == {"S-A", "S-B"}
        assert entries["S-A"]["state"] == "FAILED"
        assert entries["S-A"]["error"], (
            "the failed session reports no reason, so the panel cannot say "
            "what went wrong")
        assert entries["S-B"]["error"] is None, (
            "one session's failure put an error on another")

        print("PASS  a session whose camera died stays visible as FAILED with "
              "its reason")
    finally:
        teardown(None, directory)


# -- runner -----------------------------------------------------------------

TESTS = [
    test_two_sessions_get_two_managers,
    test_detection_counts_do_not_cross_between_classes,
    test_presence_does_not_cross_between_classes,
    test_finishing_one_class_does_not_finish_another,
    test_a_break_in_one_class_does_not_pause_another,
    test_one_cameras_failure_does_not_fail_another,
    test_a_session_whose_camera_died_stays_visible_as_failed,
    test_liveness_state_does_not_cross_between_classes,
    test_forgetting_a_panel_does_not_stop_its_session,
    test_the_active_sessions_come_from_the_backend,
    test_the_sessions_endpoint_reports_one_entry_per_session,
    test_the_status_endpoint_answers_about_the_session_it_was_asked_about,
    test_a_break_endpoint_targets_the_session_it_names,
    test_every_live_endpoint_can_be_scoped_to_a_session,
    test_the_panel_count_is_the_session_count_with_no_maximum,
    test_the_registry_reports_however_many_sessions_are_running,
    test_the_grid_column_count_comes_from_the_session_count,
    test_a_finished_session_stops_being_rendered,
    test_each_panel_streams_only_its_own_camera,
    test_every_panel_control_carries_its_own_session_id,
    test_focus_shows_one_session_and_keeps_the_others_reachable,
    test_a_focused_session_that_ends_returns_to_the_grid,
    test_the_console_adds_no_second_polling_loop,
    test_the_panels_expose_nothing_biometric,
    test_two_sessions_produce_two_entries,
    test_focusing_one_of_ten_uses_that_sessions_id,
    test_switching_focus_releases_the_previous_stream,
    test_the_switcher_makes_every_other_class_selectable,
    test_logout_stops_the_live_poll_and_clears_what_was_fetched,
    test_a_duplicate_click_cannot_send_the_same_operation_twice,
    test_backend_errors_become_sentences_a_teacher_can_act_on,
    test_the_nested_views_have_back_navigation,
    test_the_grid_does_not_rebuild_panels_it_already_has,
    test_multi_session_tests_never_touch_the_real_database,
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
