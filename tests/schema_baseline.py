"""
The one authoritative list of tables ClassVision AI is allowed to have.

WHY THIS FILE EXISTS
    Several suites assert that their own step added no table. They each used
    to carry their own hardcoded copy of the table list, so every legitimate
    schema addition broke all of them at once and each copy had to be edited
    separately - which is exactly the situation where somebody eventually
    edits the assertion instead of thinking about it.

    The list lives here instead, written out by hand. It is NOT imported from
    database/database.py on purpose: if it were, the schema would be defining
    its own expectation and the guard would prove nothing. A new CREATE TABLE
    in the implementation still fails every suite that checks this list until
    a human adds the table name here deliberately.

WHAT EACH GROUP IS FOR
    Schema version 1 - the six attendance tables (Steps 1-6).
    Schema version 2 - the four authentication tables (accounts + RBAC).
    Schema version 3 - the six registry/academic/operations tables
                       (Step 11/11A/12: permanent student registry academic
                       history, academic sessions, parent notification rules
                       and history, recognition model registry, cameras).

    Steps 9 and 10 (liveness and authenticity) contribute NO table at all.
    Session scheduling contributes no table either - it writes to the
    existing attendance_sessions and session_expected_students.
"""

# Version 1: attendance core.
ATTENDANCE_TABLES = [
    "schema_info",
    "students",
    "attendance_sessions",
    "session_expected_students",
    "attendance_records",
    "presence_segments",
]

# Version 2: accounts and role-based access control.
AUTH_TABLES = [
    "users",
    "user_class_assignments",
    "user_sessions",
    "audit_log",
]

# Version 3: student registry, academic lifecycle and operations.
REGISTRY_TABLES = [
    "academic_sessions",
    "student_academic_history",
    "notification_rules",
    "notifications",
    "model_registry",
    "cameras",
]

# Version 4: the student portal and staff personnel records.
#
# student_accounts is a SECOND credential table, deliberately. A student
# portal login is not a row in `users` and never becomes one - see the
# comment above USER_ROLES in database/database.py.
PORTAL_TABLES = [
    "student_accounts",
    "student_sessions",
    "staff_documents",
]

# Version 5: the outbound queue for an appliance that was offline.
#
# One table, and it holds no new KIND of fact: every payload in it is a copy
# of something already in the attendance tables, queued for somewhere else.
SYNC_TABLES = [
    "sync_queue",
]

# Version 6: breaks inside a lesson.
#
# One table. It holds no new kind of fact about a person - only that a period
# of a lesson does not count towards attendance - and it is what lets the rule
# engine exclude that period instead of a browser pretending to.
BREAK_TABLES = [
    "session_breaks",
]

EXPECTED_TABLES = sorted(ATTENDANCE_TABLES + AUTH_TABLES + REGISTRY_TABLES
                         + PORTAL_TABLES + SYNC_TABLES + BREAK_TABLES)
