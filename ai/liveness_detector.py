"""
ClassVision AI - Liveness / Presentation Attack Detection (Step 9)
------------------------------------------------------------------
Asks one question, and only one:

    "Does this tracked face look like it is coming from a physical person in
     front of the camera, rather than from a photograph or a screen?"

    STEP 9 IS A RESEARCH PROTOTYPE. It is NOT production-grade presentation
    attack detection. Its behaviour on real presentation attacks has NOT been
    established, because no presentation attack has ever been shown to this
    system and no PAD dataset was available or licensed for use here.

    No accuracy, FAR, FRR, APCER, BPCER or ACER is claimed. See
    "Step 9 - Evaluation" in the README, which says "Not evaluated" and means
    it.

What this is NOT
    - not face recognition. It never learns, guesses or asserts WHO somebody
      is. A SPOOF verdict names nobody;
    - not deepfake detection. A synthetic face rendered onto a real person's
      head is out of scope and is not attempted;
    - not a liveness "score" anyone should treat as a probability. The cues
      below are uncalibrated measurements, and nothing here is published as a
      confidence.

Why a deterministic baseline rather than a trained model
    Checked before deciding, in this environment:
      - OpenCV 5.0 ships no presentation-attack model, so there was nothing
        already present to use;
      - the project's rule is that model weights are downloaded only with a
        documented source, licence and SHA-256. No PAD model met that bar
        without pulling in a deep-learning runtime this project deliberately
        does not have;
      - a trained detector needs a PAD dataset with an identity-disjoint
        split. None was available, and using classmates' faces without
        consent and authorisation is not acceptable.

    The honest option left is a documented, explainable baseline whose
    limitations can be written down - not a heuristic dressed up as a model.

The three cues, and why each one
    Every cue is measured over a WINDOW of observations. There is no
    single-frame rule anywhere in this file, and no cue is trusted alone: a
    verdict requires two cues to agree, in either direction.

    A. NON-RIGID LANDMARK DYNAMICS  (the primary cue)
       The five landmarks are normalised for translation AND scale, then the
       variation of that normalised configuration is measured across the
       window.

       This is the cue that separates a face from a photograph of a face. A
       real head is a moving 3D object: the eyes, nose and mouth shift
       relative to each other constantly, even when somebody is sitting
       still. A flat print or a phone screen does not - and crucially, this
       stays true when the attacker MOVES the photo, because translation and
       scale are normalised away. Waving a photo produces rigid motion with
       near-zero internal deformation, which is exactly what this measures.

    B. TEXTURE DETAIL
       High-frequency energy in the face region, normalised by the region's
       own intensity variance.

       Re-capturing a print or a display loses fine detail - skin texture,
       eyelashes, fine shadow - relative to overall contrast. Normalising by
       intensity variance is deliberate: it keeps this from degenerating into
       a brightness or contrast rule, which would be worthless.

    C. TEXTURE STABILITY OVER TIME
       How much cue B fluctuates across the window.

       A replayed still image gives an almost constant texture measurement. A
       real face under real light does not: micro-movement and changing
       shadow keep it moving. A value that never moves is itself evidence.

    Blinking is deliberately NOT used as proof of liveness. It is easy to
    replay, easy to miss, and a person who does not blink during the window
    is not a photograph.

Privacy
    A measurement is taken from the frame and the frame is dropped. Nothing
    here stores, returns or logs an image, a face crop, an embedding, or any
    model-internal feature vector. What a track keeps is a short window of
    five-point normalised geometry and two floating-point statistics per
    observation - see LivenessSample, which has __slots__ for exactly that.

Failure behaviour
    Anything that cannot be measured produces an UNUSABLE sample, never a
    guess. A window without enough usable samples is UNCERTAIN. An exception
    is UNCERTAIN. "Liveness unavailable" NEVER becomes "everybody is live".
"""

import os

import numpy as np

# ---------------------------------------------------------------------------
# States - exactly four, and UNCERTAIN is a real answer
# ---------------------------------------------------------------------------
#
# UNKNOWN    nothing has been assessed for this track yet
# LIVE       two cues agree that this looks like a physical person
# SPOOF      two cues agree that this looks like a presented image
# UNCERTAIN  not enough evidence, conflicting evidence, or no analysis
LIVENESS_UNKNOWN = "UNKNOWN"
LIVENESS_LIVE = "LIVE"
LIVENESS_SPOOF = "SPOOF"
LIVENESS_UNCERTAIN = "UNCERTAIN"

