"""
The multi-class console's per-panel roster, bulk finish, and panel counts.

    tests/test_multi_session.py already covers the eight isolation properties,
    the dynamic panel count, focus mode and the polling contract. This suite
    covers what was added afterwards, and the explicit panel counts the
    requirement names one by one.

WHAT THIS VERIFIES

    That every grid panel carries its OWN PRESENT NOW list, that the list
    comes from that session's published snapshot, and that one class's names
    can never appear under another's heading.

    That the roster is capped, that the cap is the backend's, and that a tile
    showing six of thirty says thirty rather than quietly truncating.

    That the roster carries no biometric field - no embedding, no image, no
    cue value, no track id.

    That "Finish all sessions" is the per-session stop called once per
    session, scoped by id, sequential, confirmed, and offered only when there
    is more than one class to finish.

    That 1, 3, 4, 8, 10 and 15 active sessions produce 1, 3, 4, 8, 10 and 15
    entries, because the requirement asks for each of those numbers by name.

WHAT THIS DOES NOT VERIFY

    Real cameras. Every "running" session here is a runtime driven directly,
    which is the only way to have ten sessions running on a machine with one
    webcam - and it is precisely the part under test: whether ten managers
    keep ten sets of state apart. Nothing here opens a camera, and nothing
    here should be read as evidence that ten physical devices work at once.

Run with:
    python tests/test_multi_session_console.py
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

import backend  # noqa: F401,E402

from attendance_session import AttendanceSession  # noqa: E402
from backend.services import state  # noqa: E402
from backend.services.live_service import (  # noqa: E402
    PANEL_ROSTER_LIMIT,
    LiveSessionRegistry,
    reset_live_manager,
    roster_preview,
)
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


# -- harness ---------------------------------------------------------------

def make_database():
    directory = Path(tempfile.mkdtemp(prefix="classvision_console_"))
    state.configure(directory / "console.db")
    reset_live_manager(None)
    return state.get_database(), directory


def teardown(directory):
    reset_live_manager(None)
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def store_session(database, session_id, class_id):
    from repositories import SessionRepository

    now = datetime.now(timezone.utc)
    session = AttendanceSession(session_id, class_id,
                               now - timedelta(minutes=5),
                               now + timedelta(minutes=55),
                               policy_version="1.0")
    session.start()
    SessionRepository(database).save(session)
    return session


def running(registry, session_id, class_id, camera_index=0):
    """A manager whose runtime is RUNNING, without opening a camera."""
    manager = registry.manager_for(session_id)
    manager.runtime.begin(session_id, class_id=class_id,
                          camera_index=camera_index)
    return manager


def verified(manager, *names):
    """
    Publish a presence snapshot for this session, as its worker would.

    The rows are the shape live_pipeline.presence_view() produces, trimmed to
    the fields the console reads - which is the point: if the preview ever
    starts carrying a field that is not here, the biometric test below sees
    it.
    """
    manager.runtime.publish_presence([
        {
            "student_id": "STU%03d" % (index + 1),
            "student_name": name,
            "identity_state": "CONFIRMED",
            "presence_state": "RECOGNIZED",
            "liveness_state": "LIVE",
            "liveness_confirmed": True,
            "authenticity_state": "AUTHENTIC",
            "authenticity_confirmed": True,
            "attendance_eligibility": "COUNTED",
            "first_seen": None,
            "last_seen": None,
            "duration_seconds": 42.0,
            "segment_count": 1,
        }
        for index, name in enumerate(names)
    ])


def console_source():
    """The JavaScript that builds the multi-class console."""
    start = JS.index("   THE MULTI-CLASS LIVE CONSOLE")
    end = JS.index("/* ---------- live classroom (Step 8) ---------- */", start)
    return JS[start:end]


def entries_by_session(registry):
    return {entry["session_id"]: entry for entry in registry.active()}


# -- the per-panel roster --------------------------------------------------

def test_each_panel_carries_its_own_present_now_list():
    """
    Two classes, two rosters, and no name in the wrong one.

    This is the property the whole feature rests on: a panel can only render
    the array that arrived inside its own entry, so there is no session id for
    it to get wrong.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()

        alpha = running(registry, "S-A", "CSE-A", camera_index=0)
        beta = running(registry, "S-B", "CSE-B", camera_index=1)

        verified(alpha, "Safia Farhan", "Rahul Kumar", "Priya Das")
        verified(beta, "Amit Roy", "Sneha Pal")

        entries = entries_by_session(registry)

        names_a = [row["student_name"] for row in entries["S-A"]["present_now"]]
        names_b = [row["student_name"] for row in entries["S-B"]["present_now"]]

        assert names_a == ["Safia Farhan", "Rahul Kumar", "Priya Das"], names_a
        assert names_b == ["Amit Roy", "Sneha Pal"], names_b

        for name in names_a:
            assert name not in names_b, (
                "%r appears in both classes' rosters" % name)

        assert entries["S-A"]["present_now_total"] == 3
        assert entries["S-B"]["present_now_total"] == 2

        print("PASS  two classes carry two separate PRESENT NOW lists with no "
              "shared name")
    finally:
        teardown(directory)


