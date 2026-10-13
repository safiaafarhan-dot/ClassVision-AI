"""
ClassVision AI - Video Authenticity Analysis (Step 10)
------------------------------------------------------
Asks a third question about a tracked face, separate from the other two:

    "Does this face region look like it was captured by this camera, along
     with everything around it - or like it was generated or blended in by a
     different process?"

    STEP 10 IS A RESEARCH BASELINE. It is NOT a validated deepfake detector,
    it is NOT production-grade, and its accuracy has NOT been established.

    Accuracy, FAR, FRR, APCER, BPCER and ACER are all "Not evaluated", and
    that is meant literally - not "pending", not "approximately".

Three questions, three layers, never merged
    Identity      (Steps 3-5)  who does this face resemble?
    Liveness      (Step 9)     is this a flat presented object?
    Authenticity  (Step 10)    was this region generated rather than captured?

    They are different questions with different failure modes. A printed photo
    of a real person is a liveness problem and not an authenticity one. A
    face-swapped video of a real person moving is an authenticity problem and
    passes liveness easily. Collapsing them into one score would lose exactly
    the information a teacher needs to tell those cases apart.

Why there is no model here
    Checked before deciding, and recorded so the decision can be re-examined:

      - the official OpenCV model zoo carries 24 models and none of them is a
        forgery, authenticity or anti-spoofing detector, so the one source
        that already meets this project's provenance bar has nothing to offer;
      - the DFDC-winning ensembles need PyTorch and well over a gigabyte of
        weights, against a 65 ms frame budget;
      - the FaceForensics++ baselines are trained on a corpus that requires a
        signed EULA, and their weights are not freely redistributable;
      - the small models (MesoNet and friends) survive only as ONNX
        conversions on unvetted repositories with no published hash, which is
        precisely the "random downloaded checkpoint" this project refuses.

    And a point that outlives all of those: every one of them is trained on
    high-quality face-swap corpora, not on 640x480 webcam video. Their
    published numbers would not transfer, and there is no dataset here on
    which to re-measure them honestly.

    So this file contains no model, no weights and no new dependency. It is
    plain numpy, and it is labelled a baseline because that is what it is.

The physical idea the cues rest on
    A real camera produces ONE coherent image. The face and the wall behind it
    share a sensor, an exposure curve, a noise floor and a compressor. When
    the light changes, both change together.

    A face that was swapped or synthesised into the frame is produced by a
    different process from its own surroundings. The two become statistically
    DECOUPLED, and that decoupling is measurable without knowing anything
    about how the face was generated.

    Every cue below compares the face region against a ring of context just
    outside it. None of them looks at the face alone, because a face on its
    own tells you nothing about whether it belongs to the frame it is in.

    A. BOUNDARY SHARPNESS RATIO
       Fine detail inside the face against fine detail in the surrounding
       ring. Generators suppress high frequencies, so a synthesised face is
       often smoother than its own background.

    B. TEMPORAL COUPLING  (the primary cue)
       How strongly the face region's brightness tracks the context's
       brightness, frame to frame, across the window. Under one camera they
       move together: auto-exposure, lighting flicker and sensor noise hit
       both. A region generated per-frame drifts on its own schedule.

       This is the cue with the clearest physical argument, so a SUSPICIOUS
       verdict REQUIRES it. The texture cues may only support it.

    C. CHROMA OFFSET
       Colour difference between face and surrounding ring. Blending seams and
       colour-transfer errors show up here.

A deliberate weakness, recorded rather than hidden
    A true pixel-level residual between consecutive frames would be a stronger
    cue B than the statistical one used here. It is not used, because it would
    mean holding face PIXELS from one frame to the next - and the privacy
    guarantee that nothing retains an image is worth more than the extra
    sensitivity.

    What a track actually keeps is seven floating-point numbers per
    observation. There is nowhere to put a picture, and AuthenticitySample has
    __slots__ so there is no way to add one.

Failure behaviour
    Anything unmeasurable produces an UNUSABLE sample, never a guess. A window
    without enough usable samples is UNCERTAIN. An exception is UNCERTAIN.
    "Authenticity analysis unavailable" NEVER becomes AUTHENTIC.
"""

import os

