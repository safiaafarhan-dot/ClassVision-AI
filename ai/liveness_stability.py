"""
ClassVision AI - Liveness Stability (Step 9)
--------------------------------------------
Decides when a liveness verdict is worth believing.

    Step 9 is a research prototype. Nothing here has been validated against a
    real presentation attack.

Why this is a separate layer
    ai/liveness_detector.py answers "what does this window of observations
    look like?". That answer is allowed to wobble - somebody turns away, the
    light changes, a face gets small for a moment. Acting on each wobble would
    make a student's attendance eligibility flicker several times a second,
    which is worse than useless.

    So this file does for liveness exactly what ai/track_identity.py does for
    identity: it converts a stream of individual verdicts into a state that
    only changes when the evidence genuinely has.

The two rules that carry the weight

    1. SETTING a state needs agreement; CHANGING one needs more.
       Three consecutive agreeing verdicts confirm a state. Five are needed to
       overturn one. This is the same asymmetry Step 5 applies to identity
       switches (2 to set, 4 to switch), and it exists for the same reason:
       an established conclusion should not fall over because of a couple of
       awkward observations.

       It also matters as a defence. A confirmed SPOOF must not be clearable
       by holding up a photograph and then briefly showing a real face - that
       is precisely the sequence an attacker would try.

    2. OSCILLATION IS NOT A STATE.
       LIVE, SPOOF, LIVE, SPOOF is not weak evidence of either; it is evidence
       that something is wrong. A track that flips repeatedly without ever
       confirming is reported UNCERTAIN, and the flapping itself is recorded
       so a teacher can see the track needs review. It never causes attendance
       eligibility to flicker, because eligibility follows the CONFIRMED
       state, which by definition is not flipping.

What an UNCERTAIN verdict does NOT do
    It does not clear an already confirmed state. A student who was confirmed
    live and then turned away has not become a photograph. Keeping the
    confirmed state is not "converting insufficient evidence into LIVE" - the
    evidence that produced it was sufficient, and it was gathered before.

    What UNCERTAIN does do is refuse to CREATE a state. A track that has never
    confirmed anything stays UNCERTAIN no matter how long it is watched.

Per-track, always
    Every track gets its own record. Two tracks never share a buffer, never
    share a candidate, and never share a confirmation count - so one person
    being confirmed live can never make the person beside them eligible.
    A test asserts exactly this.

Privacy
    A record holds a track id, four state strings, some counters and two
    timestamps. There is no image, no crop, no embedding, no identity and no
    cue value anywhere in this file.
"""

from liveness_detector import (
    LIVENESS_LIVE,
    LIVENESS_SPOOF,
    LIVENESS_UNCERTAIN,
    LIVENESS_UNKNOWN,
    DEFAULT_SAMPLE_INTERVAL_FRAMES,
)

# Consecutive agreeing verdicts before a state is confirmed for the first time.
DEFAULT_CONFIRMATIONS_REQUIRED = 3

# ...and before an already confirmed state may be overturned. Higher, on
# purpose: see rule 1 above.
DEFAULT_CHANGE_CONFIRMATIONS = 5

# How many times the candidate may flip between LIVE and SPOOF, without ever
# confirming, before the track is called unstable.
DEFAULT_OSCILLATION_LIMIT = 4

# How many liveness events are kept in memory. This is a live operational log
# for one session, not an audit trail, and it is bounded so a long session
# cannot grow it without limit.
DEFAULT_EVENT_LIMIT = 500

# Event types. Only state TRANSITIONS are emitted - never one per frame, and
# never one per observation.
EVENT_LIVENESS_CHECK_STARTED = "LIVENESS_CHECK_STARTED"
EVENT_LIVENESS_CONFIRMED = "LIVENESS_CONFIRMED"
EVENT_LIVENESS_UNCERTAIN = "LIVENESS_UNCERTAIN"
EVENT_LIVENESS_SPOOF_DETECTED = "LIVENESS_SPOOF_DETECTED"
EVENT_LIVENESS_REACQUIRED = "LIVENESS_REACQUIRED"

# Outcomes returned by update().
OUTCOME_PENDING = "pending"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_CHANGED = "changed"
OUTCOME_CHANGE_REJECTED = "change_rejected"
OUTCOME_UNSTABLE = "unstable"


class LivenessEvent:
    """One meaningful change in one track's liveness state."""

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
            "liveness_state": self.state,
            "liveness_reason": self.reason,
            "frame_index": self.frame_index,
            "timestamp_utc": (self.timestamp.isoformat(timespec="milliseconds")
                              if self.timestamp else None),
        }

    def __repr__(self):
        return "LivenessEvent(%s, track=%d, %s)" % (
            self.event_type, self.track_id, self.state)


