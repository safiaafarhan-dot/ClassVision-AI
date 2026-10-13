"""
ClassVision AI - Live Pipeline (Steps 8-10)
-------------------------------------------
One frame in, presence events out - and a safe live view of who is in the room.

    Step 8 adds no detector, no recogniser, no tracker and no rule. Every
    decision below is made by a module that already existed and is already
    tested. This file coordinates them and shapes what a dashboard may see.

    Step 9 adds ONE thing to this file: liveness runs here, after the Step 5
    pipeline, over the same frame and the same detections. It re-detects
    nothing, re-tracks nothing, and never touches an identity.

    Step 10 adds one more, on exactly the same terms: authenticity analysis,
    reading the same frame and the same tapped detections. There is still ONE
    camera, ONE detector call and ONE tracker per frame.

What it reuses, unchanged
    TrackingSession (Step 5)     detection -> tracking -> recognition only
                                 when the identity manager asks for it ->
                                 presence timeline
    IdentityManager (Step 5)     when to spend an embedding, and whether to
                                 believe the result. Identity confirmation
                                 stays entirely its decision.
    PresenceTimeline (Step 5)    the record of who was visible and when.
    segments_from_events (Step 6) the ONE definition of a presence segment.

Why TrackingSession is imported from ai/tracking_test.py
    That is where the Step 5 pipeline object lives, and it is already written
    to be driven by something other than the demo window - its docstring says
    so. Re-typing it here would create a second pipeline that could drift from
    the tested one, which is exactly the failure this project keeps avoiding.
    (ai/tracking_test.py itself already imports open_camera from
    ai/camera_test.py, so a module under ai/ importing another is the existing
    convention rather than a new one.)

The live view is provisional, and says so
    presence_view() derives durations with segments_from_events, the same
    function the rule engine is fed at finalisation - so the live figure and
    the final figure come from one definition. It is still PROVISIONAL: the
    session has not ended, nobody can be absent yet, and a student who is not
    currently visible has not left. Attendance appears only after Step 6 has
    run over a COMPLETED session.

Identity, liveness and authenticity are three facts, never one
    Recognition says WHO. Liveness says WHETHER THIS CAME FROM A PERSON
    rather than a held-up object. Authenticity says WHETHER THIS VIDEO CAME
    FROM THIS CAMERA rather than from a generator.

    This file keeps all three in separate objects, updates them from separate
    code paths, and reports them in separate columns:

        recognised  does NOT imply live
        live        does NOT imply recognised
        authentic   does NOT imply either
        suspicious  does NOT imply spoof

    None can set another, and tests assert every direction.

Never in this file
    No frame is stored. No face crop is produced. No embedding is read,
    logged or returned. Similarity scores stay inside the identity manager,
    and liveness cue values stay inside the liveness detector.
"""

from datetime import timezone

from attendance_evidence import (
    authenticity_eligibility,
    combine_eligibility,
    eligibility_for_state,
)
from attendance_rules import (
    DEFAULT_TEMPORARY_LOSS_GRACE_SECONDS,
    segments_from_events,
    summarize_presence,
)
from authenticity_detector import (
    AUTHENTICITY_MODE_OFF,
    AUTHENTICITY_UNCERTAIN,
    AUTHENTICITY_UNKNOWN,
    AuthenticityDetector,
)
from authenticity_detector import BASELINE_VERSION as AUTHENTICITY_BASELINE
from authenticity_detector import (
    configured_interval as authenticity_configured_interval,
)
from authenticity_detector import configured_mode as authenticity_configured_mode
from authenticity_stability import AuthenticityManager
from liveness_detector import (
    BASELINE_VERSION,
    LIVENESS_MODE_OFF,
    LIVENESS_UNCERTAIN,
    LIVENESS_UNKNOWN,
    LivenessDetector,
    configured_interval,
    configured_mode,
)
from liveness_stability import LivenessManager
from presence_timeline import (
    EVENT_TRACK_EXITED,
    STATE_EXITED,
    STATE_PRESENT,
    STATE_RECOGNIZED,
    STATE_REACQUIRED,
    STATE_TEMPORARILY_LOST,
)