def test_a_class_with_nobody_verified_gets_an_empty_roster():
    """
    Empty is a real answer. Not the other class's list, and not a guess.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()

        alpha = running(registry, "S-A", "CSE-A")
        running(registry, "S-B", "CSE-B")

        verified(alpha, "Safia Farhan")

        entries = entries_by_session(registry)

        assert entries["S-B"]["present_now"] == [], (
            "a class with nobody verified got %r"
            % entries["S-B"]["present_now"])
        assert entries["S-B"]["present_now_total"] == 0
        assert len(entries["S-A"]["present_now"]) == 1

        print("PASS  a class with nobody verified has an empty roster, not "
              "somebody else's")
    finally:
        teardown(directory)


def test_the_roster_is_capped_and_says_how_many_it_is_not_showing():
    """
    A tile showing six of thirty must say thirty.

    The cap is the backend's constant, so the interface cannot disagree with
    it, and the total is the real figure rather than the length of the
    preview.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        manager = running(registry, "S-A", "CSE-A")

        verified(manager, *["Student %02d" % index for index in range(1, 31)])

        entry = entries_by_session(registry)["S-A"]

        assert len(entry["present_now"]) == PANEL_ROSTER_LIMIT, (
            "the preview carried %d names, the cap is %d"
            % (len(entry["present_now"]), PANEL_ROSTER_LIMIT))
        assert entry["present_now_total"] == 30, (
            "the total should be the real figure, got %r"
            % entry["present_now_total"])
        assert entry["present_now_limit"] == PANEL_ROSTER_LIMIT

        print("PASS  the roster is capped at %d names and still reports all "
              "%d verified" % (PANEL_ROSTER_LIMIT, entry["present_now_total"]))
    finally:
        teardown(directory)


def test_the_roster_carries_nothing_biometric():
    """
    Four fields, chosen one at a time.

    The presence row the worker publishes has a dozen; the tile gets a name,
    two states and the id the rest of the interface keys off. No duration, no
    track id, no cue value, no eligibility verdict - and nothing that could
    become an image or a vector.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        manager = running(registry, "S-A", "CSE-A")
        verified(manager, "Safia Farhan")

        row = entries_by_session(registry)["S-A"]["present_now"][0]

        assert set(row) == {"student_id", "student_name", "presence_state",
                            "liveness_state"}, (
            "the roster row carries unexpected fields: %s" % sorted(row))

        for banned in ("embedding", "descriptor", "crop", "image", "frame",
                       "landmark", "vector", "score", "track_id"):
            assert not any(banned in key for key in row), (
                "the roster row carries %r" % banned)

        print("PASS  a roster row is four fields and none of them is "
              "biometric")
    finally:
        teardown(directory)


def test_the_roster_reads_the_published_snapshot_only():
    """
    Read from the runtime's published view, like every other live answer, so
    the request thread never touches an object the worker is mutating.
    """
    source = (PROJECT_ROOT / "backend" / "services"
              / "live_service.py").read_text(encoding="utf-8")

    preview = source[source.index("def roster_preview("):]
    preview = preview[:preview.index("\nclass LiveSessionRegistry")]

    assert "manager.runtime.presence()" in preview, (
        "the preview does not read the published presence snapshot")
    assert "pipeline" not in preview, (
        "the preview reaches into the pipeline, which the worker owns")

    print("PASS  the roster reads the published snapshot and never the "
          "pipeline")
    return


def test_a_break_does_not_empty_the_roster_of_another_class():
    """
    A break belongs to one session, and so does its roster.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()

        alpha = running(registry, "S-A", "CSE-A")
        beta = running(registry, "S-B", "CSE-B")
        verified(alpha, "Safia Farhan")
        verified(beta, "Amit Roy", "Sneha Pal")

        store_session(database, "S-A", "CSE-A")
        store_session(database, "S-B", "CSE-B")

        from backend.services.break_service import BreakService

        BreakService(database).start("S-A")

        entries = entries_by_session(registry)

        assert [row["student_name"] for row in entries["S-B"]["present_now"]] \
            == ["Amit Roy", "Sneha Pal"], (
            "the other class's roster changed when CSE-A went on break")
        assert len(entries["S-A"]["present_now"]) == 1, (
            "a break emptied the roster of the class taking it")

        print("PASS  a break in one class leaves both rosters alone")
    finally:
        teardown(directory)


