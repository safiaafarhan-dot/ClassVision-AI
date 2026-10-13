"""
Academic years, student academic history, and promotion.

The distinction every endpoint here rests on: a student is permanent, and
their class, section, roll number and semester are facts about a period. A
promotion CREATES a period; it never edits the one before it.

Creating and activating terms, and promoting students, are administrative
acts: SUPER_ADMIN only, enforced by the dependency rather than by hiding a
button. Reading a student's own history is available to any signed-in user,
scoped to the classes they may see.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from backend.services.academic_service import (
    AcademicConflict,
    AcademicError,
    AcademicService,
)
from backend.services.authz import (
    Principal,
    require_student_scope,
    require_super_admin,
    require_user,
)
from backend.services.state import get_data_service, get_database

router = APIRouter(prefix="/api/academic", tags=["academic"])


def _service():
    return AcademicService(get_database())


@router.get("/sessions", summary="Academic years and semesters")
def list_sessions(principal: Principal = Depends(require_user)):
    """
    Every term, newest first, and which one is current.

    Readable by any signed-in user: a teacher needs to know what term it is
    in order to make sense of a register.
    """
    service = _service()
    return {"items": service.list_sessions(),
            "current": service.current_session()}


@router.post("/sessions", status_code=201,
             summary="Create an academic year or semester",
             responses={400: {"description": "Not a usable term"},
                        409: {"description": "That term already exists"}})
def create_session(payload: Dict[str, Any] = Body(...),
                   principal: Principal = Depends(require_super_admin)):
    """
    Add a term. Touches no student and promotes nobody.

    Creating 2027-28 while 2026-27 is running is normal: the new term sits
    PLANNED until somebody activates it, and promotion is a separate step.
    """
    try:
        return _service().create_session(
            academic_year=payload.get("academic_year"),
            semester=payload.get("semester", ""),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            status=payload.get("status", "PLANNED"),
            actor=principal.username)
    except AcademicConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except AcademicError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/sessions/{academic_session_id}/activate",
             summary="Make a term the current one",
             responses={404: {"description": "No such term"}})
def activate_session(academic_session_id: str = Path(max_length=120),
                     principal: Principal = Depends(require_super_admin)):
    """
    Exactly one term is current. The previous one becomes CLOSED, not deleted:
    its enrolments and its attendance stay exactly where they were.
    """
    try:
        return _service().activate_session(academic_session_id,
                                           actor=principal.username)
    except AcademicError as error:
        raise HTTPException(status_code=404, detail=str(error))


@router.get("/sessions/{academic_session_id}/students",
            summary="Who is enrolled in a term")
def session_students(academic_session_id: str = Path(max_length=120),
                     principal: Principal = Depends(require_user)):
    records = _service().academic.students_in_session(academic_session_id)

    if not principal.unrestricted:
        records = [record for record in records
                   if principal.may_see_student(record)]

    return {"items": records, "total": len(records)}


@router.post("/promotion/preview",
             summary="What a promotion would do, without doing it",
             responses={400: {"description": "Not a usable promotion"}})
def preview_promotion(payload: Dict[str, Any] = Body(...),
                      principal: Principal = Depends(require_super_admin)):
    """
    The confirmation summary. A bulk change across forty students is exactly
    the kind of thing that should be read before it is run.

    Reports, per student, what is being changed and what is being CARRIED
    FORWARD - because where the administrator supplied no allocation, the
    previous value is kept rather than invented.
    """
    try:
        return _service().preview_promotion(
            academic_session_id=payload.get("academic_session_id"),
            student_ids=payload.get("student_ids"),
            class_name=payload.get("class_name"),
            section=payload.get("section"),
            allocations=payload.get("allocations"))
    except AcademicError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/promotion", summary="Promote students into a term",
             responses={400: {"description": "Not a usable promotion"}})
def promote(payload: Dict[str, Any] = Body(...),
            principal: Principal = Depends(require_super_admin)):
    """
    Move students into a term, creating a new academic-history row each.

    The previous row is left completely alone - same class, same section, same
    roll - so the attendance recorded against it still describes the term it
    happened in.
    """
    try:
        return _service().promote(
            academic_session_id=payload.get("academic_session_id"),
            student_ids=payload.get("student_ids"),
            class_name=payload.get("class_name"),
            section=payload.get("section"),
            allocations=payload.get("allocations"),
            actor=principal.username)
    except AcademicError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/promotion/allocation",
             summary="Parse a CSV roll/section allocation",
             responses={400: {"description": "The file is not usable"}})
def parse_allocation(payload: Dict[str, Any] = Body(...),
                     principal: Principal = Depends(require_super_admin)):
    """
    Turn a class list into allocations, for a large cohort.

    Accepts the CSV a school office actually produces - a .csv exported from
    a spreadsheet - and reads three columns from it. A missing column means
    "carry the previous value forward", never "blank it".
    """
    try:
        return _service().parse_allocation_csv(payload.get("csv") or "")
    except AcademicError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.get("/students/{student_id}/history",
            summary="Where a student has been, term by term",
            responses={404: {"description": "Student not found"}})
def student_history(student_id: str = Path(max_length=120),
                    principal: Principal = Depends(require_user)):
    """
    Every term, newest first, with the current one marked.

    The previous rows are the point of the whole feature: they record the
    class and section a student was in when their old attendance was taken.
    """
    service = get_data_service()
    require_student_scope(principal, service.get_student(student_id))

    academic = _service()
    return {"student_id": student_id,
            "current": academic.current_enrolment(student_id),
            "items": academic.history(student_id)}


@router.post("/students/{student_id}/enrol",
             summary="Record a student's place in a term",
             responses={400: {"description": "Not a usable enrolment"},
                        404: {"description": "Student not found"}})
def enrol_student(payload: Dict[str, Any] = Body(...),
                  student_id: str = Path(max_length=120),
                  principal: Principal = Depends(require_user)):
    service = get_data_service()
    require_student_scope(principal, service.get_student(student_id),
                          writing=True)

    try:
        return _service().enrol(
            student_id=student_id,
            academic_session_id=payload.get("academic_session_id"),
            class_name=payload.get("class_name"),
            section=payload.get("section"),
            roll_number=payload.get("roll_number"),
            semester=payload.get("semester"),
            academic_year=payload.get("academic_year"),
            start_date=payload.get("start_date"),
            actor=principal.username)
    except AcademicError as error:
        raise HTTPException(status_code=400, detail=str(error))
