"""
ClassVision AI - Attendance reports
------------------------------------
The register, filtered, and the same rows as a CSV file.

What this is

    A READ over attendance_records that have already been written by the Step 6
    rule engine, joined to the session they belong to and the student they are
    about. It calculates no attendance and it knows no thresholds. Filter it
    differently and you get a different selection of the same decisions; you
    never get a different decision.

    That boundary is the reason this is a separate module from
    analytics_service.py. Analytics computes rates; this hands back rows. A
    report that quietly recomputed a status would be a second attendance
    engine, and the whole point of the one in ai/attendance_rules.py is that
    it is the only one.

Effective records, and why the report does not show duplicates

    attendance_records is UNIQUE on (session_id, student_id, policy_version).
    Re-evaluating a session under a new policy therefore ADDS a row rather
    than replacing one, which is deliberate - the old decision is history and
    history is not overwritten in this project.

    A report showing both rows would show a student as present and absent for
    the same lesson. So one record per student per session is chosen, by the
    same rule analytics_service._effective_records uses: the row matching the
    session's own policy version wins, and otherwise the most recently created
    row does. `policy_version` is on every exported row so which decision is
    being read is never a guess.

    Asking for a specific policy_version turns that off and reports exactly
    what was stored under it, which is what somebody comparing two policies
    actually wants.

Scope

    Every filter here is a NARROWING of what the caller may already see. The
    permission itself is the Principal passed in: sessions outside the
    caller's classes are dropped, and so are students they may not view. The
    route applies nothing extra, and it does not need to - see
    backend/routes/reports.py.

CSV

    Written with the standard library's csv module, into a string, with
    QUOTE_MINIMAL and \\r\\n line endings, which is what RFC 4180 says and what
    Excel opens without a dialog. No value is formatted for display: a ratio
    is a ratio and a timestamp is an ISO instant, because a spreadsheet is
    going to sort and subtract these and a pre-rounded "90.0%" cannot be.
"""

import csv
import io

# Every column in the export, in order, with the header a person reads.
#
# One list rather than two, so a column cannot appear in the file with no
# header or gain a header nothing fills in. The CSV and the JSON report are
# built from the same rows, so they can never disagree about a student.
CSV_COLUMNS = (
    ("session_date", "Date"),
    ("session_id", "Session"),
    ("class_id", "Class (session)"),
    ("session_section", "Section (session)"),
    ("student_id", "Student ID"),
    ("student_name", "Name"),
    ("roll_number", "Roll number"),
    ("class_name", "Class (student)"),
    ("section", "Section (student)"),
    ("status", "Status"),
    ("presence_ratio", "Presence ratio"),
    ("present_seconds", "Present seconds"),
    ("first_seen", "First seen"),
    ("last_seen", "Last seen"),
    ("was_late", "Was late"),
    ("left_early", "Left early"),
    ("is_override", "Manually corrected"),
    ("original_status", "Original status"),
    ("override_reason", "Correction reason"),
    ("overridden_by", "Corrected by"),
    ("reason", "Reason"),
    ("policy_version", "Policy version"),
)

# A report is a whole-selection answer, so it is not paged the way a table is.
# It is still bounded: an unbounded export is how a classroom machine runs out
# of memory on the one afternoon somebody asks for the whole year.
MAX_REPORT_ROWS = 20000


