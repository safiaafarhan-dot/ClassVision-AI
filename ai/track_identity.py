"""
ClassVision AI - Track Identity Association (Step 5)
----------------------------------------------------
Decides WHO a track belongs to, and when it is worth asking again.

A track and a student are two different things
    A track is "this box, followed across frames". A student is a person.
    They are kept apart on purpose:

      - the same student gets a NEW track id every time they leave and come
        back, so track ids must never be treated as identities;
      - a track must never quietly turn into a different student, because
        everything downstream would inherit that mistake.

Two jobs live here
    1. Deciding when to spend an embedding (recognition scheduling).
    2. Deciding whether to believe a recognition result (switch protection).

Recognition scheduling
    Embedding a face costs about 4.16 ms, measured in Step 4. Doing it for
    every face on every frame is what limits a full classroom to roughly
    7 FPS. A tracked face does not need re-checking every frame - it is the
    same person as it was 60 ms ago.

    Recognition is therefore requested only when it can change the answer:
      - the track has no identity yet
      - the identity is not yet confirmed
      - the track was just reacquired after being lost
      - the refresh interval has elapsed
      - a possible identity change is being investigated

Safety-first identity policy
    If recognition is not confident, the identity stays UNKNOWN. The system
    never falls back to "closest guess".
"""

from face_recognizer import STATUS_MATCH, STATUS_NO_ENROLLMENTS

# Consecutive agreeing recognitions before a NEW identity is trusted.
DEFAULT_CONFIRMATIONS_REQUIRED = 2

# Frames between routine re-checks of an already confirmed identity. At the
# measured ~15 FPS this is roughly every two seconds.
DEFAULT_RECOGNITION_INTERVAL = 30

# Switching an established identity is far more serious than setting one, so
# it demands more evidence.
DEFAULT_SWITCH_CONFIRMATIONS = 4

# ...and the newcomer must beat the incumbent's last score by this margin,
# so noise around the threshold can never cause a switch.
DEFAULT_SWITCH_MARGIN = 0.08

IDENTITY_PENDING = "PENDING"
IDENTITY_CONFIRMED = "CONFIRMED"
IDENTITY_UNKNOWN = "UNKNOWN"
IDENTITY_NO_ENROLLMENTS = "NO_ENROLLMENTS"


class TrackIdentity:
    """What is currently believed about one track's identity."""

    __slots__ = ("track_id", "student_id", "name", "similarity", "status",
                 "confirmations", "last_recognized_frame", "recognition_count",
                 "pending_student_id", "pending_count", "pending_similarity",
                 "rejected_switches", "investigating_change")

    def __init__(self, track_id):
        self.track_id = int(track_id)
        self.student_id = None
        self.name = None
        self.similarity = 0.0
        self.status = IDENTITY_UNKNOWN

        self.confirmations = 0
        self.last_recognized_frame = None
        self.recognition_count = 0

        # A different student seen recently, not yet believed.
        self.pending_student_id = None
        self.pending_count = 0
        self.pending_similarity = 0.0

        self.rejected_switches = 0
        self.investigating_change = False

    @property
    def is_confirmed(self):
        return self.status == IDENTITY_CONFIRMED and self.student_id is not None

    @property
    def label(self):
        if self.is_confirmed:
            return self.name or self.student_id
        if self.status == IDENTITY_NO_ENROLLMENTS:
            return "No enrollments"
        return "UNKNOWN"

    def to_dict(self):
        """Safe to log - carries no biometric vector."""
        return {
            "track_id": self.track_id,
            "student_id": self.student_id,
            "name": self.name,
            "similarity": round(self.similarity, 4),
            "status": self.status,
            "confirmations": self.confirmations,
            "recognition_count": self.recognition_count,
            "rejected_switches": self.rejected_switches,
        }

    def __repr__(self):
        return "TrackIdentity(track=%d, student=%r, status=%s, sim=%.3f)" % (
            self.track_id, self.student_id, self.status, self.similarity)


