"""
ClassVision AI - Data Service (Step 7)
--------------------------------------
Reads sessions, students, attendance and presence for the dashboard.

Sits between the HTTP routes and the Step 6 repositories:

    route -> service -> repository -> SQLite

Routes never write SQL, and the frontend never learns that SQL exists.

Two responsibilities beyond fetching:

    1. Shaping. Database rows carry columns the dashboard has no business
       seeing. Everything returned here is explicitly built field by field,
       so a new database column can never leak into an API response by
       accident.

    2. Refusing. Presence SEGMENTS are returned; embeddings, images and
       frames are not stored here at all and are never constructed.
"""

import sqlite3
from datetime import datetime, timezone

from attendance_rules import grace_minutes_for_duration, grace_seconds_for_duration
from attendance_session import SESSION_SCHEDULED, AttendanceSession
from database import Database
from repositories import (
    STUDENT_ACTIVE,
    STUDENT_CONTACT_FIELDS,
    STUDENT_INACTIVE,
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

# How many rows a list endpoint returns when the caller does not say.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# The longest session anybody could plausibly mean. Past this it is a
# data-entry slip - a wrong date, usually - and a presence RATIO computed over
# several days would be meaningless rather than merely odd.
MAX_SESSION_HOURS = 24

# The smallest. A zero-length session is refused by the Step 6 model already;
# this catches the almost-zero case that would make every ratio nonsense.
MIN_SESSION_SECONDS = 60

# What counts as having attended. Imported rather than restated so the
# Students page and the analytics pages cannot come to disagree about what a
# student's attendance rate means.
from backend.services.analytics_service import SATISFYING_STATUSES  # noqa: E402

# Every status a summary reports a count for, in the order a teacher reads
# them. UNKNOWN is included because a session that produced no decision for a
# student is a fact worth showing, not a zero.
SUMMARY_STATUSES = ("PRESENT", "LATE", "PARTIAL", "LEFT_EARLY", "ABSENT",
                    "UNKNOWN", "MANUAL_OVERRIDE")


class StudentValidationError(ValueError):
    """The request is not a usable student. Safe to show a teacher."""


class StudentConflictError(ValueError):
    """Well formed, but it collides with a student that already exists."""


def _clean(value, limit=200):
    return ("" if value is None else str(value).strip())[:limit]


class SessionValidationError(ValueError):
    """The request is malformed. Every message is safe to show a teacher."""


class SessionConflictError(ValueError):
    """The request is well formed but collides with a session that exists."""


def parse_timestamp(value, label):
    """
    An ISO 8601 timestamp that carries a timezone, or a clear refusal.

    A naive timestamp is rejected rather than assumed to be UTC. "09:00" means
    nothing without an offset, and quietly guessing one is how a session ends
    up an hour away from the lesson it is supposed to describe.
    """
    if not isinstance(value, str) or not value.strip():
        raise SessionValidationError("%s is required" % label)

    text = value.strip()
    # Python 3.10's fromisoformat does not accept the "Z" suffix that browsers
    # produce, so translate it rather than refusing valid input.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise SessionValidationError(
            "%s is not a valid date and time. Expected a format like "
            "2026-09-24T09:00:00+00:00." % label)

    if moment.tzinfo is None:
        raise SessionValidationError(
            "%s has no timezone offset. Send an offset (for example "
            "+00:00) so the time is unambiguous." % label)

    return moment.astimezone(timezone.utc)


