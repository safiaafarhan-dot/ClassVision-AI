"""
Year + class + section filtering over the student registry.

    Eight combinations of three independent filters, resolved in SQL.

WHAT THIS VERIFIES

    All eight combinations - none, each one alone, each pair, and all three -
    return exactly the students that match every selected filter and no
    others.

    That the filtering happens in the DATABASE: the service is handed a
    counting connection and the number of rows it reads is asserted, so a
    future change that loads the registry and sifts it in Python fails here
    rather than six months later on a school with five thousand students.

    That the filter options come from the registry rather than from a
    constant, and that they narrow by the other two selections.

    That duplicate names are irrelevant to all of it, because the identity is
    the student id.

    That an existing database's academic year is backfilled from the history
    it already holds, and that a database with no history keeps ''.

WHAT THIS DOES NOT VERIFY

    Query plans. The row-count assertions prove the work is not being done in
    Python; whether SQLite chooses idx_students_year_class_section for a
    given filter is SQLite's decision and asserting it would be asserting a
    version of SQLite.

Run with:
    python tests/test_student_registry_filters.py
"""

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

from paths import enable_safe_console  # noqa: E402

DUPLICATE_NAME = "Rahul Kumar"

# The register every filter test below is asked about.
#
# Deliberately not a neat grid: two students share a name inside one section,
# a third shares it from another year, and one student has no year recorded at
# all - which is a real state for anybody registered before a term existed.
ROSTER = (
    # student_id, name,          year,       class, section
    ("STU001", DUPLICATE_NAME,   "3rd Year", "CSE", "A"),
    ("STU002", "Priya Das",      "3rd Year", "CSE", "A"),
    ("STU027", DUPLICATE_NAME,   "3rd Year", "CSE", "B"),
    ("STU040", "Anita Bose",     "3rd Year", "ECE", "A"),
    ("STU103", DUPLICATE_NAME,   "2nd Year", "CSE", "A"),
    ("STU104", "Amit Roy",       "2nd Year", "CSE", "C"),
    ("STU150", "Sneha Pal",      "2nd Year", "ECE", "B"),
    ("STU900", "No Term Yet",    "",         "CSE", "A"),
)


class Bench:
    """A temporary registry, loaded with ROSTER."""

    def __init__(self, roster=ROSTER):
        from backend.services import state

        self.state = state
        self.directory = Path(tempfile.mkdtemp(prefix="classvision_filters_"))
        state.configure(self.directory / "filters.db")
        self.database = state.get_database()

        from backend.services.data_service import DataService

        self.service = DataService(self.database)

        for student_id, name, year, class_name, section in roster:
            self.service.create_student(student_id, name,
                                        class_name=class_name,
                                        section=section,
                                        academic_year=year)

    def ids(self, **filters):
        """The student ids one filter combination returns, and the total."""
        page = self.service.list_students(with_attendance=False,
                                          limit=100, **filters)
        return page["total"], [item["student_id"] for item in page["items"]]

    def close(self):
        self.state.close_database()
        shutil.rmtree(self.directory, ignore_errors=True)


class CountingDatabase:
    """
    A database that records how many rows each query returned.

    Wraps the real one rather than replacing it, so the queries under test are
    the real queries. `rows_read` is what the assertions are about: it is the
    number of student rows that crossed from SQLite into Python, which is the
    thing that must not grow with the size of the school.
    """

    def __init__(self, database):
        self._database = database
        self.rows_read = 0
        self.queries = []

    def query(self, sql, parameters=()):
        rows = self._database.query(sql, parameters)
        self.queries.append(sql)
        if "FROM students" in sql:
            self.rows_read += len(rows)
        return rows

    def query_one(self, sql, parameters=()):
        self.queries.append(sql)
        return self._database.query_one(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._database, name)


# -- the eight combinations ------------------------------------------------

def test_no_filters_returns_every_registered_student():
    bench = Bench()
    try:
        total, ids = bench.ids()
        assert total == len(ROSTER), (
            "expected all %d students, got %d" % (len(ROSTER), total))
        assert ids == sorted(entry[0] for entry in ROSTER), (
            "the unfiltered list is not the whole register: %s" % ids)

        print("PASS  no filters returns all %d registered students" % total)
    finally:
        bench.close()