ALL_LIVENESS_STATES = (LIVENESS_UNKNOWN, LIVENESS_LIVE, LIVENESS_SPOOF,
                       LIVENESS_UNCERTAIN)

# ---------------------------------------------------------------------------
# Operating modes
# ---------------------------------------------------------------------------
#
# OFF      no analysis at all. Zero cost, everything stays UNKNOWN.
# REPORT   analyse, show the result, and report what WOULD have been blocked -
#          but let every segment reach the attendance engine.
# ENFORCE  analyse, and let only eligible evidence reach the engine.
#
# REPORT is the default, and that is a deliberate safety choice rather than
# timidity: this baseline has never been evaluated against a real attack, so
# allowing it to silently delete a real student's attendance would be trading
# a known harm for an unmeasured benefit. Enforcement is one environment
# variable away, and is fully tested.
LIVENESS_MODE_OFF = "off"
LIVENESS_MODE_REPORT = "report"
LIVENESS_MODE_ENFORCE = "enforce"

ALL_LIVENESS_MODES = (LIVENESS_MODE_OFF, LIVENESS_MODE_REPORT,
                      LIVENESS_MODE_ENFORCE)

DEFAULT_LIVENESS_MODE = LIVENESS_MODE_REPORT

LIVENESS_MODE_ENV = "CLASSVISION_LIVENESS_MODE"
LIVENESS_INTERVAL_ENV = "CLASSVISION_LIVENESS_INTERVAL"

# A version string for the baseline, so a stored or reported verdict can
# always be traced to the rules that produced it. There is no model file and
# no weights, which is why this is called a baseline version and not a model
# version.
BASELINE_NAME = "classvision-heuristic-pad"
BASELINE_VERSION = "0.9.0"

# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
#
# Liveness is measured every Nth frame per track, not every frame. Two
# reasons, and the second matters more than the first:
#
#   1. cost - the same argument Step 5 makes for recognition scheduling;
#   2. signal - at the measured ~15 FPS, consecutive frames are 66 ms apart
#      and a face barely moves between them. Spacing the samples out is what
#      makes the motion cue measurable at all.
#
# 3 frames is ~200 ms at 15 FPS, so a full window spans roughly 2.4 seconds.
DEFAULT_SAMPLE_INTERVAL_FRAMES = 3

# How many observations the window holds. Long enough to see natural head
# movement, short enough that a verdict is not stale.
DEFAULT_WINDOW_SIZE = 12

# The minimum usable observations before ANY verdict other than UNCERTAIN may
# be returned. This is what makes a single-frame verdict impossible.
DEFAULT_MINIMUM_SAMPLES = 6

# The face region is resampled to this square before the texture cues, so the
# measurement does not depend on how close somebody is sitting.
TEXTURE_SIZE = 96

# The smallest face worth measuring. Below this the texture cues are noise.
MINIMUM_FACE_PIXELS = 40

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING ANY NUMBER BELOW.
#
# These were chosen by MEASURING the cues on controlled synthetic sequences -
# see ai/liveness_test.py --calibrate, which prints the distributions these
# came from and is the reproducible source for every figure quoted in the
# README. They were not guessed, and they were not tuned until a demo looked
# good.
#
# The TEXTURE thresholds are anchored on REAL measurements. texture_detail()
# was measured over 40 real frames from this machine's webcam at 640x480, on a
# 120x120 region - the size of a plausible classroom face:
#
#     real, sharp          0.684 - 2.614   (median 1.263)
#     one blur pass        0.111 - 0.406   (median 0.202)
#     two blur passes      0.062 - 0.248   (median 0.114)
#     three blur passes    0.042 - 0.183   (median 0.079)
#
# Blurring is this project's model of the detail loss a print or a display
# recapture causes; a blur pass costs roughly 6x of this statistic. Those
# ranges are what the two texture thresholds below sit between, with a
# deliberate UNCERTAIN band left open between them. A cue that has to choose
# between LIVE and SPOOF for every value would have no way to say "I cannot
# tell", which is the answer this layer needs most often.
#
# The MOTION thresholds come from the synthetic sequences, because a
# photograph's defining property - zero non-rigid landmark deformation under
# any amount of waving - can be reproduced exactly. They are set conservatively
# so that "very still real person" lands in UNCERTAIN rather than SPOOF:
# wrongly accusing a student is the more damaging error of the two.
#
# What NONE of this establishes:
#     behaviour on a real photograph, a real screen, a replayed video of a
#     person, a mask, or any attack performed by somebody trying to defeat it.
#     No face - live or presented - has ever been measured by this code. See
#     "Step 9 - Known failure modes" in the README, and in particular the
#     landmark-noise limitation, which is the biggest one.
MOTION_LIVE_MIN = 0.012      # normalised landmark variation, live evidence
MOTION_SPOOF_MAX = 0.004     # below this, the geometry is not deforming
DETAIL_LIVE_MIN = 0.250      # below the quietest real sharp frame (0.684)
DETAIL_SPOOF_MAX = 0.100     # below a twice-blurred real frame (median 0.114)
VARIATION_SPOOF_MAX = 0.080  # real camera frames varied by 0.33 or more


