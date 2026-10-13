"""
Step 11: per-class and per-term summaries, the seat map, and PDF export.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHAT THIS VERIFIES

    That a summary REUSES the analytics service rather than recomputing it.
    This is the whole constraint the roadmap set for Step 11, so it is checked
    directly: the per-class rate is asserted EQUAL to the number
    AnalyticsService.class_analytics returns for that class, and the per-term
    rate equal to AnalyticsService.day_summary over that term's sessions. A
    summary that drifted from the dashboard by a rounding step would fail here,
    which is the point - a teacher must not be able to read two different
    attendance figures for one class and have no way to tell which is real.

    That "no data" survives every layer. A class with no completed session in
    the window reports None, and the PDF prints "-", because a printed 0.0%
    reads as "every student was absent" and would be a fabrication.

    That the seat map is NOT a seating plan. No seat position exists anywhere
    in this schema; the grid is roll order and says so. The ordering is checked
    to be numeric where a roll number is a number, because a register ordered
    1, 10, 11, 2 is the kind of small wrongness that makes a teacher stop
    trusting a printout.

    That a student expected at a completed session and never evaluated appears
    with NO status in the grid and in the PDF, never as absent.

    That the PDF is a real PDF. The bytes are parsed back - header, xref table,
    every offset, trailer - and the page's text is decompressed and searched,
    rather than trusting that a 200 with some bytes in it would open. An export
    nobody can open is worse than no export.

    That a PDF is SCOPED. An export is the easiest thing in a system to walk
    out of a building with, so a teacher's file is checked for the other
    class's names in its DECOMPRESSED BYTES, not by trusting a row count.

WHAT THIS DOES NOT VERIFY

    Any attendance rule. Every record here was written by this file through the
    Step 6 repository; a report is a read.

    That a PDF LOOKS right. Structure and content are checked, not typography.
    The four files this suite builds were also opened in a real reader by hand.

Run with:
    python tests/test_report_exports.py
"""

import re
import shutil
import sys
import tempfile
import warnings
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from attendance_rules import AttendanceDecision  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from repositories import AttendanceRepository, SessionRepository  # noqa: E402

from backend.services import pdf_writer, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

BASE = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

# Roll numbers chosen so string ordering and numeric ordering DISAGREE: as text
# "10" sorts before "2", so a seat map in the wrong order is visible.
MINE = [("X-M1", "Asha Rao", "1"), ("X-M2", "Vikram Nair", "10"),
        ("X-M3", "Bela Kar", "2"), ("X-M4", "No Roll", "")]
THEIRS = [("X-C1", "Leena Bose", "1")]

TERM = {"academic_year": "2026-27", "semester": "1",
        "start_date": "2026-03-01", "end_date": "2026-07-31"}


# -- harness ----------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_exports_"))
    state.configure(directory / "exports.db")
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

    created = client.post("/api/users", json={
        "username": "tsmith", "display_name": "T Smith",
        "password": TEACHER_PASSWORD, "role": "TEACHER",
        "assignments": [{"class_name": "MECH", "section": "A"}]})
    assert created.status_code == 201, created.text

    for students, class_name, section in ((MINE, "MECH", "A"),
                                          (THEIRS, "CIVIL", "B")):
        for student_id, name, roll in students:
            response = client.post("/api/students", json={
                "student_id": student_id, "name": name, "roll_number": roll,
                "class_name": class_name, "section": section})
            assert response.status_code == 201, response.text


def make_term(client):
    response = client.post("/api/academic/sessions", json=TERM)
    assert response.status_code == 201, response.text
    return response.json()["academic_session_id"]


def completed(session_id, class_id, student_ids, day=0, academic=None):
    start = BASE + timedelta(days=day)
    end = start + timedelta(hours=1)

    session = AttendanceSession(session_id, class_id, start, end,
                               policy_version="1.0",
                               expected_students=list(student_ids))
    session.start()
    session.complete(completed_at=end)
    SessionRepository(state.get_database()).save(session)

    if academic:
        state.get_database().execute(
            "UPDATE attendance_sessions SET academic_session_id = ? "
            "WHERE session_id = ?", (academic, session_id))
    return session


