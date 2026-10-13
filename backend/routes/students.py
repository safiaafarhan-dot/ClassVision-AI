"""
Student endpoints.

A student here is an id, a name, a roll number, a class, a section, a status,
an attendance history, and whether face samples exist for them.

Where each of those comes from, and why it matters
    Since schema version 2 every identifying field is a SQLite column in the
    students table. The registry is the single source of truth for WHO a
    student is: opening data/classvision.db and running

        SELECT student_id, name, roll_number, class_name, section, status
          FROM students ORDER BY student_id;

    now answers that question completely, with no second store to consult.

    The embedding store answers exactly two further questions - does a face
    enrollment exist for this id, and how many samples does it hold - and its
    METADATA is what is read. The vectors live in a separate .npz per student
    and no code path from an HTTP request reaches one.

    Attendance is DERIVED, on every read, from attendance_records. It is never
    copied onto the student row: a number stored next to the registry would be
    wrong the moment a session is re-evaluated or an override applied.

Why enrollment lives in this module
    The project has always had two stores keyed on the same student_id with no
    link between them: face vectors under data/embeddings/, student rows in
    data/classvision.db. Enrolling from the command line wrote vectors and no
    row, so a student the camera recognised perfectly well was invisible on
    the Students page and unselectable when scheduling a session.

    Enrollment now writes both, from either path. The capture itself is the
    existing Step 3 pipeline - YuNet, SFace, the same quality gate and the
    same capture rules the CLI uses - driven headlessly from
    ai/enrollment_session.py.

Privacy
    No endpoint here can return biometric data. The browser never uploads or
    receives a frame, a face crop or an embedding; the camera is read
    server-side and only counters and a state come back.
"""

from typing import Optional

from enrollment_rules import DEFAULT_SAMPLES

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.responses import StreamingResponse

from backend.schemas.models import (
    StudentFilterOptions,
    EnrollmentStartRequest,
    EnrollmentStatusResponse,
    PagedAttendance,
    PagedStudents,
    Student,
    StudentDeleteResponse,
    StudentRegisterRequest,
    StudentUpdateRequest,
)
from backend.services.data_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    StudentConflictError,
    StudentValidationError,
)
from backend.services.enrollment_service import (
    EnrollmentConflict,
    EnrollmentInvalid,
    EnrollmentUnavailable,
    get_enrollment_manager,
)
from backend.services.authz import (
    Principal,
    require_student_scope,
    require_super_admin,
    require_user,
)
from backend.services.state import get_data_service, get_database

router = APIRouter(prefix="/api", tags=["students"])

ENROLL_BOUNDARY = "classvision-enroll-frame"

# multipart/x-mixed-replace is specified in CRLF. Named rather than escaped
# inline so the part builder stays readable.
CRLF = b"\r\n"


# Guardian contact a teacher is not handed. A class teacher can see WHETHER a
# guardian is contacted - that changes how they read an absence - without the
# Students page becoming an exportable list of parents' phone numbers.
REDACTED_FOR_TEACHERS = ("parent_name", "parent_phone", "parent_email")


def _redact_contact(record, principal):
    """
    Blank a guardian's contact unless the caller is a Super Admin.

    Returns None for a field that is hidden AND for a field that is empty, on
    purpose: from outside, "no number on file" and "you may not see it" look
    the same, so the response cannot be used to probe which students have a
    number recorded.
    """
    if principal.is_super_admin:
        return record

    for field in REDACTED_FOR_TEACHERS:
        if field in record:
            record[field] = None
    return record


def _redact_page(page, principal):
    for record in page.get("items", []):
        _redact_contact(record, principal)
    return page


