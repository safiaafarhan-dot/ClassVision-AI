"""
ClassVision AI - Analytics Service (Step 7)
-------------------------------------------
Deterministic attendance analytics, computed from stored records.

    No LLM, no inference, no invented explanations. Every number here is
    arithmetic over rows that Step 6 wrote, and every definition is stated
    below rather than left implicit.

Kept separate from HTTP routes and from the attendance rules on purpose: a
later reporting or assistant step can call these same functions and get the
same numbers, instead of recomputing them slightly differently.

DEFINITIONS - read these before trusting any figure
---------------------------------------------------

Which sessions count
    Only COMPLETED sessions. A SCHEDULED, ACTIVE or CANCELLED session is
    excluded entirely, because attendance is not final until a session is
    completed - that is the Step 6 rule and analytics must not quietly
    contradict it.

Which record counts, when several exist
    A session may hold records under more than one policy version. The record
    matching the SESSION'S policy_version is used; if none exists, the most
    recently created record is used. The number of distinct policy versions is
    always reported so a mixed dataset is visible rather than hidden.

"Satisfied the attendance rule"
    PRESENT, LATE and LEFT_EARLY all met the policy's duration and ratio
    requirements - they differ only in when the student arrived or left.
    PARTIAL and ABSENT did not meet the requirements. UNKNOWN is undecided.

        satisfying = {PRESENT, LATE, LEFT_EARLY}

Attendance rate
        completed sessions where the student's status is satisfying
        ----------------------------------------------------------
        completed sessions where that student was EXPECTED

    The denominator is expectation, not appearance. A student who was never
    expected is not counted, and a student who was expected but never seen
    counts against the rate.

Rates for late / partial / early-leave / absence
    Same denominator: completed sessions where the student was expected.

Overridden records
    The effective status is used - the status a human set. The original
    computed status is preserved in the database and reported separately so an
    overridden figure is never mistaken for a measured one.

NO DATA versus REAL ZERO
    A rate over zero sessions is NOT zero; it is undefined. Those cases return
    None with a sample size of 0, so the dashboard can say "no data" instead of
    showing a confident 0%.
"""

from datetime import datetime

# Statuses whose students met the policy's presence requirements.
SATISFYING_STATUSES = ("PRESENT", "LATE", "LEFT_EARLY")

# Every status the dashboard counts, in display order.
COUNTED_STATUSES = ("PRESENT", "LATE", "PARTIAL", "LEFT_EARLY", "ABSENT",
                    "UNKNOWN", "MANUAL_OVERRIDE")

COMPLETED = "COMPLETED"

# Below this many completed sessions, a change between sessions is not called
# a trend. Two points make a comparison, not a pattern.
MINIMUM_SESSIONS_FOR_TREND = 3


def _ratio(numerator, denominator):
    """
    A rate, or None when there is nothing to divide by.

    Returning None rather than 0.0 is the difference between "no data" and
    "genuinely zero", and the dashboard shows those differently.
    """
    if not denominator:
        return None
    return numerator / denominator


def _round(value, digits=4):
    return None if value is None else round(value, digits)