import numpy as np

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
#
# UNKNOWN     nothing has been assessed for this track yet
# AUTHENTIC   the face region behaves like part of this camera's image
# SUSPICIOUS  the face region behaves as if produced separately from its
#             surroundings. NOT a claim that a deepfake was found.
# UNCERTAIN   not enough evidence, conflicting evidence, or no analysis
#
# INSUFFICIENT_EVIDENCE is a REASON, not a state. It is reported in the
# explanation string attached to an UNCERTAIN verdict, because a teacher needs
# to know the difference between "we looked and could not tell" and "we have
# not looked enough yet" - while everything downstream needs both to behave
# identically, which they do.
AUTHENTICITY_UNKNOWN = "UNKNOWN"
AUTHENTICITY_AUTHENTIC = "AUTHENTIC"
AUTHENTICITY_SUSPICIOUS = "SUSPICIOUS"
AUTHENTICITY_UNCERTAIN = "UNCERTAIN"

ALL_AUTHENTICITY_STATES = (AUTHENTICITY_UNKNOWN, AUTHENTICITY_AUTHENTIC,
                           AUTHENTICITY_SUSPICIOUS, AUTHENTICITY_UNCERTAIN)

REASON_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

# ---------------------------------------------------------------------------
# Operating modes
# ---------------------------------------------------------------------------
#
# Independent of the Step 9 liveness mode, and deliberately so: the two layers
# answer different questions and an operator may well trust one and not the
# other. Setting one never changes the other.
#
# off      no analysis. Zero cost, everything stays UNKNOWN.
# report   analyse and display, but let every segment reach the rule engine.
# enforce  analyse, and hold back evidence that is not trustworthy.
#
# OFF is the default, and that is a stronger position than Step 9 took for
# liveness. Two reasons, both deliberate:
#
#   1. Step 9's default must not change. The eligibility column is shared, so
#      a second gate that is on by default would start reporting every
#      student's evidence as unverified-source the moment Step 10 landed -
#      silently altering what Step 9 means. The brief for this step forbids
#      that, and it would be wrong even if it did not.
#
#   2. This baseline has never been measured against a single piece of real
#      manipulated video. Liveness at least had a physical argument and
#      real-camera anchoring behind its thresholds. A gate with no validation
#      at all, switched on by default, would mark almost everything
#      "unverified" and teach a teacher to ignore the column - which is worse
#      than not having it.
#
# Turning it on is one environment variable, and both report and enforce are
# fully tested.
AUTHENTICITY_MODE_OFF = "off"
AUTHENTICITY_MODE_REPORT = "report"
AUTHENTICITY_MODE_ENFORCE = "enforce"

ALL_AUTHENTICITY_MODES = (AUTHENTICITY_MODE_OFF, AUTHENTICITY_MODE_REPORT,
                          AUTHENTICITY_MODE_ENFORCE)

DEFAULT_AUTHENTICITY_MODE = AUTHENTICITY_MODE_OFF

AUTHENTICITY_MODE_ENV = "CLASSVISION_AUTHENTICITY_MODE"
AUTHENTICITY_INTERVAL_ENV = "CLASSVISION_AUTHENTICITY_INTERVAL"

# There is no model file and no weights, which is why this is a baseline
# version and not a model version. Every verdict carries it, so a result can
# always be traced back to the rules that produced it.
BASELINE_NAME = "classvision-authenticity-research-baseline"
BASELINE_VERSION = "0.10.0"

# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
#
# Analysed every Nth frame per track, not every frame - the same argument
# Step 5 makes for recognition and Step 9 for liveness, plus one specific to
# cue B: consecutive frames 66 ms apart barely differ, and a coupling measured
# across near-identical frames is dominated by quantisation noise. Spacing the
# samples out is what makes the correlation mean anything.
#
# 3 frames is ~200 ms at the measured 15 FPS.
DEFAULT_SAMPLE_INTERVAL_FRAMES = 3

