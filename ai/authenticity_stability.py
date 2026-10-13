"""
ClassVision AI - Authenticity Stability (Step 10)
-------------------------------------------------
Decides when an authenticity verdict is worth believing.

    Step 10 is a research baseline. Nothing here has been validated against
    real manipulated video.

Why this is a separate layer
    ai/authenticity_detector.py answers "what does this window of
    observations look like?". That answer is allowed to wobble - somebody
    leans towards a lamp, the auto-exposure steps, the context ring fills with
    a passing person. Acting on each wobble would make a student's evidence
    flicker several times a second.

    So this file does for authenticity exactly what ai/track_identity.py does
    for identity and ai/liveness_stability.py does for liveness: it turns a
    stream of individual verdicts into a state that only changes when the
    evidence genuinely has.

The two rules that carry the weight

    1. SETTING a state needs agreement; CHANGING one needs more.
       Three consecutive agreeing verdicts confirm a state. Five are needed to
       overturn one. The asymmetry is the same one Step 5 applies to identity
       switches and Step 9 to liveness, for the same reason: an established
       conclusion should not fall over because of a couple of awkward
       observations.

    2. OSCILLATION IS NOT A STATE.
       AUTHENTIC, SUSPICIOUS, AUTHENTIC, SUSPICIOUS is not weak evidence of
       either; it is evidence that something is wrong. A track that flips
       repeatedly without ever confirming is reported UNCERTAIN, and the
       flapping is recorded so a teacher can see the track needs review.

What an UNCERTAIN verdict does NOT do
    It does not clear an already confirmed state. A student who briefly moved
    out of good light has not become synthetic. What UNCERTAIN does do is
    refuse to CREATE a state: a track that has never confirmed anything stays
    UNCERTAIN no matter how long it is watched.

    That is the right rule for a single awkward observation and the WRONG rule
    for a detector that has died - which is why the pipeline latches a
    detector failure separately and masks every view while it is set. Step 9
    needed exactly that correction after its audit, and Step 10 was built with
    it from the start rather than waiting to repeat the mistake.

Per-track, always
    Every track gets its own record. Two tracks never share a buffer, a
    candidate or a confirmation count, so one person being confirmed authentic
    can never vouch for the person beside them.

Privacy
    A record holds a track id, some state strings, counters and two
    timestamps. There is no image, no crop, no embedding, no identity and no
    cue value anywhere in this file.
"""

from authenticity_detector import (
    AUTHENTICITY_AUTHENTIC,
    AUTHENTICITY_SUSPICIOUS,
    AUTHENTICITY_UNCERTAIN,
    AUTHENTICITY_UNKNOWN,
    DEFAULT_SAMPLE_INTERVAL_FRAMES,
)

# Consecutive agreeing verdicts before a state is confirmed for the first time.
DEFAULT_CONFIRMATIONS_REQUIRED = 3

# ...and before an already confirmed state may be overturned. Higher, on
# purpose: see rule 1 above.
DEFAULT_CHANGE_CONFIRMATIONS = 5

# How many times the candidate may flip between AUTHENTIC and SUSPICIOUS,
# without ever confirming, before the track is called unstable.
DEFAULT_OSCILLATION_LIMIT = 4

# How many authenticity events are kept in memory. A live operational log for
# one session, not an audit trail, and bounded so a long session cannot grow
# it without limit.
DEFAULT_EVENT_LIMIT = 500

# Event types. Only state TRANSITIONS are emitted - never one per frame, and
# never one per observation.
EVENT_AUTHENTICITY_CHECK_STARTED = "AUTHENTICITY_CHECK_STARTED"
EVENT_AUTHENTICITY_CONFIRMED = "AUTHENTICITY_CONFIRMED"
EVENT_AUTHENTICITY_UNCERTAIN = "AUTHENTICITY_UNCERTAIN"
EVENT_AUTHENTICITY_SUSPICIOUS = "AUTHENTICITY_SUSPICIOUS"
EVENT_AUTHENTICITY_CLEARED = "AUTHENTICITY_CLEARED"

