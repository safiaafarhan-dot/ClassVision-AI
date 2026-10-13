"""
ClassVision AI - Attendance Rules (Step 6)
------------------------------------------
Turns presence evidence into an explicit, explainable attendance decision.

    Step 6 introduces deterministic attendance rules and SQLite persistence.

    Attendance accuracy has NOT been validated on a real classroom
    population. Everything here is verified against synthetic timelines only.

Where this sits
    camera -> detection -> tracking -> recognition -> presence timeline
           -> ATTENDANCE RULES (this module) -> decision -> database

    The presence timeline is the evidence. This module is the decision. The
    database is only storage. Those three stay separate so a school can change
    its attendance policy and recompute history without re-running a camera.

This module knows nothing about SQL, cameras, or models. It takes presence
segments and a policy, and returns decisions. That is what makes it testable.

Two rules that matter more than the arithmetic
    1. An UNKNOWN face NEVER contributes to anybody's attendance. Only a
       verified student identity produces presence.
    2. ABSENT is only ever decided for a COMPLETED session. A live session
       that was short, interrupted, or is still running cannot mark anyone
       absent - the system simply does not know yet.
"""

from datetime import datetime, timedelta, timezone

from presence_timeline import (
    EVENT_IDENTITY_CHANGED,
    EVENT_IDENTITY_CONFIRMED,
    EVENT_TRACK_EXITED,
    EVENT_TRACK_REACQUIRED,
    EVENT_TRACK_TEMPORARILY_LOST,
)

# ---------------------------------------------------------------------------
# Attendance states
# ---------------------------------------------------------------------------
#
# PRESENT          met the policy's duration and ratio requirements, arrived
#                  before the late threshold, and did not leave early.
# LATE             met the presence requirements, but was first verified
#                  after the late threshold.
# PARTIAL          was genuinely seen, but for less than the policy requires.
# LEFT_EARLY       met the presence requirements, but was last seen before
#                  the early-leave threshold.
# ABSENT           expected, but had no verified presence at all, in a
#                  session that has been explicitly completed.
# UNKNOWN          the system cannot decide yet - typically an expected
#                  student in a session that is still active.
# MANUAL_OVERRIDE  a human replaced the computed result. Permitted as a
#                  stored status; the audit trail lives alongside it.
STATUS_PRESENT = "PRESENT"
STATUS_LATE = "LATE"
STATUS_PARTIAL = "PARTIAL"
STATUS_LEFT_EARLY = "LEFT_EARLY"
STATUS_ABSENT = "ABSENT"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

ALL_STATUSES = (
    STATUS_PRESENT, STATUS_LATE, STATUS_PARTIAL, STATUS_LEFT_EARLY,
    STATUS_ABSENT, STATUS_UNKNOWN, STATUS_MANUAL_OVERRIDE,
)

# Which status wins when more than one description fits. A student can be
# both late AND have left early; a single status must be reported, so the
# order is fixed and documented rather than left to chance. Nothing is lost -
# the was_late and left_early flags are always recorded in the evidence.
STATUS_PRECEDENCE = (
    STATUS_ABSENT,      # no presence at all
    STATUS_PARTIAL,     # not enough presence
    STATUS_LATE,        # enough presence, but arrived late
    STATUS_LEFT_EARLY,  # enough presence, on time, but left early
    STATUS_PRESENT,
)

# ---------------------------------------------------------------------------
# Development defaults
# ---------------------------------------------------------------------------
#
# THESE ARE DEVELOPMENT DEFAULTS, NOT AN INSTITUTIONAL POLICY.
#
# No school's rules are encoded here. Every one of these is a placeholder
# chosen so the engine runs out of the box, and every one is expected to be
# replaced by whatever an institution actually requires.
DEFAULT_POLICY_VERSION = "1.0"
DEFAULT_MINIMUM_PRESENCE_SECONDS = 0
DEFAULT_MINIMUM_PRESENCE_RATIO = 0.75
DEFAULT_LATE_AFTER_SECONDS = 600           # 10 minutes past the start
DEFAULT_EARLY_LEAVE_BEFORE_SECONDS = 300   # last 5 minutes of the session
DEFAULT_TEMPORARY_LOSS_GRACE_SECONDS = 30
DEFAULT_TIMEZONE = "UTC"


class AttendancePolicyError(ValueError):
    """Raised when a policy is not internally consistent."""


