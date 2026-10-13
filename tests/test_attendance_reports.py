"""
Attendance reports: filtering, CSV export, and who may export what.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That the report SELECTS decisions and never makes one. Every filter is
    checked to narrow the same rows rather than change any status, the
    re-evaluated-under-a-new-policy case is checked to produce ONE row per
    student per session rather than a student who is both present and absent
    for the same lesson, and a student expected at a completed session who was
    never evaluated is reported with no status rather than as absent.

    That the CSV is the same rows as the JSON. They are built from one service
    call, and this asserts the file's contents against the table's, because a
    school that files an export which disagrees with the screen has a problem
    nobody will find for a term.

    And that a CSV is scoped. An export is the easiest thing in a system to
    walk out of a building with, so a teacher's file contains their classes and
    their students and nothing else - checked by looking for the other class's
    names in the bytes, not by trusting the row count.

WHAT THIS DOES NOT VERIFY
    Any attendance rule. Every row here was written by this file through the
    Step 6 repository; the report is a read.

Run with:
    python tests/test_attendance_reports.py
"""

import csv
import io
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

from fastapi.testclient import TestClient  # noqa: E402

from attendance_rules import AttendanceDecision  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from repositories import AttendanceRepository, SessionRepository  # noqa: E402

from backend.services import state  # noqa: E402
from backend.services.report_service import CSV_COLUMNS  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

BASE = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

MINE = [("R-M1", "Asha Rao"), ("R-M2", "Vikram Nair")]
THEIRS = [("R-C1", "Leena Bose")]


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_report_"))
    state.configure(directory / "reports.db")
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
        for student_id, name in students:
            response = client.post("/api/students", json={
                "student_id": student_id, "name": name, "roll_number": "1",
                "class_name": class_name, "section": section})
            assert response.status_code == 201, response.text

    return created.json()


def completed(session_id, class_id, student_ids, day=0,
              policy_version="1.0"):
    start = BASE + timedelta(days=day)
    end = start + timedelta(hours=1)

    session = AttendanceSession(session_id, class_id, start, end,
                                policy_version=policy_version,
                                expected_students=list(student_ids))
    session.start()
    session.complete(completed_at=end)
    SessionRepository(state.get_database()).save(session)
    return session


def record(session_id, student_id, status, policy_version="1.0", ratio=0.9):
    decision = AttendanceDecision(student_id, session_id, status,
                                  "synthetic test record", policy_version)
    decision.present_seconds = 3240.0
    decision.session_seconds = 3600.0
    decision.presence_ratio = ratio
    AttendanceRepository(state.get_database()).save_decision(decision)


def two_classes():
    """Two sessions in each class, over two days, with known statuses."""
    completed("R-MECH-1", "MECH-A", [sid for sid, _ in MINE], day=0)
    record("R-MECH-1", "R-M1", "PRESENT")
    record("R-MECH-1", "R-M2", "ABSENT")

    completed("R-MECH-2", "MECH-A", [sid for sid, _ in MINE], day=1)
    record("R-MECH-2", "R-M1", "LATE")
    record("R-MECH-2", "R-M2", "PRESENT")

    completed("R-CIVIL-1", "CIVIL-B", [sid for sid, _ in THEIRS], day=0)
    record("R-CIVIL-1", "R-C1", "PRESENT")


def rows_of(response):
    assert response.status_code == 200, response.text
    return response.json()["items"]


def parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


# -- selecting, not deciding -------------------------------------------------

def test_an_unfiltered_report_holds_every_stored_decision_once():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        report = client.get("/api/reports/attendance")
        assert report.status_code == 200, report.text
        body = report.json()

        assert body["total"] == 5, body["total"]
        assert body["truncated"] is False

        keys = [(row["session_id"], row["student_id"])
                for row in body["items"]]
        assert len(keys) == len(set(keys)), "a decision appeared twice"

        # Newest session first, which is how a register is read.
        dates = [row["session_date"] for row in body["items"]]
        assert dates == sorted(dates, reverse=True), dates

        print("PASS  an unfiltered report holds every stored decision exactly "
              "once, newest session first")
    finally:
        teardown(client, directory)