class ReportService:
    """Filtered reads of attendance that has already been decided."""

    def __init__(self, database):
        self.database = database

    # -- the report --------------------------------------------------------

    def attendance(self, principal=None, student_id=None, class_id=None,
                   class_name=None, section=None, date_from=None,
                   date_to=None, status=None, policy_version=None,
                   session_id=None, include_unevaluated=False,
                   limit=MAX_REPORT_ROWS):
        """
        Attendance rows matching the filters, newest session first.

        Filters that SQLite can express are pushed into SQL. The two that
        cannot are applied afterwards:

            class scope    a session's class_id is free text and an assignment
                           is class plus section, reconciled by normalising
                           both - see authz.normalise_class. Not a comparison
                           SQLite can do.

            student scope  the same question on the student side, via
                           Principal.may_see_student.

        `include_unevaluated` adds a row for every student who was EXPECTED at
        a completed session and never evaluated. They are reported with a null
        status rather than as absent, because "nobody decided" and "decided
        absent" are different facts and a report that merges them is lying
        about which.
        """
        clauses = []
        parameters = []

        if student_id:
            clauses.append("r.student_id = ?")
            parameters.append(student_id)
        if session_id:
            clauses.append("r.session_id = ?")
            parameters.append(session_id)
        if class_id:
            clauses.append("s.class_id = ? COLLATE NOCASE")
            parameters.append(class_id)
        if class_name:
            clauses.append("st.class_name = ? COLLATE NOCASE")
            parameters.append(class_name)
        if section:
            clauses.append("st.section = ? COLLATE NOCASE")
            parameters.append(section)
        if date_from:
            clauses.append("s.session_date >= ?")
            parameters.append(date_from)
        if date_to:
            clauses.append("s.session_date <= ?")
            parameters.append(date_to)
        if status:
            clauses.append("r.status = ?")
            parameters.append(status)
        if policy_version:
            clauses.append("r.policy_version = ?")
            parameters.append(policy_version)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        # Ordered by created_at ASCENDING within a student so the effective-row
        # choice below can simply keep the last one it sees. The final ordering
        # for display is applied after that.
        rows = self.database.query(
            """
            SELECT r.*,
                   st.name AS student_name,
                   st.roll_number AS roll_number,
                   st.class_name AS class_name,
                   st.section AS section,
                   st.status AS student_status,
                   s.class_id AS class_id,
                   s.session_date AS session_date,
                   s.section AS session_section,
                   s.status AS session_status,
                   s.policy_version AS session_policy_version
              FROM attendance_records r
              JOIN attendance_sessions s ON s.session_id = r.session_id
              LEFT JOIN students st ON st.student_id = r.student_id
              %s
             ORDER BY r.student_id, r.created_at
            """ % where, tuple(parameters))

        records = [self._row(row) for row in rows]

        # One record per student per session, unless a policy was named.
        if not policy_version:
            records = self._effective(records)

        if include_unevaluated:
            records.extend(self._unevaluated(
                {(record["session_id"], record["student_id"])
                 for record in records},
                class_id=class_id, class_name=class_name, section=section,
                student_id=student_id, session_id=session_id,
                date_from=date_from, date_to=date_to))

        records = self._in_scope(records, principal)

        # Newest session first, then by student, which is how somebody reads a
        # register: the most recent lesson at the top.
        records.sort(key=lambda record: (record["session_date"] or "",
                                         record["session_id"] or "",
                                         record["student_id"] or ""),
                     reverse=True)

        total = len(records)
        truncated = total > limit

        return {
            "items": records[:limit],
            "total": total,
            "returned": min(total, limit),
            "truncated": truncated,
            "limit": limit,
            "summary": self._summary(records[:limit]),
            "filters": {
                "student_id": student_id,
                "session_id": session_id,
                "class_id": class_id,
                "class_name": class_name,
                "section": section,
                "date_from": date_from,
                "date_to": date_to,
                "status": status,
                "policy_version": policy_version,
                "include_unevaluated": bool(include_unevaluated),
            },
            "note": ("Every row is an attendance decision already stored by "
                     "the rule engine. This report selects and formats them; "
                     "it calculates no attendance and applies no threshold."),
        }

    # -- assembling a row --------------------------------------------------

    @staticmethod
    def _row(row):
        """
        One database row as the report's own shape.

        Values are kept as VALUES. A ratio stays a float and a timestamp stays
        an ISO instant, because the spreadsheet this ends up in is going to
        sort and subtract them, and "90.0%" cannot be subtracted from anything.
        """
        return {
            "session_id": row["session_id"],
            "session_date": row["session_date"],
            "session_status": row["session_status"],
            "class_id": row["class_id"],
            "session_section": row["session_section"] or "",
            "student_id": row["student_id"],
            "student_name": row["student_name"] or "",
            "roll_number": row["roll_number"] or "",
            "class_name": row["class_name"] or "",
            "section": row["section"] or "",
            "student_status": row["student_status"] or "",
            "status": row["status"],
            "presence_ratio": row["presence_ratio"],
            "present_seconds": row["present_seconds"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "was_late": bool(row["was_late"]),
            "left_early": bool(row["left_early"]),
            "is_override": bool(row["is_override"]),
            "original_status": row["original_status"],
            "override_reason": row["override_reason"],
            "overridden_by": row["overridden_by"],
            "reason": row["reason"],
            "policy_version": row["policy_version"],
            "session_policy_version": row["session_policy_version"],
            "evaluated": True,
        }

    @staticmethod
    def _effective(records):
        """
        One record per (session, student): the session's policy wins, else the
        latest.

        The same rule as analytics_service._effective_records, applied to rows
        that arrive already ordered by created_at, so "latest" is "last seen".
        """
        chosen = {}

        for record in records:
            key = (record["session_id"], record["student_id"])
            current = chosen.get(key)

            if current is None:
                chosen[key] = record
                continue

            session_policy = record["session_policy_version"]
            if session_policy is not None:
                if record["policy_version"] == session_policy:
                    chosen[key] = record
                    continue
                if current["policy_version"] == session_policy:
                    continue

            chosen[key] = record

        return list(chosen.values())

    def _unevaluated(self, seen, class_id=None, class_name=None, section=None,
                     student_id=None, session_id=None, date_from=None,
                     date_to=None):
        """
        Students expected at a COMPLETED session who have no record at all.

        Reported with status None, never as ABSENT. A completed session that
        never evaluated somebody is a gap in the data, and a report that fills
        it in with a decision nobody made is the exact thing this project
        refuses to do everywhere else.
        """
        clauses = ["s.status = 'COMPLETED'"]
        parameters = []

        if session_id:
            clauses.append("s.session_id = ?")
            parameters.append(session_id)
        if class_id:
            clauses.append("s.class_id = ? COLLATE NOCASE")
            parameters.append(class_id)
        if class_name:
            clauses.append("st.class_name = ? COLLATE NOCASE")
            parameters.append(class_name)
        if section:
            clauses.append("st.section = ? COLLATE NOCASE")
            parameters.append(section)
        if student_id:
            clauses.append("e.student_id = ?")
            parameters.append(student_id)
        if date_from:
            clauses.append("s.session_date >= ?")
            parameters.append(date_from)
        if date_to:
            clauses.append("s.session_date <= ?")
            parameters.append(date_to)

        rows = self.database.query(
            """
            SELECT e.session_id, e.student_id,
                   st.name AS student_name, st.roll_number, st.class_name,
                   st.section, st.status AS student_status,
                   s.class_id, s.session_date, s.section AS session_section,
                   s.status AS session_status,
                   s.policy_version AS session_policy_version
              FROM session_expected_students e
              JOIN attendance_sessions s ON s.session_id = e.session_id
              LEFT JOIN students st ON st.student_id = e.student_id
             WHERE %s
            """ % " AND ".join(clauses), tuple(parameters))

        missing = []
        for row in rows:
            if (row["session_id"], row["student_id"]) in seen:
                continue

            missing.append({
                "session_id": row["session_id"],
                "session_date": row["session_date"],
                "session_status": row["session_status"],
                "class_id": row["class_id"],
                "session_section": row["session_section"] or "",
                "student_id": row["student_id"],
                "student_name": row["student_name"] or "",
                "roll_number": row["roll_number"] or "",
                "class_name": row["class_name"] or "",
                "section": row["section"] or "",
                "student_status": row["student_status"] or "",
                "status": None,
                "presence_ratio": None,
                "present_seconds": None,
                "first_seen": None,
                "last_seen": None,
                "was_late": False,
                "left_early": False,
                "is_override": False,
                "original_status": None,
                "override_reason": None,
                "overridden_by": None,
                "reason": "expected, but this session was never evaluated",
                "policy_version": None,
                "session_policy_version": row["session_policy_version"],
                "evaluated": False,
            })

        return missing

    @staticmethod
    def _in_scope(records, principal):
        """Drop rows the caller may not see, on both axes."""
        if principal is None or principal.unrestricted:
            return records

        kept = []
        for record in records:
            if not principal.may_see_class_id(record["class_id"]):
                continue
            if not principal.may_see_student(record):
                continue
            kept.append(record)
        return kept

    @staticmethod
    def _summary(records):
        """
        Counts over exactly the rows returned, so the total under a table is
        the total OF that table.

        attendance_rate is the satisfying share - PRESENT, LATE and LEFT_EARLY,
        the same three analytics_service.SATISFYING_STATUSES names - over every
        row. It is None when there are no rows, rather than 0.0.
        """
        from backend.services.analytics_service import SATISFYING_STATUSES

        counts = {}
        satisfying = 0
        unevaluated = 0

        for record in records:
            status = record["status"]
            if status is None:
                unevaluated += 1
                continue
            counts[status] = counts.get(status, 0) + 1
            if status in SATISFYING_STATUSES:
                satisfying += 1

        total = len(records)

        return {
            "rows": total,
            "students": len({record["student_id"] for record in records}),
            "sessions": len({record["session_id"] for record in records}),
            "counts": counts,
            "unevaluated": unevaluated,
            "satisfying_count": satisfying,
            "attendance_rate": (round(satisfying / total, 4)
                                if total else None),
        }

    # -- the same rows, as a file ------------------------------------------

    def attendance_csv(self, **filters):
        """
        The report as RFC 4180 CSV.

        Built from the SAME call the JSON report uses, so the file a school
        keeps and the table they looked at cannot disagree. A null is an empty
        field, not the word "None"; a boolean is "yes"/"no", because a
        spreadsheet filter is easier to read that way than TRUE/FALSE mixed
        with blanks.
        """
        report = self.attendance(**filters)

        buffer = io.StringIO()
        # \\r\\n is what RFC 4180 specifies. lineterminator is set explicitly
        # because csv's default is platform-dependent, and a file written on
        # Linux and opened on Windows should not differ.
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL,
                            lineterminator="\r\n")

        writer.writerow([header for _, header in CSV_COLUMNS])

        for record in report["items"]:
            writer.writerow([self._cell(record.get(field))
                             for field, _ in CSV_COLUMNS])

        return buffer.getvalue(), report

    @staticmethod
    def _cell(value):
        if value is None:
            return ""
        if isinstance(value, bool):
            return "yes" if value else "no"
        return value

    # -- summaries ---------------------------------------------------------
    #
    # Everything below RE-USES analytics_service. Not one rate is computed
    # here. The roadmap's reason for that is worth restating: a summary that
    # did its own arithmetic would be a second source of truth about a
    # student's attendance, and a teacher comparing the Reports tab against
    # the Overview tab would have no way to tell which of two different
    # numbers was the real one. So `class_summary` asks class_analytics and
    # `term_summary` asks day_summary, and both hand back what they were told.

    def _analytics(self):
        from backend.services.analytics_service import AnalyticsService
        return AnalyticsService(self.database)

    def class_summary(self, principal=None, class_id=None, date_from=None,
                      date_to=None):
        """
        One row per class, from AnalyticsService.class_analytics.

        The class list comes from the sessions table rather than from a
        classes table, because a "class" in this schema is whatever a session
        said its class_id was - there is no registry of them. Classes the
        caller may not see are dropped before any analytics call, so a
        teacher's summary never even computes a class that is not theirs.

        `date_from`/`date_to` narrow the SESSION HISTORY, not the class list.
        class_analytics has no date parameter - it is whole-class-to-date by
        design - so a dated summary is assembled from the session_history it
        returns, which is the same per-session analytics the unfiltered figure
        is built from. That is the reuse the roadmap asked for: filtering a
        list of already-computed sessions, not recomputing them differently.
        """
        rows = self.database.query(
            "SELECT DISTINCT class_id FROM attendance_sessions "
            "WHERE status = 'COMPLETED' ORDER BY class_id")

        class_ids = [row["class_id"] for row in rows]
        if class_id:
            class_ids = [each for each in class_ids
                         if (each or "").lower() == class_id.lower()]
        if principal is not None and not principal.unrestricted:
            class_ids = [each for each in class_ids
                         if principal.may_see_class_id(each)]

        analytics = self._analytics()
        summaries = []

        for each in class_ids:
            full = analytics.class_analytics(each)
            summaries.append(self._dated_class_row(full, date_from, date_to))

        # Worst attendance first. A summary exists to find the class that
        # needs attention, so sorting it alphabetically would bury the answer;
        # a class with no rate at all sorts last rather than as 0%.
        summaries.sort(key=lambda row: (row["attendance_rate"] is None,
                                        row["attendance_rate"] if
                                        row["attendance_rate"] is not None
                                        else 0.0,
                                        row["class_id"] or ""))

        return {
            "items": summaries,
            "total": len(summaries),
            "filters": {
                "class_id": class_id,
                "date_from": date_from,
                "date_to": date_to,
            },
            "totals": self._roll_up(summaries),
            "note": ("Every rate here is the one analytics_service computed "
                     "for that class. This report re-uses those numbers and "
                     "calculates none of its own."),
        }

    @staticmethod
    def _dated_class_row(full, date_from, date_to):
        """
        One class_analytics result as a summary row, optionally date-narrowed.

        With no dates this is a straight projection of what analytics said.
        With dates, the per-session history is filtered and the class-level
        counts are re-summed FROM THOSE SESSIONS - each of which carries the
        attendance_rate analytics gave it. The sessions are the unit of reuse;
        nothing re-reads an attendance record.

        A window containing no session reports None for every rate and 0
        sessions, never 0.0, so the page can say "no completed sessions in
        this window" rather than showing a confident zero.
        """
        history = full["session_history"]

        if date_from or date_to:
            history = [entry for entry in history
                       if (not date_from
                           or (entry["session_date"] or "") >= date_from)
                       and (not date_to
                            or (entry["session_date"] or "") <= date_to)]

            counts = {}
            expected = 0
            satisfying = 0
            ratios = []

            for entry in history:
                for status, count in entry["counts"].items():
                    counts[status] = counts.get(status, 0) + count
                expected += entry["expected_students"]
                if entry["attendance_rate"] is not None:
                    satisfying += round(entry["attendance_rate"]
                                        * entry["expected_students"])
                if entry["average_presence_ratio"] is not None:
                    ratios.append(entry["average_presence_ratio"])

            sessions = len(history)
            average_ratio = (round(sum(ratios) / len(ratios), 4)
                             if ratios else None)
            rate = round(satisfying / expected, 4) if expected else None
        else:
            counts = dict(full["counts"])
            expected = full["sample_size"]
            sessions = full["completed_sessions"]
            average_ratio = full["average_presence_ratio"]
            rate = full["attendance_rate"]

        def share(status):
            count = counts.get(status, 0)
            return round(count / expected, 4) if expected else None

        return {
            "class_id": full["class_id"],
            "completed_sessions": sessions,
            "student_count": full["student_count"],
            "expected_students": expected,
            "counts": counts,
            "attendance_rate": rate,
            "average_presence_ratio": average_ratio,
            "late_rate": share("LATE"),
            "partial_rate": share("PARTIAL"),
            "absence_rate": share("ABSENT"),
            "early_leave_rate": share("LEFT_EARLY"),
            "first_session": (history[0]["session_date"]
                              if history else None),
            "last_session": (history[-1]["session_date"]
                             if history else None),
            "windowed": bool(date_from or date_to),
        }

    @staticmethod
    def _roll_up(summaries):
        """
        The summary's own bottom line, weighted by expected students.

        A plain mean of per-class rates would weight a class of 4 the same as
        a class of 40, which is how a school reports a figure nobody can
        reproduce. Weighting by expected students makes the total the same
        number an unfiltered whole-school report would give.
        """
        expected = sum(row["expected_students"] or 0 for row in summaries)
        satisfying = 0

        for row in summaries:
            if row["attendance_rate"] is not None:
                satisfying += round(row["attendance_rate"]
                                    * (row["expected_students"] or 0))

        return {
            "classes": len(summaries),
            "completed_sessions": sum(row["completed_sessions"] or 0
                                      for row in summaries),
            "expected_students": expected,
            "attendance_rate": (round(satisfying / expected, 4)
                                if expected else None),
        }

    def term_summary(self, principal=None, academic_session_id=None):
        """
        One row per term, and per class within a term.

        A term is an `academic_sessions` row; a lesson belongs to one through
        `attendance_sessions.academic_session_id`. Lessons recorded before
        terms existed have that column NULL, and they are reported under a
        term of None labelled "unassigned" rather than silently dropped or
        quietly folded into the current term - a session nobody filed is a
        fact about the data, not a rounding error.

        The arithmetic is day_summary's, given the session ids of the term.
        That method takes ids precisely so it never has a second opinion about
        permission; the ids passed here are already scoped.
        """
        terms = {row["academic_session_id"]: row for row in
                 self.database.query(
                     "SELECT * FROM academic_sessions "
                     "ORDER BY academic_year, semester")}

        clauses = ["status = 'COMPLETED'"]
        parameters = []
        if academic_session_id:
            clauses.append("academic_session_id = ?")
            parameters.append(academic_session_id)

        sessions = self.database.query(
            "SELECT session_id, class_id, session_date, academic_session_id "
            "FROM attendance_sessions WHERE %s "
            "ORDER BY session_date, start_time" % " AND ".join(clauses),
            tuple(parameters))

        if principal is not None and not principal.unrestricted:
            sessions = [row for row in sessions
                        if principal.may_see_class_id(row["class_id"])]

        grouped = {}
        for row in sessions:
            grouped.setdefault(row["academic_session_id"], []).append(row)

        analytics = self._analytics()
        items = []

        for term_id, rows in grouped.items():
            term = terms.get(term_id)
            summary = analytics.day_summary([row["session_id"]
                                             for row in rows])

            classes = []
            by_class = {}
            for row in rows:
                by_class.setdefault(row["class_id"], []).append(
                    row["session_id"])

            for class_id, session_ids in sorted(by_class.items(),
                                                key=lambda pair: pair[0] or ""):
                class_summary = analytics.day_summary(session_ids)
                classes.append({
                    "class_id": class_id,
                    "completed_sessions": class_summary["completed_sessions"],
                    "expected_students": class_summary["expected_students"],
                    "attendance_rate": class_summary["attendance_rate"],
                    "counts": class_summary["counts"],
                })

            dates = sorted(row["session_date"] or "" for row in rows)

            items.append({
                "academic_session_id": term_id,
                "academic_year": term["academic_year"] if term else "",
                "semester": term["semester"] if term else "",
                "term_status": term["status"] if term else "",
                "term_start": term["start_date"] if term else None,
                "term_end": term["end_date"] if term else None,
                # A lesson with no term is reported, and labelled as such.
                "unassigned": term_id is None,
                "label": (self._term_label(term) if term
                          else "Not assigned to a term"),
                "first_session": dates[0] if dates else None,
                "last_session": dates[-1] if dates else None,
                "completed_sessions": summary["completed_sessions"],
                "expected_students": summary["expected_students"],
                "evaluated_students": summary["evaluated_students"],
                "attendance_rate": summary["attendance_rate"],
                "counts": summary["counts"],
                "classes": classes,
            })

        # Chronological by the term's own dates, with unassigned lessons last:
        # they have no term dates to sort by, and putting them first would
        # push a real term off the top of the page.
        items.sort(key=lambda row: (row["unassigned"],
                                    row["term_start"] or "",
                                    row["academic_year"] or "",
                                    row["semester"] or ""))

        return {
            "items": items,
            "total": len(items),
            "filters": {"academic_session_id": academic_session_id},
            "note": ("Each term's figures are AnalyticsService.day_summary "
                     "over that term's completed sessions. Lessons predating "
                     "academic terms are listed as unassigned rather than "
                     "folded into a term nobody filed them under."),
        }

    @staticmethod
    def _term_label(term):
        year = term["academic_year"] or ""
        semester = term["semester"] or ""
        return ("%s %s" % (year, semester)).strip() or term[
            "academic_session_id"]

    # -- the seat map ------------------------------------------------------

    def seat_map(self, session_id, principal=None, columns=6):
        """
        One session as a grid of students, each with the status already stored.

        THIS IS NOT A RECORDED SEATING PLAN. No table in this schema holds a
        seat, a desk or a coordinate, and nothing in the camera pipeline
        reports where in the room a face was - the tracker works in image
        space and the image is not calibrated to furniture. Inventing an
        x and y from a bounding box would produce a picture a teacher would
        reasonably read as "where the student sat", which would be a
        fabrication.

        What this IS: the session's students laid out in a stable grid, in
        roll-number order, so a teacher can take one printed sheet round a
        room and check it off. The order is deterministic - the same session
        always produces the same grid - and `ordering` says out loud what the
        order is, so the page can label it instead of implying a floor plan.

        Every cell's status comes from the attendance record the rule engine
        wrote, chosen by the same effective-record rule as everything else
        here. A student expected and never evaluated gets a cell with a null
        status, because that is a different fact from absent.
        """
        session = self.database.query_one(
            "SELECT * FROM attendance_sessions WHERE session_id = ?",
            (session_id,))
        if session is None:
            return None

        if (principal is not None
                and not principal.may_see_class_id(session["class_id"])):
            # Indistinguishable from a session that does not exist, which is
            # what a caller outside its class should be told.
            return None

        records = self.attendance(principal=principal, session_id=session_id,
                                 include_unevaluated=True,
                                 limit=MAX_REPORT_ROWS)

        cells = sorted(records["items"], key=self._seat_order)

        columns = max(1, min(int(columns or 6), 12))
        grid = [cells[index:index + columns]
                for index in range(0, len(cells), columns)]

        return {
            "session_id": session_id,
            "class_id": session["class_id"],
            "session_date": session["session_date"],
            "section": session["section"] or "",
            "teacher": session["teacher"] or "",
            "status": session["status"],
            "columns": columns,
            "rows": len(grid),
            "seats": len(cells),
            "grid": grid,
            "summary": records["summary"],
            "ordering": "roll_number_then_name",
            "note": ("A printable grid of this session's students in roll "
                     "order. It is NOT a record of where anybody sat: no "
                     "seat position is stored anywhere in this system, and "
                     "none is inferred from the camera."),
        }

    @staticmethod
    def _seat_order(record):
        """
        Roll number first, numerically where it is a number.

        Roll numbers are free text, so "10" sorts before "9" as a string. A
        register ordered 1, 10, 11, 2 is the kind of small wrongness that
        makes a teacher stop trusting a printout, so a numeric roll number
        sorts numerically and everything else falls back to text after it.
        """
        roll = (record.get("roll_number") or "").strip()
        numeric = roll.isdigit()
        return (not roll,                      # blank roll numbers last
                not numeric,                   # then text rolls after numbers
                int(roll) if numeric else 0,
                roll.lower(),
                (record.get("student_name") or "").lower(),
                record.get("student_id") or "")

    # -- the same answers, as a printable file ------------------------------
    #
    # Three PDFs, all built from the SAME service calls the JSON endpoints
    # use, for the reason the CSV is: a file a school keeps and a table a
    # teacher looked at must not be able to disagree about a student.
    #
    # On the encoding limit - Helvetica cannot draw a name outside Latin-1, so
    # those characters print as "?" and the count comes back in the report.
    # See the header of backend/services/pdf_writer.py; the CSV is the
    # lossless export and the dashboard says so.

    # Column layouts: (heading, relative width, alignment). Relative, so the
    # same tuple describes the table on portrait and landscape paper.
    ATTENDANCE_PDF_COLUMNS = (
        ("Date", 1.0, "left"),
        ("Class", 1.1, "left"),
        ("Student ID", 1.1, "left"),
        ("Name", 2.4, "left"),
        ("Roll", 0.6, "left"),
        ("Status", 1.0, "left"),
        ("Presence", 0.8, "right"),
        ("Late", 0.5, "center"),
        ("Left early", 0.7, "center"),
        ("Corrected", 0.8, "center"),
        ("Policy", 0.9, "left"),
    )

    CLASS_PDF_COLUMNS = (
        ("Class", 1.6, "left"),
        ("Sessions", 0.8, "right"),
        ("Students", 0.8, "right"),
        ("Attendance", 1.0, "right"),
        ("Avg presence", 1.0, "right"),
        ("Present", 0.7, "right"),
        ("Late", 0.6, "right"),
        ("Partial", 0.7, "right"),
        ("Left early", 0.8, "right"),
        ("Absent", 0.7, "right"),
    )

    TERM_PDF_COLUMNS = (
        ("Term", 1.8, "left"),
        ("Class", 1.2, "left"),
        ("Sessions", 0.8, "right"),
        ("Expected", 0.8, "right"),
        ("Attendance", 1.0, "right"),
        ("Present", 0.7, "right"),
        ("Late", 0.6, "right"),
        ("Partial", 0.7, "right"),
        ("Absent", 0.7, "right"),
    )

    @staticmethod
    def _pdf_rate(value):
        """
        A rate for print: one decimal place, or a dash for "no data".

        "-" rather than "0.0%" is the whole point. A class with no completed
        session has no attendance rate, and printing zero would report every
        student in it as absent.
        """
        if value is None:
            return "-"
        return "%.1f%%" % (value * 100.0)

    def attendance_pdf(self, **filters):
        """
        The attendance report as a paginated PDF.

        Built from self.attendance, the same call behind the JSON and the CSV.
        A row the caller may not see is absent from all three.
        """
        from backend.services import pdf_writer

        report = self.attendance(**filters)
        active = {key: value for key, value
                  in (report["filters"] or {}).items() if value}

        rows = []
        for record in report["items"]:
            rows.append([
                record["session_date"],
                record["class_id"],
                record["student_id"],
                record["student_name"],
                record["roll_number"],
                # An unevaluated student is named as such, never as absent.
                record["status"] or "not evaluated",
                ("%.3f" % record["presence_ratio"]
                 if record["presence_ratio"] is not None else "-"),
                "yes" if record["was_late"] else "",
                "yes" if record["left_early"] else "",
                "yes" if record["is_override"] else "",
                record["policy_version"] or "-",
            ])

        summary = report["summary"]
        subtitles = [
            "%d rows  -  %d students  -  %d sessions  -  attendance %s"
            % (summary["rows"], summary["students"], summary["sessions"],
               self._pdf_rate(summary["attendance_rate"])),
            ("Filters: " + self._describe(active)) if active
            else "Filters: none - every record the signed-in account may see",
        ]
        if summary["unevaluated"]:
            subtitles.append(
                "%d expected student(s) were never evaluated and are listed "
                "with no status, not as absent." % summary["unevaluated"])
        if report["truncated"]:
            subtitles.append(
                "TRUNCATED: %d rows matched and the first %d are printed."
                % (report["total"], report["returned"]))

        body, meta = pdf_writer.write_table(
            "Attendance report", subtitles,
            self.ATTENDANCE_PDF_COLUMNS, rows, landscape=True,
            footer="ClassVision AI  -  decisions stored by the rule engine; "
                   "this report applies no threshold")

        meta["report"] = report
        return body, meta

    def class_summary_pdf(self, principal=None, class_id=None, date_from=None,
                          date_to=None):
        """Per-class summary as a PDF, from class_summary."""
        from backend.services import pdf_writer

        report = self.class_summary(principal=principal, class_id=class_id,
                                    date_from=date_from, date_to=date_to)

        rows = []
        for item in report["items"]:
            counts = item["counts"]
            rows.append([
                item["class_id"],
                item["completed_sessions"],
                item["student_count"],
                self._pdf_rate(item["attendance_rate"]),
                self._pdf_rate(item["average_presence_ratio"]),
                counts.get("PRESENT", 0),
                counts.get("LATE", 0),
                counts.get("PARTIAL", 0),
                counts.get("LEFT_EARLY", 0),
                counts.get("ABSENT", 0),
            ])

        totals = report["totals"]
        window = self._describe({"date_from": date_from, "date_to": date_to})

        subtitles = [
            "%d classes  -  %d completed sessions  -  attendance %s "
            "(weighted by expected students)"
            % (totals["classes"], totals["completed_sessions"],
               self._pdf_rate(totals["attendance_rate"])),
            ("Window: " + window) if window
            else "Window: every completed session to date",
            "Worst attendance first. Every rate is the one the analytics "
            "service computed for that class.",
        ]

        body, meta = pdf_writer.write_table(
            "Attendance by class", subtitles, self.CLASS_PDF_COLUMNS, rows,
            landscape=True, footer="ClassVision AI  -  per-class summary")

        meta["report"] = report
        return body, meta

    def term_summary_pdf(self, principal=None, academic_session_id=None):
        """
        Per-term summary as a PDF, from term_summary.

        A term's own line comes first and its classes follow it with the Term
        column blank: one flat table read as a hierarchy, so the pagination
        logic stays the single one in pdf_writer.
        """
        from backend.services import pdf_writer

        report = self.term_summary(principal=principal,
                                   academic_session_id=academic_session_id)

        rows = []
        for item in report["items"]:
            counts = item["counts"]
            rows.append([
                item["label"],
                "all classes",
                item["completed_sessions"],
                item["expected_students"],
                self._pdf_rate(item["attendance_rate"]),
                counts.get("PRESENT", 0),
                counts.get("LATE", 0),
                counts.get("PARTIAL", 0),
                counts.get("ABSENT", 0),
            ])
            for entry in item["classes"]:
                entry_counts = entry["counts"]
                rows.append([
                    "",
                    entry["class_id"],
                    entry["completed_sessions"],
                    entry["expected_students"],
                    self._pdf_rate(entry["attendance_rate"]),
                    entry_counts.get("PRESENT", 0),
                    entry_counts.get("LATE", 0),
                    entry_counts.get("PARTIAL", 0),
                    entry_counts.get("ABSENT", 0),
                ])

        unassigned = sum(1 for item in report["items"] if item["unassigned"])
        subtitles = [
            "%d term(s), each followed by its classes." % report["total"],
            "Figures are AnalyticsService.day_summary over the term's "
            "completed sessions.",
        ]
        if unassigned:
            subtitles.append(
                "Lessons recorded before academic terms existed are listed "
                "as not assigned to a term, not folded into one.")

        body, meta = pdf_writer.write_table(
            "Attendance by term", subtitles, self.TERM_PDF_COLUMNS, rows,
            landscape=True, footer="ClassVision AI  -  per-term summary")

        meta["report"] = report
        return body, meta

    def seat_map_pdf(self, session_id, principal=None, columns=6):
        """
        The seat map as a sheet a teacher can carry round a room.

        Returns None where seat_map does, so the route's 404 has one cause.

        The grid is drawn as a table whose columns are seat columns, each cell
        holding roll number, name and status. It is NOT a floor plan; the
        subtitle says so on the printed page, not only in this docstring,
        because the printed page is what leaves the building.
        """
        from backend.services import pdf_writer

        report = self.seat_map(session_id, principal=principal,
                               columns=columns)
        if report is None:
            return None

        # Equal shares: a seat is a seat, and the widest name does not get to
        # make its column wider than the one next to it.
        columns_spec = tuple(("Seat %d" % (index + 1), 1.0, "left")
                             for index in range(report["columns"]))

        rows = []
        for line in report["grid"]:
            for part in range(3):
                cells = []
                for record in line:
                    if part == 0:
                        roll = record["roll_number"] or "-"
                        cells.append("%s  %s" % (roll, record["student_id"]))
                    elif part == 1:
                        cells.append(record["student_name"] or "(unnamed)")
                    else:
                        cells.append(record["status"] or "not evaluated")
                rows.append(cells)
            # A blank line between seat rows, so three-line cells do not run
            # together into an unreadable block.
            rows.append(["" for _ in range(report["columns"])])

        summary = report["summary"]
        subtitles = [
            "%s  -  %s%s  -  %s"
            % (report["class_id"], report["session_date"],
               ("  section " + report["section"]) if report["section"] else "",
               report["status"]),
            "%d students  -  attendance %s"
            % (report["seats"], self._pdf_rate(summary["attendance_rate"])),
            "NOT a seating plan. No seat position is recorded by this system "
            "or inferred from the camera; students are listed in roll order.",
        ]

        body, meta = pdf_writer.write_table(
            "Seat map", subtitles, columns_spec, rows, landscape=True,
            footer="ClassVision AI  -  roll-order grid, not a floor plan")

        meta["report"] = report
        return body, meta

    @staticmethod
    def _describe(filters):
        """Active filters as one human phrase, for a subtitle line."""
        labels = {
            "student_id": "student", "session_id": "session",
            "class_id": "class", "class_name": "student class",
            "section": "section", "date_from": "from", "date_to": "to",
            "status": "status", "policy_version": "policy",
            "include_unevaluated": "including unevaluated",
        }
        parts = []
        for key, value in filters.items():
            if not value:
                continue
            label = labels.get(key, key)
            parts.append(label if value is True else "%s %s" % (label, value))
        return ", ".join(parts)
