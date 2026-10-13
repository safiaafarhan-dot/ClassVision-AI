"""
Assembly Mode.

NO WEBCAM AND NO BROWSER ARE NEEDED. The API half runs against a temporary
SQLite file; the UI half is asserted against the interface's own source.

WHAT THIS VERIFIES, and the first item is the whole point of the suite

    THAT THERE IS NO SECOND ATTENDANCE SYSTEM. Assembly Mode is a view onto
    the machinery that already exists: it starts a session through
    /api/live/start, reads /api/live/status and /api/live/presence on the same
    one-second clock the live console uses, pauses through /api/live/break,
    ends through /api/live/stop, and the attendance it produces is computed by
    the same rules and lands in the same tables. assembly.js is held to an
    ALLOWLIST of endpoints - every one of which the live console already calls
    - and is checked to contain no rule, no threshold and no attendance
    decision of its own.

    That an assembly is an ordinary session afterwards. It is scheduled in
    Sessions, bound by the same expected roster and the same permissions, and
    appears in the sessions list and the reports like any other lesson.

    That the formation is drawn from the SESSION's expected students rather
    than from the class register, which are different lists the moment
    somebody is enrolled after the assembly was scheduled.

    That the live attendance reading is labelled as a live reading, because
    the finalised figure is not that number: the rules run at finalisation and
    apply duration, grace and the late threshold.

    That the destructive actions confirm, and that they confirm through the
    machinery that already knows the difference between cancelling a session
    and deleting one.

    That the formation is operable: real buttons, real names, reachable by
    keyboard, and the states shown as a symbol and a word rather than as a
    colour.

WHAT THIS DOES NOT VERIFY

    A camera. No pipeline is started, because starting one needs hardware;
    what is checked is that the page asks the existing endpoints to start it
    and never does the work itself.

    That a human looking at the hall sees what is described here. No DOM is
    built and nothing is painted.

Run with:
    python tests/test_assembly_mode.py
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
ASSEMBLY = (STATIC / "assembly.js").read_text(encoding="utf-8")
SPATIAL_CSS = (STATIC / "spatial.css").read_text(encoding="utf-8")


def strip_comments(source):
    """
    Block comments removed.

    assembly.js opens with a list of every endpoint it is allowed to call and
    a paragraph about what it must not contain. A test that greps the prose is
    a test that fails the file for documenting itself.
    """
    return re.sub(r"/\*.*?\*/", "", source, flags=re.S)


CODE = strip_comments(ASSEMBLY)

# The endpoints Assembly Mode may call. Every one of these is already called
# by the live classroom console or the sessions page: that is the point, and
# it is what makes this a view rather than a system.
ALLOWED_ENDPOINTS = {
    "/api/live/start",
    "/api/live/status",
    "/api/live/presence",
    "/api/live/break",
    "/api/live/break/end",
    "/api/live/stop",
    "/api/live/sessions",
    "/api/live/stream.mjpg",
    "/api/sessions",
    # The session detail endpoint, reached as "/api/sessions/" + an id. It is
    # what the sessions page already uses to open one, and it is where the
    # expected roster comes from.
    "/api/sessions/",
    "/api/sessions/expected-students",
    "/api/cameras",
}


def view_html():
    """The markup of the assembly view, bounded by the next view."""
    start = HTML.index('id="view-assembly"')
    return HTML[start:HTML.index('<section id="view-', start)]


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_assembly_"))
    state.configure(directory / "assembly.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def sign_in(client):
    client.post("/api/auth/setup", json={
        "username": "assembly_admin", "password": "Assembly-Pass-2026!",
        "display_name": "Assembly Admin"})


def seed_school(client, count=14):
    """A whole-school class, and a session scheduled against it."""
    for index in range(1, count + 1):
        response = client.post("/api/students", json={
            "student_id": "ASM%03d" % index,
            "name": "Student %d" % index,
            "class_name": "WHOLE-SCHOOL", "section": "ALL"})
        assert response.status_code == 201, response.text

    response = client.post("/api/sessions", json={
        "session_id": "ASSEMBLY-1",
        "class_id": "WHOLE-SCHOOL",
        "class_name": "WHOLE-SCHOOL",
        "section": "ALL",
        "start_time": "2026-10-02T09:00:00+00:00",
        "end_time": "2026-10-02T09:30:00+00:00"})
    assert response.status_code == 201, response.text
    return response.json()


# -- it is a view, not a system --------------------------------------------

def test_assembly_calls_only_endpoints_the_live_console_already_calls():
    """
    THE TEST THIS SUITE EXISTS FOR.

    Every path string in assembly.js has to be in the allowlist above. An
    endpoint not on that list is either a new backend surface - in which case
    the claim that Assembly Mode is a view onto the existing pipeline is no
    longer true - or a typo, which would fail silently at runtime.
    """
    found = set(re.findall(r'"(/api/[a-z0-9/_.-]+)', CODE))

    # Query strings are written as a path plus "?...", so trim at the first
    # question mark the same way the code builds them.
    found = {path.split("?")[0].rstrip("?") for path in found}

    unexpected = sorted(found - ALLOWED_ENDPOINTS)
    assert not unexpected, (
        "assembly.js calls endpoints outside the live pipeline it is supposed "
        "to be a view onto: %s" % unexpected)

    # And it really does use the pipeline, rather than quietly using none of
    # it - an empty set would pass the assertion above.
    for required in ("/api/live/start", "/api/live/status",
                     "/api/live/presence", "/api/live/stop"):
        assert required in found, (
            "assembly.js never calls %s, so it is not running the existing "
            "pipeline at all" % required)

    print("PASS  assembly.js calls %d endpoints and every one of them is "
          "already used by the live console" % len(found))


def test_assembly_contains_no_attendance_rule_of_its_own():
    """
    No threshold, no ratio, no status decision.

    The attendance rules live in ai/attendance_rules.py and run on the server
    at finalisation. A page that decided for itself that eighty per cent of a
    session is PRESENT would be a second policy, and two policies that
    disagree about one assembly is the worst outcome this product has.
    """
    # The statuses the rule engine produces. The page may not assign one.
    for status in ("PRESENT", "LATE", "PARTIAL", "LEFT_EARLY",
                   "MANUAL_OVERRIDE"):
        assigned = re.search(r'status\s*[=:]\s*"%s"' % status, CODE)
        assert not assigned, (
            "assembly.js assigns the attendance status %s; statuses are the "
            "rule engine's" % status)

    # No threshold arithmetic, and no rule input.
    #
    # Deliberately NOT a list of words. The page is required to SAY that
    # duration, grace and the late threshold are applied at finalisation -
    # test_the_live_reading_is_not_presented_as_the_finalised_attendance
    # asserts exactly that sentence - so banning those words would mean
    # banning the explanation. What is banned is the machinery: the rule
    # ENGINE's own inputs, and a ratio compared against a constant, which is
    # what a decision actually looks like in code.
    for rule in ("presence_ratio", "late_threshold", "grace_seconds",
                 "attendance_rules", "min_presence", "0.75", "0.85"):
        assert rule not in CODE, (
            "assembly.js contains what looks like an attendance rule: %r"
            % rule)

    # And no comparison of a computed rate against a threshold.
    threshold = re.search(r"(rate|ratio|percent)[^;\n]*[<>]=?\s*\d", CODE)
    assert not threshold, (
        "assembly.js compares a rate against a threshold, which is a "
        "decision: %r" % (threshold.group(0) if threshold else ""))

    # The one piece of arithmetic it IS allowed, stated positively so this
    # test cannot be satisfied by the page computing nothing at all: present
    # over expected, which is reported as a live reading and never as the
    # attendance.
    assert "counts.present / A.roster.length" in CODE, (
        "the live rate is no longer present over expected; if the arithmetic "
        "moved, check it is still a reading rather than a decision")

    print("PASS  assembly.js holds no threshold, no ratio and no status "
          "decision")


def test_the_live_reading_is_not_presented_as_the_finalised_attendance():
    """
    The one figure on this page that could mislead.

    present / expected is computed in the browser from the presence timeline.
    It is a LIVE READING and it is not the attendance: the rules run at
    finalisation and apply duration, grace and the late threshold, so a
    student present on the timeline right now can still finalise as LATE or
    PARTIAL. The page has to say so, twice: beside the ring while it is
    running, and in the summary once it is not.
    """
    assert "live reading" in ASSEMBLY, (
        "the computed rate is never described as a live reading")
    assert "Live attendance" in ASSEMBLY, (
        "the ring is labelled as though it were the finalised attendance")

    # And the summary says where its own figures came from, which is a
    # different place.
    assert "finalised figures" in ASSEMBLY, (
        "the completion summary does not distinguish its figures from the "
        "live ones")
    for word in ("duration", "late"):
        assert word in ASSEMBLY, (
            "the page never explains that %r is applied at finalisation"
            % word)

    print("PASS  the live rate is labelled a live reading, and the summary "
          "says its figures are the finalised ones")


def test_the_formation_comes_from_the_session_not_the_class_register():
    """
    Two different lists, and the difference is an invented absence.

    session.expected_students was frozen when the assembly was scheduled and
    is the list the rules will be applied to. The class register is today's
    membership. Drawing the register would silently seat a student enrolled
    after the assembly was scheduled - and then report them absent from
    something they were never expected at.
    """
    roster = ASSEMBLY[ASSEMBLY.index("async function loadRoster("):]
    roster = roster[:roster.index("\n  /* ====")]

    assert "session.expected_students" in roster, (
        "the roster is not taken from the session's expected list")
    assert "/api/sessions/expected-students" in roster, (
        "the names are not read from the class roster endpoint")

    # The intersection, in that order: expected ids are the spine, names are
    # looked up against them.
    assert "expected.map(" in roster, (
        "the roster is built by iterating something other than the session's "
        "expected list")

    print("PASS  the formation is the session's expected students, with names "
          "looked up against them")


def test_a_recognised_student_who_was_not_expected_is_reported_not_seated():
    """
    A seat means "this person was expected here".

    Somebody confirmed who is not on the session's list is real and must not
    be hidden - but giving them a seat would make the formation disagree with
    the session it is drawn from, and counting them would push the rate above
    what the roster can support. So they are counted separately and explained
    in a sentence.
    """
    assert "unexpected" in CODE, (
        "a confirmed student who was not expected is not accounted for at all")
    assert "not on this" in ASSEMBLY and "expected list" in ASSEMBLY, (
        "there is no sentence explaining what happens to a recognised student "
        "who was not expected")
    assert "not counted in the rate" in ASSEMBLY, (
        "the page does not say that an unexpected student stays out of the "
        "rate")

    print("PASS  a recognised student who was not expected is counted and "
          "explained, never seated")


# -- the destructive actions ----------------------------------------------

def test_ending_an_assembly_confirms_and_names_what_it_is_ending():
    """
    The mistake worth preventing is finalising the wrong room.

    So the confirmation names the class, and it says what the action does -
    stop the camera, run the rules - rather than asking "are you sure".
    """
    end = ASSEMBLY[ASSEMBLY.index("  async function end("):]
    end = end[:end.index("\n  /* ")]

    assert "window.confirm(" in end, "ending an assembly asks nothing"
    assert "A.session && A.session.class_id" in end, (
        "the confirmation does not name the class being finalised")
    assert "runs the attendance rules" in end, (
        "the confirmation does not say what ending actually does")
    assert "Other running classes are not affected" in end, (
        "the confirmation does not say that other classrooms are untouched")

    print("PASS  ending an assembly confirms, names the class, and says what "
          "it does")


def test_deleting_is_handed_to_the_dialog_that_already_knows_how():
    """
    Deleting a session and cancelling one are different things with different
    consequences for attendance already recorded, and openSessionRemoveDialog
    is the one place in this product that knows which is which and what has to
    be typed to confirm it.

    A second delete confirmation written for this page would be a worse copy
    of that, and would drift from it.
    """
    remove = ASSEMBLY[ASSEMBLY.index("  function remove("):]
    remove = remove[:remove.index("\n  /* ")]

    assert "openSessionRemoveDialog" in remove, (
        "Assembly Mode deletes sessions through something other than the "
        "existing removal dialog")
    # And it does not roll its own.
    assert "window.confirm" not in remove, (
        "Assembly Mode asks its own delete confirmation as well as the "
        "dialog's")
    assert 'DELETE"' not in CODE and "apiSend(\"/api/sessions" not in CODE, (
        "Assembly Mode issues its own session deletion request")

    print("PASS  deleting an assembly goes through the existing session "
          "removal dialog")


def test_the_pipeline_is_never_asked_to_start_twice_or_swapped_mid_assembly():
    """
    The controls disable themselves according to the pipeline state the server
    reported, which is the only authority on it.

    And the session and camera selects lock while something is running: the
    device cannot be swapped under a running pipeline, because the frames
    after the change would be from a different room while the presence
    evidence says one session.
    """
    controls = ASSEMBLY[ASSEMBLY.index("  function renderControls("):]
    controls = controls[:controls.index("\n  async function start(")]

    assert 'set("assembly-start", live' in controls, (
        "Start stays enabled while the pipeline is running")
    assert 'select.disabled = live' in controls, (
        "the session can be changed under a running pipeline")
    assert 'camera.disabled = live' in controls, (
        "the camera can be swapped under a running pipeline")

    print("PASS  the controls follow the server's pipeline state, and the "
          "session and camera lock while it runs")


def test_leaving_the_view_stops_the_polling_and_drops_the_stream():
    """
    Two different costs, and both are real.

    The polling is two requests a second, which is pointless for a page nobody
    is looking at. The stream is a generator held open on the server. Neither
    stops the CAMERA, because the assembly is still happening - which is
    exactly the rule the live console follows.
    """
    leave = ASSEMBLY[ASSEMBLY.index("    leave: function ()"):]

    assert "stopPolling()" in leave, "leaving the view keeps polling"
    assert "removeAttribute(\"src\")" in leave, (
        "leaving the view leaves the stream connection open on the server")

    # And it must not stop the pipeline.
    assert "/api/live/stop" not in leave, (
        "leaving the Assembly page would END the assembly, which is not what "
        "navigating away means")

    assert "window.Assembly.leave()" in JS, (
        "switchView() never tells the assembly view it has been left")

    print("PASS  leaving the view stops the polling and the stream, and never "
          "stops the assembly")


# -- the view, and its controls -------------------------------------------

def test_assembly_is_a_tab_in_the_existing_navigation():
    assert 'data-view="assembly"' in HTML, "there is no Assembly tab"
    assert ">Assembly<" in HTML, "the tab has no readable label"
    assert 'id="view-assembly"' in HTML, "there is no Assembly view"

    # Driven by the navigation that already exists, not a second one.
    assert 'class="tab nav-item" data-view="assembly"' in HTML, (
        "the Assembly item is not an ordinary .tab, so switchView() will not "
        "drive it")
    assert '$("view-assembly").hidden = view !== "assembly";' in JS, (
        "switchView() does not show and hide the Assembly view")

    print("PASS  Assembly is a tab in the existing navigation, driven by the "
          "existing view switch")


def test_every_control_in_the_view_is_labelled():
    """
    The suite-wide rule, checked for the new view specifically: the two
    selects are the only form controls on this page and both need a label a
    screen reader can find.
    """
    view = view_html()

    controls = re.findall(r'<(?:input|select)[^>]*\bid="([^"]+)"', view)
    labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', view))

    unlabelled = [control for control in controls if control not in labelled]
    assert not unlabelled, (
        "controls in the assembly view without a label: %s" % unlabelled)
    assert len(controls) >= 2, (
        "expected the session and camera selects, found %d controls"
        % len(controls))

    print("PASS  all %d controls in the assembly view are labelled"
          % len(controls))


def test_the_six_requested_controls_are_all_present():
    view = view_html()
    for control in ("assembly-start", "assembly-pause", "assembly-resume",
                    "assembly-end", "assembly-finalize", "assembly-delete"):
        assert 'id="%s"' % control in view, "there is no %s control" % control

    for label in ("Start assembly", "Pause", "Resume", "End assembly",
                  "Finalise attendance", "Delete"):
        assert label in view, "the %r control has no readable label" % label

    print("PASS  start, pause, resume, end, finalise and delete are all "
          "present and labelled")


def test_the_formation_is_operable_without_a_mouse():
    """
    A hall of five hundred dots is a picture. A hall of five hundred buttons,
    each named, is something a person can actually use.
    """
    formation = ASSEMBLY[ASSEMBLY.index("  function renderFormation("):]
    formation = formation[:formation.index("\n  /* ====")]

    assert 'el("button"' in formation, (
        "the seats are not buttons, so they are not in the tab order")
    assert 'setAttribute("aria-label"' in formation, (
        "a seat has no accessible name, and it holds no text of its own")

    # The accessible name carries the person AND the state, because a 22px
    # circle has nothing in it to read.
    assert 'person.name + ": " + word' in formation, (
        "the seat's accessible name does not carry both the name and the "
        "state")

    # And the marker is revealed by focus as well as hover.
    assert ".hall-seat:focus-visible .holo-marker" in SPATIAL_CSS, (
        "the holographic marker only appears for a mouse")

    print("PASS  every seat is a named button and its marker is reachable by "
          "keyboard")


def test_the_formation_adapts_to_the_real_number_of_students():
    """
    Ten people and five hundred are the same code path.

    The shape is computed from the count, the rows are capped so a row never
    needs its own scrollbar, and nothing about either case is special.
    """
    shape = ASSEMBLY[ASSEMBLY.index("  function formationShape("):]
    shape = shape[:shape.index("\n  function ")]

    assert "Math.sqrt(count" in shape, (
        "the formation shape is not derived from the number of students")
    assert "Math.min(26" in shape, (
        "a row is not capped, so a large assembly would overflow its own row")

    # The row depth is set from the row index, which is what makes it recede.
    render = ASSEMBLY[ASSEMBLY.index("  function renderFormation("):]
    assert 'setProperty("--depth"' in render, (
        "the rows are not given a depth, so the hall is flat")

    print("PASS  the formation is computed from the real student count and "
          "capped per row")


def test_the_timeline_does_not_pass_its_own_observations_off_as_the_servers():
    """
    Six stages, two of which the server timestamps and four of which this page
    merely noticed.

    started_at and stopped_at are the server's. "Camera connected", "Students
    detected", "Identity processing" and "Attendance recorded" are the first
    poll at which the pipeline reported the thing - which a tab that was in the
    background for a minute observed late. Presenting those as server events
    would be presenting a time the page does not know.
    """
    stages = ASSEMBLY[ASSEMBLY.index("  function observeStages("):]
    stages = stages[:stages.index("\n  function renderTimeline(")]

    assert "status.started_at" in stages and "status.stopped_at" in stages, (
        "the two timestamps the server does provide are not used")

    timeline = ASSEMBLY[ASSEMBLY.index("  function renderTimeline("):]
    timeline = timeline[:timeline.index("\n  /* ====")]
    assert "observed by this page" in timeline and "not server events" in timeline, (
        "the timeline does not say which of its times are observations")

    # A stage that has not happened has no time, and says so the way the rest
    # of the interface does.
    assert "—" in ASSEMBLY, (
        "a stage with no time renders something other than an em dash")

    print("PASS  the timeline distinguishes the server's two timestamps from "
          "its own four observations")


def test_no_figure_is_invented_in_the_markup():
    view = view_html()

    suspicious = re.findall(r">\s*\d+(?:\.\d+)?%\s*<", view)
    assert not suspicious, (
        "the assembly view contains hard-coded percentages: %s" % suspicious)

    for fake in ("STU001", "John", "Jane", "Demo Student", "Safia", "Rahul"):
        assert fake not in view, (
            "the assembly view contains a fabricated name: %s" % fake)

    print("PASS  the assembly view invents no percentage and no name")


def test_the_view_says_nothing_biometric():
    """
    The formation shows a name and a state. It must not show, or mention, the
    evidence behind them.
    """
    view = view_html().lower()
    for word in ("embedding", "descriptor", "vector", "npz", "face crop",
                 "crop", "photo"):
        assert word not in view, (
            "the assembly view mentions %s" % word)

    print("PASS  the assembly view exposes nothing biometric")


# -- the server half -------------------------------------------------------

def test_the_endpoints_assembly_uses_all_exist():
    client, directory = make_client()
    try:
        sign_in(client)
        session = seed_school(client)

        checks = [
            ("/api/sessions?status=SCHEDULED&limit=50&offset=0", 200),
            ("/api/sessions/%s" % session["session_id"], 200),
            ("/api/sessions/expected-students?class_name=WHOLE-SCHOOL", 200),
            ("/api/cameras", 200),
            ("/api/live/sessions", 200),
            ("/api/live/status?session_id=%s" % session["session_id"], 200),
        ]
        for path, expected in checks:
            response = client.get(path)
            assert response.status_code == expected, (
                "%s answered %d, not %d: %s"
                % (path, response.status_code, expected, response.text[:160]))

        print("PASS  all %d endpoints Assembly Mode reads exist and answer"
              % len(checks))
    finally:
        teardown(client, directory)


def test_an_assembly_is_an_ordinary_session_with_an_ordinary_roster():
    """
    The claim the whole feature rests on, checked against the database.

    A session scheduled against a whole-school class carries every active
    student as expected, is listed by the ordinary sessions endpoint, and has
    the ordinary policy version. Nothing about it is special.
    """
    client, directory = make_client()
    try:
        sign_in(client)
        session = seed_school(client, count=14)

        assert len(session["expected_students"]) == 14, (
            "the assembly session was not given the whole class: %s"
            % session["expected_students"])
        assert session["status"] == "SCHEDULED"
        assert session["policy_version"], (
            "the assembly session has no attendance policy version, so it "
            "would not be governed by the rules")

        listed = client.get("/api/sessions?status=SCHEDULED&limit=50&offset=0")
        assert listed.status_code == 200
        ids = [item["session_id"] for item in listed.json()["items"]]
        assert session["session_id"] in ids, (
            "the assembly does not appear in the ordinary sessions list")

        print("PASS  an assembly is an ordinary session: %d expected, "
              "policy %s, listed with every other session"
              % (len(session["expected_students"]), session["policy_version"]))
    finally:
        teardown(client, directory)


def test_every_endpoint_assembly_uses_refuses_an_unauthenticated_caller():
    """
    The spatial redesign adds no new permission surface, which means it must
    not add a new way in either.
    """
    client, directory = make_client()
    try:
        sign_in(client)
        session = seed_school(client)

        from backend.app import app
        anonymous = TestClient(app)
        try:
            for path in ("/api/sessions?status=SCHEDULED",
                         "/api/sessions/%s" % session["session_id"],
                         "/api/sessions/expected-students?class_name=X",
                         "/api/cameras",
                         "/api/analytics/today"):
                response = anonymous.get(path)
                assert response.status_code == 401, (
                    "%s is readable without signing in (%d)"
                    % (path, response.status_code))
        finally:
            anonymous.close()

        print("PASS  every endpoint the assembly view reads refuses an "
              "unauthenticated caller")
    finally:
        teardown(client, directory)


def test_assembly_tests_never_touch_the_real_database():
    """The guard every suite in this project carries."""
    source = Path(__file__).read_text(encoding="utf-8")

    # Every client this suite builds is pointed at a fresh temporary file
    # before the app is imported, and the directory is removed afterwards.
    assert "tempfile.mkdtemp" in source, (
        "this suite does not create a temporary database directory")
    assert "state.configure(directory" in source, (
        "this suite does not point the application at its temporary database")
    assert "shutil.rmtree" in source, (
        "this suite does not clean up after itself")

    # And it never names the production database or its directory.
    #
    # The two names are assembled from pieces rather than written out,
    # because a test that greps its own source for a string cannot contain
    # that string - the first version of this assertion failed on itself.
    production_db = "classvision" + ".db"
    production_dir = "data" + "/"

    assert production_db not in source, (
        "this suite names the production database")
    assert production_dir not in source.replace("data" + "_service", ""), (
        "this suite reaches into the production data directory")

    # Nothing names it by absolute path either.
    assert str(PROJECT_ROOT) not in source, (
        "this suite hard-codes an absolute path into the project")

    print("PASS  this suite runs against a temporary database only")


# -- runner ----------------------------------------------------------------

TESTS = [
    test_assembly_calls_only_endpoints_the_live_console_already_calls,
    test_assembly_contains_no_attendance_rule_of_its_own,
    test_the_live_reading_is_not_presented_as_the_finalised_attendance,
    test_the_formation_comes_from_the_session_not_the_class_register,
    test_a_recognised_student_who_was_not_expected_is_reported_not_seated,
    test_ending_an_assembly_confirms_and_names_what_it_is_ending,
    test_deleting_is_handed_to_the_dialog_that_already_knows_how,
    test_the_pipeline_is_never_asked_to_start_twice_or_swapped_mid_assembly,
    test_leaving_the_view_stops_the_polling_and_drops_the_stream,
    test_assembly_is_a_tab_in_the_existing_navigation,
    test_every_control_in_the_view_is_labelled,
    test_the_six_requested_controls_are_all_present,
    test_the_formation_is_operable_without_a_mouse,
    test_the_formation_adapts_to_the_real_number_of_students,
    test_the_timeline_does_not_pass_its_own_observations_off_as_the_servers,
    test_no_figure_is_invented_in_the_markup,
    test_the_view_says_nothing_biometric,
    test_the_endpoints_assembly_uses_all_exist,
    test_an_assembly_is_an_ordinary_session_with_an_ordinary_roster,
    test_every_endpoint_assembly_uses_refuses_an_unauthenticated_caller,
    test_assembly_tests_never_touch_the_real_database,
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
