"""
ClassVision AI - Live Session Service (Steps 8-9)
-------------------------------------------------
Starts, watches and finalises one live classroom session.

    Step 8 connects the existing pipeline to the existing dashboard. It adds
    no model, changes no threshold, and decides no attendance. The Step 6 rule
    engine remains the only thing that produces an attendance record.

    Step 9 adds one step in front of that engine: an eligibility filter that
    decides which presence evidence is trustworthy enough to count. It still
    decides no attendance, and it still never edits a rule.

Where this sits

    route  ->  THIS SERVICE  ->  live runtime + worker thread  ->  pipeline
                    |
                    +-------->  Step 6 repositories -> SQLite

Two threads, one rule about the database
    The worker thread owns the camera and the pipeline and NEVER opens a
    database connection. Every write happens on the request thread, inside
    one transaction, at /stop. That is what keeps SQLite out of a race
    without adding a queue, a lock hierarchy or a second connection policy.

    The cost is stated rather than hidden: presence evidence for a session
    that is killed mid-run (power cut, process kill) is lost, because it was
    never written. A crash in the WORKER is survivable - the runtime goes to
    FAILED, the camera is released, and /stop still finalises whatever
    evidence was gathered.

One runtime per session, and as many sessions as there are cameras
    A LiveSessionManager owns one camera, one lesson and one worker thread,
    and LiveSessionRegistry holds one manager per session id - so several
    classes run at once, each with its own pipeline, timeline and counts.
    Nothing is shared between them but the registry's lock.

    A second /start for a session that is ALREADY running is still refused
    with a conflict rather than quietly stealing its camera. What two
    sessions cannot do is share one physical device: the second worker to
    reach it fails, its runtime goes to FAILED with the camera's own reason,
    and reportable() keeps that classroom's panel on the console saying so -
    see its docstring. A failed camera is reported, never faked.

Finalisation order, which is not negotiable
    stop camera -> close timeline -> build segments -> mark the session
    COMPLETED -> run the rule engine -> write segments and decisions.

    ABSENT can only be produced for a COMPLETED session, so the session is
    completed BEFORE the engine runs. Doing it the other way round would give
    every expected student UNKNOWN forever.
"""

import threading
from datetime import datetime, timezone

from attendance_evidence import (
    authenticity_states_from_manager,
    evaluate_evidence,
    states_from_manager,
)
from attendance_rules import AttendancePolicy, AttendanceRuleEngine

# Statuses that record a student as having attended. Finalising a session
# again must never move a student OUT of this set - see
# _preserve_recorded_attendance. Kept in step with the analytics service's
# SATISFYING_STATUSES, plus the states a human has deliberately set.
ATTENDED_STATUSES = ("PRESENT", "LATE", "LEFT_EARLY", "PARTIAL",
                     "MANUAL_OVERRIDE")
from attendance_session import (
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    SESSION_SCHEDULED,
    AttendanceSession,
)
from live_camera import CameraConfig, CameraError
from live_session import (
    LIVE_COMPLETED,
    LIVE_FAILED,
    LIVE_IDLE,
    LiveRuntime,
    LiveSessionWorker,
)
from live_pipeline import LIVE_PRESENCE_NOTE
from authenticity_detector import AUTHENTICITY_MODE_OFF
from authenticity_detector import BASELINE_VERSION as AUTHENTICITY_BASELINE
from liveness_detector import BASELINE_VERSION, LIVENESS_MODE_OFF
from repositories import (
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
    from_iso,
)

# Shown with every presence response. The distinction it draws is the one that
# would do real harm if a teacher missed it.
LIVENESS_NOTE = (
    "Liveness is a RESEARCH PROTOTYPE and has not been evaluated against real "
    "presentation attacks. It is a separate fact from identity: a recognised "
    "student is not automatically live, and a live face is not automatically "
    "anybody. A track that is not confirmed live is held back for review - it "
    "is never a claim that the student was absent."
)