# How many observations the window holds, and how many are needed before any
# verdict other than UNCERTAIN may be returned.
#
# THESE ARE LARGE, AND THE SIZE WAS MEASURED RATHER THAN CHOSEN.
#
# Cue B is a correlation over consecutive differences, and a correlation from
# a short series is mostly noise: its standard error is about 1/sqrt(n). That
# is not a theoretical worry. Measured on 400 real observations from this
# machine's webcam with a face in front of it, the SAME real video gives:
#
#     window   coupling seen across the run
#      8       -0.823 .. +0.988      unusable - a real face looks decoupled
#     14       -0.268 .. +0.940
#     20       +0.003 .. +0.929
#     30       +0.161 .. +0.924
#     40       +0.233 .. +0.887
#     60       +0.427 .. +0.892      stable
#
# The median sits at about +0.75 at every window length; only the SPREAD
# changes. So a short window does not measure a different thing badly, it
# measures the right thing too noisily - and at 8 samples it would have
# produced constant false SUSPICIOUS verdicts on ordinary students.
#
# 60 samples at a 3-frame interval is about 12 seconds of video before the
# first verdict. That is slow, and it is the honest price of an estimate that
# means anything. Until then the track is UNCERTAIN, which is the correct
# answer rather than a placeholder.
DEFAULT_WINDOW_SIZE = 80
DEFAULT_MINIMUM_SAMPLES = 60

# The face region is resampled to this square before the texture statistics,
# so a measurement does not depend on how close somebody is sitting.
ANALYSIS_SIZE = 64

# How far outside the face box the context ring extends, as a fraction of the
# box. 0.35 gives a ring wide enough to carry real texture without reaching
# across the room.
CONTEXT_MARGIN = 0.35

# The smallest face worth measuring. Below this both the face statistics and
# the ring around it are noise.
MINIMUM_FACE_PIXELS = 48

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING ANY NUMBER BELOW.
#
# These were MEASURED, not guessed - see ai/authenticity_test.py --calibrate,
# which prints the distributions they came from and is the reproducible source
# for every figure quoted in the README.
#
# COUPLING_AUTHENTIC_MIN is anchored on REAL webcam video from this machine.
# Over 400 observations with a face present, the weakest 60-sample coupling
# was +0.427 and the median was +0.75. 0.35 sits below the weakest real
# reading with margin, so ordinary students reach AUTHENTIC rather than
# sitting at UNCERTAIN forever.
#
# COUPLING_SUSPICIOUS_MAX is set FAR below zero, and the reason is the single
# most important limitation of this whole layer.
#
# Coupling only carries information when the scene's illumination actually
# CHANGES during the window. Two runs on this machine's webcam, same code:
#
#     scene with varying illumination   coupling ~ +0.75
#     static, evenly lit scene          coupling ~ +0.05
#
# In a static room, face and context are both dominated by independent sensor
# noise, so the correlation collapses towards zero - which is exactly where a
# genuinely decoupled region also sits. The cue degenerates to noise, and the
# two cases become indistinguishable.
#
# So the line cannot go at zero. At an 80-sample window an uncorrelated signal
# scatters with a standard deviation of about 0.11, and -0.35 is past three of
# those. Only genuinely ANTI-correlated evidence - a face whose brightness
# moves opposite to its surroundings, which no single camera produces - can
# reach it.
#
# The consequence is stated rather than hidden: in an ordinary, evenly lit
# classroom this baseline will report UNCERTAIN for most people and will
# rarely report SUSPICIOUS at all. It is built to miss manipulations rather
# than accuse students, because a false SUSPICIOUS harms a real person and a
# missed one leaves the system exactly as safe as it was before Step 10.
#
# The gap between the two thresholds is where "I cannot tell" lives, and it is
# wide on purpose.
#
# The SHARPNESS and CHROMA thresholds are NOT anchored on real manipulated
# video, because none was available. They are models of generator behaviour -
# suppressed high frequencies, colour steps at a blend seam - and that is
# exactly why neither may decide anything on its own.
#
# What NONE of this establishes: behaviour on a real face swap, a real neural
# reenactment, a real replayed video, or anything produced by somebody trying
# to defeat it. No manipulated video has ever been shown to this code.
COUPLING_AUTHENTIC_MIN = 0.35    # below the weakest real reading (+0.427)
COUPLING_SUSPICIOUS_MAX = -0.35  # past 3 SD of an uncorrelated signal
SHARPNESS_AUTHENTIC_MIN = 0.18   # face carries detail in proportion to context
SHARPNESS_SUSPICIOUS_MAX = 0.06  # face is far smoother than its surroundings
CHROMA_SUSPICIOUS_MIN = 26.0     # colour steps across the face boundary


