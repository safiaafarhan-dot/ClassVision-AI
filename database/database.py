"""
ClassVision AI - Database (Step 6)
----------------------------------
Connection handling and schema for the attendance store.

    Step 6 introduces deterministic attendance rules and SQLite persistence.

Why SQLite
    It is in the Python standard library, so it adds NO dependency, and it is
    a real relational database with foreign keys, constraints and
    transactions. Everything written here is ordinary SQL, so moving to
    PostgreSQL later is a change of driver and dialect, not a redesign.

What this layer is, and is not
    It stores decisions. It does not make them. The attendance rule engine in
    ai/attendance_rules.py runs perfectly well with no database at all, and it
    must stay that way - the moment rules start reading from tables, policy
    changes stop being replayable.

Schema versions
    1  attendance: students, sessions, expected students, records, segments.
    2  the student REGISTRY (roll number, class and section move here from the
       embedding store, so a plain SELECT answers who a student is) and
       ACCOUNTS (users, class assignments, sign-in sessions, audit log).
    3  the ACADEMIC lifecycle (academic_sessions, student_academic_history, so
       a student is registered once and their class/section/roll become facts
       about a term) and OPERATIONS (notification_rules, notifications,
       model_registry, cameras), plus parent contact on students and the
       lifecycle columns on attendance_sessions.

    Migration is additive only - see migrate() below and
    database/migrate_students.py. No statement in this project drops a table,
    deletes a row, or resets anything.

    Tables are created first, then migrate() adds columns, then INDEXES runs -
    in that order, because an index on a column version 3 added cannot be
    created until the column exists on an upgraded database.

Privacy
    This database NEVER contains face images, face crops, video, or embedding
    vectors. Biometric data stays in the Step 3 embedding store, keyed by the
    same student_id. Attendance tables reference a student_id and nothing more.

    The users table stores PBKDF2 password HASHES, never a password; the
    sessions table stores token hashes, never a token.

Where the file lives
    data/classvision.db by default. data/ is gitignored, so the database can
    never be committed. Tests always pass their own temporary path and never
    touch this file.
"""

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DATABASE_PATH = DATA_DIR / "classvision.db"

SCHEMA_VERSION = 7

# Kept in step with ai/attendance_rules.py and ai/attendance_session.py.
# Duplicated here as SQL CHECK constraints so the database refuses bad data
# even if it is written by something other than this project's Python code.
ATTENDANCE_STATUSES = ("PRESENT", "LATE", "PARTIAL", "LEFT_EARLY",
                       "ABSENT", "UNKNOWN", "MANUAL_OVERRIDE")
SESSION_STATUSES = ("SCHEDULED", "ACTIVE", "COMPLETED", "CANCELLED")
STUDENT_STATUSES = ("ACTIVE", "INACTIVE", "TRANSFERRED", "GRADUATED")

# Where an outbound item is in its life. PENDING and FAILED are both "still
# here" - the difference is whether delivery has been tried and refused - and
# only DELIVERED means somewhere else has acknowledged it. There is no
# "probably sent".
SYNC_STATUSES = ("PENDING", "FAILED", "DELIVERED", "ABANDONED")

# Who may sign in to the STAFF side, and what they may do. Two roles: the
# project has administrators and teachers, and inventing a permission matrix
# for roles nobody has asked for would be architecture for its own sake.
#
# A student is deliberately NOT a third value here. See STUDENT ACCOUNTS
# below - students sign in against their own table, and the two reasons are
# worth stating because the obvious design is the other one:
#
#   1. This CHECK constraint cannot gain a value without rebuilding the users
#      table, and no migration in this project drops a table. The same
#      reasoning is already written out above SESSION_EXTRA_COLUMNS, where
#      correction_state was given its own column rather than widening the
#      session status CHECK.
#
#   2. A student credential that is not in `users` cannot be handed a staff
#      permission by a bug in staff role handling, because every staff guard
#      starts from a row this table does not contain. The separation is a
#      property of the schema rather than of the code that reads it.
USER_ROLES = ("SUPER_ADMIN", "TEACHER")
USER_STATUSES = ("ACTIVE", "INACTIVE")

# Staff profile detail added in version 4: a photograph and uploaded
# documents for a teacher's personnel record.
#
# The photo is a FILE on disk with its name in this column, never bytes in
# the database - the same rule the rest of the project follows, and the
# reason `data/classvision.db` can still be copied for inspection without
# carrying anybody's image with it. It is a personnel photograph and NOT a
# biometric record: nothing reads it, no embedding is computed from it, and
# the recognition pipeline never opens it.
STAFF_PROFILE_COLUMNS = (
    ("photo_name", "TEXT NOT NULL DEFAULT ''"),
    ("phone", "TEXT NOT NULL DEFAULT ''"),
    ("email", "TEXT NOT NULL DEFAULT ''"),
    # Whether this teacher may SEE every student, rather than only the
    # classes they are assigned to.
    #
    # An explicit grant rather than a change to how scoping works. A school
    # that wants its teachers to see the whole roster turns this on per
    # account and the audit log records who did; a school that does not keeps
    # the existing behaviour untouched. Defaulting to 0 means no existing
    # account silently gains visibility when this column arrives.
    #
    # It widens READING only. Class scope still governs what a teacher may
    # change, because seeing a name and editing a record are different acts.
    ("can_view_all_students", "INTEGER NOT NULL DEFAULT 0"),
)