def test_the_panel_renders_the_roster_from_its_own_entry():
    """
    Asserted in the source, because this is the bug the feature could have.

    The renderer must read entry.present_now and must NOT fetch presence per
    panel: ten panels on a one-second poll would be ten requests a second.
    """
    source = console_source()

    assert "entry.present_now" in source, (
        "the panel does not render the roster from its own entry")
    assert "updateLivePanelRoster" in source, (
        "there is no per-panel roster renderer")

    renderer = source[source.index("function updateLivePanelRoster("):]
    renderer = renderer[:renderer.index("\nfunction livePresenceWord")]

    for fetching in ("api(", "apiSend(", "fetch("):
        assert fetching not in renderer, (
            "the per-panel roster makes its own request (%r), which is one "
            "request per panel per poll" % fetching)

    assert "session_id" not in renderer, (
        "the roster renderer handles a session id, so it could address the "
        "wrong session; it should only render the array it was given")

    print("PASS  each panel renders the roster from its own entry and makes "
          "no request of its own")
    return


def test_the_grid_still_makes_exactly_one_request_per_poll():
    """
    The roster must not have added a second polling loop.
    """
    source = console_source()

    requests = re.findall(r'api\("(/api/[a-z0-9/._]+)', source)
    polled = sorted(set(requests))

    assert "/api/live/sessions" in polled, (
        "the console no longer reads the sessions endpoint")
    assert "/api/live/presence" not in polled or source.count(
        'api("/api/live/presence') <= 1, (
        "presence is fetched more than once in the console; the grid should "
        "read it from the sessions answer and only the FOCUSED panel should "
        "fetch it")

    assert source.count("setInterval") == 0, (
        "the console starts a timer of its own; the single live poll is "
        "started by the view, not by the grid")

    print("PASS  the grid adds no second request and no second timer")
    return


# -- the panel count, every number the requirement names -------------------

def test_the_panel_count_matches_the_session_count_for_every_named_number():
    """
    1, 3, 4, 8, 10 and 15 - asserted one by one, because the requirement
    names them one by one and because "no maximum" is a claim worth testing
    at a number nobody would hard-code.
    """
    for count in (1, 2, 3, 4, 5, 8, 10, 15):
        database, directory = make_database()
        try:
            registry = LiveSessionRegistry()
            for index in range(count):
                running(registry, "S-%02d" % index, "CLASS-%02d" % index,
                        camera_index=index)

            entries = registry.active()
            assert len(entries) == count, (
                "%d running sessions produced %d entries"
                % (count, len(entries)))

            ids = {entry["session_id"] for entry in entries}
            assert len(ids) == count, (
                "%d sessions produced %d distinct ids" % (count, len(ids)))

            # Every entry carries its own roster field, however many there are.
            for entry in entries:
                assert "present_now" in entry
                assert entry["present_now"] == []
        finally:
            teardown(directory)

    print("PASS  1, 2, 3, 4, 5, 8, 10 and 15 sessions produce 1, 2, 3, 4, 5, "
          "8, 10 and 15 panels")


def test_the_column_count_grows_but_the_panel_count_is_never_capped():
    """
    gridColumns caps COLUMNS, not panels.

    A five-column maximum is a layout decision - a camera squeezed into a
    twentieth of a screen is unusable - and it is not a limit on how many
    classrooms are shown: twenty sessions still render twenty panels, in five
    columns and four rows.
    """
    columns = JS[JS.index("function gridColumns("):]
    columns = columns[:columns.index("\nfunction liveSessionLabel")]

    assert "return 5" in columns, (
        "the column count no longer tops out, so twenty classes would be "
        "twenty columns of unreadable camera")

    source = console_source()
    grid = source[source.index("function renderLiveGrid("):]
    grid = grid[:grid.index("\nfunction ")]

    # The panels come from the entries, one each, with no slice of the array.
    assert "entries" in grid
    for capping in (".slice(0, 4)", ".slice(0,4)", "> 4", ">= 4",
                    "Math.min(entries.length"):
        assert capping not in grid, (
            "the grid appears to cap the number of panels: %r" % capping)

    print("PASS  the column count tops out at five and the panel count does "
          "not top out at all")
    return


