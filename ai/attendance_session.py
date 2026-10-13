"""
ClassVision AI - Attendance Session (Step 6)
--------------------------------------------
The window of time that attendance is decided for.

Why a session is its own concept
    Attendance only means anything relative to a period: "present for what?"
    A session gives presence a denominator, a start to be late against, and an
    end to leave early from.

    It also decides WHEN a conclusion may be drawn. Absence is not a fact
    about a moment; it is a fact about a finished session. Until the session
    is explicitly completed, an expected student who has not appeared is
    UNKNOWN, not absent.

Lifecycle
    SCHEDULED -> ACTIVE -> COMPLETED
                        -> CANCELLED
    SCHEDULED -> CANCELLED

    A session is NEVER completed automatically. Closing the camera window
    does not finish a session - somebody has to say so, because "the recording
    stopped" and "the lesson ended" are different claims.

Time convention
    All timestamps are timezone-aware UTC, matching the Step 5 timeline. The
    policy carries a timezone name for display purposes; the arithmetic here
    is always UTC.
"""

from datetime import datetime, timezone

SESSION_SCHEDULED = "SCHEDULED"
SESSION_ACTIVE = "ACTIVE"
SESSION_COMPLETED = "COMPLETED"
SESSION_CANCELLED = "CANCELLED"

ALL_SESSION_STATUSES = (SESSION_SCHEDULED, SESSION_ACTIVE,
                        SESSION_COMPLETED, SESSION_CANCELLED)

# Which transitions are allowed. Anything else is refused.
ALLOWED_TRANSITIONS = {
    SESSION_SCHEDULED: (SESSION_ACTIVE, SESSION_CANCELLED),
    SESSION_ACTIVE: (SESSION_COMPLETED, SESSION_CANCELLED),
    SESSION_COMPLETED: (),
    SESSION_CANCELLED: (),
}


class SessionStateError(ValueError):
    """Raised on an illegal session transition."""


def _utc(value, label):
    if not isinstance(value, datetime):
        raise ValueError("%s must be a datetime, got %s"
                         % (label, type(value).__name__))
    if value.tzinfo is None:
        raise ValueError("%s must be timezone aware (UTC)" % label)
    return value.astimezone(timezone.utc)


def normalise_intervals(intervals, window_start, window_end):
    """
    Excluded intervals, clipped to the session and merged.

    Merging matters: two breaks that overlap must not have their shared time
    subtracted twice, which would shrink the denominator below the time that
    was actually taught and inflate every attendance rate in the session.

    Clipping matters for the same reason in reverse - a break that runs past
    the end of the lesson may only remove time the lesson contained.
    """
    if not intervals:
        return ()

    clipped = []
    for start, end in intervals:
        start = _utc(start, "excluded interval start")
        end = _utc(end, "excluded interval end")
        start = max(start, window_start)
        end = min(end, window_end)
        if end > start:
            clipped.append((start, end))

    if not clipped:
        return ()

    clipped.sort()
    merged = [clipped[0]]
    for start, end in clipped[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return tuple(merged)


class AttendanceSession:
    """One class period, for one group, on one date."""

    __slots__ = ("session_id", "class_id", "session_date", "start_time",
                 "end_time", "timezone_name", "status", "policy_version",
                 "created_at", "completed_at", "expected_students",
                 "excluded_intervals")

    def __init__(self, session_id, class_id, start_time, end_time,
                 policy_version="1.0", timezone_name="UTC",
                 status=SESSION_SCHEDULED, session_date=None,
                 created_at=None, completed_at=None, expected_students=None,
                 excluded_intervals=None):
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not class_id:
            raise ValueError("class_id must not be empty")

        self.session_id = str(session_id)
        self.class_id = str(class_id)
        self.start_time = _utc(start_time, "start_time")
        self.end_time = _utc(end_time, "end_time")

        if self.end_time <= self.start_time:
            raise ValueError(
                "session end (%s) must be after its start (%s)"
                % (self.end_time.isoformat(), self.start_time.isoformat()))

        if status not in ALL_SESSION_STATUSES:
            raise ValueError("unknown session status %r" % (status,))

        self.status = status
        self.policy_version = str(policy_version)
        self.timezone_name = str(timezone_name)
        self.session_date = session_date or self.start_time.date().isoformat()
        self.created_at = created_at or datetime.now(timezone.utc)
        self.completed_at = completed_at

        # Who is supposed to be here. Absence is only meaningful against this.
        self.expected_students = sorted(set(expected_students or []))

        # Periods that do not count towards attendance - breaks. Empty for
        # every session that has never had one, which is what keeps this
        # addition invisible to everything that predates it.
        self.excluded_intervals = normalise_intervals(
            excluded_intervals, self.start_time, self.end_time)

    @property
    def duration_seconds(self):
        """
        The length of session that attendance is measured against.

        Excluded intervals are subtracted: attendance is a share of the time
        the class was being TAUGHT. A session with no excluded interval returns
        exactly what it always did - the subtraction is skipped, not applied as
        a zero.
        """
        total = self.scheduled_seconds

        if not self.excluded_intervals:
            return total

        return max(0.0, total - self.excluded_seconds)

    @property
    def scheduled_seconds(self):
        """
        Wall-clock length, breaks included.

        Named for what it is so that anything wanting "how long was the room
        booked" cannot accidentally get the attendance denominator, or the
        reverse.
        """
        return (self.end_time - self.start_time).total_seconds()

    @property
    def excluded_seconds(self):
        """How much of this session does not count towards attendance."""
        return sum((end - start).total_seconds()
                   for start, end in self.excluded_intervals)

    @property
    def is_completed(self):
        return self.status == SESSION_COMPLETED

    @property
    def is_active(self):
        return self.status == SESSION_ACTIVE

    @property
    def can_finalize_absence(self):
        """Absence may only be concluded for a completed session."""
        return self.status == SESSION_COMPLETED

    def _transition(self, new_status):
        allowed = ALLOWED_TRANSITIONS.get(self.status, ())
        if new_status not in allowed:
            raise SessionStateError(
                "cannot move a session from %s to %s (allowed: %s)"
                % (self.status, new_status,
                   ", ".join(allowed) if allowed else "nothing"))
        self.status = new_status

    def start(self):
        """Mark the session as running."""
        self._transition(SESSION_ACTIVE)
        return self

    def complete(self, completed_at=None):
        """
        Mark the session finished.

        This is the gate for final attendance: only now can an expected
        student who never appeared be called ABSENT.
        """
        self._transition(SESSION_COMPLETED)
        self.completed_at = completed_at or datetime.now(timezone.utc)
        return self

    def cancel(self):
        """
        Abandon the session.

        A cancelled session produces no absences - nothing happened that
        anyone could be absent from.
        """
        self._transition(SESSION_CANCELLED)
        return self

    def add_expected_student(self, student_id):
        if student_id and student_id not in self.expected_students:
            self.expected_students = sorted(self.expected_students + [student_id])
        return self

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "class_id": self.class_id,
            "session_date": self.session_date,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "timezone": self.timezone_name,
            "status": self.status,
            "policy_version": self.policy_version,
            "created_at": self.created_at.isoformat(),
            "completed_at": (self.completed_at.isoformat()
                             if self.completed_at else None),
            "duration_seconds": round(self.duration_seconds, 3),
            "expected_student_count": len(self.expected_students),
        }

    def __repr__(self):
        return "AttendanceSession(%s, class=%s, %s, %.0fs, expected=%d)" % (
            self.session_id, self.class_id, self.status,
            self.duration_seconds, len(self.expected_students))