def _require_contact_permission(payload, principal):
    """
    Only a Super Admin writes a guardian's contact detail.

    A teacher may still register and edit a student - that is their job - and
    the rest of the form goes through untouched. Supplying a contact field they
    may not see is a 403 rather than a silent drop: quietly ignoring half a
    form is how a school ends up believing it has phone numbers it does not.

    notification_enabled is deliberately NOT restricted. It is a switch about
    whether alerts go out, it carries no contact detail, and a class teacher is
    the person who knows a family has asked not to be messaged.
    """
    if principal.is_super_admin:
        return

    supplied = [field for field in REDACTED_FOR_TEACHERS
                if getattr(payload, field, None) is not None]
    if supplied:
        raise HTTPException(
            status_code=403,
            detail="Only a Super Admin can change parent contact details "
                   "(%s)." % ", ".join(supplied))


def _audit(principal, action, subject, detail=None):
    """
    Record an administrative action, best effort.

    Never a password, never an embedding, never anything biometric - only that
    somebody did something to a named student. A failure to write the audit
    row must not fail the operation that succeeded.
    """
    try:
        from repositories import AuditRepository
        AuditRepository(get_database()).record(
            action, actor=principal.username, actor_role=principal.role,
            subject=subject, detail=detail)
    except Exception:                                    # noqa: BLE001
        pass


@router.get("/students", response_model=PagedStudents,
            summary="Registered students")
def list_students(
        search: Optional[str] = Query(
            None, max_length=120,
            description="Matches student id, name or roll number"),
        status: Optional[str] = Query(None, pattern="^(ACTIVE|INACTIVE)$"),
        class_name: Optional[str] = Query(None, max_length=120),
        section: Optional[str] = Query(None, max_length=40),
        academic_year: Optional[str] = Query(
            None, max_length=60,
            description="The academic year, as the school writes it. "
                        "Combines with class_name and section; all three are "
                        "ANDed and any of them may be omitted."),
        enrolled: Optional[bool] = Query(
            None, description="Filter by whether face samples exist"),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(0, ge=0),
        principal: Principal = Depends(require_user)):
    """
    Every registered student, with their metadata, enrollment state and
    attendance summary.

    A teacher with class assignments sees only their own classes. That is
    applied here, in the service, not by hiding rows in the browser.

    academic_year, class_name and section are three independent filters and
    every combination of them works, including none of them. All three are
    resolved in SQL against an index, so a school of five thousand students
    returns one page and not five thousand rows for the browser to sift.
    """
    return _redact_page(get_data_service().list_students(
        search=search, status=status, class_name=class_name, section=section,
        academic_year=academic_year, enrolled=enrolled, principal=principal,
        limit=limit, offset=offset),
        principal)


@router.get("/students/filters", response_model=StudentFilterOptions,
            summary="The years, classes and sections students are actually in")
def student_filter_options(
        class_name: Optional[str] = Query(None, max_length=120),
        section: Optional[str] = Query(None, max_length=40),
        academic_year: Optional[str] = Query(None, max_length=60),
        principal: Principal = Depends(require_user)):
    """
    What the registry's filters should offer, read from the registry.

    Declared BEFORE /students/{student_id} on purpose: FastAPI matches routes
    in order, and a literal path registered after a parameterised one on the
    same prefix is never reached - "filters" would arrive as a student id.

    Each list is narrowed by the other two selections, so choosing a year and
    a class leaves only the sections that pair actually has. A teacher limited
    to their own classes gets the options their own classes produce, through
    the same scope check that filters the rows.
    """
    return get_data_service().student_filter_options(
        academic_year=academic_year, class_name=class_name, section=section,
        principal=principal)


@router.post("/students", response_model=Student, status_code=201,
             summary="Register a student",
             responses={400: {"description": "The request is not a usable "
                                             "student"},
                        409: {"description": "That student id already exists"}})
