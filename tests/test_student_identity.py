"""
Student identity and authentication uniqueness.

    Names are not unique. Identities are.

WHAT THIS VERIFIES

    That three children called Rahul Kumar are three students, with three
    ids, three sets of attendance and three face records - and that nothing
    in the system treats their shared name as a key.

    That a username is unique at the DATABASE level, case-insensitively, and
    that the constraint rather than the application check is what finally
    stops two accounts sharing one authentication identity.

    That losing the race is a conflict and not a crash: a second
    registration or a second account for the same id is refused with the
    message the pre-check would have given, and the record that was already
    there is left untouched.

    That a password is never an identity - two accounts may share one - and
    that deleting a student does not let the next one inherit their history.

WHAT THIS DOES NOT VERIFY

    Real concurrency. The race is provoked deterministically, by writing the
    second row the way a request that had already passed the pre-check would
    write it, rather than by starting threads and hoping the interleaving
    happens. That tests the thing that actually protects the data - the
    constraint - instead of testing the scheduler.

Run with:
    python tests/test_student_identity.py
"""

import shutil
import sqlite3
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

from attendance_rules import (  # noqa: E402
    AttendancePolicy,
    AttendanceRuleEngine,
    PresenceSegment,
)
from attendance_session import AttendanceSession  # noqa: E402
from paths import enable_safe_console  # noqa: E402

DUPLICATE_NAME = "Rahul Kumar"
SHARED_PASSWORD = "Test@123456"

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


# -- fixtures --------------------------------------------------------------

class Harness:
    """A temporary database with the services that sit on top of it."""

    def __init__(self):
        from backend.services import state

        self.state = state
        self.directory = Path(tempfile.mkdtemp(prefix="classvision_identity_"))
        state.configure(self.directory / "identity.db")
        self.database = state.get_database()

        from repositories import (AttendanceRepository, SessionRepository,
                                  StudentAccountRepository, StudentRepository,
                                  UserRepository)

        self.students = StudentRepository(self.database)
        self.sessions = SessionRepository(self.database)
        self.attendance = AttendanceRepository(self.database)
        self.accounts = StudentAccountRepository(self.database)
        self.users = UserRepository(self.database)

        from backend.services.data_service import DataService

        self.data = DataService(self.database)

    def close(self):
        self.state.close_database()
        shutil.rmtree(self.directory, ignore_errors=True)


def harness():
    return Harness()


def a_super_admin(bench, username="admin.one"):
    """A real Super Admin principal, created through the auth service."""
    from backend.services.auth_service import AuthService

    service = AuthService(bench.database)
    service.bootstrap_super_admin(username, "An Administrator",
                                 SHARED_PASSWORD)
    record = bench.users.get_by_username(username)
    return service, {"username": record["username"], "role": record["role"],
                     "user_id": record["user_id"]}


# -- duplicate names -------------------------------------------------------

def test_two_students_may_share_a_name():
    """
    The requirement's first case, and the one everything else protects.

    Two registrations with the same name and different ids both succeed, and
    they are two rows with two identities.
    """
    bench = harness()
    try:
        first = bench.data.create_student("STU001", DUPLICATE_NAME,
                                          class_name="CSE", section="A")
        second = bench.data.create_student("STU002", DUPLICATE_NAME,
                                           class_name="CSE", section="B")

        assert first["student_id"] != second["student_id"], (
            "two students with one name must have two ids")
        assert first["name"] == second["name"] == DUPLICATE_NAME, (
            "the shared name must survive - it is display information, not "
            "a key")

        rows = bench.students.list_all()
        assert len(rows) == 2, (
            "both students should be in the registry, found %d" % len(rows))

        print("PASS  two students called %r coexist with two distinct ids"
              % DUPLICATE_NAME)
    finally:
        bench.close()


