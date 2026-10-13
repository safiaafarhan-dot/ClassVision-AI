"""
ClassVision AI - Multi-Person Tracking (Step 5)
-----------------------------------------------
Follows each face across frames and gives it a stable track id.

    Step 5 does not calculate attendance. It produces tracks and a presence
    timeline that a later step will turn into attendance.

This module is purely geometric. It knows nothing about students, identities,
embeddings or attendance - it only answers "is this the same box as last
frame?". Identity lives in track_identity.py, timing lives in
presence_timeline.py.

Why a tracker was written here instead of installed
    Checked in this environment before deciding:
      - ByteTrack / BoT-SORT / DeepSORT need torch, or scipy plus a linear
        assignment package. This project has three dependencies in total and
        runs on CPU; adding a deep-learning stack for box association is a
        poor trade.
      - OpenCV 5.0 REMOVED cv2.legacy, TrackerCSRT and TrackerKCF. What is
        left (MIL, DaSiamRPN, Nano, ViT) are SINGLE-object trackers. Tracking
        N people would mean N tracker instances, each doing its own visual
        search every frame, and some need their own model downloads.
      - YuNet already detects every face in every frame in about 11 ms. When
        detections are that good, association is the only missing piece.

    So this is tracking-by-detection, the same family as SORT and ByteTrack,
    implemented directly in about 200 lines with no new dependency.

The ByteTrack idea that is worth keeping
    ByteTrack's central insight is that low-confidence detections are usually
    real people who are briefly blurred or partly hidden - not noise. Throwing
    them away breaks tracks; using them only to CONTINUE existing tracks
    recovers those people without inventing new ones.

    So association happens in two passes:
      pass 1  existing tracks  <-> high-confidence detections
      pass 2  still-unmatched tracks <-> low-confidence detections
      new tracks are created ONLY from unmatched high-confidence detections.

Determinism
    Matching is greedy on overlap, with ties broken by track id and box
    position rather than list position. The same set of detections therefore
    produces the same tracks no matter what order the detector returned them
    in, which matters both for correctness and for testable behaviour.
"""

# A detection must overlap a track by at least this much to continue it.
DEFAULT_IOU_THRESHOLD = 0.3

# Detections at or above this confidence can start a NEW track. Below it, a
# detection may only continue an existing one.
DEFAULT_HIGH_CONFIDENCE = 0.70

# How many frames a track may go unseen before it is given up as gone. At the
# measured ~15 FPS this is roughly one second.
#
# This is a tolerance for blinking, turning away, or someone walking in front
# of the camera - NOT a classroom policy. How long a person may be absent
# before it means something is a Step 6 decision, made from the timeline.
DEFAULT_MAX_LOST_FRAMES = 15

# A track must be seen this many times before it is trusted. A single frame is
# often a false positive.
DEFAULT_MIN_HITS = 3

# How much of the last movement to carry forward when predicting where a box
# will be. Keeps association working while someone walks across the frame.
DEFAULT_VELOCITY_SMOOTHING = 0.5

TRACK_TENTATIVE = "TENTATIVE"
TRACK_CONFIRMED = "CONFIRMED"
TRACK_LOST = "LOST"
TRACK_REMOVED = "REMOVED"


def iou(first, second):
    """
    Intersection over union of two (x, y, width, height) boxes.

    1.0 means identical, 0.0 means no overlap at all.
    """
    ax, ay, aw, ah = first
    bx, by, bw, bh = second

    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0

    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)

    if right <= left or bottom <= top:
        return 0.0

    overlap = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - overlap

    return overlap / union if union > 0 else 0.0


class Track:
    """One person being followed across frames."""

    __slots__ = ("track_id", "box", "velocity", "state", "confidence",
                 "hits", "age", "frames_since_seen", "first_seen_frame",
                 "last_seen_frame", "reacquired")

    def __init__(self, track_id, box, frame_index, confidence=0.0):
        self.track_id = int(track_id)
        self.box = tuple(int(v) for v in box)
        self.velocity = (0.0, 0.0)
        self.state = TRACK_TENTATIVE
        self.confidence = float(confidence)

        self.hits = 1
        self.age = 0
        self.frames_since_seen = 0
        self.first_seen_frame = int(frame_index)
        self.last_seen_frame = int(frame_index)

        # True on the single frame where a lost track was picked up again.
        self.reacquired = False

    @property
    def center(self):
        x, y, width, height = self.box
        return (x + width / 2.0, y + height / 2.0)

    @property
    def is_visible(self):
        return self.frames_since_seen == 0

    @property
    def is_lost(self):
        return self.state == TRACK_LOST

    @property
    def is_confirmed(self):
        return self.state == TRACK_CONFIRMED

    def predicted_box(self):
        """
        Where this box is expected to be now, based on its recent movement.

        Used only for association. A stationary track predicts itself.
        """
        if self.frames_since_seen == 0 and self.velocity == (0.0, 0.0):
            return self.box

        x, y, width, height = self.box
        steps = max(self.frames_since_seen, 1)
        return (int(round(x + self.velocity[0] * steps)),
                int(round(y + self.velocity[1] * steps)),
                width, height)

    def mark_seen(self, box, frame_index, confidence, min_hits,
                  smoothing=DEFAULT_VELOCITY_SMOOTHING):
        """Update this track with the detection that matched it."""
        new_box = tuple(int(v) for v in box)

        previous_x, previous_y = self.box[0], self.box[1]
        gap = max(self.frames_since_seen, 1)
        measured = ((new_box[0] - previous_x) / float(gap),
                    (new_box[1] - previous_y) / float(gap))

        # Blend old and new movement so one jumpy frame does not throw the
        # prediction off.
        self.velocity = (
            smoothing * measured[0] + (1.0 - smoothing) * self.velocity[0],
            smoothing * measured[1] + (1.0 - smoothing) * self.velocity[1],
        )

        self.reacquired = self.frames_since_seen > 0

        self.box = new_box
        self.confidence = float(confidence)
        self.hits += 1
        self.frames_since_seen = 0
        self.last_seen_frame = int(frame_index)

        if self.state == TRACK_LOST or self.state == TRACK_TENTATIVE:
            self.state = TRACK_CONFIRMED if self.hits >= min_hits else TRACK_TENTATIVE

    def mark_missed(self):
        """This track had no detection in this frame."""
        self.frames_since_seen += 1
        self.reacquired = False

        if self.state != TRACK_REMOVED:
            self.state = TRACK_LOST

    def __repr__(self):
        return "Track(id=%d, box=%s, state=%s, hits=%d, missed=%d)" % (
            self.track_id, self.box, self.state, self.hits,
            self.frames_since_seen)