# ---------------------------------------------------------------------------
# The duration-based grace period
# ---------------------------------------------------------------------------
#
# A longer class forgives more absence than a short one, because the same five
# minutes mean different things in a half-hour tutorial and a three-hour lab.
#
#     total class duration           grace
#     -------------------------      -----
#     30 minutes or less             0 minutes
#     more than 30, up to 60         5 minutes
#     more than 60, up to 90        10 minutes
#     more than 90                  15 minutes
#
# WHAT THE GRACE PERIOD IS NOT
#
#     It is not added to the student's present time, and it does not touch
#     the presence timeline. A student present for 55 minutes of an hour was
#     present for 55 minutes, missing for 5, and the records say exactly that
#     both before and after this rule existed. The grace period is consulted
#     ONCE, at the point where a status is chosen, and it changes nothing
#     else - which is why the evidence can still be used to recompute history
#     under a policy with different bands, or with no bands at all.
#
# The bands are a POLICY SETTING, like every other threshold in this module.
# The table above is the project's development default and is expected to be
# replaced by whatever an institution actually requires; it is written here
# once so that no route, service or template can hold a second copy of it.
#
# Each band is (upper_bound_minutes_inclusive, grace_minutes). The last band
# must have no upper bound, written as None: a class can always be longer
# than the longest band named.
DEFAULT_GRACE_BANDS = (
    (30.0, 0),
    (60.0, 5),
    (90.0, 10),
    (None, 15),
)


def grace_minutes_for_duration(class_duration_minutes, bands=DEFAULT_GRACE_BANDS):
    """
    The grace period, in whole minutes, for a class of this length.

    The comparison is "up to and including", so a 60-minute class takes the
    60-minute band's 5 minutes and a 60.1-minute class takes the next band's
    10. Binary floating point does not threaten that boundary: the risky
    values are the ones written as decimals, and 60.1 is stored as slightly
    MORE than 60.1, so it lands on the generous side of the only comparison
    it is used in.

    Returns 0 for a duration that is zero, negative or not a number, because
    a class with no length cannot have been missed.
    """
    if not bands:
        return 0

    try:
        minutes = float(class_duration_minutes)
    except (TypeError, ValueError):
        return 0

    if minutes != minutes or minutes <= 0:   # NaN, zero or negative
        return 0

    for upper_bound, grace in bands:
        if upper_bound is None or minutes <= float(upper_bound):
            return int(grace)

    # Only reachable from a band table whose last entry is bounded, which
    # validate_grace_bands refuses. Falling back to the largest grace named is
    # the safe direction: it cannot turn a PRESENT into an ABSENT.
    return int(bands[-1][1])


def grace_seconds_for_duration(session_seconds, bands=DEFAULT_GRACE_BANDS):
    """
    The same answer in seconds, which is the unit the rest of this module and
    the whole database use. The bands are expressed in minutes because that is
    how an institution states the rule; everything downstream is seconds.
    """
    try:
        seconds = float(session_seconds)
    except (TypeError, ValueError):
        return 0.0

    return float(grace_minutes_for_duration(seconds / 60.0, bands)) * 60.0


def validate_grace_bands(bands):
    """
    Reject a band table that cannot mean anything, loudly.

    Three rules: the bounds ascend, no grace is negative, and the last band is
    open-ended. A table whose last bound is a number has classes it does not
    answer for, and silently guessing at those is how a rule engine stops
    being explainable.
    """
    if bands is None or bands == ():
        return ()

    cleaned = []
    previous = None

    for index, band in enumerate(bands):
        try:
            upper_bound, grace = band
        except (TypeError, ValueError):
            raise AttendancePolicyError(
                "grace band %d must be a (upper_bound_minutes, grace_minutes) "
                "pair, got %r" % (index, band))

        grace = int(grace)
        if grace < 0:
            raise AttendancePolicyError(
                "grace band %d has a negative grace of %d minutes"
                % (index, grace))

        if upper_bound is None:
            if index != len(bands) - 1:
                raise AttendancePolicyError(
                    "only the LAST grace band may be open-ended; band %d of "
                    "%d has no upper bound" % (index, len(bands)))
            cleaned.append((None, grace))
            continue

        upper_bound = float(upper_bound)
        if upper_bound <= 0:
            raise AttendancePolicyError(
                "grace band %d has a non-positive upper bound of %r"
                % (index, upper_bound))

        if previous is not None and upper_bound <= previous:
            raise AttendancePolicyError(
                "grace bands must ascend; band %d ends at %r minutes, which "
                "is not after the previous band's %r"
                % (index, upper_bound, previous))

        previous = upper_bound
        cleaned.append((upper_bound, grace))

    if cleaned and cleaned[-1][0] is not None:
        raise AttendancePolicyError(
            "the last grace band must be open-ended (an upper bound of None), "
            "otherwise a class longer than %r minutes has no band"
            % cleaned[-1][0])

    return tuple(cleaned)