# Camera columns added in version 4.
#
# pipeline_state is what the DEVICE last reported about the processing it is
# feeding - "RUNNING", "IDLE", "FAILED". It is deliberately separate from
# `status`, which is about whether the device answered at all: a camera can be
# perfectly online and feeding a pipeline that has stopped, and a school
# debugging a blank classroom view needs to tell those two apart.
#
# registered_by records who added the device. created_at already records when.
CAMERA_EXTRA_COLUMNS = (
    ("pipeline_state", "TEXT NOT NULL DEFAULT ''"),
    ("registered_by", "TEXT NOT NULL DEFAULT ''"),
    ("last_session_id", "TEXT"),
)

# A student portal account's states. The same two words the staff table uses,
# kept as their own name so the two can diverge without a silent change of
# meaning in whichever table did not move.
STUDENT_ACCOUNT_STATUSES = ("ACTIVE", "INACTIVE")

# Student registry metadata that used to live ONLY in the embedding store.
# Added in schema version 2 so a plain SELECT on students answers "who is this
# person" without a second store having to be opened. See migrate_students.py
# for how existing rows are filled in.
STUDENT_METADATA_COLUMNS = ("roll_number", "class_name", "section")

# A student's lifecycle. Version 3 adds the two states a school actually
# needs beyond "here" and "not here": somebody who moved away, and somebody
# who finished. Both keep every attendance record they ever earned.
STUDENT_LIFECYCLE_STATUSES = ("ACTIVE", "INACTIVE", "TRANSFERRED", "GRADUATED")

# Parent/guardian contact, added in version 3. Held on the student row
# because it is contact detail, not biometric data, and protected by the same
# role checks as everything else - see backend/services/authz.py.
STUDENT_CONTACT_COLUMNS = (
    ("parent_name", "TEXT NOT NULL DEFAULT ''"),
    ("parent_phone", "TEXT NOT NULL DEFAULT ''"),
    ("parent_email", "TEXT NOT NULL DEFAULT ''"),
    ("notification_enabled", "INTEGER NOT NULL DEFAULT 1"),
)

# Version 7. The academic year, on the student row.
#
# Additive, defaulted, and BACKFILLED from the history table rather than left
# blank - see backfill_academic_year. A school that has been running terms
# already knows which year each student is in, and making them re-enter it
# would be asking for data the database is already holding.
STUDENT_YEAR_COLUMNS = (
    ("academic_year", "TEXT NOT NULL DEFAULT ''"),
)

# Classroom-session columns added in version 3.
#
# correction_state is deliberately NOT a session status. A session that ran
# and ended is ENDED, and it stays ENDED even when somebody notices the time
# was wrong - what happened, happened. Needing correction is a separate fact
# ABOUT that record, so it gets its own column rather than overwriting the
# history. It also means the status CHECK constraint never has to change,
# which would have meant rebuilding the table every attendance record in the
# system points at.
SESSION_EXTRA_COLUMNS = (
    ("academic_session_id", "TEXT"),
    ("section", "TEXT NOT NULL DEFAULT ''"),
    ("teacher", "TEXT NOT NULL DEFAULT ''"),
    ("actual_start", "TEXT"),
    ("actual_end", "TEXT"),
    ("cancelled_at", "TEXT"),
    ("cancelled_by", "TEXT"),
    ("cancellation_reason", "TEXT"),
    ("correction_state", "TEXT NOT NULL DEFAULT 'NONE'"),
    ("correction_reason", "TEXT"),
    ("correction_at", "TEXT"),
    ("correction_by", "TEXT"),
)

CORRECTION_NONE = "NONE"
CORRECTION_REQUIRED = "CORRECTION_REQUIRED"
CORRECTION_DONE = "CORRECTED"

ACADEMIC_SESSION_STATUSES = ("PLANNED", "ACTIVE", "CLOSED")

NOTIFICATION_STATUSES = ("PENDING", "SENT", "FAILED", "SUPPRESSED")


def _quoted(values):
    return ", ".join("'%s'" % value for value in values)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- The student REGISTRY. One row per person, and the only place their