def test_year_only():
    bench = Bench()
    try:
        total, ids = bench.ids(academic_year="3rd Year")
        assert ids == ["STU001", "STU002", "STU027", "STU040"], ids
        assert total == 4

        # Across classes AND sections, which is the point of a year filter.
        classes = {item["class_name"] for item in
                   bench.service.list_students(academic_year="3rd Year",
                                               with_attendance=False,
                                               limit=100)["items"]}
        assert classes == {"CSE", "ECE"}, (
            "a year filter should span classes, got %s" % classes)

        print("PASS  year alone returns every student of that year, across "
              "classes and sections")
    finally:
        bench.close()


def test_class_only():
    bench = Bench()
    try:
        total, ids = bench.ids(class_name="CSE")
        assert ids == ["STU001", "STU002", "STU027", "STU103", "STU104",
                       "STU900"], ids
        assert total == 6

        print("PASS  class alone returns every student of that class, across "
              "years and sections")
    finally:
        bench.close()


def test_section_only():
    bench = Bench()
    try:
        total, ids = bench.ids(section="A")
        assert ids == ["STU001", "STU002", "STU040", "STU103", "STU900"], ids
        assert total == 5

        print("PASS  section alone returns every student of that section, "
              "across years and classes")
    finally:
        bench.close()


def test_year_and_class():
    bench = Bench()
    try:
        total, ids = bench.ids(academic_year="3rd Year", class_name="CSE")
        assert ids == ["STU001", "STU002", "STU027"], ids
        assert total == 3

        print("PASS  year + class returns only that pair")
    finally:
        bench.close()


def test_year_and_section():
    bench = Bench()
    try:
        total, ids = bench.ids(academic_year="3rd Year", section="A")
        assert ids == ["STU001", "STU002", "STU040"], ids
        assert total == 3

        print("PASS  year + section returns only that pair, across classes")
    finally:
        bench.close()


def test_class_and_section():
    bench = Bench()
    try:
        total, ids = bench.ids(class_name="CSE", section="A")
        assert ids == ["STU001", "STU002", "STU103", "STU900"], ids
        assert total == 4

        print("PASS  class + section returns only that pair, across years")
    finally:
        bench.close()


def test_year_and_class_and_section():
    """The requirement's worked example: 3rd Year, CSE, Section A."""
    bench = Bench()
    try:
        total, ids = bench.ids(academic_year="3rd Year", class_name="CSE",
                               section="A")
        assert ids == ["STU001", "STU002"], ids
        assert total == 2

        # Every one of the three conditions is doing work: dropping any one of
        # them returns more.
        for relaxed in (dict(class_name="CSE", section="A"),
                        dict(academic_year="3rd Year", section="A"),
                        dict(academic_year="3rd Year", class_name="CSE")):
            wider, _ = bench.ids(**relaxed)
            assert wider > total, (
                "removing a filter did not widen the result: %s" % relaxed)

        print("PASS  year + class + section returns only the students "
              "matching all three")
    finally:
        bench.close()


def test_a_combination_nobody_is_in_returns_zero():
    """
    An empty result is a result. Not an error, and not the whole register.
    """
    bench = Bench()
    try:
        total, ids = bench.ids(academic_year="2nd Year", class_name="ECE",
                               section="A")
        assert total == 0, "expected nobody, got %d" % total
        assert ids == [], ids

        # And a year that does not exist at all.
        total, ids = bench.ids(academic_year="7th Year")
        assert total == 0 and ids == []

        print("PASS  a combination nobody is in returns zero students, not "
              "an error and not everybody")
    finally:
        bench.close()


def test_the_filters_are_case_insensitive():
    """
    A teacher typing "cse" is asking about CSE.

    The same COLLATE NOCASE the class and section filters already used,
    extended to the year so all three behave alike.
    """
    bench = Bench()
    try:
        total, ids = bench.ids(academic_year="3RD YEAR", class_name="cse",
                               section="a")
        assert ids == ["STU001", "STU002"], ids
        assert total == 2

        print("PASS  all three filters are case-insensitive")
    finally:
        bench.close()


# -- duplicate names ------------------------------------------------------