class AttendancePolicy:
    """
    The configurable rules used to turn presence into attendance.

    Every threshold is a setting. Nothing about a particular school is baked
    into the engine.
    """

    def __init__(self, version=DEFAULT_POLICY_VERSION,
                 minimum_presence_seconds=DEFAULT_MINIMUM_PRESENCE_SECONDS,
                 minimum_presence_ratio=DEFAULT_MINIMUM_PRESENCE_RATIO,
                 late_after_seconds=DEFAULT_LATE_AFTER_SECONDS,
                 early_leave_before_seconds=DEFAULT_EARLY_LEAVE_BEFORE_SECONDS,
                 temporary_loss_grace_seconds=DEFAULT_TEMPORARY_LOSS_GRACE_SECONDS,
                 count_unverified_presence=False,
                 allow_partial=True,
                 duration_grace_bands=DEFAULT_GRACE_BANDS,
                 timezone_name=DEFAULT_TIMEZONE):
        self.version = str(version)
        self.minimum_presence_seconds = float(minimum_presence_seconds)
        self.minimum_presence_ratio = float(minimum_presence_ratio)
        self.late_after_seconds = (None if late_after_seconds is None
                                   else float(late_after_seconds))
        self.early_leave_before_seconds = (None if early_leave_before_seconds is None
                                           else float(early_leave_before_seconds))
        self.temporary_loss_grace_seconds = float(temporary_loss_grace_seconds)
        self.count_unverified_presence = bool(count_unverified_presence)
        self.allow_partial = bool(allow_partial)
        # Pass () or None to switch the relaxation off entirely, which makes
        # every grace period zero and the engine behave exactly as it did
        # before the rule existed.
        self.duration_grace_bands = validate_grace_bands(duration_grace_bands)
        self.timezone_name = str(timezone_name)

        self.validate()

    def validate(self):
        """Reject a policy that cannot mean anything, loudly."""
        if not self.version:
            raise AttendancePolicyError("policy version must not be empty")

        if self.minimum_presence_seconds < 0:
            raise AttendancePolicyError(
                "minimum_presence_seconds must not be negative, got %r"
                % self.minimum_presence_seconds)

        if not (0.0 <= self.minimum_presence_ratio <= 1.0):
            raise AttendancePolicyError(
                "minimum_presence_ratio must be between 0 and 1, got %r"
                % self.minimum_presence_ratio)

        if self.late_after_seconds is not None and self.late_after_seconds < 0:
            raise AttendancePolicyError(
                "late_after_seconds must not be negative, got %r"
                % self.late_after_seconds)

        if self.early_leave_before_seconds is not None \
                and self.early_leave_before_seconds < 0:
            raise AttendancePolicyError(
                "early_leave_before_seconds must not be negative, got %r"
                % self.early_leave_before_seconds)

        if self.temporary_loss_grace_seconds < 0:
            raise AttendancePolicyError(
                "temporary_loss_grace_seconds must not be negative, got %r"
                % self.temporary_loss_grace_seconds)

        validate_grace_bands(self.duration_grace_bands)

        if self.count_unverified_presence:
            # Allowed, but it is a serious choice: it would let an
            # unidentified person contribute to someone's attendance.
            raise AttendancePolicyError(
                "count_unverified_presence is not supported: unverified "
                "presence must never contribute to a student's attendance")

        return self

    def to_dict(self):
        return {
            "version": self.version,
            "minimum_presence_seconds": self.minimum_presence_seconds,
            "minimum_presence_ratio": self.minimum_presence_ratio,
            "late_after_seconds": self.late_after_seconds,
            "early_leave_before_seconds": self.early_leave_before_seconds,
            "temporary_loss_grace_seconds": self.temporary_loss_grace_seconds,
            "allow_partial": self.allow_partial,
            "duration_grace_bands": [list(band)
                                     for band in self.duration_grace_bands],
            "timezone_name": self.timezone_name,
        }

    def grace_minutes_for(self, session_seconds):
        """The grace period in minutes for a session of this length."""
        return grace_minutes_for_duration(float(session_seconds) / 60.0,
                                          self.duration_grace_bands)

    def grace_seconds_for(self, session_seconds):
        """
        The grace period in seconds for a session of this length.

        The one place the engine asks the question, and it asks the shared
        function rather than carrying a copy of the table.
        """
        return grace_seconds_for_duration(session_seconds,
                                          self.duration_grace_bands)

    def __repr__(self):
        return ("AttendancePolicy(version=%r, min_ratio=%.2f, min_seconds=%.0f)"
                % (self.version, self.minimum_presence_ratio,
                   self.minimum_presence_seconds))