class LivenessConfigError(ValueError):
    """Raised when the liveness layer is configured with something unusable."""


def configured_mode(explicit=None):
    """Explicit argument first, then the environment, then the default."""
    value = explicit if explicit is not None else os.environ.get(
        LIVENESS_MODE_ENV)

    if not value:
        return DEFAULT_LIVENESS_MODE

    mode = str(value).strip().lower()
    if mode not in ALL_LIVENESS_MODES:
        raise LivenessConfigError(
            "unknown liveness mode %r - valid modes are %s"
            % (value, ", ".join(ALL_LIVENESS_MODES)))

    return mode


def configured_interval(explicit=None):
    value = explicit if explicit is not None else os.environ.get(
        LIVENESS_INTERVAL_ENV)

    # Note the explicit None/"" test rather than a truthiness test: 0 is a
    # falsy value AND an invalid interval, and it must be rejected loudly
    # instead of quietly becoming the default.
    if value is None or value == "":
        return DEFAULT_SAMPLE_INTERVAL_FRAMES

    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise LivenessConfigError(
            "%s must be a whole number of frames, got %r"
            % (LIVENESS_INTERVAL_ENV, value))

    if interval < 1:
        raise LivenessConfigError(
            "liveness interval must be at least 1 frame, got %d" % interval)

    return interval


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

class LivenessSample:
    """
    One measurement of one track, at one moment.

    __slots__ is the privacy boundary as much as an optimisation: there is
    nowhere in this object to put an image, a crop, or a feature vector even
    by accident. It holds five normalised (x, y) points and two numbers.
    """

    __slots__ = ("frame_index", "shape", "detail", "usable", "reason")

    def __init__(self, frame_index, shape=None, detail=0.0, usable=True,
                 reason=None):
        self.frame_index = int(frame_index)
        # Landmark geometry with translation and scale removed. None when the
        # landmarks were missing.
        self.shape = shape
        self.detail = float(detail)
        self.usable = bool(usable)
        self.reason = reason

    def __repr__(self):
        return "LivenessSample(frame=%d, detail=%.4f, usable=%s)" % (
            self.frame_index, self.detail, self.usable)


def _as_grayscale(frame):
    """A 2D float array, whatever colour layout the camera used."""
    array = np.asarray(frame)

    if array.ndim == 2:
        return array.astype(np.float32)

    if array.ndim == 3 and array.shape[2] >= 3:
        # Rec. 601 luma over the first three channels. OpenCV hands over BGR,
        # so the weights are reversed relative to the usual RGB ordering.
        blue = array[:, :, 0].astype(np.float32)
        green = array[:, :, 1].astype(np.float32)
        red = array[:, :, 2].astype(np.float32)
        return 0.299 * red + 0.587 * green + 0.114 * blue

    if array.ndim == 3 and array.shape[2] == 1:
        return array[:, :, 0].astype(np.float32)

    raise ValueError("a frame must be 2D or 3D, got shape %s"
                     % (array.shape,))


