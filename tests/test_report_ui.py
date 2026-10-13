"""
Step 11's front end: the four readings of the register, and their exports.

NO WEBCAM AND NO BROWSER ARE NEEDED. The API half runs against a temporary
SQLite file; the UI half is asserted against the dashboard's own source files.

WHY THIS SUITE EXISTS

    The Step 11 backend was complete and tested - per-class and per-term
    summaries, the seat map, CSV and PDF - while the page that was supposed to
    show it had four mode buttons, no panels behind them, no JavaScript bound
    to them, and one unclosed <div> left behind. Every one of those is a thing
    a passing backend suite cannot see, so each is checked here directly,
    including the markup balance.

WHAT THIS VERIFIES

    That all four modes exist as a real tablist with a real panel each, driven
    by the dashboard's own switcher rather than a second mechanism.

    That every pane reads an endpoint that ALREADY EXISTED. Step 11's whole
    constraint is that a summary reuses the analytics service's numbers; a page
    that fetched raw rows and summed them would reintroduce exactly the second
    source of truth the backend refuses to be. So the JavaScript is checked for
    arithmetic on attendance figures and there is none.

    That "no data" survives the last layer. A class with no finalised session
    in the window reports no rate, and the page renders a dash - never 0%,
    which reads as "everybody was absent".

    That a student expected and never evaluated renders as not evaluated, in
    words, rather than as absent, in the grid as well as in the table.

    That the seat map never claims to know where anybody sat. It says so on the
    page, not only in a server note, and it reads no coordinate field - because
    there is none to read.

    That each export carries the SAME query as the pane above it, so a
    downloaded file cannot contain rows the page never showed.

WHAT THIS DOES NOT VERIFY

    That a human clicking a mode button sees what is described here. No browser
    is driven - the DOM is never built, only the source that would build it.
    That limit is stated rather than glossed over.

    Any attendance rule, and any PDF structure. Both are Step 6's and
    test_report_exports.py's.

Run with:
    python tests/test_report_ui.py
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

from backend.services import state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

MODES = ("records", "classes", "terms", "seatmap")

# The endpoints that already existed before this page was finished. The panes
# must use these and add none.
ENDPOINTS = (
    "/api/reports/attendance",
    "/api/reports/classes",
    "/api/reports/terms",
    "/api/reports/sessions/",
)

# Every field GET /api/reports/classes returns per class.
CLASS_FIELDS = ("class_id", "completed_sessions", "student_count",
                "expected_students", "counts", "attendance_rate",
                "average_presence_ratio", "late_rate", "partial_rate",
                "absence_rate", "early_leave_rate", "first_session",
                "last_session", "windowed")

TERM_FIELDS = ("academic_session_id", "academic_year", "semester",
               "term_status", "term_start", "term_end", "unassigned", "label",
               "first_session", "last_session", "completed_sessions",
               "expected_students", "evaluated_students", "attendance_rate",
               "counts", "classes")

SEATMAP_FIELDS = ("session_id", "class_id", "session_date", "section",
                  "teacher", "status", "columns", "rows", "seats", "grid",
                  "summary", "ordering", "note")

# A seating plan's fields. None of these exists anywhere in this system, and
# the page must not read one - see test_the_seat_map_claims_no_position.
SEAT_POSITION_FIELDS = ("seat_x", "seat_y", "seat_row", "seat_column",
                        "desk", "position_x", "position_y", "coordinates",
                        "bench", "row_index")


# -- fixtures ---------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_reportui_"))
    state.configure(directory / "reportui.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def reports_source():
    """The JavaScript for the four report readings, and nothing else."""
    start = JS.index("   FOUR READINGS OF THE SAME STORED DECISIONS")
    end = JS.index("async function loadOverview()", start)
    return JS[start:end]


def report_pane_styles():
    """
    The stylesheet rules for the report panes and the seat grid.

    Anchored on the FIRST and LAST selector of the block rather than on a
    comment heading. Two reasons, and the second is the one that matters: a
    comment can be reworded without breaking three tests, and a slice that
    cannot find its rules raises here instead of returning "" - an empty block
    satisfies "contains no animation" without checking anything.
    """
    start = CSS.index(".mode-switch {")
    end = CSS.index(".assistant-text {", start)
    block = CSS[start:end]
    assert ".seat-grid" in block and ".term-card" in block, (
        "the report-pane style block no longer contains the rules these "
        "tests check")
    return block


def pane_html(mode):
    """The markup of one report pane."""
    marker = 'id="report-pane-%s"' % mode
    start = HTML.index(marker)
    # Each pane is a sibling of the next; the next marker, or the end of the
    # reports view, bounds it.
    #
    # "The end of the reports view" used to be spelled as the START OF THE
    # ADMIN VIEW, which is a different place: every view declared between the
    # two was inside the slice. That was invisible for as long as none of them
    # happened to contain a word this file forbids, and stopped being invisible
    # the moment a view with a camera panel in it was declared there - a panel
    # that says "the complete camera frame" is not a report pane mentioning a
    # frame. The bound is now the next view, whichever view that is, which is
    # what the sentence above always meant.
    following = [HTML.index('id="report-pane-%s"' % each)
                 for each in MODES
                 if 'id="report-pane-%s"' % each in HTML
                 and HTML.index('id="report-pane-%s"' % each) > start]
    end = (min(following) if following
           else HTML.index('<section id="view-', start + len(marker)))
    return HTML[start:end]


BASE = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

STUDENTS = [("S-1", "Asha Rao", "2"),
            ("S-2", "Bhavin Shah", "10"),
            ("S-3", "Chitra Das", "1")]


def seed(client):
    """
    One finalised session with three students: present, absent, and expected
    but never evaluated.

    Built through AttendanceSession and AttendanceDecision - the same objects
    the live pipeline writes through - so no row here is this suite's own idea
    of what an attendance record looks like.
    """
    from attendance_rules import AttendanceDecision
    from attendance_session import AttendanceSession
    from repositories import (AttendanceRepository, SessionRepository,
                              StudentRepository)

    database = state.get_database()
    students = StudentRepository(database)

    for student_id, name, roll in STUDENTS:
        students.upsert(student_id=student_id, name=name, roll_number=roll,
                        class_name="CSE", section="A")

    session_id = "SESS-REPORTUI"
    session = AttendanceSession(session_id, "CSE-A", BASE,
                                BASE + timedelta(hours=1),
                                policy_version="1.0",
                                expected_students=[sid for sid, _, _
                                                   in STUDENTS])
    session.start()
    session.complete(completed_at=BASE + timedelta(hours=1))
    SessionRepository(database).save(session)

    attendance = AttendanceRepository(database)
    for student_id, status, ratio in (("S-1", "PRESENT", 0.95),
                                      ("S-2", "ABSENT", 0.0)):
        decision = AttendanceDecision(student_id, session_id, status,
                                      "synthetic test record", "1.0")
        decision.present_seconds = 3420.0 * ratio
        decision.session_seconds = 3600.0
        decision.presence_ratio = ratio
        attendance.save_decision(decision)

    # S-3 is expected here and never evaluated, on purpose.
    return session_id, BASE.date().isoformat()


def sign_in(client):
    setup = client.post("/api/auth/setup", json={
        "username": "owner", "display_name": "Owner",
        "password": "a-long-enough-passphrase"})
    assert setup.status_code == 201, setup.text


# -- the markup the backend was waiting for ---------------------------------

def test_every_mode_button_has_a_panel_behind_it():
    for mode in MODES:
        assert 'id="report-mode-%s"' % mode in HTML, (
            "there is no %s mode button" % mode)
        assert 'id="report-pane-%s"' % mode in HTML, (
            "the %s mode button has no panel behind it - this is exactly the "
            "state Step 11's front end was left in" % mode)

    print("PASS  all four report modes have a panel behind the button")


def test_the_reports_markup_is_balanced():
    """
    The regression that started this suite.

    The records pane was opened and never closed, so every pane added after it
    would have nested inside the one before. A browser silently repairs that
    and the page still renders, which is why it survived - nothing failed.
    """
    start = HTML.index('<section id="view-reports"')
    end = HTML.index('<section id="view-admin"', start)
    block = HTML[start:end]

    for tag in ("div", "form", "table", "section", "details", "p"):
        opened = len(re.findall(r"<%s\b" % tag, block))
        closed = block.count("</%s>" % tag)
        assert opened == closed, (
            "the reports view has %d <%s> and %d </%s>"
            % (opened, tag, closed, tag))

    print("PASS  every element in the reports view is closed")


def test_the_modes_are_a_real_tablist():
    switch = HTML[HTML.index('id="report-modes"'):
                  HTML.index('id="report-pane-records"')]

    assert 'role="tablist"' in switch, "the mode switch is not a tablist"
    assert switch.count('role="tab"') == len(MODES), (
        "not every mode button is a tab")
    assert 'aria-label="Which report"' in switch, (
        "the tablist is not labelled")

    for mode in MODES:
        pane = pane_html(mode)
        assert 'role="tabpanel"' in pane, (
            "the %s pane is not a tabpanel" % mode)
        assert 'aria-labelledby="report-mode-%s"' % mode in pane, (
            "the %s pane is not tied to its button" % mode)

    print("PASS  the four modes are a real tablist with a labelled panel "
          "each")


def test_switching_a_mode_moves_aria_selected_and_the_hidden_attribute():
    source = reports_source()

    assert 'aria-selected", each === mode ? "true" : "false"' in source, (
        "aria-selected is not moved when the mode changes, so a screen reader "
        "is never told which of the four readings is showing")
    assert "pane.hidden = each !== mode" in source, (
        "the panes are not shown and hidden by the mode switch")

    # Exactly one pane is visible in the source: records, the default.
    for mode in MODES:
        opening = pane_html(mode).split(">", 1)[0]
        if mode == "records":
            assert " hidden" not in opening, (
                "the default pane starts hidden, so the tab opens blank")
        else:
            assert " hidden" in opening, (
                "the %s pane does not start hidden, so two readings show at "
                "once before any script runs" % mode)

    print("PASS  switching a mode moves aria-selected and the hidden "
          "attribute")


def test_the_chosen_mode_is_page_state_like_every_other_filter():
    assert 'reportMode: "records"' in JS, (
        "the chosen reading is not part of the page's state")
    assert "state.reportMode" in JS, "the state field is never read"
    assert "await loadReportMode();" in JS, (
        "refresh() does not go through the mode loader, so returning to the "
        "tab would show the register whatever reading you had left on")

    print("PASS  the chosen reading is page state and refresh() respects it")


# -- what the panes read ----------------------------------------------------

def test_each_pane_reads_an_endpoint_that_already_existed():
    source = reports_source()

    for endpoint in ENDPOINTS:
        assert endpoint in source, "nothing reads %s" % endpoint

    assert "/seatmap?columns=" in source, "the seat map is never fetched"

    # No endpoint of its own was invented for the page.
    urls = set(re.findall(r'"(/api/[a-z0-9/._-]+)', source))
    known = {
        "/api/reports/attendance", "/api/reports/attendance.csv",
        "/api/reports/attendance.pdf", "/api/reports/classes",
        "/api/reports/classes.pdf", "/api/reports/terms",
        "/api/reports/terms.pdf", "/api/reports/sessions/",
        "/api/academic/sessions", "/api/sessions",
    }
    unknown = {url for url in urls
               if not any(url.startswith(each) for each in known)}
    assert not unknown, "the page invented endpoints: %s" % sorted(unknown)

    print("PASS  every pane reads an endpoint that already existed")


def test_the_endpoints_return_the_fields_the_panes_read():
    client, directory = make_client()
    try:
        session_id, _ = seed(client)

        classes = client.get("/api/reports/classes")
        assert classes.status_code == 200, classes.text
        row = classes.json()["items"][0]
        for field in CLASS_FIELDS:
            assert field in row, "the class summary has no %s" % field

        terms = client.get("/api/reports/terms")
        assert terms.status_code == 200, terms.text
        term = terms.json()["items"][0]
        for field in TERM_FIELDS:
            assert field in term, "the term summary has no %s" % field

        grid = client.get("/api/reports/sessions/%s/seatmap" % session_id)
        assert grid.status_code == 200, grid.text
        for field in SEATMAP_FIELDS:
            assert field in grid.json(), "the seat map has no %s" % field

        print("PASS  the three endpoints return every field the panes read")
    finally:
        teardown(client, directory)


def test_the_panes_invent_no_field():
    """
    Every `row.<name>` and `report.<name>` the panes read must be a field the
    API actually returns. A page that reads a field nobody sends renders
    "undefined" at a teacher, which is worse than a dash.
    """
    source = reports_source()
    provided = set(CLASS_FIELDS) | set(TERM_FIELDS) | set(SEATMAP_FIELDS) | {
        # the attendance row the records table and the seat cells read
        "student_id", "student_name", "roll_number", "section", "status",
        "class_id", "session_date", "presence_ratio", "present_seconds",
        "is_override", "original_status", "policy_version", "session_id",
        # the envelopes
        "items", "total", "totals", "note", "summary", "filters", "truncated",
        "returned", "limit", "offset", "counts", "grid", "columns", "seats",
        "ordering", "rows", "label", "unassigned", "classes", "status",
        # analytics figures inside a totals roll-up
        "attendance_rate", "completed_sessions", "expected_students",
        "evaluated_students", "student_count", "average_presence_ratio",
        "late_rate", "partial_rate", "absence_rate", "early_leave_rate",
        "first_session", "last_session", "windowed", "academic_session_id",
        "academic_year", "semester", "term_status", "term_start", "term_end",
        "value", "length", "style", "hidden", "textContent", "checked",
    }

    read = set(re.findall(r"\b(?:row|report|term|seat|each|totals)\.(\w+)",
                          source))
    invented = {name for name in read if name not in provided}
    assert not invented, (
        "the panes read fields the API does not return: %s" % sorted(invented))

    for field in SEAT_POSITION_FIELDS:
        assert field not in source, (
            "the page reads %s, which does not exist and would be a "
            "fabricated seating position" % field)

    print("PASS  the panes read no field the API does not return")


def test_the_page_computes_no_attendance_figure_of_its_own():
    """
    Step 11's constraint, extended to the last layer.

    The backend reuses the analytics service so a teacher cannot read two
    different numbers for one class. A page that divided a count by a total
    would reintroduce the second source of truth one layer further out, where
    nothing would catch it.
    """
    source = reports_source()

    # No division and no accumulation anywhere in the report panes.
    assert not re.search(r"[/]\s*(?:row|report|each|term)\.", source), (
        "the page divides by a figure from the API")
    assert not re.search(r"\.reduce\(", source), (
        "the page accumulates figures from the API")
    assert "* 100" not in source, (
        "the page scales a ratio itself instead of calling percent()")

    # The one conversion it does is percent(), which only formats.
    assert "percent(row.attendance_rate)" in source, (
        "the class rate is not rendered through percent()")
    assert "percent(totals.attendance_rate)" in source, (
        "the weighted total is not rendered through percent()")

    print("PASS  the page formats the server's figures and computes none")


# -- no data is not zero ----------------------------------------------------

def test_a_class_with_no_session_in_the_window_shows_no_rate():
    client, directory = make_client()
    try:
        seed(client)

        # A window far from the seeded session.
        response = client.get("/api/reports/classes",
                              params={"date_from": "1999-01-01",
                                      "date_to": "1999-12-31"})
        assert response.status_code == 200, response.text
        row = response.json()["items"][0]

        assert row["attendance_rate"] is None, (
            "an empty window reported a rate")
        assert row["completed_sessions"] == 0

        # And the page turns that None into a dash, not 0%.
        source = reports_source()
        assert 'percent(row.attendance_rate) ?? "—"' in source, (
            "a null rate is not rendered as a dash")

        print("PASS  a window with no session shows no rate rather than 0%")
    finally:
        teardown(client, directory)


def test_a_term_with_no_rate_says_no_data_rather_than_zero():
    source = reports_source()
    assert 'percent(row.attendance_rate) ?? "no data"' in source, (
        "a term with no measurable attendance would render as 0%")

    print("PASS  a term with no rate says 'no data'")


def test_a_never_evaluated_student_is_not_drawn_as_absent():
    client, directory = make_client()
    try:
        session_id, _ = seed(client)

        grid = client.get("/api/reports/sessions/%s/seatmap"
                          % session_id).json()
        cells = [cell for row in grid["grid"] for cell in row]
        never = [cell for cell in cells if cell["student_id"] == "S-3"]

        assert never, "the never-evaluated student is missing from the grid"
        assert never[0]["status"] is None, (
            "a student nobody decided about was given a status")

        source = reports_source()
        assert 'text: "not evaluated"' in source, (
            "the grid has no wording for a student nobody decided about")
        assert "seat-NONE" in CSS, (
            "an undecided cell is not visually distinguished from a decided "
            "one")

        print("PASS  a never-evaluated student renders as not evaluated, "
              "never as absent")
    finally:
        teardown(client, directory)


# -- the seat map is not a seating plan -------------------------------------

def test_the_seat_map_claims_no_position():
    pane = pane_html("seatmap")

    assert "not a seating plan" in pane, (
        "the page does not say the grid is not a seating plan")
    assert "roll-number order" in pane or "roll number" in pane, (
        "the page does not say what the order actually is")

    source = reports_source()
    assert 'metaItem("Order"' in source, (
        "the ordering is not shown next to the grid")
    assert "roll number, then name" in source, (
        "the server's ordering is not spelled out for a reader")

    print("PASS  the seat map says what it is and does not imply a floor plan")


def test_only_finalised_sessions_are_offered_for_a_grid():
    source = reports_source()
    assert "/api/sessions?status=COMPLETED" in source, (
        "the seat map offers sessions that have not been finalised, which "
        "have no decided attendance to print")

    print("PASS  only finalised sessions are offered for a seat map")


def test_the_column_count_is_clamped_the_same_way_on_both_sides():
    source = reports_source()
    assert "Math.max(1, Math.min(raw, 12))" in source, (
        "the page does not clamp the column count")

    client, directory = make_client()
    try:
        session_id, _ = seed(client)

        too_many = client.get("/api/reports/sessions/%s/seatmap" % session_id,
                              params={"columns": 99})
        assert too_many.status_code == 422, (
            "the backend accepted an out-of-range column count")

        fine = client.get("/api/reports/sessions/%s/seatmap" % session_id,
                          params={"columns": 3})
        assert fine.status_code == 200, fine.text
        assert fine.json()["columns"] == 3

        print("PASS  the column count is bounded on the page and refused by "
              "the backend")
    finally:
        teardown(client, directory)


def test_the_grid_column_count_comes_from_the_server():
    source = reports_source()
    assert 'setProperty("--seat-columns"' in source, (
        "the grid's column count is not taken from the server's answer")
    assert "--seat-columns" in CSS, (
        "the stylesheet does not read the column count")

    print("PASS  the grid is laid out with the column count the server "
          "reported")


# -- exports ----------------------------------------------------------------

def test_every_pane_has_an_export_and_it_carries_the_panes_own_query():
    source = reports_source()

    assert "download(\"/api/reports/attendance.pdf\", reportQuery())" in source
    assert ("download(\"/api/reports/classes.pdf\", classSummaryQuery())"
            in source), "the class summary PDF does not use the pane's filters"
    assert "/api/reports/terms.pdf" in source
    assert "/seatmap.pdf" in source

    for button in ("report-export", "report-export-pdf", "class-summary-pdf",
                   "term-summary-pdf", "seatmap-pdf"):
        assert 'id="%s"' % button in HTML, "there is no %s button" % button
        assert '$("%s").addEventListener' % button in source, (
            "the %s button is wired to nothing" % button)

    print("PASS  every pane exports what it is showing, through its own "
          "filters")


def test_an_export_is_a_navigation_rather_than_a_blob():
    source = reports_source()
    assert "window.location.href = path" in source, (
        "an export does not use the browser's own download handling")
    assert "createObjectURL" not in JS, (
        "a file is being assembled in JavaScript memory")

    print("PASS  an export is a real navigation, not a blob")


def test_a_seat_map_export_with_no_session_is_refused_with_a_message():
    source = reports_source()
    block = source[source.index("function exportSeatMapPdf"):]
    assert "Choose a session first." in block, (
        "exporting with no session chosen fails silently")
    assert "if (!sessionId)" in block, (
        "the export does not check that a session was chosen")

    print("PASS  a seat map export with no session says so")


def test_every_export_endpoint_returns_a_pdf():
    client, directory = make_client()
    try:
        session_id, _ = seed(client)

        for path in ("/api/reports/classes.pdf", "/api/reports/terms.pdf",
                     "/api/reports/sessions/%s/seatmap.pdf" % session_id,
                     "/api/reports/attendance.pdf"):
            response = client.get(path)
            assert response.status_code == 200, "%s: %s" % (path,
                                                            response.text)
            assert response.content.startswith(b"%PDF-"), (
                "%s did not return a PDF" % path)
            assert "attachment" in response.headers.get(
                "content-disposition", ""), (
                "%s is not sent as a download" % path)

        print("PASS  all four export endpoints return a PDF as a download")
    finally:
        teardown(client, directory)


# -- the rest of the page ---------------------------------------------------

def test_status_is_carried_by_a_symbol_as_well_as_a_colour():
    source = reports_source()
    assert "statusPill(seat.status)" in source, (
        "a seat's status is not drawn with the dashboard's own pill, so it "
        "would carry colour without a symbol")

    for status in ("PRESENT", "LATE", "PARTIAL", "LEFT_EARLY", "ABSENT"):
        assert ".seat-%s" % status in CSS, (
            "there is no seat styling for %s" % status)

    print("PASS  a seat's status carries a symbol and its word, not only a "
          "colour")


def test_the_new_styles_reuse_the_existing_palette():
    block = report_pane_styles()

    # No raw colour anywhere: every value is a token, so both palettes and
    # both themes are inherited rather than restated.
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)
    assert not literals, (
        "the new styles hard-code colours instead of using tokens: %s"
        % literals)
    assert "var(--" in block, "the new styles use no palette token at all"

    print("PASS  the new styles use the existing palette tokens only")


def test_nothing_in_the_new_panes_animates():
    block = report_pane_styles()

    for property_name in ("animation", "transition", "transform"):
        assert not re.search(r"^\s*%s[-a-z]*\s*:" % property_name, block,
                             re.MULTILINE), (
            "the report panes declare %s - a panel that moves is a panel "
            "that is briefly unreadable, and these are read to make a "
            "decision" % property_name)

    assert "@keyframes" not in block, "the report panes define an animation"

    print("PASS  nothing in the report panes animates")


def test_long_identifiers_cannot_overflow_the_page():
    block = report_pane_styles()

    assert ".seat-id" in block and "overflow-wrap: anywhere" in block, (
        "a long student id would widen the page instead of wrapping")
    assert "minmax(0, 1fr)" in block, (
        "a grid column cannot shrink below its content, so one long name "
        "would push the grid wider than the screen")

    print("PASS  a long identifier wraps rather than widening the page")


def test_the_panes_add_no_external_request():
    source = reports_source()
    for pattern in ("http://", "https://", "//cdn", "fonts.googleapis"):
        assert pattern not in source, (
            "the report panes reach outside this machine: %s" % pattern)

    print("PASS  the report panes make no request that leaves the machine")


def test_the_panes_carry_nothing_biometric():
    source = reports_source() + pane_html("seatmap")
    for word in ("embedding", "descriptor", "face_crop", "photo", "frame",
                 "image"):
        assert word not in source.lower(), (
            "the report panes mention %s" % word)

    print("PASS  the report panes carry nothing biometric")


def test_the_summaries_require_a_signed_in_account():
    client, directory = make_client()
    try:
        session_id, _ = seed(client)
        sign_in(client)

        from backend.app import app
        anonymous = TestClient(app)
        try:
            for path in ("/api/reports/classes", "/api/reports/terms",
                         "/api/reports/sessions/%s/seatmap" % session_id,
                         "/api/reports/classes.pdf"):
                assert anonymous.get(path).status_code == 401, (
                    "%s is readable without signing in" % path)
        finally:
            anonymous.close()

        print("PASS  every summary endpoint is 401 for an unauthenticated "
              "caller")
    finally:
        teardown(client, directory)


def test_a_session_outside_the_callers_classes_is_not_found():
    """A seat map is the easiest thing here to walk out of a building with."""
    client, directory = make_client()
    try:
        session_id, _ = seed(client)
        sign_in(client)

        created = client.post("/api/users", json={
            "username": "teacher", "display_name": "Teacher",
            "password": "another-long-enough-passphrase", "role": "TEACHER",
            "assignments": [{"class_name": "MECH-B", "section": ""}]})
        assert created.status_code == 201, created.text

        from backend.app import app
        other = TestClient(app)
        try:
            signed = other.post("/api/auth/login", json={
                "username": "teacher",
                "password": "another-long-enough-passphrase"})
            assert signed.status_code == 200, signed.text

            response = other.get("/api/reports/sessions/%s/seatmap"
                                 % session_id)
            assert response.status_code == 404, (
                "a teacher read a seat map for a class that is not theirs")

            summary = other.get("/api/reports/classes").json()
            assert summary["items"] == [], (
                "a teacher's class summary included a class that is not "
                "theirs")
        finally:
            other.close()

        print("PASS  a seat map outside the caller's classes is 404, and "
              "their summary excludes it")
    finally:
        teardown(client, directory)


def test_the_dashboard_still_serves_its_own_source():
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
    for view in ("overview", "sessions", "students", "live", "cameras",
                 "admin"):
        assert 'id="view-%s"' % view in HTML, "the %s view is gone" % view
        assert '$("view-%s").hidden = view !== "%s"' % (view, view) in JS, (
            "the %s view is no longer driven by switchView()" % view)

    # The register itself still works the way it did.
    assert 'id="report-table"' in HTML
    assert "async function loadReport()" in JS

    print("PASS  the existing views and the register are untouched")


def test_report_ui_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        assert Path(state.get_database().path).parent != real.parent
        seed(client)
        client.get("/api/reports/classes")
        client.get("/api/reports/terms")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_every_mode_button_has_a_panel_behind_it,
    test_the_reports_markup_is_balanced,
    test_the_modes_are_a_real_tablist,
    test_switching_a_mode_moves_aria_selected_and_the_hidden_attribute,
    test_the_chosen_mode_is_page_state_like_every_other_filter,
    test_each_pane_reads_an_endpoint_that_already_existed,
    test_the_endpoints_return_the_fields_the_panes_read,
    test_the_panes_invent_no_field,
    test_the_page_computes_no_attendance_figure_of_its_own,
    test_a_class_with_no_session_in_the_window_shows_no_rate,
    test_a_term_with_no_rate_says_no_data_rather_than_zero,
    test_a_never_evaluated_student_is_not_drawn_as_absent,
    test_the_seat_map_claims_no_position,
    test_only_finalised_sessions_are_offered_for_a_grid,
    test_the_column_count_is_clamped_the_same_way_on_both_sides,
    test_the_grid_column_count_comes_from_the_server,
    test_every_pane_has_an_export_and_it_carries_the_panes_own_query,
    test_an_export_is_a_navigation_rather_than_a_blob,
    test_a_seat_map_export_with_no_session_is_refused_with_a_message,
    test_every_export_endpoint_returns_a_pdf,
    test_status_is_carried_by_a_symbol_as_well_as_a_colour,
    test_the_new_styles_reuse_the_existing_palette,
    test_nothing_in_the_new_panes_animates,
    test_long_identifiers_cannot_overflow_the_page,
    test_the_panes_add_no_external_request,
    test_the_panes_carry_nothing_biometric,
    test_the_summaries_require_a_signed_in_account,
    test_a_session_outside_the_callers_classes_is_not_found,
    test_the_dashboard_still_serves_its_own_source,
    test_the_existing_views_are_untouched,
    test_report_ui_tests_never_touch_the_real_database,
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