def generate_session_id(class_id, start_time):
    """
    A readable, sortable id, in the shape the project already uses.

    The start time is part of it because a class legitimately meets twice in
    one day; without it the afternoon session would collide with the morning
    one and be refused for no good reason.
    """
    slug = "".join(character if character.isalnum() else "-"
                   for character in class_id.upper()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")

    return "SES-%s-%s-%s" % (start_time.strftime("%Y-%m-%d"),
                             start_time.strftime("%H%M"),
                             slug or "CLASS")


def _principal_restricts(principal):
    """
    Whether this caller's class scope actually removes any student.

    Three callers remove nothing - no principal at all, an unrestricted one,
    and a teacher holding the can_view_all_students grant - and for all three
    may_see_student() answers True for every row, so the Python pass over the
    whole table is a no-op and skipping it cannot change an answer. Only a
    genuinely scoped teacher needs the rows read in full, and that is the one
    case this returns True for.
    """
    if principal is None:
        return False
    if getattr(principal, "unrestricted", True):
        return False
    if getattr(principal, "may_view_all_students", False):
        return False
    return True


def _missing_seconds(row):
    """
    How long a student was not present for, from a stored attendance row.

    The same subtraction the rule engine does, over the same two stored
    columns, so the API cannot report a different absence from the one the
    decision was made against. Clamped at zero: presence is clipped to the
    session window before it is ever summed, so a negative would be a bug
    rather than a student who attended more than the whole class.
    """
    return max(0.0, (row["session_seconds"] or 0.0)
               - (row["present_seconds"] or 0.0))


class DataService:
    """Read access to everything the dashboard shows."""

    def __init__(self, database):
        self.database = database
        self.students = StudentRepository(database)
        self.sessions = SessionRepository(database)
        self.attendance = AttendanceRepository(database)

    # -- health ------------------------------------------------------------

    def health(self):
        """
        Is the service usable?

        Reports whether the database answers and whether the schema is
        present. Deliberately reveals no file path, credential or setting.
        """
        try:
            tables = self.database.table_names()
            self.database.query_one("SELECT 1 AS ok")
            schema_ready = "attendance_records" in tables
            return {
                "status": "ok" if schema_ready else "degraded",
                "database": "ok",
                "schema": "ready" if schema_ready else "missing",
            }
        except Exception:
            # The reason is deliberately not echoed to the client.
            return {"status": "error", "database": "error", "schema": "unknown"}

    # -- classes -----------------------------------------------------------

    def list_classes(self, principal=None):
        """
        Every class that has sessions, narrowed to the caller's own.

        The class list feeds the dashboard's filter dropdown, so leaving it
        unscoped told a teacher the names of classes the Sessions page would
        then refuse to show them - and named every class in the school while
        doing it.
        """
        rows = self.database.query(
            """
            SELECT class_id,
                   COUNT(*) AS session_count,
                   SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END)
                       AS completed_sessions,
                   MIN(session_date) AS first_session,
                   MAX(session_date) AS last_session
              FROM attendance_sessions
             GROUP BY class_id
             ORDER BY class_id
            """)
        classes = [dict(row) for row in rows]

        if principal is not None and not principal.unrestricted:
            classes = [item for item in classes
                       if principal.may_see_class_id(item["class_id"])]

        return classes

    # -- sessions ----------------------------------------------------------

    def list_sessions(self, class_id=None, session_date=None, status=None,
                      principal=None, limit=DEFAULT_PAGE_SIZE, offset=0):
        """
        Sessions, newest first, narrowed to what the caller may see.

        `principal` is the class-scope filter, and it is applied HERE rather
        than in the route because a route that pages first and filters second
        returns short pages and a wrong total. This endpoint used to take no
        principal at all, which meant a teacher assigned to one class could
        list - and open - every register in the school.

        The filter cannot be pushed into SQL. A session carries a free-text
        class_id ("CSE(AI)-C", "cse(ai) c") and an assignment carries class and
        section as separate columns; the two are reconciled by normalising both
        sides, which is Python, not a comparison SQLite can index. So the rows
        are read, filtered, and only then paged - exactly as list_students
        already does it, and for the same reason.

        An unrestricted caller skips the filter entirely and pages in SQL, so
        a Super Admin's Sessions page does not start reading the whole table
        the day a school has ten thousand lessons in it.
        """
        clauses = []
        parameters = []

        if class_id:
            clauses.append("class_id = ?")
            parameters.append(class_id)
        if session_date:
            clauses.append("session_date = ?")
            parameters.append(session_date)
        if status:
            clauses.append("status = ?")
            parameters.append(status)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        scoped = principal is not None and not principal.unrestricted

        select = """
            SELECT s.*,
                   (SELECT COUNT(*) FROM session_expected_students e
                     WHERE e.session_id = s.session_id) AS expected_students
              FROM attendance_sessions s
              %s
             ORDER BY s.session_date DESC, s.start_time DESC
            """ % where

        if not scoped:
            total = self.database.query_one(
                "SELECT COUNT(*) AS total FROM attendance_sessions %s" % where,
                tuple(parameters))["total"]
            rows = self.database.query(select + " LIMIT ? OFFSET ?",
                                       tuple(parameters) + (limit, offset))
            items = [self._session_summary(row) for row in rows]
        else:
            items = [self._session_summary(row)
                     for row in self.database.query(select,
                                                    tuple(parameters))]
            items = [item for item in items
                     if principal.may_see_class_id(item["class_id"])]
            total = len(items)
            items = items[offset:offset + limit]

        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _session_summary(row):
        return {
            "session_id": row["session_id"],
            "class_id": row["class_id"],
            "session_date": row["session_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "timezone": row["timezone"],
            "status": row["status"],
            "policy_version": row["policy_version"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "expected_students": row["expected_students"]
            if "expected_students" in row.keys() else None,
        }

    def get_session(self, session_id):
        row = self.database.query_one(
            "SELECT * FROM attendance_sessions WHERE session_id = ?",
            (session_id,))
        if row is None:
            return None

        summary = self._session_summary(row)
        summary["expected_students"] = self.sessions.expected_students(session_id)
        summary["policy_versions_present"] = \
            self.attendance.policy_versions_for_session(session_id)
        return summary

    # -- attendance --------------------------------------------------------

    def session_attendance(self, session_id, status=None, policy_version=None,
                           sort="student_id", descending=False,
                           limit=DEFAULT_PAGE_SIZE, offset=0):
        """
        The attendance table for one session.

        Sorting and filtering happen in SQL, so the browser never receives
        rows it is not going to show.
        """
        sortable = {
            "student_id": "r.student_id",
            "name": "st.name",
            "status": "r.status",
            "present_seconds": "r.present_seconds",
            "presence_ratio": "r.presence_ratio",
            "first_seen": "r.first_seen",
        }
        # Whitelisted: the column name never comes from user text.
        column = sortable.get(sort, "r.student_id")
        direction = "DESC" if descending else "ASC"

        clauses = ["r.session_id = ?"]
        parameters = [session_id]

        if status:
            clauses.append("r.status = ?")
            parameters.append(status)
        if policy_version:
            clauses.append("r.policy_version = ?")
            parameters.append(policy_version)

        where = " AND ".join(clauses)

        total = self.database.query_one(
            "SELECT COUNT(*) AS total FROM attendance_records r WHERE " + where,
            tuple(parameters))["total"]

        rows = self.database.query(
            """
            SELECT r.*, st.name AS student_name
              FROM attendance_records r
              LEFT JOIN students st ON st.student_id = r.student_id
             WHERE %s
             ORDER BY %s %s, r.student_id ASC
             LIMIT ? OFFSET ?
            """ % (where, column, direction),
            tuple(parameters) + (limit, offset))

        return {
            "items": [self._attendance_record(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _attendance_record(row):
        """
        Build the API shape explicitly.

        Listing every field by hand is the point: a column added to the
        database later cannot appear in an API response unless somebody
        deliberately adds it here.
        """
        return {
            "attendance_id": row["attendance_id"],
            "session_id": row["session_id"],
            "student_id": row["student_id"],
            "student_name": row["student_name"]
            if "student_name" in row.keys() else None,
            "status": row["status"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "present_seconds": row["present_seconds"],
            "session_seconds": row["session_seconds"],
            "presence_ratio": row["presence_ratio"],

            # The duration-based relaxation, DERIVED rather than stored.
            #
            # Both figures follow from session_seconds and present_seconds,
            # which are already on the row, and from the one shared band
            # table in ai/attendance_rules.py - so there is no column to add,
            # no migration to run, and no second copy of the rule that could
            # disagree with the engine that made the decision.
            #
            # missing_seconds is the measurement and is never reduced by the
            # allowance: a student who missed five minutes is reported as
            # having missed five minutes whether or not those five minutes
            # were forgiven.
            "missing_seconds": _missing_seconds(row),
            "grace_seconds": grace_seconds_for_duration(row["session_seconds"]),
            "grace_minutes": grace_minutes_for_duration(
                (row["session_seconds"] or 0.0) / 60.0),
            "within_grace": _missing_seconds(row) > 0
            and _missing_seconds(row)
            <= grace_seconds_for_duration(row["session_seconds"]),
            "segment_count": row["segment_count"],
            "loss_periods": row["loss_periods"],
            "was_late": bool(row["was_late"]),
            "left_early": bool(row["left_early"]),
            "reason": row["reason"],
            "policy_version": row["policy_version"],
            "is_override": bool(row["is_override"]),
            "original_status": row["original_status"],
            "override_reason": row["override_reason"],
            "overridden_by": row["overridden_by"],
            "overridden_at": row["overridden_at"],
        }

    # -- presence ----------------------------------------------------------

    def session_presence(self, session_id, student_id=None):
        """
        The presence segments behind a session's attendance.

        Track ids are included because they are the evidence a teacher may
        need to question a result. They are technical references, never
        identities, and the API labels them as such.
        """
        segments = self.attendance.get_segments(session_id, student_id)
        session = self.database.query_one(
            "SELECT start_time, end_time FROM attendance_sessions "
            "WHERE session_id = ?", (session_id,))

        by_student = {}
        for segment in segments:
            by_student.setdefault(segment["student_id"], []).append({
                "segment_id": segment["segment_id"],
                "track_id": segment["track_id"],
                "start_time": segment["start_time"],
                "end_time": segment["end_time"],
                "duration_seconds": segment["duration_seconds"],
            })

        return {
            "session_id": session_id,
            "session_start": session["start_time"] if session else None,
            "session_end": session["end_time"] if session else None,
            "students": [
                {"student_id": student, "segments": items,
                 "segment_count": len(items),
                 "total_seconds": sum(s["duration_seconds"] for s in items)}
                for student, items in sorted(by_student.items())
            ],
            "note": ("track_id is technical evidence from the tracking layer, "
                     "not a student identity"),
        }

    # -- students ----------------------------------------------------------

    # Listed by hand rather than SELECT *, so a column added to the table
    # later cannot reach an API response until somebody adds it here. That is
    # also why academic_year had to be added in two places to work: the WHERE
    # clause could filter on it from the moment the column existed, but the
    # row could not REPORT it until it was selected.
    STUDENT_COLUMNS = ("student_id, name, roll_number, class_name, section, "
                       "academic_year, status, created_at, updated_at, "
                       "parent_name, parent_phone, parent_email, "
                       "notification_enabled")

    def list_students(self, search=None, status=None, class_name=None,
                      section=None, academic_year=None, enrolled=None,
                      principal=None, limit=DEFAULT_PAGE_SIZE, offset=0,
                      with_attendance=True):
        """
        The Students page, in one round trip.

        Every field a teacher can see comes from this method: the registry
        columns from SQLite, whether face samples exist from the embedding
        store's metadata, and an attendance summary computed from the
        attendance tables. Attendance is NOT copied into the students table -
        it is derived here, every time, from the records that own it.

        The two joins are done for the WHOLE page at once rather than per row.
        Thirty students used to mean thirty summary queries; it now means two,
        which is the difference between a page that opens and a page that
        stalls on a class of forty.
        """
        clauses = []
        parameters = []

        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if search:
            clauses.append("(student_id LIKE ? OR name LIKE ? "
                           "OR roll_number LIKE ?)")
            pattern = "%" + search + "%"
            parameters.extend([pattern, pattern, pattern])
        # Year, class and section are three independent ANDed clauses, which
        # is what makes all eight combinations of them work without eight
        # code paths: each one is added only when it was asked for, so "no
        # filters" is an empty WHERE and "year + class + section" is three
        # conditions over one index.
        #
        # COLLATE NOCASE on all three, matching how class and section already
        # behaved: a teacher typing "cse" is asking about CSE.
        if class_name:
            clauses.append("class_name = ? COLLATE NOCASE")
            parameters.append(class_name)
        if section:
            clauses.append("section = ? COLLATE NOCASE")
            parameters.append(section)
        if academic_year:
            clauses.append("academic_year = ? COLLATE NOCASE")
            parameters.append(academic_year)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        # Which filters cannot be expressed in SQL, because the facts they
        # test do not live in SQLite: `enrolled` is a question for the
        # embedding store, and a teacher's class scope is a question for
        # authz. Both have to be applied to every matching row before the
        # page can be cut, so when either is in play the rows are read in
        # full - which is the behaviour this method has always had.
        #
        # WHEN NEITHER IS IN PLAY, and that is the ordinary case for an
        # administrator browsing the registry, the database does the counting
        # and the paging. At five thousand students that is the difference
        # between reading twenty-five rows and reading five thousand, then
        # building five thousand dictionaries, then throwing away four
        # thousand nine hundred and seventy-five of them.
        out_of_sql = enrolled is not None or _principal_restricts(principal)

        if not out_of_sql:
            total = self.database.query_one(
                "SELECT COUNT(*) AS total FROM students %s" % where,
                tuple(parameters))["total"]

            rows = self.database.query(
                "SELECT %s FROM students %s ORDER BY student_id "
                "LIMIT ? OFFSET ?"
                % (self.STUDENT_COLUMNS, where),
                tuple(parameters) + (limit, offset))

            metadata = _enrollment_metadata()
            page = [self._student_record(row, metadata) for row in rows]

            if with_attendance and page:
                self._attach_attendance(page)

            return {"items": page, "total": total, "limit": limit,
                    "offset": offset}

        rows = self.database.query(
            "SELECT %s FROM students %s ORDER BY student_id"
            % (self.STUDENT_COLUMNS, where), tuple(parameters))

        metadata = _enrollment_metadata()
        records = [self._student_record(row, metadata) for row in rows]

        # Applied before paging, so the total is the count of what the
        # teacher asked for rather than the count before their scope applied.
        if enrolled is not None:
            records = [record for record in records
                       if record["enrolled"] is bool(enrolled)]

        if principal is not None and not principal.unrestricted:
            records = [record for record in records
                       if principal.may_see_student(record)]

        total = len(records)
        page = records[offset:offset + limit]

        if with_attendance and page:
            self._attach_attendance(page)

        return {"items": page, "total": total, "limit": limit,
                "offset": offset}

    def student_filter_options(self, academic_year=None, class_name=None,
                               section=None, principal=None):
        """
        The years, classes and sections that registered students actually have.

        Every list comes from the registry rather than from a constant, so a
        school sees its own terminology and a year nobody is in never
        appears. A project that has never created a term gets an empty
        `years` list, and the interface then has no year to offer - which is
        the honest answer and the same one the filter would give.

        DEPENDENT, in the one direction that is useful: each list is narrowed
        by the OTHER two selections and never by its own. Choosing
        3rd Year + CSE leaves Section offering only the sections that pair
        actually has, while the Year list still shows every year - a list
        narrowed by its own current value would collapse to one option the
        moment somebody used it.

        ONE query, and it reads GROUPINGS rather than students: a school with
        five thousand students has a few dozen distinct year/class/section
        triples, so this is bounded by the timetable and not by the roll. The
        class-scope filter is then applied to those few dozen rows through
        the same may_see_student() the row listing uses, which is what stops
        a dropdown offering a class whose students the next request would
        refuse to return.
        """
        triples = self.database.query(
            """
            SELECT academic_year, class_name, section
              FROM students
             GROUP BY academic_year COLLATE NOCASE,
                      class_name COLLATE NOCASE,
                      section COLLATE NOCASE
            """)

        visible = []
        for row in triples:
            group = {"academic_year": row["academic_year"],
                     "class_name": row["class_name"],
                     "section": row["section"]}
            if principal is not None and not principal.may_see_student(group):
                continue
            visible.append(group)

        def matches(group, ignore):
            for field, chosen in (("academic_year", academic_year),
                                  ("class_name", class_name),
                                  ("section", section)):
                if field == ignore or not chosen:
                    continue
                if (group[field] or "").casefold() != chosen.casefold():
                    return False
            return True

        def options(field):
            found = {}
            for group in visible:
                if not matches(group, field):
                    continue
                value = group[field] or ""
                if value:
                    found.setdefault(value.casefold(), value)
            return [found[key] for key in sorted(found)]

        return {
            "years": options("academic_year"),
            "classes": options("class_name"),
            "sections": options("section"),
        }

    @staticmethod
    def _student_record(row, metadata=None):
        """
        One student, built field by field.

        SQLite is the source of truth for WHO a student is - name, roll,
        class, section, status. The embedding store answers one further
        question and only one: does a face enrollment exist for this id, and
        how many samples does it hold. It is read as METADATA; the vectors sit
        in a separate file per student that nothing on this path opens.

        Before schema version 2 the roll, class and section came from the
        store as well, which meant the registry could not answer a question
        about its own students. They now come from the row, and the store's
        copy is kept in step rather than relied upon.
        """
        if metadata is None:
            metadata = _enrollment_metadata()

        enrollment = metadata.get(row["student_id"], {})
        keys = row.keys()

        record = {
            "student_id": row["student_id"],
            "name": row["name"],
            "roll_number": row["roll_number"] if "roll_number" in keys else "",
            "class_name": row["class_name"] if "class_name" in keys else "",
            "section": row["section"] if "section" in keys else "",
            # The current academic year, mirrored onto the row from
            # student_academic_history. '' means no term has been recorded
            # for this student, which is a real state and not a missing value.
            "academic_year": (row["academic_year"]
                              if "academic_year" in keys else ""),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],

            # -- parent/guardian contact ------------------------------------
            #
            # Contact detail, not biometric data, and the only reason an
            # attendance alert has anywhere to go. The ROUTE decides who is
            # allowed to see the phone number and the address; this method
            # reports what the row holds.
            "parent_name": row["parent_name"] if "parent_name" in keys else "",
            "parent_phone": (row["parent_phone"] if "parent_phone" in keys
                             else ""),
            "parent_email": (row["parent_email"] if "parent_email" in keys
                             else ""),
            "notification_enabled": bool(
                row["notification_enabled"]
                if "notification_enabled" in keys else True),

            # -- face enrollment, from the embedding store's metadata --------
            "enrolled": bool(enrollment.get("enrolled")),
            "sample_count": int(enrollment.get("sample_count") or 0),
            "enrolled_at": enrollment.get("enrolled_at"),
            "model_name": enrollment.get("model_name"),
            "model_version": enrollment.get("model_version"),

            # Filled in by _attach_attendance. Never stored on the row.
            "attendance": None,
        }
        return record

    def _attach_attendance(self, records):
        """
        Add an attendance summary to each student in a page. Two queries.

        Derived, never duplicated: nothing here writes to the students table,
        and a student who is later deactivated keeps every record, because the
        summary is a question asked of attendance_records rather than a number
        copied out of them.
        """
        student_ids = [record["student_id"] for record in records]
        summaries = self.attendance_summaries(student_ids)

        for record in records:
            record["attendance"] = summaries.get(
                record["student_id"], _empty_attendance())

    def attendance_summaries(self, student_ids):
        """
        Attendance counts for several students at once, keyed by student_id.

        Only the LATEST record per (session, student) is counted. A session
        re-evaluated under a new policy keeps both rows - that is the whole
        point of the policy_version column - but counting both would report a
        student as having attended one session twice.
        """
        if not student_ids:
            return {}

        placeholders = ", ".join("?" for _ in student_ids)
        parameters = tuple(student_ids)

        status_sums = ", ".join(
            "SUM(CASE WHEN r.status = '%s' THEN 1 ELSE 0 END) AS count_%s"
            % (status, status.lower()) for status in SUMMARY_STATUSES)

        satisfying = ", ".join("'%s'" % status for status in SATISFYING_STATUSES)

        rows = self.database.query(
            """
            SELECT r.student_id,
                   COUNT(*) AS recorded_sessions,
                   SUM(CASE WHEN r.status IN (%s) THEN 1 ELSE 0 END)
                       AS sessions_attended,
                   AVG(r.presence_ratio) AS average_presence_ratio,
                   SUM(r.present_seconds) AS total_present_seconds,
                   %s
              FROM attendance_records r
              JOIN (SELECT session_id, student_id,
                           MAX(attendance_id) AS attendance_id
                      FROM attendance_records
                     GROUP BY session_id, student_id) latest
                ON latest.attendance_id = r.attendance_id
             WHERE r.student_id IN (%s)
             GROUP BY r.student_id
            """ % (satisfying, status_sums, placeholders), parameters)

        expected = self.database.query(
            """
            SELECT e.student_id, COUNT(*) AS expected_sessions
              FROM session_expected_students e
              JOIN attendance_sessions s ON s.session_id = e.session_id
             WHERE s.status = 'COMPLETED'
               AND e.student_id IN (%s)
             GROUP BY e.student_id
            """ % placeholders, parameters)

        expected_by_student = {row["student_id"]: row["expected_sessions"]
                               for row in expected}

        summaries = {}
        for row in rows:
            student_id = row["student_id"]
            attended = row["sessions_attended"] or 0
            expected_count = expected_by_student.get(student_id, 0)

            summary = _empty_attendance()
            summary.update({
                "expected_sessions": expected_count,
                "recorded_sessions": row["recorded_sessions"] or 0,
                "sessions_attended": attended,
                # None, not zero: a student nobody has expected yet has no
                # attendance rate, and printing 0% would be a judgement the
                # data does not support.
                "attendance_rate": (round(attended / expected_count, 4)
                                    if expected_count else None),
                "average_presence_ratio": (
                    None if row["average_presence_ratio"] is None
                    else round(row["average_presence_ratio"], 4)),
                "total_present_seconds": row["total_present_seconds"] or 0.0,
            })
            for status in SUMMARY_STATUSES:
                summary["counts"][status] = row["count_%s" % status.lower()] or 0

            summaries[student_id] = summary

        # A student with no attendance row still has an expected count, and
        # saying so is more useful than omitting them.
        for student_id, expected_count in expected_by_student.items():
            if student_id not in summaries:
                summary = _empty_attendance()
                summary["expected_sessions"] = expected_count
                summary["attendance_rate"] = 0.0 if expected_count else None
                summaries[student_id] = summary

        return summaries

    # -- scheduling --------------------------------------------------------

    def expected_from_class(self, class_name, section=None):
        """
        The ACTIVE students registered in a class and section.

        This is what "expected students" means when a teacher picks a class
        rather than ticking thirty boxes: everybody currently enrolled in that
        class is expected at its lesson.

        ACTIVE only, and that is the whole point of the method. A student who
        has left, transferred or graduated is not expected at Tuesday's lesson,
        and counting them would manufacture an absence for somebody who is not
        in the building - which then drags the class's attendance rate down and
        shows up in a report as a child with a problem.

        An empty section means EVERY section of that class. A named one means
        only that section. Matching is case-insensitive, because a class typed
        as "mech" on one screen and "MECH" on another is the same class.

        Returns the ids, sorted, and never raises: a class nobody is in is an
        empty list, which the caller reports rather than guesses around.
        """
        clauses = ["status = 'ACTIVE'", "class_name = ? COLLATE NOCASE"]
        parameters = [(class_name or "").strip()]

        section = (section or "").strip()
        if section:
            clauses.append("section = ? COLLATE NOCASE")
            parameters.append(section)

        rows = self.database.query(
            "SELECT student_id FROM students WHERE %s ORDER BY student_id"
            % " AND ".join(clauses), tuple(parameters))

        return [row["student_id"] for row in rows]

    def class_roster(self, class_name, section=None):
        """
        Who would be expected for a class and section, and who would not be.

        The dashboard shows this count before a session is created, so a
        teacher finds out that a class has nobody active in it BEFORE
        scheduling a lesson against it rather than afterwards.

        `inactive` is reported separately and deliberately. "No students in
        MECH-A" and "seven students in MECH-A, none of them active" are very
        different problems, and a screen that shows 0 for both sends somebody
        to look for the wrong one.
        """
        class_name = (class_name or "").strip()
        section = (section or "").strip()

        clauses = ["class_name = ? COLLATE NOCASE"]
        parameters = [class_name]
        if section:
            clauses.append("section = ? COLLATE NOCASE")
            parameters.append(section)

        # class_name is selected even though it was just filtered on, because
        # the ROW is handed to Principal.may_see_student by the route, and that
        # check reads class_name and section off it. Without the column every
        # student looked like class "" and a scoped teacher saw nobody -
        # including in their own class.
        rows = self.database.query(
            "SELECT student_id, name, roll_number, class_name, section, status "
            "FROM students WHERE %s ORDER BY student_id"
            % " AND ".join(clauses), tuple(parameters))

        active = [dict(row) for row in rows if row["status"] == "ACTIVE"]
        inactive = [dict(row) for row in rows if row["status"] != "ACTIVE"]

        return {
            "class_name": class_name,
            "section": section,
            "expected_students": [row["student_id"] for row in active],
            "expected_count": len(active),
            "students": active,
            "inactive_count": len(inactive),
            "inactive": inactive,
            "note": ("Only ACTIVE students are expected. A student who has "
                     "left, transferred or graduated is listed as inactive "
                     "and produces no absence."),
        }

    def create_session(self, class_id, start_time, end_time,
                       expected_students=None, session_id=None,
                       policy_version=None, timezone_name=None,
                       class_name=None, section=None):
        """
        Schedule a session. The second mutating operation the dashboard has.

        Everything it builds is a Step 6 object saved through the Step 6
        repository - there is no second session model, and no SQL here. What
        this method adds is the validation a web form needs and the Step 6
        constructor does not do on its own: that the students exist, that the
        id is free, and that the window is a plausible lesson.

        A session is ALWAYS created SCHEDULED. There is deliberately no way to
        post a COMPLETED one: attendance belongs to the rule engine, and a
        session that skipped the camera has no evidence behind it.

        Expected students, two ways

            An explicit `expected_students` list is used as given, exactly as
            it always has been. Every existing caller - the CLI, the test
            suites, the dashboard's roster checkboxes - is unaffected.

            Omit it and give `class_name` (with an optional `section`) instead,
            and the list becomes the ACTIVE students registered in that class.
            That is the common case, it is not something a teacher should do by
            hand for a class of forty, and it is resolved HERE rather than in
            the browser so it cannot be a stale copy of the register.

            A class with no active students is REFUSED, with a message naming
            which of the two reasons it is. A session with an empty expected
            list can never produce attendance - absence is only meaningful
            against a list of who was expected - so it would be a lesson that
            silently records nothing.
        """
        from attendance_rules import DEFAULT_POLICY_VERSION

        class_id = (class_id or "").strip()
        if not class_id:
            raise SessionValidationError("A class identifier is required.")

        class_name = (class_name or "").strip()
        section = (section or "").strip()

        if not expected_students and class_name:
            expected_students = self.expected_from_class(class_name, section)

            if not expected_students:
                roster = self.class_roster(class_name, section)
                where = class_name + ("-" + section if section else "")

                if roster["inactive_count"]:
                    raise SessionValidationError(
                        "No ACTIVE students are registered in %s. There are "
                        "%d inactive student(s) there, and an inactive "
                        "student is not expected at a lesson. Reactivate "
                        "somebody, or choose the expected students by hand."
                        % (where, roster["inactive_count"]))

                raise SessionValidationError(
                    "No students are registered in %s, so there is nobody to "
                    "expect. Register a student in that class first, or "
                    "choose the expected students by hand." % where)

        start = parse_timestamp(start_time, "The start time")
        end = parse_timestamp(end_time, "The end time")

        if end <= start:
            raise SessionValidationError(
                "The session must end after it starts.")

        length = (end - start).total_seconds()
        if length < MIN_SESSION_SECONDS:
            raise SessionValidationError(
                "A session must be at least %d minute long."
                % (MIN_SESSION_SECONDS // 60))
        if length > MAX_SESSION_HOURS * 3600:
            raise SessionValidationError(
                "A session cannot be longer than %d hours. Check the dates."
                % MAX_SESSION_HOURS)

        students = self._validated_students(expected_students)

        session_id = (session_id or "").strip() or generate_session_id(
            class_id, start)

        if len(session_id) > 200:
            raise SessionValidationError(
                "The session identifier is too long (limit 200 characters).")

        if self.sessions.get(session_id) is not None:
            raise SessionConflictError(
                "A session with the identifier %s already exists. A class "
                "that meets twice in one day needs a different start time."
                % session_id)

        session = AttendanceSession(
            session_id=session_id,
            class_id=class_id,
            start_time=start,
            end_time=end,
            policy_version=policy_version or DEFAULT_POLICY_VERSION,
            timezone_name=(timezone_name or "UTC").strip() or "UTC",
            status=SESSION_SCHEDULED,
            expected_students=students)

        with self.database.transaction():
            self.sessions.save(session)

            # The section is a column on attendance_sessions (schema version
            # 3) and the Step 6 AttendanceSession model has no field for it -
            # deliberately, because the rule engine does not know what a
            # section is and must not start to. It is written here, after the
            # model's own save, so the session row records which section was
            # scheduled without the engine gaining a concept.
            if section:
                self.database.execute(
                    "UPDATE attendance_sessions SET section = ? "
                    "WHERE session_id = ?", (section, session.session_id))

        return self.get_session(session.session_id)

    def _validated_students(self, expected_students):
        """
        Every expected student must already exist, and be named to say so.

        The foreign key would refuse an unknown id anyway, but as an opaque
        integrity error. A teacher who mistyped one id out of thirty deserves
        to be told which one.
        """
        requested = [str(student_id).strip()
                     for student_id in (expected_students or [])
                     if str(student_id).strip()]

        if not requested:
            raise SessionValidationError(
                "Select at least one expected student, or name a class so "
                "they can be taken from the register. Absence is only "
                "meaningful against a list of who was expected, so a session "
                "with nobody expected can never produce attendance.")

        unique = sorted(set(requested))

        known = {row["student_id"] for row in self.database.query(
            "SELECT student_id FROM students")}

        unknown = [student_id for student_id in unique if student_id not in known]
        if unknown:
            raise SessionValidationError(
                "These student ids are not registered: %s"
                % ", ".join(unknown[:10]))

        return unique

    def create_student(self, student_id, name, roll_number=None,
                       class_name=None, section=None, academic_year=None,
                       status=None, parent_name=None, parent_phone=None,
                       parent_email=None, notification_enabled=None):
        """
        Register a student, with all of their metadata, or refuse a duplicate.

        Everything a teacher types on the Add Student form is written HERE, to
        SQLite, whether or not a camera is ever involved. Registration and
        face enrollment stay separate steps - a student row with no face
        samples is a real state, shown as "Not enrolled" - but registration is
        no longer a half-record that some later enrollment has to complete.

        Nothing about this method knows any particular student. It is the same
        code path for the first student and the four-hundredth.
        """
        student_id = _clean(student_id, 64)
        name = _clean(name, 120)

        if not student_id:
            raise StudentValidationError("A student id is required.")
        if not name:
            raise StudentValidationError("A student name is required.")

        # The embedding store's rule, reused rather than restated: student ids
        # become file names there, so an id this rejects could never be
        # enrolled and must not be registrable either.
        _check_student_id(student_id)

        # Asked for the message, not for the protection.
        #
        # This check and the INSERT below are two statements, and two
        # concurrent registrations can both pass it before either writes.
        # What stops the second one is the PRIMARY KEY, a few lines down -
        # which is why the write is an insert() and not an upsert(): upsert
        # would have found the row the other request had just created and
        # UPDATED it, writing one student's name over another's, leaving one
        # row where there should be two and handing the newcomer the first
        # student's attendance history.
        if self.students.get(student_id) is not None:
            raise StudentConflictError(
                "A student with the id %s already exists." % student_id)

        if status is not None and status not in (STUDENT_ACTIVE,
                                                 STUDENT_INACTIVE):
            raise StudentValidationError(
                "Student status must be ACTIVE or INACTIVE.")

        contact = self._contact_fields(parent_name, parent_phone,
                                       parent_email, notification_enabled)

        try:
            with self.database.transaction():
                self.students.insert(
                    student_id, name, status=status,
                    roll_number=_clean(roll_number, 40),
                    class_name=_clean(class_name, 120),
                    section=_clean(section, 40),
                    academic_year=_clean(academic_year, 60))
                # A second statement rather than more insert() parameters:
                # the enrollment path shares the repository and has no
                # business carrying contact detail it never collected.
                if contact:
                    self.students.update(student_id, **contact)
        except sqlite3.IntegrityError:
            # The constraint won the race. Same answer as the pre-check, so
            # the caller cannot tell which of the two refused - and either
            # way the student who was already there is untouched.
            raise StudentConflictError(
                "A student with the id %s already exists." % student_id)

        return self.get_student(student_id)

    @staticmethod
    def _contact_fields(parent_name, parent_phone, parent_email,
                        notification_enabled):
        """
        The contact fields that were actually supplied.

        Omitted still means "leave it alone", the same rule the rest of the
        registry follows, so an edit that only changes a section cannot blank
        a phone number somebody typed last term.
        """
        fields = {}
        if parent_name is not None:
            fields["parent_name"] = _clean(parent_name, 120)
        if parent_phone is not None:
            fields["parent_phone"] = _clean(parent_phone, 40)
        if parent_email is not None:
            fields["parent_email"] = _clean(parent_email, 200)
        if notification_enabled is not None:
            fields["notification_enabled"] = bool(notification_enabled)
        return fields

    def update_student(self, student_id, name=None, roll_number=None,
                       class_name=None, section=None, academic_year=None,
                       status=None, parent_name=None, parent_phone=None,
                       parent_email=None,
                       notification_enabled=None, sync_store=True):
        """
        Edit a registered student. Returns None if there is no such student.

        student_id is not among the arguments, and that is the design: it is
        the key attendance records, presence segments, expected-student rows
        and the face enrollment all hang from. A field that several tables
        reference is not a field a form may quietly rewrite.

        The embedding store's copy of the metadata is updated to match, so the
        two never drift - but only as a best effort. A student's registry
        entry is the truth, and failing to update a cached copy of it must not
        lose the edit a teacher just made.
        """
        if self.students.get(student_id) is None:
            return None

        if name is not None and not _clean(name):
            raise StudentValidationError("A student name is required.")

        if status is not None and status not in (STUDENT_ACTIVE,
                                                 STUDENT_INACTIVE):
            raise StudentValidationError(
                "Student status must be ACTIVE or INACTIVE.")

        fields = {}
        if name is not None:
            fields["name"] = _clean(name, 120)
        if roll_number is not None:
            fields["roll_number"] = _clean(roll_number, 40)
        if class_name is not None:
            fields["class_name"] = _clean(class_name, 120)
        if section is not None:
            fields["section"] = _clean(section, 40)
        if academic_year is not None:
            fields["academic_year"] = _clean(academic_year, 60)
        if status is not None:
            fields["status"] = status

        fields.update(self._contact_fields(parent_name, parent_phone,
                                           parent_email, notification_enabled))

        with self.database.transaction():
            self.students.update(student_id, **fields)

        if sync_store:
            _sync_store_metadata(self.students.get(student_id))

        return self.get_student(student_id)

    def set_student_status(self, student_id, status):
        """
        Activate or deactivate. The safe alternative to deletion.

        A deactivated student keeps every attendance record, every presence
        segment and every expected-student row. They simply stop appearing in
        the active roster.
        """
        if self.students.get(student_id) is None:
            return None
        if status not in (STUDENT_ACTIVE, STUDENT_INACTIVE):
            raise StudentValidationError(
                "Student status must be ACTIVE or INACTIVE.")

        with self.database.transaction():
            self.students.set_status(student_id, status)

        return self.get_student(student_id)

    def student_removal_impact(self, student_id):
        """
        What deleting this student would actually destroy.

        Asked before the dashboard offers the choice, so "Delete permanently"
        can say how many attendance records it is about to take with it
        instead of finding out afterwards.
        """
        row = self.database.query_one(
            """
            SELECT
              (SELECT COUNT(*) FROM attendance_records
                WHERE student_id = ?) AS attendance_records,
              (SELECT COUNT(*) FROM presence_segments
                WHERE student_id = ?) AS presence_segments,
              (SELECT COUNT(*) FROM session_expected_students
                WHERE student_id = ?) AS expected_in_sessions
            """, (student_id, student_id, student_id))

        impact = dict(row)
        impact["has_history"] = any(impact.values())
        return impact

    def delete_student(self, student_id, force=False, remove_enrollment=True):
        """
        Remove a student permanently.

        Refused by default when anything references them. Every table that
        points at students(student_id) cascades on delete, so removing a
        student who has been in a lesson silently removes that lesson's
        attendance for them - which is precisely what a school must not be
        able to do by clicking Remove. The caller gets a refusal naming what
        would be lost, and Deactivate as the alternative.

        force=True performs it anyway, for the case the refusal exists to
        protect against being mistaken for: a student registered by accident
        five minutes ago who was never in a session.

        The face enrollment goes too, unless asked otherwise. A vector file
        for a student who no longer exists is an orphaned biometric record,
        and keeping one would be the opposite of what deletion means.
        """
        student = self.get_student(student_id)
        if student is None:
            return None

        impact = self.student_removal_impact(student_id)

        if impact["has_history"] and not force:
            raise StudentConflictError(
                "%s has attendance history (%d attendance record(s), %d "
                "presence segment(s), expected in %d session(s)). Deleting "
                "them would delete that history too. Deactivate them instead, "
                "or confirm permanent deletion."
                % (student_id, impact["attendance_records"],
                   impact["presence_segments"], impact["expected_in_sessions"]))

        enrollment_removed = False
        if remove_enrollment:
            enrollment_removed = _delete_enrollment(student_id)

        with self.database.transaction():
            self.students.delete(student_id)

        return {"student_id": student_id, "deleted": True,
                "enrollment_removed": enrollment_removed,
                "history_removed": impact}

    def get_student(self, student_id, with_attendance=True):
        row = self.database.query_one(
            "SELECT %s FROM students WHERE student_id = ?"
            % self.STUDENT_COLUMNS, (student_id,))
        if row is None:
            return None

        record = self._student_record(row)
        if with_attendance:
            self._attach_attendance([record])
        return record

    def student_attendance(self, student_id, limit=DEFAULT_PAGE_SIZE, offset=0):
        total = self.database.query_one(
            "SELECT COUNT(*) AS total FROM attendance_records "
            "WHERE student_id = ?", (student_id,))["total"]

        rows = self.database.query(
            """
            SELECT r.*, st.name AS student_name,
                   s.session_date, s.class_id, s.status AS session_status
              FROM attendance_records r
              LEFT JOIN students st ON st.student_id = r.student_id
              LEFT JOIN attendance_sessions s ON s.session_id = r.session_id
             WHERE r.student_id = ?
             ORDER BY s.session_date DESC, r.created_at DESC
             LIMIT ? OFFSET ?
            """, (student_id, limit, offset))

        items = []
        for row in rows:
            record = self._attendance_record(row)
            record["session_date"] = row["session_date"]
            record["class_id"] = row["class_id"]
            record["session_status"] = row["session_status"]
            items.append(record)

        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # -- the only write the dashboard performs ----------------------------

    def apply_override(self, session_id, student_id, new_status, reason,
                       actor=None, policy_version=None):
        """
        Record a teacher correcting a status.

        The only mutating operation the dashboard exposes. Step 6 keeps the
        original computed status and the full audit trail; this layer adds
        nothing and hides nothing.
        """
        return self._attendance_record_from_dict(
            self.attendance.apply_override(
                session_id, student_id, new_status, reason, actor,
                policy_version))

    def clear_override(self, session_id, student_id, policy_version=None):
        record = self.attendance.clear_override(session_id, student_id,
                                                policy_version)
        return self._attendance_record_from_dict(record) if record else None

    @staticmethod
    def _attendance_record_from_dict(record):
        if record is None:
            return None

        class _Row(dict):
            def keys(self):
                return dict.keys(self)

        return DataService._attendance_record(_Row(record))


def _empty_attendance():
    """
    The shape of "no attendance yet", with every count present.

    A missing key and a zero are different facts to a renderer, and building
    the full shape here means the Students page never has to guess which one
    it is looking at.
    """
    return {
        "expected_sessions": 0,
        "recorded_sessions": 0,
        "sessions_attended": 0,
        "attendance_rate": None,
        "average_presence_ratio": None,
        "total_present_seconds": 0.0,
        "counts": {status: 0 for status in SUMMARY_STATUSES},
    }


def _enrollment_metadata():
    """
    Face-enrollment facts per student id, or an empty mapping.

    Isolated here so the failure mode is stated once: a missing or unreadable
    embedding store means nobody is enrolled, which is the normal state of a
    fresh checkout and must not stop the Students page from rendering the
    registry.
    """
    try:
        from backend.services.enrollment_service import enrollment_metadata
        return enrollment_metadata()
    except Exception:                                    # noqa: BLE001
        return {}


def _check_student_id(student_id):
    """
    Apply the embedding store's id rule to a registration.

    A student id becomes a file name when they are enrolled, so an id that
    store would refuse must not be registrable either - otherwise a teacher
    can create a student who can never have a face.
    """
    try:
        from embedding_store import check_student_id
    except Exception:                                    # noqa: BLE001
        return student_id

    try:
        return check_student_id(student_id)
    except ValueError as error:
        raise StudentValidationError(
            "That student id cannot be used: %s" % error)


def _sync_store_metadata(student):
    """
    Push a registry edit into the embedding store's metadata copy.

    Best effort, and one-directional. SQLite is the source of truth; the store
    keeps its own copy of the name, roll, class and section because an
    enrollment record that cannot say who it belongs to is not much use on its
    own. Keeping them in step means `python ai/embedding_store.py list` and
    the Students page never disagree.

    A student who has never been enrolled has nothing to sync, and that is not
    an error.
    """
    if not student:
        return False

    try:
        from embedding_store import EmbeddingStore

        store = EmbeddingStore()
        if not store.has_student(student["student_id"]):
            return False

        existing = store.get_student(student["student_id"])
        store.save_student(
            student_id=student["student_id"],
            name=student["name"],
            roll_number=student.get("roll_number", ""),
            class_name=student.get("class_name", ""),
            section=student.get("section", ""),
            model_name=existing.get("model_name"),
            model_version=existing.get("model_version"),
            embedding_id=existing.get("embedding_id"),
            created_at=existing.get("created_at"))
        return True
    except Exception:                                    # noqa: BLE001
        return False


def _delete_enrollment(student_id):
    """Remove a student's face samples. Never raises; reports what happened."""
    try:
        from embedding_store import EmbeddingStore
        return bool(EmbeddingStore().delete_student(student_id))
    except Exception:                                    # noqa: BLE001
        return False


def open_service(path=None):
    """Open the database and return a DataService for it."""
    database = Database(path)
    database.connect()
    return DataService(database)
