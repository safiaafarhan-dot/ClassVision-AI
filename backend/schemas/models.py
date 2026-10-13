"""
ClassVision AI - API Response Schemas (Step 7)
----------------------------------------------
Explicit models for everything the API returns.

Why declare these at all when the services already build plain dictionaries:
the schema is a second, enforced boundary. A field that is not declared here
does not appear in a response, so biometric or internal data cannot leak
through a refactor. It also gives the OpenAPI documentation real types instead
of "object".

NOTHING in this file describes an embedding, an image, a frame, a file path, or
a credential - because none of those may ever cross this boundary.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(description="ok, degraded, or error")
    database: str = Field(description="ok or error")
    schema_state: str = Field(alias="schema",
                              description="ready or missing")

    model_config = {"populate_by_name": True}


class ErrorResponse(BaseModel):
    detail: str = Field(description="A short, safe description of the problem")


class ClassSummary(BaseModel):
    class_id: str
    session_count: int
    completed_sessions: int
    first_session: Optional[str] = None
    last_session: Optional[str] = None


class SessionSummary(BaseModel):
    session_id: str
    class_id: str
    session_date: str
    start_time: str
    end_time: str
    timezone: str
    status: str
    policy_version: str
    created_at: str
    completed_at: Optional[str] = None
    expected_students: Optional[int] = None


class SessionDetail(BaseModel):
    session_id: str
    class_id: str
    session_date: str
    start_time: str
    end_time: str
    timezone: str
    status: str
    policy_version: str
    created_at: str
    completed_at: Optional[str] = None
    expected_students: List[str] = []
    policy_versions_present: List[str] = []


class AttendanceRecord(BaseModel):
    attendance_id: int
    session_id: str
    student_id: str
    student_name: Optional[str] = None
    status: str
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    present_seconds: float
    session_seconds: float
    presence_ratio: float

    # The duration-based relaxation. Optional with defaults, so a client
    # written against the previous shape keeps validating and a stored row
    # from before the rule existed still serialises.
    missing_seconds: float = 0.0
    grace_seconds: float = 0.0
    grace_minutes: int = 0
    within_grace: bool = False

    segment_count: int
    loss_periods: int
    was_late: bool
    left_early: bool
    reason: str
    policy_version: str
    is_override: bool
    original_status: Optional[str] = None
    override_reason: Optional[str] = None
    overridden_by: Optional[str] = None
    overridden_at: Optional[str] = None
    session_date: Optional[str] = None
    class_id: Optional[str] = None
    session_status: Optional[str] = None


class PagedSessions(BaseModel):
    items: List[SessionSummary]
    total: int
    limit: int
    offset: int


class PagedAttendance(BaseModel):
    items: List[AttendanceRecord]
    total: int
    limit: int
    offset: int


class AttendanceSummary(BaseModel):
    """
    A student's attendance, DERIVED from the attendance tables on every read.

    Deliberately not stored on the student row. Attendance belongs to
    attendance_records, and a copy kept next to the registry would be a second
    version of a fact that already has an owner - wrong the moment a session
    is re-evaluated or an override is applied.
    """

    expected_sessions: int = Field(
        default=0,
        description="Completed sessions this student was expected in")
    recorded_sessions: int = Field(
        default=0, description="Sessions that produced a decision for them")
    sessions_attended: int = Field(
        default=0, description="Decisions of PRESENT, LATE or LEFT_EARLY")
    attendance_rate: Optional[float] = Field(
        default=None,
        description="None when nobody has expected this student yet - which "
                    "is not the same as 0%")
    average_presence_ratio: Optional[float] = None
    total_present_seconds: float = 0.0
    counts: Dict[str, int] = Field(default_factory=dict)


class Student(BaseModel):
    """
    One student, as the registry knows them.

    Since schema version 2 every identifying field is a SQLite column, so the
    dashboard, a DB Browser window and `SELECT * FROM students` all show the
    same thing. The embedding store answers exactly two further questions -
    does a face enrollment exist, and how many samples does it hold - and its
    vectors are never read on this path.
    """

    student_id: str
    name: str
    roll_number: str = ""
    class_name: str = ""
    section: str = ""

    # The current academic year, mirrored onto the student row from
    # student_academic_history. Defaulted, so a client written against the
    # previous shape keeps validating and a student with no term recorded
    # serialises as "" rather than failing.
    academic_year: str = ""

    status: str
    created_at: str
    updated_at: str

    # -- face enrollment ---------------------------------------------------
    #
    # A student can exist in the registry with no enrollment (registered, but
    # never sat in front of a camera), so `enrolled` is a real question with a
    # real answer rather than a formality.
    enrolled: bool = Field(
        default=False,
        description="Whether face samples exist for this student. Without "
                    "them the camera cannot recognise them.")
    sample_count: int = Field(
        default=0, description="How many face samples are stored. A count, "
                               "never the samples themselves.")
    enrolled_at: Optional[str] = None
    model_name: Optional[str] = None
    model_version: Optional[str] = None

    # -- parent/guardian contact -------------------------------------------
    #
    # Contact detail, and the only reason an attendance alert has anywhere to
    # go. The phone number and the address are returned to a SUPER_ADMIN and
    # REDACTED to None for anybody else - a teacher can see WHETHER a guardian
    # is contacted without being handed a class list of phone numbers.
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = Field(
        default=None,
        description="None means either no number is on file or the caller is "
                    "not permitted to see it. The two are deliberately "
                    "indistinguishable from outside.")
    parent_email: Optional[str] = None
    notification_enabled: bool = Field(
        default=True,
        description="Whether attendance alerts about this student are sent to "
                    "their guardian at all.")

    attendance: Optional[AttendanceSummary] = None


class PagedStudents(BaseModel):
    items: List[Student]
    total: int
    limit: int
    offset: int


class PresenceSegment(BaseModel):
    segment_id: int
    track_id: Optional[int] = None
    start_time: str
    end_time: str
    duration_seconds: float


class StudentPresence(BaseModel):
    student_id: str
    segments: List[PresenceSegment]
    segment_count: int
    total_seconds: float


class PresenceResponse(BaseModel):
    session_id: str
    session_start: Optional[str] = None
    session_end: Optional[str] = None
    students: List[StudentPresence]
    note: str


class OverviewResponse(BaseModel):
    has_data: bool = Field(
        description="False means an empty database, not a zero result")
    total_sessions: int
    completed_sessions: int
    total_students: int
    students_in_scope: int
    classes_in_scope: List[str]
    counts: Dict[str, int]
    attendance_rate: Optional[float] = Field(
        default=None,
        description="None when there is nothing to divide by - not zero")
    average_presence_ratio: Optional[float] = None
    sample_size: int
    policy_versions: List[str]
    has_multiple_policy_versions: bool
    filters: Dict[str, Optional[str]]


class SessionAnalytics(BaseModel):
    session_id: str
    class_id: str
    session_date: str
    status: str
    policy_version: str
    policy_versions_present: List[str]
    has_multiple_policy_versions: bool
    is_final: bool
    expected_students: int
    evaluated_students: int
    verified_students: int
    presence_segments: int
    counts: Dict[str, int]
    satisfying_count: int
    attendance_rate: Optional[float] = None
    average_presence_ratio: Optional[float] = None
    average_present_seconds: Optional[float] = None
    override_count: int
    sample_size: int


class StudentHistoryEntry(BaseModel):
    session_id: str
    class_id: str
    session_date: str
    status: Optional[str] = None
    presence_ratio: Optional[float] = None
    present_seconds: Optional[float] = None
    policy_version: Optional[str] = None
    is_override: bool = False
    original_status: Optional[str] = None
    reason: Optional[str] = None


class AttendanceTrend(BaseModel):
    """
    Whether recent attendance is better or worse than earlier attendance.

    direction is NO_DATA until there are enough evaluated sessions to split
    into two halves; the rates are null in that case rather than 0.0, so the
    profile says "not enough sessions" instead of showing a confident zero.
    """

    direction: str
    earlier_rate: Optional[float] = None
    recent_rate: Optional[float] = None
    change: Optional[float] = None
    sample_size: int
    note: str


class StudentAnalytics(BaseModel):
    student_id: str
    name: str
    status: str
    # Registry detail, so the performance profile can name the class a rate
    # was earned in without a second request. Blank when not recorded.
    roll_number: str = ""
    class_name: str = ""
    section: str = ""
    expected_sessions: int
    evaluated_sessions: int
    sessions_without_record: int
    counts: Dict[str, int]
    satisfying_count: int
    attendance_rate: Optional[float] = None
    late_rate: Optional[float] = None
    partial_rate: Optional[float] = None
    early_leave_rate: Optional[float] = None
    absence_rate: Optional[float] = None
    average_presence_ratio: Optional[float] = None
    average_present_seconds: Optional[float] = None
    history: List[StudentHistoryEntry]
    sample_size: int
    has_trend_data: bool
    # Sessions the student was expected at and did not satisfy: the ABSENT
    # count plus the ones that completed without ever being evaluated. The
    # second half is included because from a student's point of view an
    # unevaluated session is a session they got no credit for.
    absent_sessions: int = 0
    trend: Optional[AttendanceTrend] = None


class ClassSessionHistory(BaseModel):
    session_id: str
    session_date: str
    attendance_rate: Optional[float] = None
    average_presence_ratio: Optional[float] = None
    expected_students: int
    counts: Dict[str, int]


class ClassAnalytics(BaseModel):
    class_id: str
    completed_sessions: int
    student_count: int
    counts: Dict[str, int]
    attendance_rate: Optional[float] = None
    late_rate: Optional[float] = None
    partial_rate: Optional[float] = None
    early_leave_rate: Optional[float] = None
    absence_rate: Optional[float] = None
    average_presence_ratio: Optional[float] = None
    session_history: List[ClassSessionHistory]
    sample_size: int
    comparison_available: bool
    has_trend_data: bool


class OverrideRequest(BaseModel):
    status: str = Field(description="The corrected attendance status")
    reason: str = Field(min_length=3, max_length=500,
                        description="Why the status was changed")
    actor: Optional[str] = Field(
        default=None, max_length=120,
        description="Who made the change. Free text - Step 7 has no "
                    "authentication, so this is not verified.")
    policy_version: Optional[str] = None


# ---------------------------------------------------------------------------
# Live classroom session (Step 8)
# ---------------------------------------------------------------------------
#
# These describe a RUNNING session. Nothing here is an attendance result, and
# nothing here may carry a picture. The runtime deliberately has no field for
# a frame, a crop, an embedding, a similarity score or a model location, so
# there is no shape in which one could be returned.


class LiveStartRequest(BaseModel):
    session_id: str = Field(
        min_length=1, max_length=200,
        description="An existing SCHEDULED or ACTIVE session to run")
    camera_id: Optional[str] = Field(
        default=None, max_length=120,
        description="A REGISTERED device to run on. Its camera_index is used, "
                    "so a teacher picks a room rather than a number. Ignored "
                    "when camera_index is given explicitly.")
    camera_index: Optional[int] = Field(
        default=None, ge=0, le=64,
        description="Which camera to open. Defaults to the registered "
                    "device's index, then CLASSVISION_CAMERA_INDEX, then 0.")
    width: Optional[int] = Field(default=None, ge=160, le=4096)
    height: Optional[int] = Field(default=None, ge=120, le=4096)


class LiveStatusResponse(BaseModel):
    state: str = Field(
        description="IDLE, STARTING, RUNNING, STOPPING, COMPLETED or FAILED")
    session_id: Optional[str] = None
    class_id: Optional[str] = None
    camera: str = Field(
        description="disconnected, connected, released or error")
    camera_index: Optional[int] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    elapsed_seconds: float = 0.0
    fps: float = Field(default=0.0, description="Measured, not requested")
    processed_count: int = Field(
        default=0,
        description="Captures processed by the pipeline. A counter only - no "
                    "capture is stored, and none can be retrieved.")
    dropped_count: int = Field(
        default=0, description="Captures the camera failed to deliver")
    recognition_calls: int = Field(
        default=0,
        description="How often recognition actually ran, which is far less "
                    "often than once per capture")
    active_tracks: int = 0
    recognized_students: int = 0
    unknown_tracks: int = Field(
        default=0,
        description="People being tracked whose identity is not confirmed. "
                    "A count, never a person, and never attendance.")
    last_event_at: Optional[str] = None
    worker_alive: bool = False
    error: Optional[str] = Field(
        default=None,
        description="A short safe sentence. Never a traceback or a path.")

    # -- breaks (Step 14) --------------------------------------------------
    #
    # Carried here rather than on an endpoint of its own because this response
    # is already polled once a second. A separate break poll would be a second
    # clock to keep in step with this one.
    #
    # `remaining_seconds` is the SERVER's answer. The page counts down from it
    # for display and asks again rather than deciding for itself when ten
    # minutes are up - a browser clock that is two minutes out must not be able
    # to decide how much of a lesson counted towards attendance.
    # Scalars, deliberately. A nested object here would defeat the guarantee
    # that no field of this response can carry a captured image - see
    # test_live_responses_carry_no_capture_only_counts.
    on_break: bool = Field(
        default=False,
        description="True while a break is running. Students are NOT marked "
                    "absent for a break: the interval is excluded from both "
                    "sides of the attendance ratio.")
    break_remaining_seconds: float = Field(
        default=0.0,
        description="Seconds left of the running break, measured by the "
                    "SERVER. The page counts down from this for display and "
                    "asks again rather than deciding when ten minutes are up.")
    break_started_at: Optional[str] = None
    break_ends_at: Optional[str] = Field(
        default=None,
        description="When the break is due to end. Fixed by the server when "
                    "the break starts.")
    break_count: int = Field(
        default=0, description="Breaks this session has had, running or over.")
    break_excluded_seconds: float = Field(
        default=0.0,
        description="Total time excluded from this session's attendance by "
                    "breaks so far.")

    # -- liveness (Step 9) -------------------------------------------------
    #
    # Counts and configuration. No cue value is published: the Step 9 cues are
    # uncalibrated measurements, and a number shown beside a person would be
    # read as a confidence about that person, which it is not.
    liveness_mode: str = Field(
        default="off", description="off, report or enforce")
    liveness_available: bool = Field(
        default=True,
        description="False when the liveness layer itself failed. Every track "
                    "then reports UNCERTAIN - never LIVE.")
    baseline_version: Optional[str] = Field(
        default=None,
        description="Version of the liveness baseline. There is no trained "
                    "model and no weights file.")
    live_tracks: int = 0
    spoof_tracks: int = Field(
        default=0,
        description="Tracks showing presentation-attack evidence. A count, "
                    "never a person, and never an attendance decision.")
    uncertain_tracks: int = 0
    liveness_checks: int = 0

    # -- authenticity (Step 10) --------------------------------------------
    #
    # A third independent layer, reported separately from liveness because it
    # answers a different question. Counts only - no cue value is published,
    # for the same reason none is published for liveness.
    authenticity_mode: str = Field(
        default="off", description="off, report or enforce")
    authenticity_available: bool = Field(
        default=True,
        description="False when the authenticity layer itself failed. Every "
                    "track then reports UNCERTAIN - never AUTHENTIC.")
    authenticity_baseline_version: Optional[str] = Field(
        default=None,
        description="Version of the authenticity research baseline. There is "
                    "no trained model and no weights file.")
    authentic_tracks: int = 0
    suspicious_tracks: int = Field(
        default=0,
        description="Tracks whose video did not behave like this camera's "
                    "output. A research signal, NOT a finding that a deepfake "
                    "was used, and never an attendance decision.")
    unverified_source_tracks: int = 0
    authenticity_checks: int = 0


class LiveTrack(BaseModel):
    """
    One tracked person: identity, liveness and presence, side by side.

    The three are separate facts and are reported separately. A track can be
    CONFIRMED as a student and SPOOF; it can be LIVE and belong to nobody the
    system knows. Neither combination is contradictory, and neither is
    collapsed into a single verdict.
    """

    track_id: int
    student_id: Optional[str] = Field(
        default=None,
        description="Null unless the identity was confirmed. An unverified "
                    "person is never named or guessed.")
    student_name: Optional[str] = None
    identity_state: str = Field(description="CONFIRMED or UNKNOWN")
    liveness_state: str = Field(
        description="UNKNOWN, LIVE, SPOOF or UNCERTAIN")
    liveness_confirmed: bool = Field(
        default=False,
        description="Whether the state survived the temporal stability check")
    liveness_reason: Optional[str] = Field(
        default=None,
        description="A short sentence for a teacher. Never a cue value.")
    authenticity_state: str = Field(
        default="UNKNOWN",
        description="UNKNOWN, AUTHENTIC, SUSPICIOUS or UNCERTAIN. Independent "
                    "of both identity and liveness.")
    authenticity_confirmed: bool = False
    authenticity_reason: Optional[str] = Field(
        default=None,
        description="A short sentence for a teacher. Never a cue value.")
    presence_state: str
    attendance_eligibility: str = Field(
        description="ELIGIBLE, BLOCKED_SPOOF, BLOCKED_UNCERTAIN or "
                    "NOT_CHECKED. Blocked is never a claim of absence.")
    visible: bool = False


class LivePresenceStudent(BaseModel):
    student_id: str
    student_name: Optional[str] = None
    identity_state: str = Field(
        description="CONFIRMED. Only a confirmed identity appears here.")
    presence_state: str = Field(
        description="PRESENT, RECOGNIZED, TEMPORARILY_LOST or SEEN_EARLIER. "
                    "None of these is an attendance status, and none of them "
                    "means absent.")
    liveness_state: str = Field(
        default="UNKNOWN",
        description="UNKNOWN, LIVE, SPOOF or UNCERTAIN. Independent of "
                    "identity: being recognised does not make somebody live.")
    liveness_confirmed: bool = False
    authenticity_state: str = Field(
        default="UNKNOWN",
        description="UNKNOWN, AUTHENTIC, SUSPICIOUS or UNCERTAIN. Independent "
                    "of identity and of liveness.")
    authenticity_confirmed: bool = False
    attendance_eligibility: str = Field(
        default="NOT_CHECKED",
        description="Whether this student's evidence is trustworthy enough to "
                    "reach the attendance rules. Not an attendance status.")
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    duration_seconds: float = 0.0
    segment_count: int = 0


class LivePresenceResponse(BaseModel):
    session_id: Optional[str] = None
    state: str
    students: List[LivePresenceStudent] = []
    tracks: List[LiveTrack] = Field(
        default=[],
        description="Every tracked person, including those with no confirmed "
                    "identity. They are listed as UNKNOWN and produce no "
                    "attendance.")
    unknown_tracks: int = 0
    recognized_students: int = 0
    liveness_mode: str = "off"
    liveness_available: bool = True
    baseline_version: Optional[str] = None
    authenticity_mode: str = "off"
    authenticity_available: bool = True
    authenticity_baseline_version: Optional[str] = None
    note: str
    liveness_note: str = ""
    authenticity_note: str = ""


class LiveStopResponse(BaseModel):
    session_id: str
    session_status: str
    state: str
    finalized: bool
    already_finalized: bool = Field(
        description="True when this session had already been finalised, so "
                    "the call changed nothing")
    policy_version: str
    expected_students: int = 0
    students_evaluated: int = 0
    segments_saved: int = 0
    segments_skipped_unenrolled: int = 0
    counts: Dict[str, int] = {}
    liveness: Dict[str, object] = Field(
        default={},
        description="What the Step 9 eligibility layer did: how many presence "
                    "segments reached the rule engine, and how many were held "
                    "back for review. Held back is never a claim of absence, "
                    "and held-back evidence is still stored.")
    elapsed_seconds: float = 0.0
    processed_count: int = 0
    note: str


# ---------------------------------------------------------------------------
# Scheduling a session (Step 7 dashboard, added later)
# ---------------------------------------------------------------------------
#
# The dashboard could read sessions but never create one, which left the Live
# Classroom tab with nothing to run. This is the request body for the one new
# operation that fixes that.
#
# Times are ISO 8601 and MUST carry a timezone offset. The browser builds them
# from the teacher's local date and time and converts to UTC before sending, so
# no timezone library is needed on either side and there is never an ambiguous
# "09:00" in flight.


class SessionCreateRequest(BaseModel):
    class_id: str = Field(
        min_length=1, max_length=120,
        description="The class or course this session belongs to")
    start_time: str = Field(
        description="ISO 8601 with a timezone offset, e.g. "
                    "2026-09-24T09:00:00+00:00")
    end_time: str = Field(
        description="ISO 8601 with a timezone offset. Must be after the start.")
    expected_students: Optional[List[str]] = Field(
        default=None,
        description="Student ids expected in this session. Every id must "
                    "already exist. Absence is only meaningful against this "
                    "list, so it may not be empty - but it may be OMITTED, in "
                    "which case class_name (and section) below are used to "
                    "take the ACTIVE students from the register instead.")
    class_name: Optional[str] = Field(
        default=None, max_length=120,
        description="The registered class to take expected students from when "
                    "expected_students is omitted. Ignored when an explicit "
                    "list is given.")
    section: Optional[str] = Field(
        default=None, max_length=40,
        description="Narrows class_name to one section. Empty means every "
                    "section of that class. Recorded on the session row.")
    session_id: Optional[str] = Field(
        default=None, max_length=200,
        description="Optional. Generated from the class, date and start time "
                    "when omitted.")
    policy_version: Optional[str] = Field(
        default=None, max_length=50,
        description="Attendance policy version to record against this "
                    "session. Defaults to the engine's current version.")
    timezone: Optional[str] = Field(
        default=None, max_length=60,
        description="Display label only. All stored arithmetic is UTC.")


# ---------------------------------------------------------------------------
# Dashboard student enrollment
# ---------------------------------------------------------------------------
#
# Enrollment happens server-side against the webcam. The browser sends student
# details and receives counters; it never uploads or receives a frame, a face
# crop or an embedding vector, and there is no field below in which one could
# travel.


class ParentContactFields(BaseModel):
    """
    Guardian contact, shared by the create and edit forms.

    Every field optional, and omitted means "leave it alone" - so correcting a
    section cannot blank a phone number somebody typed last term. Writing any
    of these is a SUPER_ADMIN action, enforced in the route.
    """

    parent_name: Optional[str] = Field(default=None, max_length=120)
    parent_phone: Optional[str] = Field(default=None, max_length=40)
    parent_email: Optional[str] = Field(default=None, max_length=200)
    notification_enabled: Optional[bool] = Field(
        default=None,
        description="Switch attendance alerts about this student on or off. "
                    "Omitted leaves the current setting alone.")


class StudentFilterOptions(BaseModel):
    """
    What the student registry's filters should offer.

    Read from the registry rather than from a constant, so the lists are the
    school's own terminology and a year nobody is in never appears. Three
    plain lists of strings and nothing else: no counts, no ids, and nothing
    about any individual student.
    """

    years: List[str] = []
    classes: List[str] = []
    sections: List[str] = []


class StudentCreateRequest(BaseModel):
    student_id: str = Field(
        min_length=1, max_length=64,
        description="Stable identifier, shared by the registry, the face "
                    "enrollment, recognition results and attendance")
    name: str = Field(min_length=1, max_length=120)
    roll_number: Optional[str] = Field(default=None, max_length=40)
    class_name: Optional[str] = Field(default=None, max_length=120)
    section: Optional[str] = Field(default=None, max_length=40)
    academic_year: Optional[str] = Field(
        default=None, max_length=60,
        description="The academic year this student is currently in, as the "
                    "school writes it. Mirrors student_academic_history.")


class StudentRegisterRequest(StudentCreateRequest, ParentContactFields):
    """
    The Add Student form: registry fields plus guardian contact.

    Separate from StudentCreateRequest so EnrollmentStartRequest can extend
    that one without declaring contact fields. An endpoint that accepts a
    phone number and then ignores it is worse than one that does not accept it:
    the caller believes a number was recorded.
    """


class StudentUpdateRequest(ParentContactFields):
    """
    An edit. Every field optional; omitted means "leave it alone".

    student_id is absent on purpose. It is the key that attendance records,
    presence segments, expected-student rows and the face enrollment all hang
    from, and a field several tables reference is not one a form may rewrite.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    roll_number: Optional[str] = Field(default=None, max_length=40)
    class_name: Optional[str] = Field(default=None, max_length=120)
    academic_year: Optional[str] = Field(default=None, max_length=60)
    section: Optional[str] = Field(default=None, max_length=40)
    status: Optional[str] = Field(default=None, pattern="^(ACTIVE|INACTIVE)$")