class PresenceSegment:
    """
    One continuous period during which a VERIFIED student was present.

    A segment only ever exists for a confirmed identity. There is no such
    thing as a segment for an unknown face.
    """

    __slots__ = ("student_id", "track_id", "start", "end")

    def __init__(self, student_id, start, end, track_id=None):
        if not student_id:
            raise ValueError("a presence segment requires a verified student_id")

        self.student_id = str(student_id)
        self.track_id = track_id
        self.start = _check_time(start, "segment start")
        self.end = _check_time(end, "segment end")

        if self.end < self.start:
            raise ValueError(
                "segment ends before it starts (%s -> %s)"
                % (self.start.isoformat(), self.end.isoformat()))

    @property
    def duration_seconds(self):
        return (self.end - self.start).total_seconds()

    def clipped_to(self, start, end):
        """
        The part of this segment inside a window, or None.

        Presence recorded outside the session must not count towards it.
        """
        new_start = max(self.start, start)
        new_end = min(self.end, end)

        if new_end <= new_start:
            return None

        return PresenceSegment(self.student_id, new_start, new_end, self.track_id)

    def to_dict(self):
        return {
            "student_id": self.student_id,
            "track_id": self.track_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
        }

    def __repr__(self):
        return "PresenceSegment(%s, %s -> %s, %.1fs)" % (
            self.student_id, self.start.strftime("%H:%M:%S"),
            self.end.strftime("%H:%M:%S"), self.duration_seconds)


def _check_time(value, label):
    """Every timestamp must be a timezone-aware datetime, in UTC."""
    if not isinstance(value, datetime):
        raise ValueError("%s must be a datetime, got %s"
                         % (label, type(value).__name__))

    if value.tzinfo is None:
        raise ValueError(
            "%s has no timezone. All ClassVision timestamps are UTC-aware; a "
            "naive datetime is ambiguous and is refused." % label)

    return value.astimezone(timezone.utc)


def merge_segments(segments):
    """
    Combine overlapping or touching segments for the SAME student.

    This is what stops double counting. Two tracks covering
    09:00-09:20 and 09:10-09:40 are 40 minutes of presence, not 50.

    Returns a new sorted list. Zero-length segments are dropped.
    """
    by_student = {}
    for segment in segments:
        by_student.setdefault(segment.student_id, []).append(segment)

    merged = []
    for student_id in sorted(by_student):
        ordered = sorted(by_student[student_id], key=lambda s: (s.start, s.end))
        current = None

        for segment in ordered:
            if segment.duration_seconds <= 0:
                continue

            if current is None:
                current = PresenceSegment(student_id, segment.start,
                                          segment.end, segment.track_id)
                continue

            if segment.start <= current.end:
                # Overlapping or adjacent - extend rather than add.
                if segment.end > current.end:
                    current.end = segment.end
            else:
                merged.append(current)
                current = PresenceSegment(student_id, segment.start,
                                          segment.end, segment.track_id)

        if current is not None:
            merged.append(current)

    return merged


class PresenceSummary:
    """Everything the rules need to know about one student's presence."""

    __slots__ = ("student_id", "segments", "first_seen", "last_seen",
                 "present_seconds", "segment_count", "longest_segment_seconds",
                 "track_ids", "loss_periods", "loss_seconds")

    def __init__(self, student_id, segments):
        self.student_id = student_id
        self.segments = list(segments)
        self.segment_count = len(self.segments)

        if self.segments:
            self.first_seen = min(s.start for s in self.segments)
            self.last_seen = max(s.end for s in self.segments)
            self.present_seconds = sum(s.duration_seconds for s in self.segments)
            self.longest_segment_seconds = max(s.duration_seconds
                                               for s in self.segments)
        else:
            self.first_seen = None
            self.last_seen = None
            self.present_seconds = 0.0
            self.longest_segment_seconds = 0.0

        self.track_ids = sorted({s.track_id for s in self.segments
                                 if s.track_id is not None})

        # Gaps BETWEEN segments: the student was away and came back.
        self.loss_periods = max(self.segment_count - 1, 0)
        self.loss_seconds = 0.0
        ordered = sorted(self.segments, key=lambda s: s.start)
        for previous, following in zip(ordered, ordered[1:]):
            self.loss_seconds += (following.start - previous.end).total_seconds()

    @property
    def has_presence(self):
        return self.present_seconds > 0

    def to_dict(self):
        return {
            "student_id": self.student_id,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "present_seconds": round(self.present_seconds, 3),
            "segment_count": self.segment_count,
            "longest_segment_seconds": round(self.longest_segment_seconds, 3),
            "track_ids": list(self.track_ids),
            "loss_periods": self.loss_periods,
            "loss_seconds": round(self.loss_seconds, 3),
        }