-- identifying metadata lives. Face embeddings are NOT here and never will be:
-- they stay in the Step 3 store, keyed by the same student_id.
CREATE TABLE IF NOT EXISTS students (
    student_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    roll_number TEXT NOT NULL DEFAULT '',
    class_name  TEXT NOT NULL DEFAULT '',
    section     TEXT NOT NULL DEFAULT '',

    -- Version 7. The current academic year, mirrored from
    -- student_academic_history exactly as the three columns above it are.
    -- '' means "no term recorded for this student yet", which is a real
    -- state for a student registered before anybody created a term - and is
    -- different from a student whose year is genuinely unknown.
    academic_year TEXT NOT NULL DEFAULT '',

    status      TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN (%(student_statuses)s)),

    -- Version 3. Contact detail, not biometric data, and protected by the
    -- same role checks as everything else.
    parent_name  TEXT NOT NULL DEFAULT '',
    parent_phone TEXT NOT NULL DEFAULT '',
    parent_email TEXT NOT NULL DEFAULT '',
    notification_enabled INTEGER NOT NULL DEFAULT 1
                CHECK (notification_enabled IN (0, 1)),

    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_sessions (
    session_id      TEXT PRIMARY KEY,
    class_id        TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    status          TEXT NOT NULL
                    CHECK (status IN (%(session_statuses)s)),
    policy_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,

    -- Version 3. Which term this lesson belongs to, who taught it, when it
    -- actually ran, and the audit trail for cancelling or correcting it.
    --
    -- correction_state is a column rather than a status on purpose: a
    -- session that ran and ended is ENDED, and noticing afterwards that its
    -- time was wrong does not change what happened. Keeping it separate also
    -- means the status CHECK never changes, so the table every attendance
    -- record points at never has to be rebuilt.
    academic_session_id TEXT,
    section         TEXT NOT NULL DEFAULT '',
    teacher         TEXT NOT NULL DEFAULT '',
    actual_start    TEXT,
    actual_end      TEXT,
    cancelled_at    TEXT,
    cancelled_by    TEXT,
    cancellation_reason TEXT,
    correction_state TEXT NOT NULL DEFAULT 'NONE',
    correction_reason TEXT,
    correction_at   TEXT,
    correction_by   TEXT,

    CHECK (end_time > start_time)
);

-- Who was supposed to be in the session. Absence is only meaningful
-- against this list, which is why it is stored rather than inferred.
CREATE TABLE IF NOT EXISTS session_expected_students (
    session_id  TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    PRIMARY KEY (session_id, student_id),
    FOREIGN KEY (session_id) REFERENCES attendance_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attendance_records (
    attendance_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    student_id      TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN (%(attendance_statuses)s)),
    first_seen      TEXT,
    last_seen       TEXT,
    present_seconds REAL NOT NULL DEFAULT 0
                    CHECK (present_seconds >= 0),
    session_seconds REAL NOT NULL DEFAULT 0
                    CHECK (session_seconds >= 0),
    presence_ratio  REAL NOT NULL DEFAULT 0
                    CHECK (presence_ratio >= 0 AND presence_ratio <= 1),
    segment_count   INTEGER NOT NULL DEFAULT 0
                    CHECK (segment_count >= 0),
    loss_periods    INTEGER NOT NULL DEFAULT 0
                    CHECK (loss_periods >= 0),
    was_late        INTEGER NOT NULL DEFAULT 0 CHECK (was_late IN (0, 1)),
    left_early      INTEGER NOT NULL DEFAULT 0 CHECK (left_early IN (0, 1)),
    reason          TEXT NOT NULL,
    policy_version  TEXT NOT NULL,

    -- Manual override audit trail. NULL until a human changes the result.
    is_override     INTEGER NOT NULL DEFAULT 0 CHECK (is_override IN (0, 1)),
    original_status TEXT
                    CHECK (original_status IS NULL
                           OR original_status IN (%(attendance_statuses)s)),
    override_reason TEXT,
    overridden_by   TEXT,
    overridden_at   TEXT,

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    -- One result per student per session PER POLICY. Re-evaluating with the
    -- same policy updates in place; a new policy version creates a new row,
    -- so history is preserved rather than overwritten.
    UNIQUE (session_id, student_id, policy_version),

    FOREIGN KEY (session_id) REFERENCES attendance_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,

    -- An override must carry its audit trail.
    CHECK (is_override = 0 OR (original_status IS NOT NULL
                               AND override_reason IS NOT NULL
                               AND overridden_at IS NOT NULL))
);

-- The presence evidence a decision was derived from, kept so attendance can
-- be recomputed under a different policy without re-running the camera.
CREATE TABLE IF NOT EXISTS presence_segments (
    segment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    student_id       TEXT NOT NULL,
    track_id         INTEGER,
    start_time       TEXT NOT NULL,
    end_time         TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    created_at       TEXT NOT NULL,

    UNIQUE (session_id, student_id, start_time, end_time),

    FOREIGN KEY (session_id) REFERENCES attendance_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,

    CHECK (end_time >= start_time)
);

-- Sign-in accounts. Added in schema version 2, alongside the first
-- authentication this project has ever had.
--
-- password_hash is a PBKDF2-HMAC-SHA256 digest with a per-user salt (see
-- backend/services/auth_service.py). A plaintext password is never written
-- here, never logged, and never returned by any endpoint.
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN (%(user_roles)s)),
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ACTIVE'
                  CHECK (status IN (%(user_statuses)s)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT,
    created_by    TEXT
);