def create_student(payload: StudentRegisterRequest,
                   principal: Principal = Depends(require_user)):
    """
    Add a student to the registry, with all of their metadata.

    Everything on the form is written to SQLite, whether or not a camera is
    ever involved. Registration and face enrollment stay separate steps - a
    student with no face samples is a real state, shown as "Not enrolled" -
    but registration is no longer a half-record that an enrollment has to
    complete later.

    Nothing about this endpoint knows any particular student: the same code
    path serves the first student and the four-hundredth.
    """
    _require_contact_permission(payload, principal)

    try:
        student = get_data_service().create_student(
            payload.student_id, payload.name,
            roll_number=payload.roll_number,
            class_name=payload.class_name,
            section=payload.section,
            academic_year=payload.academic_year,
            parent_name=payload.parent_name,
            parent_phone=payload.parent_phone,
            parent_email=payload.parent_email,
            notification_enabled=payload.notification_enabled)
    except StudentConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except (StudentValidationError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error))

    _audit(principal, "student.create", payload.student_id,
           "class=%s-%s" % (payload.class_name or "", payload.section or ""))
    return _redact_contact(student, principal)


@router.post("/students/enrollment/start",
             response_model=EnrollmentStatusResponse,
             summary="Start camera face enrollment",
             responses={400: {"description": "The request is not a usable "
                                             "enrollment"},
                        409: {"description": "An enrollment or a live session "
                                             "is already using the camera, or "
                                             "this student is enrolled"},
                        503: {"description": "The camera or a model is not "
                                             "available"}})
def start_enrollment(payload: EnrollmentStartRequest,
                     principal: Principal = Depends(require_user)):
    """
    Open the camera and capture face samples for one student.

    On success the registry row is written too, with the full metadata, so a
    student enrolled here is immediately visible on the Students page and
    selectable when scheduling a session.

    The dashboard's "Save and enroll face" registers the student BEFORE
    calling this, which is why a cancelled capture there leaves a registered
    student who is simply not enrolled yet - a true and recoverable state.
    Calling this endpoint directly for an unregistered student writes nothing
    at all unless the capture completes.

    The camera is opened before this returns, so a missing or busy camera is
    reported immediately rather than appearing to start and dying a moment
    later. Capture then runs on a background thread; poll
    /api/students/enrollment/status for progress.

    No vectors are stored until every sample has been captured. A cancelled or
    failed enrollment leaves none.

    The browser sends these details and nothing else - it never uploads a
    frame, a face crop or an embedding.
    """
    manager = get_enrollment_manager()

    try:
        return manager.start(
            get_database(),
            student_id=payload.student_id,
            name=payload.name,
            roll_number=payload.roll_number or "",
            class_name=payload.class_name or "",
            section=payload.section or "",
            samples=payload.samples or DEFAULT_SAMPLES,
            camera_index=payload.camera_index,
            replace=payload.replace)
    except EnrollmentConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except EnrollmentInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))
    except EnrollmentUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))


@router.get("/students/enrollment/status",
            response_model=EnrollmentStatusResponse,
            summary="Face enrollment progress")
def enrollment_status(principal: Principal = Depends(require_user)):
    """
    How the current enrollment is going, safe to poll once a second.

    Always answers, including when nothing is running - an IDLE enrollment is
    a valid answer, not an error. Reports counters and a state; never an
    image.
    """
    return get_enrollment_manager().status()


@router.post("/students/enrollment/cancel",
             response_model=EnrollmentStatusResponse,
             summary="Cancel a running face enrollment")
def cancel_enrollment(principal: Principal = Depends(require_user)):
    """
    Stop a running enrollment and release the camera.

    Safe to call when nothing is running, and safe to call twice. A cancelled
    capture stores no vectors, because they are only written once every sample
    has been taken.
    """
    return get_enrollment_manager().cancel()


# -- the enrollment camera view ---------------------------------------------
#
# The COMPLETE camera frame while somebody is being enrolled, with the
# detected face boxed on it. Enrollment fails most often because the person is
# too far away, off-centre or backlit, and every one of those is obvious in a
# picture of the room and invisible in a crop of a face - so a crop is exactly
# what is NOT served here.
#
# It reads the slot the capture worker publishes into. No second camera is
# opened and no extra detection runs; the box comes from the detection the
# sample collector just used. Nothing is stored: the bytes are replaced by the
# next frame and dropped when the capture ends.