def subtract_intervals(segments, intervals):
    """
    Remove excluded time from a list of segments, splitting where needed.

    A segment that straddles a break becomes two segments; one entirely inside
    a break disappears. The excluded intervals are expected already merged and
    sorted - see normalise_intervals in attendance_session.

    Why presence inside a break is removed at all: if it were kept, a student
    who stayed in their seat through the break would score more present time
    than one who went to get water, and attendance would be measuring
    willingness to sit still rather than attendance.
    """
    if not intervals:
        return segments

    remaining = list(segments)

    for excluded_start, excluded_end in intervals:
        kept = []
        for segment in remaining:
            if segment.end <= excluded_start or segment.start >= excluded_end:
                kept.append(segment)
                continue

            before = segment.clipped_to(segment.start, excluded_start)
            if before is not None:
                kept.append(before)

            after = segment.clipped_to(excluded_end, segment.end)
            if after is not None:
                kept.append(after)
        remaining = kept

    return remaining


def summarize_presence(student_id, segments, window_start=None,
                       window_end=None, excluded_intervals=None):
    """Clip, merge and summarise one student's segments."""
    mine = [s for s in segments if s.student_id == student_id]

    if window_start is not None and window_end is not None:
        clipped = []
        for segment in mine:
            part = segment.clipped_to(window_start, window_end)
            if part is not None:
                clipped.append(part)
        mine = clipped

    # Breaks come out of the numerator as well as the denominator. Skipped
    # entirely when there are none, so a session without a break is summarised
    # exactly as it was before this existed.
    if excluded_intervals:
        mine = subtract_intervals(mine, excluded_intervals)

    return PresenceSummary(student_id, merge_segments(mine))


