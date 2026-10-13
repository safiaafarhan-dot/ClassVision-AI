"""
ClassVision AI - Classroom Session Lifecycle
---------------------------------------------
Editing, cancelling and correcting a lesson without damaging its attendance.

The problem this exists for

    A teacher creates Tuesday's session for 10:00 when it should have been
    11:00. What happens next depends entirely on one question - has the
    session already produced attendance?

        No     the session is just a plan. Correct it and move on.
        Yes    the attendance is a record of something that was observed.
               It is not editable by changing the plan it hung from.

    Everything in this module follows from that. Metadata is freely editable
    until evidence exists, and after that a change is a CORRECTION: recorded,
    attributed, reasoned, and never applied by silently recomputing a register.

Why cancel is not delete

    session_expected_students, attendance_records and presence_segments all
    cascade from attendance_sessions(session_id). Deleting a session that ran
    deletes the lesson's whole register, for everybody, with no trace that it
    ever existed. Cancelling leaves the row, marks it CANCELLED, and records
    who did it and why.

    Hard deletion still exists, because a session created by mistake five
    minutes ago is clutter rather than history. It is refused whenever
    anything references the session, and the refusal names what would be lost.

The states

    SCHEDULED -> ACTIVE -> COMPLETED          the ordinary life of a lesson
    SCHEDULED -> CANCELLED                    it never happened
    ACTIVE    -> CANCELLED                    it was abandoned

    COMPLETED is what the rest of this project has always called ENDED, and
    renaming it now would mean rewriting every stored row for no gain.

    "Needs correcting" is deliberately NOT a status. A session that ran and
    ended is COMPLETED, and noticing afterwards that its time was wrong does
    not change what happened - so it is a separate column, and the status
    keeps telling the truth about the lesson.
"""

from datetime import datetime, timezone

from attendance_session import (
    SESSION_ACTIVE,
    SESSION_CANCELLED,
    SESSION_COMPLETED,
    SESSION_SCHEDULED,
)
from database import CORRECTION_DONE, CORRECTION_NONE, CORRECTION_REQUIRED
from repositories import AttendanceRepository, AuditRepository, SessionRepository

# Fields a correction may touch. Deliberately excludes session_id, which
# everything else references, and status, which has its own transitions.
EDITABLE_FIELDS = ("class_id", "section", "teacher", "academic_session_id",
                   "session_date", "start_time", "end_time", "timezone")

# Changing one of these changes the WINDOW attendance is measured over, so
# doing it to a session that already has evidence needs a correction rather
# than an edit.
WINDOW_FIELDS = ("session_date", "start_time", "end_time", "timezone")


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds") if moment else None


class SessionError(ValueError):
    """Every message here is written to be shown to a teacher."""


class SessionConflict(SessionError):
    """Well formed, but it would damage something that already exists."""


class SessionNotFound(SessionError):
    """No session with that id."""


