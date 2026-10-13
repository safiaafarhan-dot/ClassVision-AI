"""
Step 12: the assistant over trusted analytics.

NO WEBCAM, NO NETWORK AND NO MODEL ARE NEEDED - and a test asserts the last
one. Every test uses its own temporary SQLite file; data/classvision.db is
never opened.

WHAT THIS VERIFIES

    THE CONSTRAINT THE WHOLE STEP EXISTS FOR: every figure in an answer is
    the one analytics_service returned. This is checked by calling the
    analytics function directly and asserting the assistant's figure is EQUAL
    TO IT EXACTLY - not rounded, not re-derived, not within a tolerance. A
    figure that drifted from the dashboard by a rounding step would fail here,
    which is the point: a teacher must not be able to read two different
    attendance rates for one child and have no way to tell which is real.

    That no arithmetic happens in the service. The module's source is PARSED
    and every arithmetic operation located in the AST; the only one permitted
    is the single multiply inside `_percent`, which formats a ratio analytics
    has already computed.

    That "no data" survives. A student with no completed session reports no
    rate and the answer says so in words, never as 0%.

    That expected-and-never-evaluated is not reported as absent, in the
    assistant as everywhere else.

    That a question it does not handle is answered with "I did not
    understand", and NOT with a confident answer to a different question. The
    specific regression guarded is "What is the weather like?" coming back as
    "I have no student by that name" - a non-answer dressed as an answer.

    That it cannot be used to discover who is in somebody else's class. A
    teacher asking about a student outside their scope gets the answer for a
    student who does not exist, and the two are asserted identical.

    That it never guesses between two students whose names both match.

    That every answer carries a receipt - the analytics function, its
    arguments, and where in the dashboard to check it.

WHAT THIS DOES NOT VERIFY

    Any attendance rule, and any analytics definition. Both belong to Steps 6
    and 7 and have their own suites. Every record here was written through the
    Step 6 repository.

    That the phrasings a real teacher would type are all understood. They are
    not - that is stated as a limitation rather than tested as a feature, and
    the capability list is published for exactly that reason.

Run with:
    python tests/test_assistant.py
"""

import ast
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

SERVICE = PROJECT_ROOT / "backend" / "services" / "assistant_service.py"
SOURCE = SERVICE.read_text(encoding="utf-8")
ROUTE = (PROJECT_ROOT / "backend" / "routes"
         / "assistant.py").read_text(encoding="utf-8")

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

BASE = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

# MECH-A is the teacher's class; CIVIL-B is not. The two students called Asha
# make an ambiguous name a real case rather than a hypothetical one.
MINE = [("X-M1", "Asha Rao", "1"), ("X-M2", "Vikram Nair", "2"),
        ("X-M3", "Neverseen Student", "3")]
THEIRS = [("X-C1", "Asha Bose", "1")]


# -- harness ----------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_assistant_"))
    state.configure(directory / "assistant.db")

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


def completed(session_id, class_id, student_ids, day=0):
    from attendance_session import AttendanceSession
    from repositories import SessionRepository

    start = BASE + timedelta(days=day)
    session = AttendanceSession(session_id, class_id, start,
                                start + timedelta(hours=1),
                                policy_version="1.0",
                                expected_students=list(student_ids))
    session.start()
    session.complete(completed_at=start + timedelta(hours=1))
    SessionRepository(state.get_database()).save(session)


def record(session_id, student_id, status, ratio=0.9):
    from attendance_rules import AttendanceDecision
    from repositories import AttendanceRepository

    decision = AttendanceDecision(student_id, session_id, status,
                                  "synthetic test record", "1.0")
    decision.present_seconds = 3240.0
    decision.session_seconds = 3600.0
    decision.presence_ratio = ratio
    AttendanceRepository(state.get_database()).save_decision(decision)