# Shown with every presence response when Step 10 is switched on. Longer than
# the liveness note because the thing it has to prevent - reading SUSPICIOUS
# as "a deepfake was detected" - is a bigger leap.
AUTHENTICITY_NOTE = (
    "Video authenticity is a RESEARCH BASELINE, not a deepfake detector. It "
    "has never been evaluated against real manipulated video, and no accuracy "
    "figure of any kind is claimed. SUSPICIOUS means the face region did not "
    "behave like the rest of this camera's image - it is NOT a finding that a "
    "deepfake was used, and it is NOT a claim that the student was absent. In "
    "an evenly lit, static room this layer will report UNCERTAIN for most "
    "people, which is the honest answer rather than a fault."
)

# How long /stop waits for the worker to notice the stop flag and finish its
# current frame. One frame is ~65 ms at the measured 15 FPS, so this is very
# generous; it exists so a wedged worker cannot hang the request forever.
WORKER_JOIN_TIMEOUT_SECONDS = 10.0

# Which session statuses a live run may be started against.
STARTABLE_STATUSES = (SESSION_SCHEDULED, SESSION_ACTIVE)


class LiveSessionError(Exception):
    """Base class. Every message here is safe to show a teacher."""


class LiveSessionNotFound(LiveSessionError):
    """The requested session does not exist."""


class LiveSessionConflict(LiveSessionError):
    """The request is valid but the runtime is in the wrong state for it."""


class LiveSessionUnavailable(LiveSessionError):
    """A resource the session needs - camera or model - is not available."""


def _utc_now():
    return datetime.now(timezone.utc)


def _default_camera_factory(config):
    from live_camera import open_camera_source
    return open_camera_source(config)


def _default_pipeline_factory(session_start, session_end, student_names):
    from live_pipeline import build_pipeline
    return build_pipeline(session_start=session_start,
                          session_end=session_end,
                          student_names=student_names)