def record(session_id, student_id, status, ratio=0.9):
    decision = AttendanceDecision(student_id, session_id, status,
                                  "synthetic test record", "1.0")
    decision.present_seconds = 3240.0
    decision.session_seconds = 3600.0
    decision.presence_ratio = ratio
    AttendanceRepository(state.get_database()).save_decision(decision)


def a_school(term_id=None):
    """
    Two classes. MECH-A has two sessions, one of which leaves X-M4 expected
    and never evaluated; CIVIL-B has one, deliberately filed under NO term.
    """
    completed("X-S1", "MECH-A", [sid for sid, _, _ in MINE], day=0,
              academic=term_id)
    record("X-S1", "X-M1", "PRESENT")
    record("X-S1", "X-M2", "ABSENT")
    record("X-S1", "X-M3", "LATE")
    # X-M4 is expected here and never evaluated, on purpose.

    completed("X-S2", "MECH-A", ["X-M1", "X-M2"], day=1, academic=term_id)
    record("X-S2", "X-M1", "PARTIAL", ratio=0.4)
    record("X-S2", "X-M2", "PRESENT")

    # Filed under no term at all, the way every session recorded before
    # academic terms existed is.
    completed("X-OLD", "CIVIL-B", ["X-C1"], day=2)
    record("X-OLD", "X-C1", "PRESENT")


def analytics():
    from backend.services.analytics_service import AnalyticsService
    return AnalyticsService(state.get_database())


# -- reading a PDF back ------------------------------------------------------

def pdf_objects(raw):
    """
    Every offset in the xref table, checked against the object it claims.

    A PDF whose xref is wrong opens in some readers and not others, which is
    the worst way for an export to fail - it works on the machine it was
    tested on. "startxref" contains the substring "xref", so the table is
    found through the startxref VALUE rather than by searching for the word.
    """
    assert raw.startswith(b"%PDF-1.4"), "no PDF header"
    assert raw.rstrip().endswith(b"%%EOF"), "no EOF marker"

    marker = raw.rfind(b"startxref")
    assert marker > 0, "no startxref"
    start = int(raw[marker + len("startxref"):].split()[0])
    assert raw[start:start + 4] == b"xref", "startxref does not point at xref"

    lines = raw[start:].split(b"\n")
    count = int(lines[1].split()[1])

    for number in range(1, count):
        offset = int(lines[2 + number].split()[0])
        expected = b"%d 0 obj" % number
        assert raw[offset:offset + len(expected)] == expected, \
            "xref entry %d points at %r" % (number, raw[offset:offset + 20])

    return count - 1


def pdf_text(raw):
    """
    Every string drawn on every page, from the decompressed content streams.

    The text operators are read rather than rendered: this is enough to prove
    a name IS or IS NOT in a file, which is what the scope tests need.
    """
    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw,
                             re.DOTALL):
        try:
            body = zlib.decompress(match.group(1))
        except zlib.error:
            continue
        for piece in re.finditer(rb"\((.*?)\)\s*Tj", body, re.DOTALL):
            out.append(piece.group(1).decode("latin-1"))
    return "\n".join(out)


def pdf_pages(raw):
    return raw.count(b"/Type /Page ")


# -- the PDF writer itself ---------------------------------------------------

def test_the_pdf_writer_produces_a_file_whose_xref_is_sound():
    columns = (("Name", 2.0, "left"), ("Status", 1.0, "left"),
               ("Ratio", 1.0, "right"))
    rows = [["Student %d" % index, "PRESENT", "0.900"] for index in range(200)]

    body, report = pdf_writer.write_table(
        "Structure", ["a subtitle"], columns, rows, footer="footer")

    objects = pdf_objects(body)

    # 4 fixed objects (catalogue, page tree, two fonts) + 2 per page.
    assert objects == 4 + 2 * report["pages"], (objects, report)
    assert report["pages"] > 1, "200 rows should not fit on one page"
    assert pdf_pages(body) == report["pages"]
    assert report["rows"] == 200

    print("PASS  the writer produces a %d-page PDF whose %d xref offsets all "
          "point at their objects" % (report["pages"], objects))