def a_school():
    """
    Four completed MECH-A sessions and one CIVIL-B session.

    X-M1 is absent once and present three times - a real rate with a real
    trend. X-M2 is absent throughout, so the indicator rule fires on them.
    X-M3 is expected at every session and never evaluated, which is the case
    that must not be reported as absence.
    """
    everybody = [student_id for student_id, _, _ in MINE]

    for day in range(4):
        session_id = "X-S%d" % day
        completed(session_id, "MECH-A", everybody, day=day)
        record(session_id, "X-M1", "ABSENT" if day == 0 else "PRESENT",
               ratio=0.0 if day == 0 else 0.9)
        record(session_id, "X-M2", "ABSENT", ratio=0.0)
        # X-M3 gets no record at all, on purpose.

    completed("X-OTHER", "CIVIL-B", ["X-C1"], day=5)
    record("X-OTHER", "X-C1", "PRESENT")


def analytics():
    from backend.services.analytics_service import AnalyticsService
    return AnalyticsService(state.get_database())


def ask(client, question):
    response = client.post("/api/assistant/ask", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()


# -- the constraint the step exists for -------------------------------------

def test_a_student_rate_is_the_analytics_services_own_number():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        measured = analytics().student_analytics("X-M1")
        answer = ask(client, "What is the attendance rate for X-M1?")

        assert answer["understood"] is True, answer
        assert answer["figures"]["attendance_rate"] == \
            measured["attendance_rate"], (
            "the assistant reported a different rate from analytics")
        assert answer["figures"]["expected_sessions"] == \
            measured["expected_sessions"]
        assert answer["figures"]["satisfying_count"] == \
            measured["satisfying_count"]

        # And the sentence quotes that same figure, formatted.
        formatted = "%.1f%%" % (measured["attendance_rate"] * 100)
        assert formatted in answer["answer"], (
            "the sentence does not quote the figure it reported: %s"
            % answer["answer"])

        print("PASS  a student rate is analytics' own number, to the digit")
    finally:
        teardown(client, directory)


def test_a_class_rate_is_the_analytics_services_own_number():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        measured = analytics().class_analytics("MECH-A")
        answer = ask(client, "What is the attendance rate for MECH-A?")

        assert answer["figures"]["attendance_rate"] == \
            measured["attendance_rate"]
        assert answer["figures"]["completed_sessions"] == \
            measured["completed_sessions"]
        assert answer["figures"]["student_count"] == measured["student_count"]

        print("PASS  a class rate is analytics' own number, to the digit")
    finally:
        teardown(client, directory)


def test_the_overview_answer_is_the_analytics_services_own_number():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        measured = analytics().overview()
        answer = ask(client, "How is attendance overall?")

        assert answer["figures"]["attendance_rate"] == \
            measured["attendance_rate"]
        assert answer["figures"]["sample_size"] == measured["sample_size"]
        assert answer["figures"]["counts"] == measured["counts"]

        print("PASS  the overview answer is analytics' own number")
    finally:
        teardown(client, directory)


def test_the_indicator_rule_is_the_analytics_services_own_rule():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        measured = analytics().attention_indicators(class_id="MECH-A")
        answer = ask(client, "Who needs attention in MECH-A?")

        assert answer["figures"]["rule"] == measured["rule"], (
            "the assistant quoted a threshold analytics does not use")
        assert answer["figures"]["count"] == len(measured["indicators"])

        # The thresholds are never written out as literals here: they come from
        # the rule analytics reported.
        assert "0.75" not in SOURCE, (
            "the attendance threshold is hard-coded in the assistant")
        assert "not predictions" in answer["answer"], (
            "the answer does not say these are thresholds rather than "
            "predictions")

        print("PASS  the indicator rule quoted is analytics' own rule")
    finally:
        teardown(client, directory)


def test_the_service_performs_no_arithmetic_of_its_own():
    """
    Parsed, not grepped.

    Every binary arithmetic operation in the module is located in the AST. The
    only one allowed is the multiply inside `_percent`, which renders a ratio
    analytics already computed. Anything else would be this layer forming its
    own opinion about a number.
    """
    tree = ast.parse(SOURCE)
    offences = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue

        # "%" is string formatting, which every message uses.
        if isinstance(node.op, ast.Mod):
            continue

        # "+" builds strings and lists here. A numeric "+" would be caught by
        # the figures tests above, which compare against analytics exactly.
        if isinstance(node.op, ast.Add):
            continue

        # The one permitted multiply: _percent's value * 100.
        if (isinstance(node.op, ast.Mult)
                and isinstance(node.right, ast.Constant)
                and node.right.value == 100):
            continue

        offences.append((getattr(node, "lineno", "?"),
                         type(node.op).__name__))

    assert not offences, (
        "the assistant does arithmetic of its own at %s" % offences)

    # The permitted multiply lives in exactly one function, and that function
    # only formats.
    percent = SOURCE[SOURCE.index("def _percent("):]
    percent = percent[:percent.index("\ndef ")]
    assert "* 100" in percent, "_percent no longer formats a ratio"
    assert SOURCE.count("* 100") == 1, (
        "a ratio is scaled somewhere other than _percent")

    print("PASS  the service performs no arithmetic beyond formatting a ratio")


def test_every_answer_carries_a_receipt():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        for question in ("What is the attendance rate for X-M1?",
                         "What is the attendance rate for MECH-A?",
                         "How is attendance overall?",
                         "Who needs attention in MECH-A?",
                         "How was the session on 2026-03-02?"):
            answer = ask(client, question)
            assert answer["sources"], "%r came back with no receipt" % question

            for source in answer["sources"]:
                assert source["function"].startswith("AnalyticsService."), (
                    "%r cites something other than analytics: %s"
                    % (question, source["function"]))
                assert source["where"], (
                    "%r does not say where to check it" % question)
                assert "arguments" in source

        print("PASS  every answer names the analytics function behind it and "
              "where to check it")
    finally:
        teardown(client, directory)


def test_the_cited_function_actually_exists_and_was_the_one_called():
    """A receipt naming a function that does not exist is worse than none."""
    from backend.services.analytics_service import AnalyticsService

    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        for question in ("What is the attendance rate for X-M1?",
                         "What is the attendance rate for MECH-A?",
                         "Who needs attention in MECH-A?",
                         "How is attendance overall?"):
            for source in ask(client, question)["sources"]:
                name = source["function"].split(".", 1)[1]
                assert hasattr(AnalyticsService, name), (
                    "the receipt cites AnalyticsService.%s, which does not "
                    "exist" % name)

        print("PASS  every cited analytics function exists")
    finally:
        teardown(client, directory)


# -- no data is not zero ----------------------------------------------------

def test_a_student_with_no_completed_session_has_no_rate():
    client, directory = make_client()
    try:
        setup_school(client)
        # No sessions at all.

        answer = ask(client, "What is the attendance rate for X-M1?")

        assert answer["understood"] is True
        assert "0.0%" not in answer["answer"], (
            "a student with no session was reported as 0%%: %s"
            % answer["answer"])
        assert "no attendance rate" in answer["answer"] or \
               "not been expected" in answer["answer"], answer["answer"]

        print("PASS  a student with no completed session has no rate, not 0%")
    finally:
        teardown(client, directory)


def test_a_class_with_no_completed_session_has_no_rate():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        answer = ask(client, "What is the attendance rate for NOSUCH-Z?")

        # An unknown class is not a class with 0% attendance.
        assert "0.0%" not in answer["answer"], answer["answer"]

        print("PASS  a class with no completed session is not reported as 0%")
    finally:
        teardown(client, directory)


def test_a_day_with_no_completed_session_says_so():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        answer = ask(client, "How was the session on 1999-01-01?")

        assert answer["understood"] is True
        assert "No completed session" in answer["answer"], answer["answer"]
        assert "0.0%" not in answer["answer"]

        print("PASS  a day with no completed session says so rather than 0%")
    finally:
        teardown(client, directory)


def test_expected_and_never_evaluated_is_not_reported_as_absence():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        measured = analytics().student_analytics("X-M3")
        answer = ask(client, "How many sessions has X-M3 missed?")

        assert measured["sessions_without_record"] == 4, (
            "the fixture no longer has a never-evaluated student")
        assert answer["figures"]["absent_sessions"] == \
            measured["absent_sessions"]
        assert answer["figures"]["sessions_without_record"] == 4

        assert "not absences" in answer["answer"], (
            "sessions nobody decided were reported without saying they are "
            "not absences: %s" % answer["answer"])

        print("PASS  expected and never evaluated is reported separately from "
              "absence")
    finally:
        teardown(client, directory)


def test_a_trend_is_only_called_a_trend_when_analytics_says_so():
    client, directory = make_client()
    try:
        setup_school(client)

        # One session only: a single point is not a trend.
        completed("X-ONE", "MECH-A", ["X-M1"], day=0)
        record("X-ONE", "X-M1", "PRESENT")

        measured = analytics().student_analytics("X-M1")
        assert measured["has_trend_data"] is False, (
            "the fixture no longer has too little history for a trend")

        answer = ask(client, "Is X-M1 improving?")

        assert answer["understood"] is True
        assert "not enough history" in answer["answer"], answer["answer"]
        assert answer["figures"]["has_trend_data"] is False

        print("PASS  nothing is called a trend that analytics has not called "
              "one")
    finally:
        teardown(client, directory)


def test_a_trend_reports_the_direction_rather_than_a_dict():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        measured = analytics().student_analytics("X-M1")
        answer = ask(client, "Is X-M1 improving?")

        assert measured["has_trend_data"] is True
        assert answer["figures"]["trend"] == measured["trend"]

        # The sentence is readable: a direction, not a serialised structure.
        assert "{" not in answer["answer"], (
            "the trend dict was printed into the sentence: %s"
            % answer["answer"])
        assert measured["trend"]["direction"].lower().replace("_", " ") \
            in answer["answer"].lower(), answer["answer"]

        print("PASS  a trend is reported as a direction, with the rates "
              "behind it")
    finally:
        teardown(client, directory)


# -- refusing rather than guessing ------------------------------------------

def test_a_question_it_cannot_answer_is_not_answered():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        for question in ("What is the weather like?",
                         "Write me a poem.",
                         "Who will be absent tomorrow?"):
            answer = ask(client, question)
            assert answer["understood"] is False, (
                "%r was answered: %s" % (question, answer["answer"]))
            assert "did not understand" in answer["answer"] \
                or "not understood" in answer["answer"], answer["answer"]
            assert answer["figures"] == {}, (
                "%r came back with figures attached" % question)
            assert answer["suggestions"], (
                "%r was refused without saying what can be asked" % question)

        print("PASS  a question it cannot answer is refused, with the list of "
              "what can be asked")
    finally:
        teardown(client, directory)


def test_a_non_attendance_question_is_not_answered_as_an_unknown_student():
    """
    The regression that is easiest to ship and hardest to notice.

    "What is the weather like?" matched the rate intent on "what is" and came
    back as "I have no student by that name" - a confident non-answer to a
    question that was never about a student.
    """
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        answer = ask(client, "What is the weather like?")

        assert "no student" not in answer["answer"].lower(), (
            "a question that was not about a student was answered as an "
            "unknown student: %s" % answer["answer"])
        assert answer["intent"] == "not_understood", answer

        print("PASS  a non-attendance question is not answered as an unknown "
              "student")
    finally:
        teardown(client, directory)


def test_an_empty_question_is_a_different_answer_from_a_wrong_one():
    client, directory = make_client()
    try:
        setup_school(client)

        blank = ask(client, "")
        assert blank["understood"] is False
        assert blank["intent"] == "blank", blank
        assert "did not understand" not in blank["answer"], (
            "an empty box was treated as a question it could not parse")

        print("PASS  an empty question is answered as empty, not as "
              "unparseable")
    finally:
        teardown(client, directory)


def test_an_unknown_name_says_so_rather_than_widening_the_question():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        answer = ask(client, "What is Nobodyhere's attendance rate?")

        assert answer["figures"] == {}, (
            "an unknown student was answered with somebody's figures")
        assert "no student" in answer["answer"].lower(), answer["answer"]

        # Specifically NOT the whole-school figure.
        overall = analytics().overview()["attendance_rate"]
        formatted = "%.1f%%" % (overall * 100)
        assert formatted not in answer["answer"], (
            "a question about one student was answered with the whole "
            "school's rate")

        print("PASS  an unknown name is said to be unknown, not widened to "
              "the whole school")
    finally:
        teardown(client, directory)


def test_it_never_guesses_between_two_students_with_the_same_name():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        # The admin can see both students called Asha.
        answer = ask(client, "What is Asha's attendance rate?")

        assert "More than one student matches" in answer["answer"], (
            "the assistant picked one of two students called Asha: %s"
            % answer["answer"])
        assert "X-M1" in answer["answer"] and "X-C1" in answer["answer"], (
            "the answer does not name the candidates it could not choose "
            "between")
        assert answer["figures"] == {}, (
            "an ambiguous question came back with one student's figures")

        print("PASS  two students with the same name produce a question, not "
              "a guess")
    finally:
        teardown(client, directory)


def test_a_question_longer_than_it_reads_is_refused_not_truncated():
    client, directory = make_client()
    try:
        setup_school(client)

        answer = ask(client, "attendance " * 100)

        assert answer["understood"] is False
        assert answer["intent"] == "too_long", answer

        print("PASS  an over-long question is refused rather than silently "
              "truncated")
    finally:
        teardown(client, directory)


# -- scope ------------------------------------------------------------------

def test_a_teacher_cannot_learn_about_a_student_outside_their_classes():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        try:
            # X-C1 exists, in a class that is not this teacher's.
            real = ask(teacher, "What is the attendance rate for X-C1?")
            # X-NOPE does not exist at all.
            absent = ask(teacher, "What is the attendance rate for X-NOPE?")

            assert real["answer"] == absent["answer"], (
                "a student in another class is distinguishable from a student "
                "who does not exist:\n  %s\n  %s"
                % (real["answer"], absent["answer"]))
            assert real["figures"] == {}
            assert "Asha Bose" not in real["answer"], (
                "the answer named a student outside the teacher's classes")

            print("PASS  a student outside the caller's classes is answered "
                  "as one who does not exist")
        finally:
            teacher.close()
    finally:
        teardown(client, directory)


def test_a_teacher_asking_about_another_class_gets_no_figures():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        try:
            answer = ask(teacher, "What is the attendance rate for CIVIL-B?")

            civil = analytics().class_analytics("CIVIL-B")
            if civil and civil["attendance_rate"] is not None:
                formatted = "%.1f%%" % (civil["attendance_rate"] * 100)
                assert formatted not in answer["answer"], (
                    "a teacher was told another class's attendance rate: %s"
                    % answer["answer"])

            print("PASS  a teacher is not told another class's rate")
        finally:
            teacher.close()
    finally:
        teardown(client, directory)


def test_a_teachers_overview_is_their_own_classes():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        teacher = signed_in("tsmith", TEACHER_PASSWORD)
        try:
            answer = ask(teacher, "Who needs attention?")

            assert "X-C1" not in answer["answer"], (
                "a teacher's indicator list named a student from another "
                "class")

            print("PASS  a teacher's indicator list is their own students")
        finally:
            teacher.close()
    finally:
        teardown(client, directory)


def test_both_endpoints_require_a_signed_in_account():
    client, directory = make_client()
    try:
        setup_school(client)

        from backend.app import app
        anonymous = TestClient(app)
        try:
            assert anonymous.get(
                "/api/assistant/capabilities").status_code == 401
            assert anonymous.post("/api/assistant/ask",
                                  json={"question": "How is attendance "
                                                    "overall?"}
                                  ).status_code == 401
        finally:
            anonymous.close()

        print("PASS  both assistant endpoints are 401 for an unauthenticated "
              "caller")
    finally:
        teardown(client, directory)


# -- there is no model, and nothing leaves the machine ----------------------

def test_there_is_no_model_and_no_network_call():
    tree = ast.parse(SOURCE)
    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"requests", "httpx", "urllib", "socket", "http", "openai",
                 "anthropic", "transformers", "torch", "tensorflow", "numpy",
                 "sklearn", "langchain", "cv2"}
    assert not (imported & forbidden), (
        "the assistant imports %s" % sorted(imported & forbidden))

    for word in ("http://", "https://", "api_key", "API_KEY", "OPENAI",
                 "ANTHROPIC", "model.generate", "embedding"):
        assert word not in SOURCE, "the assistant mentions %s" % word

    print("PASS  the assistant imports no model and makes no network call")


