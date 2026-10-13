"""
Analytics endpoints (Step 7).

Every number returned here is deterministic arithmetic over stored records.
No model, no inference, no generated text. The definitions behind each figure
are documented in backend/services/analytics_service.py and summarised in the
README.

Where a rate has nothing to divide by, it is returned as null rather than 0.0,
so the dashboard can distinguish "no data" from "genuinely zero".
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend.schemas.models import (
    ClassAnalytics,
    OverviewResponse,
    SessionAnalytics,
    StudentAnalytics,
)
from backend.services.data_service import MAX_PAGE_SIZE
from backend.services.state import get_analytics_service, get_data_service
from backend.services.authz import (
    Principal,
    require_class_scope,
    require_student_scope,
    require_user,
)

# Authentication is applied to the WHOLE router rather than endpoint by
# endpoint, so a route added later is protected by default instead of by
# somebody remembering. On an installation with no accounts yet, require_user
# lets everything through and the dashboard asks for the first Super Admin -
# see backend/services/authz.py for why that state exists at all.
router = APIRouter(prefix="/api/analytics", tags=["analytics"],
                   dependencies=[Depends(require_user)])


@router.get("/overview", response_model=OverviewResponse,
            summary="Headline attendance numbers")
def overview(
        class_id: Optional[str] = Query(None, max_length=120),
        session_date: Optional[str] = Query(
            None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """
    Scoped to COMPLETED sessions only.

    `has_data` reports whether the database holds anything at all, so an empty
    dashboard can say "no sessions yet" rather than showing a confident 0%.
    """
    return get_analytics_service().overview(class_id=class_id,
                                            session_date=session_date)


@router.get("/sessions/{session_id}", response_model=SessionAnalytics,
            summary="Analytics for one session",
            responses={404: {"description": "Session not found"}})
def session_analytics(session_id: str = Path(max_length=200),
                      principal: Principal = Depends(require_user)):
    """
    Scoped to the caller's classes, exactly as /api/sessions/{id} is.

    This endpoint used to take a session id and answer with its whole
    register, whoever asked. Authentication was applied to the router, so a
    teacher could not read it anonymously - but any signed-in teacher could
    read every class in the school through it, including the ones the Sessions
    page will not show them. The scope check is here now, and a session
    outside the caller's classes answers 404 like everywhere else.
    """
    session = get_data_service().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404,
                            detail="Session not found: %s" % session_id)

    require_class_scope(principal, session["class_id"])

    analytics = get_analytics_service().session_analytics(session_id)
    if analytics is None:
        raise HTTPException(status_code=404,
                            detail="Session not found: %s" % session_id)
    return analytics


@router.get("/students/{student_id}", response_model=StudentAnalytics,
            summary="Analytics for one student",
            responses={404: {"description": "Student not found"}})
def student_analytics(student_id: str = Path(max_length=120),
                      principal: Principal = Depends(require_user)):
    """
    Computed across every COMPLETED session the student was EXPECTED in.

    Expectation is the denominator, not appearance: a student who was expected
    and never seen counts against their rate.

    Scoped with require_student_scope in READ mode, so it answers for the
    caller's own classes and - when a Super Admin has granted it - for the
    whole roster. A student outside that answers 404 rather than 403, so the
    status code cannot be used to discover which ids exist in a class the
    caller may not see.

    This is the endpoint behind the performance profile. It used to accept any
    student id from any signed-in account, which made the Students page's
    class scoping a presentational detail rather than a permission.
    """
    require_student_scope(principal,
                          get_data_service().get_student(student_id))

    analytics = get_analytics_service().student_analytics(student_id)
    if analytics is None:
        raise HTTPException(status_code=404,
                            detail="Student not found: %s" % student_id)
    return analytics


@router.get("/classes/{class_id}", response_model=ClassAnalytics,
            summary="Analytics for one class",
            responses={404: {"description": "Class not found"}})
def class_analytics(class_id: str = Path(max_length=120),
                    principal: Principal = Depends(require_user)):
    """
    `comparison_available` is true from two completed sessions;
    `has_trend_data` only from three. One session is neither.

    Scoped to the caller's classes. A class they are not assigned to answers
    404, which is the same answer they get for a class that does not exist.
    """
    known = {row["class_id"] for row in get_data_service().list_classes()}
    if class_id not in known:
        raise HTTPException(status_code=404,
                            detail="Class not found: %s" % class_id)

    # Checked here rather than through require_class_scope, whose 404 says
    # "Session not found" - correct for a session route and confusing on this
    # one. The answer is the same 404 either way, which is the part that
    # matters: a class the caller is not assigned to is indistinguishable from
    # a class that does not exist.
    if not principal.may_see_class_id(class_id):
        raise HTTPException(status_code=404,
                            detail="Class not found: %s" % class_id)

    return get_analytics_service().class_analytics(class_id)


@router.get("/today", summary="The caller's own day: sessions, students, "
                            "attendance and whatever is running")
def today(session_date: Optional[str] = Query(
              None, pattern=r"^\d{4}-\d{2}-\d{2}$",
              description="ISO date. Defaults to the server's current date."),
          principal: Principal = Depends(require_user)):
    """
    One request for the dashboard a teacher opens in the morning.

    Everything here is narrowed to the caller: a teacher with class
    assignments sees their own classes and their own students, a teacher
    granted can_view_all_students sees every student's count but still only
    their own sessions, and a Super Admin sees the school. The narrowing is
    not done here - it is `principal` handed to the two list methods that
    already know how to apply it, which is what keeps this endpoint from
    becoming a second opinion about who may see what.

    The date defaults to the SERVER's current date, deliberately. A browser in
    another timezone asking for "today" would otherwise get a different day
    from the one the sessions were scheduled against, and the session rows
    carry the server's date.

    What is not here: any invented figure. `attendance.has_data` is false until
    a session has actually COMPLETED, because attendance is produced by the
    rule engine after a session is finalised and a lesson still running has
    none. `active_session` is null when nothing is running rather than being
    the next scheduled one.
    """
    from datetime import date as _date

    session_date = session_date or _date.today().isoformat()

    data = get_data_service()

    # Every session on this date the caller may see. MAX_PAGE_SIZE is the cap;
    # a day with more lessons than that in one teacher's scope is not a case
    # this dashboard is for, and the count reported is the honest total.
    page = data.list_sessions(session_date=session_date, principal=principal,
                              limit=MAX_PAGE_SIZE, offset=0)
    sessions = page["items"]

    by_status = {}
    for session in sessions:
        by_status[session["status"]] = by_status.get(session["status"], 0) + 1

    # Students in the caller's scope. with_attendance is off because this is a
    # count, and computing a term's attendance summary per student in order to
    # display "38" would be the expensive way to answer it.
    roster = data.list_students(principal=principal, limit=1, offset=0,
                                with_attendance=False)
    active_roster = data.list_students(principal=principal, status="ACTIVE",
                                       limit=1, offset=0,
                                       with_attendance=False)

    summary = get_analytics_service().day_summary(
        [session["session_id"] for session in sessions])

    # What is actually running, from the live runtime rather than the table, so
    # this agrees with the Live classroom tab. Reported only when the caller
    # may see that session's class: a teacher does not learn that another
    # class's camera is on.
    from backend.services.live_service import get_live_manager

    runtime = get_live_manager().status()
    live_session_id = runtime.get("session_id")
    live = None

    if live_session_id:
        running = data.get_session(live_session_id)
        if running and principal.may_see_class_id(running["class_id"]):
            live = {
                "session_id": live_session_id,
                "class_id": running["class_id"],
                "session_date": running["session_date"],
                "state": runtime.get("state"),
                "fps": runtime.get("fps"),
                "started_at": runtime.get("started_at"),
            }

    # The session row a teacher would act on: whatever is live, else the first
    # ACTIVE session today. Null when neither exists - never the next
    # scheduled one dressed up as a current one.
    active_session = None
    if live is not None:
        active_session = next((session for session in sessions
                               if session["session_id"] == live_session_id),
                              None)
    if active_session is None:
        active_session = next((session for session in sessions
                               if session["status"] == "ACTIVE"), None)

    return {
        "session_date": session_date,
        "scope": principal.describe(),
        "sessions": sessions,
        "session_counts": {
            "total": page["total"],
            "scheduled": by_status.get("SCHEDULED", 0),
            "active": by_status.get("ACTIVE", 0),
            "completed": by_status.get("COMPLETED", 0),
            "cancelled": by_status.get("CANCELLED", 0),
        },
        "students": {
            "total": roster["total"],
            "active": active_roster["total"],
        },
        "attendance": summary,
        "active_session": active_session,
        "live": live,
    }


@router.get("/attention", summary="Students meeting explicit review thresholds")
def attention(
        class_id: Optional[str] = Query(None, max_length=120),
        minimum_sessions: int = Query(3, ge=1, le=100)):
    """
    NOT anomaly detection. No model is involved.

    A student appears only when a stated arithmetic threshold is literally
    exceeded, and the response includes the rule that produced the list so it
    can be checked or disagreed with.
    """
    return get_analytics_service().attention_indicators(
        class_id=class_id, minimum_sessions=minimum_sessions)