class LiveSessionManager:
    """
    The one live session this process may run.

    The factories are attributes rather than imports so tests can drive the
    whole lifecycle with a fake camera and a fake pipeline, and never need a
    webcam or a model file.
    """

    def __init__(self, camera_factory=None, pipeline_factory=None):
        self._lock = threading.RLock()
        self.runtime = LiveRuntime()

        self.camera_factory = camera_factory or _default_camera_factory
        self.pipeline_factory = pipeline_factory or _default_pipeline_factory

        self._worker = None
        self._camera = None
        self._pipeline = None
        self._stop_event = None
        self._summary = None
        self._session_id = None

    # -- start -------------------------------------------------------------

    def start(self, database, session_id, camera_index=None, width=None,
              height=None):
        """
        Begin a live session, or refuse with a reason.

        The camera is opened on THIS thread, before the worker starts, so a
        missing camera is an immediate, clear error rather than a session that
        appears to run and silently fails a moment later.
        """
        with self._lock:
            if self.runtime.is_busy:
                raise LiveSessionConflict(
                    "A live session is already %s for %s. Stop it before "
                    "starting another."
                    % (self.runtime.state.lower(),
                       self.runtime.session_id or "another session"))

            row = self._load_session(database, session_id)

            if row["status"] not in STARTABLE_STATUSES:
                raise LiveSessionConflict(
                    "Session %s is %s. Only a %s or %s session can be run "
                    "live." % (session_id, row["status"], SESSION_SCHEDULED,
                               SESSION_ACTIVE))

            try:
                config = CameraConfig.from_environment(
                    index=camera_index, width=width, height=height)
            except ValueError as error:
                raise LiveSessionConflict(str(error))

            self.runtime.begin(session_id, class_id=row["class_id"],
                               camera_index=config.index)
            self._session_id = session_id
            self._summary = None

            try:
                self._pipeline = self._build_pipeline(database, row)
                self._camera = self.camera_factory(config)
            except CameraError as error:
                self._abandon(str(error))
                raise LiveSessionUnavailable(str(error))
            except Exception as error:                  # noqa: BLE001
                # A model that will not load is the other realistic failure.
                # The type name is safe; the message may contain a path.
                self._abandon(
                    "The live pipeline could not be started (%s). Check that "
                    "the detection and recognition models are downloaded."
                    % type(error).__name__)
                raise LiveSessionUnavailable(self.runtime.error)

            # The camera is open, so the lesson has genuinely begun. Only now
            # is the stored session moved to ACTIVE.
            try:
                self._activate_session(database, row)
            except Exception as error:                  # noqa: BLE001
                self._release_camera()
                self._abandon("The session could not be marked active (%s)."
                              % type(error).__name__)
                raise LiveSessionConflict(self.runtime.error)

            self._stop_event = threading.Event()
            self._worker = LiveSessionWorker(
                self.runtime, self._camera, self._pipeline, self._stop_event)

            # Publish the liveness configuration BEFORE the worker starts.
            # The worker only republishes every 15 frames, so without this the
            # first second of /status would report liveness as "off" while it
            # was in fact running - a status that is wrong about a safety
            # control is worse than no status at all.
            self.runtime.publish_liveness(self._pipeline.liveness_summary(), [])
            self.runtime.publish_authenticity(
                self._pipeline.authenticity_summary())

            self.runtime.mark_running()
            self._worker.start()

            return self.status()

    def _build_pipeline(self, database, row):
        names = {student["student_id"]: student["name"]
                 for student in StudentRepository(database).list_all()}

        return self.pipeline_factory(from_iso(row["start_time"]),
                                     from_iso(row["end_time"]), names)

    def _activate_session(self, database, row):
        if row["status"] == SESSION_ACTIVE:
            return

        session = _session_from_row(row)
        session.start()
        SessionRepository(database).set_status(session.session_id,
                                               SESSION_ACTIVE)

    def _abandon(self, message):
        """A start that failed: nothing is left running, and the state says so."""
        self.runtime.fail(message)
        self._pipeline = None
        self._camera = None
        self._worker = None
        self._stop_event = None

    def _release_camera(self):
        if self._camera is not None:
            self._camera.release()
            self.runtime.mark_camera_released()
            self._camera = None

    # -- status and presence ----------------------------------------------

    def status(self):
        """Safe operational status. Never an image, a score or a path."""
        snapshot = self.runtime.snapshot()
        snapshot["worker_alive"] = bool(self._worker and self._worker.is_alive())
        return snapshot

    def liveness(self):
        """
        What the liveness layer is doing right now.

        Counts and configuration only. No cue value is published: the cues are
        uncalibrated measurements, and a number presented next to a person's
        name would be read as a confidence about that person, which it is not.
        """
        return self.runtime.liveness_snapshot()

    def presence(self):
        """
        The live presence view, exactly as the worker last published it.

        Reading the published snapshot rather than the pipeline is what keeps
        the request thread away from objects the worker is mutating.
        """
        liveness = self.runtime.liveness_snapshot()
        authenticity = self.runtime.authenticity_snapshot()

        return {
            "session_id": self.runtime.session_id,
            "state": self.runtime.state,
            "students": self.runtime.presence(),
            "tracks": self.runtime.tracks(),
            "unknown_tracks": self.runtime.unknown_tracks,
            "recognized_students": self.runtime.recognized_students,
            "liveness_mode": liveness["liveness_mode"],
            "liveness_available": liveness["liveness_available"],
            "baseline_version": liveness["baseline_version"],
            "authenticity_mode": authenticity["authenticity_mode"],
            "authenticity_available": authenticity["authenticity_available"],
            "authenticity_baseline_version": authenticity[
                "authenticity_baseline_version"],
            "note": LIVE_PRESENCE_NOTE,
            "liveness_note": LIVENESS_NOTE,
            "authenticity_note": AUTHENTICITY_NOTE,
        }

    # -- stop --------------------------------------------------------------

    def stop(self, database):
        """
        Stop the camera and finalise attendance. Safe to call twice.

        A second call returns the SAME summary and writes nothing more, so a
        double-clicked button cannot finalise a session twice.
        """
        with self._lock:
            if self._summary is not None:
                summary = dict(self._summary)
                summary["already_finalized"] = True
                return summary

            if self.runtime.state == LIVE_IDLE or self._session_id is None:
                raise LiveSessionConflict(
                    "There is no live session to stop.")

            if self.runtime.state not in (LIVE_FAILED, LIVE_COMPLETED):
                self.runtime.mark_stopping()

            self._shutdown_worker()

            try:
                summary = self._finalize(database)
            except LiveSessionError:
                raise
            except Exception as error:                  # noqa: BLE001
                self.runtime.fail(
                    "The session stopped but attendance could not be "
                    "finalised (%s). No partial attendance was written."
                    % type(error).__name__)
                raise LiveSessionConflict(self.runtime.error)

            if self.runtime.state != LIVE_FAILED:
                self.runtime.mark_completed()

            summary["state"] = self.runtime.state
            self._summary = dict(summary)
            summary["already_finalized"] = False
            return summary

    def _shutdown_worker(self):
        """Stop the loop, wait for it, and make sure the camera is back."""
        if self._stop_event is not None:
            self._stop_event.set()

        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

            if worker.is_alive():
                # The thread is a daemon and the camera is still held by it.
                # Say so rather than pretending the stop was clean.
                self.runtime.fail(
                    "The camera worker did not stop within %d seconds. "
                    "Restart the application before starting another session."
                    % int(WORKER_JOIN_TIMEOUT_SECONDS))

        # Belt and braces: the worker releases the camera in its finally
        # block, and release() is safe to call twice.
        self._release_camera()
        self._worker = None
        self._stop_event = None

    def _finalize(self, database):
        """
        Presence evidence -> eligibility -> attendance, once.

        Everything that DECIDES attendance is ordinary Step 6 code. Step 8
        contributes the order and the single transaction; Step 9 contributes
        one filter in front of the rule engine:

            segments  ->  eligibility (Step 9)  ->  rule engine (Step 6)

        The filter only ever REMOVES evidence, and only in enforce mode. It
        never edits a segment, never shortens one, and never writes an
        attendance status of its own. In report mode - the default - the
        engine sees exactly what it saw in Step 8, and the blocking is
        reported instead of applied.

        Ending early
            Attendance is evaluated over the window that was actually
            OBSERVED, not the window that was scheduled. See
            _observed_window() for why - it is the difference between a
            student who sat through the whole lesson being marked PRESENT and
            being marked PARTIAL because the teacher pressed stop.
        """
        pipeline = self._pipeline
        sessions = SessionRepository(database)
        attendance = AttendanceRepository(database)
        students = StudentRepository(database)

        row = sessions.get(self._session_id)
        if row is None:
            raise LiveSessionNotFound(
                "Session %s no longer exists." % self._session_id)

        # The session's breaks, subtracted from both sides of the attendance
        # ratio. Read here rather than held in the runtime because a break is a
        # fact about the session, not about this process - finalising after a
        # restart must still exclude it.
        from backend.services.break_service import BreakService

        session = _session_from_row(
            row, excluded_intervals=BreakService(database).excluded_intervals(
                self._session_id))
        expected = list(row.get("expected_students") or [])

        if pipeline is not None:
            pipeline.close()
            segments = pipeline.segments(session_end=session.end_time)
        else:
            segments = []

        # A person can only have presence evidence stored if they exist as a
        # student. Anyone recognised but not on the roll produces no row -
        # the foreign key would refuse it, and inventing one would be worse.
        known = {student["student_id"] for student in students.list_all()}
        storable = [segment for segment in segments if segment.student_id in known]
        skipped = len(segments) - len(storable)

        completed_at = _utc_now()
        if session.status == SESSION_SCHEDULED:
            session.start()
        if session.status == SESSION_ACTIVE:
            session.complete(completed_at=completed_at)

        # The window attendance is MEASURED over. The session keeps its
        # scheduled end - history is not rewritten - but a presence ratio is
        # only meaningful over the time somebody was actually looking.
        evaluation, ended_early = self._observed_window(session, completed_at)

        # -- Step 9: which evidence is trustworthy enough to count? ------
        #
        # Note what is NOT filtered: the evidence STORED. Blocked presence is
        # still presence that was observed, and deleting the record of it
        # would destroy the very thing a teacher needs in order to review the
        # decision. Only what reaches the rule engine is filtered.
        evidence = evaluate_evidence(
            storable,
            states_from_manager(pipeline.liveness_manager if pipeline else None),
            mode=(pipeline.liveness_mode if pipeline else LIVENESS_MODE_OFF),
            baseline_version=BASELINE_VERSION,
            authenticity_states=(pipeline.authenticity_states()
                                 if pipeline else {}),
            authenticity_mode=(pipeline.authenticity_mode if pipeline
                               else AUTHENTICITY_MODE_OFF),
            authenticity_baseline_version=AUTHENTICITY_BASELINE)

        policy = AttendancePolicy(version=session.policy_version)
        engine = AttendanceRuleEngine(policy)
        # Both arguments are filtered, and for different reasons. The
        # segments, so untrusted presence cannot become attendance. The
        # students, so a student whose evidence was withheld is not handed to
        # an engine that would read "no evidence" as "never appeared".
        decisions = engine.evaluate_session(
            evaluation,
            evidence.segments_for_rules(storable),
            evidence.students_for_rules(expected))

        # A student who was already marked present is already marked present.
        # Finalising must not take that away from them.
        decisions, preserved = self._preserve_recorded_attendance(
            attendance, session, decisions)

        with database.transaction():
            attendance.save_segments(session.session_id, storable)
            sessions.set_status(session.session_id, session.status,
                                session.completed_at)
            for decision in decisions:
                attendance.save_decision(decision)

        counts = {}
        for decision in decisions:
            counts[decision.status] = counts.get(decision.status, 0) + 1

        return {
            "session_id": session.session_id,
            "session_status": session.status,
            "finalized": True,
            "already_finalized": False,
            "state": self.runtime.state,
            "policy_version": policy.version,
            "expected_students": len(expected),
            "students_evaluated": len(decisions),
            "segments_saved": len(storable),
            "segments_skipped_unenrolled": skipped,
            "counts": counts,
            "liveness": evidence.to_dict(),
            "elapsed_seconds": round(self.runtime.elapsed_seconds(), 1),
            "processed_count": self.runtime.processed_count,
            "ended_early": ended_early,
            "scheduled_end": session.end_time.isoformat(),
            "observed_end": evaluation.end_time.isoformat(),
            "attendance_preserved": preserved,
            "note": (
                "Attendance was calculated by the Step 6 rule engine using "
                "policy version %s, over the window that was actually "
                "observed (%s to %s). %s%s"
                % (policy.version,
                   evaluation.start_time.isoformat(),
                   evaluation.end_time.isoformat(),
                   ("The session was stopped before its scheduled end, so "
                    "presence is measured against the time the camera was "
                    "running rather than the full scheduled window. "
                    if ended_early else ""),
                   _liveness_note(evidence))),
        }

    @staticmethod
    def _observed_window(session, stopped_at):
        """
        The window attendance is measured over, and whether it was cut short.

        A presence RATIO is presence divided by the length of the session. If
        a lesson is scheduled 10:00-11:00 and the teacher presses stop at
        10:35, dividing by the full hour says a student who sat through every
        single second the camera was running was present 58% of the time -
        which, against a 75% policy, makes them PARTIAL. They did nothing
        wrong; nobody was looking for the last 25 minutes.

        So the denominator is the OBSERVED window: scheduled start to whenever
        the session actually stopped, when that is earlier than the scheduled
        end. Stopping late, or on time, changes nothing - the lesson is the
        lesson, and evidence after its scheduled end does not extend it.

        The session row keeps its scheduled start and end. This shortens what
        is MEASURED, never what was scheduled, so no historical timestamp is
        rewritten.

        Returns (session_to_evaluate, ended_early).
        """
        if stopped_at is None or stopped_at >= session.end_time:
            return session, False

        # A stop at or before the start leaves no observable window at all.
        # Evaluating over a zero-length session would divide by zero and is
        # refused by the Step 6 model anyway, so the scheduled window stands
        # and everybody is correctly absent - nothing was observed.
        if stopped_at <= session.start_time:
            return session, True

        shortened = AttendanceSession(
            session_id=session.session_id,
            class_id=session.class_id,
            start_time=session.start_time,
            end_time=stopped_at,
            timezone_name=session.timezone_name,
            status=session.status,
            policy_version=session.policy_version,
            created_at=session.created_at,
            completed_at=session.completed_at,
            expected_students=list(session.expected_students),
            # Carried through, and re-clipped to the shorter window by the
            # constructor: a session stopped during a break excludes only the
            # part of that break the lesson actually reached.
            excluded_intervals=session.excluded_intervals)

        return shortened, True

    @staticmethod
    def _preserve_recorded_attendance(attendance, session, decisions):
        """
        Never downgrade a student who was already recorded as attending.

        Attendance that has already been written is a historical record of
        something that was observed. Finalising a session again - a
        double-clicked Stop, a second process, a re-run after a crash - must
        not be able to turn it into an absence, because nothing about
        finalising constitutes evidence that the student was not there.

        Only DOWNGRADES are blocked, and only away from a status that says
        the student attended. An upgrade, a correction from ABSENT to
        PRESENT, and every first-time decision go through untouched.

        Manual overrides are already protected a layer below, in
        AttendanceRepository.save_decision.

        Returns (decisions, list of student ids whose recorded status was kept).
        """
        preserved = []
        kept = []

        for decision in decisions:
            existing = attendance.get(session.session_id, decision.student_id,
                                      decision.policy_version)

            if (existing is not None
                    and existing["status"] in ATTENDED_STATUSES
                    and decision.status not in ATTENDED_STATUSES):
                # Keep the recorded result. The new evidence is still saved as
                # segments, so a teacher reviewing the session sees both.
                preserved.append(decision.student_id)
                continue

            kept.append(decision)

        return kept, preserved

    # -- housekeeping ------------------------------------------------------

    def shutdown(self):
        """
        Release the camera on application shutdown.

        Deliberately does NOT finalise: an application being closed is not a
        teacher saying the lesson ended.
        """
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()

            worker = self._worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

            self._release_camera()
            self._worker = None
            self._stop_event = None

    def reset(self):
        """Return to IDLE, releasing anything held. Used between tests."""
        with self._lock:
            self.shutdown()
            self.runtime.reset()
            self._pipeline = None
            self._summary = None
            self._session_id = None

    @staticmethod
    def _load_session(database, session_id):
        row = SessionRepository(database).get(session_id)
        if row is None:
            raise LiveSessionNotFound("Session not found: %s" % session_id)
        return row