class IdentityManager:
    """
    Holds the identity state of every live track.

    Also counts how often recognition actually ran, which is the evidence for
    the claim that tracking reduces embedding work.
    """

    def __init__(self, confirmations_required=DEFAULT_CONFIRMATIONS_REQUIRED,
                 recognition_interval=DEFAULT_RECOGNITION_INTERVAL,
                 switch_confirmations=DEFAULT_SWITCH_CONFIRMATIONS,
                 switch_margin=DEFAULT_SWITCH_MARGIN):
        self.confirmations_required = int(confirmations_required)
        self.recognition_interval = int(recognition_interval)
        self.switch_confirmations = int(switch_confirmations)
        self.switch_margin = float(switch_margin)

        self.identities = {}

        # Accounting, for the performance report.
        self.recognition_calls = 0
        self.frames_processed = 0
        self.faces_seen = 0

    def get(self, track_id):
        return self.identities.get(track_id)

    def get_or_create(self, track_id):
        identity = self.identities.get(track_id)
        if identity is None:
            identity = TrackIdentity(track_id)
            self.identities[track_id] = identity
        return identity

    def forget(self, track_id):
        self.identities.pop(track_id, None)

    def should_recognize(self, track, frame_index):
        """
        Is it worth running recognition on this track now?

        This is the whole performance argument: say no to a settled track and
        the embedding is never computed.
        """
        identity = self.get_or_create(track.track_id)

        # Never recognise a face we cannot currently see - there is no fresh
        # image to embed.
        if not track.is_visible:
            return False

        if identity.last_recognized_frame is None:
            return True

        if not identity.is_confirmed:
            return True

        if track.reacquired:
            return True

        if identity.investigating_change:
            return True

        elapsed = frame_index - identity.last_recognized_frame
        return elapsed >= self.recognition_interval

    def note_frame(self, visible_faces):
        """Record one processed frame, for the efficiency numbers."""
        self.frames_processed += 1
        self.faces_seen += int(visible_faces)

    def update(self, track, result, frame_index):
        """
        Apply one recognition result to a track.

        Returns (identity, outcome) where outcome is one of:
            "confirmed"        the identity became confirmed
            "pending"          more evidence needed
            "unchanged"        already confirmed, result agreed
            "switch_rejected"  a different student was proposed and refused
            "switched"         a different student was accepted after evidence
            "unknown"          not confident enough to name anyone
            "no_enrollments"   nobody is enrolled
        """
        identity = self.get_or_create(track.track_id)
        identity.last_recognized_frame = int(frame_index)
        identity.recognition_count += 1
        self.recognition_calls += 1

        if result is None:
            return identity, "unknown"

        status = getattr(result, "status", None)

        if status == STATUS_NO_ENROLLMENTS:
            identity.status = IDENTITY_NO_ENROLLMENTS
            identity.similarity = 0.0
            return identity, "no_enrollments"

        if status != STATUS_MATCH or result.identity is None:
            # _handle_no_match already returns the (identity, outcome) pair.
            return self._handle_no_match(identity, result)

        return self._handle_match(identity, result)

    def _handle_no_match(self, identity, result):
        """Recognition did not clear the threshold."""
        similarity = float(getattr(result, "similarity", 0.0) or 0.0)

        # A non-match is evidence against a proposed change, so stop
        # investigating one.
        identity.pending_student_id = None
        identity.pending_count = 0
        identity.investigating_change = False

        if identity.is_confirmed:
            # An established identity is NOT dropped because of one weak look.
            # The person may simply have turned away. The track ending is what
            # closes a presence, not a single poor frame.
            return identity, "unchanged"

        identity.status = IDENTITY_UNKNOWN
        identity.student_id = None
        identity.name = None
        identity.similarity = similarity
        identity.confirmations = 0

        return identity, "unknown"

    def _handle_match(self, identity, result):
        """Recognition proposes a student."""
        student_id = result.identity
        similarity = float(result.similarity)

        # Case 1: the same student we already believe in.
        if identity.is_confirmed and identity.student_id == student_id:
            identity.similarity = similarity
            identity.confirmations += 1
            identity.pending_student_id = None
            identity.pending_count = 0
            identity.investigating_change = False
            return identity, "unchanged"

        # Case 2: a DIFFERENT student than the one already confirmed.
        if identity.is_confirmed and identity.student_id != student_id:
            return self._consider_switch(identity, student_id, result.name,
                                         similarity)

        # Case 3: no identity yet - build up evidence.
        if identity.pending_student_id == student_id:
            identity.pending_count += 1
        else:
            identity.pending_student_id = student_id
            identity.pending_count = 1

        identity.pending_similarity = similarity
        identity.similarity = similarity

        if identity.pending_count >= self.confirmations_required:
            identity.student_id = student_id
            identity.name = result.name
            identity.status = IDENTITY_CONFIRMED
            identity.confirmations = identity.pending_count
            identity.pending_student_id = None
            identity.pending_count = 0
            return identity, "confirmed"

        identity.status = IDENTITY_UNKNOWN
        return identity, "pending"

    def _consider_switch(self, identity, student_id, name, similarity):
        """
        A confirmed track is being claimed by a different student.

        Usually this means two people crossed and the boxes were swapped, or a
        borderline face scored just over the line for the wrong person. It is
        refused unless the evidence is both repeated AND clearly stronger than
        the current identity's own score.
        """
        identity.investigating_change = True

        if identity.pending_student_id == student_id:
            identity.pending_count += 1
        else:
            identity.pending_student_id = student_id
            identity.pending_count = 1

        identity.pending_similarity = similarity

        strong_enough = similarity >= identity.similarity + self.switch_margin
        repeated_enough = identity.pending_count >= self.switch_confirmations

        if strong_enough and repeated_enough:
            identity.student_id = student_id
            identity.name = name
            identity.similarity = similarity
            identity.status = IDENTITY_CONFIRMED
            identity.confirmations = identity.pending_count
            identity.pending_student_id = None
            identity.pending_count = 0
            identity.investigating_change = False
            return identity, "switched"

        identity.rejected_switches += 1
        return identity, "switch_rejected"

    def efficiency(self):
        """
        Numbers behind the "tracking saves work" claim.

        naive_calls is what a recognise-every-face-every-frame pipeline would
        have done, which is exactly what Step 4 did.
        """
        naive = self.faces_seen
        actual = self.recognition_calls

        return {
            "frames_processed": self.frames_processed,
            "face_frames_seen": naive,
            "recognition_calls": actual,
            "naive_recognition_calls": naive,
            "calls_per_frame": (actual / self.frames_processed
                                if self.frames_processed else 0.0),
            "calls_avoided": max(naive - actual, 0),
            "reduction_percent": ((naive - actual) / naive * 100.0
                                  if naive else 0.0),
        }

    def prune(self, live_track_ids):
        """Drop identity state for tracks that no longer exist."""
        live = set(live_track_ids)
        for track_id in [t for t in self.identities if t not in live]:
            del self.identities[track_id]

    def reset(self):
        self.identities = {}
        self.recognition_calls = 0
        self.frames_processed = 0
        self.faces_seen = 0