def test_a_session_that_starts_appears_and_one_that_ends_disappears():
    """
    9 -> 10 -> 9, from the backend rather than from anything the page
    remembers.
    """
    database, directory = make_database()
    try:
        registry = LiveSessionRegistry()
        for index in range(9):
            running(registry, "S-%02d" % index, "CLASS-%02d" % index,
                    camera_index=index)

        assert len(registry.active()) == 9

        running(registry, "S-NEW", "CLASS-NEW", camera_index=9)
        after_start = {entry["session_id"] for entry in registry.active()}
        assert len(after_start) == 10, (
            "a new session did not appear: %d entries" % len(after_start))
        assert "S-NEW" in after_start

        # It ends, through the states the runtime actually has, and the
        # registry stops reporting it.
        manager = registry.manager_for("S-NEW")
        manager.runtime.mark_stopping()
        manager.runtime.mark_completed()
        registry.forget("S-NEW")

        after_finish = {entry["session_id"] for entry in registry.active()}
        assert len(after_finish) == 9, (
            "a finished session is still reported: %d entries"
            % len(after_finish))
        assert "S-NEW" not in after_finish

        print("PASS  a session that starts appears, and one that ends stops "
              "being reported")
    finally:
        teardown(directory)


# -- finishing every class -------------------------------------------------

def test_finish_all_calls_the_same_per_session_stop_once_per_session():
    """
    No new endpoint, no bulk finalisation rule, and no shared call.

    Every stop is the per-session one, scoped by that session's own id - so
    the product rules a single Finish follows are exactly the rules this
    follows, once per class.
    """
    source = console_source()

    assert "async function finishAllLiveSessions(" in source, (
        "there is no bulk finish")

    bulk = source[source.index("async function finishAllLiveSessions("):]
    bulk = bulk[:bulk.index("\nfunction renderLiveUpdated")]

    assert '"/api/live/stop?session_id="' in bulk, (
        "the bulk finish does not use the per-session stop endpoint")
    assert "encodeURIComponent(entry.session_id)" in bulk, (
        "the bulk finish does not scope each stop by that entry's own id")

    # Sequential. A for-of with an await inside is the shape that matters;
    # Promise.all would fire every finalisation at once.
    assert "for (const entry of running)" in bulk, (
        "the bulk finish does not iterate the running sessions one at a time")
    assert "Promise.all" not in bulk, (
        "the bulk finish runs finalisations in parallel; attendance writes "
        "should not race each other")

    # No endpoint was invented for it.
    routes = (PROJECT_ROOT / "backend" / "routes"
              / "live.py").read_text(encoding="utf-8")
    assert "stop_all" not in routes and "/stop/all" not in routes, (
        "a bulk stop endpoint was added; the per-session one is what carries "
        "the finalisation rules")

    print("PASS  Finish all is the per-session stop, once per session, "
          "sequentially, with no new endpoint")
    return


def test_finish_all_confirms_and_names_every_class_first():
    source = console_source()
    bulk = source[source.index("async function finishAllLiveSessions("):]
    bulk = bulk[:bulk.index("\nfunction renderLiveUpdated")]

    assert "window.confirm(" in bulk, (
        "the bulk finish does not confirm")
    assert "You are about to finish:" in bulk, (
        "the confirmation does not say what it is about to do")
    assert "labels.map(" in bulk, (
        "the confirmation does not list the classes, so its scope cannot be "
        "read before agreeing to it")
    assert "if (!confirmed) return;" in bulk, (
        "the bulk finish proceeds whether or not it was confirmed")

    print("PASS  the bulk finish confirms first and names every class in the "
          "confirmation")
    return


def test_finish_all_is_offered_only_when_there_is_more_than_one_class():
    source = console_source()

    render = source[source.index("function renderGlobalLiveActions("):]
    render = render[:render.index("\n/*")]

    assert "running.length < 2" in render, (
        "the bulk finish is offered for a single class, where it duplicates "
        "that panel's own Finish button")
    assert "bar.hidden" in render, (
        "the bulk action bar is never hidden")

    assert 'id="live-global-actions"' in HTML, (
        "the page has no bulk action bar")
    assert 'id="live-finish-all"' in HTML
    assert "hidden" in HTML[HTML.index('id="live-global-actions"') - 80:
                            HTML.index('id="live-global-actions"') + 80], (
        "the bulk action bar is not hidden by default, so it would flash on "
        "a page with no live classes")

    print("PASS  Finish all appears only from two running classes upwards")
    return


