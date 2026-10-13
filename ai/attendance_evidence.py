"""
ClassVision AI - Attendance Evidence Eligibility (Step 9)
---------------------------------------------------------
Decides which presence evidence is trustworthy enough to reach the attendance
rules. It does not decide attendance.

    Step 9 adds liveness as an evidence filter, and Step 10 adds
    authenticity as a second, independent one. ai/attendance_rules.py is NOT
    modified and remains the only thing that produces PRESENT, LATE, PARTIAL,
    LEFT_EARLY, ABSENT or UNKNOWN.

Where this sits

    recognition  ->  identity confirmed          (Step 5, unchanged)
    liveness     ->  liveness confirmed          (Step 9)
    authenticity ->  authenticity confirmed      (Step 10)
    presence     ->  presence segments           (Step 6, unchanged)
                          |
                          v
                 THIS LAYER: is the evidence trustworthy?
                          |
                          v
                 ai/attendance_rules.py          (Step 6, unchanged)

    The separation is the whole point. The rule engine already knows how to
    turn presence into attendance; what it cannot know is whether the presence
    it was handed came from a person or from a photograph. That question is
    answered here, before the engine ever sees the evidence, so the engine
    stays a pure function of segments and policy.

Three facts, never conflated
    A segment only exists at all if an identity was CONFIRMED - Step 6
    guarantees that, and Step 9 does not relax it. On top of that, this layer
    asks whether liveness was CONFIRMED LIVE for the track that produced the
    segment.

        recognised  does NOT imply live
        live        does NOT imply recognised
        authentic   does NOT imply either
        suspicious  does NOT imply spoof

    A track can be confirmed live and still belong to nobody the system
    knows; it produces no segment, so nothing here can turn it into
    attendance. A track can be confirmed as a student and be a photograph of
    that student; it produces a segment, and the liveness gate is what stops
    it. And a track can pass liveness comfortably - a moving, deforming,
    detailed face - while being a manipulated video of that movement, which
    is the case the authenticity gate exists for and the liveness gate cannot
    see.

    The two gates are evaluated separately and reported separately. A single
    combined "trust score" would be smaller code and would destroy exactly the
    information a teacher needs to tell a held-up photograph from a face
    swap.

What "blocked" means, and what it does not
    Blocked evidence does not reach the rule engine. It does NOT mean the
    student is absent.

    Getting that second sentence to be TRUE took more than saying it. Step 6
    marks an expected student with no evidence ABSENT once a session is
    completed - correctly, because normally "no evidence" means "never
    appeared". But a student whose evidence was withheld by Step 9 is not a
    student who never appeared: they were seen, and the system declined to
    trust what it saw. Letting them fall through to ABSENT would convert
    "we could not verify this" into "they were not here", which is a
    different and much stronger claim.

    So a student whose evidence is ENTIRELY withheld is also withheld from
    evaluation, and gets NO attendance record for that session rather than a
    confident one. "The system cannot say" is an honest outcome; Step 7's
    analytics already count sessions without a record, and the stop summary
    names every student this happened to so a teacher can review them.

    A student with SOME eligible evidence is evaluated normally on that
    evidence. Only the trustworthy part contributes, and only the untrusted
    part is dropped.

Modes
    off       no filtering at all. Step 8 behaviour, exactly.
    report    compute eligibility, report it, filter nothing.
    enforce   compute eligibility and filter.

    report is the default. This baseline has never been evaluated against a
    real presentation attack, so letting it silently delete a real student's
    attendance would trade a known harm for an unmeasured benefit.
"""

from authenticity_detector import (
    AUTHENTICITY_AUTHENTIC,
    AUTHENTICITY_MODE_ENFORCE,
    AUTHENTICITY_MODE_OFF,
    AUTHENTICITY_MODE_REPORT,
    AUTHENTICITY_SUSPICIOUS,
)
from liveness_detector import (
    LIVENESS_LIVE,
    LIVENESS_SPOOF,
    LIVENESS_MODE_ENFORCE,
    LIVENESS_MODE_OFF,
    LIVENESS_MODE_REPORT,
)

# Why a segment may or may not contribute.
ELIGIBLE = "ELIGIBLE"
BLOCKED_SPOOF = "BLOCKED_SPOOF"
BLOCKED_UNCERTAIN = "BLOCKED_UNCERTAIN"
NOT_CHECKED = "NOT_CHECKED"