def test_a_character_helvetica_cannot_draw_is_counted_not_hidden():
    """
    The base-14 font is Latin-1 and a Devanagari name cannot be drawn.

    What must not happen is a silent "???" on a printed register. The
    substitution is allowed; going unreported is not.
    """
    columns = (("Name", 1.0, "left"),)

    plain, clean = pdf_writer.write_table("t", [], columns, [["Asha Rao"]])
    assert clean["unencodable_characters"] == 0, clean

    name = "प्रियंका"
    body, report = pdf_writer.write_table("t", [], columns, [[name]])

    assert report["unencodable_characters"] == len(name), \
        (report, len(name))
    assert "?" * len(name) in pdf_text(body), "the name was not substituted"
    assert name not in pdf_text(body)

    # An accented Latin-1 name is drawable and must NOT be counted.
    latin, report = pdf_writer.write_table("t", [], columns, [["José Muñoz"]])
    assert report["unencodable_characters"] == 0, report

    print("PASS  a name Helvetica cannot draw becomes ? and is COUNTED (%d "
          "characters), while Latin-1 accents are drawn and not counted"
          % len(name))


def test_a_cell_too_wide_for_its_column_is_cut_not_overrun():
    wide = "A" * 400
    columns = (("Narrow", 1.0, "left"), ("Also narrow", 1.0, "left"))

    body, _ = pdf_writer.write_table("t", [], columns, [[wide, wide]])
    # Only the cells, not the "Narrow"/"Also narrow" headings, which also
    # contain an A and are not truncated.
    # split("\n"), NOT splitlines(). The WinAnsi ellipsis is byte 0x85, which
    # is U+0085 NEL once decoded, and str.splitlines() treats that as a line
    # break - so splitlines() would split the string ON the very character
    # this test is looking for and report it missing.
    drawn = [line for line in pdf_text(body).split("\n")
             if line.startswith("AAA")]

    assert len(drawn) == 2, drawn
    for line in drawn:
        assert len(line) < len(wide), "a cell was not truncated"
        # Byte 0x85, the WinAnsi ellipsis - NOT U+2026, which the base-14 font
        # cannot address. pdf_text decodes the stream as latin-1, so this is
        # the literal byte the reader will be handed.
        assert line.endswith(pdf_writer.ELLIPSIS), \
            "a truncated cell has no ellipsis: %r" % line[-4:]

    # And the measurement it relies on is real, not a character count.
    assert pdf_writer.text_width("iii", 10) < pdf_writer.text_width("WWW", 10)
    assert pdf_writer.truncate("", 50, 8) == ""
    assert pdf_writer.truncate("abc", 0, 8) == ""

    print("PASS  a cell wider than its column is truncated with a real "
          "one-glyph ellipsis, measured by Helvetica widths not characters")


def test_the_apostrophe_a_word_processor_produces_is_drawn_not_replaced():
    """
    U+2019 is what every word processor makes of a typed apostrophe.

    Helvetica CAN draw it - at WinAnsi byte 0x92 - so replacing it would print
    "O?Brien" for a name the font handles perfectly. This is the case that
    makes the WinAnsi mapping worth having rather than just encoding Latin-1.
    """
    columns = (("Name", 1.0, "left"),)

    body, report = pdf_writer.write_table("t", [], columns,
                                          [["O’Brien"], ["“quoted” – dash"]])

    assert report["unencodable_characters"] == 0, report

    text = pdf_text(body)
    assert "?" not in text, text
    assert chr(0x92) in text, "the apostrophe did not become byte 0x92"
    assert chr(0x93) in text and chr(0x94) in text, "curly quotes lost"
    assert chr(0x96) in text, "en dash lost"

    print("PASS  a curly apostrophe, curly quotes and an en dash are "
          "translated to the bytes that draw them, not replaced with ?")