def _session_from_row(row, excluded_intervals=None):
    """
    Rebuild the Step 6 session object from its stored row.

    `excluded_intervals` carries the session's breaks. Without them the rule
    engine divides by the whole lesson, and ten minutes nobody was taught
    counts against every student in the room.
    """
    return AttendanceSession(
        session_id=row["session_id"],
        class_id=row["class_id"],
        start_time=from_iso(row["start_time"]),
        end_time=from_iso(row["end_time"]),
        policy_version=row["policy_version"],
        timezone_name=row["timezone"],
        status=row["status"],
        session_date=row["session_date"],
        created_at=from_iso(row["created_at"]),
        completed_at=from_iso(row["completed_at"]),
        expected_students=row.get("expected_students") or [],
        excluded_intervals=excluded_intervals)


# -- the single instance the API uses --------------------------------------

# How many names a GRID panel carries.
#
# The panel is a monitoring tile, not a register: an operator watching eight
# classrooms needs to see that the right class is being recognised, and the
# full list is one click away in the focused view. Six is enough to show that
# recognition is working and short enough that a tile stays a tile.
PANEL_ROSTER_LIMIT = 6


def roster_preview(manager, limit=None):
    """
    The students this session has verified: named, capped, and scoped to it.

    WHY THIS IS NOT A SECOND ENDPOINT

        Every panel needs its own PRESENT NOW list, and the obvious way is one
        /api/live/presence request per panel. With ten classrooms and a
        one-second poll that is ten requests a second, each reading a
        different manager's snapshot - the exact duplicate polling a real-time
        console cannot afford.

        So it rides along on the answer the panels already ask for. One
        request draws the whole grid, however many classrooms are in it.

    WHAT IT CARRIES

        A name, the two states already on the panel's telemetry, and the
        student id the rest of the interface keys off. No duration, no track
        id, no cue value, no embedding and no image - the focused view is
        where a full presence row belongs, and this is a tile.

        Read from the manager's PUBLISHED snapshot, like every other live
        view, so the request thread never touches an object the worker is
        mutating.
    """
    cap = PANEL_ROSTER_LIMIT if limit is None else limit
    rows = manager.runtime.presence()

    return [{
        "student_id": row.get("student_id"),
        "student_name": row.get("student_name"),
        "presence_state": row.get("presence_state"),
        "liveness_state": row.get("liveness_state"),
    } for row in rows[:cap]]