# Step 10. Named separately from BLOCKED_SPOOF on purpose: "this looked like a
# held-up photograph" and "this looked like manipulated video" are different
# findings, and a teacher reviewing them needs to know which one fired.
BLOCKED_SUSPICIOUS = "BLOCKED_SUSPICIOUS"
BLOCKED_UNVERIFIED_SOURCE = "BLOCKED_UNVERIFIED_SOURCE"

ALL_ELIGIBILITY_STATES = (ELIGIBLE, BLOCKED_SPOOF, BLOCKED_UNCERTAIN,
                          BLOCKED_SUSPICIOUS, BLOCKED_UNVERIFIED_SOURCE,
                          NOT_CHECKED)

# Short sentences, written to be read by a teacher rather than a developer.
ELIGIBILITY_REASONS = {
    ELIGIBLE: "Identity and liveness were both confirmed for this track.",
    BLOCKED_SPOOF: ("This track showed presentation-attack evidence, so its "
                    "presence is held back for review. It is not a claim "
                    "that the student was absent."),
    BLOCKED_UNCERTAIN: ("Liveness could not be confirmed for this track, so "
                        "its presence is held back for review. It is not a "
                        "claim that the student was absent."),
    NOT_CHECKED: "Liveness checking is switched off.",
    BLOCKED_SUSPICIOUS: (
        "This track showed evidence that the video may not have come from "
        "this camera, so its presence is held back for review. It is not a "
        "finding that a deepfake was used, and it is not a claim that the "
        "student was absent."),
    BLOCKED_UNVERIFIED_SOURCE: (
        "Video authenticity could not be confirmed for this track, so its "
        "presence is held back for review. It is not a claim that the "
        "student was absent."),
}


def eligibility_for_state(liveness_state, confirmed=True,
                          mode=LIVENESS_MODE_REPORT):
    """
    Turn one track's liveness state into an eligibility verdict.

    Only a CONFIRMED LIVE track is eligible. Everything else is held back,
    including UNKNOWN - because "we never looked" and "we looked and it was
    fine" must not produce the same answer.
    """
    if mode == LIVENESS_MODE_OFF:
        return NOT_CHECKED

    if liveness_state == LIVENESS_SPOOF:
        return BLOCKED_SPOOF

    if liveness_state == LIVENESS_LIVE and confirmed:
        return ELIGIBLE

    # UNKNOWN, UNCERTAIN, or LIVE that was never confirmed.
    return BLOCKED_UNCERTAIN


def authenticity_eligibility(authenticity_state, confirmed=True,
                             mode=AUTHENTICITY_MODE_REPORT):
    """
    Turn one track's authenticity state into an eligibility verdict.

    Deliberately a separate function from eligibility_for_state, not an extra
    argument to it. The two gates answer different questions, fail in
    different ways, and are switched on and off independently; sharing one
    function would be the first step towards sharing one threshold.
    """
    if mode == AUTHENTICITY_MODE_OFF:
        return NOT_CHECKED

    if authenticity_state == AUTHENTICITY_SUSPICIOUS:
        return BLOCKED_SUSPICIOUS

    if authenticity_state == AUTHENTICITY_AUTHENTIC and confirmed:
        return ELIGIBLE

    # UNKNOWN, UNCERTAIN, or AUTHENTIC that was never confirmed.
    return BLOCKED_UNVERIFIED_SOURCE


def combine_eligibility(liveness_verdict, authenticity_verdict):
    """
    One verdict per segment, from two independent gates.

    A blocking result from either gate blocks, and the more specific finding
    wins when both fire - a track that looked like both a presentation attack
    and a manipulation is reported as the attack, because that is the
    narrower and more actionable claim.

    NOT_CHECKED means a gate is switched off and therefore has no opinion. It
    never blocks, and it never overrides the other gate's opinion either.
    """
    for blocking in (BLOCKED_SPOOF, BLOCKED_SUSPICIOUS, BLOCKED_UNCERTAIN,
                     BLOCKED_UNVERIFIED_SOURCE):
        if liveness_verdict == blocking or authenticity_verdict == blocking:
            return blocking

    if ELIGIBLE in (liveness_verdict, authenticity_verdict):
        return ELIGIBLE

    return NOT_CHECKED


def is_eligible(eligibility):
    """Only ELIGIBLE and NOT_CHECKED let evidence through."""
    return eligibility in (ELIGIBLE, NOT_CHECKED)