def test_parentheses_and_backslashes_do_not_break_the_page():
    """
    "(" ")" and "\\" are PDF syntax. A student named O'(Brien\\ must not
    corrupt the content stream - the file simply would not open.
    """
    awkward = "Back\\slash (and) parens"
    body, _ = pdf_writer.write_table("t", [], (("Name", 1.0, "left"),),
                                     [[awkward]])

    pdf_objects(body)
    assert awkward in pdf_text(body).replace("\\\\", "\\") \
        .replace("\\(", "(").replace("\\)", ")"), pdf_text(body)

    print("PASS  parentheses and backslashes are escaped, and the file still "
          "parses")


# -- per-class summary -------------------------------------------------------

def test_the_class_summary_reports_the_analytics_services_own_numbers():
    """
    The Step 11 constraint, asserted directly rather than described.

    If this ever fails, the Reports tab and the Overview tab are telling a
    teacher two different things about one class.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        body = client.get("/api/reports/classes").json()
        rows = {row["class_id"]: row for row in body["items"]}
        assert set(rows) == {"MECH-A", "CIVIL-B"}, rows.keys()

        service = analytics()
        for class_id, row in rows.items():
            truth = service.class_analytics(class_id)

            assert row["attendance_rate"] == truth["attendance_rate"], \
                (class_id, row["attendance_rate"], truth["attendance_rate"])
            assert row["average_presence_ratio"] == \
                truth["average_presence_ratio"], class_id
            assert row["completed_sessions"] == truth["completed_sessions"]
            assert row["student_count"] == truth["student_count"]
            assert row["counts"] == truth["counts"], class_id
            assert row["absence_rate"] == truth["absence_rate"], class_id

        print("PASS  every per-class figure equals the one "
              "AnalyticsService.class_analytics returns, for all %d classes"
              % len(rows))
    finally:
        teardown(client, directory)


def test_the_summary_is_ordered_worst_first_and_totals_are_weighted():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        body = client.get("/api/reports/classes").json()
        rates = [row["attendance_rate"] for row in body["items"]]

        assert rates == sorted(rates), rates
        assert rates[0] == 0.5 and rates[-1] == 1.0, rates

        totals = body["totals"]
        assert totals["classes"] == 2
        assert totals["completed_sessions"] == 3
        # 6 expected in MECH-A + 1 in CIVIL-B; 3 satisfying + 1 = 4/7.
        assert totals["expected_students"] == 7, totals
        assert totals["attendance_rate"] == round(4 / 7, 4), totals

        # NOT the mean of 0.5 and 1.0, which is what an unweighted roll-up
        # would report and is the figure nobody can reproduce.
        assert totals["attendance_rate"] != 0.75, totals

        print("PASS  worst attendance first, and the total is weighted by "
              "expected students (%.4f) rather than a mean of rates (0.75)"
              % totals["attendance_rate"])
    finally:
        teardown(client, directory)


def test_a_window_with_no_session_in_it_reports_nothing_not_zero():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        body = client.get("/api/reports/classes"
                          "?date_from=2030-01-01").json()

        assert body["items"], "the classes themselves should still be listed"
        for row in body["items"]:
            assert row["completed_sessions"] == 0, row
            assert row["attendance_rate"] is None, row
            assert row["average_presence_ratio"] is None, row
            assert row["windowed"] is True, row

        assert body["totals"]["attendance_rate"] is None, body["totals"]

        # And the PDF prints a dash rather than a confident 0.0%.
        response = client.get("/api/reports/classes.pdf"
                              "?date_from=2030-01-01")
        assert response.status_code == 200
        text = pdf_text(response.content)
        assert "0.0%" not in text, text
        assert "-" in text

        print("PASS  a window containing no session reports None everywhere "
              "and prints \"-\", never 0.0%")
    finally:
        teardown(client, directory)


def test_a_window_narrows_the_sessions_it_counts():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        # X-S1 is 2026-03-02, X-S2 is 03-03, X-OLD is 03-04.
        body = client.get("/api/reports/classes"
                          "?date_from=2026-03-03").json()
        rows = {row["class_id"]: row for row in body["items"]}

        assert rows["MECH-A"]["completed_sessions"] == 1, rows["MECH-A"]
        # X-S2 alone: X-M1 PARTIAL, X-M2 PRESENT - 1 satisfying of 2 expected.
        assert rows["MECH-A"]["attendance_rate"] == 0.5, rows["MECH-A"]
        assert rows["MECH-A"]["first_session"] == "2026-03-03"

        assert rows["CIVIL-B"]["completed_sessions"] == 1
        assert rows["CIVIL-B"]["attendance_rate"] == 1.0

        print("PASS  a date window counts only the sessions inside it "
              "(MECH-A: 2 sessions to 1, rate 0.5)")
    finally:
        teardown(client, directory)


# -- per-term summary --------------------------------------------------------

def test_the_term_summary_reports_day_summarys_own_numbers():
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)
        a_school(term_id)

        body = client.get("/api/reports/terms").json()
        named = [row for row in body["items"] if not row["unassigned"]]
        assert len(named) == 1, body["items"]

        term = named[0]
        truth = analytics().day_summary(["X-S1", "X-S2"])

        assert term["attendance_rate"] == truth["attendance_rate"], \
            (term["attendance_rate"], truth["attendance_rate"])
        assert term["expected_students"] == truth["expected_students"]
        assert term["evaluated_students"] == truth["evaluated_students"]
        assert term["counts"] == truth["counts"]
        assert term["completed_sessions"] == truth["completed_sessions"] == 2

        print("PASS  the term's figures equal AnalyticsService.day_summary "
              "over its sessions (rate %s over %d expected)"
              % (term["attendance_rate"], term["expected_students"]))
    finally:
        teardown(client, directory)


def test_a_session_belonging_to_no_term_is_reported_as_unassigned():
    """
    Every session recorded before academic terms existed has a NULL term.

    Dropping those rows would understate a school's attendance and folding
    them into the current term would file a lesson under a term it did not
    happen in. Both are worse than saying so.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)
        a_school(term_id)

        body = client.get("/api/reports/terms").json()
        loose = [row for row in body["items"] if row["unassigned"]]

        assert len(loose) == 1, body["items"]
        assert loose[0]["academic_session_id"] is None
        assert loose[0]["completed_sessions"] == 1
        assert [entry["class_id"] for entry in loose[0]["classes"]] \
            == ["CIVIL-B"]
        assert "not assigned" in loose[0]["label"].lower()

        # And it sorts after the real term rather than above it.
        assert body["items"][-1]["unassigned"] is True

        print("PASS  a session filed under no term is reported as unassigned, "
              "last, and never folded into a real term")
    finally:
        teardown(client, directory)


