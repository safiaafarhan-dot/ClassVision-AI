"""
ClassVision AI - Recognition Stability (Step 4)
-----------------------------------------------
Stops the on-screen label from flickering between a name and "Unknown".

The problem
    Recognition runs independently on every frame. A face at the edge of the
    threshold will sit just above it in one frame and just below in the next,
    so the label flickers. A single frame is weak evidence.

What this does
    Requires the SAME identity in several consecutive frames before calling it
    confirmed, and smooths the displayed score over recent frames.

    frame 1  Rahul 0.81   ->  pending
    frame 2  Rahul 0.88   ->  pending
    frame 3  Rahul 0.91   ->  CONFIRMED: Rahul
    frame 4  Rahul 0.92   ->  CONFIRMED: Rahul

What this is NOT
    This is NOT object tracking and NOT attendance.

    Faces are associated between frames by nothing more than "the nearest box
    to where this one was last frame". That is a placeholder. It has no motion
    model, no re-identification after occlusion, and no stable track ids. Two
    people crossing in front of the camera can swap slots.

    Step 5 replaces this module with real multi-object tracking. Everything
    here is deliberately contained in one small file so that swap is easy.
"""

import time
from collections import deque

from face_recognizer import STATUS_MATCH, STATUS_NO_ENROLLMENTS, STATUS_UNKNOWN

# How many consecutive frames must agree before an identity is confirmed.
DEFAULT_FRAMES_TO_CONFIRM = 3

# How many recent scores to average when smoothing.
DEFAULT_SMOOTHING_WINDOW = 5

# A face must be within this fraction of the frame width of its previous
# position to be considered the same face.
DEFAULT_MATCH_RADIUS_FRACTION = 0.25

# Slots not seen for this long are forgotten.
DEFAULT_SLOT_TIMEOUT_SECONDS = 1.5


def box_center(box):
    x, y, width, height = box
    return (x + width / 2.0, y + height / 2.0)


class StabilisedIdentity:
    """
    The running state for one face slot.

    Holds recent results and decides when an identity is settled enough to
    display as confirmed.
    """

    __slots__ = ("frames_to_confirm", "history", "scores", "confirmed_identity",
                 "confirmed_name", "last_seen", "last_box", "pending_identity",
                 "pending_count")

    def __init__(self, frames_to_confirm=DEFAULT_FRAMES_TO_CONFIRM,
                 smoothing_window=DEFAULT_SMOOTHING_WINDOW):
        self.frames_to_confirm = int(frames_to_confirm)
        self.history = deque(maxlen=max(self.frames_to_confirm, 1))
        self.scores = deque(maxlen=max(int(smoothing_window), 1))
        self.confirmed_identity = None
        self.confirmed_name = None
        self.pending_identity = None
        self.pending_count = 0
        self.last_seen = time.time()
        self.last_box = None

    @property
    def smoothed_similarity(self):
        if not self.scores:
            return 0.0
        return sum(self.scores) / len(self.scores)

    @property
    def is_confirmed(self):
        return self.confirmed_identity is not None

    def update(self, result, now=None):
        """
        Feed in this frame's result and return the identity to display.

        Returns (identity, name, status, smoothed_similarity), where status is
        "MATCH" only once the identity has been confirmed.
        """
        self.last_seen = time.time() if now is None else now
        self.last_box = result.box
        self.scores.append(result.similarity)

        current = result.identity if result.status == STATUS_MATCH else None
        self.history.append(current)

        if current is None:
            # A non-match breaks the run of agreement.
            self.pending_identity = None
            self.pending_count = 0

            # Only drop a confirmed identity once the whole window disagrees.
            # One bad frame should not erase a settled name.
            if self.is_confirmed and all(item is None for item in self.history) \
                    and len(self.history) >= self.history.maxlen:
                self.confirmed_identity = None
                self.confirmed_name = None
        else:
            if current == self.pending_identity:
                self.pending_count += 1
            else:
                self.pending_identity = current
                self.pending_count = 1

            if self.pending_count >= self.frames_to_confirm:
                self.confirmed_identity = current
                self.confirmed_name = result.name

        if self.is_confirmed:
            return (self.confirmed_identity, self.confirmed_name,
                    STATUS_MATCH, self.smoothed_similarity)

        if result.status == STATUS_NO_ENROLLMENTS:
            return (None, None, STATUS_NO_ENROLLMENTS, self.smoothed_similarity)

        return (None, None, STATUS_UNKNOWN, self.smoothed_similarity)


class StabilitySet:
    """
    Keeps one StabilisedIdentity per face currently on screen.

    Association between frames is by nearest box centre only - see the module
    docstring for why that is a placeholder, not tracking.
    """

    def __init__(self, frames_to_confirm=DEFAULT_FRAMES_TO_CONFIRM,
                 smoothing_window=DEFAULT_SMOOTHING_WINDOW,
                 match_radius_fraction=DEFAULT_MATCH_RADIUS_FRACTION,
                 timeout_seconds=DEFAULT_SLOT_TIMEOUT_SECONDS):
        self.frames_to_confirm = int(frames_to_confirm)
        self.smoothing_window = int(smoothing_window)
        self.match_radius_fraction = float(match_radius_fraction)
        self.timeout_seconds = float(timeout_seconds)
        self.slots = []

    @property
    def slot_count(self):
        return len(self.slots)

    def expire(self, now=None):
        """Forget slots whose face has not been seen for a while."""
        moment = time.time() if now is None else now
        self.slots = [slot for slot in self.slots
                      if moment - slot.last_seen <= self.timeout_seconds]

    def _closest_free_slot(self, box, used, frame_width):
        """Find the nearest unused slot within the match radius."""
        if box is None:
            return None

        radius = frame_width * self.match_radius_fraction
        centre = box_center(box)
        best_slot = None
        best_distance = None

        for slot in self.slots:
            if id(slot) in used or slot.last_box is None:
                continue

            other = box_center(slot.last_box)
            distance = ((centre[0] - other[0]) ** 2
                        + (centre[1] - other[1]) ** 2) ** 0.5

            if distance <= radius and (best_distance is None or distance < best_distance):
                best_slot = slot
                best_distance = distance

        return best_slot

    def update(self, results, frame_width, now=None):
        """
        Feed one frame's results in and get the stabilised versions back.

        The returned list matches the input order, one entry per face:
        (identity, name, status, smoothed_similarity).
        """
        moment = time.time() if now is None else now
        self.expire(moment)

        used = set()
        stabilised = []

        for result in results:
            slot = self._closest_free_slot(result.box, used, frame_width)

            if slot is None:
                slot = StabilisedIdentity(self.frames_to_confirm,
                                          self.smoothing_window)
                self.slots.append(slot)

            used.add(id(slot))
            stabilised.append(slot.update(result, now=moment))

        return stabilised

    def reset(self):
        self.slots = []