class EvidenceReport:
    """
    What the eligibility layer concluded for one session.

    Carries segments and counts. It carries no image, no crop, no embedding
    and no cue value, because none of those ever entered this layer.
    """

    __slots__ = ("mode", "eligible_segments", "blocked_segments",
                 "by_track", "by_student", "counts", "baseline_version",
                 "authenticity_mode", "authenticity_baseline_version",
                 "enforced_blocked")

    #: See "What blocked means" above: a student whose evidence was entirely
    #: withheld is withheld from evaluation too, rather than being marked
    #: absent on the strength of evidence nobody was allowed to use.

    def __init__(self, mode, eligible_segments, blocked_segments, by_track,
                 by_student, counts, baseline_version=None,
                 authenticity_mode=AUTHENTICITY_MODE_OFF,
                 authenticity_baseline_version=None,
                 enforced_blocked=None):
        self.mode = mode
        self.eligible_segments = eligible_segments
        self.blocked_segments = blocked_segments
        self.by_track = by_track
        self.by_student = by_student
        self.counts = counts
        self.baseline_version = baseline_version
        self.authenticity_mode = authenticity_mode
        self.authenticity_baseline_version = authenticity_baseline_version

        # The segments an ENFORCING gate blocked. Distinct from
        # blocked_segments, which is what the gates COLLECTIVELY think - see
        # segments_for_rules for why the difference matters.
        self.enforced_blocked = enforced_blocked or []

    @property
    def blocked_seconds(self):
        return round(sum(segment.duration_seconds
                         for segment in self.blocked_segments), 3)

    @property
    def enforced(self):
        """True when EITHER gate is set to enforce.

        Each gate decides independently whether it has an opinion; this says
        whether anything is acting on those opinions at all.
        """
        return (self.mode == LIVENESS_MODE_ENFORCE
                or self.authenticity_mode == AUTHENTICITY_MODE_ENFORCE)

    def segments_for_rules(self, all_segments):
        """
        What the attendance engine should actually be given.

        Only a gate that is ENFORCING may remove a segment. A gate in report
        mode has an opinion and no power, and that distinction has to survive
        the two gates being combined: without it, switching authenticity to
        enforce would silently start applying liveness's report-only opinion
        as well, which is not what an operator asked for and not what either
        mode is documented to do.
        """
        blocked = {id(segment) for segment in self.enforced_blocked}
        return [segment for segment in all_segments
                if id(segment) not in blocked]

    @property
    def withheld_students(self):
        """
        Students whose evidence was entirely held back BY AN ENFORCING GATE.

        These are the students who must NOT be evaluated - not because they
        were absent, but because the only thing the system saw of them is the
        thing it decided not to trust.

        Counted from the enforced set rather than the reported one, for the
        same reason segments_for_rules is: a gate in report mode must not
        remove anybody from evaluation.
        """
        if not self.enforced_blocked:
            return []

        blocked_seconds = {}
        for segment in self.enforced_blocked:
            blocked_seconds[segment.student_id] = True

        kept = {segment.student_id
                for segment in self.eligible_segments}
        kept |= {segment.student_id for segment in self.blocked_segments
                 if id(segment) not in {id(s) for s in self.enforced_blocked}}

        return sorted(student_id for student_id in blocked_seconds
                      if student_id not in kept)

    def students_for_rules(self, expected_students):
        """
        Which expected students the engine should decide.

        A student with no evidence at all is still evaluated - that is an
        ordinary absence and Step 6 has always handled it. A student whose
        evidence was withheld is not, because the engine would read the
        absence of evidence as evidence of absence.
        """
        withheld = set(self.withheld_students)
        return [student_id for student_id in expected_students
                if student_id not in withheld]

    def to_dict(self):
        """Safe summary for the API and the stop response."""
        return {
            "liveness_mode": self.mode,
            "baseline_version": self.baseline_version,
            "eligible_segments": len(self.eligible_segments),
            "blocked_segments": len(self.blocked_segments),
            "blocked_seconds": self.blocked_seconds,
            "blocked_students": sorted(
                student_id for student_id, entry in self.by_student.items()
                if entry["blocked_segments"]),
            "withheld_students": self.withheld_students,
            "counts": dict(self.counts),
            "enforced": self.enforced,
            "authenticity_mode": self.authenticity_mode,
            "authenticity_baseline_version": self.authenticity_baseline_version,
        }

    def __repr__(self):
        return "EvidenceReport(mode=%s, eligible=%d, blocked=%d)" % (
            self.mode, len(self.eligible_segments),
            len(self.blocked_segments))


