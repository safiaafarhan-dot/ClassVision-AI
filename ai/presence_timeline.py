"""
ClassVision AI - Presence Timeline (Step 5)
-------------------------------------------
Turns tracks into a record of who was visible, and when.

    Step 5 does not calculate attendance.

    This module produces a diagnostic timeline of presence EVENTS. Attendance
    will be derived later, in a separate step, by applying configurable rules
    to this timeline - for example "present = visible for at least 60% of the
    session". Keeping the record and the rule apart is what lets a school
    change its policy without re-running the camera.

Why events and not a boolean
    "Was this student seen?" is a checkbox and throws away everything
    interesting. "This student was visible from 12:01:04 to 12:01:12, briefly
    lost, seen again until 12:01:20" can answer questions a checkbox cannot,
    and can be reviewed and corrected by a teacher.

States
    NEW                a track has appeared, nobody knows who it is yet
    UNKNOWN            seen, but not matched to an enrolled student
    RECOGNIZED         an identity has just been confirmed
    PRESENT            confirmed and still visible
    TEMPORARILY_LOST   not visible right now, inside the grace period
    REACQUIRED         seen again after being lost
    EXITED             gone for longer than the grace period

Time convention
    Every event carries BOTH a UTC timestamp and a frame index.

    UTC is used throughout - never local time - so a timeline is unambiguous
    across machines and survives daylight-saving changes. The frame index is
    kept alongside it because it is exact, while wall-clock time can jump if
    the system clock is adjusted mid-session.
"""

from datetime import datetime, timezone

# States
STATE_NEW = "NEW"
STATE_UNKNOWN = "UNKNOWN"
STATE_RECOGNIZED = "RECOGNIZED"
STATE_PRESENT = "PRESENT"
STATE_TEMPORARILY_LOST = "TEMPORARILY_LOST"
STATE_REACQUIRED = "REACQUIRED"
STATE_EXITED = "EXITED"

# Events
EVENT_TRACK_STARTED = "TRACK_STARTED"
EVENT_IDENTITY_CONFIRMED = "IDENTITY_CONFIRMED"
EVENT_PRESENCE_CONTINUED = "PRESENCE_CONTINUED"
EVENT_TRACK_TEMPORARILY_LOST = "TRACK_TEMPORARILY_LOST"
EVENT_TRACK_REACQUIRED = "TRACK_REACQUIRED"
EVENT_TRACK_EXITED = "TRACK_EXITED"
EVENT_IDENTITY_CHANGED_REJECTED = "IDENTITY_CHANGED_REJECTED"
EVENT_IDENTITY_CHANGED = "IDENTITY_CHANGED"
EVENT_UNKNOWN_PERSON = "UNKNOWN_PERSON"

# How often to emit a "still here" heartbeat, in frames. Without this the
# timeline would either flood with one event per frame or say nothing at all
# between arriving and leaving.
DEFAULT_HEARTBEAT_FRAMES = 60

# How many recognition attempts a track gets before it is announced as an
# unknown person.
#
# Identity needs two agreeing recognitions to confirm, so a genuine student is
# still "not confirmed" after their first one. Announcing UNKNOWN_PERSON then
# would label every student as a stranger for one frame, immediately before
# naming them. Waiting for the second attempt removes that false event.
DEFAULT_UNKNOWN_AFTER_ATTEMPTS = 2


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(moment):
    return moment.isoformat(timespec="milliseconds")