class AnalyticsService:
    """Reads the attendance tables and reports deterministic metrics."""

    def __init__(self, database):
        self.database = database

    # -- shared helpers ----------------------------------------------------

    def _completed_sessions(self, class_id=None):
        if class_id:
            return self.database.query(
                "SELECT * FROM attendance_sessions WHERE status = ? "
                "AND class_id = ? ORDER BY session_date, start_time",
                (COMPLETED, class_id))
        return self.database.query(
            "SELECT * FROM attendance_sessions WHERE status = ? "
            "ORDER BY session_date, start_time", (COMPLETED,))

    def _effective_records(self, session_id, session_policy_version=None):
        """
        One record per student for a session, choosing between policy versions.

        Prefers the session's own policy version; otherwise the most recent
        record for that student.
        """
        rows = self.database.query(
            "SELECT * FROM attendance_records WHERE session_id = ? "
            "ORDER BY student_id, created_at", (session_id,))

        chosen = {}
        for row in rows:
            student_id = row["student_id"]
            current = chosen.get(student_id)

            if current is None:
                chosen[student_id] = row
                continue

            # A record matching the session's policy always wins.
            if session_policy_version is not None:
                if row["policy_version"] == session_policy_version:
                    chosen[student_id] = row
                    continue
                if current["policy_version"] == session_policy_version:
                    continue

            # Otherwise the later record wins (rows are ordered by created_at).
            chosen[student_id] = row

        return chosen

    def _expected_students(self, session_id):
        rows = self.database.query(
            "SELECT student_id FROM session_expected_students "
            "WHERE session_id = ? ORDER BY student_id", (session_id,))
        return [row["student_id"] for row in rows]

    @staticmethod
    def _empty_counts():
        return {status: 0 for status in COUNTED_STATUSES}

    # -- session-level -----------------------------------------------------

    def session_analytics(self, session_id):
        """
        Metrics for one session.

        Returns None if the session does not exist.
        """
        session = self.database.query_one(
            "SELECT * FROM attendance_sessions WHERE session_id = ?",
            (session_id,))
        if session is None:
            return None

        expected = self._expected_students(session_id)
        records = self._effective_records(session_id, session["policy_version"])

        counts = self._empty_counts()
        ratios = []
        durations = []
        overrides = 0

        for row in records.values():
            status = row["status"]
            if status in counts:
                counts[status] += 1
            ratios.append(row["presence_ratio"])
            durations.append(row["present_seconds"])
            overrides += 1 if row["is_override"] else 0

        satisfying = sum(counts[status] for status in SATISFYING_STATUSES)
        expected_count = len(expected)

        policy_versions = [
            row["policy_version"] for row in self.database.query(
                "SELECT DISTINCT policy_version FROM attendance_records "
                "WHERE session_id = ? ORDER BY policy_version", (session_id,))]

        # Presence segments are operational evidence, not attendance.
        segment_row = self.database.query_one(
            "SELECT COUNT(*) AS segments, COUNT(DISTINCT student_id) AS students "
            "FROM presence_segments WHERE session_id = ?", (session_id,))

        return {
            "session_id": session_id,
            "class_id": session["class_id"],
            "session_date": session["session_date"],
            "status": session["status"],
            "policy_version": session["policy_version"],
            "policy_versions_present": policy_versions,
            "has_multiple_policy_versions": len(policy_versions) > 1,
            "is_final": session["status"] == COMPLETED,
            "expected_students": expected_count,
            "evaluated_students": len(records),
            "verified_students": segment_row["students"] or 0,
            "presence_segments": segment_row["segments"] or 0,
            "counts": counts,
            "satisfying_count": satisfying,
            "attendance_rate": _round(_ratio(satisfying, expected_count)),
            "average_presence_ratio": _round(
                _ratio(sum(ratios), len(ratios))),
            "average_present_seconds": _round(
                _ratio(sum(durations), len(durations)), 1),
            "override_count": overrides,
            "sample_size": expected_count,
        }

    # -- a day at a time ---------------------------------------------------

    def day_summary(self, session_ids):
        """
        What a named set of sessions adds up to. Used by the day dashboard.

        Takes SESSION IDS rather than a class filter, and that is the whole
        design: deciding which sessions a caller may see is a permission
        question, it is answered once by the Principal in
        data_service.list_sessions, and this method must not grow a second
        opinion about it. Give it the ids and it does arithmetic.

        Attendance is summed over COMPLETED sessions only. A lesson that is
        still running has no attendance yet - the rule engine has not run -
        and counting its absences would report every student as missing for
        the first forty minutes of every day.

        Every figure is None rather than 0 where there is nothing to divide by,
        so the dashboard can say "no completed sessions today" instead of
        showing a confident 0%.
        """
        counts = self._empty_counts()
        expected_total = 0
        satisfying_total = 0
        completed = 0
        evaluated_total = 0

        for session_id in session_ids:
            analytics = self.session_analytics(session_id)
            if analytics is None or not analytics["is_final"]:
                continue

            completed += 1
            for status, count in analytics["counts"].items():
                counts[status] += count

            expected_total += analytics["expected_students"]
            satisfying_total += analytics["satisfying_count"]
            evaluated_total += analytics["evaluated_students"]

        return {
            "completed_sessions": completed,
            "expected_students": expected_total,
            "evaluated_students": evaluated_total,
            "satisfying_count": satisfying_total,
            "counts": counts,
            "attendance_rate": _round(_ratio(satisfying_total,
                                             expected_total)),
            "has_data": completed > 0,
        }

    # -- student-level -----------------------------------------------------

    def student_analytics(self, student_id):
        """
        Metrics for one student across every COMPLETED session they were
        expected in.

        Returns None if the student does not exist.
        """
        student = self.database.query_one(
            "SELECT * FROM students WHERE student_id = ?", (student_id,))
        if student is None:
            return None

        rows = self.database.query(
            """
            SELECT s.session_id, s.class_id, s.session_date, s.start_time,
                   s.policy_version AS session_policy_version
              FROM attendance_sessions s
              JOIN session_expected_students e
                ON e.session_id = s.session_id
             WHERE s.status = ? AND e.student_id = ?
             ORDER BY s.session_date, s.start_time
            """,
            (COMPLETED, student_id))

        counts = self._empty_counts()
        history = []
        ratios = []
        durations = []
        missing_records = 0

        for row in rows:
            records = self._effective_records(
                row["session_id"], row["session_policy_version"])
            record = records.get(student_id)

            if record is None:
                # Expected, session completed, but never evaluated. Counted in
                # the denominator; reported so the gap is not invisible.
                missing_records += 1
                history.append({
                    "session_id": row["session_id"],
                    "class_id": row["class_id"],
                    "session_date": row["session_date"],
                    "status": None,
                    "presence_ratio": None,
                    "present_seconds": None,
                    "policy_version": None,
                    "is_override": False,
                })
                continue

            status = record["status"]
            if status in counts:
                counts[status] += 1
            ratios.append(record["presence_ratio"])
            durations.append(record["present_seconds"])

            history.append({
                "session_id": row["session_id"],
                "class_id": row["class_id"],
                "session_date": row["session_date"],
                "status": status,
                "presence_ratio": _round(record["presence_ratio"]),
                "present_seconds": _round(record["present_seconds"], 1),
                "policy_version": record["policy_version"],
                "is_override": bool(record["is_override"]),
                "original_status": record["original_status"],
                "reason": record["reason"],
            })

        expected_sessions = len(rows)
        satisfying = sum(counts[status] for status in SATISFYING_STATUSES)

        # Who this person is, alongside how they have been doing.
        #
        # Added for the performance profile: the page that shows a rate also
        # has to name the class it was earned in, and a second request to
        # /api/students/{id} for three columns that are already on the row
        # this method just read would be a round trip for nothing.
        #
        # Read from the registry row, never computed. A blank class is
        # reported blank - the profile says "not recorded" rather than
        # guessing from whichever session the student turned up in.
        return {
            "student_id": student_id,
            "name": student["name"],
            "status": student["status"],
            "roll_number": student["roll_number"] or "",
            "class_name": student["class_name"] or "",
            "section": student["section"] or "",
            "expected_sessions": expected_sessions,
            "evaluated_sessions": expected_sessions - missing_records,
            "sessions_without_record": missing_records,
            "counts": counts,
            "satisfying_count": satisfying,
            "attendance_rate": _round(_ratio(satisfying, expected_sessions)),
            "late_rate": _round(_ratio(counts["LATE"], expected_sessions)),
            "partial_rate": _round(_ratio(counts["PARTIAL"], expected_sessions)),
            "early_leave_rate": _round(
                _ratio(counts["LEFT_EARLY"], expected_sessions)),
            "absence_rate": _round(_ratio(counts["ABSENT"], expected_sessions)),
            "average_presence_ratio": _round(_ratio(sum(ratios), len(ratios))),
            "average_present_seconds": _round(
                _ratio(sum(durations), len(durations)), 1),
            "history": history,
            "sample_size": expected_sessions,
            "has_trend_data": expected_sessions >= MINIMUM_SESSIONS_FOR_TREND,
            "absent_sessions": counts["ABSENT"] + missing_records,
            "trend": self._attendance_trend(history),
        }

    # -- trend -------------------------------------------------------------

    @staticmethod
    def _attendance_trend(history):
        """
        Whether a student's recent attendance is better or worse than before.

        Deliberately the simplest thing that can be checked by hand: split the
        sessions down the middle in date order, take the satisfying share of
        each half, and report the difference. No regression, no smoothing, no
        model - somebody disputing this number can recount it from the table
        printed directly underneath it.

        Returns direction NO_DATA until there are MINIMUM_SESSIONS_FOR_TREND
        sessions to split. Two halves of one session each is not a trend, and
        calling it one would be the invented figure this project does not
        print.
        """
        evaluated = [entry for entry in history if entry["status"] is not None]

        if len(evaluated) < MINIMUM_SESSIONS_FOR_TREND:
            return {
                "direction": "NO_DATA",
                "earlier_rate": None,
                "recent_rate": None,
                "change": None,
                "sample_size": len(evaluated),
                "note": ("At least %d evaluated sessions are needed before a "
                         "trend means anything." % MINIMUM_SESSIONS_FOR_TREND),
            }

        def share(entries):
            if not entries:
                return None
            satisfying = sum(1 for entry in entries
                             if entry["status"] in SATISFYING_STATUSES)
            return _ratio(satisfying, len(entries))

        middle = len(evaluated) // 2
        earlier = share(evaluated[:middle])
        recent = share(evaluated[middle:])
        change = recent - earlier

        # A threshold rather than a sign test, because a single session moving
        # a five-session rate by 20% is noise and reporting it as "improving"
        # would make the word useless.
        if change >= 0.1:
            direction = "IMPROVING"
        elif change <= -0.1:
            direction = "DECLINING"
        else:
            direction = "STEADY"

        return {
            "direction": direction,
            "earlier_rate": _round(earlier),
            "recent_rate": _round(recent),
            "change": _round(change),
            "sample_size": len(evaluated),
            "note": ("The satisfying share of the most recent %d sessions "
                     "against the %d before them."
                     % (len(evaluated) - middle, middle)),
        }

    # -- class-level -------------------------------------------------------

    def class_analytics(self, class_id):
        """Metrics for one class across its completed sessions."""
        sessions = self._completed_sessions(class_id)

        if not sessions:
            return {
                "class_id": class_id,
                "completed_sessions": 0,
                "session_history": [],
                "counts": self._empty_counts(),
                "attendance_rate": None,
                "average_presence_ratio": None,
                "late_rate": None,
                "partial_rate": None,
                "absence_rate": None,
                "early_leave_rate": None,
                "student_count": 0,
                "sample_size": 0,
                "has_trend_data": False,
                "comparison_available": False,
            }

        counts = self._empty_counts()
        total_expected = 0
        total_satisfying = 0
        ratios = []
        history = []
        students = set()

        for session in sessions:
            analytics = self.session_analytics(session["session_id"])
            if analytics is None:
                continue

            for status, count in analytics["counts"].items():
                counts[status] += count

            total_expected += analytics["expected_students"]
            total_satisfying += analytics["satisfying_count"]

            if analytics["average_presence_ratio"] is not None:
                ratios.append(analytics["average_presence_ratio"])

            students.update(self._expected_students(session["session_id"]))

            history.append({
                "session_id": session["session_id"],
                "session_date": session["session_date"],
                "attendance_rate": analytics["attendance_rate"],
                "average_presence_ratio": analytics["average_presence_ratio"],
                "expected_students": analytics["expected_students"],
                "counts": analytics["counts"],
            })

        return {
            "class_id": class_id,
            "completed_sessions": len(sessions),
            "student_count": len(students),
            "counts": counts,
            "attendance_rate": _round(_ratio(total_satisfying, total_expected)),
            "late_rate": _round(_ratio(counts["LATE"], total_expected)),
            "partial_rate": _round(_ratio(counts["PARTIAL"], total_expected)),
            "early_leave_rate": _round(
                _ratio(counts["LEFT_EARLY"], total_expected)),
            "absence_rate": _round(_ratio(counts["ABSENT"], total_expected)),
            "average_presence_ratio": _round(_ratio(sum(ratios), len(ratios))),
            "session_history": history,
            "sample_size": total_expected,
            # Two sessions allow a comparison; a trend needs more.
            "comparison_available": len(sessions) >= 2,
            "has_trend_data": len(sessions) >= MINIMUM_SESSIONS_FOR_TREND,
        }

    # -- overview ----------------------------------------------------------

    def overview(self, class_id=None, session_date=None):
        """
        The dashboard's headline numbers.

        Everything is scoped to COMPLETED sessions, optionally filtered by
        class or date.
        """
        clauses = ["status = ?"]
        parameters = [COMPLETED]

        if class_id:
            clauses.append("class_id = ?")
            parameters.append(class_id)
        if session_date:
            clauses.append("session_date = ?")
            parameters.append(session_date)

        sessions = self.database.query(
            "SELECT * FROM attendance_sessions WHERE %s "
            "ORDER BY session_date, start_time" % " AND ".join(clauses),
            tuple(parameters))

        counts = self._empty_counts()
        total_expected = 0
        total_satisfying = 0
        ratios = []
        students = set()
        classes = set()
        policy_versions = set()

        for session in sessions:
            analytics = self.session_analytics(session["session_id"])
            if analytics is None:
                continue

            for status, count in analytics["counts"].items():
                counts[status] += count

            total_expected += analytics["expected_students"]
            total_satisfying += analytics["satisfying_count"]
            if analytics["average_presence_ratio"] is not None:
                ratios.append(analytics["average_presence_ratio"])

            students.update(self._expected_students(session["session_id"]))
            classes.add(session["class_id"])
            policy_versions.update(analytics["policy_versions_present"])

        all_sessions = self.database.query_one(
            "SELECT COUNT(*) AS total FROM attendance_sessions")["total"]
        all_students = self.database.query_one(
            "SELECT COUNT(*) AS total FROM students")["total"]

        return {
            "has_data": all_sessions > 0,
            "total_sessions": all_sessions,
            "completed_sessions": len(sessions),
            "total_students": all_students,
            "students_in_scope": len(students),
            "classes_in_scope": sorted(classes),
            "counts": counts,
            "attendance_rate": _round(_ratio(total_satisfying, total_expected)),
            "average_presence_ratio": _round(_ratio(sum(ratios), len(ratios))),
            "sample_size": total_expected,
            "policy_versions": sorted(policy_versions),
            "has_multiple_policy_versions": len(policy_versions) > 1,
            "filters": {"class_id": class_id, "session_date": session_date},
        }

    # -- deterministic indicators -----------------------------------------

    def attention_indicators(self, class_id=None, minimum_sessions=3):
        """
        Students worth a second look, by an EXPLICIT arithmetic rule.

        These are NOT anomalies and no model is involved. A student is listed
        only when, across at least `minimum_sessions` completed sessions they
        were expected in, one of these is literally true:

            attendance_rate < 0.75   - below the dataset's own default ratio
            late_rate       > 0.25   - late in more than a quarter of sessions
            absence_rate    > 0.25   - absent in more than a quarter

        Each entry states which rule fired and the numbers behind it, so the
        teacher can disagree with it.
        """
        low_attendance = 0.75
        high_late = 0.25
        high_absence = 0.25

        if class_id:
            rows = self.database.query(
                """
                SELECT DISTINCT e.student_id
                  FROM session_expected_students e
                  JOIN attendance_sessions s ON s.session_id = e.session_id
                 WHERE s.class_id = ? AND s.status = ?
                 ORDER BY e.student_id
                """, (class_id, COMPLETED))
        else:
            rows = self.database.query(
                """
                SELECT DISTINCT e.student_id
                  FROM session_expected_students e
                  JOIN attendance_sessions s ON s.session_id = e.session_id
                 WHERE s.status = ?
                 ORDER BY e.student_id
                """, (COMPLETED,))

        indicators = []

        for row in rows:
            analytics = self.student_analytics(row["student_id"])
            if analytics is None:
                continue

            sessions = analytics["expected_sessions"]
            if sessions < minimum_sessions:
                continue

            reasons = []
            if analytics["attendance_rate"] is not None \
                    and analytics["attendance_rate"] < low_attendance:
                reasons.append(
                    "attendance rate %.0f%% is below %.0f%%"
                    % (analytics["attendance_rate"] * 100, low_attendance * 100))
            if analytics["late_rate"] is not None \
                    and analytics["late_rate"] > high_late:
                reasons.append("late in %.0f%% of sessions"
                               % (analytics["late_rate"] * 100))
            if analytics["absence_rate"] is not None \
                    and analytics["absence_rate"] > high_absence:
                reasons.append("absent in %.0f%% of sessions"
                               % (analytics["absence_rate"] * 100))

            if reasons:
                indicators.append({
                    "student_id": analytics["student_id"],
                    "name": analytics["name"],
                    "attendance_rate": analytics["attendance_rate"],
                    "late_rate": analytics["late_rate"],
                    "absence_rate": analytics["absence_rate"],
                    "sessions_considered": sessions,
                    "reasons": reasons,
                })

        return {
            "indicators": indicators,
            "rule": {
                "minimum_sessions": minimum_sessions,
                "attendance_rate_below": low_attendance,
                "late_rate_above": high_late,
                "absence_rate_above": high_absence,
            },
            "note": ("Deterministic thresholds over completed sessions. "
                     "These are not anomalies and no model is involved."),
        }