class AuthenticityConfigError(ValueError):
    """Raised when the authenticity layer is configured with something unusable."""


def configured_mode(explicit=None):
    """Explicit argument first, then the environment, then the default."""
    value = explicit if explicit is not None else os.environ.get(
        AUTHENTICITY_MODE_ENV)

    if not value:
        return DEFAULT_AUTHENTICITY_MODE

    mode = str(value).strip().lower()
    if mode not in ALL_AUTHENTICITY_MODES:
        raise AuthenticityConfigError(
            "unknown authenticity mode %r - valid modes are %s"
            % (value, ", ".join(ALL_AUTHENTICITY_MODES)))

    return mode


def configured_interval(explicit=None):
    value = explicit if explicit is not None else os.environ.get(
        AUTHENTICITY_INTERVAL_ENV)

    # An explicit None/"" test, not a truthiness test: 0 is falsy AND an
    # invalid interval, and must be refused rather than silently defaulted.
    if value is None or value == "":
        return DEFAULT_SAMPLE_INTERVAL_FRAMES

    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise AuthenticityConfigError(
            "%s must be a whole number of frames, got %r"
            % (AUTHENTICITY_INTERVAL_ENV, value))

    if interval < 1:
        raise AuthenticityConfigError(
            "authenticity interval must be at least 1 frame, got %d" % interval)

    return interval


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

class AuthenticitySample:
    """
    One measurement of one track, at one moment.

    __slots__ is the privacy boundary as much as an optimisation: seven
    floating-point numbers and a flag. There is nowhere to put a face crop,
    and an attempt to attach one raises.
    """

    __slots__ = ("frame_index", "face_detail", "context_detail", "face_luma",
                 "context_luma", "chroma_offset", "usable", "reason")

    def __init__(self, frame_index, face_detail=0.0, context_detail=0.0,
                 face_luma=0.0, context_luma=0.0, chroma_offset=0.0,
                 usable=True, reason=None):
        self.frame_index = int(frame_index)
        self.face_detail = float(face_detail)
        self.context_detail = float(context_detail)
        self.face_luma = float(face_luma)
        self.context_luma = float(context_luma)
        self.chroma_offset = float(chroma_offset)
        self.usable = bool(usable)
        self.reason = reason

    def __repr__(self):
        return ("AuthenticitySample(frame=%d, face_detail=%.4f, usable=%s)"
                % (self.frame_index, self.face_detail, self.usable))


def _channels(region):
    """(luma, chroma_a, chroma_b) as plain float arrays, whatever came in."""
    array = np.asarray(region)

    if array.ndim == 2:
        luma = array.astype(np.float32)
        return luma, np.zeros_like(luma), np.zeros_like(luma)

    if array.ndim == 3 and array.shape[2] == 1:
        luma = array[:, :, 0].astype(np.float32)
        return luma, np.zeros_like(luma), np.zeros_like(luma)

    if array.ndim == 3 and array.shape[2] >= 3:
        # OpenCV hands over BGR.
        blue = array[:, :, 0].astype(np.float32)
        green = array[:, :, 1].astype(np.float32)
        red = array[:, :, 2].astype(np.float32)
        luma = 0.299 * red + 0.587 * green + 0.114 * blue
        # Two simple colour-difference channels. Enough to notice a colour
        # step across a blending seam without pulling in a colour-space
        # conversion library.
        return luma, red - luma, blue - luma

    raise ValueError("a frame region must be 2D or 3D, got shape %s"
                     % (array.shape,))