def test_three_students_with_one_name_filter_by_their_own_grouping():
    """
    The name is in all three places and decides none of them.

    Rahul Kumar is in 3rd/CSE/A, 3rd/CSE/B and 2nd/CSE/A, and each filter
    picks out exactly the right one by its own year, class and section.
    """
    bench = Bench()
    try:
        rahuls = [entry[0] for entry in ROSTER if entry[1] == DUPLICATE_NAME]
        assert len(rahuls) == 3, "the fixture should have three of them"

        total, ids = bench.ids(search=DUPLICATE_NAME)
        assert sorted(ids) == sorted(rahuls), (
            "searching the shared name should find all three: %s" % ids)

        # Each one, isolated by its grouping rather than by its name.
        cases = (
            (dict(academic_year="3rd Year", class_name="CSE", section="A"),
             "STU001"),
            (dict(academic_year="3rd Year", class_name="CSE", section="B"),
             "STU027"),
            (dict(academic_year="2nd Year", class_name="CSE", section="A"),
             "STU103"),
        )
        for filters, expected in cases:
            filters = dict(filters, search=DUPLICATE_NAME)
            total, ids = bench.ids(**filters)
            assert ids == [expected], (
                "%s should isolate %s, got %s" % (filters, expected, ids))

        print("PASS  three students called %r are told apart by year, class "
              "and section" % DUPLICATE_NAME)
    finally:
        bench.close()


def test_every_row_carries_the_identity_and_the_grouping():
    """
    What the table needs in order to be readable when names repeat - and
    nothing a password lives in.
    """
    bench = Bench()
    try:
        page = bench.service.list_students(with_attendance=False, limit=100)
        row = next(item for item in page["items"]
                   if item["student_id"] == "STU001")

        for field in ("student_id", "name", "roll_number", "academic_year",
                      "class_name", "section", "status"):
            assert field in row, "the row is missing %s" % field

        assert row["academic_year"] == "3rd Year", row["academic_year"]

        for secret in ("password", "password_hash", "username"):
            assert secret not in row, (
                "the student row exposes %s" % secret)

        print("PASS  every row carries its id, year, class and section, and "
              "no credential")
    finally:
        bench.close()


def test_a_student_with_no_term_recorded_is_not_invented():
    """
    '' is a real value and must not be guessed at.

    A student registered before anybody created a term has no year. They
    appear unfiltered, they appear under their class, and they appear under NO
    year - because claiming one would be making it up.
    """
    bench = Bench()
    try:
        _, unfiltered = bench.ids()
        assert "STU900" in unfiltered

        _, by_class = bench.ids(class_name="CSE")
        assert "STU900" in by_class

        for year in ("3rd Year", "2nd Year", ""):
            _, by_year = bench.ids(academic_year=year)
            if year:
                assert "STU900" not in by_year, (
                    "a student with no term was returned for %r" % year)

        # An empty year is "no filter", not "students with no year": it is the
        # same value the Clear button writes, and clearing must show everyone.
        total, _ = bench.ids(academic_year="")
        assert total == len(ROSTER), (
            "an empty year should mean no filter, got %d" % total)

        options = bench.service.student_filter_options()
        assert "" not in options["years"], (
            "the blank year should not be offered as an option")

        print("PASS  a student with no term recorded keeps '' and is never "
              "filed under a year")
    finally:
        bench.close()


# -- the filter options ---------------------------------------------------

def test_the_options_come_from_the_registry():
    bench = Bench()
    try:
        options = bench.service.student_filter_options()

        assert options["years"] == ["2nd Year", "3rd Year"], options["years"]
        assert options["classes"] == ["CSE", "ECE"], options["classes"]
        assert options["sections"] == ["A", "B", "C"], options["sections"]

        print("PASS  the filter options are the years, classes and sections "
              "students are actually in")
    finally:
        bench.close()