def test_each_filter_narrows_the_same_rows_and_changes_no_status():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        everything = {(row["session_id"], row["student_id"]): row["status"]
                      for row in rows_of(
                          client.get("/api/reports/attendance"))}

        cases = [
            ("student_id=R-M1", 2),
            ("class_id=MECH-A", 4),
            ("class_id=CIVIL-B", 1),
            ("section=A", 4),
            ("section=B", 1),
            ("status=PRESENT", 3),
            ("status=ABSENT", 1),
            ("session_id=R-MECH-1", 2),
            ("date_from=%s" % (BASE + timedelta(days=1)).date(), 2),
            ("date_to=%s" % BASE.date(), 3),
            ("class_id=MECH-A&status=LATE", 1),
            ("student_id=R-M1&status=PRESENT", 1),
        ]

        for query, expected in cases:
            rows = rows_of(client.get("/api/reports/attendance?" + query))
            assert len(rows) == expected, (
                "%s returned %d rows, expected %d"
                % (query, len(rows), expected))

            # And every row is the SAME decision it was without the filter.
            for row in rows:
                key = (row["session_id"], row["student_id"])
                assert row["status"] == everything[key], (
                    "%s changed %s's status for %s from %s to %s"
                    % (query, row["student_id"], row["session_id"],
                       everything[key], row["status"]))

        print("PASS  every filter narrows the selection and none of them "
              "alters a recorded status")
    finally:
        teardown(client, directory)


def test_the_summary_counts_exactly_the_rows_returned():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        body = client.get("/api/reports/attendance?class_id=MECH-A").json()
        summary = body["summary"]

        assert summary["rows"] == len(body["items"]) == 4
        assert summary["students"] == 2
        assert summary["sessions"] == 2
        assert summary["counts"] == {"PRESENT": 2, "ABSENT": 1, "LATE": 1}, \
            summary["counts"]

        # PRESENT and LATE satisfy; ABSENT does not.
        assert summary["satisfying_count"] == 3
        assert abs(summary["attendance_rate"] - 0.75) < 1e-9

        # An empty selection has no rate rather than 0.0.
        empty = client.get("/api/reports/attendance?student_id=NOBODY").json()
        assert empty["summary"]["rows"] == 0
        assert empty["summary"]["attendance_rate"] is None, (
            "an empty report was given an attendance rate")

        print("PASS  the summary under a report counts exactly its own rows, "
              "and an empty report has no rate rather than 0%")
    finally:
        teardown(client, directory)


def test_a_re_evaluated_session_reports_one_decision_not_two():
    client, directory = make_client()
    try:
        setup_school(client)

        # A session run under policy 1.0 and then re-evaluated under 2.0. Both
        # rows exist, deliberately - history is not overwritten in this project.
        completed("R-REPOLICY", "MECH-A", ["R-M1"], day=0,
                  policy_version="2.0")
        record("R-REPOLICY", "R-M1", "ABSENT", policy_version="1.0")
        record("R-REPOLICY", "R-M1", "PRESENT", policy_version="2.0")

        rows = rows_of(client.get(
            "/api/reports/attendance?session_id=R-REPOLICY"))

        assert len(rows) == 1, (
            "a re-evaluated session reported the student twice: %s"
            % [(row["status"], row["policy_version"]) for row in rows])

        # The session's own policy wins, so the current decision is reported.
        assert rows[0]["policy_version"] == "2.0", rows[0]
        assert rows[0]["status"] == "PRESENT", rows[0]

        # And naming a policy explicitly reports what was stored under it,
        # which is what somebody comparing two policies wants.
        old = rows_of(client.get("/api/reports/attendance"
                                 "?session_id=R-REPOLICY&policy_version=1.0"))
        assert len(old) == 1
        assert old[0]["status"] == "ABSENT", old[0]

        print("PASS  a re-evaluated session reports one effective decision, "
              "and an explicit policy version reports that policy's own")
    finally:
        teardown(client, directory)