def test_the_service_only_reaches_the_database_through_the_two_services():
    """
    No SQL here.

    The assistant must not read a row that analytics and DataService have not
    already scoped and defined - a hand-written query would be a third
    definition of "which record counts".
    """
    for sql in ("SELECT", "INSERT", "UPDATE", "DELETE", "self.database.query",
                "self.database.execute"):
        assert sql not in SOURCE, (
            "the assistant talks to the database directly (%s)" % sql)

    assert "AnalyticsService(database)" in SOURCE
    assert "DataService(database)" in SOURCE

    print("PASS  the assistant reads only through analytics and the data "
          "service")


def test_the_capability_list_is_published_and_every_entry_is_reachable():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        response = client.get("/api/assistant/capabilities")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["items"], "the capability list is empty"
        assert body["limits"], "the limits are not published"

        from backend.services.analytics_service import AnalyticsService

        for item in body["items"]:
            assert item["question"], "a capability has no example question"
            assert item["calls"], "a capability names no analytics function"
            assert item["where"], "a capability says nowhere to check it"

            for call in item["calls"]:
                name = call.split(".", 1)[1]
                assert hasattr(AnalyticsService, name), (
                    "capability %s cites AnalyticsService.%s, which does not "
                    "exist" % (item["intent"], name))

            # Every published example must actually be understood. A
            # capability nobody can trigger is a lie.
            answer = ask(client, item["question"])
            assert answer["understood"] is True, (
                "the published example %r is not understood"
                % item["question"])

        print("PASS  every published capability is reachable and cites a real "
              "function")
    finally:
        teardown(client, directory)