def test_the_options_narrow_by_the_other_two_selections():
    """
    The requirement's example: 3rd Year + CSE has sections A and B, so C must
    not be offered.

    And each list is NOT narrowed by its own value, so the year dropdown still
    shows every year after one has been chosen - otherwise it would collapse
    to a single option the moment somebody used it.
    """
    bench = Bench()
    try:
        options = bench.service.student_filter_options(
            academic_year="3rd Year", class_name="CSE")

        assert options["sections"] == ["A", "B"], (
            "3rd Year + CSE has sections A and B only, got %s"
            % options["sections"])
        assert "C" not in options["sections"], (
            "a section that pair has nobody in was offered")

        assert options["years"] == ["2nd Year", "3rd Year"], (
            "the year list must not narrow by its own value: %s"
            % options["years"])
        assert options["classes"] == ["CSE", "ECE"], (
            "the class list must not narrow by its own value: %s"
            % options["classes"])

        # Section narrows the classes, in the other direction.
        narrowed = bench.service.student_filter_options(section="C")
        assert narrowed["classes"] == ["CSE"], narrowed["classes"]
        assert narrowed["years"] == ["2nd Year"], narrowed["years"]

        print("PASS  each option list narrows by the other two selections and "
              "never by its own")
    finally:
        bench.close()


def test_an_empty_registry_offers_no_options():
    bench = Bench(roster=())
    try:
        options = bench.service.student_filter_options()
        assert options == {"years": [], "classes": [], "sections": []}, options

        total, ids = bench.ids()
        assert total == 0 and ids == []

        print("PASS  an empty registry offers no filter options and lists no "
              "students")
    finally:
        bench.close()


# -- it happens in the database -------------------------------------------

def test_filtering_is_done_by_the_database_and_not_in_python():
    """
    The scalability assertion, and the reason this suite exists at all.

    A filtered page must read only the rows it returns. If somebody later
    replaces the WHERE clause with a list comprehension, the row count jumps
    from two to eight here and from twenty-five to five thousand on a real
    school.
    """
    bench = Bench()
    try:
        counting = CountingDatabase(bench.database)

        from backend.services.data_service import DataService

        service = DataService(counting)
        page = service.list_students(academic_year="3rd Year",
                                     class_name="CSE", section="A",
                                     with_attendance=False, limit=25)

        assert page["total"] == 2, page["total"]
        assert counting.rows_read == 2, (
            "the service read %d student rows to return 2; the filter is not "
            "being applied in SQL" % counting.rows_read)

        # The total came from a COUNT, not from counting rows in Python.
        assert any("COUNT(*)" in sql and "FROM students" in sql
                   for sql in counting.queries), (
            "the total was not obtained with a COUNT query")

        print("PASS  a three-filter page reads 2 student rows and counts with "
              "COUNT(*), not in Python")
    finally:
        bench.close()


def test_paging_reads_one_page_and_not_the_whole_register():
    bench = Bench()
    try:
        counting = CountingDatabase(bench.database)

        from backend.services.data_service import DataService

        service = DataService(counting)
        page = service.list_students(with_attendance=False, limit=3, offset=0)

        assert page["total"] == len(ROSTER), (
            "the total should be the whole register, got %d" % page["total"])
        assert len(page["items"]) == 3
        assert counting.rows_read == 3, (
            "the service read %d rows to return a page of 3"
            % counting.rows_read)

        print("PASS  a page of 3 reads 3 rows and still reports the full "
              "total")
    finally:
        bench.close()


def test_the_options_do_not_read_one_row_per_student():
    """
    The option lists are bounded by the timetable, not by the roll.

    One GROUP BY returns one row per distinct year/class/section, so a school
    of five thousand students answers this with a few dozen rows.
    """
    bench = Bench()
    try:
        counting = CountingDatabase(bench.database)

        from backend.services.data_service import DataService

        DataService(counting).student_filter_options()

        groupings = len({(entry[2], entry[3], entry[4]) for entry in ROSTER})
        assert counting.rows_read == groupings, (
            "the options read %d rows for %d distinct groupings"
            % (counting.rows_read, groupings))
        assert counting.rows_read < len(ROSTER), (
            "the options read one row per student")

        print("PASS  the filter options read %d grouping rows, not %d student "
              "rows" % (groupings, len(ROSTER)))
    finally:
        bench.close()