class LiveSessionRegistry:
    """
    One LiveSessionManager per live session.

    Each manager keeps its own camera, pipeline, worker and lock, so a second
    class running in another room is a second manager and nothing is shared
    between them. The registry only decides WHICH manager a request is about.

    Managers are kept after their session stops rather than discarded, so the
    summary a teacher reads after pressing Finish is still there when the page
    polls again. `forget` drops the finished ones.
    """

    def __init__(self, factory=None):
        self._factory = factory or LiveSessionManager
        self._managers = {}
        self._lock = threading.Lock()

    def manager_for(self, session_id, create=True):
        """The manager for one session, created on first use."""
        if not session_id:
            return None

        with self._lock:
            manager = self._managers.get(session_id)
            if manager is None and create:
                manager = self._factory()
                self._managers[session_id] = manager
            return manager

    def busy(self):
        """Every manager with a session actually running, session id keyed."""
        with self._lock:
            managers = dict(self._managers)

        return {session_id: manager
                for session_id, manager in managers.items()
                if manager.runtime.is_busy}

    def all_managers(self):
        with self._lock:
            return dict(self._managers)

    def current(self):
        """
        The manager a request with no session id is about.

        Historically there could only be one live session, so "the live
        session" was unambiguous and the single-session API said nothing about
        which one it meant. That API still works: this answers with the running
        manager when exactly one is running, the most recently touched one when
        several are, and an idle manager when none is - which is what the old
        global returned on a fresh process.
        """
        busy = self.busy()
        if len(busy) == 1:
            return next(iter(busy.values()))

        with self._lock:
            if busy:
                # Several are running. The last one registered is the one a
                # session-less call is most likely to mean, and a caller that
                # cares must name its session.
                for session_id in reversed(list(self._managers)):
                    if session_id in busy:
                        return self._managers[session_id]

            if self._managers:
                return next(reversed(list(self._managers.values())))

            manager = self._factory()
            self._managers["__idle__"] = manager
            return manager

    def reportable(self):
        """
        Every session the dashboard should still be showing a panel for.

        BUSY is not the right question here. `busy` means "holding a camera",
        which is what decides whether the devices can be probed - and a session
        whose camera DIED is not holding one.

        But a dead session must not vanish from the console. On a machine where
        two classes were pointed at one physical camera, the second start takes
        the device and the first one's worker fails; if the list only reported
        busy sessions, that classroom's panel would silently disappear and the
        operator would be told nothing. Reporting it as FAILED, with the
        runtime's own reason, is the honest answer - and it is what the panel's
        is-failed state and the summary bar's failed count are for.

        So: anything that has a session loaded and is not IDLE.
        """
        with self._lock:
            managers = dict(self._managers)

        return {session_id: manager
                for session_id, manager in managers.items()
                if manager.runtime.is_busy
                or manager.runtime.state == "FAILED"}

    def active(self, database=None):
        """
        Every session worth a panel, as the dashboard needs to draw it.

        Each entry is that manager's OWN status - never a merged figure across
        sessions. A count in one panel belongs to one classroom.

        Includes FAILED sessions: see reportable() for why a classroom whose
        camera died must appear as broken rather than disappear.

        Each entry also carries a SHORT roster, so every panel in the grid can
        name the students its own camera has verified. See roster_preview for
        why it is short and why it rides on this answer rather than having a
        request of its own.
        """
        entries = []

        for session_id, manager in self.reportable().items():
            status = manager.status()
            entry = status if isinstance(status, dict) else dict(status)
            entry["present_now"] = roster_preview(manager)
            entry["present_now_total"] = len(manager.runtime.presence())
            entry["present_now_limit"] = PANEL_ROSTER_LIMIT
            entries.append(entry)

        entries.sort(key=lambda entry: (entry.get("class_id") or "",
                                        entry.get("session_id") or ""))
        return entries

    def forget(self, session_id):
        """Drop a manager whose session is over. Refuses while it is busy."""
        with self._lock:
            manager = self._managers.get(session_id)
            if manager is None:
                return False
            if manager.runtime.is_busy:
                return False
            manager.reset()
            del self._managers[session_id]
            return True

    def shutdown_all(self):
        """Give every camera back. Called when the process is going away."""
        for manager in self.all_managers().values():
            try:
                manager.shutdown()
            except Exception:                            # noqa: BLE001
                pass

    def reset(self):
        for manager in self.all_managers().values():
            try:
                manager.reset()
            except Exception:                            # noqa: BLE001
                pass
        with self._lock:
            self._managers = {}