class AttendanceDecision:
    """
    One student's attendance for one session, with the evidence behind it.

    Every decision explains itself. A result nobody can question is a result
    nobody can correct.
    """

    __slots__ = ("student_id", "session_id", "status", "reason",
                 "present_seconds", "session_seconds", "presence_ratio",
                 "first_seen", "last_seen", "segment_count",
                 "longest_segment_seconds", "loss_periods", "loss_seconds",
                 "track_ids", "was_late", "left_early", "grace_seconds",
                 "policy_version", "evaluated_at")

    def __init__(self, student_id, session_id, status, reason, policy_version,
                 summary=None, session_seconds=0.0, presence_ratio=0.0,
                 was_late=False, left_early=False, evaluated_at=None,
                 grace_seconds=0.0):
        self.student_id = student_id
        self.session_id = session_id
        self.status = status
        self.reason = reason
        self.policy_version = policy_version

        self.session_seconds = float(session_seconds)
        self.presence_ratio = float(presence_ratio)
        self.was_late = bool(was_late)
        self.left_early = bool(left_early)
        self.grace_seconds = float(grace_seconds)
        self.evaluated_at = evaluated_at or datetime.now(timezone.utc)

        if summary is not None:
            self.present_seconds = summary.present_seconds
            self.first_seen = summary.first_seen
            self.last_seen = summary.last_seen
            self.segment_count = summary.segment_count
            self.longest_segment_seconds = summary.longest_segment_seconds
            self.loss_periods = summary.loss_periods
            self.loss_seconds = summary.loss_seconds
            self.track_ids = list(summary.track_ids)
        else:
            self.present_seconds = 0.0
            self.first_seen = None
            self.last_seen = None
            self.segment_count = 0
            self.longest_segment_seconds = 0.0
            self.loss_periods = 0
            self.loss_seconds = 0.0
            self.track_ids = []

    @property
    def missing_seconds(self):
        """
        How long the student was NOT present for, as measured.

        session length minus present time, and deliberately nothing else. It
        is never reduced by the grace period: a student who missed five
        minutes missed five minutes, and the record has to go on saying so
        after the grace period has decided that it was allowable.

        Clamped at zero only because presence is clipped to the session
        window, so a negative here would be an arithmetic bug rather than a
        student who attended more than the whole class.
        """
        return max(0.0, self.session_seconds - self.present_seconds)

    @property
    def within_grace(self):
        """
        Whether the absence measured here fits inside the class's allowance.

        A MEASUREMENT, not a claim about how this status was reached. It is
        true for a student who missed two minutes of an hour and met the
        ratio comfortably, who needed no relaxation at all - and the reason
        text, not this flag, is what says whether the grace period is what
        granted the PRESENT.

        False when nothing was missed: there is no absence to be inside an
        allowance, and recording a student who attended the whole class as
        having used up some grace would be a small untruth a parent could
        reasonably ask about.
        """
        missing = self.missing_seconds
        return bool(missing > 0 and missing <= self.grace_seconds)

    def to_dict(self):
        """Structured, loggable, and free of any biometric data."""
        return {
            "student_id": self.student_id,
            "session_id": self.session_id,
            "status": self.status,
            "reason": self.reason,
            "present_seconds": round(self.present_seconds, 3),
            "session_seconds": round(self.session_seconds, 3),
            "missing_seconds": round(self.missing_seconds, 3),
            "grace_seconds": round(self.grace_seconds, 3),
            "within_grace": self.within_grace,
            "presence_ratio": round(self.presence_ratio, 4),
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "segment_count": self.segment_count,
            "longest_segment_seconds": round(self.longest_segment_seconds, 3),
            "loss_periods": self.loss_periods,
            "loss_seconds": round(self.loss_seconds, 3),
            "track_ids": list(self.track_ids),
            "was_late": self.was_late,
            "left_early": self.left_early,
            "policy_version": self.policy_version,
        }

    def __repr__(self):
        return "AttendanceDecision(%s, %s, %.0fs, ratio=%.3f)" % (
            self.student_id, self.status, self.present_seconds,
            self.presence_ratio)