def _resample(plane, size=ANALYSIS_SIZE):
    """Nearest-neighbour resample to a fixed square, in numpy alone."""
    height, width = plane.shape
    rows = np.minimum((np.arange(size) * height) // size, height - 1)
    columns = np.minimum((np.arange(size) * width) // size, width - 1)
    return plane[rows][:, columns]


def detail_energy(plane):
    """
    High-frequency energy, normalised by the region's own intensity variance.

    The normalisation matters: without it this would mostly measure how
    brightly lit something is, and a brightness rule would be worthless here
    for the same reason it was worthless in Step 9.
    """
    if plane.size == 0 or min(plane.shape) < 3:
        return 0.0

    patch = _resample(plane)

    laplacian = (-4.0 * patch[1:-1, 1:-1]
                 + patch[:-2, 1:-1] + patch[2:, 1:-1]
                 + patch[1:-1, :-2] + patch[1:-1, 2:])

    variance = float(np.var(patch))
    if variance <= 1e-6:
        return 0.0

    return float(np.var(laplacian)) / variance


def _clip_box(detection, width, height):
    left = max(int(detection.x), 0)
    top = max(int(detection.y), 0)
    right = min(int(detection.x) + int(detection.width), width)
    bottom = min(int(detection.y) + int(detection.height), height)
    return left, top, right, bottom


class AuthenticityVerdict:
    """
    What the baseline concluded about one track, and why.

    There is no score field, deliberately. The cues are uncalibrated
    measurements; a number printed beside somebody's name would be read as a
    probability that this person is faked, which it emphatically is not.
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
    def is_authentic(self):
        return self.state == AUTHENTICITY_AUTHENTIC

    @property
    def is_suspicious(self):
        return self.state == AUTHENTICITY_SUSPICIOUS

    def to_dict(self):
        """Safe to log. No image, no crop, no vector, no raw cue value."""
        return {
            "track_id": self.track_id,
            "authenticity_state": self.state,
            "authenticity_reason": self.reason,
            "authenticity_observations": self.observations,
            "baseline_version": self.baseline_version,
        }

    def __repr__(self):
        return "AuthenticityVerdict(track=%d, %s, %d obs)" % (
            self.track_id, self.state, self.observations)


class AuthenticityDetector:
    """
    Turns tracked faces into per-track authenticity verdicts.

    Holds a short window of scalar measurements per track. It holds no images
    and no identities: a track id is the only thing it knows about anybody.
    """

    def __init__(self, window_size=DEFAULT_WINDOW_SIZE,
                 minimum_samples=DEFAULT_MINIMUM_SAMPLES,
                 coupling_authentic_min=COUPLING_AUTHENTIC_MIN,
                 coupling_suspicious_max=COUPLING_SUSPICIOUS_MAX,
                 sharpness_authentic_min=SHARPNESS_AUTHENTIC_MIN,
                 sharpness_suspicious_max=SHARPNESS_SUSPICIOUS_MAX,
                 chroma_suspicious_min=CHROMA_SUSPICIOUS_MIN):
        self.window_size = max(int(window_size), 4)
        self.minimum_samples = max(int(minimum_samples), 4)

        if self.minimum_samples > self.window_size:
            raise AuthenticityConfigError(
                "minimum_samples (%d) cannot exceed the window size (%d)"
                % (self.minimum_samples, self.window_size))

        self.coupling_authentic_min = float(coupling_authentic_min)
        self.coupling_suspicious_max = float(coupling_suspicious_max)
        self.sharpness_authentic_min = float(sharpness_authentic_min)
        self.sharpness_suspicious_max = float(sharpness_suspicious_max)
        self.chroma_suspicious_min = float(chroma_suspicious_min)

        if self.coupling_suspicious_max >= self.coupling_authentic_min:
            raise AuthenticityConfigError(
                "the coupling thresholds overlap (%r >= %r), so no "
                "observation could ever be UNCERTAIN"
                % (self.coupling_suspicious_max, self.coupling_authentic_min))

        if self.sharpness_suspicious_max >= self.sharpness_authentic_min:
            raise AuthenticityConfigError(
                "the sharpness thresholds overlap (%r >= %r)"
                % (self.sharpness_suspicious_max, self.sharpness_authentic_min))

        self._windows = {}
        self.samples_taken = 0
        self.failed_samples = 0

    # -- measurement -------------------------------------------------------

    def observe(self, track_id, frame, detection):
        """
        Measure one frame for one track and add it to that track's window.

        Never raises. Anything unmeasurable becomes an UNUSABLE sample, which
        is evidence of nothing rather than evidence of authenticity.
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
            # never stop a lesson, and must never be rounded up to AUTHENTIC.
            # The exception TYPE is kept because it is useful and carries no
            # data; the message is not, because it could contain a path.
            return AuthenticitySample(
                frame_index, usable=False,
                reason="not measurable (%s)" % type(error).__name__)

    def _measure_strict(self, frame, detection, frame_index):
        array = np.asarray(frame)
        if array.ndim not in (2, 3):
            raise ValueError("a frame must be 2D or 3D, got shape %s"
                             % (array.shape,))

        height, width = array.shape[0], array.shape[1]
        left, top, right, bottom = _clip_box(detection, width, height)

        if right - left < MINIMUM_FACE_PIXELS or \
                bottom - top < MINIMUM_FACE_PIXELS:
            return AuthenticitySample(
                frame_index, usable=False,
                reason="face region too small to compare against its context")

        # The context ring: the box grown outwards, with the face itself
        # excluded. Everything here is a view into the caller's array; nothing
        # is copied and nothing outlives this function.
        margin_x = int((right - left) * CONTEXT_MARGIN)
        margin_y = int((bottom - top) * CONTEXT_MARGIN)

        outer_left = max(left - margin_x, 0)
        outer_top = max(top - margin_y, 0)
        outer_right = min(right + margin_x, width)
        outer_bottom = min(bottom + margin_y, height)

        outer_area = (outer_right - outer_left) * (outer_bottom - outer_top)
        face_area = (right - left) * (bottom - top)

        if outer_area - face_area < face_area * 0.15:
            # The face fills the frame, so there is no context to compare it
            # against. Saying so is the honest answer; guessing is not.
            return AuthenticitySample(
                frame_index, usable=False,
                reason="no usable context around the face to compare against")

        face_luma, face_a, face_b = _channels(array[top:bottom, left:right])
        outer_luma, outer_a, outer_b = _channels(
            array[outer_top:outer_bottom, outer_left:outer_right])

        # Mask the face out of the outer block so the ring is context only.
        mask = np.ones(outer_luma.shape, dtype=bool)
        mask[top - outer_top:bottom - outer_top,
             left - outer_left:right - outer_left] = False

        if not mask.any():
            return AuthenticitySample(
                frame_index, usable=False,
                reason="no usable context around the face to compare against")

        context_luma_values = outer_luma[mask]

        chroma_offset = (
            abs(float(np.mean(face_a)) - float(np.mean(outer_a[mask])))
            + abs(float(np.mean(face_b)) - float(np.mean(outer_b[mask]))))

        return AuthenticitySample(
            frame_index,
            face_detail=detail_energy(face_luma),
            context_detail=detail_energy(outer_luma),
            face_luma=float(np.mean(face_luma)),
            context_luma=float(np.mean(context_luma_values)),
            chroma_offset=chroma_offset,
            usable=True)

    # -- assessment --------------------------------------------------------

    def assess(self, track_id):
        """
        The verdict for one track, from its whole window.

        Two cues must agree before anything other than UNCERTAIN is returned,
        and the temporal-coupling cue must be one of them.
        """
        window = self._windows.get(track_id, [])
        usable = [sample for sample in window if sample.usable]
        latest = window[-1].frame_index if window else -1

        if len(usable) < self.minimum_samples:
            if window and not usable:
                return AuthenticityVerdict(
                    track_id, AUTHENTICITY_UNCERTAIN,
                    "authenticity analysis is unavailable for this track (%s)"
                    % (window[-1].reason or "not measurable"),
                    len(window), len(usable), latest)

            return AuthenticityVerdict(
                track_id, AUTHENTICITY_UNCERTAIN,
                "%s: not enough observations yet (%d of %d needed)"
                % (REASON_INSUFFICIENT_EVIDENCE, len(usable),
                   self.minimum_samples),
                len(window), len(usable), latest)

        coupling = self._coupling(usable)
        sharpness = self._sharpness_ratio(usable)
        chroma = float(np.median([s.chroma_offset for s in usable]))

        if coupling is None:
            return AuthenticityVerdict(
                track_id, AUTHENTICITY_UNCERTAIN,
                "the face and its surroundings could not be compared over "
                "time", len(window), len(usable), latest)

        # Cue B is mandatory in both directions; the texture cues may only
        # support it. That is deliberate: coupling is the cue with a physical
        # argument behind it, while sharpness and chroma are models of
        # generator behaviour rather than measurements of real generators.
        # Letting texture decide alone would call every softly-lit student
        # suspicious, which is the worst error this layer can make.
        coupled = coupling >= self.coupling_authentic_min
        decoupled = coupling <= self.coupling_suspicious_max

        authentic_cues = []
        if coupled:
            authentic_cues.append(
                "the face and its surroundings change together, as one "
                "camera image does")
        if sharpness >= self.sharpness_authentic_min:
            authentic_cues.append(
                "the face carries detail in proportion to its surroundings")

        suspicious_cues = []
        if decoupled:
            suspicious_cues.append(
                "the face changes independently of its surroundings")
        if sharpness <= self.sharpness_suspicious_max:
            suspicious_cues.append(
                "the face is far smoother than everything around it")
        if chroma >= self.chroma_suspicious_min:
            suspicious_cues.append("colour steps across the face boundary")

        suspicious_agrees = decoupled and len(suspicious_cues) >= 2
        authentic_agrees = coupled and len(authentic_cues) >= 2

        if suspicious_agrees and not authentic_agrees:
            return AuthenticityVerdict(track_id, AUTHENTICITY_SUSPICIOUS,
                                       "; ".join(suspicious_cues[:2]),
                                       len(window), len(usable), latest)

        if authentic_agrees and not suspicious_agrees:
            return AuthenticityVerdict(track_id, AUTHENTICITY_AUTHENTIC,
                                       "; ".join(authentic_cues),
                                       len(window), len(usable), latest)

        if suspicious_agrees and authentic_agrees:
            return AuthenticityVerdict(
                track_id, AUTHENTICITY_UNCERTAIN,
                "the evidence disagrees with itself", len(window),
                len(usable), latest)

        return AuthenticityVerdict(
            track_id, AUTHENTICITY_UNCERTAIN,
            "no two measurements agree strongly enough to decide",
            len(window), len(usable), latest)

    def measurements(self, track_id):
        """
        The raw cue values for one track. For calibration and tests ONLY.

        Deliberately unreachable from any API route: these are uncalibrated
        numbers, and publishing one would invite somebody to read it as a
        probability that a particular person is fake.
        """
        usable = [s for s in self._windows.get(track_id, []) if s.usable]
        if not usable:
            return None

        return {
            "coupling": self._coupling(usable),
            "sharpness_ratio": self._sharpness_ratio(usable),
            "chroma_offset": float(np.median([s.chroma_offset for s in usable])),
            "usable_observations": len(usable),
        }

    def _coupling(self, usable):
        """
        Cue B: how strongly face brightness tracks context brightness.

        Pearson correlation of the frame-to-frame CHANGES, not of the levels.
        Correlating the levels would mostly report that a face is brighter
        than a wall, which is true of every real face and says nothing.

        Returns None when the window is too short or either side never moved -
        a static scene carries no coupling information, and inventing one from
        a zero-variance series would be noise amplified to a verdict.
        """
        if len(usable) < 4:
            return None

        face = np.diff(np.array([s.face_luma for s in usable], dtype=np.float64))
        context = np.diff(np.array([s.context_luma for s in usable],
                                   dtype=np.float64))

        if face.size < 3:
            return None

        face_spread = float(np.std(face))
        context_spread = float(np.std(context))

        # Below this, nothing moved enough to correlate. Real webcam noise
        # alone clears it comfortably.
        if face_spread < 1e-3 or context_spread < 1e-3:
            return None

        centred_face = face - face.mean()
        centred_context = context - context.mean()

        denominator = float(np.sqrt(np.sum(centred_face ** 2)
                                    * np.sum(centred_context ** 2)))
        if denominator <= 1e-9:
            return None

        return float(np.dot(centred_face, centred_context) / denominator)

    def _sharpness_ratio(self, usable):
        """Cue A: face detail against context detail."""
        ratios = []
        for sample in usable:
            if sample.context_detail > 1e-6:
                ratios.append(sample.face_detail / sample.context_detail)

        return float(np.median(ratios)) if ratios else 0.0

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
            "model": None,
            "trained": False,
            "evaluated": False,
        }