def evaluate_evidence(segments, liveness_states=None,
                      mode=LIVENESS_MODE_REPORT, baseline_version=None,
                      authenticity_states=None,
                      authenticity_mode=AUTHENTICITY_MODE_OFF,
                      authenticity_baseline_version=None):
    """
    Sort presence segments into trustworthy and held-back.

    segments              Step 6 PresenceSegment objects. Each already belongs
                          to a CONFIRMED identity - unknown faces never
                          produce one.
    liveness_states       {track_id: (state, confirmed)} or {track_id: state}
    mode                  liveness gate: off / report / enforce
    authenticity_states   {track_id: (state, confirmed)} or {track_id: state}
    authenticity_mode     authenticity gate: off / report / enforce

    The authenticity arguments default to OFF so that every existing caller -
    and every Step 9 test - gets exactly the behaviour it had before Step 10.

    Returns an EvidenceReport. Nothing is mutated, and no segment is altered:
    a blocked segment is held back, never edited or shortened.
    """
    states = _normalise(liveness_states)
    authenticity = _normalise(authenticity_states, AUTHENTICITY_AUTHENTIC)

    eligible = []
    blocked = []
    enforced_blocked = []
    by_track = {}
    by_student = {}
    counts = {ELIGIBLE: 0, BLOCKED_SPOOF: 0, BLOCKED_UNCERTAIN: 0,
              BLOCKED_SUSPICIOUS: 0, BLOCKED_UNVERIFIED_SOURCE: 0,
              NOT_CHECKED: 0}

    for segment in segments:
        state, confirmed = states.get(segment.track_id, (None, False))
        liveness_verdict = eligibility_for_state(state, confirmed, mode)

        source_state, source_confirmed = authenticity.get(
            segment.track_id, (None, False))
        authenticity_verdict = authenticity_eligibility(
            source_state, source_confirmed, authenticity_mode)

        verdict = combine_eligibility(liveness_verdict, authenticity_verdict)

        # Which gate, if any, has the AUTHORITY to remove this segment.
        if (mode == LIVENESS_MODE_ENFORCE
                and not is_eligible(liveness_verdict)):
            enforced_blocked.append(segment)
        elif (authenticity_mode == AUTHENTICITY_MODE_ENFORCE
                and not is_eligible(authenticity_verdict)):
            enforced_blocked.append(segment)

        counts[verdict] = counts.get(verdict, 0) + 1

        if is_eligible(verdict):
            eligible.append(segment)
        else:
            blocked.append(segment)

        track_entry = by_track.setdefault(segment.track_id, {
            "track_id": segment.track_id,
            "liveness_state": state,
            "liveness_confirmed": bool(confirmed),
            "authenticity_state": source_state,
            "authenticity_confirmed": bool(source_confirmed),
            "eligibility": verdict,
            "segments": 0,
            "seconds": 0.0,
        })
        track_entry["segments"] += 1
        track_entry["seconds"] = round(
            track_entry["seconds"] + segment.duration_seconds, 3)

        student_entry = by_student.setdefault(segment.student_id, {
            "student_id": segment.student_id,
            "eligible_segments": 0,
            "blocked_segments": 0,
            "eligible_seconds": 0.0,
            "blocked_seconds": 0.0,
        })

        if is_eligible(verdict):
            student_entry["eligible_segments"] += 1
            student_entry["eligible_seconds"] = round(
                student_entry["eligible_seconds"] + segment.duration_seconds, 3)
        else:
            student_entry["blocked_segments"] += 1
            student_entry["blocked_seconds"] = round(
                student_entry["blocked_seconds"] + segment.duration_seconds, 3)

    return EvidenceReport(mode, eligible, blocked, by_track, by_student,
                          counts, baseline_version,
                          authenticity_mode=authenticity_mode,
                          authenticity_baseline_version=(
                              authenticity_baseline_version),
                          enforced_blocked=enforced_blocked)


def _normalise(raw_states, trusted_state=LIVENESS_LIVE):
    """Accept {track: state} or {track: (state, confirmed)}."""
    normalised = {}

    for track_id, value in (raw_states or {}).items():
        if isinstance(value, tuple):
            state, confirmed = value
        else:
            state, confirmed = value, value == trusted_state

        normalised[track_id] = (state, bool(confirmed))

    return normalised


def authenticity_states_from_manager(manager):
    """
    Read {track_id: (state, confirmed)} out of an AuthenticityManager.

    Separate from states_from_manager below, even though the bodies match, so
    that a future change to either manager cannot silently alter the other
    gate's input.
    """
    if manager is None:
        return {}

    return manager.all_states()


def states_from_manager(manager):
    """
    Read {track_id: (state, confirmed)} out of a LivenessManager.

    Uses all_states() rather than the live records, because finalisation runs
    after every track has exited. A verdict that was confirmed during the
    lesson must still count when the lesson is scored.
    """
    if manager is None:
        return {}

    return manager.all_states()