class StudentDeleteResponse(BaseModel):
    student_id: str
    deleted: bool
    enrollment_removed: bool = Field(
        description="Whether face samples were removed with the student. A "
                    "vector file for a student who no longer exists would be "
                    "an orphaned biometric record.")
    history_removed: Dict[str, Any] = Field(
        default_factory=dict,
        description="What was destroyed alongside the student row")


class EnrollmentStartRequest(StudentCreateRequest):
    samples: Optional[int] = Field(
        default=None, ge=1, le=20,
        description="Face samples to capture. Defaults to the same number the "
                    "command-line tool uses.")
    camera_index: Optional[int] = Field(default=None, ge=0, le=64)
    replace: bool = Field(
        default=False,
        description="Replace an existing enrollment for this student id")


class EnrollmentStatusResponse(BaseModel):
    state: str = Field(
        description="IDLE, STARTING, CAPTURING, SAVING, COMPLETED, FAILED "
                    "or CANCELLED")
    student_id: Optional[str] = None
    name: Optional[str] = None
    captured: int = Field(default=0, description="Samples taken so far")
    required: int = Field(default=0, description="Samples needed")
    rejected: int = Field(
        default=0,
        description="Samples the quality gate refused. Nothing was stored "
                    "for them.")
    message: Optional[str] = Field(
        default=None, description="A short sentence for a teacher")
    error: Optional[str] = Field(
        default=None,
        description="A short safe sentence. Never a traceback or a path.")
    camera_index: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    worker_alive: bool = False
    samples_stored: int = 0
    registry_synced: bool = Field(
        default=False,
        description="Whether the student row reached the attendance database")