def test_a_term_breaks_down_into_its_classes():
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)
        a_school(term_id)

        body = client.get("/api/reports/terms?academic_session_id=%s"
                          % term_id).json()

        assert body["total"] == 1, body
        term = body["items"][0]
        assert term["academic_year"] == "2026-27"
        assert term["semester"] == "1"

        classes = {entry["class_id"]: entry for entry in term["classes"]}
        assert set(classes) == {"MECH-A"}, classes.keys()

        truth = analytics().day_summary(["X-S1", "X-S2"])
        assert classes["MECH-A"]["attendance_rate"] == truth["attendance_rate"]

        print("PASS  a term breaks down into its classes, each from the same "
              "day_summary call")
    finally:
        teardown(client, directory)


# -- the seat map ------------------------------------------------------------

def test_the_seat_map_is_in_roll_order_and_numbers_sort_as_numbers():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        body = client.get("/api/reports/sessions/X-S1/seatmap"
                          "?columns=2").json()

        flat = [cell for line in body["grid"] for cell in line]
        rolls = [cell["roll_number"] for cell in flat]

        # 1, 2, 10 - not 1, 10, 2, which is what text ordering gives.
        assert rolls == ["1", "2", "10", ""], rolls
        assert body["columns"] == 2
        assert body["rows"] == 2
        assert body["seats"] == 4
        assert body["ordering"] == "roll_number_then_name"
        assert "not" in body["note"].lower() and "seat" in body["note"].lower()

        print("PASS  the grid is roll order with numeric rolls sorted "
              "numerically (%s) and a blank roll last" % ", ".join(rolls[:3]))
    finally:
        teardown(client, directory)