# Outcomes returned by update().
OUTCOME_PENDING = "pending"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_CHANGED = "changed"
OUTCOME_CHANGE_REJECTED = "change_rejected"
OUTCOME_UNSTABLE = "unstable"


class AuthenticityEvent:
    """One meaningful change in one track's authenticity state."""

    __slots__ = ("event_type", "track_id", "state", "reason", "frame_index",
                 "timestamp")

    def __init__(self, event_type, track_id, state, reason=None,
                 frame_index=-1, timestamp=None):
        self.event_type = event_type
        self.track_id = int(track_id)
        self.state = state
        self.reason = reason
        self.frame_index = int(frame_index)
        self.timestamp = timestamp

    def to_dict(self):
        return {
            "event_type": self.event_type,
            "track_id": self.track_id,
            "authenticity_state": self.state,
            "authenticity_reason": self.reason,
            "frame_index": self.frame_index,
            "timestamp_utc": (self.timestamp.isoformat(timespec="milliseconds")
                              if self.timestamp else None),
        }

    def __repr__(self):
        return "AuthenticityEvent(%s, track=%d, %s)" % (
            self.event_type, self.track_id, self.state)


class TrackAuthenticity:
    """What is currently believed about one track's authenticity."""

    __slots__ = ("track_id", "state", "confirmed", "candidate",
                 "candidate_count", "observations", "authentic_observations",
                 "suspicious_observations", "uncertain_observations",
                 "confirmations", "changes", "flips", "oscillating",
                 "reason", "last_checked_frame", "first_confirmed_at",
                 "last_confirmed_at", "started")

    def __init__(self, track_id):
        self.track_id = int(track_id)

        self.state = AUTHENTICITY_UNKNOWN
        self.confirmed = False

        self.candidate = None
        self.candidate_count = 0

        self.observations = 0
        self.authentic_observations = 0
        self.suspicious_observations = 0
        self.uncertain_observations = 0

        self.confirmations = 0
        self.changes = 0
        self.flips = 0
        self.oscillating = False

        self.reason = None
        self.last_checked_frame = None
        self.first_confirmed_at = None
        self.last_confirmed_at = None
        self.started = False

    @property
    def effective_state(self):
        """
        The state anything downstream should act on.

        A track that has confirmed nothing is UNCERTAIN once it has been
        looked at, and UNKNOWN before that. Neither is ever AUTHENTIC.
        """
        if self.confirmed:
            return self.state
        if self.observations:
            return AUTHENTICITY_UNCERTAIN
        return AUTHENTICITY_UNKNOWN

    @property
    def is_confirmed_authentic(self):
        return self.confirmed and self.state == AUTHENTICITY_AUTHENTIC

    @property
    def is_confirmed_suspicious(self):
        return self.confirmed and self.state == AUTHENTICITY_SUSPICIOUS

    def to_dict(self):
        """Safe to log and to serve. No identity, no image, no cue value."""
        return {
            "track_id": self.track_id,
            "authenticity_state": self.effective_state,
            "authenticity_confirmed": self.confirmed,
            "authenticity_observations": self.observations,
            "authenticity_unstable": self.oscillating,
            "authenticity_reason": self.reason,
        }

    def __repr__(self):
        return "TrackAuthenticity(track=%d, %s, confirmed=%s, obs=%d)" % (
            self.track_id, self.effective_state, self.confirmed,
            self.observations)