class PresenceEvent:
    """One thing that happened to one track."""

    __slots__ = ("event_type", "track_id", "student_id", "name", "similarity",
                 "state", "timestamp", "timestamp_iso", "frame_index", "detail")

    def __init__(self, event_type, track_id, frame_index, timestamp=None,
                 student_id=None, name=None, similarity=0.0, state=None,
                 detail=None):
        moment = timestamp or utc_now()

        self.event_type = event_type
        self.track_id = int(track_id)
        self.student_id = student_id
        self.name = name
        self.similarity = round(float(similarity), 4)
        self.state = state
        self.frame_index = int(frame_index)
        self.timestamp = moment
        self.timestamp_iso = to_iso(moment)
        self.detail = detail

    def to_dict(self):
        """Structured form. Carries no biometric vector."""
        return {
            "event_type": self.event_type,
            "track_id": self.track_id,
            "student_id": self.student_id,
            "name": self.name,
            "similarity": self.similarity,
            "state": self.state,
            "frame_index": self.frame_index,
            "timestamp_utc": self.timestamp_iso,
            "detail": self.detail,
        }

    def format_line(self):
        """One readable console line."""
        clock = self.timestamp.strftime("%H:%M:%S.") + \
            "%03d" % (self.timestamp.microsecond // 1000)

        parts = ["[%s]" % clock, "%-26s" % self.event_type,
                 "track=%-3d" % self.track_id]

        if self.student_id:
            parts.append("student=%s" % self.student_id)
        if self.similarity:
            parts.append("similarity=%.2f" % self.similarity)
        if self.detail:
            parts.append("(%s)" % self.detail)

        return " ".join(parts)

    def __repr__(self):
        return "PresenceEvent(%s, track=%d, student=%r, frame=%d)" % (
            self.event_type, self.track_id, self.student_id, self.frame_index)


class PresenceRecord:
    """The running presence state of one track."""

    __slots__ = ("track_id", "state", "student_id", "name", "similarity",
                 "first_frame", "last_visible_frame", "first_seen",
                 "last_seen", "exited_at", "exit_frame",
                 "last_heartbeat_frame", "identity_announced",
                 "unknown_announced", "visible_frames", "lost_episodes",
                 "exited")

    def __init__(self, track_id, frame_index, timestamp):
        self.track_id = int(track_id)
        self.state = STATE_NEW
        self.student_id = None
        self.name = None
        self.similarity = 0.0

        self.first_frame = int(frame_index)
        self.last_visible_frame = int(frame_index)
        self.first_seen = timestamp
        self.last_seen = timestamp
        self.exited_at = None
        self.exit_frame = None

        self.last_heartbeat_frame = int(frame_index)
        self.identity_announced = False
        self.unknown_announced = False

        self.visible_frames = 0
        self.lost_episodes = 0
        self.exited = False

    @property
    def duration_frames(self):
        end = self.exit_frame if self.exit_frame is not None else self.last_visible_frame
        return end - self.first_frame

    @property
    def duration_seconds(self):
        end = self.exited_at or self.last_seen
        return (end - self.first_seen).total_seconds()

    def to_dict(self):
        return {
            "track_id": self.track_id,
            "student_id": self.student_id,
            "name": self.name,
            "state": self.state,
            "first_seen_utc": to_iso(self.first_seen),
            "last_seen_utc": to_iso(self.last_seen),
            "exited_utc": to_iso(self.exited_at) if self.exited_at else None,
            "first_frame": self.first_frame,
            "last_visible_frame": self.last_visible_frame,
            "visible_frames": self.visible_frames,
            "lost_episodes": self.lost_episodes,
            "duration_seconds": round(self.duration_seconds, 3),
        }


class PresenceTimeline:
    """
    Builds the presence timeline from tracks and their identities.

    Call update() once per frame, after the tracker and identity manager have
    run. It returns only the events that are new in that frame.
    """

    def __init__(self, heartbeat_frames=DEFAULT_HEARTBEAT_FRAMES,
                 keep_history=True,
                 unknown_after_attempts=DEFAULT_UNKNOWN_AFTER_ATTEMPTS):
        self.heartbeat_frames = int(heartbeat_frames)
        self.keep_history = bool(keep_history)
        self.unknown_after_attempts = int(unknown_after_attempts)

        self.records = {}
        self.events = []

    @property
    def active_records(self):
        return [r for r in self.records.values() if not r.exited]

    def _emit(self, events, event):
        events.append(event)
        if self.keep_history:
            self.events.append(event)
        return event

    def update(self, tracks, identity_manager, frame_index,
               removed_tracks=None, timestamp=None, outcomes=None):
        """
        Advance the timeline by one frame.

        tracks           - every live track from the tracker (visible or lost)
        identity_manager - the IdentityManager holding identity state
        frame_index      - current frame number
        removed_tracks   - tracks the tracker retired this frame
        outcomes         - {track_id: outcome} from IdentityManager.update(),
                           used to report rejected identity switches

        Returns the events generated by this frame only.
        """
        moment = timestamp or utc_now()
        outcomes = outcomes or {}
        new_events = []

        for track in tracks:
            record = self.records.get(track.track_id)

            if record is None:
                record = PresenceRecord(track.track_id, frame_index, moment)
                self.records[track.track_id] = record
                self._emit(new_events, PresenceEvent(
                    EVENT_TRACK_STARTED, track.track_id, frame_index, moment,
                    state=STATE_NEW))

            identity = identity_manager.get(track.track_id)
            outcome = outcomes.get(track.track_id)

            if outcome == "switch_rejected" and identity is not None:
                self._emit(new_events, PresenceEvent(
                    EVENT_IDENTITY_CHANGED_REJECTED, track.track_id,
                    frame_index, moment,
                    student_id=record.student_id, name=record.name,
                    similarity=identity.pending_similarity,
                    state=record.state,
                    detail="proposed %s, kept %s"
                           % (identity.pending_student_id, record.student_id)))

            elif outcome == "switched" and identity is not None:
                self._emit(new_events, PresenceEvent(
                    EVENT_IDENTITY_CHANGED, track.track_id, frame_index, moment,
                    student_id=identity.student_id, name=identity.name,
                    similarity=identity.similarity, state=record.state,
                    detail="was %s" % record.student_id))
                record.student_id = identity.student_id
                record.name = identity.name

            # An identity becoming confirmed for the first time.
            if identity is not None and identity.is_confirmed \
                    and not record.identity_announced:
                record.student_id = identity.student_id
                record.name = identity.name
                record.similarity = identity.similarity
                record.identity_announced = True
                record.state = STATE_RECOGNIZED

                self._emit(new_events, PresenceEvent(
                    EVENT_IDENTITY_CONFIRMED, track.track_id, frame_index,
                    moment, student_id=identity.student_id, name=identity.name,
                    similarity=identity.similarity, state=STATE_RECOGNIZED))

            elif identity is not None and identity.is_confirmed:
                record.similarity = identity.similarity

            # A track that has been visible a while and is still nobody.
            # Waiting for unknown_after_attempts avoids announcing someone as a
            # stranger one frame before confirming them as a student.
            if identity is not None and not identity.is_confirmed \
                    and not record.unknown_announced \
                    and identity.recognition_count >= self.unknown_after_attempts \
                    and track.is_visible:
                record.unknown_announced = True
                record.state = STATE_UNKNOWN
                self._emit(new_events, PresenceEvent(
                    EVENT_UNKNOWN_PERSON, track.track_id, frame_index, moment,
                    similarity=identity.similarity, state=STATE_UNKNOWN))

            if track.is_visible:
                record.visible_frames += 1
                record.last_visible_frame = frame_index
                record.last_seen = moment

                was_lost = record.state == STATE_TEMPORARILY_LOST
                if was_lost:
                    record.state = STATE_REACQUIRED
                    self._emit(new_events, PresenceEvent(
                        EVENT_TRACK_REACQUIRED, track.track_id, frame_index,
                        moment, student_id=record.student_id, name=record.name,
                        similarity=record.similarity, state=STATE_REACQUIRED))

                # Settle into PRESENT / UNKNOWN once anything transient passed.
                if record.state in (STATE_RECOGNIZED, STATE_REACQUIRED, STATE_NEW):
                    if record.identity_announced:
                        record.state = STATE_PRESENT
                    elif record.unknown_announced:
                        record.state = STATE_UNKNOWN

                elapsed = frame_index - record.last_heartbeat_frame
                if elapsed >= self.heartbeat_frames:
                    record.last_heartbeat_frame = frame_index
                    self._emit(new_events, PresenceEvent(
                        EVENT_PRESENCE_CONTINUED, track.track_id, frame_index,
                        moment, student_id=record.student_id, name=record.name,
                        similarity=record.similarity, state=record.state))

            else:
                # Not visible, but the tracker still holds it: inside the
                # grace period. This is NOT an exit.
                if record.state != STATE_TEMPORARILY_LOST:
                    record.state = STATE_TEMPORARILY_LOST
                    record.lost_episodes += 1
                    self._emit(new_events, PresenceEvent(
                        EVENT_TRACK_TEMPORARILY_LOST, track.track_id,
                        frame_index, moment, student_id=record.student_id,
                        name=record.name, similarity=record.similarity,
                        state=STATE_TEMPORARILY_LOST))

        # Tracks the tracker gave up on: now, and only now, an exit.
        for track in (removed_tracks or []):
            record = self.records.get(track.track_id)
            if record is None or record.exited:
                continue

            record.exited = True
            record.state = STATE_EXITED
            record.exited_at = moment
            record.exit_frame = frame_index

            self._emit(new_events, PresenceEvent(
                EVENT_TRACK_EXITED, track.track_id, frame_index, moment,
                student_id=record.student_id, name=record.name,
                similarity=record.similarity, state=STATE_EXITED,
                detail="visible %d frames" % record.visible_frames))

        return new_events

    def close_all(self, frame_index, timestamp=None):
        """
        End every open presence, for when a session stops.

        Without this, anyone still on camera when the program closes would
        have no exit event at all.
        """
        moment = timestamp or utc_now()
        closing = []

        for record in self.records.values():
            if record.exited:
                continue

            record.exited = True
            record.state = STATE_EXITED
            record.exited_at = moment
            record.exit_frame = frame_index

            self._emit(closing, PresenceEvent(
                EVENT_TRACK_EXITED, record.track_id, frame_index, moment,
                student_id=record.student_id, name=record.name,
                similarity=record.similarity, state=STATE_EXITED,
                detail="session ended"))

        return closing

    def summary(self):
        """
        A per-track summary of the session.

        This is presence information, NOT attendance. It says how long each
        track was visible; it does not say whether anyone counts as present.
        """
        return [record.to_dict() for record in
                sorted(self.records.values(), key=lambda r: r.first_frame)]

    def events_for_track(self, track_id):
        return [e for e in self.events if e.track_id == track_id]

    def events_for_student(self, student_id):
        return [e for e in self.events if e.student_id == student_id]

    def reset(self):
        self.records = {}
        self.events = []