def test_a_student_never_evaluated_has_no_status_in_the_grid():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        body = client.get("/api/reports/sessions/X-S1/seatmap").json()
        cells = {cell["student_id"]: cell
                 for line in body["grid"] for cell in line}

        assert cells["X-M4"]["status"] is None, cells["X-M4"]
        assert cells["X-M4"]["evaluated"] is False
        assert cells["X-M2"]["status"] == "ABSENT"

        # And on paper: "not evaluated", never folded in with the absences.
        response = client.get("/api/reports/sessions/X-S1/seatmap.pdf")
        text = pdf_text(response.content)
        assert "not evaluated" in text, text
        assert text.count("ABSENT") == 1, text

        print("PASS  an expected-but-never-evaluated student has a null "
              "status in the grid and prints as \"not evaluated\", not absent")
    finally:
        teardown(client, directory)


def test_the_seat_map_never_claims_to_know_where_anybody_sat():
    """
    No table in this schema holds a seat, so nothing may imply one.

    Checked on the PRINTED page rather than only in the JSON, because the
    printed page is what leaves the building.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        response = client.get("/api/reports/sessions/X-S1/seatmap.pdf")
        text = pdf_text(response.content).lower()

        assert "not a seating plan" in text, text
        assert "roll order" in text, text

        # And no coordinate is exposed by the JSON either.
        body = client.get("/api/reports/sessions/X-S1/seatmap").json()
        flat = [cell for line in body["grid"] for cell in line]
        for cell in flat:
            for forbidden in ("seat_x", "seat_y", "x", "y", "row", "column",
                              "position", "bbox"):
                assert forbidden not in cell, (forbidden, sorted(cell))

        print("PASS  the printed sheet says it is not a seating plan, and no "
              "cell carries a coordinate of any kind")
    finally:
        teardown(client, directory)


def test_a_seat_map_outside_the_callers_classes_is_not_found():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        try:
            assert teacher.get(
                "/api/reports/sessions/X-S1/seatmap").status_code == 200

            # CIVIL-B is not theirs. 404, the same answer as a session that
            # does not exist - not 403, which would confirm it exists.
            for path in ("/api/reports/sessions/X-OLD/seatmap",
                         "/api/reports/sessions/X-OLD/seatmap.pdf",
                         "/api/reports/sessions/NOPE/seatmap"):
                assert teacher.get(path).status_code == 404, path
        finally:
            teacher.close()

        print("PASS  a seat map for another teacher's class is 404, "
              "indistinguishable from one that does not exist")
    finally:
        teardown(client, directory)


# -- the PDF endpoints -------------------------------------------------------

def test_every_pdf_endpoint_returns_a_pdf_a_reader_could_open():
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)
        a_school(term_id)

        paths = ("/api/reports/attendance.pdf",
                 "/api/reports/classes.pdf",
                 "/api/reports/terms.pdf",
                 "/api/reports/sessions/X-S1/seatmap.pdf")

        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text[:200])
            assert response.headers["content-type"] == "application/pdf", path

            pdf_objects(response.content)

            assert "attachment" in response.headers["content-disposition"]
            assert response.headers["content-disposition"].endswith('.pdf"')
            assert "no-store" in response.headers["cache-control"]
            assert int(response.headers["X-Report-Pages"]) >= 1
            assert "X-Report-Unencodable" in response.headers

        print("PASS  all %d PDF endpoints return a structurally sound PDF, "
              "as a no-store attachment with a named file" % len(paths))
    finally:
        teardown(client, directory)


def test_the_pdf_is_built_from_the_same_rows_as_the_json_and_the_csv():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        query = "?class_id=MECH-A&include_unevaluated=true"

        report = client.get("/api/reports/attendance" + query).json()
        pdf = client.get("/api/reports/attendance.pdf" + query)

        assert int(pdf.headers["X-Report-Rows"]) == report["returned"], \
            (pdf.headers["X-Report-Rows"], report["returned"])

        text = pdf_text(pdf.content)
        for row in report["items"]:
            assert row["student_id"] in text, row["student_id"]

        # The CSV agrees on the row count too - three exports, one call.
        csv_response = client.get("/api/reports/attendance.csv" + query)
        assert int(csv_response.headers["X-Report-Rows"]) == report["returned"]

        print("PASS  the PDF, the CSV and the JSON report all carry the same "
              "%d rows" % report["returned"])
    finally:
        teardown(client, directory)


def test_a_teachers_pdf_does_not_contain_another_classs_students():
    """
    Checked in the decompressed BYTES, not by counting rows.

    An export is the easiest thing in a system to walk out of a building with,
    so the question is whether the name is in the file at all.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)
        a_school(term_id)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        try:
            for path in ("/api/reports/attendance.pdf",
                         "/api/reports/classes.pdf",
                         "/api/reports/terms.pdf"):
                response = teacher.get(path)
                assert response.status_code == 200, path
                text = pdf_text(response.content)

                for _, name, _ in THEIRS:
                    assert name not in text, (path, name)
                assert "CIVIL" not in text, path

                if path == "/api/reports/attendance.pdf":
                    assert MINE[0][1] in text, "their own student is missing"
        finally:
            teacher.close()

        print("PASS  no PDF a teacher can download contains the other "
              "class's students or even its class id")
    finally:
        teardown(client, directory)