class AttendanceRuleEngine:
    """
    Applies an AttendancePolicy to presence evidence.

    Deliberately has no database, no camera, and no model. Give it segments
    and a session; it returns decisions.
    """

    def __init__(self, policy=None):
        self.policy = policy or AttendancePolicy()
        self.policy.validate()

    def evaluate_student(self, student_id, segments, session, evaluated_at=None):
        """
        Decide one student's attendance for one session.

        Two steps, in this order and for a reason:

            1. the ordinary rules decide, exactly as they did before the
               grace period existed;
            2. if and only if they withheld PRESENT, the duration-based
               relaxation is asked whether the absence was allowable.

        Running the relaxation SECOND is what makes it a relaxation. Asked
        first it would become a shortcut: a student who comfortably met the
        ratio would be recorded as having needed grace they did not need,
        and the explanation on their record would be the wrong one. Asked
        last it can only ever turn a withheld PRESENT into a granted one,
        which is the whole of what the rule is for.
        """
        return self._relax(self._decide(student_id, segments, session,
                                        evaluated_at))

    def _relax(self, decision):
        """
        Grant PRESENT when the absence was inside the class's grace period.

        Nothing is recomputed here and nothing is adjusted. present_seconds,
        the ratio, first_seen, last_seen, the segment count and the was_late
        and left_early flags all arrive decided and all leave untouched - the
        grace period moves the STATUS and the reason, and only ever in the
        one direction. A student the ordinary rules already called PRESENT is
        returned exactly as they came in.
        """
        if decision.status == STATUS_PRESENT:
            return decision

        # ABSENT and UNKNOWN are not shortfalls to be forgiven. ABSENT means
        # no verified presence at all, UNKNOWN means the session cannot answer
        # yet, and a grace period cannot manufacture attendance out of either.
        if decision.status in (STATUS_ABSENT, STATUS_UNKNOWN,
                               STATUS_MANUAL_OVERRIDE):
            return decision

        if not decision.within_grace:
            return decision

        decision.status = STATUS_PRESENT
        decision.reason = (
            "Absence of %.0fs is within the %.0fs grace period a %.0f-minute "
            "class allows; present for %.0fs (%.1f%% of the session)"
            % (decision.missing_seconds, decision.grace_seconds,
               decision.session_seconds / 60.0, decision.present_seconds,
               decision.presence_ratio * 100))
        return decision

    def _decide(self, student_id, segments, session, evaluated_at=None):
        """The attendance rules as they stand, with no grace period in them."""
        from attendance_session import SESSION_COMPLETED

        summary = summarize_presence(
            student_id, segments, session.start_time, session.end_time,
            excluded_intervals=getattr(session, "excluded_intervals", ()))

        session_seconds = session.duration_seconds
        ratio = (summary.present_seconds / session_seconds
                 if session_seconds > 0 else 0.0)
        ratio = min(ratio, 1.0)

        if not summary.has_presence:
            return self._no_presence(student_id, session, summary,
                                     session_seconds, evaluated_at,
                                     SESSION_COMPLETED)

        was_late = self._is_late(summary, session)
        left_early = self._left_early(summary, session)

        # Carried on every decision whether it is used or not, so a record
        # always states what the class allowed - a teacher reading a PARTIAL
        # can see how far outside the allowance the student fell.
        grace_seconds = self.policy.grace_seconds_for(session_seconds)

        meets_duration = summary.present_seconds >= self.policy.minimum_presence_seconds
        meets_ratio = ratio >= self.policy.minimum_presence_ratio

        if not (meets_duration and meets_ratio):
            return self._partial(student_id, session, summary, session_seconds,
                                 ratio, meets_duration, meets_ratio, was_late,
                                 left_early, evaluated_at, grace_seconds)

        if was_late:
            late_at = self._late_threshold(session)
            reason = ("First verified presence at %s occurred after the late "
                      "threshold of %s" % (_clock(summary.first_seen),
                                           _clock(late_at)))
            status = STATUS_LATE
        elif left_early:
            leave_at = self._early_leave_threshold(session)
            reason = ("Last verified presence at %s occurred before the "
                      "early-leave threshold of %s"
                      % (_clock(summary.last_seen), _clock(leave_at)))
            status = STATUS_LEFT_EARLY
        else:
            reason = ("Presence of %.0fs (%.1f%% of the session) met the "
                      "minimum duration and ratio thresholds"
                      % (summary.present_seconds, ratio * 100))
            status = STATUS_PRESENT

        return AttendanceDecision(
            student_id, session.session_id, status, reason,
            self.policy.version, summary, session_seconds, ratio,
            was_late, left_early, evaluated_at, grace_seconds)

    def _no_presence(self, student_id, session, summary, session_seconds,
                     evaluated_at, completed_state):
        """
        No verified presence at all.

        ABSENT is only correct for a session that has been explicitly
        completed. A session that is still running, was cancelled, or was cut
        short cannot support that conclusion.
        """
        if session.status == completed_state:
            status = STATUS_ABSENT
            reason = ("No verified presence during the completed session. "
                      "The student was expected but never identified.")
        else:
            status = STATUS_UNKNOWN
            reason = ("No verified presence yet. The session is %s, so "
                      "absence cannot be determined."
                      % session.status)

        return AttendanceDecision(
            student_id, session.session_id, status, reason,
            self.policy.version, summary, session_seconds, 0.0,
            False, False, evaluated_at,
            self.policy.grace_seconds_for(session_seconds))

    def _partial(self, student_id, session, summary, session_seconds, ratio,
                 meets_duration, meets_ratio, was_late, left_early,
                 evaluated_at, grace_seconds=0.0):
        parts = []
        if not meets_duration:
            parts.append("presence of %.0fs is below the required %.0fs"
                         % (summary.present_seconds,
                            self.policy.minimum_presence_seconds))
        if not meets_ratio:
            parts.append("presence ratio of %.3f is below the required %.3f"
                         % (ratio, self.policy.minimum_presence_ratio))

        status = STATUS_PARTIAL if self.policy.allow_partial else STATUS_ABSENT
        reason = "Verified presence, but " + " and ".join(parts)

        if status == STATUS_ABSENT:
            reason += " (policy does not allow partial attendance)"

        return AttendanceDecision(
            student_id, session.session_id, status, reason,
            self.policy.version, summary, session_seconds, ratio,
            was_late, left_early, evaluated_at, grace_seconds)

    def _late_threshold(self, session):
        if self.policy.late_after_seconds is None:
            return None
        return session.start_time + timedelta(
            seconds=self.policy.late_after_seconds)

    def _early_leave_threshold(self, session):
        if self.policy.early_leave_before_seconds is None:
            return None
        return session.end_time - timedelta(
            seconds=self.policy.early_leave_before_seconds)

    def _is_late(self, summary, session):
        threshold = self._late_threshold(session)
        if threshold is None or summary.first_seen is None:
            return False
        return summary.first_seen > threshold

    def _left_early(self, summary, session):
        """
        Did the student leave before the session was effectively over?

        Only meaningful for a session that actually finished. A camera being
        stopped early must never look like a student leaving early.
        """
        from attendance_session import SESSION_COMPLETED

        if session.status != SESSION_COMPLETED:
            return False

        threshold = self._early_leave_threshold(session)
        if threshold is None or summary.last_seen is None:
            return False

        return summary.last_seen < threshold

    def evaluate_session(self, session, segments, expected_students,
                         evaluated_at=None):
        """
        Decide attendance for every EXPECTED student in a session.

        Only expected students get a record. A person seen but not enrolled -
        or seen but never identified - produces no attendance record at all.
        """
        moment = evaluated_at or datetime.now(timezone.utc)

        return [self.evaluate_student(student_id, segments, session, moment)
                for student_id in sorted(set(expected_students))]


