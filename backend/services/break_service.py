"""
ClassVision AI - session breaks (Step 14)
-----------------------------------------
A period inside a lesson that does not count towards attendance.

WHY THIS IS A BACKEND FEATURE AND NOT A COUNTDOWN

    The rule engine decides attendance from

        ratio = present time / session length

    A ten-minute break in a sixty-minute lesson takes up to ten minutes of
    presence away from every student while leaving the denominator at sixty.
    Every ratio falls by a sixth, and a student sitting on the PARTIAL boundary
    is marked PARTIAL because the class was told to stand up and stretch.

    So the break has to reach the arithmetic. It is subtracted from BOTH sides:
    out of the denominator through AttendanceSession.duration_seconds, and out
    of the numerator by cutting the interval out of every presence segment -
    which is what makes leaving the room during a break cost nothing and
    staying in frame earn nothing.

THE CLOCK IS THE SERVER'S

    `ends_at` is computed once, here, when the break starts. A browser counting
    ten minutes on its own clock would disagree with the attendance arithmetic
    on any machine whose clock is off, and the arithmetic is the one that
    decides whether a child was present.

    The browser is told `ends_at` and `remaining_seconds` and counts down for
    display only. When it reaches zero it asks the server rather than deciding.

WHAT IT REFUSES

    A break on a session that is not running. There is nothing to interrupt.

    A second break while one is already running - the answer is the break that
    is already there, not a new one, so a double-clicked button cannot create
    two overlapping intervals.

    A break longer than the lesson it is inside. The interval is clipped to the
    session before it reaches attendance; see normalise_intervals.
"""

from datetime import datetime, timedelta, timezone

# Ten minutes, fixed. Not configurable, because the requirement is a
# ten-minute break and a configurable one would need a policy version to
# record WHICH length a historical session used.
BREAK_SECONDS = 600


class BreakError(ValueError):
    """Every message is written to be read by somebody mid-lesson."""


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds")


def _parse(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class BreakService:
    """Start, end and report breaks. One row per break, never deleted."""

    def __init__(self, database):
        self.database = database

    # -- reading ------------------------------------------------------------

    def breaks_for(self, session_id):
        """Every break this session has had, oldest first."""
        return [dict(row) for row in self.database.query(
            "SELECT * FROM session_breaks WHERE session_id = ? "
            "ORDER BY break_id", (session_id,))]

    def excluded_intervals(self, session_id):
        """
        The intervals to subtract from attendance, as datetimes.

        A break still running is excluded up to NOW rather than up to its
        planned end: finalising a session mid-break must not remove time the
        lesson never reached.
        """
        now = utc_now()
        intervals = []

        for row in self.breaks_for(session_id):
            start = _parse(row["started_at"])
            end = _parse(row["ended_at"]) or min(_parse(row["ends_at"]), now)
            if start and end and end > start:
                intervals.append((start, end))

        return intervals

    def current(self, session_id):
        """
        The break that is running, or None.

        "Running" is decided by comparing the stored end to the server's clock,
        so a break whose ten minutes have elapsed while nobody was looking
        reports as finished without anything having to tick.
        """
        now = utc_now()

        for row in reversed(self.breaks_for(session_id)):
            if row["ended_at"]:
                continue
            ends_at = _parse(row["ends_at"])
            if ends_at and ends_at > now:
                return self.describe(row, now=now)

        return None

    def describe(self, row, now=None):
        """One break as the API reports it."""
        now = now or utc_now()
        started_at = _parse(row["started_at"])
        ends_at = _parse(row["ends_at"])
        ended_at = _parse(row["ended_at"])

        finished = ended_at or (ends_at if ends_at and ends_at <= now else None)
        remaining = 0.0 if finished else max(
            0.0, (ends_at - now).total_seconds())

        return {
            "break_id": row["break_id"],
            "session_id": row["session_id"],
            "started_at": row["started_at"],
            "ends_at": row["ends_at"],
            "ended_at": row["ended_at"],
            "ended_early": bool(ended_at and ends_at and ended_at < ends_at),
            "active": finished is None,
            "remaining_seconds": round(remaining, 1),
            "length_seconds": BREAK_SECONDS,
            "elapsed_seconds": round(
                ((finished or now) - started_at).total_seconds(), 1),
            "started_by": row["started_by"],
            "ended_by": row["ended_by"],
        }

    def state(self, session_id):
        """
        What the dashboard needs in order to draw the break, in one answer.

        Folded into GET /api/live/status by the route, so the page's existing
        one-second poll carries it and no second polling loop exists.
        """
        if not session_id:
            return {"active": False, "break": None, "count": 0,
                    "excluded_seconds": 0.0, "length_seconds": BREAK_SECONDS}

        rows = self.breaks_for(session_id)
        current = self.current(session_id)

        excluded = sum((end - start).total_seconds()
                       for start, end in self.excluded_intervals(session_id))

        return {
            "active": current is not None,
            "break": current,
            "count": len(rows),
            "excluded_seconds": round(excluded, 1),
            "length_seconds": BREAK_SECONDS,
        }

    # -- writing ------------------------------------------------------------

    def start(self, session_id, actor=None, reason=""):
        """
        Begin a ten-minute break.

        Idempotent while one is running: the answer is the break that is
        already there. A double-clicked button therefore cannot create two
        overlapping intervals, which would be subtracted twice.
        """
        session = self.database.query_one(
            "SELECT session_id, status, start_time, end_time "
            "FROM attendance_sessions WHERE session_id = ?", (session_id,))

        if session is None:
            raise BreakError("No session called %s." % session_id)

        if session["status"] != "ACTIVE":
            raise BreakError(
                "A break can only be taken during a running session. This one "
                "is %s." % session["status"])

        running = self.current(session_id)
        if running is not None:
            return running

        now = utc_now()
        ends_at = now + timedelta(seconds=BREAK_SECONDS)

        # Never past the end of the lesson. A break cannot exclude time the
        # session does not contain - see normalise_intervals, which clips it
        # again on the way into the arithmetic.
        session_end = _parse(session["end_time"])
        if session_end and ends_at > session_end:
            ends_at = session_end

        if ends_at <= now:
            raise BreakError(
                "This session ends too soon for a break to fit inside it.")

        self.database.execute(
            "INSERT INTO session_breaks (session_id, started_at, ends_at, "
            "started_by, reason) VALUES (?, ?, ?, ?, ?)",
            (session_id, _iso(now), _iso(ends_at), actor, reason or ""))

        return self.current(session_id) or self.describe(
            self.breaks_for(session_id)[-1])

    def end(self, session_id, actor=None):
        """
        End the running break early.

        Supported because a class that comes back after four minutes should not
        have six minutes of teaching excluded from its attendance. The interval
        that reaches the arithmetic is what happened, not what was planned.
        """
        running = self.current(session_id)
        if running is None:
            raise BreakError("No break is running for this session.")

        self.database.execute(
            "UPDATE session_breaks SET ended_at = ?, ended_by = ? "
            "WHERE break_id = ?",
            (_iso(utc_now()), actor, running["break_id"]))

        row = self.database.query_one(
            "SELECT * FROM session_breaks WHERE break_id = ?",
            (running["break_id"],))
        return self.describe(row)
