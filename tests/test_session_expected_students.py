"""
Scheduling a session by class and section, and who it decides to expect.

NO WEBCAM AND NO NETWORK ARE NEEDED. Every test uses its own temporary SQLite
file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That naming a class expects its ACTIVE students and nobody else. An
    inactive, transferred or graduated student is not expected at Tuesday's
    lesson, and counting one would manufacture an absence for somebody who is
    not in the building - which then drags the class's rate down and surfaces
    in a report as a child with a problem.

    That a class with no active students is REFUSED rather than producing a
    session nobody is expected at. Absence is only meaningful against a list of
    who was expected, so such a session could never produce attendance at all;
    it would be a lesson that silently recorded nothing.

    That the refusal says WHICH problem it is. "Nobody is registered in MECH-A"
    and "seven people are registered in MECH-A and none of them is active" need
    different fixing, and one message for both sends somebody to look in the
    wrong place.

    And that the existing explicit-list path is untouched. Every caller that
    passes expected_students - the CLI, the other suites, the dashboard's own
    roster checkboxes - behaves exactly as it did.

WHAT THIS DOES NOT VERIFY
    Any attendance rule. Scheduling records an intention and calculates
    nothing; the rule engine is unchanged and is not exercised here.

Run with:
    python tests/test_session_expected_students.py
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
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

ADMIN = {"username": "timetabler", "display_name": "The Timetabler",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

# MECH-A has three active students and one who has left. MECH-B is a different
# section of the same class. CIVIL-B belongs to nobody in these tests.
ROSTER = (
    ("E-A1", "Asha Rao", "MECH", "A", "ACTIVE"),
    ("E-A2", "Vikram Nair", "MECH", "A", "ACTIVE"),
    ("E-A3", "Rhea Menon", "MECH", "A", "ACTIVE"),
    ("E-A4", "Left Already", "MECH", "A", "INACTIVE"),
    ("E-B1", "Kabir Shah", "MECH", "B", "ACTIVE"),
    ("E-C1", "Leena Bose", "CIVIL", "B", "ACTIVE"),
)

START = "2026-04-06T09:00:00+00:00"
END = "2026-04-06T10:00:00+00:00"


def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_expect_"))
    state.configure(directory / "expected.db")
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

    for student_id, name, class_name, section, status in ROSTER:
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": "1",
            "class_name": class_name, "section": section})
        assert response.status_code == 201, response.text

        if status != "ACTIVE":
            # Through the endpoint a school would actually use, so this is the
            # same "inactive" the rest of the system means.
            off = client.post("/api/students/%s/status?status=INACTIVE"
                              % student_id)
            assert off.status_code == 200, off.text

    return created.json()


def schedule(client, **body):
    payload = {"class_id": "MECH-A", "start_time": START, "end_time": END}
    payload.update(body)
    return client.post("/api/sessions", json=payload)


def expected_of(client, session_id):
    """The stored expected list for a session, read back from the database."""
    rows = state.get_database().query(
        "SELECT student_id FROM session_expected_students "
        "WHERE session_id = ? ORDER BY student_id", (session_id,))
    return [row["student_id"] for row in rows]


# -- the preview -------------------------------------------------------------

def test_the_preview_counts_active_students_and_reports_the_rest_apart():
    client, directory = make_client()
    try:
        setup_school(client)

        roster = client.get("/api/sessions/expected-students"
                            "?class_name=MECH&section=A")
        assert roster.status_code == 200, roster.text
        body = roster.json()

        assert body["expected_count"] == 3, body
        assert body["expected_students"] == ["E-A1", "E-A2", "E-A3"], body

        # The one who left is reported, separately, and is NOT expected.
        assert body["inactive_count"] == 1, body
        assert [row["student_id"] for row in body["inactive"]] == ["E-A4"]

        print("PASS  the preview counts the active students and reports the "
              "inactive ones separately rather than folding them in")
    finally:
        teardown(client, directory)


def test_an_empty_section_covers_every_section_of_the_class():
    client, directory = make_client()
    try:
        setup_school(client)

        whole = client.get(
            "/api/sessions/expected-students?class_name=MECH").json()
        assert whole["expected_students"] == ["E-A1", "E-A2", "E-A3", "E-B1"], \
            whole["expected_students"]

        just_b = client.get("/api/sessions/expected-students"
                            "?class_name=MECH&section=B").json()
        assert just_b["expected_students"] == ["E-B1"], just_b

        print("PASS  no section means every section of the class, and a named "
              "section means only that one")
    finally:
        teardown(client, directory)


def test_the_class_name_is_matched_without_regard_to_case():
    client, directory = make_client()
    try:
        setup_school(client)

        for query in ("class_name=MECH&section=A", "class_name=mech&section=a",
                      "class_name=Mech&section=A"):
            body = client.get(
                "/api/sessions/expected-students?" + query).json()
            assert body["expected_count"] == 3, (
                "%s found %d students" % (query, body["expected_count"]))

        print("PASS  a class typed in any case finds the same students")
    finally:
        teardown(client, directory)


def test_a_class_nobody_is_in_previews_as_empty_rather_than_failing():
    client, directory = make_client()
    try:
        setup_school(client)

        body = client.get(
            "/api/sessions/expected-students?class_name=NOSUCHCLASS")
        assert body.status_code == 200, body.text
        assert body.json()["expected_count"] == 0
        assert body.json()["inactive_count"] == 0

        print("PASS  a class nobody is registered in previews as empty rather "
              "than as an error")
    finally:
        teardown(client, directory)


def test_the_preview_is_narrowed_to_the_callers_own_students():
    client, directory = make_client()
    try:
        setup_school(client)

        teacher = signed_in("tsmith", TEACHER_PASSWORD)

        mine = teacher.get("/api/sessions/expected-students"
                           "?class_name=MECH&section=A").json()
        assert mine["expected_count"] == 3, mine

        # The teacher is assigned to MECH-A only. Asking about another class
        # through the scheduling form must not enumerate it.
        other = teacher.get("/api/sessions/expected-students"
                            "?class_name=CIVIL&section=B").json()
        assert other["expected_count"] == 0, other
        assert other["expected_students"] == []
        assert other["students"] == []

        teacher.close()
        print("PASS  a teacher cannot enumerate another class's register "
              "through the scheduling form")
    finally:
        teardown(client, directory)


def test_the_preview_needs_a_signed_in_caller():
    client, directory = make_client()
    try:
        setup_school(client)
        from backend.app import app

        stranger = TestClient(app)
        assert stranger.get("/api/sessions/expected-students"
                            "?class_name=MECH").status_code == 401
        stranger.close()

        print("PASS  the expected-students preview refuses a caller with no "
              "session")
    finally:
        teardown(client, directory)


# -- creating a session from a class -----------------------------------------

def test_naming_a_class_expects_its_active_students():
    client, directory = make_client()
    try:
        setup_school(client)

        created = schedule(client, class_name="MECH", section="A")
        assert created.status_code == 201, created.text
        session = created.json()

        # On the session DETAIL this is the LIST of ids; the session LIST
        # endpoint is the one that reports a count. Both are asserted, because
        # the dashboard reads each of them and read one as the other once.
        assert session["expected_students"] == ["E-A1", "E-A2", "E-A3"], session

        listed = client.get("/api/sessions").json()["items"][0]
        assert listed["expected_students"] == 3, listed

        stored = expected_of(client, session["session_id"])
        assert stored == ["E-A1", "E-A2", "E-A3"], stored

        # The whole point: the student who left is not in the list, so no
        # absence will ever be produced for them.
        assert "E-A4" not in stored, (
            "an inactive student was expected at a lesson")

        print("PASS  scheduling by class expects its active students and "
              "leaves out the one who has left")
    finally:
        teardown(client, directory)


def test_the_section_is_recorded_on_the_session():
    client, directory = make_client()
    try:
        setup_school(client)

        created = schedule(client, class_name="MECH", section="A")
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]

        row = state.get_database().query_one(
            "SELECT section FROM attendance_sessions WHERE session_id = ?",
            (session_id,))
        assert row["section"] == "A", row["section"]

        # And it comes back on the session detail a teacher reads.
        details = client.get("/api/sessions/%s/details" % session_id)
        assert details.status_code == 200, details.text
        assert details.json()["section"] == "A", details.json()

        print("PASS  the section a session was scheduled for is recorded on "
              "the session row and read back")
    finally:
        teardown(client, directory)


def test_no_section_expects_every_section_of_the_class():
    client, directory = make_client()
    try:
        setup_school(client)

        created = schedule(client, class_id="MECH-ALL", class_name="MECH")
        assert created.status_code == 201, created.text

        stored = expected_of(client, created.json()["session_id"])
        assert stored == ["E-A1", "E-A2", "E-A3", "E-B1"], stored

        print("PASS  scheduling a class with no section expects every section "
              "of it")
    finally:
        teardown(client, directory)


# -- what it refuses to create ----------------------------------------------

def test_a_class_with_only_inactive_students_is_refused_and_says_so():
    client, directory = make_client()
    try:
        setup_school(client)

        # Deactivate the whole of MECH-B, so the class exists and nobody in it
        # is active.
        assert client.post(
            "/api/students/E-B1/status?status=INACTIVE").status_code == 200

        refused = schedule(client, class_id="MECH-B", class_name="MECH",
                           section="B")
        assert refused.status_code == 400, refused.text

        detail = refused.json()["detail"]
        assert "MECH-B" in detail, detail
        assert "inactive" in detail.lower(), (
            "the refusal does not say that the students there are inactive, "
            "so a teacher cannot tell which problem they have: %r" % detail)
        assert "1 inactive" in detail, detail

        # And nothing was created.
        assert client.get("/api/sessions").json()["total"] == 0

        print("PASS  a class whose students are all inactive is refused, and "
              "the message says that is why")
    finally:
        teardown(client, directory)


def test_a_class_nobody_is_registered_in_is_refused_differently():
    client, directory = make_client()
    try:
        setup_school(client)

        refused = schedule(client, class_id="GHOST-1",
                           class_name="NOSUCHCLASS")
        assert refused.status_code == 400, refused.text

        detail = refused.json()["detail"]
        assert "NOSUCHCLASS" in detail, detail
        assert "No students are registered" in detail, detail
        # And it does NOT claim there are inactive students there.
        assert "inactive" not in detail.lower(), (
            "an empty class was reported as a class of inactive students: %r"
            % detail)

        assert client.get("/api/sessions").json()["total"] == 0

        print("PASS  a class nobody is registered in is refused with a "
              "different message from one whose students are all inactive")
    finally:
        teardown(client, directory)


def test_naming_neither_a_class_nor_a_list_is_refused():
    client, directory = make_client()
    try:
        setup_school(client)

        refused = schedule(client)
        assert refused.status_code == 400, refused.text
        detail = refused.json()["detail"]
        assert "expected student" in detail, detail

        # An explicitly empty list is the same refusal, not a session with
        # nobody in it.
        empty = schedule(client, expected_students=[])
        assert empty.status_code == 400, empty.text

        assert client.get("/api/sessions").json()["total"] == 0

        print("PASS  a session with neither a class nor a student list is "
              "refused, and so is an explicitly empty list")
    finally:
        teardown(client, directory)


def test_no_session_can_be_created_with_nobody_expected():
    """
    The invariant behind all of the above, asserted directly.

    Whatever route a session arrives by, it ends up with at least one expected
    student - because absence is only meaningful against that list, and a
    session with an empty one would run, finalise, and record nothing while
    looking like it worked.
    """
    client, directory = make_client()
    try:
        setup_school(client)

        attempts = [
            {},
            {"expected_students": []},
            {"expected_students": [], "class_name": "NOSUCHCLASS"},
            {"class_name": "NOSUCHCLASS"},
            {"class_name": "MECH", "section": "ZZ"},
        ]
        for index, body in enumerate(attempts):
            response = schedule(client, class_id="TRY-%d" % index, **body)
            assert response.status_code == 400, (
                "%s created a session (%d)" % (body, response.status_code))

        # And the one legitimate route does create one, with people in it.
        good = schedule(client, class_id="REAL-1", class_name="MECH",
                        section="A")
        assert good.status_code == 201, good.text

        empty_sessions = state.get_database().query(
            """
            SELECT s.session_id FROM attendance_sessions s
             WHERE NOT EXISTS (SELECT 1 FROM session_expected_students e
                                WHERE e.session_id = s.session_id)
            """)
        assert not empty_sessions, (
            "sessions exist with nobody expected: %s"
            % [row["session_id"] for row in empty_sessions])

        print("PASS  no route creates a session with nobody expected, and the "
              "database holds none")
    finally:
        teardown(client, directory)


# -- the existing path is untouched -----------------------------------------

def test_an_explicit_list_is_used_exactly_as_given():
    client, directory = make_client()
    try:
        setup_school(client)

        # Including an INACTIVE student, on purpose. A teacher who names
        # somebody explicitly means it - this is the manual override, and the
        # automatic path is what filters by status.
        created = schedule(client, expected_students=["E-A1", "E-A4"])
        assert created.status_code == 201, created.text

        stored = expected_of(client, created.json()["session_id"])
        assert stored == ["E-A1", "E-A4"], stored

        print("PASS  an explicit list is stored exactly as given, inactive "
              "students included, because naming somebody is deliberate")
    finally:
        teardown(client, directory)


def test_an_explicit_list_wins_over_a_named_class():
    client, directory = make_client()
    try:
        setup_school(client)

        created = schedule(client, expected_students=["E-A1"],
                           class_name="MECH", section="A")
        assert created.status_code == 201, created.text

        stored = expected_of(client, created.json()["session_id"])
        assert stored == ["E-A1"], (
            "the named class overrode the list the teacher chose: %s" % stored)

        print("PASS  a list the teacher adjusted by hand is not overwritten "
              "by the class they picked it from")
    finally:
        teardown(client, directory)


def test_an_unknown_student_id_is_still_named_in_the_refusal():
    client, directory = make_client()
    try:
        setup_school(client)

        refused = schedule(client, expected_students=["E-A1", "TYPO-9"])
        assert refused.status_code == 400, refused.text
        assert "TYPO-9" in refused.json()["detail"], refused.json()

        print("PASS  a mistyped student id is still named in the refusal "
              "rather than becoming an integrity error")
    finally:
        teardown(client, directory)


# -- the form ---------------------------------------------------------------

def test_the_form_offers_a_class_and_a_section():
    assert 'id="new-session-class-name"' in HTML, (
        "the scheduling form has no registered-class field")
    assert 'id="new-session-section"' in HTML, (
        "the scheduling form has no section field")
    assert 'id="new-session-expected"' in HTML, (
        "the expected count has nowhere to appear")
    assert 'id="registered-classes"' in HTML

    assert "/api/sessions/expected-students" in JS, (
        "the form never asks the server who would be expected")

    print("PASS  the scheduling form offers a registered class and section "
          "and shows the expected count")


def test_the_form_previews_the_servers_answer_rather_than_computing_its_own():
    preview = JS[JS.index("async function previewExpectedStudents"):]
    preview = preview[:preview.index("\nasync function loadKnownClasses")]

    assert "/api/sessions/expected-students" in preview, (
        "the preview counts students in the browser instead of asking the "
        "server, so the two can disagree")
    assert "expected_count" in preview
    assert "inactive_count" in preview, (
        "the preview does not distinguish an empty class from one whose "
        "students are all inactive")

    # And the state is a sentence, not only a colour - rule 1 of the
    # stylesheet.
    assert "No active students in" in preview
    assert ".roster-summary" in CSS
    assert ".roster-summary.warn" in CSS

    print("PASS  the form previews the server's own answer and states the "
          "problem in words, not in colour alone")


def test_every_element_the_form_uses_exists_in_the_page():
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
        schedule(client, class_name="MECH", section="A")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real attendance database was written to"

    print("PASS  the real database was never touched")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_the_preview_counts_active_students_and_reports_the_rest_apart,
        test_an_empty_section_covers_every_section_of_the_class,
        test_the_class_name_is_matched_without_regard_to_case,
        test_a_class_nobody_is_in_previews_as_empty_rather_than_failing,
        test_the_preview_is_narrowed_to_the_callers_own_students,
        test_the_preview_needs_a_signed_in_caller,
        test_naming_a_class_expects_its_active_students,
        test_the_section_is_recorded_on_the_session,
        test_no_section_expects_every_section_of_the_class,
        test_a_class_with_only_inactive_students_is_refused_and_says_so,
        test_a_class_nobody_is_registered_in_is_refused_differently,
        test_naming_neither_a_class_nor_a_list_is_refused,
        test_no_session_can_be_created_with_nobody_expected,
        test_an_explicit_list_is_used_exactly_as_given,
        test_an_explicit_list_wins_over_a_named_class,
        test_an_unknown_student_id_is_still_named_in_the_refusal,
        test_the_form_offers_a_class_and_a_section,
        test_the_form_previews_the_servers_answer_rather_than_computing_its_own,
        test_every_element_the_form_uses_exists_in_the_page,
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