class AuthenticityManager:
    """
    Holds the authenticity state of every live track, and decides when to look.

    Also counts how often the detector actually ran, which is the evidence for
    the claim that Step 10 does not cost a frame's budget.
    """

    def __init__(self, confirmations_required=DEFAULT_CONFIRMATIONS_REQUIRED,
                 change_confirmations=DEFAULT_CHANGE_CONFIRMATIONS,
                 oscillation_limit=DEFAULT_OSCILLATION_LIMIT,
                 sample_interval=DEFAULT_SAMPLE_INTERVAL_FRAMES,
                 event_limit=DEFAULT_EVENT_LIMIT):
        self.confirmations_required = max(int(confirmations_required), 1)
        self.change_confirmations = max(int(change_confirmations), 1)
        self.oscillation_limit = max(int(oscillation_limit), 2)
        self.sample_interval = max(int(sample_interval), 1)
        self.event_limit = max(int(event_limit), 1)

        self.records = {}
        self.events = []
        self.checks = 0

        # When a track exits its RUNTIME state is released, but the verdict it
        # reached is kept here: two strings per track. The finaliser runs long
        # after every track has gone, and without this it would see no
        # authenticity state at all.
        self.retired = {}

    # -- scheduling --------------------------------------------------------

    def should_check(self, track, frame_index):
        """
        Is it worth measuring this track now?

        Only a visible track can be measured - there is no fresh image
        otherwise - and then only every sample_interval frames.
        """
        if not getattr(track, "is_visible", False):
            return False

        record = self.get_or_create(track.track_id)

        if record.last_checked_frame is None:
            return True

        return (frame_index - record.last_checked_frame) >= self.sample_interval

    # -- records -----------------------------------------------------------

    def get(self, track_id):
        return self.records.get(track_id)

    def get_or_create(self, track_id):
        record = self.records.get(track_id)
        if record is None:
            record = TrackAuthenticity(track_id)
            self.records[track_id] = record
        return record

    def state_for(self, track_id):
        record = self.records.get(track_id)
        return record.effective_state if record else AUTHENTICITY_UNKNOWN

    def confirmed_authentic_tracks(self):
        return sorted(track_id for track_id, record in self.records.items()
                      if record.is_confirmed_authentic)

    def confirmed_suspicious_tracks(self):
        return sorted(track_id for track_id, record in self.records.items()
                      if record.is_confirmed_suspicious)

    # -- the decision ------------------------------------------------------

    def update(self, track_id, verdict, frame_index, timestamp=None):
        """
        Apply one detector verdict to a track.

        Returns (record, outcome). Nothing here inspects an image or an
        identity; a verdict carries neither.
        """
        record = self.get_or_create(track_id)
        record.observations += 1
        record.last_checked_frame = int(frame_index)
        record.reason = getattr(verdict, "reason", None)
        self.checks += 1

        state = getattr(verdict, "state", AUTHENTICITY_UNCERTAIN)

        if not record.started:
            record.started = True
            self._emit(EVENT_AUTHENTICITY_CHECK_STARTED, record,
                       AUTHENTICITY_UNKNOWN, frame_index, timestamp,
                       "authenticity analysis started for this track")

        if state == AUTHENTICITY_AUTHENTIC:
            record.authentic_observations += 1
        elif state == AUTHENTICITY_SUSPICIOUS:
            record.suspicious_observations += 1
        else:
            record.uncertain_observations += 1
            return record, self._handle_uncertain(record)

        return record, self._handle_decided(record, state, frame_index,
                                            timestamp)

    def _handle_uncertain(self, record):
        """
        Not enough evidence this time.

        An uncertain observation breaks any run that was building up - it is
        not agreement - but it does not tear down a state that was already
        confirmed on sufficient evidence.
        """
        record.candidate = None
        record.candidate_count = 0

        if record.confirmed:
            return OUTCOME_UNCHANGED

        return OUTCOME_PENDING

    def _handle_decided(self, record, state, frame_index, timestamp):
        """A verdict of AUTHENTIC or SUSPICIOUS."""
        if record.candidate == state:
            record.candidate_count += 1
        else:
            if record.candidate is not None:
                record.flips += 1
                if record.flips >= self.oscillation_limit:
                    record.oscillating = True

            record.candidate = state
            record.candidate_count = 1

        if record.confirmed and record.state == state:
            record.confirmations += 1
            record.last_confirmed_at = timestamp or record.last_confirmed_at
            record.candidate = None
            record.candidate_count = 0
            return OUTCOME_UNCHANGED

        needed = (self.change_confirmations if record.confirmed
                  else self.confirmations_required)

        if record.candidate_count < needed:
            if record.oscillating:
                return OUTCOME_UNSTABLE
            return (OUTCOME_CHANGE_REJECTED if record.confirmed
                    else OUTCOME_PENDING)

        was_confirmed = record.confirmed
        previous = record.state

        record.state = state
        record.confirmed = True
        record.confirmations = record.candidate_count
        record.candidate = None
        record.candidate_count = 0
        record.flips = 0
        record.oscillating = False
        record.last_confirmed_at = timestamp
        if record.first_confirmed_at is None:
            record.first_confirmed_at = timestamp

        if state == AUTHENTICITY_SUSPICIOUS:
            self._emit(EVENT_AUTHENTICITY_SUSPICIOUS, record, state,
                       frame_index, timestamp, record.reason)
        elif was_confirmed and previous == AUTHENTICITY_SUSPICIOUS:
            self._emit(EVENT_AUTHENTICITY_CLEARED, record, state,
                       frame_index, timestamp, record.reason)
        else:
            self._emit(EVENT_AUTHENTICITY_CONFIRMED, record, state,
                       frame_index, timestamp, record.reason)

        if was_confirmed:
            record.changes += 1
            return OUTCOME_CHANGED

        return OUTCOME_CONFIRMED

    def note_unavailable(self, track_id, frame_index, reason, timestamp=None):
        """
        Record that authenticity could not be assessed for this track at all.

        Identical in effect to an uncertain observation, which is the point:
        there is no code path where "the check did not run" means anything
        other than "we do not know".
        """
        record = self.get_or_create(track_id)
        record.observations += 1
        record.uncertain_observations += 1
        record.last_checked_frame = int(frame_index)
        record.reason = reason
        record.candidate = None
        record.candidate_count = 0

        if not record.confirmed:
            self._emit(EVENT_AUTHENTICITY_UNCERTAIN, record,
                       AUTHENTICITY_UNCERTAIN, frame_index, timestamp, reason)

        return record, (OUTCOME_UNCHANGED if record.confirmed
                        else OUTCOME_PENDING)

    # -- events ------------------------------------------------------------

    def _emit(self, event_type, record, state, frame_index, timestamp, reason):
        event = AuthenticityEvent(event_type, record.track_id, state, reason,
                                  frame_index, timestamp)
        self.events.append(event)

        if len(self.events) > self.event_limit:
            del self.events[0]

        return event

    def recent_events(self, limit=20):
        return [event.to_dict() for event in self.events[-int(limit):]]

    # -- housekeeping ------------------------------------------------------

    def forget(self, track_id):
        """
        Release a track's runtime state when the tracker retires it.

        The verdict it reached is archived first - see self.retired.
        """
        record = self.records.pop(track_id, None)
        if record is not None:
            self.retired[track_id] = (record.effective_state, record.confirmed)
        return record

    def prune(self, live_track_ids):
        live = set(live_track_ids)
        for track_id in [t for t in self.records if t not in live]:
            self.forget(track_id)

    def all_states(self):
        """
        {track_id: (state, confirmed)} for every track this session has seen.

        Live tracks win over retired ones, which only matters if a track id
        were ever reused - it is not, but relying on that would be careless.
        """
        states = dict(self.retired)
        for track_id, record in self.records.items():
            states[track_id] = (record.effective_state, record.confirmed)
        return states

    def summary(self):
        """Counts only. Safe for the runtime status and the dashboard."""
        counts = {AUTHENTICITY_AUTHENTIC: 0, AUTHENTICITY_SUSPICIOUS: 0,
                  AUTHENTICITY_UNCERTAIN: 0, AUTHENTICITY_UNKNOWN: 0}

        for record in self.records.values():
            counts[record.effective_state] = \
                counts.get(record.effective_state, 0) + 1

        return {
            "tracks": len(self.records),
            "authentic": counts[AUTHENTICITY_AUTHENTIC],
            "suspicious": counts[AUTHENTICITY_SUSPICIOUS],
            "uncertain": counts[AUTHENTICITY_UNCERTAIN],
            "unknown": counts[AUTHENTICITY_UNKNOWN],
            "checks": self.checks,
        }

    def reset(self):
        self.records = {}
        self.retired = {}
        self.events = []
        self.checks = 0