def test_five_thousand_students_filter_without_reading_them_all():
    """
    The scale case, run rather than argued.

    Five thousand students across three years, three classes and four
    sections. A fully filtered page must still read only its own page.
    """
    bench = Bench(roster=())
    try:
        from repositories import StudentRepository

        students = StudentRepository(bench.database)
        years = ("1st Year", "2nd Year", "3rd Year")
        classes = ("CSE", "ECE", "EEE")
        sections = ("A", "B", "C", "D")

        total = 5000
        with bench.database.transaction():
            for index in range(total):
                students.insert(
                    "STU%05d" % index,
                    # Every student shares one name, so nothing can be
                    # accidentally relying on it to tell them apart.
                    DUPLICATE_NAME,
                    # Different divisors on purpose: with the same one,
                    # year and class would move together and "3rd Year + CSE"
                    # would have nobody in it.
                    academic_year=years[index % 3],
                    class_name=classes[(index // 3) % 3],
                    section=sections[(index // 9) % 4])

        counting = CountingDatabase(bench.database)

        from backend.services.data_service import DataService

        service = DataService(counting)
        page = service.list_students(academic_year="3rd Year",
                                     class_name="CSE", section="A",
                                     with_attendance=False, limit=25)

        assert page["total"] > 0, "the fixture produced no matching students"
        assert len(page["items"]) == 25
        assert counting.rows_read == 25, (
            "the service read %d of %d students to return a page of 25"
            % (counting.rows_read, total))

        # Every returned student really does match all three filters.
        for item in page["items"]:
            assert item["academic_year"] == "3rd Year"
            assert item["class_name"] == "CSE"
            assert item["section"] == "A"

        # And the count is the database's, not a length.
        expected = bench.database.query_one(
            "SELECT COUNT(*) AS total FROM students WHERE academic_year = ? "
            "AND class_name = ? AND section = ?",
            ("3rd Year", "CSE", "A"))["total"]
        assert page["total"] == expected, (
            "reported %d, database says %d" % (page["total"], expected))

        print("PASS  %d students, all sharing one name: a filtered page reads "
              "25 rows and the total is a COUNT" % total)
    finally:
        bench.close()


def test_the_index_the_filter_relies_on_exists():
    bench = Bench(roster=())
    try:
        indexes = {row["name"] for row in bench.database.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'students'")}

        assert "idx_students_year_class_section" in indexes, (
            "the year-leading composite index is missing: %s" % indexes)
        assert "idx_students_class" in indexes, (
            "the existing class+section index was removed")

        print("PASS  both student filter indexes exist, the new one and the "
              "one it did not replace")
    finally:
        bench.close()


# -- the API and the interface --------------------------------------------

def test_the_api_filters_and_counts():
    """
    Through HTTP, with the query parameters the interface actually sends.
    """
    from fastapi.testclient import TestClient

    bench = Bench()
    client = None
    try:
        from backend.app import app

        client = TestClient(app)

        cases = (
            ({}, 8),
            ({"academic_year": "3rd Year"}, 4),
            ({"class_name": "CSE"}, 6),
            ({"section": "A"}, 5),
            ({"academic_year": "3rd Year", "class_name": "CSE"}, 3),
            ({"academic_year": "3rd Year", "section": "A"}, 3),
            ({"class_name": "CSE", "section": "A"}, 4),
            ({"academic_year": "3rd Year", "class_name": "CSE",
              "section": "A"}, 2),
            ({"academic_year": "2nd Year", "class_name": "ECE",
              "section": "A"}, 0),
        )

        for parameters, expected in cases:
            response = client.get("/api/students", params=parameters)
            assert response.status_code == 200, (
                "%s returned %d" % (parameters, response.status_code))
            body = response.json()
            assert body["total"] == expected, (
                "%s returned %d students, expected %d"
                % (parameters, body["total"], expected))

        # The published schema still validates every row.
        from backend.schemas.models import Student

        for item in client.get("/api/students").json()["items"]:
            Student(**item)

        options = client.get("/api/students/filters",
                             params={"academic_year": "3rd Year",
                                     "class_name": "CSE"}).json()
        assert options["sections"] == ["A", "B"], options

        print("PASS  the API filters on all eight combinations, returns 0 "
              "cleanly, and its rows still validate")
    finally:
        if client is not None:
            client.close()
        bench.close()


def test_the_filters_endpoint_is_not_shadowed_by_the_student_route():
    """
    /api/students/filters must not be read as a student whose id is
    "filters".

    FastAPI matches in declaration order, so this is a real ordering bug and
    not a hypothetical one - it would return 404 for a student nobody has.
    """
    from fastapi.testclient import TestClient

    bench = Bench()
    client = None
    try:
        from backend.app import app

        client = TestClient(app)
        response = client.get("/api/students/filters")

        assert response.status_code == 200, (
            "/api/students/filters returned %d - the literal route is "
            "probably declared after /students/{student_id}"
            % response.status_code)
        body = response.json()
        assert set(body) == {"years", "classes", "sections"}, body

        print("PASS  /api/students/filters is reached, not swallowed by the "
              "student-id route")
    finally:
        if client is not None:
            client.close()
        bench.close()


def test_the_registry_still_requires_a_signed_in_caller():
    """
    RBAC is not bypassed by the new parameters.

    Both endpoints depend on require_user, exactly as the student list
    already did - asserted by reading the route rather than by configuring
    an installation, because an unconfigured one is deliberately open.
    """
    source = (PROJECT_ROOT / "backend" / "routes"
              / "students.py").read_text(encoding="utf-8")

    listing = source[source.index("def list_students("):]
    listing = listing[:listing.index("def student_filter_options(")]
    assert "principal: Principal = Depends(require_user)" in listing, (
        "the student list no longer requires a signed-in caller")

    options = source[source.index("def student_filter_options("):]
    options = options[:options.index("@router.post")]
    assert "principal: Principal = Depends(require_user)" in options, (
        "the filter-options endpoint does not require a signed-in caller")
    assert "principal=principal" in options, (
        "the filter-options endpoint does not pass the principal to the "
        "service, so a teacher's class scope would not apply")

    print("PASS  both registry endpoints require a signed-in caller and pass "
          "the principal through")


def test_a_scoped_teacher_sees_only_their_own_classes_and_options():
    """
    A teacher assigned one class gets that class's students and that class's
    filter options - the dropdown cannot offer what the rows would refuse.
    """
    bench = Bench()
    try:
        from backend.services.authz import Principal

        # configured=True matters: an UNCONFIGURED installation is
        # deliberately unrestricted, so a Principal built without it reports
        # unrestricted and would scope nothing.
        teacher = Principal({
            "user_id": 2, "username": "teacher.one", "role": "TEACHER",
            "assignments": [{"class_name": "CSE", "section": ""}],
        }, configured=True)

        page = bench.service.list_students(principal=teacher,
                                           with_attendance=False, limit=100)
        classes = {item["class_name"] for item in page["items"]}
        assert classes == {"CSE"}, (
            "a CSE teacher saw %s" % classes)

        options = bench.service.student_filter_options(principal=teacher)
        assert options["classes"] == ["CSE"], options["classes"]
        assert "ECE" not in options["classes"]

        print("PASS  a teacher scoped to CSE sees only CSE students and only "
              "CSE in the filters")
    finally:
        bench.close()


def test_the_interface_sends_the_filters_rather_than_sifting_rows():
    """
    The browser asks the server. It does not download the register.
    """
    script = (PROJECT_ROOT / "backend" / "static"
              / "app.js").read_text(encoding="utf-8")

    listing = script[script.index("async function loadStudents()"):]
    listing = listing[:listing.index("\nasync function renderStudentCount")]

    for parameter in ("academic_year", "class_name", "section"):
        assert 'query.set("%s"' % parameter in listing, (
            "loadStudents does not send %s to the server" % parameter)

    for sifting in (".filter((student)", "students.filter("):
        assert sifting not in listing, (
            "loadStudents appears to filter rows in the browser: %r"
            % sifting)

    html = (PROJECT_ROOT / "backend" / "static"
            / "index.html").read_text(encoding="utf-8")
    for control in ("student-filter-year", "student-filter-class",
                    "student-filter-section", "student-filter-clear",
                    "students-count"):
        assert 'id="%s"' % control in html, (
            "the registry has no %s control" % control)

    # The Year column, so the table shows the grouping it is filtered by.
    assert '<th scope="col">Year</th>' in html, (
        "the student table has no Year column")

    print("PASS  the interface sends all three filters to the API and sifts "
          "nothing itself")


# -- the upgrade path -----------------------------------------------------

def test_an_existing_database_is_backfilled_from_its_own_history():
    """
    A school already running terms knows which year each student is in.

    The column is filled from the is_current row of
    student_academic_history - the same row the registry's class and section
    were already mirrored from - so nobody has to re-enter data the database
    is holding. Nothing is invented: a student with no current enrolment
    keeps ''.
    """
    from database import Database

    directory = Path(tempfile.mkdtemp(prefix="classvision_backfill_"))
    database = None
    try:
        database = Database(directory / "old.db")
        database.initialize()

        from repositories import AcademicRepository, StudentRepository

        students = StudentRepository(database)
        academic = AcademicRepository(database)

        with database.transaction():
            students.insert("STU001", DUPLICATE_NAME, class_name="CSE",
                            section="A")
            students.insert("STU002", DUPLICATE_NAME, class_name="ECE",
                            section="B")

            academic.upsert_enrolment(
                student_id="STU001", academic_session_id=None,
                academic_year="3rd Year", semester="5", class_name="CSE",
                section="A", roll_number="17", is_current=1)

        # Blank the column to recreate the pre-upgrade state, then upgrade.
        database.connection.execute("UPDATE students SET academic_year = ''")
        filled = database.backfill_academic_year()

        assert filled >= 1, "the backfill filled nothing"
        assert students.get("STU001")["academic_year"] == "3rd Year", (
            "STU001 was not backfilled from their current enrolment")
        assert students.get("STU002")["academic_year"] == "", (
            "STU002 has no enrolment and must not be given a year")

        # Idempotent, and it cannot overwrite a hand-corrected year.
        students.update("STU002", academic_year="2nd Year")
        database.backfill_academic_year()
        assert students.get("STU002")["academic_year"] == "2nd Year", (
            "the backfill overwrote a year somebody had set by hand")

        print("PASS  an existing database is backfilled from its own history, "
              "invents nothing, and does not overwrite a correction")
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_enrolling_a_student_in_a_term_keeps_the_mirror_in_step():
    """
    The authority is still student_academic_history.

    Enrolling - or promoting - writes the year onto the student row in the
    same transaction it already used for class, section and roll number, so
    the registry's filter cannot drift from the history it mirrors.
    """
    bench = Bench(roster=())
    try:
        from repositories import StudentRepository

        students = StudentRepository(bench.database)
        with bench.database.transaction():
            students.insert("STU001", DUPLICATE_NAME)

        from backend.services.academic_service import AcademicService

        service = AcademicService(bench.database)
        service.enrol("STU001", academic_year="3rd Year", class_name="CSE",
                      section="A", roll_number="17")

        row = students.get("STU001")
        assert row["academic_year"] == "3rd Year", (
            "the mirror was not updated: %r" % row["academic_year"])
        assert row["class_name"] == "CSE" and row["section"] == "A"

        # And the filter finds them by it immediately.
        total, ids = bench.ids(academic_year="3rd Year", class_name="CSE",
                               section="A")
        assert ids == ["STU001"], ids
        assert total == 1

        print("PASS  enrolling a student mirrors the year onto the registry, "
              "and the filter finds them by it")
    finally:
        bench.close()


# -- runner ---------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_no_filters_returns_every_registered_student,
        test_year_only,
        test_class_only,
        test_section_only,
        test_year_and_class,
        test_year_and_section,
        test_class_and_section,
        test_year_and_class_and_section,
        test_a_combination_nobody_is_in_returns_zero,
        test_the_filters_are_case_insensitive,
        test_three_students_with_one_name_filter_by_their_own_grouping,
        test_every_row_carries_the_identity_and_the_grouping,
        test_a_student_with_no_term_recorded_is_not_invented,
        test_the_options_come_from_the_registry,
        test_the_options_narrow_by_the_other_two_selections,
        test_an_empty_registry_offers_no_options,
        test_filtering_is_done_by_the_database_and_not_in_python,
        test_paging_reads_one_page_and_not_the_whole_register,
        test_the_options_do_not_read_one_row_per_student,
        test_five_thousand_students_filter_without_reading_them_all,
        test_the_index_the_filter_relies_on_exists,
        test_the_api_filters_and_counts,
        test_the_filters_endpoint_is_not_shadowed_by_the_student_route,
        test_the_registry_still_requires_a_signed_in_caller,
        test_a_scoped_teacher_sees_only_their_own_classes_and_options,
        test_the_interface_sends_the_filters_rather_than_sifting_rows,
        test_an_existing_database_is_backfilled_from_its_own_history,
        test_enrolling_a_student_in_a_term_keeps_the_mirror_in_step,
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