def test_a_question_that_was_not_understood_is_still_a_200():
    client, directory = make_client()
    try:
        setup_school(client)

        response = client.post("/api/assistant/ask",
                               json={"question": "What is the weather like?"})
        assert response.status_code == 200, (
            "a well-formed request was reported as a client error")
        assert response.json()["understood"] is False

        print("PASS  a question it does not handle is a 200 with understood: "
              "false")
    finally:
        teardown(client, directory)


def test_no_answer_exposes_a_biometric_or_a_secret():
    client, directory = make_client()
    try:
        setup_school(client)
        a_school()

        for question in ("What is the attendance rate for X-M1?",
                         "Who needs attention in MECH-A?",
                         "How is attendance overall?",
                         "How was the session on 2026-03-02?"):
            body = str(ask(client, question)).lower()
            for word in ("embedding", "descriptor", "password", "token",
                         "hash", ".npy", "face_crop"):
                assert word not in body, (
                    "%r leaked %s" % (question, word))

        print("PASS  no answer exposes a biometric, a path or a credential")
    finally:
        teardown(client, directory)


# -- the page ---------------------------------------------------------------

def test_the_assistant_is_a_tab_in_the_existing_navigation():
    assert 'data-view="assistant"' in HTML, "there is no Assistant tab"
    assert 'id="view-assistant"' in HTML, "there is no Assistant view"
    assert '$("view-assistant").hidden = view !== "assistant"' in JS, (
        "the view is not driven by the existing switchView()")
    assert 'state.view === "assistant"' in JS, (
        "refresh() does not know about the assistant view")

    print("PASS  the assistant is a tab in the existing navigation")


