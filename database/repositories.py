"""
ClassVision AI - Data Access Layer (Step 6)
-------------------------------------------
All SQL for the attendance store lives here, and nowhere else.

    The attendance rule engine must be able to produce a decision without
    knowing that SQL exists. Keeping every statement in this file is what
    enforces that: if a rule ever needs a query, the layering has gone wrong.

Three repositories, one per entity, in one module because they are small and
share the same connection and conversion helpers. Splitting them across three
files would add imports without adding separation.

Every statement is parameterised. No value is ever formatted into SQL.
"""

from datetime import datetime, timezone

STUDENT_ACTIVE = "ACTIVE"
STUDENT_INACTIVE = "INACTIVE"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def to_iso(moment):
    if moment is None:
        return None
    if isinstance(moment, str):
        return moment
    return moment.isoformat(timespec="milliseconds")


def from_iso(text):
    if not text:
        return None
    return datetime.fromisoformat(text)


def _text(value):
    """
    A trimmed string, never None.

    The registry metadata columns are NOT NULL, and "" means nobody has told
    us the roll number yet. Allowing NULL as well would give two spellings of
    the same fact and make every query care about the difference.
    """
    return "" if value is None else str(value).strip()


# The registry metadata a student row carries. Schema version 2 moved these
# into SQLite; before it, they existed only in the embedding store, so a plain
# SELECT on students could not say who a student actually was.
# Schema version 7 added academic_year to this list, which is why it is a
# list: the mirror of the current enrolment is four columns now, and
# update() is what academic_service.enrol() writes it through.
STUDENT_METADATA_FIELDS = ("roll_number", "class_name", "section",
                           "academic_year")

# Parent/guardian contact, schema version 3. Text columns, updated the same way
# as the metadata above - contact detail, not biometric data, and the only
# reason a notification has anywhere to go.
STUDENT_CONTACT_FIELDS = ("parent_name", "parent_phone", "parent_email")

# Whether this student's guardian is contacted at all. Stored as 0/1 rather
# than text because it is a switch, and handled separately from the text
# columns above because "" is a meaningful value for a name and not for a
# boolean.
STUDENT_NOTIFICATION_FIELD = "notification_enabled"