class SessionService:
    """The operations a teacher needs after a session has been created."""

    def __init__(self, database):
        self.database = database
        self.sessions = SessionRepository(database)
        self.attendance = AttendanceRepository(database)
        self.audit = AuditRepository(database)

    # -- reading -----------------------------------------------------------

    def get(self, session_id):
        row = self.sessions.get(session_id)
        if row is None:
            raise SessionNotFound("Session %s does not exist." % session_id)
        return row

    def evidence(self, session_id):
        """
        What this session has produced. The question every decision turns on.
        """
        row = self.database.query_one(
            """
            SELECT
              (SELECT COUNT(*) FROM attendance_records
                WHERE session_id = ?) AS attendance_records,
              (SELECT COUNT(*) FROM presence_segments
                WHERE session_id = ?) AS presence_segments,
              (SELECT COUNT(*) FROM session_expected_students
                WHERE session_id = ?) AS expected_students
            """, (session_id, session_id, session_id))

        found = dict(row)
        # Expected students are a PLAN, not evidence. A roll that has been
        # chosen but never run is still freely editable, which is the whole
        # point of being able to fix a session before the lesson.
        found["has_attendance"] = bool(found["attendance_records"]
                                       or found["presence_segments"])
        return found

    def details(self, session_id):
        """Everything the session detail page shows, in one call."""
        session = self.get(session_id)
        evidence = self.evidence(session_id)

        counts = {}
        for record in self.attendance.list_for_session(session_id):
            counts[record["status"]] = counts.get(record["status"], 0) + 1

        session["evidence"] = evidence
        session["attendance_counts"] = counts
        session["present"] = sum(
            counts.get(status, 0) for status in ("PRESENT", "LATE",
                                                 "LEFT_EARLY"))
        session["absent"] = counts.get("ABSENT", 0)
        session["available_actions"] = self.available_actions(session)
        session["audit"] = self.history(session_id)
        return session

    @staticmethod
    def available_actions(session):
        """
        What may be done to this session NOW.

        Computed on the server and sent to the browser, so the dashboard shows
        the same set the API will actually accept - rather than offering an
        End Session button on a session that ended last week.
        """
        status = session["status"]
        correction = session.get("correction_state") or CORRECTION_NONE

        if status == SESSION_SCHEDULED:
            return ["edit", "start", "cancel", "delete"]

        if status == SESSION_ACTIVE:
            return ["view_live", "end", "cancel"]

        if status == SESSION_COMPLETED:
            actions = ["view_attendance", "view_analytics", "correct"]
            if correction == CORRECTION_REQUIRED:
                actions.append("resolve_correction")
            return actions

        # CANCELLED
        return ["view_details", "view_audit"]

    def history(self, session_id):
        rows = self.database.query(
            "SELECT * FROM audit_log WHERE subject = ? "
            "ORDER BY event_id DESC LIMIT 50", (session_id,))
        return [dict(row) for row in rows]

    # -- editing -----------------------------------------------------------

    def edit(self, session_id, changes, actor=None, confirm_correction=False):
        """
        Correct a session's metadata.

        Free while the session has produced no evidence. Once it has, a change
        to the WINDOW - the date, the times, the timezone - is refused unless
        the caller explicitly confirms a correction, because those four
        decide what "present" meant.

        Even then, no attendance record is touched. The session is flagged
        CORRECTION_REQUIRED and the existing register stands. Re-deriving a
        past register from an edited plan would be inventing a different
        lesson.
        """
        session = self.get(session_id)

        if session["status"] == SESSION_CANCELLED:
            raise SessionConflict(
                "Session %s was cancelled and cannot be edited. Create a new "
                "session instead." % session_id)

        wanted = {field: value for field, value in (changes or {}).items()
                  if field in EDITABLE_FIELDS and value is not None}

        if not wanted:
            return session

        evidence = self.evidence(session_id)
        touches_window = [field for field in wanted if field in WINDOW_FIELDS]

        if evidence["has_attendance"] and touches_window:
            if not confirm_correction:
                raise SessionConflict(
                    "Session %s already has attendance (%d record(s), %d "
                    "presence segment(s)). Changing the date or times would "
                    "change the window that attendance was measured over. "
                    "Confirm a correction to proceed - the existing records "
                    "will be kept exactly as they are and the session will be "
                    "flagged for review."
                    % (session_id, evidence["attendance_records"],
                       evidence["presence_segments"]))

        before = {field: session.get(field) for field in wanted}

        with self.database.transaction():
            self._update_columns(session_id, dict(wanted, updated_at=None))

            if evidence["has_attendance"] and touches_window:
                self._flag_correction(
                    session_id, actor,
                    "window changed after attendance existed: %s"
                    % ", ".join(sorted(touches_window)))

        self.audit.record(
            "session.update", actor=actor, subject=session_id,
            detail="; ".join("%s: %r -> %r" % (field, before[field],
                                               wanted[field])
                             for field in sorted(wanted))[:500])

        return self.get(session_id)

    def _update_columns(self, session_id, values):
        values = {field: value for field, value in values.items()
                  if value is not None}
        if not values:
            return

        assignments = ", ".join("%s = ?" % field for field in values)
        self.database.execute(
            "UPDATE attendance_sessions SET %s WHERE session_id = ?"
            % assignments, tuple(values.values()) + (session_id,))

    def _flag_correction(self, session_id, actor, reason):
        self.database.execute(
            """
            UPDATE attendance_sessions
               SET correction_state = ?, correction_reason = ?,
                   correction_at = ?, correction_by = ?
             WHERE session_id = ?
            """,
            (CORRECTION_REQUIRED, reason, _iso(utc_now()), actor, session_id))

    # -- correction --------------------------------------------------------

    def flag_for_correction(self, session_id, reason, actor=None):
        """
        Say out loud that a finished session is wrong, without changing it.

        The register stays exactly as it was recorded. This marks it as
        disputed so a teacher reviewing attendance can see that somebody has
        questioned it - which is more useful, and far safer, than quietly
        recomputing it.
        """
        session = self.get(session_id)

        if not (reason or "").strip():
            raise SessionError("A reason is required to flag a correction.")

        with self.database.transaction():
            self._flag_correction(session_id, actor, reason.strip()[:500])

        self.audit.record("session.correction_started", actor=actor,
                          subject=session_id, detail=reason.strip()[:500])
        return self.get(session_id)

    def resolve_correction(self, session_id, note=None, actor=None):
        """Mark a flagged session reviewed. The register is still untouched."""
        self.get(session_id)

        with self.database.transaction():
            self.database.execute(
                """
                UPDATE attendance_sessions
                   SET correction_state = ?, correction_at = ?,
                       correction_by = ?,
                       correction_reason = COALESCE(?, correction_reason)
                 WHERE session_id = ?
                """,
                (CORRECTION_DONE, _iso(utc_now()), actor,
                 (note or "").strip()[:500] or None, session_id))

        self.audit.record("session.correction_completed", actor=actor,
                          subject=session_id, detail=(note or "")[:500])
        return self.get(session_id)

    # -- cancellation ------------------------------------------------------

    def cancel(self, session_id, reason=None, actor=None):
        """
        Retire a session without destroying anything.

        The row stays, its attendance stays, and it stops behaving like a
        lesson: it cannot be started, and the live classroom will not run it.
        """
        session = self.get(session_id)

        if session["status"] == SESSION_CANCELLED:
            # Idempotent: cancelling twice is not an error, and must not
            # overwrite who cancelled it the first time.
            return session

        if session["status"] == SESSION_COMPLETED:
            raise SessionConflict(
                "Session %s has already finished and produced attendance. "
                "Cancelling it now would make a register that exists belong "
                "to a lesson that did not happen. Flag it for correction "
                "instead." % session_id)

        with self.database.transaction():
            self._update_columns(session_id, {
                "status": SESSION_CANCELLED,
                "cancelled_at": _iso(utc_now()),
                "cancelled_by": actor,
                "cancellation_reason": (reason or "").strip()[:500] or None,
            })

        self.audit.record("session.cancelled", actor=actor, subject=session_id,
                          detail=(reason or "")[:500])
        return self.get(session_id)

    # -- deletion ----------------------------------------------------------

    def delete(self, session_id, confirm=None, actor=None):
        """
        Remove a session permanently. Refused whenever it would cost history.

        Three tables cascade from this row. A session that has been run has a
        register attached to it, and there is no version of "delete the
        lesson" that leaves the register meaningful - so the answer is a
        refusal naming what would go, and cancel as the alternative.

        A session nothing references may be deleted outright, because it is a
        plan somebody typed by mistake. Even then the confirmation must be the
        session's own id, for the same reason it is for a student: a button
        cannot be clicked into destroying a term of data.
        """
        session = self.get(session_id)
        evidence = self.evidence(session_id)

        if evidence["has_attendance"]:
            raise SessionConflict(
                "Session %s has %d attendance record(s) and %d presence "
                "segment(s). Deleting it would delete them too. Cancel it "
                "instead - a cancelled session keeps its history and stops "
                "behaving like a lesson."
                % (session_id, evidence["attendance_records"],
                   evidence["presence_segments"]))

        if session["status"] == SESSION_ACTIVE:
            raise SessionConflict(
                "Session %s is running. Stop it before deleting it."
                % session_id)

        if (confirm or "").strip() != session_id:
            raise SessionError(
                "To delete %s permanently, type its session id exactly."
                % session_id)

        with self.database.transaction():
            self.database.execute(
                "DELETE FROM session_expected_students WHERE session_id = ?",
                (session_id,))
            self.database.execute(
                "DELETE FROM attendance_sessions WHERE session_id = ?",
                (session_id,))

        # The audit row outlives the session, on purpose: it records the
        # session id as text, not as a foreign key, so deleting a session
        # never rewrites the history of who did what.
        self.audit.record("session.deleted", actor=actor, subject=session_id,
                          detail="no attendance existed; %d expected student "
                                 "row(s) removed" % evidence["expected_students"])

        return {"session_id": session_id, "deleted": True,
                "expected_students_removed": evidence["expected_students"]}

    # -- conflicts ---------------------------------------------------------

    def conflicts(self, class_id, start_time, end_time, section=None,
                  teacher=None, exclude=None):
        """
        Sessions that overlap this one. A warning, never a refusal.

        A class legitimately meets twice in a day, two sections legitimately
        run at once, and a teacher legitimately covers back-to-back lessons.
        So this reports what it found and lets an authorised person decide,
        rather than blocking a timetable it does not understand.
        """
        clauses = ["status != ?", "start_time < ?", "end_time > ?"]
        parameters = [SESSION_CANCELLED, _iso(end_time), _iso(start_time)]

        if exclude:
            clauses.append("session_id != ?")
            parameters.append(exclude)

        rows = self.database.query(
            "SELECT session_id, class_id, section, teacher, session_date, "
            "start_time, end_time, status FROM attendance_sessions "
            "WHERE %s ORDER BY start_time" % " AND ".join(clauses),
            tuple(parameters))

        overlapping = []
        for row in rows:
            entry = dict(row)
            reasons = []

            if (entry["class_id"] or "").lower() == (class_id or "").lower():
                if not section or (entry["section"] or "").lower() == \
                        (section or "").lower():
                    reasons.append("same class and section")
                else:
                    reasons.append("same class, different section")

            if teacher and (entry["teacher"] or "").lower() == teacher.lower():
                reasons.append("same teacher")

            if entry["status"] == SESSION_ACTIVE:
                reasons.append("currently running")

            if reasons:
                entry["reasons"] = reasons
                overlapping.append(entry)

        return overlapping