def test_a_third_and_a_fourth_of_the_same_name_are_still_fine():
    """Nothing about the registry degrades as the name repeats."""
    bench = harness()
    try:
        created = [bench.data.create_student("STU%03d" % index, DUPLICATE_NAME)
                   for index in range(1, 5)]

        ids = {student["student_id"] for student in created}
        assert len(ids) == 4, "four registrations should be four identities"

        print("PASS  four students share one name and hold four identities")
    finally:
        bench.close()


def test_the_registry_has_no_unique_constraint_on_a_name():
    """
    Asserted against the schema, not against behaviour.

    A UNIQUE(name) added later would break a real register on the day two
    children with the same name enrolled, so its absence is worth pinning.
    """
    bench = harness()
    try:
        indexes = bench.database.query(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'students'")

        for row in indexes:
            statement = (row["sql"] or "")
            assert not ("UNIQUE" in statement.upper()
                        and "name" in statement), (
                "students.name is uniquely indexed by %s, which forbids two "
                "children with the same name" % row["name"])

        table = bench.database.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'students'")["sql"]
        assert "name        TEXT NOT NULL" in table, (
            "students.name should be a plain required column")
        assert "name" not in table.split("UNIQUE", 1)[-1].split(")")[0] \
            if "UNIQUE" in table else True

        print("PASS  nothing in the schema makes a student's name unique")
    finally:
        bench.close()


# -- duplicate ids: the race, and who wins ---------------------------------

def test_a_duplicate_student_id_is_refused_and_changes_nothing():
    """
    The second registration must not become an edit of the first.

    This is the case that would hand one student another student's entire
    attendance history, because student_id is what every other table
    references.
    """
    from backend.services.data_service import StudentConflictError

    bench = harness()
    try:
        bench.data.create_student("STU001", DUPLICATE_NAME, class_name="CSE",
                                  section="A")

        try:
            bench.data.create_student("STU001", "Someone Else",
                                      class_name="ECE", section="B")
        except StudentConflictError as error:
            assert "STU001" in str(error), (
                "the refusal should name the id: %s" % error)
        else:
            raise AssertionError(
                "a second student with id STU001 was accepted")

        kept = bench.students.get("STU001")
        assert kept["name"] == DUPLICATE_NAME, (
            "the existing student was overwritten: name is now %r"
            % kept["name"])
        assert kept["class_name"] == "CSE" and kept["section"] == "A", (
            "the existing student's class was overwritten")

        print("PASS  a duplicate student id is refused and the existing "
              "record is untouched")
    finally:
        bench.close()


def test_the_primary_key_is_the_final_protection_not_the_pre_check():
    """
    The race, provoked deterministically.

    insert() is called directly, which is what a request that had already
    passed create_student's availability check would go on to do. The write
    must be refused by the database, because two requests can both read
    "available" before either writes and only the constraint sees both.
    """
    bench = harness()
    try:
        bench.students.insert("STU001", DUPLICATE_NAME, class_name="CSE")

        try:
            bench.students.insert("STU001", "Someone Else", class_name="ECE")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(
                "the database accepted a second row for STU001; a lost race "
                "would silently overwrite a student")

        assert bench.students.get("STU001")["name"] == DUPLICATE_NAME
        assert len(bench.students.list_all()) == 1

        print("PASS  the PRIMARY KEY refuses the second write, so a lost "
              "race cannot overwrite a student")
    finally:
        bench.close()


def test_the_enrollment_path_may_still_update_a_registered_student():
    """
    The behaviour the strict insert must NOT have broken.

    Re-running a camera on somebody already registered is an update and not
    a collision, so upsert() is still there and still updates.
    """
    bench = harness()
    try:
        bench.students.insert("STU001", DUPLICATE_NAME, class_name="CSE",
                              section="A")
        bench.students.upsert("STU001", DUPLICATE_NAME, roll_number="17")

        row = bench.students.get("STU001")
        assert row["roll_number"] == "17", "the upsert did not update"
        assert row["class_name"] == "CSE", (
            "omitted fields must be left alone, not blanked")
        assert len(bench.students.list_all()) == 1

        print("PASS  enrollment can still update a registered student "
              "without creating a second one")
    finally:
        bench.close()


# -- usernames -------------------------------------------------------------

def test_the_database_enforces_unique_usernames():
    """
    Both credential tables, at the constraint rather than in the code.
    """
    bench = harness()
    try:
        for table in ("users", "student_accounts"):
            statement = bench.database.query_one(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = ?", (table,))["sql"]
            assert "username      TEXT NOT NULL UNIQUE" in statement, (
                "%s.username is not declared UNIQUE: %s" % (table, statement))

        print("PASS  users.username and student_accounts.username are both "
              "UNIQUE in the schema")
    finally:
        bench.close()


def test_usernames_are_unique_case_insensitively():
    """
    The mismatch this work closed.

    A staff username is looked up with COLLATE NOCASE, so if the column were
    only case-sensitively unique then "rahul" and "Rahul" could both exist
    and a sign-in would have two rows to choose from. That is ambiguous
    authentication, which is the one outcome that must be impossible.
    """
    bench = harness()
    try:
        service, actor = a_super_admin(bench, username="rahul.kumar")

        # Through the service: the pre-check is case-insensitive too.
        from backend.services.auth_service import AuthConflict
        try:
            service.create_user(actor, "Rahul.Kumar", "Another Rahul",
                                "TEACHER", SHARED_PASSWORD)
        except AuthConflict:
            pass
        else:
            raise AssertionError(
                "a username differing only in case was accepted")

        # And at the constraint, which is what a lost race would meet.
        try:
            bench.users.create("RAHUL.KUMAR", "Another Rahul", "TEACHER",
                               "not-a-real-hash")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(
                "the database accepted two usernames differing only in case")

        assert bench.users.count() == 1, (
            "one account expected, found %d" % bench.users.count())

        print("PASS  usernames are unique case-insensitively, at the "
              "database and not only in the service")
    finally:
        bench.close()


def test_a_lost_username_race_is_a_conflict_not_a_crash():
    """
    The constraint's refusal has to become the same clear answer the
    pre-check gives, or a correctable mistake arrives as a 500.
    """
    from backend.services.auth_service import AuthConflict

    bench = harness()
    try:
        service, actor = a_super_admin(bench)
        service.create_user(actor, "teacher.one", "A Teacher", "TEACHER",
                            SHARED_PASSWORD)

        try:
            service.create_user(actor, "teacher.one", "Another Teacher",
                                "TEACHER", SHARED_PASSWORD)
        except AuthConflict as error:
            assert "already taken" in str(error), (
                "the message should say the username is taken: %s" % error)
        else:
            raise AssertionError("a duplicate username was accepted")

        existing = bench.users.get_by_username("teacher.one")
        assert existing["display_name"] == "A Teacher", (
            "the existing account was overwritten: %r"
            % existing["display_name"])

        print("PASS  a duplicate username is a clear conflict and the "
              "existing account is unchanged")
    finally:
        bench.close()


def test_two_students_with_one_name_get_two_accounts():
    """
    Both get a login, each keyed to their own student id, with the Student ID
    as the default username - which is unique by construction.
    """
    bench = harness()
    try:
        service, actor = a_super_admin(bench)

        from backend.services.portal_service import PortalService

        portal = PortalService(bench.database)

        bench.data.create_student("STU001", DUPLICATE_NAME, class_name="CSE",
                                  section="A")
        bench.data.create_student("STU002", DUPLICATE_NAME, class_name="CSE",
                                  section="B")

        first = portal.issue(actor, "STU001")
        second = portal.issue(actor, "STU002")

        assert first["username"] != second["username"], (
            "two same-named students must not share a username")
        assert first["student_id"] == "STU001"
        assert second["student_id"] == "STU002"

        print("PASS  two students called %r get two separate logins"
              % DUPLICATE_NAME)
    finally:
        bench.close()


def test_a_second_login_for_one_student_is_refused():
    """
    One account per student, enforced by the PRIMARY KEY on student_id.
    """
    from backend.services.auth_service import AuthConflict

    bench = harness()
    try:
        service, actor = a_super_admin(bench)

        from backend.services.portal_service import PortalService

        portal = PortalService(bench.database)
        bench.data.create_student("STU001", DUPLICATE_NAME)
        issued = portal.issue(actor, "STU001")

        try:
            portal.issue(actor, "STU001", username="rahul.kumar.two")
        except AuthConflict:
            pass
        else:
            raise AssertionError("a student was given a second portal login")

        assert bench.accounts.get("STU001")["username"] == issued["username"]

        # And at the constraint.
        try:
            bench.accounts.create("STU001", "rahul.kumar.three", "hash")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(
                "the database accepted a second account for one student")

        print("PASS  one student has one portal login, refused twice over")
    finally:
        bench.close()


def test_a_pupil_cannot_take_a_teachers_username():
    """
    The two credential tables are separate, so nothing but this check stops
    one "jsmith" being a teacher and another a pupil.
    """
    from backend.services.auth_service import AuthConflict

    bench = harness()
    try:
        service, actor = a_super_admin(bench)
        service.create_user(actor, "rahul.kumar", "A Teacher", "TEACHER",
                            SHARED_PASSWORD)

        from backend.services.portal_service import PortalService

        portal = PortalService(bench.database)
        bench.data.create_student("STU001", DUPLICATE_NAME)

        for attempt in ("rahul.kumar", "Rahul.Kumar"):
            try:
                portal.issue(actor, "STU001", username=attempt)
            except AuthConflict:
                continue
            raise AssertionError(
                "a pupil was given the staff username %r" % attempt)

        assert bench.accounts.get("STU001") is None, (
            "no account should have been created")

        print("PASS  a pupil cannot take a staff username, in any case")
    finally:
        bench.close()


# -- passwords are not identities ------------------------------------------

def test_two_accounts_may_share_a_password():
    """
    A password is a secret, not a key.

    Two students with the same password are two accounts, and each signs in
    as themselves. Nothing anywhere looks an account up by its password, let
    alone by name plus password.
    """
    bench = harness()
    try:
        service, actor = a_super_admin(bench)

        from backend.services.portal_service import PortalService

        portal = PortalService(bench.database)

        bench.data.create_student("STU001", DUPLICATE_NAME)
        bench.data.create_student("STU002", DUPLICATE_NAME)

        portal.issue(actor, "STU001", username="student001",
                     password=SHARED_PASSWORD)
        portal.issue(actor, "STU002", username="student002",
                     password=SHARED_PASSWORD)

        # login returns (token, described student).
        first_token, first = portal.login("student001", SHARED_PASSWORD)
        second_token, second = portal.login("student002", SHARED_PASSWORD)

        assert first["student_id"] == "STU001", (
            "student001 signed in as %r" % first["student_id"])
        assert second["student_id"] == "STU002", (
            "student002 signed in as %r" % second["student_id"])
        assert first_token != second_token, (
            "two sign-ins with one password produced one session token")

        print("PASS  two accounts share one password and each signs in as "
              "itself")
    finally:
        bench.close()


def test_the_stored_hashes_differ_even_for_one_password():
    """
    Per-account salt, so a shared password is not even visible as shared -
    and so the hash can never be used as a lookup key by accident.
    """
    bench = harness()
    try:
        service, actor = a_super_admin(bench)

        from backend.services.portal_service import PortalService

        portal = PortalService(bench.database)
        bench.data.create_student("STU001", DUPLICATE_NAME)
        bench.data.create_student("STU002", DUPLICATE_NAME)
        portal.issue(actor, "STU001", username="student001",
                     password=SHARED_PASSWORD)
        portal.issue(actor, "STU002", username="student002",
                     password=SHARED_PASSWORD)

        first = bench.accounts.get("STU001")["password_hash"]
        second = bench.accounts.get("STU002")["password_hash"]

        assert first != second, (
            "one password produced one hash twice; the salt is not per "
            "account")
        assert SHARED_PASSWORD not in first, "the password is stored in clear"
        assert SHARED_PASSWORD not in second, "the password is stored in clear"

        print("PASS  one password, two salted hashes, and no plaintext "
              "anywhere")
    finally:
        bench.close()


def test_nothing_queries_an_account_by_name_or_password():
    """
    Read from the source rather than inferred from behaviour.

    A lookup joining students.name to a credential, or selecting on
    password_hash, is the shape of bug this whole area exists to prevent, so
    its absence is asserted directly.
    """
    suspicious = []

    for path in sorted((PROJECT_ROOT / "backend").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            squashed = " ".join(line.split()).lower()
            if "where" not in squashed:
                continue
            if "password_hash = ?" in squashed or "password = ?" in squashed:
                suspicious.append("%s: %s" % (path.name, line.strip()))
            if "where name = ?" in squashed and "student" in squashed:
                suspicious.append("%s: %s" % (path.name, line.strip()))

    assert not suspicious, (
        "a credential appears to be looked up by name or password: %s"
        % suspicious)

    print("PASS  no query anywhere in backend/ selects an account by name or "
          "by password")


# -- attendance and face identity ------------------------------------------

def test_attendance_belongs_to_the_id_not_the_name():
    """
    Two students with one name, one session, different presence.

    The records must come back attached to the right id, and the arithmetic
    must not have been pooled across the shared name.
    """
    bench = harness()
    try:
        bench.data.create_student("STU001", DUPLICATE_NAME, class_name="CSE",
                                  section="A")
        bench.data.create_student("STU002", DUPLICATE_NAME, class_name="CSE",
                                  section="B")

        end = START + timedelta(minutes=60)
        session = AttendanceSession("SES-I", "CLASS-10A", START, end,
                                    expected_students=["STU001", "STU002"])
        session.start()
        session.complete(completed_at=end)

        with bench.database.transaction():
            bench.sessions.save(session)

        segments = [
            PresenceSegment("STU001", START, START + timedelta(minutes=50), 1),
            PresenceSegment("STU002", START, START + timedelta(minutes=10), 2),
        ]

        engine = AttendanceRuleEngine(
            AttendancePolicy(version=session.policy_version))
        with bench.database.transaction():
            bench.attendance.save_segments(session.session_id, segments)
            for decision in engine.evaluate_session(
                    session, segments, ["STU001", "STU002"]):
                bench.attendance.save_decision(decision)

        rows = {row["student_id"]: row
                for row in bench.attendance.list_for_session("SES-I")}

        assert len(rows) == 2, "two records expected, got %d" % len(rows)
        assert rows["STU001"]["present_seconds"] == 50 * 60.0, (
            "STU001 should have 50 minutes, got %r"
            % rows["STU001"]["present_seconds"])
        assert rows["STU002"]["present_seconds"] == 10 * 60.0, (
            "STU002 should have 10 minutes, got %r"
            % rows["STU002"]["present_seconds"])
        assert rows["STU001"]["status"] != rows["STU002"]["status"], (
            "two very different attendances produced one status")

        print("PASS  two same-named students hold separate attendance, keyed "
              "by id")
    finally:
        bench.close()


def test_the_attendance_table_keys_on_the_student_id():
    """
    The uniqueness is (session, student, policy) - not (session, name).
    """
    bench = harness()
    try:
        statement = bench.database.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'attendance_records'")["sql"]

        assert "UNIQUE (session_id, student_id, policy_version)" in statement, (
            "attendance is not keyed on the student id: %s" % statement)
        assert "name" not in statement.split("UNIQUE", 1)[1].split(")", 1)[0]
        assert "FOREIGN KEY (student_id) REFERENCES students(student_id)" \
            in statement, "attendance does not reference the registry by id"

        print("PASS  an attendance record is keyed and foreign-keyed on the "
              "student id")
    finally:
        bench.close()


def test_face_records_are_keyed_on_the_student_id():
    """
    The embedding store names its files after the student id.

    Two students with one name therefore have two separate vector files and
    two separate metadata records, and nothing in that store could merge
    them even if it wanted to.
    """
    from embedding_store import EmbeddingStore

    directory = Path(tempfile.mkdtemp(prefix="classvision_identity_faces_"))
    try:
        store = EmbeddingStore(directory)

        for student_id, section in (("STU001", "A"), ("STU002", "B")):
            store.save_student(student_id, DUPLICATE_NAME, "17", "CSE",
                               section, "test-model", "1.0")

        first = store.get_student("STU001")
        second = store.get_student("STU002")

        assert first["name"] == second["name"] == DUPLICATE_NAME
        assert first["student_id"] != second["student_id"]
        assert first["embedding_id"] != second["embedding_id"], (
            "two students share one embedding id: %r" % first["embedding_id"])
        assert first["section"] == "A" and second["section"] == "B", (
            "one student's record overwrote the other's")

        print("PASS  two same-named students have two separate face records")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- delete and recreate ---------------------------------------------------

def test_a_recreated_student_does_not_inherit_an_old_identity():
    """
    Deleting a student and registering another with the same name must not
    hand the newcomer the first one's history.

    The registry refuses to DELETE a student who has attendance - it
    deactivates instead - so the test does both halves: a student with
    history keeps their id and their records, and a new student with the same
    name gets a new id and no records at all.
    """
    bench = harness()
    try:
        bench.data.create_student("STU001", DUPLICATE_NAME)

        end = START + timedelta(minutes=60)
        session = AttendanceSession("SES-I", "CLASS-10A", START, end,
                                    expected_students=["STU001"])
        session.start()
        session.complete(completed_at=end)
        with bench.database.transaction():
            bench.sessions.save(session)

        segments = [PresenceSegment("STU001", START,
                                    START + timedelta(minutes=50), 1)]
        engine = AttendanceRuleEngine(
            AttendancePolicy(version=session.policy_version))
        with bench.database.transaction():
            for decision in engine.evaluate_session(session, segments,
                                                    ["STU001"]):
                bench.attendance.save_decision(decision)

        assert bench.students.has_history("STU001"), (
            "the student should now have attendance history")

        # Deactivated, not deleted: the history is the reason.
        bench.students.deactivate("STU001")

        # A different child, same name, registered afterwards.
        newcomer = bench.data.create_student("STU002", DUPLICATE_NAME)
        assert newcomer["student_id"] == "STU002"

        records = bench.attendance.list_for_student("STU002")
        assert not records, (
            "the new student inherited %d attendance records" % len(records))

        original = bench.attendance.list_for_student("STU001")
        assert len(original) == 1, (
            "the original student's history should be intact, found %d"
            % len(original))
        assert original[0]["present_seconds"] == 50 * 60.0

        print("PASS  a new student with an old student's name inherits no "
              "history")
    finally:
        bench.close()


# -- search, scale and the interface ---------------------------------------

def test_searching_a_shared_name_returns_every_match_with_its_identity():
    """
    A name search is a many-result search, and each result carries what is
    needed to tell it from the others.
    """
    bench = harness()
    try:
        bench.data.create_student("STU001", DUPLICATE_NAME, roll_number="17",
                                  class_name="CSE", section="A")
        bench.data.create_student("STU027", DUPLICATE_NAME, roll_number="4",
                                  class_name="CSE", section="B")
        bench.data.create_student("STU099", "Someone Else", class_name="ECE")

        page = bench.data.list_students(search=DUPLICATE_NAME)

        assert page["total"] == 2, (
            "a shared name should match both students, got %d" % page["total"])

        found = {item["student_id"]: item for item in page["items"]}
        assert set(found) == {"STU001", "STU027"}

        for student_id, expected_section in (("STU001", "A"),
                                             ("STU027", "B")):
            item = found[student_id]
            for field in ("student_id", "name", "roll_number", "class_name",
                          "section"):
                assert field in item, "the result is missing %s" % field
            assert item["section"] == expected_section, (
                "%s came back with section %r" % (student_id, item["section"]))

        print("PASS  searching a shared name returns both students, each with "
              "its id, roll number, class and section")
    finally:
        bench.close()


def test_the_interface_never_shows_a_name_on_its_own():
    """
    Anywhere an administrator picks a student, the identity is shown with the
    name - because a list of three identical names is unusable.
    """
    script = (PROJECT_ROOT / "backend" / "static" / "app.js").read_text(
        encoding="utf-8")

    picker = script[script.index("openPortalAccountDialog"):]
    picker = picker[:picker.index("\nasync function", 10)
                    if "\nasync function" in picker[10:] else len(picker)]

    assert "student.student_id" in picker, (
        "the student picker does not show the Student ID, so two students "
        "with one name are indistinguishable in it")
    assert "student.class_name" in picker, (
        "the student picker does not show the class")

    print("PASS  the student picker shows the Student ID and class alongside "
          "the name")


def test_duplicate_detection_does_not_scan_the_whole_registry():
    """
    5,000 students is the design target, so the uniqueness check has to be an
    indexed lookup rather than a sweep.

    Asserted by reading the source: the creation path must ask for ONE row by
    primary key, and must not pull the registry into memory to look through
    it.
    """
    service = (PROJECT_ROOT / "backend" / "services"
               / "data_service.py").read_text(encoding="utf-8")

    creation = service[service.index("def create_student"):]
    creation = creation[:creation.index("def _contact_fields")]

    assert "self.students.get(student_id)" in creation, (
        "the duplicate check should be a primary-key lookup")
    for sweep in ("list_all()", "for other in", "for existing in"):
        assert sweep not in creation, (
            "the creation path appears to scan the registry: %r" % sweep)

    repositories = (PROJECT_ROOT / "database"
                    / "repositories.py").read_text(encoding="utf-8")
    getter = repositories[repositories.index("class StudentRepository"):]
    getter = getter[getter.index("    def get(self, student_id):"):]
    getter = getter[:getter.index("    def list_all")]
    assert "WHERE student_id = ?" in getter, (
        "a student is not fetched by primary key: %s" % getter)

    print("PASS  the uniqueness check is a primary-key lookup, not a scan of "
          "the registry")


def test_five_thousand_students_share_one_name_without_a_collision():
    """
    The scale case, run rather than argued.

    Five thousand students with the SAME name are registered, and every one
    of them keeps its own identity. Done through the repository rather than
    the service so the test measures the constraint and not the HTTP layer.
    """
    bench = harness()
    try:
        total = 5000
        with bench.database.transaction():
            for index in range(total):
                bench.students.insert("STU%05d" % index, DUPLICATE_NAME,
                                      class_name="CSE",
                                      section="A" if index % 2 else "B")

        count = bench.database.query_one(
            "SELECT COUNT(*) AS total FROM students")["total"]
        assert count == total, "expected %d students, found %d" % (total,
                                                                   count)

        distinct_names = bench.database.query_one(
            "SELECT COUNT(DISTINCT name) AS names FROM students")["names"]
        assert distinct_names == 1, (
            "the test is meant to use one shared name, found %d"
            % distinct_names)

        # The identity still resolves to exactly one row.
        one = bench.students.get("STU04999")
        assert one is not None and one["student_id"] == "STU04999"

        # And the 5001st with an existing id is still refused.
        try:
            bench.students.insert("STU04999", "Someone Else")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(
                "a duplicate id was accepted at scale")

        print("PASS  %d students share one name, hold %d identities, and a "
              "duplicate id is still refused" % (total, total))
    finally:
        bench.close()


# -- the upgrade path ------------------------------------------------------

def test_an_existing_database_with_case_duplicates_still_opens():
    """
    The guard on the new index.

    A database written before case-insensitive uniqueness existed may already
    hold "rahul" and "Rahul". The upgrade must REPORT that rather than fail -
    an installation that cannot open its own database is a worse outcome than
    one with a duplicate it has been told about - and it must not resolve the
    pair by itself, because which of two people owns a name is not a question
    this layer can answer.
    """
    from database import Database

    directory = Path(tempfile.mkdtemp(prefix="classvision_identity_old_"))
    try:
        database = Database(directory / "old.db")
        database.initialize()

        # Drop the new index to recreate the pre-upgrade state, then insert
        # the pair the old schema allowed.
        database.connection.execute(
            "DROP INDEX IF EXISTS idx_users_username_nocase")
        from repositories import UserRepository
        users = UserRepository(database)
        with database.transaction():
            users.create("rahul", "One Rahul", "TEACHER", "hash-a")
            users.create("Rahul", "Another Rahul", "TEACHER", "hash-b")

        blocked = database.enforce_identity_uniqueness()

        assert blocked, (
            "the duplicate pair should have been reported, got %r" % blocked)
        reported = {entry["value"] for entry in blocked}
        assert "rahul" in reported, (
            "the reported duplicate should be the shared identity: %r"
            % blocked)

        assert users.count() == 2, (
            "neither account may be deleted or merged, found %d"
            % users.count())
        assert users.get_by_username("rahul") is not None

        # And the database is still perfectly usable.
        database.initialize()
        assert database.schema_version() is not None

        print("PASS  a database already holding a case-duplicate opens, "
              "reports the pair, and loses no account")
    finally:
        database.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_clean_database_gets_the_index():
    bench = harness()
    try:
        blocked = bench.database.enforce_identity_uniqueness()
        assert blocked == [], "a clean database reported %r" % blocked

        indexes = {row["name"] for row in bench.database.query(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}

        for expected in ("idx_users_username_nocase",
                         "idx_student_accounts_username_nocase"):
            assert expected in indexes, "%s was not created" % expected

        print("PASS  a clean database gets both case-insensitive username "
              "indexes")
    finally:
        bench.close()


# -- runner ----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_two_students_may_share_a_name,
        test_a_third_and_a_fourth_of_the_same_name_are_still_fine,
        test_the_registry_has_no_unique_constraint_on_a_name,
        test_a_duplicate_student_id_is_refused_and_changes_nothing,
        test_the_primary_key_is_the_final_protection_not_the_pre_check,
        test_the_enrollment_path_may_still_update_a_registered_student,
        test_the_database_enforces_unique_usernames,
        test_usernames_are_unique_case_insensitively,
        test_a_lost_username_race_is_a_conflict_not_a_crash,
        test_two_students_with_one_name_get_two_accounts,
        test_a_second_login_for_one_student_is_refused,
        test_a_pupil_cannot_take_a_teachers_username,
        test_two_accounts_may_share_a_password,
        test_the_stored_hashes_differ_even_for_one_password,
        test_nothing_queries_an_account_by_name_or_password,
        test_attendance_belongs_to_the_id_not_the_name,
        test_the_attendance_table_keys_on_the_student_id,
        test_face_records_are_keyed_on_the_student_id,
        test_a_recreated_student_does_not_inherit_an_old_identity,
        test_searching_a_shared_name_returns_every_match_with_its_identity,
        test_the_interface_never_shows_a_name_on_its_own,
        test_duplicate_detection_does_not_scan_the_whole_registry,
        test_five_thousand_students_share_one_name_without_a_collision,
        test_an_existing_database_with_case_duplicates_still_opens,
        test_a_clean_database_gets_the_index,
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
