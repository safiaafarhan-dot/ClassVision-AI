"""
Student portal endpoints.

A student signs in here and sees their own attendance. That is the entire
surface, and keeping it that small is the point.

The rule every endpoint in this file obeys

    The student is taken from the SESSION TOKEN. Never from a path, never
    from a query parameter, never from the body. There is no
    /api/portal/students/{student_id}, because an endpoint that accepts an id
    is an endpoint somebody tries their friend's id in - and the first person
    to try it would be right to expect it to work, because it would.

    That is why these routes read `/api/portal/me/...` rather than taking an
    identifier at all. The server already knows who is asking; being asked to
    be told is how a portal leaks a year group.

Administration lives at the other end

    Issuing, resetting, suspending and revoking a portal login are Super
    Admin operations and sit under /api/portal/accounts. They are guarded
    twice - by require_super_admin on the route and again inside
    PortalService - because a service that is only safe when called correctly
    is a service waiting for one careless route.

What is never returned

    A password or a hash; another student's anything; a parent's contact
    detail; a face sample, an embedding, a frame or a file path. A student
    looking at their own attendance does not need their mother's phone
    number displayed back at them, and the endpoint that does not return it
    cannot leak it.
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from backend.services.auth_service import (
    AuthConflict,
    AuthFailed,
    AuthForbidden,
    AuthInvalid,
)
from backend.services.authz import Principal, require_super_admin
from backend.services.portal_service import (
    PORTAL_COOKIE,
    PORTAL_SESSION_HOURS,
    PortalService,
)
from backend.services.state import get_data_service, get_database

router = APIRouter(prefix="/api/portal", tags=["student portal"])


def _service():
    return PortalService(get_database())


def _set_cookie(response: Response, request: Request, token: str):
    """
    The same cookie discipline the staff session uses, under a DIFFERENT name.

    HttpOnly so no script can read it, SameSite=Lax so another site cannot
    make a browser use it, and Secure only over HTTPS - marking it Secure on
    a plain-HTTP classroom machine would break sign-in and teach a school to
    switch authentication off.
    """
    response.set_cookie(
        PORTAL_COOKIE, token,
        max_age=PORTAL_SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/")


def current_student(request: Request):
    """
    The signed-in student, or None. Never raises.

    Reads the portal cookie only. A staff session cookie presented here
    resolves to nobody, which is the intended answer: an administrator is not
    a student and has no attendance of their own to look at.
    """
    token = request.cookies.get(PORTAL_COOKIE)
    if not token:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("portal "):
            token = header[7:].strip()
    return _service().resolve(token)


def require_student(request: Request):
    """The guard every /me endpoint depends on."""
    student = current_student(request)
    if student is None:
        raise HTTPException(status_code=401,
                            detail="Sign in to see your attendance.")
    return student


def require_settled_student(student=Depends(require_student)):
    """
    A student who has replaced the password they were handed.

    Until they have, the portal shows the change-password form and nothing
    else. An administrator types a first password onto a printed slip; that
    slip gets left on a desk, and a credential which still works a term later
    is the predictable end of it.
    """
    if student.get("must_change"):
        raise HTTPException(
            status_code=403,
            detail="Choose a new password before viewing your attendance.")
    return student


# -- signing in --------------------------------------------------------------

@router.get("/state", summary="Whether a student is signed in")
def portal_state(request: Request):
    """
    Open to everybody, like the staff equivalent, because the page has to ask
    something before it can know which screen to draw.
    """
    student = current_student(request)
    return {"authenticated": student is not None, "student": student}


@router.post("/login", summary="Sign in to the student portal",
             responses={401: {"description": "Incorrect username or "
                                             "password"}})
def portal_login(response: Response, request: Request,
                 payload: dict = Body(...)):
    """
    Exchange a username and password for a portal session.

    One refusal for an unknown username, a wrong password and a suspended
    account alike. Which of the three it was is information the person typing
    has not earned.
    """
    try:
        token, student = _service().login(
            (payload or {}).get("username"), (payload or {}).get("password"))
    except AuthFailed as error:
        raise HTTPException(status_code=401, detail=str(error))

    _set_cookie(response, request, token)
    return student


@router.post("/logout", summary="Sign out of the student portal")
def portal_logout(response: Response, request: Request):
    _service().logout(request.cookies.get(PORTAL_COOKIE))
    response.delete_cookie(PORTAL_COOKIE, path="/")
    return {"ok": True}


@router.post("/me/password", summary="Change your own portal password",
             responses={400: {"description": "The new password is not usable"},
                        401: {"description": "Not signed in, or the current "
                                             "password is wrong"}})
def change_portal_password(response: Response, request: Request,
                           payload: dict = Body(...),
                           student=Depends(require_student)):
    """
    Replace your own password, with the old one required.

    Signs every portal session out, this one included, so the student signs
    back in with the password they just chose. If the reason for the change
    was that somebody else knew the old one, leaving a session alive would
    undo the point of it.
    """
    body = payload or {}
    try:
        _service().change_own_password(
            student["student_id"], body.get("current_password"),
            body.get("new_password"))
    except AuthFailed as error:
        raise HTTPException(status_code=401, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))

    response.delete_cookie(PORTAL_COOKIE, path="/")
    return {"ok": True, "signed_out": True}


# -- what a student may see --------------------------------------------------

@router.get("/me", summary="Your own registry record")
def portal_me(student=Depends(require_student)):
    """
    Who the portal thinks you are.

    Available BEFORE the first password change, because the change-password
    screen has to be able to greet somebody by name. It carries the registry
    row and no contact detail.
    """
    return student


@router.get("/me/attendance", summary="Your own attendance history")
def portal_attendance(limit: int = Query(50, ge=1, le=200),
                      offset: int = Query(0, ge=0),
                      student=Depends(require_settled_student)):
    """
    Every session you were expected at, and what was recorded.

    The student_id is taken from the session, so there is no parameter to
    change and no other student's history reachable from here.
    """
    return get_data_service().student_attendance(
        student["student_id"], limit=limit, offset=offset)


@router.get("/me/performance", summary="Your attendance performance")
def portal_performance(student=Depends(require_settled_student)):
    """
    The summary a student actually wants: how many sessions, how many
    attended, the percentage, and the current run of absences.

    Derived on every read from attendance_records, exactly as the staff
    analytics are. No number here is stored anywhere, so it cannot drift from
    the register it describes.
    """
    service = get_data_service()
    record = service.get_student(student["student_id"])

    if record is None:
        raise HTTPException(status_code=404, detail="Student not found.")

    history = service.student_attendance(student["student_id"], limit=200,
                                         offset=0)

    return {
        "student": {
            "student_id": record.get("student_id"),
            "name": record.get("name"),
            "roll_number": record.get("roll_number"),
            "class_name": record.get("class_name"),
            "section": record.get("section"),
            "status": record.get("status"),
        },
        "attendance": record.get("attendance") or {},
        "recent": history.get("items", []),
        "total_records": history.get("total", 0),
    }


# -- administration ----------------------------------------------------------

@router.get("/accounts", summary="Every student portal login")
def list_accounts(principal: Principal = Depends(require_super_admin)):
    return _service().list_accounts(principal.actor())


@router.post("/accounts", status_code=201,
             summary="Issue a portal login to a student",
             responses={400: {"description": "Not a usable account"},
                        403: {"description": "Only a Super Admin can do this"},
                        409: {"description": "Already has a login, or the "
                                             "username is taken"}})
def issue_account(payload: dict = Body(...),
                  principal: Principal = Depends(require_super_admin)):
    """
    Create the credential a student signs in with.

    `username` and `password` are both optional. Omitted, the username is the
    STUDENT ID and the password is generated and returned once - see
    PortalService.issue, which documents why those are the right defaults.
    Supplying either still works exactly as before.

    must_change is on either way, so the password an administrator writes on a
    handout stops working the first time it is used properly.
    """
    body = payload or {}
    try:
        return _service().issue(
            principal.actor(), body.get("student_id"),
            body.get("username") or None, body.get("password") or None)
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/accounts/issue-missing", status_code=201,
             summary="Give every student without a login one",
             responses={403: {"description": "Only a Super Admin can do "
                                             "this"}})
def issue_missing_accounts(principal: Principal = Depends(require_super_admin)):
    """
    Issue a portal login to every registered student who has not got one.

    The answer to "every registered student has a Student ID and a password"
    for a school whose register predates the portal. The username is the
    student id and the password is generated; both are the defaults documented
    on PortalService.issue.

    Idempotent, and deliberately so. A student who already has a login is
    SKIPPED and their password is left alone, because the alternative - a
    bulk operation that quietly resets credentials a class is already using -
    is the kind of button somebody presses twice.

    Each generated password is in the response ONCE. It is stored as a PBKDF2
    hash and cannot be read back afterwards, only reset.

    It must be declared above /accounts/{student_id}/... - FastAPI matches
    routes in order, and a literal path registered after a parameterised one
    is never reached.
    """
    return _service().issue_missing(principal.actor())


@router.post("/accounts/{student_id}/password",
             summary="Reset a student's portal password",
             responses={400: {"description": "Not a usable password"},
                        403: {"description": "Only a Super Admin can do this"}})
def reset_account_password(student_id: str, payload: dict = Body(None),
                           principal: Principal = Depends(
                               require_super_admin)):
    """
    Set a new password for a student, and require them to change it again.

    Body optional. With no `password` a new one is generated and returned
    once, which is what a forgotten password actually needs - the office
    should not have to invent a string in order to unblock a child.

    Signs every open portal session for that student out, because a reset
    whose reason is "somebody else knew the old one" has to mean that.
    """
    try:
        return _service().reset_password(
            principal.actor(), student_id,
            (payload or {}).get("password") or None)
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/accounts/{student_id}/status",
             summary="Suspend or restore portal access",
             responses={400: {"description": "Not a usable status"},
                        403: {"description": "Only a Super Admin can do this"}})
def set_account_status(student_id: str,
                       status: str = Query(pattern="^(ACTIVE|INACTIVE)$"),
                       principal: Principal = Depends(require_super_admin)):
    """
    The reversible answer, and the one to reach for first.

    A suspended account keeps its row, its username and its history; the
    student simply cannot sign in until it is restored.
    """
    try:
        return _service().set_status(principal.actor(), student_id, status)
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.delete("/accounts/{student_id}",
               summary="Remove a student's portal login",
               responses={400: {"description": "No such portal account"},
                          403: {"description": "Only a Super Admin can do "
                                               "this"}})
def revoke_account(student_id: str,
                   principal: Principal = Depends(require_super_admin)):
    """
    Delete the LOGIN. The student, and every attendance record they have, is
    untouched.

    Worth saying plainly because "delete" means two very different things in
    this project: DELETE /api/students/{id} destroys a person's history, and
    this destroys a password. Suspending is still the better first answer,
    because it can be undone without issuing new credentials.
    """
    try:
        return _service().revoke(principal.actor(), student_id)
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))
