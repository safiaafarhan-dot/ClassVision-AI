"""
Sign-in endpoints.

    The dashboard used to say "Authentication is not implemented." It now has
    a login screen, and the disclaimer says what is actually true instead:
    local password authentication, not independently reviewed.

The first run
    A database with no accounts is UNCONFIGURED. /api/auth/state says so, the
    dashboard shows a create-the-first-administrator form, and /api/auth/setup
    accepts exactly one account before refusing forever. There is no default
    administrator password anywhere in this project, and that endpoint is why
    there does not need to be one.

The cookie
    HttpOnly, so no script on the page can read it - including any script an
    attacker manages to inject. SameSite=Lax, so another site cannot make a
    browser use it. Secure only when the request arrived over HTTPS, because
    marking it Secure on a plain-HTTP classroom machine would stop sign-in
    working entirely and teach everybody to turn authentication off.

What is never returned
    A password, a password hash, a session token belonging to somebody else,
    or a hint about which half of a wrong username-and-password was wrong.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.schemas.models import (
    AuthStateResponse,
    BootstrapRequest,
    LoginRequest,
    PasswordChangeRequest,
    SimpleResult,
    UserAccount,
)
from backend.services.auth_service import (
    SESSION_COOKIE,
    SESSION_HOURS,
    AuthConflict,
    AuthFailed,
    AuthForbidden,
    AuthInvalid,
    AuthService,
)
from backend.services.authz import Principal, current_principal, require_user
from backend.services.state import get_database

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def _service():
    return AuthService(get_database())


def _set_cookie(response: Response, request: Request, token: str):
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/")


@router.get("/state", response_model=AuthStateResponse,
            summary="Whether sign-in is configured, and who is signed in")
def auth_state(principal: Principal = Depends(current_principal)):
    """
    The one endpoint that never requires a session.

    The dashboard calls it before drawing anything, to decide between the
    first-run form, the login screen and the application itself.
    """
    return principal.describe()


@router.post("/setup", response_model=UserAccount, status_code=201,
             summary="Create the first Super Admin",
             responses={400: {"description": "The account is not usable"},
                        409: {"description": "Accounts already exist"}})
def setup(payload: BootstrapRequest, request: Request, response: Response):
    """
    Create the first account, and only the first.

    Refused outright once any account exists, so this can never mint a second
    administrator without signing in. That refusal is the only thing between
    first-run convenience and "anybody can become an administrator", so it is
    a hard refusal rather than a permission.

    Signs the new administrator in, because being bounced to a login screen
    immediately after choosing a password helps nobody.
    """
    service = _service()

    try:
        user = service.bootstrap_super_admin(payload.username,
                                             payload.display_name,
                                             payload.password)
        token, described = service.login(payload.username, payload.password)
    except AuthConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))

    _set_cookie(response, request, token)
    return described


@router.post("/login", response_model=UserAccount,
             summary="Sign in",
             responses={401: {"description": "Incorrect credentials, or the "
                                             "account is deactivated"}})
def login(payload: LoginRequest, request: Request, response: Response):
    try:
        token, user = _service().login(payload.username, payload.password)
    except AuthFailed as error:
        raise HTTPException(status_code=401, detail=str(error))

    _set_cookie(response, request, token)
    return user


@router.post("/logout", response_model=SimpleResult, summary="Sign out")
def logout(request: Request, response: Response):
    """
    End this session. Safe to call when not signed in.

    The token is deleted server-side as well as cleared from the browser, so
    a copy of the cookie taken beforehand stops working too.
    """
    _service().logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "detail": "Signed out."}


@router.get("/me", response_model=AuthStateResponse,
            summary="The signed-in account")
def me(principal: Principal = Depends(require_user)):
    return principal.describe()


@router.post("/password", response_model=SimpleResult,
             summary="Change your own password",
             responses={400: {"description": "The new password is not usable"},
                        401: {"description": "The current password is wrong"}})
def change_password(payload: PasswordChangeRequest,
                    request: Request, response: Response,
                    principal: Principal = Depends(require_user)):
    """
    Change your own password, proving you know the current one.

    Every other session belonging to this account is ended, because a password
    change is usually a response to suspecting somebody else has it.
    """
    try:
        _service().change_own_password(principal.actor(),
                                       payload.current_password,
                                       payload.new_password)
    except AuthFailed as error:
        raise HTTPException(status_code=401, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))

    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True,
            "detail": "Password changed. Sign in again with the new one."}