class PersonTracker:
    """
    Turns per-frame face detections into stable tracks.

    Call update() once per frame with that frame's detections.
    """

    def __init__(self, iou_threshold=DEFAULT_IOU_THRESHOLD,
                 high_confidence=DEFAULT_HIGH_CONFIDENCE,
                 max_lost_frames=DEFAULT_MAX_LOST_FRAMES,
                 min_hits=DEFAULT_MIN_HITS):
        self.iou_threshold = float(iou_threshold)
        self.high_confidence = float(high_confidence)
        self.max_lost_frames = int(max_lost_frames)
        self.min_hits = int(min_hits)

        self.tracks = []
        self.removed_tracks = []
        self._next_track_id = 1
        self.frame_index = -1

    @property
    def active_tracks(self):
        """Tracks seen in the most recent frame."""
        return [t for t in self.tracks if t.is_visible]

    @property
    def track_count(self):
        return len(self.tracks)

    def _new_track(self, box, frame_index, confidence):
        track = Track(self._next_track_id, box, frame_index, confidence)
        self._next_track_id += 1
        self.tracks.append(track)
        return track

    def _associate(self, tracks, detections, used_detections):
        """
        Greedily pair tracks with detections by overlap.

        Returns {track_id: detection}. Pairs are considered strongest-overlap
        first; ties are broken by track id and box position so the result never
        depends on the order detections were supplied in.
        """
        candidates = []

        for track in tracks:
            predicted = track.predicted_box()
            for index, detection in enumerate(detections):
                if index in used_detections:
                    continue

                score = iou(predicted, detection.as_tuple())
                if score >= self.iou_threshold:
                    # Sort key avoids list position entirely.
                    candidates.append((-score, track.track_id,
                                       detection.x, detection.y, index, track))

        candidates.sort()

        matched = {}
        for _, track_id, _, _, index, track in candidates:
            if track_id in matched or index in used_detections:
                continue
            matched[track_id] = detections[index]
            used_detections.add(index)

        return matched

    def update(self, detections, frame_index=None):
        """
        Advance the tracker by one frame.

        detections  - this frame's Detection objects, in any order
        frame_index - the frame number; counted automatically if omitted

        Returns the list of tracks that are currently visible.
        """
        self.frame_index = (self.frame_index + 1 if frame_index is None
                            else int(frame_index))

        detections = list(detections or [])

        for track in self.tracks:
            track.age += 1
            track.reacquired = False

        high = [d for d in detections if d.confidence >= self.high_confidence]
        low = [d for d in detections if d.confidence < self.high_confidence]

        # Pass 1: every track competes for the confident detections.
        used_high = set()
        matched = self._associate(self.tracks, high, used_high)

        # Pass 2: tracks that found nothing may be continued by a weak
        # detection. This is the ByteTrack recovery step - it keeps a person
        # who briefly blurred or turned away, without inventing new people.
        unmatched_tracks = [t for t in self.tracks if t.track_id not in matched]
        used_low = set()
        matched.update(self._associate(unmatched_tracks, low, used_low))

        for track in self.tracks:
            detection = matched.get(track.track_id)
            if detection is not None:
                track.mark_seen(detection.as_tuple(), self.frame_index,
                                detection.confidence, self.min_hits)
            else:
                track.mark_missed()

        # New tracks come only from confident, unused detections. A weak
        # detection is never allowed to invent a person.
        for index, detection in enumerate(high):
            if index not in used_high:
                self._new_track(detection.as_tuple(), self.frame_index,
                                detection.confidence)

        self._retire_expired()

        return self.active_tracks

    def _retire_expired(self):
        """Move tracks that have been gone too long out of the active list."""
        still_alive = []

        for track in self.tracks:
            if track.frames_since_seen > self.max_lost_frames:
                track.state = TRACK_REMOVED
                self.removed_tracks.append(track)
            else:
                still_alive.append(track)

        self.tracks = still_alive

    def get(self, track_id):
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        return None

    def take_removed(self):
        """Return tracks retired since the last call, and clear the list."""
        removed = self.removed_tracks
        self.removed_tracks = []
        return removed

    def reset(self):
        self.tracks = []
        self.removed_tracks = []
        self._next_track_id = 1
        self.frame_index = -1