# -- live student states ----------------------------------------------------
#
# Operational states, shown to a teacher while the lesson is running. None of
# them is an attendance status, and the distinction matters:
#
#   PRESENT            verified and visible right now
#   RECOGNIZED         verified, settling into the timeline
#   TEMPORARILY_LOST   verified, not visible right now, still inside the
#                      tracker's grace period. NOT absent.
#   SEEN_EARLIER       verified earlier in this session, not currently
#                      tracked. NOT absent - absence is a conclusion about a
#                      finished session, and this session has not finished.
#
# UNKNOWN and TRACKED describe tracks, not students, so they never appear in
# the per-student list. An unverified person is reported only as a count.
LIVE_PRESENT = "PRESENT"
LIVE_RECOGNIZED = "RECOGNIZED"
LIVE_TEMPORARILY_LOST = "TEMPORARILY_LOST"
LIVE_SEEN_EARLIER = "SEEN_EARLIER"
LIVE_TRACKED = "TRACKED"
LIVE_UNKNOWN = "UNKNOWN"

ALL_LIVE_PRESENCE_STATES = (LIVE_PRESENT, LIVE_RECOGNIZED,
                            LIVE_TEMPORARILY_LOST, LIVE_SEEN_EARLIER,
                            LIVE_TRACKED, LIVE_UNKNOWN)

IDENTITY_CONFIRMED_STATE = "CONFIRMED"

# How a Step 5 timeline state is reported to a teacher.
_TIMELINE_TO_LIVE = {
    STATE_PRESENT: LIVE_PRESENT,
    STATE_RECOGNIZED: LIVE_RECOGNIZED,
    STATE_REACQUIRED: LIVE_PRESENT,
    STATE_TEMPORARILY_LOST: LIVE_TEMPORARILY_LOST,
    STATE_EXITED: LIVE_SEEN_EARLIER,
}

LIVE_PRESENCE_NOTE = (
    "Provisional. These are live presence readings, not attendance. Nobody "
    "can be marked absent while a session is running, and a student who is "
    "not visible right now has not left. Attendance is calculated by the "
    "rule engine once the session is stopped and completed."
)


class _DetectionTap:
    """
    Remembers the detections the Step 5 pipeline just used.

    Liveness needs the frame AND the landmarks for each visible track, but
    TrackingSession.process() computes its detections locally and does not
    return them. Rather than detect a second time - which would double the
    most expensive stage in the pipeline - or edit Step 5 to hand them back,
    the detector itself is wrapped. TrackingSession calls detect() exactly as
    before and is not aware of this; the detections it used are simply also
    visible here, at no cost and with no change to Step 5.
    """

    __slots__ = ("detector", "last")

    def __init__(self, detector):
        self.detector = detector
        self.last = []

    def detect(self, frame):
        self.last = self.detector.detect(frame)
        return self.last

    def __getattr__(self, name):
        # Anything else the Step 5 pipeline asks of a detector still works.
        return getattr(self.detector, name)


