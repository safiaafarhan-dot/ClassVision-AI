"""
Academic sessions, academic history and promotion.

NO WEBCAM, NO MODEL AND NO NETWORK ARE NEEDED. Every test runs against its own
temporary SQLite file; data/classvision.db is never opened.

WHAT THIS VERIFIES
    That a student's identity is permanent while their class, section, roll
    number and semester are facts about a TERM; that promotion CREATES a new
    academic-history row and leaves the previous one exactly as it was; that
    nothing about an allocation is ever guessed; that a term is closed rather
    than deleted; and that attendance taken last term still describes last
    term after the student has moved.

    Nothing here mentions a real student. The fixtures are generated ids
    across two classes and three sections, so an implementation that only
    worked for one particular student would fail every test in this file.

WHAT THIS DOES NOT VERIFY
    Recognition, liveness or the camera. A promotion is a database fact and
    touches none of them.

Run with:
    python tests/test_academic_sessions.py
"""

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

from attendance_rules import (  # noqa: E402
    STATUS_PRESENT,
    AttendanceDecision,
)
from attendance_session import AttendanceSession  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import state  # noqa: E402
from backend.services.academic_service import (  # noqa: E402
    ACADEMIC_ACTIVE,
    ACADEMIC_CLOSED,
    ACADEMIC_PLANNED,
    AcademicService,
    session_identifier,
)
from paths import enable_safe_console  # noqa: E402

# Two classes, three sections, three different roll formats. Nothing in the
# implementation may depend on any one of them.
FIXTURES = [
    ("STU_TEST_001", "Aarav Menon", "R-11", "MECH", "A"),
    ("STU_TEST_002", "Brigid O'Hare", "0002", "MECH", "B"),
    ("STU_TEST_003", "Chen Wei", "c3", "CIVIL", "A"),
]

YEAR_ONE = "2026-27"
YEAR_TWO = "2027-28"


# -- fixtures ---------------------------------------------------------------