def test_one_failed_finish_does_not_abandon_the_others():
    source = console_source()
    bulk = source[source.index("async function finishAllLiveSessions("):]
    bulk = bulk[:bulk.index("\nfunction renderLiveUpdated")]

    assert "failures.push(" in bulk, (
        "a failure is not recorded, so the operator is not told which class "
        "was not finished")
    assert "try {" in bulk and "} catch (failure) {" in bulk, (
        "the loop does not survive one class failing")
    assert "were not finished" in bulk, (
        "the operator is not told which classes remain running")

    print("PASS  one class failing to finish names itself and leaves the "
          "others finished")
    return


# -- the roster in the interface -------------------------------------------

def test_the_panel_markup_and_styles_carry_the_roster():
    source = console_source()

    assert "live-panel-roster" in source, (
        "the panel builder does not create a roster section")
    for rule in (".live-panel-roster {", ".live-panel-face {",
                 ".live-panel-face-state {"):
        assert rule in CSS, "the stylesheet has no %s rule" % rule

    # The tile stays a tile: the stage is the row that grows.
    panel = CSS[CSS.index(".live-panel {"):]
    panel = panel[:panel.index("}")]
    assert "auto 1fr auto auto auto" in panel, (
        "the panel's row template does not account for the roster: %s" % panel)

    print("PASS  the roster has markup and styles, and the camera is still "
          "the row that grows")
    return


def test_the_roster_never_invents_a_state():
    source = console_source()
    word = source[source.index("function livePresenceWord("):]
    word = word[:word.index("\n/*")]

    assert '"No data"' in word, (
        "a missing state is not reported as missing")
    assert "LIVE" not in word.replace("liveness_state", ""), (
        "the roster appears to default a reading to LIVE")

    print("PASS  a roster row with no reading says 'No data' rather than "
          "guessing one")
    return


def test_console_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    database, directory = make_database()
    try:
        assert Path(database.path).parent != real.parent
        store_session(database, "S-A", "CSE-A")
    finally:
        teardown(directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


def test_the_preview_helper_is_the_only_place_the_cap_lives():
    """
    One constant, imported rather than repeated.

    A cap written again in the route, the renderer or the stylesheet is a cap
    that drifts.
    """
    service = (PROJECT_ROOT / "backend" / "services"
               / "live_service.py").read_text(encoding="utf-8")
    routes = (PROJECT_ROOT / "backend" / "routes"
              / "live.py").read_text(encoding="utf-8")

    assert service.count("PANEL_ROSTER_LIMIT = ") == 1, (
        "the cap is defined more than once")
    assert str(PANEL_ROSTER_LIMIT) not in re.sub(
        r"#.*", "", routes), (
        "the route appears to carry its own copy of the roster cap")

    # And the renderer takes the cap from the entry rather than deciding it.
    source = console_source()
    renderer = source[source.index("function updateLivePanelRoster("):]
    renderer = renderer[:renderer.index("\nfunction livePresenceWord")]
    assert "slice(0, 6)" not in renderer and "slice(0,6)" not in renderer, (
        "the interface caps the roster itself, so it could disagree with the "
        "backend")

    print("PASS  the roster cap is defined once, in the service")
    return


# -- runner ----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_each_panel_carries_its_own_present_now_list,
        test_a_class_with_nobody_verified_gets_an_empty_roster,
        test_the_roster_is_capped_and_says_how_many_it_is_not_showing,
        test_the_roster_carries_nothing_biometric,
        test_the_roster_reads_the_published_snapshot_only,
        test_a_break_does_not_empty_the_roster_of_another_class,
        test_the_panel_renders_the_roster_from_its_own_entry,
        test_the_grid_still_makes_exactly_one_request_per_poll,
        test_the_panel_count_matches_the_session_count_for_every_named_number,
        test_the_column_count_grows_but_the_panel_count_is_never_capped,
        test_a_session_that_starts_appears_and_one_that_ends_disappears,
        test_finish_all_calls_the_same_per_session_stop_once_per_session,
        test_finish_all_confirms_and_names_every_class_first,
        test_finish_all_is_offered_only_when_there_is_more_than_one_class,
        test_one_failed_finish_does_not_abandon_the_others,
        test_the_panel_markup_and_styles_carry_the_roster,
        test_the_roster_never_invents_a_state,
        test_console_tests_never_touch_the_real_database,
        test_the_preview_helper_is_the_only_place_the_cap_lives,
    ]

    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as failure:
            print("FAIL  %s: %s" % (test.__name__, failure))
        except Exception as failure:                      # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(failure).__name__, failure))

    print("\n%d/%d tests passed." % (passed, len(tests)))
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