class LivePipeline:
    """
    Wraps the Step 5 pipeline and exposes only what a dashboard may see.

    Owned by the worker thread. Nothing else calls process(), and the views
    below are read by the worker too - it publishes them into the runtime,
    which is what the API actually serves.
    """

    def __init__(self, detector, recognizer, session_start=None,
                 session_end=None, student_names=None, tracking_session=None,
                 grace_seconds=DEFAULT_TEMPORARY_LOSS_GRACE_SECONDS,
                 clock=None, liveness_mode=None, liveness_detector=None,
                 liveness_manager=None, liveness_interval=None,
                 authenticity_mode=None, authenticity_detector=None,
                 authenticity_manager=None, authenticity_interval=None):
        self.tap = _DetectionTap(detector)

        if tracking_session is None:
            # Imported here, not at module import time: tracking_test pulls in
            # OpenCV, and a test that drives this pipeline with fakes should
            # not pay for that unless it needs to.
            from tracking_test import TrackingSession
            tracking_session = TrackingSession(self.tap, recognizer)
        else:
            # A caller that built its own Step 5 session still gets liveness:
            # its detector is wrapped in place.
            self.tap.detector = tracking_session.detector
            tracking_session.detector = self.tap

        self.session = tracking_session
        self.session_start = session_start
        self.session_end = session_end
        self.student_names = dict(student_names or {})
        self.grace_seconds = float(grace_seconds)

        from live_session import utc_now
        self.clock = clock or utc_now

        # -- liveness (Step 9) --------------------------------------------
        #
        # An independent evidence source, deliberately not wired into
        # recognition. Nothing below can change an identity, and nothing in
        # the recognition path can change a liveness state.
        self.liveness_mode = configured_mode(liveness_mode)
        self.liveness_enabled = self.liveness_mode != LIVENESS_MODE_OFF

        if self.liveness_enabled:
            self.liveness = liveness_detector or LivenessDetector()
            self.liveness_manager = liveness_manager or LivenessManager(
                sample_interval=configured_interval(liveness_interval))
        else:
            self.liveness = None
            self.liveness_manager = None

        # Set once if the liveness layer itself fails. The session keeps
        # running; every track simply reports UNCERTAIN from then on, which is
        # the only safe meaning of "the check did not run".
        self.liveness_failure = None

        # -- authenticity (Step 10) ---------------------------------------
        #
        # A third independent evidence source. Its mode is read separately
        # from the liveness mode, so switching one on never switches the
        # other on.
        self.authenticity_mode = authenticity_configured_mode(authenticity_mode)
        self.authenticity_enabled = (
            self.authenticity_mode != AUTHENTICITY_MODE_OFF)

        if self.authenticity_enabled:
            self.authenticity = authenticity_detector or AuthenticityDetector()
            self.authenticity_manager = (
                authenticity_manager or AuthenticityManager(
                    sample_interval=authenticity_configured_interval(
                        authenticity_interval)))
        else:
            self.authenticity = None
            self.authenticity_manager = None

        self.authenticity_failure = None

        self.closed = False

    # -- convenience handles on the reused Step 5 objects -------------------

    @property
    def tracker(self):
        return self.session.tracker

    @property
    def identities(self):
        return self.session.identities

    @property
    def timeline(self):
        return self.session.timeline

    @property
    def frame_index(self):
        return self.session.frame_index

    # -- per frame ---------------------------------------------------------

    def process(self, frame):
        """
        Run one frame through detection, tracking, recognition and timeline,
        then through liveness.

        Liveness runs AFTER the Step 5 pipeline and changes nothing in it. It
        reads the same detections and the same tracks; it never re-detects,
        never re-tracks, and never touches an identity.

        Returns this frame's presence events. The frame itself is not kept.
        """
        _, events = self.session.process(frame)

        if self.liveness_enabled:
            self._check_liveness(frame, events)

        if self.authenticity_enabled:
            self._check_authenticity(frame, events)

        return events

    def _check_authenticity(self, frame, events):
        """
        Measure authenticity for the visible tracks that are due a check.

        Wrapped whole, for the same reason the liveness block is: a failure
        here must never stop a lesson, and must never be rounded up to
        AUTHENTIC. The failure is LATCHED, because recording an unavailable
        observation is not enough on its own - the stability layer correctly
        refuses to drop a confirmed state on one bad observation, which is
        right for a student who moved and wrong for a detector that has died.

        Step 9 needed exactly this correction after its audit. Step 10 was
        built with it rather than waiting to repeat the mistake.
        """
        try:
            succeeded = self._check_authenticity_strict(frame, events)

            # Clear the latch only when a check actually RAN and returned. A
            # frame on which no track was due a check proves nothing about
            # whether the layer works.
            if succeeded:
                self.authenticity_failure = None
        except Exception as error:                       # noqa: BLE001
            self.authenticity_failure = (
                "Authenticity analysis is unavailable (%s). Every track is "
                "reported UNCERTAIN until it recovers."
                % type(error).__name__)

            moment = self.clock()
            for track in self.tracker.tracks:
                if track.is_visible:
                    self.authenticity_manager.note_unavailable(
                        track.track_id, self.frame_index,
                        self.authenticity_failure, moment)

    def _check_authenticity_strict(self, frame, events):
        detections = {detection.as_tuple(): detection
                      for detection in self.tap.last}
        moment = self.clock()
        succeeded = 0

        for track in self.tracker.tracks:
            if not track.is_visible:
                continue

            if not self.authenticity_manager.should_check(track,
                                                          self.frame_index):
                continue

            detection = detections.get(track.box)
            if detection is None:
                # Running on prediction this frame, so there is no fresh image
                # to measure. That is not evidence of anything.
                self.authenticity_manager.note_unavailable(
                    track.track_id, self.frame_index,
                    "no detection matched this track in this frame", moment)
                continue

            self.authenticity.observe(track.track_id, frame, detection)
            verdict = self.authenticity.assess(track.track_id)
            self.authenticity_manager.update(track.track_id, verdict,
                                             self.frame_index, moment)
            succeeded += 1

        for event in events:
            if event.event_type == EVENT_TRACK_EXITED:
                self.authenticity.forget(event.track_id)
                self.authenticity_manager.forget(event.track_id)

        return succeeded

    # -- authenticity views ------------------------------------------------

    @property
    def authenticity_trustworthy(self):
        """
        May any authenticity verdict be believed right now?

        False while the layer is failing. A checker that cannot run cannot
        vouch for anybody, including somebody it confirmed a moment before it
        broke.
        """
        return self.authenticity_enabled and self.authenticity_failure is None

    def authenticity_state_for(self, track_id):
        if not self.authenticity_enabled:
            return AUTHENTICITY_UNKNOWN
        if not self.authenticity_trustworthy:
            return AUTHENTICITY_UNCERTAIN
        return self.authenticity_manager.state_for(track_id)

    def authenticity_states(self):
        """
        {track_id: (state, confirmed)} for every track this session has seen.

        What the eligibility layer consumes at finalisation, so a failed layer
        must report UNCERTAIN here too - otherwise a verdict reached before
        the failure would still be counted.
        """
        if not self.authenticity_enabled:
            return {}

        states = self.authenticity_manager.all_states()

        if not self.authenticity_trustworthy:
            return {track_id: (AUTHENTICITY_UNCERTAIN, False)
                    for track_id in states}

        return states

    def authenticity_summary(self):
        """Counts and configuration. No identities, no cue values."""
        if not self.authenticity_enabled:
            return {
                "authenticity_mode": self.authenticity_mode,
                "authenticity_baseline_version": AUTHENTICITY_BASELINE,
                "tracks": 0, "authentic": 0, "suspicious": 0, "uncertain": 0,
                "unknown": 0, "checks": 0,
                "authenticity_available": True,
            }

        summary = self.authenticity_manager.summary()

        if not self.authenticity_trustworthy:
            undecided = summary["tracks"]
            summary.update({"authentic": 0, "suspicious": 0, "unknown": 0,
                            "uncertain": undecided})

        summary["authenticity_mode"] = self.authenticity_mode
        summary["authenticity_baseline_version"] = AUTHENTICITY_BASELINE
        summary["authenticity_available"] = self.authenticity_failure is None
        return summary

    def _check_liveness(self, frame, events):
        """
        Measure liveness for the visible tracks that are due a check.

        Wrapped whole in a try/except on purpose. A liveness failure must
        never stop a lesson, and must never be rounded up to LIVE.

        Note what the except branch does NOT rely on. Recording an unavailable
        observation is not enough on its own: the stability layer deliberately
        refuses to tear down an already-confirmed state on one bad
        observation, which is correct for a student who briefly turned away
        and WRONG for a checker that has died. So the failure is also latched,
        and every view below reports UNCERTAIN while it is set - see
        liveness_trustworthy.
        """
        try:
            succeeded = self._check_liveness_strict(frame, events)

            # Clear the latch only when a check actually RAN and returned. A
            # frame on which no track happened to be due a check proves
            # nothing about whether the layer works, and must not be mistaken
            # for recovery.
            if succeeded:
                self.liveness_failure = None
        except Exception as error:                       # noqa: BLE001
            self.liveness_failure = (
                "The liveness check is unavailable (%s). Every track is "
                "reported UNCERTAIN until it recovers."
                % type(error).__name__)

            moment = self.clock()
            for track in self.tracker.tracks:
                if track.is_visible:
                    self.liveness_manager.note_unavailable(
                        track.track_id, self.frame_index,
                        self.liveness_failure, moment)

    def _check_liveness_strict(self, frame, events):
        detections = {detection.as_tuple(): detection
                      for detection in self.tap.last}
        moment = self.clock()
        succeeded = 0

        for track in self.tracker.tracks:
            if not track.is_visible:
                continue

            if not self.liveness_manager.should_check(track, self.frame_index):
                continue

            detection = detections.get(track.box)
            if detection is None:
                # The track is running on prediction this frame, so there is
                # no fresh face to measure. That is not evidence of anything.
                self.liveness_manager.note_unavailable(
                    track.track_id, self.frame_index,
                    "no detection matched this track in this frame", moment)
                continue

            self.liveness.observe(track.track_id, frame, detection)
            verdict = self.liveness.assess(track.track_id)
            self.liveness_manager.update(track.track_id, verdict,
                                         self.frame_index, moment)
            succeeded += 1

        # Release the runtime state of tracks the tracker has retired. The
        # verdict each of them reached is archived by the manager, because
        # finalisation runs long after every track has gone.
        for event in events:
            if event.event_type == EVENT_TRACK_EXITED:
                self.liveness.forget(event.track_id)
                self.liveness_manager.forget(event.track_id)

        return succeeded

    # -- liveness views ----------------------------------------------------

    @property
    def liveness_trustworthy(self):
        """
        May any liveness verdict be believed right now?

        False while the liveness layer is failing. A checker that cannot run
        cannot vouch for anybody - including somebody it confirmed a moment
        before it broke. Every view below asks this first, so there is one
        place that decides it rather than five.
        """
        return self.liveness_enabled and self.liveness_failure is None

    def liveness_state_for(self, track_id):
        if not self.liveness_enabled:
            return LIVENESS_UNKNOWN
        if not self.liveness_trustworthy:
            return LIVENESS_UNCERTAIN
        return self.liveness_manager.state_for(track_id)

    def liveness_states(self):
        """
        {track_id: (state, confirmed)} for every track this session has seen.

        This is what the eligibility layer consumes at finalisation, so a
        failed layer must report UNCERTAIN here too - otherwise a verdict
        reached before the failure would still be counted as attendance.

        The manager's records are left intact rather than erased: if the layer
        recovers, the evidence it had already gathered is still there.
        """
        if not self.liveness_enabled:
            return {}

        states = self.liveness_manager.all_states()

        if not self.liveness_trustworthy:
            return {track_id: (LIVENESS_UNCERTAIN, False) for track_id in states}

        return states

    def liveness_summary(self):
        """Counts and configuration. No identities, no cue values."""
        if not self.liveness_enabled:
            return {
                "liveness_mode": self.liveness_mode,
                "baseline_version": BASELINE_VERSION,
                "tracks": 0, "live": 0, "spoof": 0, "uncertain": 0,
                "unknown": 0, "checks": 0,
                "liveness_available": True,
            }

        summary = self.liveness_manager.summary()

        if not self.liveness_trustworthy:
            # Nothing is live while the checker is down. Reporting the last
            # known counts would be reporting a verdict nothing is standing
            # behind.
            undecided = summary["tracks"]
            summary.update({"live": 0, "spoof": 0, "unknown": 0,
                            "uncertain": undecided})

        summary["liveness_mode"] = self.liveness_mode
        summary["baseline_version"] = BASELINE_VERSION
        summary["liveness_available"] = self.liveness_failure is None
        return summary

    def tracks_view(self):
        """
        One row per live track: identity, liveness and presence, side by side.

        Deliberately shows all three as separate columns. A track that is
        recognised but not confirmed live, or confirmed live but recognised as
        nobody, is exactly the case this view exists to make visible.
        """
        rows = []

        for track in self.tracker.tracks:
            identity = self.identities.get(track.track_id)
            record = self.timeline.records.get(track.track_id)

            confirmed_identity = bool(identity and identity.is_confirmed)
            liveness_record = (self.liveness_manager.get(track.track_id)
                               if self.liveness_enabled else None)

            if self.liveness_enabled and not self.liveness_trustworthy:
                liveness_state = LIVENESS_UNCERTAIN
                liveness_confirmed = False
            else:
                liveness_state = (liveness_record.effective_state
                                  if liveness_record else LIVENESS_UNKNOWN)
                liveness_confirmed = bool(liveness_record
                                          and liveness_record.confirmed)

            if self.authenticity_enabled and not self.authenticity_trustworthy:
                authenticity_state = AUTHENTICITY_UNCERTAIN
                authenticity_confirmed = False
                authenticity_reason = self.authenticity_failure
            else:
                source_record = (
                    self.authenticity_manager.get(track.track_id)
                    if self.authenticity_enabled else None)
                authenticity_state = (source_record.effective_state
                                      if source_record
                                      else AUTHENTICITY_UNKNOWN)
                authenticity_confirmed = bool(source_record
                                              and source_record.confirmed)
                authenticity_reason = (source_record.reason
                                       if source_record else None)

            eligibility = combine_eligibility(
                eligibility_for_state(liveness_state, liveness_confirmed,
                                      self.liveness_mode),
                authenticity_eligibility(authenticity_state,
                                         authenticity_confirmed,
                                         self.authenticity_mode))

            rows.append({
                "track_id": track.track_id,
                "student_id": (identity.student_id
                               if confirmed_identity else None),
                "student_name": (self.student_names.get(identity.student_id)
                                 if confirmed_identity else None),
                "identity_state": (IDENTITY_CONFIRMED_STATE
                                   if confirmed_identity else LIVE_UNKNOWN),
                "liveness_state": liveness_state,
                "liveness_confirmed": liveness_confirmed,
                "liveness_reason": (self.liveness_failure
                                    if not self.liveness_trustworthy
                                    else (liveness_record.reason
                                          if liveness_record else None)),
                "authenticity_state": authenticity_state,
                "authenticity_confirmed": authenticity_confirmed,
                "authenticity_reason": authenticity_reason,
                "presence_state": _TIMELINE_TO_LIVE.get(
                    record.state if record else None,
                    LIVE_TRACKED if track.is_visible else LIVE_TEMPORARILY_LOST),
                "attendance_eligibility": eligibility,
                "visible": track.is_visible,
            })

        return sorted(rows, key=lambda row: row["track_id"])

    def overlay_view(self):
        """
        What the preview needs to draw: a box, and what is known about it.

        Deliberately separate from tracks_view(). That one is an API response
        and must never carry pixel coordinates - a box is a position in a
        frame, and the frame is not something the JSON API publishes. This one
        never leaves the process: it goes to ai/live_overlay.py, which draws
        it onto a copy of the frame the worker is already holding.

        No detection, recognition or liveness work happens here. Every value
        was computed during process() for this same frame.
        """
        by_track = {row["track_id"]: row for row in self.tracks_view()}
        rows = []

        for track in self.tracker.tracks:
            if not track.is_visible:
                continue

            row = dict(by_track.get(track.track_id, {}))
            row["track_id"] = track.track_id
            row["box"] = tuple(int(value) for value in track.box)
            rows.append(row)

        return rows

    def counts(self):
        """Cheap per-frame numbers for the runtime status. No identities."""
        visible = 0
        unknown = 0
        confirmed = set()

        for track in self.tracker.tracks:
            if not track.is_visible:
                continue
            visible += 1

            identity = self.identities.get(track.track_id)
            if identity is not None and identity.is_confirmed:
                confirmed.add(identity.student_id)
            else:
                unknown += 1

        liveness = self.liveness_summary()
        authenticity = self.authenticity_summary()

        return {
            "active_tracks": visible,
            "unknown_tracks": unknown,
            "recognized_students": len(confirmed),
            "recognition_calls": self.identities.recognition_calls,
            "live_tracks": liveness["live"],
            "spoof_tracks": liveness["spoof"],
            "uncertain_tracks": liveness["uncertain"],
            "liveness_checks": liveness["checks"],
            "liveness_available": liveness["liveness_available"],
            "authentic_tracks": authenticity["authentic"],
            "suspicious_tracks": authenticity["suspicious"],
            "unverified_source_tracks": authenticity["uncertain"],
            "authenticity_checks": authenticity["checks"],
            "authenticity_available": authenticity["authenticity_available"],
        }

    # -- the view the dashboard sees ---------------------------------------

    def presence_view(self, now=None):
        """
        One safe row per VERIFIED student, derived the same way attendance is.

        A track that has never been confirmed contributes nothing here, which
        is the same rule the attendance engine enforces: an unknown face is
        never anybody's presence.
        """
        moment = now or self.clock()

        segments = segments_from_events(
            self.timeline.events,
            grace_seconds=self.grace_seconds,
            session_end=moment)

        window_start = self.session_start
        window_end = self.session_end or moment

        states = self._states_by_student()
        liveness = self._liveness_by_student()
        authenticity = self._authenticity_by_student()

        rows = []
        for student_id in sorted({segment.student_id for segment in segments}):
            summary = summarize_presence(student_id, segments,
                                         window_start, window_end)

            state, confirmed = liveness.get(student_id,
                                            (LIVENESS_UNKNOWN, False))
            source_state, source_confirmed = authenticity.get(
                student_id, (AUTHENTICITY_UNKNOWN, False))

            rows.append({
                "student_id": student_id,
                "student_name": self.student_names.get(student_id),
                "identity_state": IDENTITY_CONFIRMED_STATE,
                "presence_state": states.get(student_id, LIVE_SEEN_EARLIER),
                "liveness_state": state,
                "liveness_confirmed": confirmed,
                "authenticity_state": source_state,
                "authenticity_confirmed": source_confirmed,
                "attendance_eligibility": combine_eligibility(
                    eligibility_for_state(state, confirmed,
                                          self.liveness_mode),
                    authenticity_eligibility(source_state, source_confirmed,
                                             self.authenticity_mode)),
                "first_seen": _iso(summary.first_seen),
                "last_seen": _iso(summary.last_seen),
                "duration_seconds": round(summary.present_seconds, 1),
                "segment_count": summary.segment_count,
            })

        return rows

    def _liveness_by_student(self):
        """
        Each verified student's liveness, from their most recent track.

        A student can hold several tracks across a session. The one that
        describes them now is the one most recently seen - the same rule the
        presence state uses, so the two columns can never describe different
        moments.

        Note what this does NOT do: it does not merge or average verdicts
        across a student's tracks. Liveness belongs to a track, and a
        finalisation decision is made per track, per segment.
        """
        if not self.liveness_enabled:
            return {}

        latest = {}
        for record in self.timeline.records.values():
            if not record.student_id:
                continue
            seen = latest.get(record.student_id)
            if seen is None or record.last_seen > seen.last_seen:
                latest[record.student_id] = record

        # liveness_states() is the masked accessor: while the layer is
        # failing it reports UNCERTAIN for everyone, so a student cannot keep
        # a green tick earned before the checker broke.
        states = self.liveness_states()
        default = (LIVENESS_UNCERTAIN if not self.liveness_trustworthy
                   else LIVENESS_UNKNOWN)
        return {student_id: states.get(record.track_id, (default, False))
                for student_id, record in latest.items()}

    def _authenticity_by_student(self):
        """
        Each verified student's authenticity, from their most recent track.

        The same rule the presence and liveness columns use, so the three can
        never describe different moments. Verdicts are NOT merged across a
        student's tracks: authenticity belongs to a track, and finalisation
        decides per track, per segment.
        """
        if not self.authenticity_enabled:
            return {}

        latest = {}
        for record in self.timeline.records.values():
            if not record.student_id:
                continue
            seen = latest.get(record.student_id)
            if seen is None or record.last_seen > seen.last_seen:
                latest[record.student_id] = record

        states = self.authenticity_states()
        default = (AUTHENTICITY_UNCERTAIN
                   if not self.authenticity_trustworthy
                   else AUTHENTICITY_UNKNOWN)
        return {student_id: states.get(record.track_id, (default, False))
                for student_id, record in latest.items()}

    def _states_by_student(self):
        """
        The current operational state of each verified student.

        A student can hold more than one track over a session - they left and
        came back, and the tracker gave them a new id. The most recently seen
        track is the one that describes where they are now.
        """
        latest = {}

        for record in self.timeline.records.values():
            if not record.student_id:
                continue

            seen = latest.get(record.student_id)
            if seen is None or record.last_seen > seen.last_seen:
                latest[record.student_id] = record

        return {student_id: _TIMELINE_TO_LIVE.get(record.state,
                                                  LIVE_RECOGNIZED)
                for student_id, record in latest.items()}

    def unknown_track_count(self):
        return self.counts()["unknown_tracks"]

    # -- finalisation ------------------------------------------------------

    def close(self):
        """
        End every open presence, once.

        Called on the stopping path so that anyone still on camera gets an
        exit event. Without it their segment would have no end.
        """
        if self.closed:
            return []

        self.closed = True
        return self.session.close()

    def segments(self, session_end=None):
        """
        The presence evidence for the whole session.

        This is the Step 6 bridge function, used exactly as the Step 6 tests
        use it - no live-specific variant, so live attendance and replayed
        attendance cannot disagree.
        """
        return segments_from_events(
            self.timeline.events,
            grace_seconds=self.grace_seconds,
            session_end=session_end or self.session_end or self.clock())

    def efficiency(self):
        """Recognition work actually done versus the naive every-face cost."""
        return self.identities.efficiency()

    def timings(self):
        """Per-stage frame timings collected by the Step 5 pipeline, in ms."""
        return {
            "detect_ms": list(self.session.detect_times),
            "track_ms": list(self.session.track_times),
            "recognize_ms": list(self.session.recognize_times),
            "total_ms": list(self.session.total_times),
        }


def _iso(moment):
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def build_pipeline(session_start=None, session_end=None, student_names=None,
                   detector=None, recognizer=None):
    """
    The default pipeline: YuNet detection, SFace recognition, Step 5 tracking.

    Both models are the ones Steps 2-4 already chose and measured. The
    recognition threshold is whatever face_recognizer decides - it is not
    passed, overridden or re-tuned here.
    """
    if detector is None:
        from face_detector import YuNetFaceDetector
        detector = YuNetFaceDetector()

    if recognizer is None:
        from face_recognizer import FaceRecognizer
        recognizer = FaceRecognizer()

    return LivePipeline(detector, recognizer,
                        session_start=session_start,
                        session_end=session_end,
                        student_names=student_names)