def test_a_never_evaluated_student_is_not_reported_as_absent():
    client, directory = make_client()
    try:
        setup_school(client)

        # Both expected, only one evaluated.
        completed("R-GAP", "MECH-A", [sid for sid, _ in MINE], day=0)
        record("R-GAP", "R-M1", "PRESENT")

        default = rows_of(client.get(
            "/api/reports/attendance?session_id=R-GAP"))
        assert len(default) == 1, (
            "the unevaluated student appeared without being asked for")

        asked = client.get("/api/reports/attendance"
                           "?session_id=R-GAP&include_unevaluated=true").json()
        assert len(asked["items"]) == 2, asked["items"]

        gap = [row for row in asked["items"] if row["student_id"] == "R-M2"][0]

        # The whole point: no status, NOT absent. Nobody decided this.
        assert gap["status"] is None, gap
        assert gap["evaluated"] is False
        assert gap["policy_version"] is None
        assert "never evaluated" in gap["reason"]

        # And it is counted separately from the absences.
        summary = asked["summary"]
        assert summary["unevaluated"] == 1
        assert summary["counts"].get("ABSENT", 0) == 0, (
            "a student nobody evaluated was counted as an absence")

        print("PASS  a student expected at a completed session and never "
              "evaluated is reported with no status, not as absent")
    finally:
        teardown(client, directory)


def test_a_corrected_record_reports_the_correction_and_the_original():
    client, directory = make_client()
    try:
        setup_school(client)
        completed("R-FIX", "MECH-A", ["R-M1"], day=0)
        record("R-FIX", "R-M1", "ABSENT")

        fixed = client.post(
            "/api/sessions/R-FIX/attendance/R-M1/override",
            json={"status": "PRESENT", "reason": "seen by the teacher"})
        assert fixed.status_code == 200, fixed.text

        row = rows_of(client.get("/api/reports/attendance?session_id=R-FIX"))[0]

        assert row["status"] == "PRESENT"
        assert row["is_override"] is True
        assert row["original_status"] == "ABSENT", row
        assert row["override_reason"] == "seen by the teacher"
        assert row["overridden_by"] == ADMIN["username"], row

        print("PASS  a corrected record reports the correction, what it was "
              "before, who made it and why")
    finally:
        teardown(client, directory)


# -- the CSV ----------------------------------------------------------------

def test_the_csv_is_the_same_rows_as_the_report():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        report = client.get("/api/reports/attendance").json()
        export = client.get("/api/reports/attendance.csv")
        assert export.status_code == 200, export.text

        rows = parse_csv(export.text)
        header, body = rows[0], rows[1:]

        assert header == [label for _, label in CSV_COLUMNS], header
        assert len(body) == len(report["items"]), (
            "the file has %d rows and the report has %d"
            % (len(body), len(report["items"])))

        # Same students, same sessions, same statuses - compared as sets so an
        # ordering difference is not mistaken for a content difference.
        index = {label: position
                 for position, label in enumerate(header)}

        from_file = {(line[index["Session"]], line[index["Student ID"]],
                      line[index["Status"]]) for line in body}
        from_json = {(row["session_id"], row["student_id"], row["status"])
                     for row in report["items"]}
        assert from_file == from_json, (
            "the exported file and the report disagree: %s"
            % (from_file ^ from_json))

        print("PASS  the CSV holds exactly the rows the report holds, with "
              "the same statuses")
    finally:
        teardown(client, directory)


def test_the_csv_carries_the_headers_a_download_needs():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        export = client.get("/api/reports/attendance.csv?class_id=MECH-A")

        assert export.headers["content-type"].startswith("text/csv")
        assert "charset=utf-8" in export.headers["content-type"], (
            "a spreadsheet will guess the encoding of non-ASCII names")

        disposition = export.headers["content-disposition"]
        assert disposition.startswith("attachment;"), disposition
        # Named after the filters, so three exports in a morning are
        # distinguishable in a Downloads folder.
        assert "MECH-A" in disposition, disposition
        assert disposition.endswith('.csv"'), disposition

        # Somebody's register should not sit in a shared machine's HTTP cache.
        assert "no-store" in export.headers["cache-control"]

        assert export.headers["x-report-rows"] == "4"
        assert export.headers["x-report-truncated"] == "0"

        print("PASS  the export is an attachment, named after its filters, "
              "UTF-8, and not cached")
    finally:
        teardown(client, directory)