class StudentRepository:
    """
    The student REGISTRY - who a person is, never what they look like.

    Holds id, name, roll number, class, section and status. Face embeddings
    stay in the Step 3 store under the same student_id, and no method here
    accepts, returns, or can reach one.
    """

    def __init__(self, database):
        self.database = database

    def insert(self, student_id, name, status=None, roll_number=None,
               class_name=None, section=None, academic_year=None):
        """
        Add a student who is not there yet, or raise.

        The difference from upsert() is the whole point of it existing: this
        one has no update branch, so a student_id that is already taken ends
        in sqlite3.IntegrityError from the PRIMARY KEY rather than in an
        UPDATE that would overwrite somebody.

        That matters because student_id is the identity everything else hangs
        off - attendance records, presence segments, expected-student rows and
        the embedding store all reference it - so quietly writing a new
        student's name onto an existing id does not create a duplicate, it
        hands one student another student's entire history.

        The registration path uses this. The ENROLLMENT path still uses
        upsert(), because re-running a camera on a student who is already
        registered is a legitimate update and not a collision.
        """
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO students (student_id, name, roll_number,
                                  class_name, section, academic_year,
                                  status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, name, _text(roll_number), _text(class_name),
             _text(section), _text(academic_year),
             status or STUDENT_ACTIVE, now, now))
        return self.get(student_id)

    def upsert(self, student_id, name, status=None, roll_number=None,
               class_name=None, section=None, academic_year=None):
        """
        Add a student, or update the fields that were actually supplied.

        The omitted-means-keep rule matters more than it looks. Enrollment
        calls this after a capture, and a teacher who deactivated a student
        last term should not find them quietly ACTIVE again because somebody
        re-ran the camera. Equally, a caller that knows only an id and a name
        must not blank the roll number somebody typed on the Students page.

        So None means "leave this column alone", and only a value that was
        passed is written.

        Deliberately accepts no biometric data of any kind.
        """
        now = utc_now_iso()

        if self.get(student_id) is None:
            self.database.execute(
                """
                INSERT INTO students (student_id, name, roll_number,
                                      class_name, section, academic_year,
                                      status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (student_id, name, _text(roll_number), _text(class_name),
                 _text(section), _text(academic_year),
                 status or STUDENT_ACTIVE, now, now))
            return self.get(student_id)

        values = {"name": name, "updated_at": now}
        if status is not None:
            values["status"] = status
        if roll_number is not None:
            values["roll_number"] = _text(roll_number)
        if class_name is not None:
            values["class_name"] = _text(class_name)
        if section is not None:
            values["section"] = _text(section)
        if academic_year is not None:
            values["academic_year"] = _text(academic_year)

        self._update_columns(student_id, values)
        return self.get(student_id)

    def update(self, student_id, **fields):
        """
        Edit a registered student. Returns None if there is no such student.

        student_id is not editable, and that is the whole point of it being
        the key: attendance records, presence segments, expected-student rows
        and the embedding store all reference it. Changing it in one place
        would orphan the rest, so it is changed in none of them.
        """
        if self.get(student_id) is None:
            return None

        allowed = (("name", "status") + STUDENT_METADATA_FIELDS
                   + STUDENT_CONTACT_FIELDS)
        values = {key: _text(value) for key, value in fields.items()
                  if key in allowed and value is not None}

        # The notification switch is a 0/1 column, so it cannot go through
        # _text() with the rest. Anything truthy is on; None still means
        # "leave it alone", the same rule as every other field here.
        switch = fields.get(STUDENT_NOTIFICATION_FIELD)
        if switch is not None:
            values[STUDENT_NOTIFICATION_FIELD] = 1 if switch else 0

        if not values:
            return self.get(student_id)

        values["updated_at"] = utc_now_iso()
        self._update_columns(student_id, values)
        return self.get(student_id)

    def _update_columns(self, student_id, values):
        """
        One parameterised UPDATE from a dict of column -> value.

        The column names come from the whitelists above and never from caller
        text, so the assignment list is safe to build. Every VALUE is still
        bound, never formatted.
        """
        assignments = ", ".join("%s = ?" % column for column in values)
        self.database.execute(
            "UPDATE students SET %s WHERE student_id = ?" % assignments,
            tuple(values.values()) + (student_id,))

    def get(self, student_id):
        row = self.database.query_one(
            "SELECT * FROM students WHERE student_id = ?", (student_id,))
        return dict(row) if row else None

    def list_all(self, status=None):
        if status is None:
            rows = self.database.query(
                "SELECT * FROM students ORDER BY student_id")
        else:
            rows = self.database.query(
                "SELECT * FROM students WHERE status = ? ORDER BY student_id",
                (status,))
        return [dict(row) for row in rows]

    def set_status(self, student_id, status):
        self.database.execute(
            "UPDATE students SET status = ?, updated_at = ? WHERE student_id = ?",
            (status, utc_now_iso(), student_id))
        return self.get(student_id)

    def deactivate(self, student_id):
        """
        Retire a student WITHOUT touching their history.

        Deliberately not a DELETE. attendance_records, presence_segments and
        session_expected_students all cascade from students(student_id), so
        removing the row would take a term of attendance with it - which is
        precisely what a school must not be able to do by clicking Remove.
        """
        return self.set_status(student_id, STUDENT_INACTIVE)

    def activate(self, student_id):
        return self.set_status(student_id, STUDENT_ACTIVE)

    def has_history(self, student_id):
        """
        Whether anything in the database references this student.

        Consulted before a hard delete, so "can this row go" is answered by
        the database rather than by hope.
        """
        row = self.database.query_one(
            """
            SELECT
              (SELECT COUNT(*) FROM attendance_records WHERE student_id = ?)
            + (SELECT COUNT(*) FROM presence_segments  WHERE student_id = ?)
            + (SELECT COUNT(*) FROM session_expected_students
                WHERE student_id = ?) AS total
            """, (student_id, student_id, student_id))
        return bool(row["total"])

    def delete(self, student_id):
        cursor = self.database.execute(
            "DELETE FROM students WHERE student_id = ?", (student_id,))
        return cursor.rowcount > 0


class SessionRepository:
    """Attendance sessions and the students expected in them."""

    def __init__(self, database):
        self.database = database

    def save(self, session):
        """Insert or update a session from an AttendanceSession object."""
        self.database.execute(
            """
            INSERT INTO attendance_sessions
                (session_id, class_id, session_date, start_time, end_time,
                 timezone, status, policy_version, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                class_id = excluded.class_id,
                session_date = excluded.session_date,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                timezone = excluded.timezone,
                status = excluded.status,
                policy_version = excluded.policy_version,
                completed_at = excluded.completed_at
            """,
            (session.session_id, session.class_id, session.session_date,
             to_iso(session.start_time), to_iso(session.end_time),
             session.timezone_name, session.status, session.policy_version,
             to_iso(session.created_at), to_iso(session.completed_at)))

        for student_id in session.expected_students:
            self.add_expected_student(session.session_id, student_id)

        return self.get(session.session_id)

    def add_expected_student(self, session_id, student_id):
        """
        Record that a student is expected.

        The student must already exist - the foreign key enforces that, so a
        typo in an id fails loudly instead of creating a phantom absentee.
        """
        self.database.execute(
            "INSERT OR IGNORE INTO session_expected_students "
            "(session_id, student_id) VALUES (?, ?)",
            (session_id, student_id))

    def expected_students(self, session_id):
        rows = self.database.query(
            "SELECT student_id FROM session_expected_students "
            "WHERE session_id = ? ORDER BY student_id", (session_id,))
        return [row["student_id"] for row in rows]

    def get(self, session_id):
        row = self.database.query_one(
            "SELECT * FROM attendance_sessions WHERE session_id = ?",
            (session_id,))
        if row is None:
            return None

        record = dict(row)
        record["expected_students"] = self.expected_students(session_id)
        return record

    def list_for_class(self, class_id):
        rows = self.database.query(
            "SELECT * FROM attendance_sessions WHERE class_id = ? "
            "ORDER BY session_date, start_time", (class_id,))
        return [dict(row) for row in rows]

    def set_status(self, session_id, status, completed_at=None):
        self.database.execute(
            "UPDATE attendance_sessions SET status = ?, completed_at = ? "
            "WHERE session_id = ?",
            (status, to_iso(completed_at), session_id))
        return self.get(session_id)

    def complete(self, session_id, completed_at=None):
        return self.set_status(session_id, "COMPLETED",
                               completed_at or datetime.now(timezone.utc))


class AttendanceRepository:
    """Attendance decisions and the presence evidence behind them."""

    def __init__(self, database):
        self.database = database

    # -- presence evidence -------------------------------------------------

    def save_segments(self, session_id, segments):
        """
        Persist the presence segments a decision was derived from.

        These are what make re-evaluation under a new policy possible without
        re-running the camera. INSERT OR IGNORE plus the UNIQUE constraint
        makes saving the same evidence twice a no-op.
        """
        now = utc_now_iso()
        saved = 0

        for segment in segments:
            cursor = self.database.execute(
                """
                INSERT OR IGNORE INTO presence_segments
                    (session_id, student_id, track_id, start_time, end_time,
                     duration_seconds, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, segment.student_id, segment.track_id,
                 to_iso(segment.start), to_iso(segment.end),
                 segment.duration_seconds, now))
            saved += cursor.rowcount

        return saved

    def get_segments(self, session_id, student_id=None):
        if student_id is None:
            rows = self.database.query(
                "SELECT * FROM presence_segments WHERE session_id = ? "
                "ORDER BY student_id, start_time", (session_id,))
        else:
            rows = self.database.query(
                "SELECT * FROM presence_segments WHERE session_id = ? "
                "AND student_id = ? ORDER BY start_time",
                (session_id, student_id))
        return [dict(row) for row in rows]

    # -- decisions ---------------------------------------------------------

    def save_decision(self, decision):
        """
        Store one attendance decision.

        Idempotent by design: the UNIQUE(session, student, policy_version)
        constraint means re-running an evaluation with the same policy UPDATES
        the existing row instead of adding a second one. A different policy
        version creates a separate row, so the earlier result survives.

        A manual override is never clobbered by re-evaluation - the override
        columns are left alone by this statement.
        """
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO attendance_records
                (session_id, student_id, status, first_seen, last_seen,
                 present_seconds, session_seconds, presence_ratio,
                 segment_count, loss_periods, was_late, left_early,
                 reason, policy_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, student_id, policy_version) DO UPDATE SET
                status = CASE WHEN attendance_records.is_override = 1
                              THEN attendance_records.status
                              ELSE excluded.status END,
                first_seen = excluded.first_seen,
                last_seen = excluded.last_seen,
                present_seconds = excluded.present_seconds,
                session_seconds = excluded.session_seconds,
                presence_ratio = excluded.presence_ratio,
                segment_count = excluded.segment_count,
                loss_periods = excluded.loss_periods,
                was_late = excluded.was_late,
                left_early = excluded.left_early,
                reason = CASE WHEN attendance_records.is_override = 1
                              THEN attendance_records.reason
                              ELSE excluded.reason END,
                updated_at = excluded.updated_at
            """,
            (decision.session_id, decision.student_id, decision.status,
             to_iso(decision.first_seen), to_iso(decision.last_seen),
             decision.present_seconds, decision.session_seconds,
             min(decision.presence_ratio, 1.0), decision.segment_count,
             decision.loss_periods, int(decision.was_late),
             int(decision.left_early), decision.reason,
             decision.policy_version, now, now))

        return self.get(decision.session_id, decision.student_id,
                        decision.policy_version)

    def save_decisions(self, decisions):
        """Save a whole session's decisions in one transaction."""
        with self.database.transaction():
            for decision in decisions:
                self.save_decision(decision)
        return len(decisions)

    def get(self, session_id, student_id, policy_version=None):
        if policy_version is None:
            row = self.database.query_one(
                "SELECT * FROM attendance_records WHERE session_id = ? "
                "AND student_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id, student_id))
        else:
            row = self.database.query_one(
                "SELECT * FROM attendance_records WHERE session_id = ? "
                "AND student_id = ? AND policy_version = ?",
                (session_id, student_id, policy_version))
        return dict(row) if row else None

    def list_for_session(self, session_id, policy_version=None):
        if policy_version is None:
            rows = self.database.query(
                "SELECT * FROM attendance_records WHERE session_id = ? "
                "ORDER BY student_id, policy_version", (session_id,))
        else:
            rows = self.database.query(
                "SELECT * FROM attendance_records WHERE session_id = ? "
                "AND policy_version = ? ORDER BY student_id",
                (session_id, policy_version))
        return [dict(row) for row in rows]

    def list_for_student(self, student_id):
        rows = self.database.query(
            "SELECT * FROM attendance_records WHERE student_id = ? "
            "ORDER BY created_at", (student_id,))
        return [dict(row) for row in rows]

    def policy_versions_for_session(self, session_id):
        rows = self.database.query(
            "SELECT DISTINCT policy_version FROM attendance_records "
            "WHERE session_id = ? ORDER BY policy_version", (session_id,))
        return [row["policy_version"] for row in rows]

    # -- manual override ---------------------------------------------------

    def apply_override(self, session_id, student_id, new_status, reason,
                       actor=None, policy_version=None, overridden_at=None):
        """
        Record a human correcting an attendance result.

        The computed status is preserved in original_status, so the record
        always shows both what the system decided and what a person changed it
        to. There is no way to alter a result without leaving that trail.
        """
        existing = self.get(session_id, student_id, policy_version)
        if existing is None:
            raise LookupError(
                "no attendance record for student %r in session %r"
                % (student_id, session_id))

        # Keep the FIRST computed status if this record was already overridden,
        # so repeated corrections never erase what the system originally said.
        original = existing["original_status"] or existing["status"]
        moment = to_iso(overridden_at or datetime.now(timezone.utc))

        self.database.execute(
            """
            UPDATE attendance_records
               SET status = ?,
                   is_override = 1,
                   original_status = ?,
                   override_reason = ?,
                   overridden_by = ?,
                   overridden_at = ?,
                   updated_at = ?
             WHERE attendance_id = ?
            """,
            (new_status, original, reason, actor, moment, moment,
             existing["attendance_id"]))

        return self.get(session_id, student_id, existing["policy_version"])

    def clear_override(self, session_id, student_id, policy_version=None):
        """Undo an override, restoring the computed status."""
        existing = self.get(session_id, student_id, policy_version)
        if existing is None or not existing["is_override"]:
            return existing

        self.database.execute(
            """
            UPDATE attendance_records
               SET status = original_status,
                   is_override = 0,
                   original_status = NULL,
                   override_reason = NULL,
                   overridden_by = NULL,
                   overridden_at = NULL,
                   updated_at = ?
             WHERE attendance_id = ?
            """,
            (utc_now_iso(), existing["attendance_id"]))

        return self.get(session_id, student_id, existing["policy_version"])

    def count_for_session(self, session_id):
        row = self.database.query_one(
            "SELECT COUNT(*) AS total FROM attendance_records WHERE session_id = ?",
            (session_id,))
        return row["total"]


# ---------------------------------------------------------------------------
# Accounts, class assignments, sign-in sessions and the audit trail.
#
# Added with schema version 2, which is the first time this project has had
# any notion of "who is asking". Password HASHES are stored and compared here;
# the hashing itself lives in backend/services/auth_service.py, because how a
# password is derived is a security decision and not a data-access one.
#
# No method in this section returns a password hash to a caller that did not
# ask for it by name, and the API layer never asks.
# ---------------------------------------------------------------------------

USER_ACTIVE = "ACTIVE"
USER_INACTIVE = "INACTIVE"

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_TEACHER = "TEACHER"

# Everything about an account EXCEPT the password hash. Used wherever a user
# is about to cross a boundary - into an API response, into a log line - so
# the hash has to be asked for deliberately rather than merely not removed.
PUBLIC_USER_FIELDS = ("user_id", "username", "display_name", "role", "status",
                      "created_at", "updated_at", "last_login_at",
                      "created_by", "photo_name", "can_view_all_students")


def public_user(record):
    """Strip an account down to the fields that may leave the server."""
    if record is None:
        return None
    return {field: record.get(field) for field in PUBLIC_USER_FIELDS}


class UserRepository:
    """Sign-in accounts and the classes their owners are responsible for."""

    def __init__(self, database):
        self.database = database

    # -- accounts ----------------------------------------------------------

    def create(self, username, display_name, role, password_hash,
               status=USER_ACTIVE, created_by=None):
        now = utc_now_iso()
        cursor = self.database.execute(
            """
            INSERT INTO users (username, display_name, role, password_hash,
                               status, created_at, updated_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (username, display_name, role, password_hash, status, now, now,
             created_by))
        return self.get(cursor.lastrowid)

    def get(self, user_id):
        row = self.database.query_one(
            "SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    def get_by_username(self, username):
        """
        Case-insensitive, because a person typing their own name in a
        different case is not a different person.
        """
        row = self.database.query_one(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,))
        return dict(row) if row else None

    def list_all(self):
        rows = self.database.query(
            "SELECT * FROM users ORDER BY role, username")
        return [dict(row) for row in rows]

    def count(self):
        return self.database.query_one(
            "SELECT COUNT(*) AS total FROM users")["total"]

    def count_active_super_admins(self, excluding=None):
        """
        How many usable administrators would remain.

        Asked before deactivating, demoting or deleting one, so the system
        cannot be locked out of its own administration by a single click.
        """
        if excluding is None:
            row = self.database.query_one(
                "SELECT COUNT(*) AS total FROM users "
                "WHERE role = ? AND status = ?",
                (ROLE_SUPER_ADMIN, USER_ACTIVE))
        else:
            row = self.database.query_one(
                "SELECT COUNT(*) AS total FROM users "
                "WHERE role = ? AND status = ? AND user_id != ?",
                (ROLE_SUPER_ADMIN, USER_ACTIVE, excluding))
        return row["total"]

    def update(self, user_id, **fields):
        if self.get(user_id) is None:
            return None

        allowed = ("display_name", "role", "status", "password_hash",
                   "can_view_all_students",
                   "last_login_at")
        values = {key: value for key, value in fields.items()
                  if key in allowed and value is not None}
        if not values:
            return self.get(user_id)

        values["updated_at"] = utc_now_iso()
        assignments = ", ".join("%s = ?" % column for column in values)
        self.database.execute(
            "UPDATE users SET %s WHERE user_id = ?" % assignments,
            tuple(values.values()) + (user_id,))
        return self.get(user_id)

    def set_status(self, user_id, status):
        return self.update(user_id, status=status)

    def touch_login(self, user_id):
        self.database.execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?",
            (utc_now_iso(), user_id))

    def delete(self, user_id):
        cursor = self.database.execute(
            "DELETE FROM users WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0

    # -- class assignments -------------------------------------------------

    def assign_class(self, user_id, class_name, section=""):
        self.database.execute(
            "INSERT OR IGNORE INTO user_class_assignments "
            "(user_id, class_name, section, created_at) VALUES (?, ?, ?, ?)",
            (user_id, _text(class_name), _text(section), utc_now_iso()))

    def clear_assignments(self, user_id):
        self.database.execute(
            "DELETE FROM user_class_assignments WHERE user_id = ?", (user_id,))

    def set_assignments(self, user_id, assignments):
        """
        Replace a teacher's class list.

        assignments is a sequence of (class_name, section) pairs or dicts. An
        empty list is meaningful: it means "no restriction", not "no access" -
        see backend/services/authz.py for why that distinction is deliberate.
        """
        self.clear_assignments(user_id)
        for assignment in assignments or []:
            if isinstance(assignment, dict):
                class_name = assignment.get("class_name")
                section = assignment.get("section", "")
            else:
                class_name, section = (list(assignment) + [""])[:2]
            if _text(class_name):
                self.assign_class(user_id, class_name, section)
        return self.assignments(user_id)

    def assignments(self, user_id):
        rows = self.database.query(
            "SELECT class_name, section FROM user_class_assignments "
            "WHERE user_id = ? ORDER BY class_name, section", (user_id,))
        return [{"class_name": row["class_name"], "section": row["section"]}
                for row in rows]


class SessionTokenRepository:
    """
    Live sign-ins.

    Only the SHA-256 of a token is stored, so somebody who can read this table
    still cannot sign in as anybody: the value the browser holds cannot be
    recovered from the value the database holds.
    """

    def __init__(self, database):
        self.database = database

    def create(self, token_hash, user_id, expires_at):
        now = utc_now_iso()
        self.database.execute(
            "INSERT OR REPLACE INTO user_sessions "
            "(token_hash, user_id, created_at, expires_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, now, to_iso(expires_at), now))

    def get(self, token_hash):
        row = self.database.query_one(
            "SELECT * FROM user_sessions WHERE token_hash = ?", (token_hash,))
        return dict(row) if row else None

    def touch(self, token_hash):
        self.database.execute(
            "UPDATE user_sessions SET last_seen = ? WHERE token_hash = ?",
            (utc_now_iso(), token_hash))

    def delete(self, token_hash):
        cursor = self.database.execute(
            "DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))
        return cursor.rowcount > 0

    def delete_for_user(self, user_id):
        """
        Sign a user out everywhere.

        Called when an account is deactivated or its role changes, so a
        revoked teacher's open browser tab stops working at the next request
        rather than at the next restart.
        """
        cursor = self.database.execute(
            "DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
        return cursor.rowcount

    def purge_expired(self, now=None):
        cursor = self.database.execute(
            "DELETE FROM user_sessions WHERE expires_at <= ?",
            (now or utc_now_iso(),))
        return cursor.rowcount


class AuditRepository:
    """
    What was done, by whom, and when.

    Append-only by use rather than by constraint: nothing in this project
    updates or deletes a row here. It records that an action happened - never
    a password, never an embedding, never anything biometric.
    """

    def __init__(self, database):
        self.database = database

    def record(self, action, actor=None, actor_role=None, subject=None,
               detail=None):
        self.database.execute(
            """
            INSERT INTO audit_log (at, actor, actor_role, action, subject, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (utc_now_iso(), actor, actor_role, action, subject,
             None if detail is None else str(detail)[:500]))

    def recent(self, limit=100):
        rows = self.database.query(
            "SELECT * FROM audit_log ORDER BY event_id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Academic sessions and student academic history.
#
# Added with schema version 3. The split it implements is the one thing worth
# stating twice: a STUDENT is permanent, and their class, section, roll number
# and semester are facts about a PERIOD. Promotion writes a new period; it
# never edits an old one.
# ---------------------------------------------------------------------------


class AcademicRepository:
    """Terms, and which student was where during each of them."""

    def __init__(self, database):
        self.database = database

    # -- academic sessions -------------------------------------------------

    def create_session(self, academic_session_id, academic_year, semester="",
                       start_date=None, end_date=None, status="PLANNED"):
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO academic_sessions
                (academic_session_id, academic_year, semester, start_date,
                 end_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (academic_session_id, _text(academic_year), _text(semester),
             start_date, end_date, status, now, now))
        return self.get_session(academic_session_id)

    def get_session(self, academic_session_id):
        row = self.database.query_one(
            "SELECT * FROM academic_sessions WHERE academic_session_id = ?",
            (academic_session_id,))
        return dict(row) if row else None

    def list_sessions(self):
        rows = self.database.query(
            "SELECT * FROM academic_sessions "
            "ORDER BY academic_year DESC, semester DESC")
        return [dict(row) for row in rows]

    def current_session(self):
        row = self.database.query_one(
            "SELECT * FROM academic_sessions WHERE status = 'ACTIVE' "
            "ORDER BY academic_year DESC, semester DESC LIMIT 1")
        return dict(row) if row else None

    def set_session_status(self, academic_session_id, status):
        self.database.execute(
            "UPDATE academic_sessions SET status = ?, updated_at = ? "
            "WHERE academic_session_id = ?",
            (status, utc_now_iso(), academic_session_id))
        return self.get_session(academic_session_id)

    def deactivate_other_sessions(self, academic_session_id):
        """
        Exactly one term is current.

        The others become CLOSED rather than PLANNED: a term that was running
        and is no longer running has finished, and calling it "planned" would
        make it look like it had not happened yet.
        """
        self.database.execute(
            "UPDATE academic_sessions SET status = 'CLOSED', updated_at = ? "
            "WHERE academic_session_id != ? AND status = 'ACTIVE'",
            (utc_now_iso(), academic_session_id))

    # -- enrolments --------------------------------------------------------

    def upsert_enrolment(self, student_id, academic_session_id=None,
                         academic_year="", semester="", class_name="",
                         section="", roll_number="", start_date=None,
                         end_date=None, is_current=True,
                         status=STUDENT_ACTIVE):
        """
        Record one student's place in one term.

        UPDATES when that student already has a row for that term - the UNIQUE
        constraint says a student is in a section once - and INSERTS
        otherwise. Promoting into a NEW term therefore always creates a row,
        which is what keeps the previous term intact.
        """
        now = utc_now_iso()
        existing = self.enrolment_for_session(student_id, academic_session_id)

        if existing is None:
            cursor = self.database.execute(
                """
                INSERT INTO student_academic_history
                    (student_id, academic_session_id, academic_year, semester,
                     class_name, section, roll_number, status, start_date,
                     end_date, is_current, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (student_id, academic_session_id, _text(academic_year),
                 _text(semester), _text(class_name), _text(section),
                 _text(roll_number), status, start_date, end_date,
                 int(bool(is_current)), now, now))
            return self.get_enrolment(cursor.lastrowid)

        self.database.execute(
            """
            UPDATE student_academic_history
               SET academic_year = ?, semester = ?, class_name = ?,
                   section = ?, roll_number = ?, status = ?, start_date = ?,
                   end_date = ?, is_current = ?, updated_at = ?
             WHERE academic_history_id = ?
            """,
            (_text(academic_year), _text(semester), _text(class_name),
             _text(section), _text(roll_number), status, start_date, end_date,
             int(bool(is_current)), now, existing["academic_history_id"]))

        return self.get_enrolment(existing["academic_history_id"])

    def get_enrolment(self, academic_history_id):
        row = self.database.query_one(
            "SELECT * FROM student_academic_history "
            "WHERE academic_history_id = ?", (academic_history_id,))
        return dict(row) if row else None

    def enrolment_for_session(self, student_id, academic_session_id):
        if academic_session_id is None:
            row = self.database.query_one(
                "SELECT * FROM student_academic_history "
                "WHERE student_id = ? AND academic_session_id IS NULL",
                (student_id,))
        else:
            row = self.database.query_one(
                "SELECT * FROM student_academic_history "
                "WHERE student_id = ? AND academic_session_id = ?",
                (student_id, academic_session_id))
        return dict(row) if row else None

    def current_enrolment(self, student_id):
        row = self.database.query_one(
            "SELECT * FROM student_academic_history "
            "WHERE student_id = ? AND is_current = 1 "
            "ORDER BY academic_history_id DESC LIMIT 1", (student_id,))
        return dict(row) if row else None

    def mark_only_current(self, student_id, academic_history_id):
        """
        Move the is_current marker, without touching anything else.

        The previous row keeps its class, its section and its roll number.
        That is the whole point: the attendance recorded during that term must
        go on describing that term.
        """
        self.database.execute(
            "UPDATE student_academic_history SET is_current = 0, "
            "updated_at = ? WHERE student_id = ? AND academic_history_id != ?",
            (utc_now_iso(), student_id, academic_history_id))
        self.database.execute(
            "UPDATE student_academic_history SET is_current = 1, "
            "updated_at = ? WHERE academic_history_id = ?",
            (utc_now_iso(), academic_history_id))

    def history(self, student_id):
        rows = self.database.query(
            """
            SELECT h.*, s.status AS session_status
              FROM student_academic_history h
              LEFT JOIN academic_sessions s
                ON s.academic_session_id = h.academic_session_id
             WHERE h.student_id = ?
             ORDER BY h.is_current DESC, h.academic_history_id DESC
            """, (student_id,))
        return [dict(row) for row in rows]

    def students_in_session(self, academic_session_id):
        rows = self.database.query(
            "SELECT * FROM student_academic_history "
            "WHERE academic_session_id = ? ORDER BY roll_number, student_id",
            (academic_session_id,))
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Alert rules, the notifications they raised, recognition models, and cameras.
#
# Added with schema version 3.
#
# A notification row records WHO it was about, WHICH rule fired and whether
# delivery succeeded. It deliberately holds no phone number and no email
# address: those live on the student row and are read at send time. A delivery
# log that accumulates contact details is a breach waiting to be copied.
# ---------------------------------------------------------------------------


class NotificationRepository:
    """Alert rules and the notifications they produced."""

    def __init__(self, database):
        self.database = database

    # -- rules -------------------------------------------------------------

    def save_rule(self, rule_id, name, rule_type, threshold=None,
                  window_days=None, enabled=True, class_name="", section="",
                  message=""):
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO notification_rules
                (rule_id, name, rule_type, threshold, window_days, enabled,
                 class_name, section, message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                name = excluded.name,
                rule_type = excluded.rule_type,
                threshold = excluded.threshold,
                window_days = excluded.window_days,
                enabled = excluded.enabled,
                class_name = excluded.class_name,
                section = excluded.section,
                message = excluded.message,
                updated_at = excluded.updated_at
            """,
            (rule_id, _text(name), rule_type,
             None if threshold is None else float(threshold),
             None if window_days is None else int(window_days),
             int(bool(enabled)), _text(class_name), _text(section),
             _text(message, ), now, now))
        return self.get_rule(rule_id)

    def get_rule(self, rule_id):
        row = self.database.query_one(
            "SELECT * FROM notification_rules WHERE rule_id = ?", (rule_id,))
        return dict(row) if row else None

    def list_rules(self):
        rows = self.database.query(
            "SELECT * FROM notification_rules ORDER BY rule_id")
        return [dict(row) for row in rows]

    def delete_rule(self, rule_id):
        cursor = self.database.execute(
            "DELETE FROM notification_rules WHERE rule_id = ?", (rule_id,))
        return cursor.rowcount > 0

    # -- notifications -----------------------------------------------------

    def create(self, student_id, rule_id, notification_type, subject="",
               summary="", measured_value=None, threshold=None,
               period_start=None, period_end=None, generated_at=None,
               recipient_kind="parent"):
        cursor = self.database.execute(
            """
            INSERT INTO notifications
                (student_id, rule_id, notification_type, recipient_kind,
                 subject, summary, measured_value, threshold, period_start,
                 period_end, generated_at, delivery_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (student_id, rule_id, notification_type, recipient_kind,
             _text(subject), _text(summary, ), measured_value, threshold,
             period_start, period_end, generated_at or utc_now_iso()))
        return self.get(cursor.lastrowid)

    def get(self, notification_id):
        row = self.database.query_one(
            "SELECT * FROM notifications WHERE notification_id = ?",
            (notification_id,))
        return dict(row) if row else None

    def set_status(self, notification_id, status, failure_reason=None):
        self.database.execute(
            """
            UPDATE notifications
               SET delivery_status = ?,
                   delivered_at = CASE WHEN ? = 'SENT' THEN ? ELSE delivered_at END,
                   failure_reason = ?
             WHERE notification_id = ?
            """,
            (status, status, utc_now_iso(), failure_reason, notification_id))
        return self.get(notification_id)

    def recent_for(self, student_id, rule_id, since):
        """
        How many times this rule has already fired for this student lately.

        The cooldown check. Without it a nightly run tells a parent the same
        thing every night until the average recovers, which is how people
        learn to ignore alerts.
        """
        row = self.database.query_one(
            "SELECT COUNT(*) AS total FROM notifications "
            "WHERE student_id = ? AND rule_id = ? AND generated_at >= ?",
            (student_id, rule_id, since))
        return row["total"]

    def history(self, student_id=None, limit=100):
        if student_id:
            rows = self.database.query(
                "SELECT * FROM notifications WHERE student_id = ? "
                "ORDER BY notification_id DESC LIMIT ?", (student_id, limit))
        else:
            rows = self.database.query(
                "SELECT * FROM notifications "
                "ORDER BY notification_id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]


class ModelRepository:
    """
    Recognition models and which one is in production.

    SFace is the baseline. A custom model is recorded here with its metrics
    and cannot become ACTIVE without an explicit activation - see
    ai/model_registry.py for the states and what each one means.
    """

    def __init__(self, database):
        self.database = database

    def register(self, model_id, model_name, version, kind="experimental",
                 status="TESTING", dataset_version=None, metrics=None,
                 notes=None, trained_at=None):
        import json

        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO model_registry
                (model_id, model_name, version, kind, status, dataset_version,
                 metrics, notes, trained_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_id) DO UPDATE SET
                status = excluded.status,
                dataset_version = excluded.dataset_version,
                metrics = excluded.metrics,
                notes = excluded.notes,
                trained_at = excluded.trained_at,
                updated_at = excluded.updated_at
            """,
            (model_id, _text(model_name), _text(version), kind, status,
             dataset_version,
             None if metrics is None else json.dumps(metrics),
             _text(notes, ) or None, trained_at, now, now))
        return self.get(model_id)

    def get(self, model_id):
        import json

        row = self.database.query_one(
            "SELECT * FROM model_registry WHERE model_id = ?", (model_id,))
        if row is None:
            return None

        record = dict(row)
        if record.get("metrics"):
            try:
                record["metrics"] = json.loads(record["metrics"])
            except (ValueError, TypeError):
                record["metrics"] = None
        return record

    def list_all(self):
        rows = self.database.query(
            "SELECT model_id FROM model_registry "
            "ORDER BY kind, model_name, version")
        return [self.get(row["model_id"]) for row in rows]

    def active(self):
        row = self.database.query_one(
            "SELECT model_id FROM model_registry WHERE status = 'ACTIVE' "
            "ORDER BY updated_at DESC LIMIT 1")
        return self.get(row["model_id"]) if row else None

    def set_status(self, model_id, status, activated_at=None):
        self.database.execute(
            "UPDATE model_registry SET status = ?, activated_at = ?, "
            "updated_at = ? WHERE model_id = ?",
            (status, activated_at, utc_now_iso(), model_id))
        return self.get(model_id)

    def deactivate_others(self, model_id):
        """Exactly one model is in production at a time."""
        self.database.execute(
            "UPDATE model_registry SET status = 'VALIDATED', updated_at = ? "
            "WHERE model_id != ? AND status = 'ACTIVE'",
            (utc_now_iso(), model_id))


class CameraRepository:
    """
    Cameras, and whether they are answering.

    Operational only. A row says where a device is and when it last delivered
    a frame. It says nothing about any person the camera has ever seen.
    """

    def __init__(self, database):
        self.database = database

    def upsert(self, camera_id, label="", classroom="", camera_index=None,
               status="unknown", registered_by=None):
        """
        Register a device, or correct the details of one already registered.

        `status` is only applied when the row is CREATED. Re-registering a
        camera to fix a typo in its classroom must not reset a device that is
        currently online back to "unknown" - what the hardware last reported
        is a fact about the hardware, not about the form somebody just saved.

        created_at is likewise left alone by the update, so it stays the
        registration time rather than the last-edited time.
        """
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO cameras
                (camera_id, label, classroom, camera_index, status,
                 registered_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(camera_id) DO UPDATE SET
                label = excluded.label,
                classroom = excluded.classroom,
                camera_index = excluded.camera_index,
                updated_at = excluded.updated_at
            """,
            (camera_id, _text(label), _text(classroom), camera_index, status,
             _text(registered_by), now, now))
        return self.get(camera_id)

    def get(self, camera_id):
        row = self.database.query_one(
            "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,))
        return dict(row) if row else None

    def list_all(self):
        rows = self.database.query("SELECT * FROM cameras ORDER BY camera_id")
        return [dict(row) for row in rows]

    def heartbeat(self, camera_id, status="online", fps=None, latency_ms=None,
                  error=None, pipeline_state=None, session_id=None):
        """
        Record that a camera answered, and how well.

        Only values the hardware actually reported are written. A camera that
        does not report frames per second leaves last_fps alone rather than
        being credited with a number nobody measured - COALESCE is doing the
        work, and it is the reason a partial heartbeat cannot invent a
        reading. A device that has never reported an FPS keeps NULL, and the
        dashboard shows "No data" rather than a zero somebody might read as
        "the camera is delivering no frames".
        """
        now = utc_now_iso()
        self.database.execute(
            """
            UPDATE cameras
               SET status = ?,
                   last_heartbeat = ?,
                   last_fps = COALESCE(?, last_fps),
                   last_latency_ms = COALESCE(?, last_latency_ms),
                   last_error = ?,
                   pipeline_state = COALESCE(?, pipeline_state),
                   last_session_id = COALESCE(?, last_session_id),
                   updated_at = ?
             WHERE camera_id = ?
            """,
            (status, now, fps, latency_ms, error, pipeline_state, session_id,
             now, camera_id))
        return self.get(camera_id)

    def delete(self, camera_id):
        cursor = self.database.execute(
            "DELETE FROM cameras WHERE camera_id = ?", (camera_id,))
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# STUDENT PORTAL ACCOUNTS (schema version 4)
#
# A student's own login, used for exactly one thing: letting them see their
# own attendance. It is a SEPARATE table from `users`, and that is the whole
# security argument - see the comment above USER_ROLES in
# database/database.py. Every staff permission starts by resolving a row in
# `users`, and nothing in this section can produce one.
#
# The row is keyed on student_id, so an account cannot exist for somebody who
# is not in the registry, and deleting a student removes their login with
# them rather than leaving a credential pointing at nobody.
# ---------------------------------------------------------------------------

STUDENT_ACCOUNT_ACTIVE = "ACTIVE"
STUDENT_ACCOUNT_INACTIVE = "INACTIVE"

# Everything about a portal account except the hash. Same discipline as
# PUBLIC_USER_FIELDS: the hash has to be asked for by name.
PUBLIC_STUDENT_ACCOUNT_FIELDS = (
    "student_id", "username", "status", "must_change", "created_at",
    "updated_at", "last_login_at", "created_by")


def public_student_account(record):
    """Strip a portal account down to the fields that may leave the server."""
    if record is None:
        return None
    return {field: record.get(field)
            for field in PUBLIC_STUDENT_ACCOUNT_FIELDS}


class StudentAccountRepository:
    """Portal logins, one per student, issued by a Super Admin."""

    def __init__(self, database):
        self.database = database

    def create(self, student_id, username, password_hash, created_by=None,
               must_change=True):
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO student_accounts
                (student_id, username, password_hash, status, must_change,
                 created_at, updated_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, username, password_hash, STUDENT_ACCOUNT_ACTIVE,
             1 if must_change else 0, now, now, created_by))
        return self.get(student_id)

    def get(self, student_id):
        row = self.database.query_one(
            "SELECT * FROM student_accounts WHERE student_id = ?",
            (student_id,))
        return dict(row) if row else None

    def get_by_username(self, username):
        row = self.database.query_one(
            "SELECT * FROM student_accounts WHERE username = ?", (username,))
        return dict(row) if row else None

    def list_all(self):
        rows = self.database.query(
            "SELECT * FROM student_accounts ORDER BY username")
        return [dict(row) for row in rows]

    def update(self, student_id, **fields):
        """
        Change a portal account. Only the four fields named here are writable.

        student_id is not among them: it is the primary key AND the link to
        the registry, and an account that could be re-pointed at a different
        student would be a way to read somebody else's attendance.
        """
        allowed = ("username", "password_hash", "status", "must_change")
        assignments, values = [], []

        for field in allowed:
            if field in fields and fields[field] is not None:
                assignments.append("%s = ?" % field)
                values.append(fields[field])

        if not assignments:
            return self.get(student_id)

        assignments.append("updated_at = ?")
        values.extend([utc_now_iso(), student_id])

        self.database.execute(
            "UPDATE student_accounts SET %s WHERE student_id = ?"
            % ", ".join(assignments), tuple(values))
        return self.get(student_id)

    def touch_login(self, student_id):
        self.database.execute(
            "UPDATE student_accounts SET last_login_at = ? "
            "WHERE student_id = ?", (utc_now_iso(), student_id))

    def delete(self, student_id):
        """
        Remove the login. The STUDENT is untouched.

        Revoking portal access is not the same operation as removing somebody
        from the school, and this one loses nothing: no attendance record, no
        presence segment, no registry row. It is the reversible answer, and
        the only one this repository offers.
        """
        cursor = self.database.execute(
            "DELETE FROM student_accounts WHERE student_id = ?", (student_id,))
        return cursor.rowcount > 0


class StudentSessionRepository:
    """
    Live portal sign-ins. Only the SHA-256 of a token is stored.

    A deliberate copy of SessionTokenRepository rather than a shared base
    class: the two tables have different owners and different cascade rules,
    and a single generic token store serving both would be one edit away from
    letting a portal token resolve to a staff account.
    """

    def __init__(self, database):
        self.database = database

    def create(self, token_hash, student_id, expires_at):
        now = utc_now_iso()
        self.database.execute(
            "INSERT OR REPLACE INTO student_sessions "
            "(token_hash, student_id, created_at, expires_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, student_id, now, to_iso(expires_at), now))

    def get(self, token_hash):
        row = self.database.query_one(
            "SELECT * FROM student_sessions WHERE token_hash = ?",
            (token_hash,))
        return dict(row) if row else None

    def touch(self, token_hash):
        self.database.execute(
            "UPDATE student_sessions SET last_seen = ? WHERE token_hash = ?",
            (utc_now_iso(), token_hash))

    def delete(self, token_hash):
        cursor = self.database.execute(
            "DELETE FROM student_sessions WHERE token_hash = ?", (token_hash,))
        return cursor.rowcount > 0

    def delete_for_student(self, student_id):
        """Sign a student out everywhere - used when access is revoked."""
        cursor = self.database.execute(
            "DELETE FROM student_sessions WHERE student_id = ?", (student_id,))
        return cursor.rowcount

    def purge_expired(self):
        cursor = self.database.execute(
            "DELETE FROM student_sessions WHERE expires_at <= ?",
            (utc_now_iso(),))
        return cursor.rowcount


class StaffDocumentRepository:
    """
    A teacher's personnel documents, recorded by name rather than by content.

    The file lives under data/staff/; this table says what it was called, how
    big it is and who uploaded it. stored_name is a random filename, so the
    URL a document is served from cannot be guessed from a person's name.
    """

    def __init__(self, database):
        self.database = database

    def add(self, user_id, filename, stored_name, label="",
            content_type="", byte_size=0, uploaded_by=None):
        cursor = self.database.execute(
            """
            INSERT INTO staff_documents
                (user_id, label, filename, stored_name, content_type,
                 byte_size, uploaded_at, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, label, filename, stored_name, content_type, byte_size,
             utc_now_iso(), uploaded_by))
        return self.get(cursor.lastrowid)

    def get(self, document_id):
        row = self.database.query_one(
            "SELECT * FROM staff_documents WHERE document_id = ?",
            (document_id,))
        return dict(row) if row else None

    def list_for_user(self, user_id):
        rows = self.database.query(
            "SELECT * FROM staff_documents WHERE user_id = ? "
            "ORDER BY uploaded_at DESC", (user_id,))
        return [dict(row) for row in rows]

    def delete(self, document_id):
        cursor = self.database.execute(
            "DELETE FROM staff_documents WHERE document_id = ?",
            (document_id,))
        return cursor.rowcount > 0