-- Which classes a teacher is responsible for. A teacher with no row here is
-- unrestricted within their role; a teacher WITH rows sees only those classes.
CREATE TABLE IF NOT EXISTS user_class_assignments (
    user_id    INTEGER NOT NULL,
    class_name TEXT NOT NULL,
    section    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, class_name, section),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Live sign-in sessions. Only the SHA-256 of a token is stored, so reading
-- this table does not let anybody sign in as somebody else.
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- What was done, by whom, and when. Deliberately holds no password, no
-- embedding and no biometric data of any kind - only that an action happened.
CREATE TABLE IF NOT EXISTS audit_log (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT,
    actor_role TEXT,
    action     TEXT NOT NULL,
    subject    TEXT,
    detail     TEXT
);

-- ACADEMIC SESSIONS: a year, and a semester within it.
--
-- Separate from attendance_sessions, which are single lessons. The names are
-- close and the things are not: one is "2026-27, semester 6", the other is
-- "Tuesday's 10 o'clock".
CREATE TABLE IF NOT EXISTS academic_sessions (
    academic_session_id TEXT PRIMARY KEY,
    academic_year       TEXT NOT NULL,
    semester            TEXT NOT NULL DEFAULT '',
    start_date          TEXT,
    end_date            TEXT,
    status              TEXT NOT NULL DEFAULT 'PLANNED'
                        CHECK (status IN (%(academic_statuses)s)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (academic_year, semester)
);

-- WHERE A STUDENT WAS, AND WHEN.
--
-- Roll number, class, section and semester all change; a student does not.
-- Version 2 put those four on the student row, which was right for "who is
-- this person today" and wrong for "who were they last year". They stay on
-- the student row as the CURRENT values - that is what every existing query
-- reads - and this table records each period separately so the previous one
-- survives a promotion.
--
-- is_current marks the row the registry mirrors. Exactly one per student, and
-- promote() is the only thing that moves it.
CREATE TABLE IF NOT EXISTS student_academic_history (
    academic_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id          TEXT NOT NULL,
    academic_session_id TEXT,
    academic_year       TEXT NOT NULL DEFAULT '',
    semester            TEXT NOT NULL DEFAULT '',
    class_name          TEXT NOT NULL DEFAULT '',
    section             TEXT NOT NULL DEFAULT '',
    roll_number         TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    start_date          TEXT,
    end_date            TEXT,
    is_current          INTEGER NOT NULL DEFAULT 1
                        CHECK (is_current IN (0, 1)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,

    FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,
    FOREIGN KEY (academic_session_id)
        REFERENCES academic_sessions(academic_session_id) ON DELETE SET NULL,

    -- One enrolment per student per academic session. A promotion into a
    -- session they are already in is an UPDATE, never a second row.
    UNIQUE (student_id, academic_session_id)
);

-- WHEN TO TELL A PARENT SOMETHING.
--
-- Rules are rows, not constants. A school that wants 60%% instead of 75%%
-- changes a row; nothing in the engine knows any particular threshold.
CREATE TABLE IF NOT EXISTS notification_rules (
    rule_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    rule_type      TEXT NOT NULL,
    threshold      REAL,
    window_days    INTEGER,
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    class_name     TEXT NOT NULL DEFAULT '',
    section        TEXT NOT NULL DEFAULT '',
    message        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- WHAT WAS SENT, TO WHOM, AND WHETHER IT ARRIVED.
--
-- Holds no message body containing anything biometric, and no parent contact
-- detail - the recipient is recorded as a KIND (parent/teacher/admin), and
-- the address is looked up at send time from the student row. A delivery log
-- that accumulates phone numbers is a breach waiting to be copied.
CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      TEXT NOT NULL,
    rule_id         TEXT,
    notification_type TEXT NOT NULL,
    recipient_kind  TEXT NOT NULL DEFAULT 'parent',
    subject         TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    measured_value  REAL,
    threshold       REAL,
    period_start    TEXT,
    period_end      TEXT,
    generated_at    TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (delivery_status IN (%(notification_statuses)s)),
    delivered_at    TEXT,
    failure_reason  TEXT,

    FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,
    FOREIGN KEY (rule_id) REFERENCES notification_rules(rule_id)
        ON DELETE SET NULL
);

-- RECOGNITION MODELS, AND WHICH ONE IS IN PRODUCTION.
--
-- SFace is the baseline and stays the baseline. A custom model is recorded
-- here with its metrics and cannot become ACTIVE without an explicit
-- activation - see ai/model_registry.py.
CREATE TABLE IF NOT EXISTS model_registry (
    model_id       TEXT PRIMARY KEY,
    model_name     TEXT NOT NULL,
    version        TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'experimental',
    status         TEXT NOT NULL DEFAULT 'TESTING',
    dataset_version TEXT,
    metrics        TEXT,
    notes          TEXT,
    trained_at     TEXT,
    activated_at   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (model_name, version)
);

-- CAMERAS, AND WHETHER THEY ARE ANSWERING.
--
-- Operational only. A camera row says where a device is and when it last
-- delivered a frame; it says nothing about any person it has ever seen.
CREATE TABLE IF NOT EXISTS cameras (
    camera_id      TEXT PRIMARY KEY,
    label          TEXT NOT NULL DEFAULT '',
    classroom      TEXT NOT NULL DEFAULT '',
    camera_index   INTEGER,
    status         TEXT NOT NULL DEFAULT 'unknown',
    last_heartbeat TEXT,
    last_fps       REAL,
    last_latency_ms REAL,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- STUDENT ACCOUNTS: the portal login a student uses to see their OWN
-- attendance, and nothing else.
--
-- Separate from `users` on purpose, and the separation is the security
-- property rather than a filing preference. Every staff permission in
-- backend/services/authz.py begins by resolving a row in `users`; a student
-- credential lives in a table those guards never read, so no bug in role
-- handling can turn a portal login into a teacher. The reverse holds too -
-- a staff account cannot appear in the portal.
--
-- The row is keyed on student_id, so an account IS a student: there is no
-- way to create a portal login for somebody who is not in the registry, and
-- deleting a student takes their login with them rather than leaving a
-- credential pointing at nobody.
--
-- Credentials are PBKDF2 hashes from backend/services/auth_service.py - the
-- same function the staff side uses. There is no second password
-- implementation in this project.
CREATE TABLE IF NOT EXISTS student_accounts (
    student_id    TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ACTIVE'
                  CHECK (status IN (%(student_account_statuses)s)),
    must_change   INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT,
    created_by    TEXT,
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

-- Live portal sign-ins. Only the SHA-256 of a token is stored, exactly as
-- user_sessions does it, so reading this table does not let anybody sign in
-- as a student.
CREATE TABLE IF NOT EXISTS student_sessions (
    token_hash TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    FOREIGN KEY (student_id) REFERENCES student_accounts(student_id)
        ON DELETE CASCADE
);

-- A teacher's personnel documents: an ID scan, a certificate, a contract.
--
-- The FILE lives under data/staff/, and this row records what it is and who
-- uploaded it. Nothing here is biometric and nothing here is read by the
-- recognition pipeline; a stored_name is a random filename, so guessing a
-- URL does not produce somebody's identity document.
CREATE TABLE IF NOT EXISTS staff_documents (
    document_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    filename     TEXT NOT NULL,
    stored_name  TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL DEFAULT '',
    byte_size    INTEGER NOT NULL DEFAULT 0,
    uploaded_at  TEXT NOT NULL,
    uploaded_by  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- BREAKS (schema version 6).
--
-- A period inside a lesson that does not count towards attendance.
--
-- Why this is in the database rather than in the browser: the rule engine
-- divides present time by session length, so a ten-minute break in a
-- sixty-minute lesson lowers every student's ratio by a sixth unless the
-- interval is subtracted from BOTH sides of that division. A countdown in a
-- browser cannot reach the arithmetic, and a student must not be marked
-- PARTIAL because the class was told to stand up and stretch.
--
-- `ends_at` is when the break is DUE to end - ten minutes after it started,
-- fixed by the server when it starts. `ended_at` is when it actually ended,
-- and is null while the break is running; a break ended early carries an
-- earlier `ended_at` and the attendance arithmetic uses that. The excluded
-- interval is always what happened, never what was planned.
CREATE TABLE IF NOT EXISTS session_breaks (
    break_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    ended_at    TEXT,
    started_by  TEXT,
    ended_by    TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES attendance_sessions(session_id)
        ON DELETE CASCADE
);

-- THE OUTBOUND QUEUE (schema version 5).
--
-- What this table is for: a classroom appliance that recorded a lesson while
-- the network was down must not lose it, and must not send it twice when the
-- network comes back.
--
-- Each row is a complete, self-contained envelope - the payload is the record
-- as JSON, not a pointer to a row that might be edited before it is sent, so
-- what eventually leaves is what was true when it was queued.
--
-- `item_key` is the IDEMPOTENCY key and it is UNIQUE. It is derived from what
-- the item IS - its kind and its natural identity - and not from when it was
-- queued, so queueing the same attendance decision twice updates one row
-- rather than creating a second delivery of the same fact.
--
-- `attempts` and `last_error` exist so that a queue which is not draining
-- says WHY. A queue that silently stops is worse than no queue: the appliance
-- looks healthy and the term of attendance never arrives.
--
-- Nothing here is biometric. The payloads are attendance decisions, presence
-- summaries and session metadata - the same things the reports already
-- export - and a test asserts no embedding, crop or credential can enter.
CREATE TABLE IF NOT EXISTS sync_queue (
    queue_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key     TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN (%(sync_statuses)s)),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    queued_at    TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    delivered_at TEXT
);

""" % {
    "student_statuses": _quoted(STUDENT_STATUSES),
    "session_statuses": _quoted(SESSION_STATUSES),
    "attendance_statuses": _quoted(ATTENDANCE_STATUSES),
    "user_roles": _quoted(USER_ROLES),
    "user_statuses": _quoted(USER_STATUSES),
    "academic_statuses": _quoted(ACADEMIC_SESSION_STATUSES),
    "notification_statuses": _quoted(NOTIFICATION_STATUSES),
    "student_account_statuses": _quoted(STUDENT_ACCOUNT_STATUSES),
    "sync_statuses": _quoted(SYNC_STATUSES),
}

# Indexes, in their own script and applied AFTER migrate().
#
# Why they are not in SCHEMA: three of these index a column that version 3
# added - academic_session_id on attendance_sessions, class_name on students.
# On a NEW database the CREATE TABLE above includes those columns and an index
# on them is fine. On an EXISTING version-2 database the table already exists,
# CREATE TABLE IF NOT EXISTS does nothing, and the column does not arrive until
# migrate() runs - so creating the index in the same script failed with
# "no such column: academic_session_id" and took the whole upgrade with it.
#
# Every real upgrade hit that and no fresh-database test could, which is the
# shape of bug worth a comment this long.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_history_student
    ON student_academic_history(student_id, is_current);
CREATE INDEX IF NOT EXISTS idx_history_session
    ON student_academic_history(academic_session_id);
CREATE INDEX IF NOT EXISTS idx_notifications_student
    ON notifications(student_id, generated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_academic
    ON attendance_sessions(academic_session_id);
CREATE INDEX IF NOT EXISTS idx_students_class
    ON students(class_name, section);
-- Version 7. The registry's filter, which is asked year-first.
--
-- The index above already serves "class", "class + section" and a
-- section-only scan; this one serves "year", "year + class" and "year +
-- class + section", which are the combinations a year-leading filter puts
-- in front of SQLite. Section-only still scans, and deliberately: an index
-- whose leading column is a single letter shared by a third of the school
-- would be read and then discarded.
CREATE INDEX IF NOT EXISTS idx_students_year_class_section
    ON students(academic_year, class_name, section);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user
    ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_at
    ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_attendance_session
    ON attendance_records(session_id);
CREATE INDEX IF NOT EXISTS idx_attendance_student
    ON attendance_records(student_id);
CREATE INDEX IF NOT EXISTS idx_segments_session_student
    ON presence_segments(session_id, student_id);
CREATE INDEX IF NOT EXISTS idx_sessions_class_date
    ON attendance_sessions(class_id, session_date);
CREATE INDEX IF NOT EXISTS idx_student_sessions_student
    ON student_sessions(student_id);
CREATE INDEX IF NOT EXISTS idx_staff_documents_user
    ON staff_documents(user_id);
CREATE INDEX IF NOT EXISTS idx_sync_queue_status
    ON sync_queue(status, queue_id);
CREATE INDEX IF NOT EXISTS idx_session_breaks_session
    ON session_breaks(session_id, started_at);
""" % {
    "student_statuses": _quoted(STUDENT_STATUSES),
    "session_statuses": _quoted(SESSION_STATUSES),
    "attendance_statuses": _quoted(ATTENDANCE_STATUSES),
    "user_roles": _quoted(USER_ROLES),
    "user_statuses": _quoted(USER_STATUSES),
    "academic_statuses": _quoted(ACADEMIC_SESSION_STATUSES),
    "notification_statuses": _quoted(NOTIFICATION_STATUSES),
    "student_account_statuses": _quoted(STUDENT_ACCOUNT_STATUSES),
    "sync_statuses": _quoted(SYNC_STATUSES),
}


# ---------------------------------------------------------------------------
# Identity, enforced by the database rather than by the code above it
# ---------------------------------------------------------------------------
#
# A username is an AUTHENTICATION IDENTITY. Two accounts that share one are
# not a tidiness problem, they are two people who can sign in as each other,
# and an application-level "is this taken?" cannot prevent it: two requests
# can both read "no" before either writes.
#
# So the constraint is the protection and the check above it is only a better
# error message. users.username and student_accounts.username are already
# declared UNIQUE in SCHEMA, which handles the exact-match case.
#
# WHAT THESE TWO INDEXES ADD is case-insensitive uniqueness, and they exist
# because of a real mismatch rather than a hypothetical one:
# UserRepository.get_by_username looks a staff account up with COLLATE
# NOCASE - deliberately, because somebody typing their own name in a
# different case is not a different person - while the column's UNIQUE
# constraint is case-SENSITIVE. "rahul" and "Rahul" therefore both fit in the
# table, and the lookup then has two rows to choose from and returns one of
# them. That is ambiguous authentication, which is the one thing this whole
# area exists to prevent.
#
# NOT IN `INDEXES` above, and not in SCHEMA, for one reason: unlike every
# other statement in this file, a UNIQUE index can FAIL on a database that is
# otherwise perfectly fine - an existing file may already hold the pair this
# is meant to forbid. Creating it in the same executescript as the rest would
# take the whole upgrade down with it, on exactly the installations that most
# need to be told. See enforce_identity_uniqueness.
IDENTITY_INDEXES = (
    ("users", "username",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase "
     "ON users(lower(username))"),
    ("student_accounts", "username",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_student_accounts_username_nocase "
     "ON student_accounts(lower(username))"),
)


class Database:
    """
    A SQLite connection with the project's settings applied.

    Foreign keys are enforced explicitly: SQLite leaves them OFF by default,
    per connection, so forgetting this would silently allow orphaned
    attendance rows.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_DATABASE_PATH
        self._connection = None

    @property
    def connection(self):
        if self._connection is None:
            self.connect()
        return self._connection

    def connect(self):
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(self.path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,          # explicit transactions only
        )
        self._connection.row_factory = sqlite3.Row

        # Off by default in SQLite, and per-connection. Without this, the
        # foreign keys declared above would be decorative.
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")

        return self._connection

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def initialize(self):
        """
        Create the schema, then bring an older one forward.

        Safe to run repeatedly: every CREATE is IF NOT EXISTS, and the
        migration below only ever ADDS. No statement in this method drops a
        table, deletes a row, or resets anything, and that is not an
        accident - a database holding a term's attendance must never be
        recoverable-from rather than simply correct.
        """
        connection = self.connection
        connection.executescript(SCHEMA)
        # Columns first, then the indexes that may depend on them. See the
        # comment on INDEXES: the order is the whole point.
        self.migrate()
        connection.executescript(INDEXES)
        # Separately, and allowed to decline. See IDENTITY_INDEXES.
        self.enforce_identity_uniqueness()
        connection.execute(
            "INSERT INTO schema_info (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),))
        return self

    def column_names(self, table):
        """The columns a table actually has, whatever the schema says."""
        rows = self.connection.execute(
            "PRAGMA table_info(%s)" % table).fetchall()
        return [row[1] for row in rows]

    def migrate(self):
        """
        Bring an existing database up to the current schema. Additive only.

        Version 1 stored a student as an id, a name and a status; roll number,
        class and section lived in the Step 3 embedding store, so a plain
        SELECT on students could not answer who a person was. Version 2 adds
        those three columns to the REGISTRY, which is where identifying
        metadata belongs.

        Version 3 adds parent/guardian contact to the student row and the
        classroom-session lifecycle columns (academic session, section,
        teacher, actual start/end, cancellation and correction detail) to
        attendance_sessions.

        Version 4 adds a photograph filename and contact detail to a staff
        account, and three new tables - student_accounts, student_sessions
        and staff_documents. It changes no existing constraint: the student
        portal gets its own credential table precisely so that the users
        role CHECK does not have to be rebuilt.

        Version 5 adds ONE table, sync_queue, and touches nothing else. There
        is no ALTER for it at all: a new table arrives through the CREATE
        TABLE IF NOT EXISTS in SCHEMA, which is why upgrading to it cannot
        fail on a database already holding a term of attendance.

        Version 6 adds session_breaks the same way, for the same reason. No
        existing row changes and no existing column moves: a session that has
        never had a break has no row in it, and the attendance arithmetic for
        that session is unchanged down to the floating-point bit.

        ALTER TABLE ... ADD COLUMN is the one schema change SQLite performs
        without rewriting the table: existing rows keep their bytes and gain
        the default. Nothing is copied, nothing is dropped, and a database
        already at the current version comes through here untouched.

        Filling those new columns in for students enrolled before the change
        is a separate step, in database/migrate_students.py - it needs the
        embedding store, and this layer must not know that the embedding store
        exists.

        Returns the list of columns that were added, which is empty on every
        run after the first.
        """
        added = []

        # Migrating a table that is not there yet is not a failure. When
        # initialize() calls this, the CREATEs have already run, so every
        # table exists. Called on its own against a partial database - which
        # is what a genuinely old file can be - ALTER TABLE would raise
        # "no such table", so each block asks whether its table is there.
        tables = set(self.table_names())

        if "students" in tables:
            existing = set(self.column_names("students"))

            for column in STUDENT_METADATA_COLUMNS:
                if column not in existing:
                    # NOT NULL needs a default, and '' is the honest one: the
                    # teacher has not told us the roll number yet, which is
                    # different from the student not having one.
                    self.connection.execute(
                        "ALTER TABLE students ADD COLUMN %s TEXT NOT NULL "
                        "DEFAULT ''" % column)
                    added.append(column)

            # Version 3. Contact detail on the student, lifecycle detail on
            # the classroom session. Additive in exactly the same way: every
            # existing row keeps its bytes and gains the default.
            for column, declaration in STUDENT_CONTACT_COLUMNS:
                if column not in existing:
                    self.connection.execute(
                        "ALTER TABLE students ADD COLUMN %s %s"
                        % (column, declaration))
                    added.append(column)

            # Version 7. The academic year, and then the one derived UPDATE
            # in this method - see backfill_academic_year for why it is safe
            # to run and why it is run only on the pass that adds the column.
            for column, declaration in STUDENT_YEAR_COLUMNS:
                if column not in existing:
                    self.connection.execute(
                        "ALTER TABLE students ADD COLUMN %s %s"
                        % (column, declaration))
                    added.append(column)
                    self.backfill_academic_year()

        if "attendance_sessions" in tables:
            session_columns = set(self.column_names("attendance_sessions"))
            for column, declaration in SESSION_EXTRA_COLUMNS:
                if column not in session_columns:
                    self.connection.execute(
                        "ALTER TABLE attendance_sessions ADD COLUMN %s %s"
                        % (column, declaration))
                    added.append("attendance_sessions.%s" % column)

        # Version 4. A teacher's personnel detail: a photograph filename and
        # the two contact fields a school needs in order to reach staff.
        #
        # Additive in exactly the same way, and note what is NOT here: the
        # role CHECK constraint is untouched, because widening it would mean
        # rebuilding the table every session token points at. Students sign
        # in against student_accounts instead.
        if "users" in tables:
            user_columns = set(self.column_names("users"))
            for column, declaration in STAFF_PROFILE_COLUMNS:
                if column not in user_columns:
                    self.connection.execute(
                        "ALTER TABLE users ADD COLUMN %s %s"
                        % (column, declaration))
                    added.append("users.%s" % column)

        if "cameras" in tables:
            camera_columns = set(self.column_names("cameras"))
            for column, declaration in CAMERA_EXTRA_COLUMNS:
                if column not in camera_columns:
                    self.connection.execute(
                        "ALTER TABLE cameras ADD COLUMN %s %s"
                        % (column, declaration))
                    added.append("cameras.%s" % column)

        return added

    def enforce_identity_uniqueness(self):
        """
        Make the database refuse two accounts with one authentication identity.

        Case-insensitive uniqueness on the two username columns, applied one
        index at a time and only when the data already satisfies it.

        WHY IT ASKS FIRST. A database written before this rule existed may
        already contain "rahul" and "Rahul". Creating the index would raise,
        and raising here would mean an installation with that pair could no
        longer open its own database - so instead the duplicate is FOUND,
        the index is skipped, and the pair is returned to be reported.

        Nothing is deleted, nothing is merged and no account is renamed. Which
        of two same-named accounts is the real one is a question about people,
        and this layer has no business guessing at it; a tool that resolves it
        can use what this returns.

        Returns a list of {table, column, value, count} for every identity
        that blocked its index, and an empty list on the normal path.
        """
        blocked = []

        for table, column, statement in IDENTITY_INDEXES:
            if table not in self.table_names():
                continue

            duplicates = self.connection.execute(
                "SELECT lower(%s) AS value, COUNT(*) AS count FROM %s "
                "GROUP BY lower(%s) HAVING COUNT(*) > 1"
                % (column, table, column)).fetchall()

            if duplicates:
                blocked.extend({"table": table, "column": column,
                                "value": row["value"], "count": row["count"]}
                               for row in duplicates)
                continue

            self.connection.execute(statement)

        return blocked

    def backfill_academic_year(self):
        """
        Fill the new column from the history the database already holds.

        The ONLY derived write in migrate(), and it is allowed because it
        invents nothing: every value copied here was already recorded by
        academic_service.enrol() on the row it marked is_current, which is
        the same row the registry's class and section were mirrored from. A
        student with no current enrolment keeps '' and is not guessed at.

        Writes only where the column is still blank, so re-running it cannot
        overwrite a year somebody has since corrected by hand. Returns the
        number of students filled in.
        """
        if "student_academic_history" not in self.table_names():
            return 0

        cursor = self.connection.execute(
            """
            UPDATE students
               SET academic_year = COALESCE((
                       SELECT h.academic_year
                         FROM student_academic_history h
                        WHERE h.student_id = students.student_id
                          AND h.is_current = 1
                        LIMIT 1), '')
             WHERE academic_year = ''
            """)
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def schema_version(self):
        row = self.connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else None

    def table_names(self):
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def foreign_keys_enabled(self):
        return bool(self.connection.execute("PRAGMA foreign_keys").fetchone()[0])

    # -- transactions ------------------------------------------------------

    def transaction(self):
        """
        Use as:  with database.transaction(): ...

        Commits on success, rolls back on any exception. Errors are never
        swallowed - a failed write must be visible, not quietly skipped.
        """
        return _Transaction(self.connection)

    def execute(self, sql, parameters=()):
        """Run one parameterised statement. Never build SQL by string joining."""
        return self.connection.execute(sql, parameters)

    def query(self, sql, parameters=()):
        return self.connection.execute(sql, parameters).fetchall()

    def query_one(self, sql, parameters=()):
        return self.connection.execute(sql, parameters).fetchone()


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.execute("BEGIN")
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.connection.execute("COMMIT")
        else:
            self.connection.execute("ROLLBACK")
        return False


def open_database(path=None, initialize=True):
    """Open (and by default create) the attendance database."""
    database = Database(path)
    database.connect()
    if initialize:
        database.initialize()
    return database