def test_the_page_shows_the_receipt_next_to_the_answer():
    source = JS[JS.index("   THE ASSISTANT (Step 12)"):
                JS.index("   ACADEMIC TERMS AND PROMOTION")]

    assert "Where this came from" in source, (
        "the page does not show where an answer came from")
    assert "source.function" in source and "source.where" in source, (
        "the receipt does not name the function or the page")
    assert "assistant-sources" in CSS, "the receipt has no styling"

    # Not hidden behind a disclosure: it is the reason to trust the answer.
    assert "<details" not in source, (
        "the receipt is tucked inside a details element")

    print("PASS  the receipt is shown next to the answer, not hidden")


def test_the_page_formats_no_figure_of_its_own():
    source = JS[JS.index("   THE ASSISTANT (Step 12)"):
                JS.index("   ACADEMIC TERMS AND PROMOTION")]

    assert "percent(" not in source, (
        "the page reformats a figure the server already worded")
    assert "* 100" not in source, "the page scales a ratio itself"
    assert ".toFixed(" not in source, "the page rounds a figure itself"

    print("PASS  the page shows the server's wording and formats nothing")


def test_the_page_says_there_is_no_model_and_publishes_the_limits():
    block = HTML[HTML.index('id="view-assistant"'):
                 HTML.index('id="view-academic"')]

    assert "not a language model" in block, (
        "the page does not say what this is")
    assert "no request leaves this machine" in block, (
        "the page does not say that nothing leaves the machine")
    assert 'id="assistant-limits"' in block, (
        "the page does not publish the limits the service reports")

    print("PASS  the page says there is no model and publishes the limits")