def make_client():
    """A private database and a client bound to it."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_academic_"))
    state.configure(directory / "academic.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def register_all(client, fixtures=None):
    for student_id, name, roll, class_name, section in (fixtures or FIXTURES):
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": roll,
            "class_name": class_name, "section": section})
        assert response.status_code in (200, 201), (
            "registering %s failed: %s" % (student_id, response.text))


def create_term(client, year, semester, status=ACADEMIC_PLANNED):
    response = client.post("/api/academic/sessions", json={
        "academic_year": year, "semester": semester, "status": status})
    assert response.status_code == 201, response.text
    return response.json()


def service():
    return AcademicService(state.get_database())


# -- identifiers ------------------------------------------------------------

def test_a_term_identifier_is_derived_not_counted():
    """
    The same year and semester must always produce the same id.

    That is what makes creating a term twice a conflict rather than a silent
    duplicate, and it is why the id is built from the values instead of from
    a sequence.
    """
    assert session_identifier("2026-27", "6") == "AY-2026-27-SEM-6"
    assert session_identifier("2026-27", "6") == session_identifier(
        " 2026-27 ", " 6 "), "whitespace changed the identifier"

    # Punctuation a school actually types, reduced to something usable.
    assert session_identifier("2026 / 27", "Sem 6") == "AY-2026-27-SEM-SEM6"
    assert session_identifier("2026-27", "") == "AY-2026-27", (
        "a year with no semester should not carry an empty SEM suffix")
    assert session_identifier("", "") == "AY-UNSET"

    print("PASS  the term id is derived from the year and semester, not a "
          "counter")


# -- academic sessions ------------------------------------------------------

def test_creating_a_term_touches_no_student():
    client, directory = make_client()
    try:
        register_all(client)
        before = client.get("/api/students").json()["items"]

        term = create_term(client, YEAR_ONE, "6")
        assert term["academic_session_id"] == "AY-2026-27-SEM-6"
        assert term["status"] == ACADEMIC_PLANNED, (
            "a new term should not become current by itself")

        after = client.get("/api/students").json()["items"]
        assert after == before, "creating a term changed a student row"

        history = state.get_database().query(
            "SELECT * FROM student_academic_history")
        assert len(history) == 0, (
            "creating a term enrolled somebody: %d row(s)" % len(history))

        print("PASS  creating a term adds the term and nothing else")
    finally:
        teardown(client, directory)


def test_a_term_needs_a_year_and_cannot_be_created_twice():
    client, directory = make_client()
    try:
        empty = client.post("/api/academic/sessions", json={"semester": "6"})
        assert empty.status_code == 400, empty.text
        assert "academic year" in empty.json()["detail"].lower()

        bad_status = client.post("/api/academic/sessions", json={
            "academic_year": YEAR_ONE, "status": "RUNNING"})
        assert bad_status.status_code == 400, bad_status.text

        create_term(client, YEAR_ONE, "6")
        again = client.post("/api/academic/sessions", json={
            "academic_year": YEAR_ONE, "semester": "6"})
        assert again.status_code == 409, again.text

        terms = client.get("/api/academic/sessions").json()["items"]
        assert len(terms) == 1, "the conflict created a second term"

        print("PASS  a term needs a year, and the same term cannot exist "
              "twice")
    finally:
        teardown(client, directory)


def test_activating_a_term_closes_the_previous_one_rather_than_deleting_it():
    client, directory = make_client()
    try:
        first = create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        second = create_term(client, YEAR_ONE, "7")

        listing = client.get("/api/academic/sessions").json()
        assert listing["current"]["academic_session_id"] == \
            first["academic_session_id"]

        activated = client.post("/api/academic/sessions/%s/activate"
                                % second["academic_session_id"])
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == ACADEMIC_ACTIVE

        listing = client.get("/api/academic/sessions").json()
        assert listing["current"]["academic_session_id"] == \
            second["academic_session_id"]

        by_id = {term["academic_session_id"]: term
                 for term in listing["items"]}
        assert len(by_id) == 2, "a term disappeared when another was activated"
        assert by_id[first["academic_session_id"]]["status"] == \
            ACADEMIC_CLOSED, "the previous term should be CLOSED, not removed"

        print("PASS  activating semester 7 closes semester 6 and keeps it")
    finally:
        teardown(client, directory)


def test_activating_a_term_that_does_not_exist_is_a_404():
    client, directory = make_client()
    try:
        response = client.post("/api/academic/sessions/AY-NOPE/activate")
        assert response.status_code == 404, response.text
        assert "database" not in response.json()["detail"].lower(), (
            "the refusal leaks implementation detail")

        print("PASS  activating an unknown term is a 404 with a plain reason")
    finally:
        teardown(client, directory)


def test_only_one_term_is_current_however_many_are_activated():
    client, directory = make_client()
    try:
        terms = [create_term(client, YEAR_ONE, "5", ACADEMIC_ACTIVE),
                 create_term(client, YEAR_ONE, "6"),
                 create_term(client, YEAR_TWO, "7")]

        for term in terms:
            client.post("/api/academic/sessions/%s/activate"
                        % term["academic_session_id"])

            active = [row for row in
                      client.get("/api/academic/sessions").json()["items"]
                      if row["status"] == ACADEMIC_ACTIVE]
            assert len(active) == 1, (
                "%d terms are active at once" % len(active))
            assert active[0]["academic_session_id"] == \
                term["academic_session_id"]

        print("PASS  exactly one term is current after each activation")
    finally:
        teardown(client, directory)


# -- academic history -------------------------------------------------------

def test_registering_a_student_and_enrolling_them_in_a_term():
    client, directory = make_client()
    try:
        term = create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)

        enrolled = client.post("/api/academic/students/STU_TEST_001/enrol",
                               json={"class_name": "MECH", "section": "A",
                                     "roll_number": "001"})
        assert enrolled.status_code == 200, enrolled.text
        record = enrolled.json()

        assert record["student_id"] == "STU_TEST_001"
        assert record["academic_session_id"] == term["academic_session_id"]
        assert record["academic_year"] == YEAR_ONE
        assert record["semester"] == "6"
        assert record["roll_number"] == "001"
        assert record["is_current"] in (1, True)

        history = client.get(
            "/api/academic/students/STU_TEST_001/history").json()
        assert len(history["items"]) == 1
        assert history["current"]["section"] == "A"

        # The registry mirrors the current term so every existing query keeps
        # working without learning about history.
        student = client.get("/api/students/STU_TEST_001").json()
        assert student["roll_number"] == "001"
        assert student["section"] == "A"

        print("PASS  an enrolment records the term and the registry mirrors "
              "it")
    finally:
        teardown(client, directory)


def test_enrolling_into_the_same_term_twice_updates_one_row():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)

        client.post("/api/academic/students/STU_TEST_001/enrol",
                    json={"class_name": "MECH", "section": "A",
                          "roll_number": "001"})
        corrected = client.post("/api/academic/students/STU_TEST_001/enrol",
                                json={"class_name": "MECH", "section": "B",
                                      "roll_number": "017"})
        assert corrected.status_code == 200, corrected.text

        history = client.get(
            "/api/academic/students/STU_TEST_001/history").json()["items"]
        assert len(history) == 1, (
            "a student is in a section once, not twice - found %d rows"
            % len(history))
        assert history[0]["section"] == "B"
        assert history[0]["roll_number"] == "017"

        print("PASS  a correction within one term updates that term's row")
    finally:
        teardown(client, directory)


def test_enrolling_a_student_who_does_not_exist_is_refused():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        response = client.post("/api/academic/students/NOBODY/enrol",
                               json={"section": "A"})
        assert response.status_code in (400, 404), response.text

        rows = state.get_database().query(
            "SELECT * FROM student_academic_history")
        assert rows == [], "an enrolment was written for a student who is not "\
                           "in the registry"

        print("PASS  there is no academic history without a student")
    finally:
        teardown(client, directory)


def test_the_history_of_a_student_nobody_wrote_into_the_code():
    client, directory = make_client()
    try:
        generated = [("GEN%04d" % index, "Generated Person %d" % index,
                      "G%d" % index, "CLASS%d" % (index % 3),
                      chr(65 + index % 3))
                     for index in range(1, 6)]
        register_all(client, generated)

        first = create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        for student_id, _, roll, class_name, section in generated:
            client.post("/api/academic/students/%s/enrol" % student_id,
                        json={"class_name": class_name, "section": section,
                              "roll_number": roll})

        second = create_term(client, YEAR_ONE, "7")
        promoted = client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"]})
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["count"] == len(generated)

        for student_id, _, _, _, _ in generated:
            history = client.get(
                "/api/academic/students/%s/history" % student_id
            ).json()["items"]
            assert len(history) == 2, (
                "%s has %d terms, expected 2" % (student_id, len(history)))
            terms = {row["academic_session_id"] for row in history}
            assert terms == {first["academic_session_id"],
                             second["academic_session_id"]}

        print("PASS  5 generated students each carry both terms - nothing is "
              "hard-coded")
    finally:
        teardown(client, directory)


# -- promotion --------------------------------------------------------------

def test_a_promotion_creates_a_term_and_leaves_the_previous_one_untouched():
    client, directory = make_client()
    try:
        first = create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        for student_id, _, roll, class_name, section in FIXTURES:
            client.post("/api/academic/students/%s/enrol" % student_id,
                        json={"class_name": class_name, "section": section,
                              "roll_number": roll})

        before = {row["academic_history_id"]: dict(row) for row in
                  state.get_database().query(
                      "SELECT * FROM student_academic_history")}

        second = create_term(client, YEAR_ONE, "7")
        result = client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"],
            "allocations": {"STU_TEST_001": {"section": "B",
                                             "roll_number": "021"}}}).json()

        assert result["count"] == 3, result
        assert sorted(result["promoted"]) == [
            "STU_TEST_001", "STU_TEST_002", "STU_TEST_003"]

        after = {row["academic_history_id"]: dict(row) for row in
                 state.get_database().query(
                     "SELECT * FROM student_academic_history")}

        # Every original row still exists, byte for byte, except that it is no
        # longer the current one.
        for history_id, original in before.items():
            assert history_id in after, (
                "promotion destroyed history row %s" % history_id)
            moved = after[history_id]
            for field in ("student_id", "academic_session_id", "academic_year",
                          "semester", "class_name", "section", "roll_number"):
                assert moved[field] == original[field], (
                    "promotion rewrote %s on the previous term" % field)
            assert not moved["is_current"], (
                "the previous term is still marked current")

        assert len(after) == len(before) + 3, (
            "promotion should add one row per student, found %d new"
            % (len(after) - len(before)))

        history = client.get(
            "/api/academic/students/STU_TEST_001/history").json()
        assert history["current"]["semester"] == "7"
        assert history["current"]["section"] == "B"
        assert history["current"]["roll_number"] == "021"

        old = [row for row in history["items"]
               if row["academic_session_id"] == first["academic_session_id"]][0]
        assert old["section"] == "A", "last term's section changed"
        assert old["roll_number"] == "R-11", "last term's roll changed"

        print("PASS  promotion adds 3 rows, rewrites none, and moves the "
              "current marker")
    finally:
        teardown(client, directory)


def test_a_promotion_never_guesses_an_allocation():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        for student_id, _, roll, class_name, section in FIXTURES:
            client.post("/api/academic/students/%s/enrol" % student_id,
                        json={"class_name": class_name, "section": section,
                              "roll_number": roll})

        second = create_term(client, YEAR_ONE, "7")
        preview = client.post("/api/academic/promotion/preview", json={
            "academic_session_id": second["academic_session_id"],
            "allocations": {"STU_TEST_002": {"section": "C"}}}).json()

        rows = {row["student_id"]: row for row in preview["students"]}

        # Nothing supplied: every field carried forward, none invented.
        assert rows["STU_TEST_001"]["carried_forward"] == [
            "class_name", "roll_number", "section"]
        assert rows["STU_TEST_001"]["to"] == rows["STU_TEST_001"]["from"]

        # Section supplied: the other two are carried forward and said so.
        assert rows["STU_TEST_002"]["carried_forward"] == [
            "class_name", "roll_number"]
        assert rows["STU_TEST_002"]["to"]["section"] == "C"
        assert rows["STU_TEST_002"]["to"]["roll_number"] == "0002", (
            "a roll number was invented for the new term")

        result = client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"],
            "allocations": {"STU_TEST_002": {"section": "C"}}}).json()

        assert result["carried_forward"]["STU_TEST_001"] == [
            "class_name", "roll_number", "section"]

        current = client.get(
            "/api/academic/students/STU_TEST_001/history").json()["current"]
        assert current["roll_number"] == "R-11", (
            "the roll number changed without anybody saying so")

        print("PASS  unsupplied allocation is carried forward and reported, "
              "never guessed")
    finally:
        teardown(client, directory)


def test_a_preview_changes_nothing():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        for student_id, _, roll, class_name, section in FIXTURES:
            client.post("/api/academic/students/%s/enrol" % student_id,
                        json={"class_name": class_name, "section": section,
                              "roll_number": roll})

        second = create_term(client, YEAR_ONE, "7")
        before = [dict(row) for row in state.get_database().query(
            "SELECT * FROM student_academic_history ORDER BY "
            "academic_history_id")]

        preview = client.post("/api/academic/promotion/preview", json={
            "academic_session_id": second["academic_session_id"]})
        assert preview.status_code == 200, preview.text
        assert preview.json()["total"] == 3

        after = [dict(row) for row in state.get_database().query(
            "SELECT * FROM student_academic_history ORDER BY "
            "academic_history_id")]
        assert after == before, "the preview wrote to the database"

        print("PASS  the preview reports 3 students and writes nothing")
    finally:
        teardown(client, directory)


def test_a_promotion_can_be_limited_to_named_students_or_a_class():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)

        second = create_term(client, YEAR_ONE, "7")

        named = client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"],
            "student_ids": ["STU_TEST_003"]}).json()
        assert named["promoted"] == ["STU_TEST_003"], named

        third = create_term(client, YEAR_TWO, "8")
        by_class = client.post("/api/academic/promotion", json={
            "academic_session_id": third["academic_session_id"],
            "class_name": "MECH"}).json()
        assert sorted(by_class["promoted"]) == [
            "STU_TEST_001", "STU_TEST_002"], by_class

        # And the class + section filter narrows further.
        fourth = create_term(client, YEAR_TWO, "9")
        by_section = client.post("/api/academic/promotion", json={
            "academic_session_id": fourth["academic_session_id"],
            "class_name": "MECH", "section": "B"}).json()
        assert by_section["promoted"] == ["STU_TEST_002"], by_section

        print("PASS  promotion honours an explicit list, a class, and a "
              "class + section")
    finally:
        teardown(client, directory)


def test_a_student_who_has_left_is_not_promoted():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)

        left = client.post("/api/students/STU_TEST_002/deactivate",
                           json={"status": "TRANSFERRED"})
        if left.status_code not in (200, 204):
            # The deactivate endpoint may not take a status; set it directly
            # through the registry, which is the same service layer.
            StudentRepository(state.get_database()).update(
                "STU_TEST_002", status="TRANSFERRED")

        second = create_term(client, YEAR_ONE, "7")
        result = client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"]}).json()

        assert "STU_TEST_002" not in result["promoted"], (
            "a student who has left was promoted into a new register")
        assert sorted(result["promoted"]) == [
            "STU_TEST_001", "STU_TEST_003"], result

        history = client.get(
            "/api/academic/students/STU_TEST_002/history").json()["items"]
        assert all(row["academic_session_id"] !=
                   second["academic_session_id"] for row in history)

        print("PASS  only ACTIVE students are promoted; a transfer keeps its "
              "history")
    finally:
        teardown(client, directory)


def test_promoting_into_a_term_that_does_not_exist_is_refused():
    client, directory = make_client()
    try:
        register_all(client)
        response = client.post("/api/academic/promotion", json={
            "academic_session_id": "AY-NOPE"})
        assert response.status_code == 400, response.text

        rows = state.get_database().query(
            "SELECT * FROM student_academic_history")
        assert rows == [], "a failed promotion still wrote history"

        print("PASS  an unknown term refuses the promotion and writes nothing")
    finally:
        teardown(client, directory)


def test_promoting_when_nobody_is_eligible_says_so():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        second = create_term(client, YEAR_ONE, "7")

        response = client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"]})
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "ACTIVE" in detail, (
            "the refusal should explain who is promoted: %r" % detail)

        print("PASS  an empty promotion is refused with the reason, not a "
              "silent no-op")
    finally:
        teardown(client, directory)


def test_promoting_twice_into_one_term_does_not_duplicate_a_row():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        second = create_term(client, YEAR_ONE, "7")

        client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"]})

        preview = client.post("/api/academic/promotion/preview", json={
            "academic_session_id": second["academic_session_id"]}).json()
        assert preview["already_enrolled"] == 3, (
            "the second run does not warn that everybody is already enrolled")

        client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"],
            "allocations": {"STU_TEST_001": {"section": "D"}}})

        rows = state.get_database().query(
            "SELECT * FROM student_academic_history WHERE student_id = ? "
            "AND academic_session_id = ?",
            ("STU_TEST_001", second["academic_session_id"]))
        assert len(rows) == 1, (
            "promoting twice created %d rows for one term" % len(rows))
        assert rows[0]["section"] == "D", (
            "the second run should correct the row it already owns")

        print("PASS  a repeated promotion corrects its own row instead of "
              "duplicating it")
    finally:
        teardown(client, directory)


# -- allocation import ------------------------------------------------------

def test_a_csv_allocation_is_read_column_by_column():
    client, directory = make_client()
    try:
        csv = ("student_id,class_name,section,roll_number\r\n"
               "STU_TEST_001,CSE(AI),C,001\r\n"
               "STU_TEST_002,CSE(AI),C,002\r\n")
        parsed = client.post("/api/academic/promotion/allocation",
                             json={"csv": csv})
        assert parsed.status_code == 200, parsed.text
        body = parsed.json()

        assert body["students"] == 2
        assert body["problems"] == []
        assert body["allocations"]["STU_TEST_001"] == {
            "class_name": "CSE(AI)", "section": "C", "roll_number": "001"}

        print("PASS  a spreadsheet export of 2 students parses into "
              "allocations")
    finally:
        teardown(client, directory)


def test_a_csv_missing_a_column_carries_that_value_forward():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        for student_id, _, roll, class_name, section in FIXTURES:
            client.post("/api/academic/students/%s/enrol" % student_id,
                        json={"class_name": class_name, "section": section,
                              "roll_number": roll})

        # Section only. Roll and class must be carried forward, not blanked.
        parsed = client.post("/api/academic/promotion/allocation", json={
            "csv": "student_id,section\nSTU_TEST_001,C\n"}).json()
        assert parsed["allocations"]["STU_TEST_001"] == {"section": "C"}

        second = create_term(client, YEAR_ONE, "7")
        client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"],
            "student_ids": ["STU_TEST_001"],
            "allocations": parsed["allocations"]})

        current = client.get(
            "/api/academic/students/STU_TEST_001/history").json()["current"]
        assert current["section"] == "C"
        assert current["class_name"] == "MECH", "the class was blanked"
        assert current["roll_number"] == "R-11", "the roll number was blanked"

        print("PASS  a column the office left out is carried forward, not "
              "blanked")
    finally:
        teardown(client, directory)


def test_an_unusable_allocation_file_is_refused_with_the_reason():
    client, directory = make_client()
    try:
        for csv, reason in (("", "empty"),
                            ("   ", "empty"),
                            ("name,section\nAarav,C\n", "student_id"),
                            ("student_id,section\n", "no students")):
            response = client.post("/api/academic/promotion/allocation",
                                   json={"csv": csv})
            assert response.status_code == 400, (
                "%r was accepted: %s" % (csv, response.text))
            detail = response.json()["detail"].lower()
            assert reason.split()[0] in detail, (
                "%r was refused with %r, which does not mention %r"
                % (csv, detail, reason))

        print("PASS  4 unusable allocation files refused, each with its "
              "reason")
    finally:
        teardown(client, directory)


def test_a_duplicate_or_blank_row_is_reported_not_silently_dropped():
    client, directory = make_client()
    try:
        csv = ("student_id,section\n"
               "STU_TEST_001,A\n"
               ",B\n"
               "STU_TEST_001,C\n"
               "STU_TEST_002,D\n")
        body = client.post("/api/academic/promotion/allocation",
                           json={"csv": csv}).json()

        assert body["students"] == 2
        assert len(body["problems"]) == 2, body["problems"]
        assert any("row 3" in problem for problem in body["problems"])
        assert any("row 4" in problem and "STU_TEST_001" in problem
                   for problem in body["problems"])
        assert body["allocations"]["STU_TEST_001"] == {"section": "A"}, (
            "the later duplicate silently overwrote the first row")

        print("PASS  a blank id and a repeated id are both reported by row "
              "number")
    finally:
        teardown(client, directory)


# -- attendance stays historical -------------------------------------------

def test_attendance_taken_last_term_still_describes_last_term():
    """
    The whole reason academic history exists.

    A student who moves from section A to section B must not make last term's
    register look like it happened in section B.
    """
    client, directory = make_client()
    try:
        first = create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        client.post("/api/academic/students/STU_TEST_001/enrol",
                    json={"class_name": "MECH", "section": "A",
                          "roll_number": "R-11"})

        database = state.get_database()
        sessions = SessionRepository(database)
        attendance = AttendanceRepository(database)

        start = datetime.now(timezone.utc).replace(microsecond=0) - \
            timedelta(days=30)
        lesson = AttendanceSession("SES-LAST-TERM", "MECH-A", start,
                                   start + timedelta(hours=1),
                                   policy_version="1.0",
                                   expected_students=["STU_TEST_001"])
        lesson.start()
        lesson.complete()
        sessions.save(lesson)

        attendance.save_decision(AttendanceDecision(
            student_id="STU_TEST_001", session_id="SES-LAST-TERM",
            status=STATUS_PRESENT,
            reason="present for the whole lesson", policy_version="1.0",
            session_seconds=3600.0, presence_ratio=1.0))

        # The whole register, exactly as it stood before anybody moved.
        before = dict(database.query_one(
            "SELECT * FROM attendance_records WHERE session_id = ?",
            ("SES-LAST-TERM",)))

        second = create_term(client, YEAR_ONE, "7")
        client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"],
            "student_ids": ["STU_TEST_001"],
            "allocations": {"STU_TEST_001": {"section": "B",
                                             "roll_number": "021"}}})

        # The registry now says section B.
        assert client.get("/api/students/STU_TEST_001").json()["section"] == "B"

        # The old register is untouched: every column of it, the session's
        # class id, and the history row for that term still saying section A.
        record = dict(database.query_one(
            "SELECT * FROM attendance_records WHERE session_id = ?",
            ("SES-LAST-TERM",)))
        assert record["status"] == STATUS_PRESENT, (
            "a promotion changed a historical attendance status")
        assert record == before, (
            "a promotion rewrote a historical attendance row: %s"
            % {key: (before[key], record[key]) for key in before
               if before[key] != record[key]})

        session_row = database.query_one(
            "SELECT class_id FROM attendance_sessions WHERE session_id = ?",
            ("SES-LAST-TERM",))
        assert session_row["class_id"] == "MECH-A", (
            "a promotion rewrote a historical session's class")

        old_term = [row for row in client.get(
            "/api/academic/students/STU_TEST_001/history").json()["items"]
            if row["academic_session_id"] == first["academic_session_id"]][0]
        assert old_term["section"] == "A", (
            "last term's section was rewritten to this term's")

        print("PASS  last term's register still says section A after a move "
              "to B")
    finally:
        teardown(client, directory)


def test_a_promotion_is_audited_without_naming_anything_sensitive():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        second = create_term(client, YEAR_ONE, "7")
        client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"]})

        rows = [dict(row) for row in state.get_database().query(
            "SELECT * FROM audit_log ORDER BY event_id")]
        actions = [row["action"] for row in rows]

        assert "academic.session_create" in actions
        assert "academic.promote" in actions

        promote = [row for row in rows
                   if row["action"] == "academic.promote"][0]
        assert promote["subject"] == second["academic_session_id"]
        assert "3 student(s)" in (promote["detail"] or "")

        blob = " ".join(str(value) for row in rows for value in row.values())
        for forbidden in ("embedding", "vector", "password", "hash", ".npz",
                          "token", "secret"):
            assert forbidden not in blob.lower(), (
                "the audit log carries %r" % forbidden)

        print("PASS  %d audit rows record the term and the promotion, and "
              "leak nothing" % len(rows))
    finally:
        teardown(client, directory)


def test_no_academic_response_carries_a_face_or_a_path():
    client, directory = make_client()
    try:
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
        for student_id, _, roll, class_name, section in FIXTURES:
            client.post("/api/academic/students/%s/enrol" % student_id,
                        json={"class_name": class_name, "section": section,
                              "roll_number": roll})
        second = create_term(client, YEAR_ONE, "7")
        client.post("/api/academic/promotion", json={
            "academic_session_id": second["academic_session_id"]})

        bodies = [
            client.get("/api/academic/sessions").text,
            client.get("/api/academic/sessions/%s/students"
                       % second["academic_session_id"]).text,
            client.get("/api/academic/students/STU_TEST_001/history").text,
            client.post("/api/academic/promotion/preview", json={
                "academic_session_id": second["academic_session_id"]}).text,
        ]

        forbidden = ("embedding", "vector", "descriptor", "image", "photo",
                     "crop", "thumbnail", ".npz", ".onnx", "password", "hash",
                     "token", "secret", "db_path", "file_path", "similarity")

        for body in bodies:
            lowered = body.lower()
            for word in forbidden:
                assert word not in lowered, (
                    "an academic response carries %r" % word)

        print("PASS  4 academic responses checked against %d forbidden "
              "patterns - nothing leaked" % len(forbidden))
    finally:
        teardown(client, directory)


def test_academic_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        assert Path(state.get_database().path).parent != real.parent, (
            "the suite is configured against the real data directory")
        create_term(client, YEAR_ONE, "6", ACADEMIC_ACTIVE)
        register_all(client)
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_a_term_identifier_is_derived_not_counted,
    test_creating_a_term_touches_no_student,
    test_a_term_needs_a_year_and_cannot_be_created_twice,
    test_activating_a_term_closes_the_previous_one_rather_than_deleting_it,
    test_activating_a_term_that_does_not_exist_is_a_404,
    test_only_one_term_is_current_however_many_are_activated,
    test_registering_a_student_and_enrolling_them_in_a_term,
    test_enrolling_into_the_same_term_twice_updates_one_row,
    test_enrolling_a_student_who_does_not_exist_is_refused,
    test_the_history_of_a_student_nobody_wrote_into_the_code,
    test_a_promotion_creates_a_term_and_leaves_the_previous_one_untouched,
    test_a_promotion_never_guesses_an_allocation,
    test_a_preview_changes_nothing,
    test_a_promotion_can_be_limited_to_named_students_or_a_class,
    test_a_student_who_has_left_is_not_promoted,
    test_promoting_into_a_term_that_does_not_exist_is_refused,
    test_promoting_when_nobody_is_eligible_says_so,
    test_promoting_twice_into_one_term_does_not_duplicate_a_row,
    test_a_csv_allocation_is_read_column_by_column,
    test_a_csv_missing_a_column_carries_that_value_forward,
    test_an_unusable_allocation_file_is_refused_with_the_reason,
    test_a_duplicate_or_blank_row_is_reported_not_silently_dropped,
    test_attendance_taken_last_term_still_describes_last_term,
    test_a_promotion_is_audited_without_naming_anything_sensitive,
    test_no_academic_response_carries_a_face_or_a_path,
    test_academic_tests_never_touch_the_real_database,
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