# ---------------------------------------------------------------------------
# Authentication and accounts.
#
# No model in this section has a password_hash field, and that is not an
# oversight. These shapes are what leave the server, and a hash that cannot be
# named cannot be serialised by accident.
# ---------------------------------------------------------------------------


class ClassAssignment(BaseModel):
    class_name: str = Field(max_length=120)
    section: str = Field(default="", max_length=40)


class UserAccount(BaseModel):
    """An account, as everything outside the auth service sees it."""

    user_id: int
    username: str
    display_name: str
    role: str = Field(description="SUPER_ADMIN or TEACHER")
    status: str = Field(description="ACTIVE or INACTIVE")
    created_at: str
    updated_at: str
    last_login_at: Optional[str] = None
    created_by: Optional[str] = None
    assignments: List[ClassAssignment] = Field(default_factory=list)
    photo_name: str = Field(
        default="",
        description="Filename of the staff portrait, or empty. Never a path, "
                    "and never the image itself.")
    can_view_all_students: bool = Field(
        default=False,
        description="Whether this teacher may READ every student rather than "
                    "only their assigned classes. Does not widen what they "
                    "may change.")


class AuthStateResponse(BaseModel):
    """
    Whether anybody can sign in, and who is signed in now.

    `configured` false means a fresh installation with no accounts: the API is
    open and the dashboard asks for the first Super Admin. It is the state
    every installation predating authentication is in, and it ends
    permanently the moment an account is created.
    """

    configured: bool
    authenticated: bool
    user: Optional[UserAccount] = None
    may_view_all_students: bool = Field(
        default=False,
        description="Whether this caller may read every student. The "
                    "dashboard uses it for wording only; the server applies "
                    "it.")
    is_super_admin: bool = False
    unrestricted: bool = Field(
        default=False,
        description="Whether this caller sees every class, or only assigned "
                    "ones")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class BootstrapRequest(BaseModel):
    """
    The first Super Admin. Accepted only while no account exists.

    There is no default administrator password anywhere in this project, and
    this endpoint is why there does not need to be one.
    """

    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=200)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=200)
    role: str = Field(default="TEACHER", pattern="^(SUPER_ADMIN|TEACHER)$")
    assignments: List[ClassAssignment] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1,
                                        max_length=120)
    role: Optional[str] = Field(default=None,
                                pattern="^(SUPER_ADMIN|TEACHER)$")
    status: Optional[str] = Field(default=None, pattern="^(ACTIVE|INACTIVE)$")
    password: Optional[str] = Field(default=None, min_length=10, max_length=200)
    assignments: Optional[List[ClassAssignment]] = None
    can_view_all_students: Optional[bool] = Field(
        default=None,
        description="Grant or withdraw sight of every student. Reading only - "
                    "class scope still governs what they may change.")


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class AuditEntry(BaseModel):
    event_id: int
    at: str
    actor: Optional[str] = None
    actor_role: Optional[str] = None
    action: str
    subject: Optional[str] = None
    detail: Optional[str] = None


class SimpleResult(BaseModel):
    ok: bool = True
    detail: Optional[str] = None