@router.get("/students/enrollment/frame.jpg",
            summary="The current enrollment camera view",
            response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}},
                       503: {"description": "No enrollment is running"}})
def enrollment_frame(principal: Principal = Depends(require_user)):
    """One JPEG of the enrollment camera, or 503 when nothing is capturing."""
    jpeg, index = get_enrollment_manager().runtime.latest_frame()

    if not jpeg:
        raise HTTPException(
            status_code=503,
            detail="No enrollment camera view is available.")

    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, no-cache",
                             "X-Frame-Index": str(index)})


@router.get("/students/enrollment/stream.mjpg",
            summary="The enrollment camera view, as a live stream",
            response_class=StreamingResponse)
def enrollment_stream(principal: Principal = Depends(require_user)):
    """
    Motion JPEG, so a plain <img> can show it with no player and no library.

    Ends as soon as the capture does, so a dialog left open cannot hold the
    generator alive.
    """
    import time

    manager = get_enrollment_manager()

    def frames():
        last_index = -1
        idle_since = time.time()

        while True:
            jpeg, index = manager.runtime.latest_frame()

            if jpeg and index != last_index:
                last_index = index
                idle_since = time.time()
                header = CRLF.join([
                    b"--" + ENROLL_BOUNDARY.encode(),
                    b"Content-Type: image/jpeg",
                    b"Content-Length: " + str(len(jpeg)).encode(),
                    b"", b"",
                ])
                yield header + jpeg + CRLF
            else:
                if not manager.is_busy and time.time() - idle_since > 2:
                    return
                time.sleep(0.03)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=%s" % ENROLL_BOUNDARY,
        headers={"Cache-Control": "no-store, no-cache"})


@router.get("/students/{student_id}", response_model=Student,
            summary="One student",
            responses={404: {"description": "Student not found"}})
def get_student(student_id: str = Path(max_length=120),
                principal: Principal = Depends(require_user)):
    student = get_data_service().get_student(student_id)
    return _redact_contact(require_student_scope(principal, student),
                           principal)


@router.patch("/students/{student_id}", response_model=Student,
              summary="Edit a student",
              responses={400: {"description": "The request is not a usable "
                                              "edit"},
                         404: {"description": "Student not found"}})
def update_student(payload: StudentUpdateRequest,
                   student_id: str = Path(max_length=120),
                   principal: Principal = Depends(require_user)):
    """
    Change a student's name, roll number, class, section or status.

    The student id is not editable, here or anywhere: attendance records,
    presence segments, expected-student rows and the face enrollment all
    reference it. Editing metadata never touches the face enrollment - a
    corrected spelling must not cost a student their samples.

    The embedding store's copy of the metadata is brought into step as a best
    effort, so `python ai/embedding_store.py list` and the Students page do
    not disagree.
    """
    service = get_data_service()
    require_student_scope(principal, service.get_student(student_id),
                          writing=True)

    _require_contact_permission(payload, principal)

    try:
        student = service.update_student(
            student_id,
            name=payload.name,
            roll_number=payload.roll_number,
            class_name=payload.class_name,
            section=payload.section,
            academic_year=payload.academic_year,
            status=payload.status,
            parent_name=payload.parent_name,
            parent_phone=payload.parent_phone,
            parent_email=payload.parent_email,
            notification_enabled=payload.notification_enabled)
    except StudentValidationError as error:
        raise HTTPException(status_code=400, detail=str(error))

    if student is None:
        raise HTTPException(status_code=404,
                            detail="Student not found: %s" % student_id)

    _audit(principal, "student.update", student_id,
           ",".join(sorted(payload.model_dump(exclude_none=True))))
    return _redact_contact(student, principal)


@router.post("/students/{student_id}/status", response_model=Student,
             summary="Activate or deactivate a student",
             responses={404: {"description": "Student not found"}})