class TrackLiveness:
    """What is currently believed about one track's liveness."""

    __slots__ = ("track_id", "state", "confirmed", "candidate",
                 "candidate_count", "observations", "live_observations",
                 "spoof_observations", "uncertain_observations",
                 "confirmations", "changes", "flips", "oscillating",
                 "reason", "last_checked_frame", "first_confirmed_at",
                 "last_confirmed_at", "started")

    def __init__(self, track_id):
        self.track_id = int(track_id)

        # The state that has actually been confirmed. UNKNOWN until one has.
        self.state = LIVENESS_UNKNOWN
        self.confirmed = False

        self.candidate = None
        self.candidate_count = 0

        self.observations = 0
        self.live_observations = 0
        self.spoof_observations = 0
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
        looked at, and UNKNOWN before that. Neither is ever LIVE.
        """
        if self.confirmed:
            return self.state
        if self.observations:
            return LIVENESS_UNCERTAIN
        return LIVENESS_UNKNOWN

    @property
    def is_confirmed_live(self):
        return self.confirmed and self.state == LIVENESS_LIVE

    @property
    def is_confirmed_spoof(self):
        return self.confirmed and self.state == LIVENESS_SPOOF

    def to_dict(self):
        """Safe to log and to serve. No identity, no image, no cue value."""
        return {
            "track_id": self.track_id,
            "liveness_state": self.effective_state,
            "liveness_confirmed": self.confirmed,
            "liveness_observations": self.observations,
            "liveness_unstable": self.oscillating,
            "liveness_reason": self.reason,
        }

    def __repr__(self):
        return "TrackLiveness(track=%d, %s, confirmed=%s, obs=%d)" % (
            self.track_id, self.effective_state, self.confirmed,
            self.observations)


class LivenessManager:
    """
    Holds the liveness state of every live track, and decides when to look.

    Also counts how often the detector actually ran, which is the evidence for
    the claim that liveness does not cost a frame's budget.
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

        # When a track exits, its RUNTIME state is released but its final
        # verdict is kept here: two strings per track, nothing else. The
        # finaliser runs long after every track has gone, and without this it
        # would see no liveness state at all and hold back evidence it had in
        # fact confirmed. Releasing the analysis buffers while keeping the
        # conclusion is the distinction that matters.
        self.retired = {}

    # -- scheduling --------------------------------------------------------

    def should_check(self, track, frame_index):
        """
        Is it worth measuring this track now?

        Only a visible track can be measured - there is no fresh face to look
        at otherwise - and then only every sample_interval frames. The spacing
        is not purely a saving: consecutive frames 66 ms apart barely differ,
        and the motion cue needs the gap to mean anything.
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
            record = TrackLiveness(track_id)
            self.records[track_id] = record
        return record

    def state_for(self, track_id):
        record = self.records.get(track_id)
        return record.effective_state if record else LIVENESS_UNKNOWN

    def confirmed_live_tracks(self):
        return sorted(track_id for track_id, record in self.records.items()
                      if record.is_confirmed_live)

    def confirmed_spoof_tracks(self):
        return sorted(track_id for track_id, record in self.records.items()
                      if record.is_confirmed_spoof)

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

        state = getattr(verdict, "state", LIVENESS_UNCERTAIN)

        if not record.started:
            record.started = True
            self._emit(EVENT_LIVENESS_CHECK_STARTED, record,
                       LIVENESS_UNKNOWN, frame_index, timestamp,
                       "liveness analysis started for this track")

        if state == LIVENESS_LIVE:
            record.live_observations += 1
        elif state == LIVENESS_SPOOF:
            record.spoof_observations += 1
        else:
            record.uncertain_observations += 1
            return record, self._handle_uncertain(record, frame_index,
                                                  timestamp)

        return record, self._handle_decided(record, state, frame_index,
                                            timestamp)

    def _handle_uncertain(self, record, frame_index, timestamp):
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
        """A verdict of LIVE or SPOOF."""
        if record.candidate == state:
            record.candidate_count += 1
        else:
            if record.candidate is not None:
                # The candidate changed its mind. Repeated flipping is the
                # signature of a track that cannot be trusted either way.
                record.flips += 1
                if record.flips >= self.oscillation_limit:
                    record.oscillating = True

            record.candidate = state
            record.candidate_count = 1

        # Already believe this? Then this is just more of the same.
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

        # Enough agreement. Confirm, or overturn.
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

        if state == LIVENESS_SPOOF:
            self._emit(EVENT_LIVENESS_SPOOF_DETECTED, record, state,
                       frame_index, timestamp, record.reason)
        elif was_confirmed and previous == LIVENESS_SPOOF:
            self._emit(EVENT_LIVENESS_REACQUIRED, record, state,
                       frame_index, timestamp, record.reason)
        else:
            self._emit(EVENT_LIVENESS_CONFIRMED, record, state,
                       frame_index, timestamp, record.reason)

        if was_confirmed:
            record.changes += 1
            return OUTCOME_CHANGED

        return OUTCOME_CONFIRMED

    def note_unavailable(self, track_id, frame_index, reason, timestamp=None):
        """
        Record that liveness could not be assessed for this track at all.

        Used when the whole liveness layer failed rather than one measurement.
        It is the same as an uncertain observation - which is the point. There
        is no code path in this project where "the check did not run" is
        allowed to mean anything other than UNCERTAIN.
        """
        record = self.get_or_create(track_id)
        record.observations += 1
        record.uncertain_observations += 1
        record.last_checked_frame = int(frame_index)
        record.reason = reason
        record.candidate = None
        record.candidate_count = 0

        if not record.confirmed:
            self._emit(EVENT_LIVENESS_UNCERTAIN, record, LIVENESS_UNCERTAIN,
                       frame_index, timestamp, reason)

        return record, OUTCOME_UNCHANGED if record.confirmed else OUTCOME_PENDING

    # -- events ------------------------------------------------------------

    def _emit(self, event_type, record, state, frame_index, timestamp, reason):
        event = LivenessEvent(event_type, record.track_id, state, reason,
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
        counts = {LIVENESS_LIVE: 0, LIVENESS_SPOOF: 0,
                  LIVENESS_UNCERTAIN: 0, LIVENESS_UNKNOWN: 0}

        for record in self.records.values():
            counts[record.effective_state] = \
                counts.get(record.effective_state, 0) + 1

        return {
            "tracks": len(self.records),
            "live": counts[LIVENESS_LIVE],
            "spoof": counts[LIVENESS_SPOOF],
            "uncertain": counts[LIVENESS_UNCERTAIN],
            "unknown": counts[LIVENESS_UNKNOWN],
            "checks": self.checks,
        }

    def reset(self):
        self.records = {}
        self.retired = {}
        self.events = []
        self.checks = 0