def test_the_csv_writes_a_blank_rather_than_the_word_none():
    client, directory = make_client()
    try:
        setup_school(client)
        completed("R-GAP", "MECH-A", [sid for sid, _ in MINE], day=0)
        record("R-GAP", "R-M1", "PRESENT")

        export = client.get("/api/reports/attendance.csv"
                            "?include_unevaluated=true")
        rows = parse_csv(export.text)
        header, body = rows[0], rows[1:]
        index = {label: position for position, label in enumerate(header)}

        gap = [line for line in body
               if line[index["Student ID"]] == "R-M2"][0]

        assert gap[index["Status"]] == "", (
            "an undecided status was written as %r" % gap[index["Status"]])
        assert gap[index["Presence ratio"]] == ""
        assert "None" not in gap, (
            "the file contains the Python word None: %s" % gap)

        # Booleans are words a spreadsheet filter can read.
        present = [line for line in body
                   if line[index["Student ID"]] == "R-M1"][0]
        assert present[index["Manually corrected"]] == "no", present

        print("PASS  a missing value is an empty field, never the word None, "
              "and a boolean is yes/no")
    finally:
        teardown(client, directory)


def test_the_csv_uses_rfc_4180_line_endings():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        raw = client.get("/api/reports/attendance.csv").content

        assert raw.count(b"\r\n") >= 5, (
            "the export does not use CRLF, which is what RFC 4180 specifies "
            "and what a spreadsheet opens without a dialog")
        # No bare newline outside a CRLF pair.
        assert raw.replace(b"\r\n", b"").count(b"\n") == 0, (
            "the export mixes line endings")

        print("PASS  the export uses CRLF line endings throughout")
    finally:
        teardown(client, directory)


# -- who may export what -----------------------------------------------------

def test_a_teacher_exports_their_own_classes_and_nobody_elses():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        teacher = signed_in("tsmith", TEACHER_PASSWORD)

        report = teacher.get("/api/reports/attendance").json()
        assert report["total"] == 4, report["total"]
        assert {row["class_id"] for row in report["items"]} == {"MECH-A"}

        export = teacher.get("/api/reports/attendance.csv")
        assert export.status_code == 200

        # Checked against the BYTES, not the row count: the point of a scoped
        # export is that the other class's children are not in the file.
        for student_id, name in THEIRS:
            assert student_id not in export.text, (
                "%s is in a teacher's export" % student_id)
            assert name not in export.text, (
                "%s is in a teacher's export" % name)

        # And asking for the other class explicitly matches nothing rather
        # than answering with it.
        asked = teacher.get("/api/reports/attendance?class_id=CIVIL-B").json()
        assert asked["items"] == [], asked["items"]
        assert asked["total"] == 0

        teacher.close()
        print("PASS  a teacher's report and export contain their own classes, "
              "and naming another class returns nothing")
    finally:
        teardown(client, directory)


def test_a_student_and_a_stranger_reach_no_report():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        issued = client.post("/api/portal/accounts",
                             json={"student_id": "R-M1"})
        assert issued.status_code == 201, issued.text

        from backend.app import app
        student = TestClient(app)
        assert student.post("/api/portal/login", json={
            "username": "R-M1",
            "password": issued.json()["password"]}).status_code == 200

        stranger = TestClient(app)

        for caller, who in ((student, "a student"), (stranger, "a stranger")):
            for path in ("/api/reports/attendance",
                         "/api/reports/attendance.csv"):
                response = caller.get(path)
                assert response.status_code in (401, 403), (
                    "%s reached %s (%d)" % (who, path, response.status_code))

        student.close()
        stranger.close()
        print("PASS  neither a signed-in student nor an unauthenticated "
              "caller can run or export a report")
    finally:
        teardown(client, directory)