# -- the instances the API uses --------------------------------------------

_manager = None
_manager_lock = threading.Lock()
_registry = None


def get_live_registry():
    """The process-wide registry of live sessions."""
    global _registry
    with _manager_lock:
        if _registry is None:
            _registry = LiveSessionRegistry()
        return _registry


def get_live_manager(session_id=None):
    """
    The manager a request is about.

    An explicitly injected manager wins over everything - that is how the test
    suites put a fake runtime in place, and it keeps the single-session API
    behaving exactly as it did.

    Otherwise: the manager for the named session, or "the one that is running"
    when no session is named.
    """
    global _manager
    with _manager_lock:
        override = _manager

    if override is not None:
        return override

    registry = get_live_registry()
    if session_id:
        return registry.manager_for(session_id)
    return registry.current()


def reset_live_manager(manager=None):
    """
    Replace the process-wide manager override. For tests and shutdown.

    Returns the manager now in place. Also clears the registry, so a test that
    injects a fake runtime does not inherit sessions from the one before it.
    """
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.reset()
        _manager = manager

    get_live_registry().reset()

    with _manager_lock:
        return _manager


def _liveness_note(evidence):
    """One sentence about what liveness did to this session's evidence."""
    if (evidence.mode == LIVENESS_MODE_OFF
            and evidence.authenticity_mode == AUTHENTICITY_MODE_OFF):
        return ("Liveness and authenticity checking were both switched off "
                "for this session.")

    if not evidence.blocked_segments:
        return ("Liveness held back no evidence (mode: %s)." % evidence.mode)

    if evidence.enforced:
        note = ("Liveness held %d presence segment(s) back from the rule "
                "engine for review. That is not a claim that anybody was "
                "absent - the evidence is still stored."
                % len(evidence.blocked_segments))

        if evidence.withheld_students:
            note += (" %s received no attendance record for this session, "
                     "because the only evidence of them was evidence the "
                     "liveness check declined to trust. That is 'the system "
                     "cannot say', not 'they were absent'."
                     % ", ".join(evidence.withheld_students))

        return note

    return ("Liveness would have held %d presence segment(s) back, but the "
            "mode is 'report', so every segment was counted. Set "
            "CLASSVISION_LIVENESS_MODE=enforce to apply it."
            % len(evidence.blocked_segments))
