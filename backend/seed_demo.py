"""
ClassVision AI - Demo Data (Step 7)
-----------------------------------
Creates a SEPARATE demo database so the dashboard can be looked at before any
real session exists.

    THIS DATA IS FICTIONAL. The students are invented and the attendance
    figures are constructed. Nothing here is a real-world result.

Two safeguards, because seeding fake attendance into somebody's real records
would be a serious thing to do by accident:

    1. It writes to data/classvision-demo.db, NEVER to data/classvision.db.
       Pointing it at the real database is refused.
    2. It does nothing unless you explicitly ask, with --yes.

    python backend/seed_demo.py --yes
    uvicorn backend.app:app --reload --env-file /dev/null
        ... then set CLASSVISION_DB to the demo file, or simply inspect it:
    python backend/seed_demo.py --show

To view the demo data in the dashboard, start the backend with:

    CLASSVISION_DB=data/classvision-demo.db uvicorn backend.app:app

(on PowerShell: $env:CLASSVISION_DB="data/classvision-demo.db")
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend  # noqa: F401,E402

from attendance_rules import (  # noqa: E402
    AttendancePolicy,
    AttendanceRuleEngine,
    PresenceSegment,
)
from attendance_session import AttendanceSession  # noqa: E402
from database import DEFAULT_DATABASE_PATH, Database  # noqa: E402
from paths import enable_safe_console, relative  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)

DEMO_PATH = DEFAULT_DATABASE_PATH.parent / "classvision-demo.db"

# Invented people. Any resemblance to a real student is coincidental.
DEMO_STUDENTS = {
    "STU001": "Demo Student One",
    "STU002": "Demo Student Two",
    "STU003": "Demo Student Three",
    "STU004": "Demo Student Four",
    "STU005": "Demo Student Five",
}

CLASS_ID = "DEMO-10A"

# (day offset, {student: (start minute, end minute) or None for absent})
DEMO_SESSIONS = [
    (0, {"STU001": (0, 58), "STU002": (14, 59), "STU003": (0, 18),
         "STU004": None, "STU005": (2, 57)}),
    (1, {"STU001": (1, 59), "STU002": (3, 57), "STU003": (0, 25),
         "STU004": (12, 58), "STU005": None}),
    (2, {"STU001": (0, 57), "STU002": (16, 58), "STU003": None,
         "STU004": (1, 40), "STU005": (0, 59)}),
    (3, {"STU001": (2, 58), "STU002": (11, 59), "STU003": (0, 30),
         "STU004": None, "STU005": (0, 56)}),
]


def build(database):
    policy = AttendancePolicy(version="1.0", minimum_presence_ratio=0.75,
                              late_after_seconds=600,
                              early_leave_before_seconds=300)
    engine = AttendanceRuleEngine(policy)

    students = StudentRepository(database)
    sessions = SessionRepository(database)
    attendance = AttendanceRepository(database)

    for student_id, name in DEMO_STUDENTS.items():
        students.upsert(student_id, name)

    base = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
    created = 0

    for offset, plan in DEMO_SESSIONS:
        start = base + timedelta(days=offset)
        end = start + timedelta(hours=1)
        session_id = "DEMO-%s" % start.date().isoformat()

        session = AttendanceSession(session_id, CLASS_ID, start, end,
                                    policy_version=policy.version,
                                    expected_students=list(DEMO_STUDENTS))
        session.start()
        session.complete(completed_at=end)

        segments = []
        for index, (student_id, window) in enumerate(sorted(plan.items()), start=1):
            if window is None:
                continue
            first, last = window
            segments.append(PresenceSegment(
                student_id,
                start + timedelta(minutes=first),
                start + timedelta(minutes=last),
                track_id=index))

        decisions = engine.evaluate_session(session, segments,
                                            session.expected_students)

        sessions.save(session)
        attendance.save_segments(session_id, segments)
        attendance.save_decisions(decisions)
        created += 1

    return created


def show(database):
    counts = {}
    for table in database.table_names():
        counts[table] = database.query_one(
            "SELECT COUNT(*) AS total FROM %s" % table)["total"]

    print("  path : %s" % relative(database.path))
    for table, total in sorted(counts.items()):
        print("      %-28s %5d row(s)" % (table, total))


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Seed a SEPARATE demo database with fictional attendance. "
                    "Never writes to the real database.")
    parser.add_argument("--yes", action="store_true",
                        help="required: confirm you want fictional data created")
    parser.add_argument("--show", action="store_true",
                        help="report what the demo database contains")
    parser.add_argument("--path", default=str(DEMO_PATH),
                        help="demo database path (default: data/classvision-demo.db)")
    args = parser.parse_args()

    path = Path(args.path).resolve()

    if path == DEFAULT_DATABASE_PATH.resolve():
        print("REFUSED: that is the real database (%s)."
              % relative(DEFAULT_DATABASE_PATH))
        print("Demo data is fictional and must never be mixed with real records.")
        return 1

    database = Database(path)
    database.connect()
    database.initialize()

    try:
        if args.show:
            print("Demo database contents\n")
            show(database)
            return 0

        if not args.yes:
            print("This creates FICTIONAL attendance data for %d invented "
                  "students." % len(DEMO_STUDENTS))
            print("It writes to %s and never to the real database." % relative(path))
            print("\nRe-run with --yes to proceed.")
            return 1

        created = build(database)

        print("Seeded %d fictional sessions into %s\n" % (created, relative(path)))
        show(database)
        print("\nTHIS DATA IS FICTIONAL - invented students, constructed figures.")
        print("\nView it with:")
        print('    $env:CLASSVISION_DB="%s"' % relative(path))
        print("    uvicorn backend.app:app --reload")
        return 0

    finally:
        database.close()


if __name__ == "__main__":
    sys.exit(main())