def set_student_status(status: str = Query(pattern="^(ACTIVE|INACTIVE)$"),
                       student_id: str = Path(max_length=120),
                       principal: Principal = Depends(require_user)):
    """
    Retire a student, or bring them back, WITHOUT touching their history.

    This is the safe alternative to deletion and the one the dashboard offers
    first. A deactivated student keeps every attendance record, every presence
    segment and every expected-student row; they simply stop appearing in the
    active roster.
    """
    service = get_data_service()
    require_student_scope(principal, service.get_student(student_id),
                          writing=True)

    student = service.set_student_status(student_id, status)
    if student is None:
        raise HTTPException(status_code=404,
                            detail="Student not found: %s" % student_id)

    _audit(principal, "student.status", student_id, status)
    return student


@router.delete("/students/{student_id}", response_model=StudentDeleteResponse,
               summary="Delete a student permanently",
               responses={403: {"description": "Only a Super Admin can do "
                                               "this"},
                          404: {"description": "Student not found"},
                          409: {"description": "Deleting them would destroy "
                                               "attendance history"}})
def delete_student(student_id: str = Path(max_length=120),
                   confirm: Optional[str] = Query(
                       None,
                       max_length=64,
                       description="To delete a student who has attendance "
                                   "history, set this to their exact student "
                                   "id. Everything listed in the 409 response "
                                   "is then destroyed permanently."),
                   principal: Principal = Depends(require_super_admin)):
    """
    Remove a student, their registry row and their face samples. Super Admin
    only.

    Refused by default when anything references them. Every table that points
    at students(student_id) cascades on delete, so removing a student who has
    been in a lesson would silently remove that lesson's attendance for them -
    which is exactly what a school must not be able to do by clicking Remove.
    The refusal names what would be lost and Deactivate remains available.

    Why this one endpoint is narrower than the rest of the module
        A teacher registers, edits, enrolls and deactivates students - that is
        their job, and none of it loses anything. This is the only student
        operation that destroys data no other record can reconstruct:
        attendance, presence segments, expected-student rows, academic
        history, notifications and the face samples all cascade from
        students(student_id). DELETE /api/sessions/{id} has always been Super
        Admin only for exactly that reason, and a student deletion cascades
        wider than a session deletion does, so leaving it open to any
        signed-in account was an asymmetry rather than a decision.

        Deactivate is unchanged and remains available to teachers. It is the
        answer to "this student has left", and it costs nothing.

    A teacher calling this by hand gets 403 whether or not the student exists
    and whether or not they are inside that teacher's classes: the guard runs
    as a dependency, before the row is read, so the status code cannot be used
    to probe which ids exist.

    To go through with it, confirm must be the student's own id, typed out.
    A boolean was not enough: one stray click on a button already labelled
    "Delete permanently" destroyed a real student's term of attendance during
    development. Typing the id is the standard protection for an irreversible
    destruction, and it cannot be produced by a double-click.

    The face enrollment goes with them. A vector file for a student who no
    longer exists is an orphaned biometric record, and keeping one would be
    the opposite of what deletion means.
    """
    service = get_data_service()
    require_student_scope(principal, service.get_student(student_id),
                          writing=True)

    confirmed = confirm is not None and confirm.strip() == student_id

    if confirm is not None and not confirmed:
        raise HTTPException(
            status_code=400,
            detail="To delete %s permanently, type their student id exactly."
                   % student_id)

    try:
        result = service.delete_student(student_id, force=confirmed)
    except StudentConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))

    if result is None:
        raise HTTPException(status_code=404,
                            detail="Student not found: %s" % student_id)

    _audit(principal, "student.delete", student_id,
           "confirmed by typed id" if confirmed else "no history to lose")
    return result


@router.get("/students/{student_id}/attendance", response_model=PagedAttendance,
            summary="A student's attendance history",
            responses={404: {"description": "Student not found"}})
def student_attendance(
        student_id: str = Path(max_length=120),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(0, ge=0),
        principal: Principal = Depends(require_user)):
    service = get_data_service()
    require_student_scope(principal, service.get_student(student_id))
    return service.student_attendance(student_id, limit=limit, offset=offset)