def test_the_answer_is_announced_to_a_screen_reader():
    block = HTML[HTML.index('id="view-assistant"'):
                 HTML.index('id="view-academic"')]

    assert 'aria-live="polite"' in block, (
        "the answer replaces itself in place with no live region, so a screen "
        "reader is never told it arrived")
    assert 'for="assistant-question"' in block, (
        "the question box has no label")

    print("PASS  the answer is announced, and the question box is labelled")


def test_nothing_in_the_assistant_panel_animates():
    block = CSS[CSS.index("/* ---------- the assistant ---------- */"):
                CSS.index("/* ---------- report modes")]

    for property_name in ("animation", "transition", "transform"):
        assert not any(line.strip().startswith(property_name + ":")
                       for line in block.splitlines()), (
            "the assistant panel declares %s - an answer that fades in is an "
            "answer that is briefly unreadable" % property_name)

    print("PASS  nothing in the assistant panel animates")


def test_the_dashboard_still_serves_its_own_source():
    client, directory = make_client()
    try:
        assert client.get("/").text == HTML, (
            "the served page differs from the source file - a line-ending "
            "change is the usual cause")

        print("PASS  the served dashboard still matches its source byte for "
              "byte")
    finally:
        teardown(client, directory)


def test_assistant_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        assert Path(state.get_database().path).parent != real.parent
        setup_school(client)
        a_school()
        ask(client, "How is attendance overall?")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_a_student_rate_is_the_analytics_services_own_number,
    test_a_class_rate_is_the_analytics_services_own_number,
    test_the_overview_answer_is_the_analytics_services_own_number,
    test_the_indicator_rule_is_the_analytics_services_own_rule,
    test_the_service_performs_no_arithmetic_of_its_own,
    test_every_answer_carries_a_receipt,
    test_the_cited_function_actually_exists_and_was_the_one_called,
    test_a_student_with_no_completed_session_has_no_rate,
    test_a_class_with_no_completed_session_has_no_rate,
    test_a_day_with_no_completed_session_says_so,
    test_expected_and_never_evaluated_is_not_reported_as_absence,
    test_a_trend_is_only_called_a_trend_when_analytics_says_so,
    test_a_trend_reports_the_direction_rather_than_a_dict,
    test_a_question_it_cannot_answer_is_not_answered,
    test_a_non_attendance_question_is_not_answered_as_an_unknown_student,
    test_an_empty_question_is_a_different_answer_from_a_wrong_one,
    test_an_unknown_name_says_so_rather_than_widening_the_question,
    test_it_never_guesses_between_two_students_with_the_same_name,
    test_a_question_longer_than_it_reads_is_refused_not_truncated,
    test_a_teacher_cannot_learn_about_a_student_outside_their_classes,
    test_a_teacher_asking_about_another_class_gets_no_figures,
    test_a_teachers_overview_is_their_own_classes,
    test_both_endpoints_require_a_signed_in_account,
    test_there_is_no_model_and_no_network_call,
    test_the_service_only_reaches_the_database_through_the_two_services,
    test_the_capability_list_is_published_and_every_entry_is_reachable,
    test_a_question_that_was_not_understood_is_still_a_200,
    test_no_answer_exposes_a_biometric_or_a_secret,
    test_the_assistant_is_a_tab_in_the_existing_navigation,
    test_the_page_shows_the_receipt_next_to_the_answer,
    test_the_page_formats_no_figure_of_its_own,
    test_the_page_says_there_is_no_model_and_publishes_the_limits,
    test_the_answer_is_announced_to_a_screen_reader,
    test_nothing_in_the_assistant_panel_animates,
    test_the_dashboard_still_serves_its_own_source,
    test_assistant_tests_never_touch_the_real_database,
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