def test_the_unencodable_count_reaches_the_client_as_a_header():
    """
    A register that silently prints a student as "???" is the failure here.

    The substitution is unavoidable with a base-14 font; going unannounced is
    not, so the page that offered the download can point at the CSV instead.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        assert client.post("/api/students", json={
            "student_id": "X-M9", "name": "प्रियंका दास", "roll_number": "9",
            "class_name": "MECH", "section": "A"}).status_code == 201

        completed("X-S9", "MECH-A", ["X-M9"], day=3)
        record("X-S9", "X-M9", "PRESENT")

        plain = client.get("/api/reports/attendance.pdf?session_id=X-S1")
        assert plain.headers["X-Report-Unencodable"] == "0", plain.headers

        marked = client.get("/api/reports/attendance.pdf?session_id=X-S9")
        assert int(marked.headers["X-Report-Unencodable"]) > 0, marked.headers

        # The CSV has no such limit and carries the real name.
        csv_response = client.get("/api/reports/attendance.csv"
                                  "?session_id=X-S9")
        assert "प्रियंका दास" in csv_response.text

        print("PASS  X-Report-Unencodable is 0 for a Latin-1 register and %s "
              "for a Devanagari name, which the CSV still carries intact"
              % marked.headers["X-Report-Unencodable"])
    finally:
        teardown(client, directory)


def test_no_export_exposes_a_biometric_or_a_secret():
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)
        a_school(term_id)

        forbidden = ("embedding", "descriptor", "face_vector", "password",
                     "password_hash", "session_token", "onnx", "\\models\\",
                     "/models/", "secret")

        for path in ("/api/reports/attendance.pdf", "/api/reports/classes.pdf",
                     "/api/reports/terms.pdf",
                     "/api/reports/sessions/X-S1/seatmap.pdf"):
            blob = client.get(path).content.lower()
            for needle in forbidden:
                assert needle.encode("latin-1") not in blob, (path, needle)

        for path in ("/api/reports/classes", "/api/reports/terms",
                     "/api/reports/sessions/X-S1/seatmap"):
            body = client.get(path).text.lower()
            for needle in forbidden:
                assert needle not in body, (path, needle)

        print("PASS  no summary, seat map or PDF carries an embedding, a "
              "model path or a credential (%d patterns)" % len(forbidden))
    finally:
        teardown(client, directory)


def test_the_new_endpoints_require_a_signed_in_account():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        from backend.app import app
        anonymous = TestClient(app)
        try:
            for path in ("/api/reports/classes", "/api/reports/classes.pdf",
                         "/api/reports/terms", "/api/reports/terms.pdf",
                         "/api/reports/attendance.pdf",
                         "/api/reports/sessions/X-S1/seatmap",
                         "/api/reports/sessions/X-S1/seatmap.pdf"):
                assert anonymous.get(path).status_code == 401, path
        finally:
            anonymous.close()

        print("PASS  all 7 new report endpoints refuse an unauthenticated "
              "caller")
    finally:
        teardown(client, directory)


def test_an_impossible_date_is_refused_rather_than_widening_a_report():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        for query in ("date_from=2026-13-45", "date_to=2026-02-30"):
            response = client.get("/api/reports/classes?" + query)
            assert response.status_code == 422, (query, response.status_code)

        assert client.get("/api/reports/sessions/X-S1/seatmap"
                          "?columns=0").status_code == 422
        assert client.get("/api/reports/sessions/X-S1/seatmap"
                          "?columns=99").status_code == 422

        print("PASS  a date that matches the pattern but is not a real day is "
              "422, and the seat grid width is bounded")
    finally:
        teardown(client, directory)


# -- runner -----------------------------------------------------------------

TESTS = [
    test_the_pdf_writer_produces_a_file_whose_xref_is_sound,
    test_a_character_helvetica_cannot_draw_is_counted_not_hidden,
    test_a_cell_too_wide_for_its_column_is_cut_not_overrun,
    test_the_apostrophe_a_word_processor_produces_is_drawn_not_replaced,
    test_parentheses_and_backslashes_do_not_break_the_page,
    test_the_class_summary_reports_the_analytics_services_own_numbers,
    test_the_summary_is_ordered_worst_first_and_totals_are_weighted,
    test_a_window_with_no_session_in_it_reports_nothing_not_zero,
    test_a_window_narrows_the_sessions_it_counts,
    test_the_term_summary_reports_day_summarys_own_numbers,
    test_a_session_belonging_to_no_term_is_reported_as_unassigned,
    test_a_term_breaks_down_into_its_classes,
    test_the_seat_map_is_in_roll_order_and_numbers_sort_as_numbers,
    test_a_student_never_evaluated_has_no_status_in_the_grid,
    test_the_seat_map_never_claims_to_know_where_anybody_sat,
    test_a_seat_map_outside_the_callers_classes_is_not_found,
    test_every_pdf_endpoint_returns_a_pdf_a_reader_could_open,
    test_the_pdf_is_built_from_the_same_rows_as_the_json_and_the_csv,
    test_a_teachers_pdf_does_not_contain_another_classs_students,
    test_the_unencodable_count_reaches_the_client_as_a_header,
    test_no_export_exposes_a_biometric_or_a_secret,
    test_the_new_endpoints_require_a_signed_in_account,
    test_an_impossible_date_is_refused_rather_than_widening_a_report,
]


def main():
    enable_safe_console()

    passed = 0
    for test in TESTS:
        try:
            test()
            passed += 1
        except AssertionError as failure:
            print("FAIL  %s: %s" % (test.__name__, failure))
        except Exception as error:                       # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(error).__name__, error))

    print("")
    print("%d/%d tests passed." % (passed, len(TESTS)))
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