def test_a_bad_filter_is_refused_rather_than_ignored():
    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()

        # A status that is not a status, and a date that is not a date. Both
        # must be refused: quietly dropping a filter returns MORE rows than
        # were asked for, which is the dangerous direction for a report.
        for query in ("status=NOT_A_STATUS", "date_from=last-tuesday",
                      "date_to=2026-13-45", "date_from=2026-02-30",
                      "limit=0"):
            response = client.get("/api/reports/attendance?" + query)
            assert response.status_code == 422, (
                "%s was accepted (%d)" % (query, response.status_code))

        # 2026-13-45 is the one that matters, and it is worth saying why: it
        # matches the four-two-two pattern, session_date is compared as TEXT,
        # and every real date sorts BELOW it - so accepting it as date_to would
        # have returned the whole table for a filter that looks restrictive.
        # The export must refuse it too, not only the JSON report.
        assert client.get("/api/reports/attendance.csv"
                          "?date_to=2026-13-45").status_code == 422

        print("PASS  an unusable filter is refused rather than dropped, so a "
              "report never silently widens")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_reports_page_offers_every_filter_and_the_export():
    assert 'data-view="reports"' in HTML, "there is no Reports tab"
    assert 'id="view-reports"' in HTML

    for element in ("report-student", "report-class", "report-section",
                    "report-from", "report-to", "report-status",
                    "report-export", "report-run", "report-clear"):
        assert 'id="%s"' % element in HTML, "the page has no %s" % element

    assert "/api/reports/attendance.csv" in JS, "nothing exports a CSV"
    assert "Export CSV" in HTML

    print("PASS  the Reports page offers student, class, section and date "
          "filters and a CSV export")


def test_the_export_and_the_table_are_built_from_one_set_of_filters():
    """
    Both must call reportQuery(). An export built from its own reading of the
    form is how a file ends up holding rows the page never showed.
    """
    assert "function reportQuery" in JS

    table = JS[JS.index("async function loadReport"):]
    table = table[:table.index("function renderReportSummary")]
    assert "reportQuery()" in table, "the table does not use reportQuery"

    export = JS[JS.index("function exportReport"):]
    export = export[:export.index("\n}")]
    assert "reportQuery()" in export, "the export does not use reportQuery"

    print("PASS  the table and the CSV export are built from the same "
          "filters by construction")


def test_the_page_says_not_evaluated_rather_than_drawing_an_absence():
    table = JS[JS.index("async function loadReport"):
               JS.index("function renderReportSummary")]

    assert "not evaluated" in table, (
        "a row with no status is not labelled, so it reads as an absence")
    assert "truncated" in JS, (
        "a truncated report does not say so, and a silently short export is "
        "worse than a refused one")

    print("PASS  an undecided row is labelled in words and a truncated "
          "report says so")


def test_tests_never_touch_the_real_database():
    from database import DEFAULT_DATABASE_PATH

    real = Path(DEFAULT_DATABASE_PATH)
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        setup_school(client)
        two_classes()
        client.get("/api/reports/attendance.csv")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_an_unfiltered_report_holds_every_stored_decision_once,
        test_each_filter_narrows_the_same_rows_and_changes_no_status,
        test_the_summary_counts_exactly_the_rows_returned,
        test_a_re_evaluated_session_reports_one_decision_not_two,
        test_a_never_evaluated_student_is_not_reported_as_absent,
        test_a_corrected_record_reports_the_correction_and_the_original,
        test_the_csv_is_the_same_rows_as_the_report,
        test_the_csv_carries_the_headers_a_download_needs,
        test_the_csv_writes_a_blank_rather_than_the_word_none,
        test_the_csv_uses_rfc_4180_line_endings,
        test_a_teacher_exports_their_own_classes_and_nobody_elses,
        test_a_student_and_a_stranger_reach_no_report,
        test_a_bad_filter_is_refused_rather_than_ignored,
        test_the_reports_page_offers_every_filter_and_the_export,
        test_the_export_and_the_table_are_built_from_one_set_of_filters,
        test_the_page_says_not_evaluated_rather_than_drawing_an_absence,
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
