"""
Session, attendance and presence endpoints (Step 7).

Every list endpoint is paginated and filtered server-side. The browser never
receives rows it is not going to display.

Two endpoints here change data, and only two: the manual attendance override,
and scheduling a session. Everything else is read-only.

Scheduling was added after Step 9, because the dashboard could read sessions
but never create one - which left the Live Classroom tab with nothing to run.
It creates a Step 6 AttendanceSession through the Step 6 repository. There is
no second session model anywhere in this project.
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from database import ATTENDANCE_STATUSES, SESSION_STATUSES

from backend.schemas.models import (
    AttendanceRecord,
    ClassSummary,
    OverrideRequest,
    PagedAttendance,
    PagedSessions,
    PresenceResponse,
    SessionCreateRequest,
    SessionDetail,
)
from backend.services.data_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SessionConflictError,
    SessionValidationError,
)
from backend.services.session_service import (
    SessionConflict,
    SessionError,
    SessionNotFound,
    SessionService,
)
from backend.services.state import get_data_service, get_database
from backend.services.authz import (
    require_class_scope,
    require_super_admin,
    require_user,
)

# Authentication is applied to the WHOLE router rather than endpoint by
# endpoint, so a route added later is protected by default instead of by
# somebody remembering. On an installation with no accounts yet, require_user
# lets everything through and the dashboard asks for the first Super Admin -
# see backend/services/authz.py for why that state exists at all.
router = APIRouter(prefix="/api", tags=["sessions"],
                   dependencies=[Depends(require_user)])

SORT_FIELDS = ("student_id", "name", "status", "present_seconds",
               "presence_ratio", "first_seen")


@router.get("/classes", response_model=list[ClassSummary],
            summary="Classes that have sessions")
def list_classes(principal=Depends(require_user)):
    """
    The classes a caller may see, which is what the filter dropdown offers.

    Scoped, because an unscoped list named every class in the school to a
    teacher assigned to one of them - and then the Sessions page refused to
    open any of the others, which is a worse experience as well as a leak.
    """
    return get_data_service().list_classes(principal=principal)


@router.get("/sessions", response_model=PagedSessions,
            summary="Attendance sessions, newest first")
def list_sessions(
        class_id: Optional[str] = Query(None, max_length=120),
        session_date: Optional[str] = Query(
            None, pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="ISO date, for example 2026-03-02"),
        status: Optional[str] = Query(None),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(0, ge=0),
        principal=Depends(require_user)):
    """
    Narrowed to the caller's own classes.

    The scope is applied in the service, before paging, so the total is the
    count of what this teacher may see rather than the count of the table.
    """
    if status is not None and status not in SESSION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Unknown session status. Valid values: %s"
                   % ", ".join(SESSION_STATUSES))

    return get_data_service().list_sessions(
        class_id=class_id, session_date=session_date, status=status,
        principal=principal, limit=limit, offset=offset)


@router.get("/sessions/expected-students",
            summary="Who would be expected for a class and section")
def expected_students_preview(
        class_name: str = Query(max_length=120),
        section: Optional[str] = Query(None, max_length=40),
        principal=Depends(require_user)):
    """
    The ACTIVE students in a class, before a session is created against it.

    So the teacher scheduling a lesson sees the expected count on the form
    rather than discovering after the fact that the class they picked has
    nobody active in it. `inactive_count` is reported separately, because
    "nobody is in MECH-A" and "seven people are in MECH-A and none of them is
    active" need different fixing.

    Declared ABOVE /sessions/{session_id} on purpose: FastAPI matches in
    declaration order, and registered after it this path would arrive as a
    session id of "expected-students".

    Narrowed to the caller's own students, so a teacher cannot enumerate
    another class's register through the scheduling form.
    """
    roster = get_data_service().class_roster(class_name, section)

    if not principal.unrestricted:
        roster["students"] = [student for student in roster["students"]
                              if principal.may_see_student(student)]
        roster["inactive"] = [student for student in roster["inactive"]
                              if principal.may_see_student(student)]
        roster["expected_students"] = [student["student_id"]
                                       for student in roster["students"]]
        roster["expected_count"] = len(roster["students"])
        roster["inactive_count"] = len(roster["inactive"])

    return roster


@router.post("/sessions", response_model=SessionDetail, status_code=201,
             summary="Schedule a new session",
             responses={400: {"description": "The request is not a usable "
                                             "session"},
                        409: {"description": "A session with that identifier "
                                             "already exists"}})
def create_session(payload: SessionCreateRequest):
    """
    Schedule a session so it can be run from the Live Classroom tab.

    The session is always created SCHEDULED. There is deliberately no way to
    post a COMPLETED one: attendance is produced by the Step 6 rule engine
    from presence evidence, and a session that never ran has none.

    Nothing is calculated here. Scheduling records an intention - who is
    expected, and when - and touches no attendance record.

    Expected students may be given explicitly, or taken from the register by
    naming `class_name` (and optionally `section`) and omitting the list. The
    second form takes the ACTIVE students in that class: an inactive student is
    not expected at a lesson, and counting one would manufacture an absence for
    somebody who has left. A class with no active students is refused rather
    than producing a session nobody is expected at, which could never produce
    attendance at all. See DataService.create_session.

    Times must carry a timezone offset. The dashboard converts the teacher's
    local date and time to UTC before sending, so an ambiguous "09:00" never
    reaches this layer.

    Note: there is no authentication, exactly as everywhere else in this
    dashboard. Anyone who can reach the port can schedule a session. That is
    the same local-development boundary Step 7 documented; it is not relaxed
    here, and it is not tightened here either.
    """
    try:
        return get_data_service().create_session(
            class_id=payload.class_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
            expected_students=payload.expected_students,
            session_id=payload.session_id,
            policy_version=payload.policy_version,
            timezone_name=payload.timezone,
            class_name=payload.class_name,
            section=payload.section)
    except SessionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except SessionValidationError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except ValueError as error:
        # The Step 6 model does its own checking. Its messages are written for
        # a developer, so they are not echoed - the shape of the problem is
        # enough, and a stack trace must never reach a browser.
        raise HTTPException(
            status_code=400,
            detail="The session could not be created: %s" % error)


@router.get("/sessions/{session_id}", response_model=SessionDetail,
            summary="One session",
            responses={404: {"description": "Session not found"}})
def get_session(session_id: str = Path(max_length=200),
                principal=Depends(require_user)):
    """
    One session, if it is in the caller's classes.

    A session outside them answers the same 404 as a session that does not
    exist, so the status code cannot be used to map the timetable.
    """
    session = get_data_service().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404,
                            detail="Session not found: %s" % session_id)
    require_class_scope(principal, session["class_id"])
    return session


@router.get("/sessions/{session_id}/attendance", response_model=PagedAttendance,
            summary="Attendance records for a session",
            responses={404: {"description": "Session not found"}})
def session_attendance(
        session_id: str = Path(max_length=200),
        status: Optional[str] = Query(None),
        policy_version: Optional[str] = Query(None, max_length=50),
        sort: str = Query("student_id"),
        descending: bool = Query(False),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(0, ge=0),
        principal=Depends(require_user)):
    """
    The register for one session, if it is in the caller's classes.

    Scoped exactly as the override endpoint below already was. Reading another
    class's register and correcting it are different acts, but they are
    different acts on the same data, and only one of the two used to be
    checked.
    """
    service = get_data_service()

    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404,
                            detail="Session not found: %s" % session_id)
    require_class_scope(principal, session["class_id"])

    if status is not None and status not in ATTENDANCE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Unknown attendance status. Valid values: %s"
                   % ", ".join(ATTENDANCE_STATUSES))

    if sort not in SORT_FIELDS:
        raise HTTPException(
            status_code=400,
            detail="Cannot sort by %r. Valid fields: %s"
                   % (sort, ", ".join(SORT_FIELDS)))

    return service.session_attendance(
        session_id, status=status, policy_version=policy_version,
        sort=sort, descending=descending, limit=limit, offset=offset)


@router.get("/sessions/{session_id}/presence", response_model=PresenceResponse,
            summary="Presence segments behind a session's attendance",
            responses={404: {"description": "Session not found"}})
def session_presence(
        session_id: str = Path(max_length=200),
        student_id: Optional[str] = Query(None, max_length=120),
        principal=Depends(require_user)):
    """
    The evidence a decision was derived from, for a session in the caller's
    classes.

    Returns time segments only. There is no image, crop, frame or embedding in
    this response, because none of that is stored.
    """
    service = get_data_service()

    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404,
                            detail="Session not found: %s" % session_id)
    require_class_scope(principal, session["class_id"])

    return service.session_presence(session_id, student_id)


@router.post("/sessions/{session_id}/attendance/{student_id}/override",
             response_model=AttendanceRecord,
             summary="Record a manual attendance correction",
             responses={404: {"description": "Attendance record not found"},
                        400: {"description": "Invalid status"}})
def override_attendance(
        payload: OverrideRequest,
        session_id: str = Path(max_length=200),
        student_id: str = Path(max_length=120),
        principal=Depends(require_user)):
    """
    Correct one student's attendance for one session. Teacher or Super Admin.

    Step 6 preserves the original computed status, the reason, the timestamp
    and the actor, so a correction ADDS to the record rather than replacing
    it. Nothing here bypasses that trail.

    Two things this endpoint used to get wrong, both fixed:

        The class scope was not applied, so any signed-in teacher could edit
        any session's register - including a class they cannot even see on
        the Sessions page. It is applied now, and a session outside the
        caller's classes answers 404 exactly as it does everywhere else.

        `actor` was free text taken from the request body, and the docstring
        said so: it recorded a claim, not an identity. It is now the
        SIGNED-IN username. A correction is the one thing in an attendance
        record a person is accountable for, and letting the browser name
        somebody else made the audit trail worth nothing.

        The body's `actor` is still accepted and still used on an
        unconfigured installation, which is what keeps the CLI and the
        pre-authentication test suites working unchanged.
    """
    if payload.status not in ATTENDANCE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Unknown attendance status. Valid values: %s"
                   % ", ".join(ATTENDANCE_STATUSES))

    service = _session_service()
    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))

    actor = principal.username or payload.actor

    try:
        return get_data_service().apply_override(
            session_id, student_id, payload.status, payload.reason,
            actor, payload.policy_version)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail="No attendance record for student %s in session %s"
                   % (student_id, session_id))


@router.delete("/sessions/{session_id}/attendance/{student_id}/override",
               response_model=AttendanceRecord,
               summary="Undo a manual correction",
               responses={404: {"description": "Attendance record not found"}})
def clear_override(session_id: str = Path(max_length=200),
                   student_id: str = Path(max_length=120),
                   policy_version: Optional[str] = Query(None, max_length=50),
                   principal=Depends(require_user)):
    """
    Undo a manual correction, putting the computed status back.

    Scoped exactly as the override is: a session outside the caller's classes
    answers 404. Undoing a correction is as much a change to the register as
    making one.
    """
    service = _session_service()
    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))

    record = get_data_service().clear_override(session_id, student_id,
                                               policy_version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No attendance record for student %s in session %s"
                   % (student_id, session_id))
    return record


# ---------------------------------------------------------------------------
# The rest of a session's life: correcting it, cancelling it, and - only when
# it would cost nothing - deleting it.
#
# The single question every one of these turns on is whether the session has
# produced attendance yet. Before that it is a plan and freely editable; after
# that it is a record of something that was observed, and changing the plan it
# hung from does not change what happened.
# ---------------------------------------------------------------------------


def _session_service():
    return SessionService(get_database())


def _actor(principal):
    return principal.username or "unauthenticated"


@router.get("/sessions/{session_id}/details",
            summary="Everything about one session",
            responses={404: {"description": "Session not found"}})
def session_details(session_id: str = Path(max_length=200),
                    principal=Depends(require_user)):
    """
    Session, academic context, attendance summary, evidence and audit history.

    Includes `available_actions`, computed on the server, so the dashboard
    offers exactly the operations the API will actually accept rather than an
    End Session button on a session that finished last week.
    """
    try:
        details = _session_service().details(session_id)
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))

    require_class_scope(principal, details["class_id"])
    return details


@router.patch("/sessions/{session_id}", summary="Correct a session",
              responses={400: {"description": "Not a usable change"},
                         404: {"description": "Session not found"},
                         409: {"description": "It already has attendance"}})
def edit_session(payload: dict = Body(...),
                 session_id: str = Path(max_length=200),
                 confirm_correction: bool = Query(
                     False,
                     description="Required to change the date or times of a "
                                 "session that already has attendance. The "
                                 "existing records are kept exactly as they "
                                 "are and the session is flagged for review."),
                 principal=Depends(require_user)):
    """
    Fix a session that was created wrongly.

    Free while it has produced no evidence. Once it has, changing the window -
    the date, the times, the timezone - needs an explicit correction, and even
    then not one attendance record is recomputed. Re-deriving a past register
    from an edited plan would be inventing a different lesson.
    """
    service = _session_service()

    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
        return service.edit(session_id, payload, actor=_actor(principal),
                            confirm_correction=confirm_correction)
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except SessionConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except SessionError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/sessions/{session_id}/cancel", summary="Cancel a session",
             responses={404: {"description": "Session not found"},
                        409: {"description": "It has already finished"}})
def cancel_session(payload: dict = Body(None),
                   session_id: str = Path(max_length=200),
                   principal=Depends(require_user)):
    """
    Retire a session without destroying anything.

    The row stays, its attendance stays, and it stops behaving like a lesson.
    This is the answer to "I created that by mistake" - not deletion, which
    would take the register with it.
    """
    service = _session_service()

    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
        return service.cancel(session_id,
                              reason=(payload or {}).get("reason"),
                              actor=_actor(principal))
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except SessionConflict as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.post("/sessions/{session_id}/correction",
             summary="Flag a finished session as needing review",
             responses={400: {"description": "A reason is required"},
                        404: {"description": "Session not found"}})
def flag_correction(payload: dict = Body(...),
                    session_id: str = Path(max_length=200),
                    principal=Depends(require_user)):
    """
    Say out loud that a register is disputed, without changing it.

    More useful and far safer than quietly recomputing attendance: a teacher
    reviewing the session sees that somebody questioned it, and the evidence
    is all still there to question it with.
    """
    service = _session_service()

    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
        return service.flag_for_correction(
            session_id, reason=(payload or {}).get("reason"),
            actor=_actor(principal))
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except SessionError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/sessions/{session_id}/correction/resolve",
             summary="Mark a flagged session reviewed",
             responses={404: {"description": "Session not found"}})
def resolve_correction(payload: dict = Body(None),
                       session_id: str = Path(max_length=200),
                       principal=Depends(require_user)):
    service = _session_service()

    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
        return service.resolve_correction(
            session_id, note=(payload or {}).get("note"),
            actor=_actor(principal))
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))


@router.delete("/sessions/{session_id}", summary="Delete a session",
               responses={400: {"description": "Not confirmed"},
                          404: {"description": "Session not found"},
                          409: {"description": "It would destroy attendance"}})
def delete_session(session_id: str = Path(max_length=200),
                   confirm: str = Query(
                       None, max_length=200,
                       description="The session id, typed out."),
                   principal=Depends(require_super_admin)):
    """
    Remove a session permanently. Super Admin only, and refused whenever
    anything references it.

    Three tables cascade from this row, so deleting a session that ran takes
    its whole register with it. A session nothing references is a plan
    somebody typed by mistake, and removing that is fine - but even then the
    confirmation is the session's own id, because a button should not be able
    to be clicked into destroying a term of data.
    """
    service = _session_service()

    try:
        require_class_scope(principal, service.get(session_id)["class_id"])
        return service.delete(session_id, confirm=confirm,
                              actor=_actor(principal))
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except SessionConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except SessionError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.get("/sessions/{session_id}/conflicts",
            summary="Sessions that overlap this one")
def session_conflicts(session_id: str = Path(max_length=200),
                      principal=Depends(require_user)):
    """
    A warning, never a refusal.

    A class legitimately meets twice in a day and two sections legitimately
    run at once, so this reports what it found and lets an authorised person
    decide.
    """
    from datetime import datetime

    service = _session_service()

    try:
        session = service.get(session_id)
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))

    require_class_scope(principal, session["class_id"])

    found = service.conflicts(
        class_id=session["class_id"],
        start_time=datetime.fromisoformat(session["start_time"]),
        end_time=datetime.fromisoformat(session["end_time"]),
        section=session.get("section"),
        teacher=session.get("teacher"),
        exclude=session_id)

    return {"session_id": session_id, "conflicts": found,
            "count": len(found)}