def _resample(region, size=TEXTURE_SIZE):
    """
    Nearest-neighbour resample to a fixed square.

    Written with numpy indexing rather than cv2.resize so this module needs no
    OpenCV at all: it can then be tested, benchmarked and reasoned about
    without loading a vision stack.
    """
    height, width = region.shape
    rows = np.minimum((np.arange(size) * height) // size, height - 1)
    columns = np.minimum((np.arange(size) * width) // size, width - 1)
    return region[rows][:, columns]


def texture_detail(region):
    """
    High-frequency energy, normalised by the region's own intensity variance.

    The normalisation is the important part. Raw Laplacian variance rises with
    contrast, so an unnormalised version would mostly measure how brightly lit
    somebody is - a brightness rule wearing a disguise. Dividing by the
    intensity variance asks a better question: "of the variation in this
    region, how much of it is FINE detail?"

    A print or a display recapture loses fine detail while keeping its overall
    contrast, so this figure falls.
    """
    if region.size == 0 or min(region.shape) < 3:
        return 0.0

    patch = _resample(region)

    # 4-neighbour discrete Laplacian, interior pixels only.
    laplacian = (-4.0 * patch[1:-1, 1:-1]
                 + patch[:-2, 1:-1] + patch[2:, 1:-1]
                 + patch[1:-1, :-2] + patch[1:-1, 2:])

    intensity_variance = float(np.var(patch))
    if intensity_variance <= 1e-6:
        # A perfectly flat region carries no detail and no contrast either.
        return 0.0

    return float(np.var(laplacian)) / intensity_variance


def normalised_shape(landmarks):
    """
    Landmark geometry with translation and scale removed.

    Removing BOTH is what makes the motion cue an attack detector rather than
    a movement detector. A photograph carried across the room translates; a
    photograph moved towards the camera scales. Neither changes the normalised
    configuration, so neither looks like a living face. Only genuine
    deformation - a head turning, a jaw moving, an expression changing - does.
    """
    points = np.asarray(landmarks, dtype=np.float32).reshape(-1, 2)

    if points.shape[0] < 3:
        raise ValueError("at least three landmarks are needed, got %d"
                         % points.shape[0])

    centred = points - points.mean(axis=0)

    scale = float(np.sqrt(np.mean(np.sum(centred ** 2, axis=1))))
    if scale <= 1e-6:
        raise ValueError("landmarks are degenerate - all points coincide")

    return centred / scale


class LivenessVerdict:
    """
    What the detector concluded about one track, and why.

    The reason is written to be read by a teacher. There is no score field,
    deliberately: the cues are uncalibrated measurements, and publishing one
    as a number invites it to be read as a probability it is not.
    """

    __slots__ = ("track_id", "state", "reason", "observations",
                 "usable_observations", "frame_index", "baseline_version")

    def __init__(self, track_id, state, reason, observations=0,
                 usable_observations=0, frame_index=-1):
        self.track_id = int(track_id)
        self.state = state
        self.reason = reason
        self.observations = int(observations)
        self.usable_observations = int(usable_observations)
        self.frame_index = int(frame_index)
        self.baseline_version = BASELINE_VERSION

    @property
    def is_live(self):
        return self.state == LIVENESS_LIVE

    @property
    def is_spoof(self):
        return self.state == LIVENESS_SPOOF

    def to_dict(self):
        """Safe to log. No image, no crop, no vector, no raw cue value."""
        return {
            "track_id": self.track_id,
            "liveness_state": self.state,
            "liveness_reason": self.reason,
            "liveness_observations": self.observations,
            "baseline_version": self.baseline_version,
        }

    def __repr__(self):
        return "LivenessVerdict(track=%d, %s, %d obs)" % (
            self.track_id, self.state, self.observations)


class LivenessDetector:
    """
    Turns tracked faces into per-track liveness verdicts.

    Holds a short window of measurements per track, because a verdict from one
    frame would be meaningless. It holds no images and no identities: a track
    id is the only thing it knows about anybody.
    """

    def __init__(self, window_size=DEFAULT_WINDOW_SIZE,
                 minimum_samples=DEFAULT_MINIMUM_SAMPLES,
                 motion_live_min=MOTION_LIVE_MIN,
                 motion_spoof_max=MOTION_SPOOF_MAX,
                 detail_live_min=DETAIL_LIVE_MIN,
                 detail_spoof_max=DETAIL_SPOOF_MAX,
                 variation_spoof_max=VARIATION_SPOOF_MAX):
        self.window_size = max(int(window_size), 2)
        self.minimum_samples = max(int(minimum_samples), 2)

        if self.minimum_samples > self.window_size:
            raise LivenessConfigError(
                "minimum_samples (%d) cannot exceed the window size (%d)"
                % (self.minimum_samples, self.window_size))

        self.motion_live_min = float(motion_live_min)
        self.motion_spoof_max = float(motion_spoof_max)
        self.detail_live_min = float(detail_live_min)
        self.detail_spoof_max = float(detail_spoof_max)
        self.variation_spoof_max = float(variation_spoof_max)

        if self.motion_spoof_max >= self.motion_live_min:
            raise LivenessConfigError(
                "the spoof and live motion thresholds overlap (%r >= %r), so "
                "no observation could ever be UNCERTAIN"
                % (self.motion_spoof_max, self.motion_live_min))

        self._windows = {}
        self.samples_taken = 0
        self.failed_samples = 0

    # -- measurement -------------------------------------------------------

    def observe(self, track_id, frame, detection):
        """
        Measure one frame for one track and add it to that track's window.

        Never raises. Anything unmeasurable becomes an UNUSABLE sample, which
        is evidence of nothing rather than evidence of liveness.
        """
        frame_index = getattr(detection, "frame_index", -1)
        sample = self._measure(frame, detection, frame_index)

        window = self._windows.setdefault(track_id, [])
        window.append(sample)
        if len(window) > self.window_size:
            del window[0]

        self.samples_taken += 1
        if not sample.usable:
            self.failed_samples += 1

        return sample

    def _measure(self, frame, detection, frame_index):
        try:
            return self._measure_strict(frame, detection, frame_index)
        except Exception as error:                       # noqa: BLE001
            # Deliberately broad. A measurement that cannot be taken must
            # never stop the classroom session, and must never be rounded up
            # to LIVE. The exception TYPE is kept because it is useful and
            # carries no data; the message is not, because it could contain a
            # path or an array dump.
            return LivenessSample(frame_index, usable=False,
                                  reason="not measurable (%s)"
                                         % type(error).__name__)

    def _measure_strict(self, frame, detection, frame_index):
        landmarks = getattr(detection, "landmarks", None)
        if landmarks is None:
            return LivenessSample(frame_index, usable=False,
                                  reason="no landmarks for this face")

        shape = normalised_shape(landmarks)

        array = np.asarray(frame)
        if array.ndim not in (2, 3):
            raise ValueError("a frame must be 2D or 3D, got shape %s"
                             % (array.shape,))

        height, width = array.shape[0], array.shape[1]

        left = max(int(detection.x), 0)
        top = max(int(detection.y), 0)
        right = min(int(detection.x) + int(detection.width), width)
        bottom = min(int(detection.y) + int(detection.height), height)

        if right - left < MINIMUM_FACE_PIXELS or \
                bottom - top < MINIMUM_FACE_PIXELS:
            return LivenessSample(
                frame_index, shape=shape, usable=False,
                reason="face region too small to measure texture")

        # Crop FIRST, convert second. Converting a whole 640x480 frame to
        # float costs about 2.5 ms; converting a 120x120 face costs a
        # fraction of that, and the rest of the frame was never going to
        # be looked at. The crop is a view into the caller's array -
        # nothing is copied, and nothing outlives this function.
        region = _as_grayscale(array[top:bottom, left:right])
        detail = texture_detail(region)

        # The region goes out of scope here. Nothing above this line is kept.
        return LivenessSample(frame_index, shape=shape, detail=detail,
                              usable=True)

    # -- assessment --------------------------------------------------------

    def assess(self, track_id):
        """
        The verdict for one track, from its whole window.

        Two cues must agree before anything other than UNCERTAIN is returned.
        """
        window = self._windows.get(track_id, [])
        usable = [sample for sample in window if sample.usable]
        latest = window[-1].frame_index if window else -1

        if len(usable) < self.minimum_samples:
            if window and not usable:
                return LivenessVerdict(
                    track_id, LIVENESS_UNCERTAIN,
                    "liveness analysis is unavailable for this track (%s)"
                    % (window[-1].reason or "not measurable"),
                    len(window), len(usable), latest)

            return LivenessVerdict(
                track_id, LIVENESS_UNCERTAIN,
                "not enough observations yet (%d of %d needed)"
                % (len(usable), self.minimum_samples),
                len(window), len(usable), latest)

        motion = self._motion(usable)
        detail = float(np.median([sample.detail for sample in usable]))
        variation = self._variation(usable)

        if motion is None:
            return LivenessVerdict(
                track_id, LIVENESS_UNCERTAIN,
                "facial geometry could not be compared across observations",
                len(window), len(usable), latest)

        # Cue A is mandatory in BOTH directions, and the texture cues can only
        # support it. That is deliberate: the landmark-dynamics cue is the one
        # with a physical argument behind it - a flat object does not deform,
        # however it is waved about - while the texture cues are a model of
        # detail loss, not a measurement of real prints and real screens.
        #
        # Letting texture decide alone would produce the two worst errors this
        # layer can make: calling a blurry, distant, very still student a
        # SPOOF, and calling a sharp photograph LIVE.
        live_cues = []
        if motion >= self.motion_live_min:
            live_cues.append("facial geometry is changing between observations")
        if detail >= self.detail_live_min:
            live_cues.append("the face carries fine detail")

        spoof_cues = []
        if motion <= self.motion_spoof_max:
            spoof_cues.append("facial geometry is not changing, even allowing "
                              "for movement of the whole face")
        if detail <= self.detail_spoof_max:
            spoof_cues.append("fine detail is missing, as in a print or a "
                              "screen")
        if variation <= self.variation_spoof_max:
            spoof_cues.append("the image is unnaturally static between "
                              "observations")

        # Two cues, in the same direction, and the geometry cue is one of
        # them. One cue is never enough in either direction.
        moving = motion >= self.motion_live_min
        still = motion <= self.motion_spoof_max

        spoof_agrees = still and len(spoof_cues) >= 2
        live_agrees = moving and len(live_cues) >= 2

        if spoof_agrees and not live_agrees:
            return LivenessVerdict(track_id, LIVENESS_SPOOF,
                                   "; ".join(spoof_cues[:2]),
                                   len(window), len(usable), latest)

        if live_agrees and not spoof_agrees:
            return LivenessVerdict(track_id, LIVENESS_LIVE,
                                   "; ".join(live_cues),
                                   len(window), len(usable), latest)

        if spoof_agrees and live_agrees:
            return LivenessVerdict(
                track_id, LIVENESS_UNCERTAIN,
                "the evidence disagrees with itself", len(window),
                len(usable), latest)

        return LivenessVerdict(
            track_id, LIVENESS_UNCERTAIN,
            "no two measurements agree strongly enough to decide",
            len(window), len(usable), latest)

    def measurements(self, track_id):
        """
        The raw cue values for one track. For calibration and tests ONLY.

        Deliberately not reachable from any API route: these are uncalibrated
        numbers, and publishing them would invite somebody to read one as a
        confidence.
        """
        usable = [s for s in self._windows.get(track_id, []) if s.usable]
        if not usable:
            return None

        return {
            "motion": self._motion(usable),
            "detail": float(np.median([s.detail for s in usable])),
            "variation": self._variation(usable),
            "usable_observations": len(usable),
        }

    def _motion(self, usable):
        """
        Cue A: how much the normalised landmark configuration varies.

        Returns None when the geometry was never available.
        """
        shapes = [sample.shape for sample in usable if sample.shape is not None]
        if len(shapes) < 2:
            return None

        stacked = np.stack(shapes)
        # Standard deviation per landmark coordinate, averaged. In units of
        # the face's own size, so it means the same thing near or far.
        return float(np.mean(np.std(stacked, axis=0)))

    def _variation(self, usable):
        """Cue C: how much cue B moves. Coefficient of variation."""
        details = np.array([sample.detail for sample in usable],
                           dtype=np.float64)
        mean = float(np.mean(details))

        if mean <= 1e-9:
            # No detail at all is itself maximally static.
            return 0.0

        return float(np.std(details) / mean)

    # -- housekeeping ------------------------------------------------------

    def forget(self, track_id):
        """Release a track's window. Called when the tracker retires it."""
        self._windows.pop(track_id, None)

    def prune(self, live_track_ids):
        live = set(live_track_ids)
        for track_id in [t for t in self._windows if t not in live]:
            del self._windows[track_id]

    def window_size_for(self, track_id):
        return len(self._windows.get(track_id, []))

    def tracked_ids(self):
        return sorted(self._windows)

    def reset(self):
        self._windows = {}
        self.samples_taken = 0
        self.failed_samples = 0

    def describe(self):
        """What this detector is, for the API and the logs."""
        return {
            "baseline": BASELINE_NAME,
            "baseline_version": BASELINE_VERSION,
            "window_size": self.window_size,
            "minimum_samples": self.minimum_samples,
            "trained": False,
            "evaluated": False,
        }