def _clock(moment):
    return moment.strftime("%H:%M:%S") if moment else "unknown"


# ---------------------------------------------------------------------------
# Bridge from the Step 5 presence timeline
# ---------------------------------------------------------------------------

def segments_from_events(events, grace_seconds=DEFAULT_TEMPORARY_LOSS_GRACE_SECONDS,
                         session_end=None):
    """
    Build presence segments from Step 5 presence events.

    The rules that matter here:

      - a segment starts only at IDENTITY_CONFIRMED. A track that is never
        identified produces NOTHING, so an unknown face can never become
        somebody's attendance;
      - TEMPORARILY_LOST does not end a segment. If the person is REACQUIRED
        within the grace period the segment continues unbroken, which is what
        makes "blinked" different from "left";
      - if the gap exceeds the grace period, the segment is closed at the
        moment the person was lost, and a new one opens when they return;
      - TRACK_EXITED closes the segment, at the moment presence was actually
        last observed rather than when the tracker gave up.

    Any segment left open at the end is closed at session_end, or at the last
    event seen.
    """
    ordered = sorted(events, key=lambda e: (e.timestamp, e.frame_index))

    open_segments = {}   # track_id -> {student_id, start, lost_at}
    finished = []
    last_time = None

    def close(track_id, moment):
        state = open_segments.pop(track_id, None)
        if state is None:
            return
        if moment > state["start"]:
            finished.append(PresenceSegment(
                state["student_id"], state["start"], moment, track_id))

    for event in ordered:
        last_time = event.timestamp
        track_id = event.track_id

        if event.event_type in (EVENT_IDENTITY_CONFIRMED, EVENT_IDENTITY_CHANGED):
            if not event.student_id:
                continue
            # An identity change closes the previous student's segment.
            close(track_id, event.timestamp)
            open_segments[track_id] = {
                "student_id": event.student_id,
                "start": event.timestamp,
                "lost_at": None,
            }

        elif event.event_type == EVENT_TRACK_TEMPORARILY_LOST:
            state = open_segments.get(track_id)
            if state is not None and state["lost_at"] is None:
                state["lost_at"] = event.timestamp

        elif event.event_type == EVENT_TRACK_REACQUIRED:
            state = open_segments.get(track_id)
            if state is None:
                continue

            lost_at = state["lost_at"]
            if lost_at is not None:
                gap = (event.timestamp - lost_at).total_seconds()
                if gap > grace_seconds:
                    # Away too long: close at the moment they vanished and
                    # start a fresh segment now.
                    student_id = state["student_id"]
                    close(track_id, lost_at)
                    open_segments[track_id] = {
                        "student_id": student_id,
                        "start": event.timestamp,
                        "lost_at": None,
                    }
                else:
                    state["lost_at"] = None

        elif event.event_type == EVENT_TRACK_EXITED:
            state = open_segments.get(track_id)
            if state is not None:
                # Credit presence up to when they were last actually seen.
                end = state["lost_at"] or event.timestamp
                close(track_id, end)

    closing_time = session_end or last_time
    if closing_time is not None:
        for track_id in list(open_segments):
            state = open_segments[track_id]
            end = state["lost_at"] or closing_time
            close(track_id, end)

    return finished


def segments_from_timeline(timeline, grace_seconds=DEFAULT_TEMPORARY_LOSS_GRACE_SECONDS,
                           session_end=None):
    """Convenience wrapper for a Step 5 PresenceTimeline object."""
    return segments_from_events(timeline.events, grace_seconds, session_end)
