"""
ClassVision AI - Authorization
-------------------------------
Who is asking, and what they are allowed to see.

    Hiding a button is not a permission. Every rule in this module is applied
    on the SERVER, before any data is read, so calling an endpoint by hand
    gets a teacher exactly as far as clicking would have.

Three states, not two

    UNCONFIGURED  No accounts exist. The API is open and the dashboard asks
                  for the first Super Admin. Every installation that predates
                  authentication is in this state, and the alternative -
                  shipping a default administrator password - is worse in
                  every way that matters. There is no API route back into
                  this state: it ends the moment an account is created.

    SUPER_ADMIN   Everything, including accounts and assignments.

    TEACHER       Their own classes. A teacher with NO class assignments is
                  unrestricted within the teacher role, because a teacher who
                  has been given an account but not yet a timetable should be
                  able to work rather than see an empty dashboard and assume
                  the system is broken.

Class scope, and why it is fuzzy on purpose

    A session carries a free-text class_id ("CSE(AI)-C", "cse(ai) c",
    "CLASS-10A"); the student registry carries class_name and section as
    separate columns. Those two spellings of the same idea already existed,
    and inventing a canonical class table would mean rewriting sessions that
    teachers have already scheduled.

    So both sides are normalised - uppercased, stripped to letters and digits
    - and compared. It is a small, reversible accommodation of the data that
    is actually there, and it is applied in ONE place so it can be replaced
    with a real class table later without touching a route.
"""

from fastapi import Depends, HTTPException, Request

from repositories import ROLE_SUPER_ADMIN, ROLE_TEACHER

from backend.services.auth_service import SESSION_COOKIE, AuthService
from backend.services.state import get_database

UNAUTHENTICATED = "Sign in to use this."
FORBIDDEN = "Your account does not have access to this."


def normalise_class(text):
    """
    A class name reduced to what two people typing it would agree on.

    "CSE(AI)-C", "cse (ai) c" and "CSE_AI_C" all become "CSEAIC".
    """
    return "".join(character for character in str(text or "").upper()
                   if character.isalnum())


class Principal:
    """
    The caller, and what they may see. Immutable for the life of a request.

    A Principal with `unrestricted` is not a Principal with every permission:
    an unconfigured installation is unrestricted AND not signed in, and the
    difference matters when deciding whether to write an audit row naming an
    actor who does not exist.
    """

    __slots__ = ("user", "configured")

    def __init__(self, user=None, configured=False):
        self.user = user
        self.configured = configured

    # -- identity ----------------------------------------------------------

    @property
    def is_authenticated(self):
        return self.user is not None

    @property
    def role(self):
        return self.user.get("role") if self.user else None

    @property
    def username(self):
        return self.user.get("username") if self.user else None

    @property
    def is_super_admin(self):
        """
        True for a signed-in Super Admin, and for an unconfigured system.

        The second half is what lets a fresh installation create its first
        account and lets the 26 test suites that predate authentication keep
        running unchanged against databases that have no users in them.
        """
        return (not self.configured) or self.role == ROLE_SUPER_ADMIN

    @property
    def is_teacher(self):
        return self.role == ROLE_TEACHER

    # -- scope -------------------------------------------------------------

    @property
    def assignments(self):
        return (self.user or {}).get("assignments") or []

    @property
    def may_view_all_students(self):
        """
        Whether a Super Admin has granted this teacher the whole roster.

        A separate, explicit grant rather than a change to how scoping works,
        so a school that wants the existing behaviour keeps it and one that
        wants teachers to see everybody turns it on per account. It widens
        READING only - see `unrestricted`, which this deliberately does not
        feed into.
        """
        return bool((self.user or {}).get("can_view_all_students"))

    @property
    def unrestricted(self):
        """
        Whether this caller sees everything AND may act on it.

        A Super Admin does. So does a teacher with no class assignment, and so
        does an unconfigured installation. Only a teacher WITH assignments is
        narrowed.

        can_view_all_students is deliberately NOT consulted here. That grant
        is about what a teacher may look at; this property also governs what
        they may change, and the two are different permissions. A teacher who
        can see every class still edits only their own - see
        `may_see_student`, which is the read-side question.
        """
        if self.is_super_admin:
            return True
        return not self.assignments

    def class_keys(self):
        """Normalised class+section keys this caller may see."""
        keys = set()
        for assignment in self.assignments:
            class_key = normalise_class(assignment.get("class_name"))
            section_key = normalise_class(assignment.get("section"))
            if class_key:
                keys.add((class_key, section_key))
        return keys

    def in_assigned_classes(self, student):
        """
        Whether one student row falls inside this caller's own assignments.

        An assignment with no section covers every section of that class; an
        assignment with a section covers only that one.

        This is the raw class-scope question and it knows nothing about
        grants. Both may_see_student and may_edit_student are built from it,
        which is what keeps the read and write answers in step with each
        other except where they are deliberately meant to differ.
        """
        student_class = normalise_class(student.get("class_name"))
        student_section = normalise_class(student.get("section"))

        for class_key, section_key in self.class_keys():
            if class_key != student_class:
                continue
            if not section_key or section_key == student_section:
                return True
        return False

    def may_see_student(self, student):
        """
        May this caller LOOK at this student?

        Their own classes, plus everybody if a Super Admin has granted
        can_view_all_students. That grant is the whole reason this is a
        separate question from may_edit_student.
        """
        if self.unrestricted or self.may_view_all_students:
            return True
        return self.in_assigned_classes(student)

    def may_edit_student(self, student):
        """
        May this caller CHANGE this student?

        Their own classes only. The can_view_all_students grant is
        deliberately not consulted: it is labelled "view", a school turning it
        on is agreeing that teachers may see the whole roster, and it must not
        quietly also let them rename or deactivate a child in somebody else's
        class.

        Note this is NOT `unrestricted`. A teacher with assignments is not
        unrestricted and must still be able to edit their own students - that
        is their job.
        """
        if self.unrestricted:
            return True
        return self.in_assigned_classes(student)

    def may_see_class_id(self, class_id):
        """
        Whether a session's free-text class_id is inside this caller's classes.

        Matched against the class alone and against class+section joined,
        because both spellings occur in sessions that already exist.
        """
        if self.unrestricted:
            return True

        target = normalise_class(class_id)
        if not target:
            return False

        for class_key, section_key in self.class_keys():
            if target == class_key or target == class_key + section_key:
                return True
            if target.startswith(class_key):
                return True
        return False

    def describe(self):
        """What the dashboard needs to draw the right navigation."""
        return {
            "configured": self.configured,
            "authenticated": self.is_authenticated,
            "user": self.user,
            "is_super_admin": self.is_super_admin,
            "unrestricted": self.unrestricted,
            "may_view_all_students": self.may_view_all_students,
        }

    def actor(self):
        """The dict the auth and audit layers expect, or an empty one."""
        return self.user or {}


# -- FastAPI dependencies ---------------------------------------------------

def _auth_service():
    return AuthService(get_database())


def current_principal(request: Request) -> Principal:
    """
    Resolve the caller. Never raises - a missing token is a valid answer.

    Used directly only by endpoints that must answer whether anybody is signed
    in. Everything else depends on require_user or require_super_admin.
    """
    service = _auth_service()
    configured = service.is_configured()

    if not configured:
        return Principal(user=None, configured=False)

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()

    return Principal(user=service.resolve(token), configured=True)


def require_user(principal: Principal = Depends(current_principal)) -> Principal:
    """Any signed-in account. The default guard for dashboard data."""
    if principal.configured and not principal.is_authenticated:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED)
    return principal


def require_super_admin(
        principal: Principal = Depends(current_principal)) -> Principal:
    """Administration. A teacher calling one of these gets 403, not a 404."""
    if principal.configured and not principal.is_authenticated:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED)
    if not principal.is_super_admin:
        raise HTTPException(
            status_code=403,
            detail="Only a Super Admin can do this.")
    return principal


def require_student_scope(principal: Principal, student, writing=False):
    """
    Refuse a named student who is outside the caller's classes.

    404 rather than 403 deliberately: a teacher who may not see class 10B
    should not be able to learn which student ids exist in it by watching the
    status code change.

    `writing` is what keeps the can_view_all_students grant honest.

        writing=False  the read question. A teacher granted sight of the
                       whole roster passes for every student.

        writing=True   the change question. The grant does NOT apply, so a
                       teacher who can SEE every class still edits only their
                       own. Seeing a name and rewriting a record are
                       different acts, and a grant labelled "view all
                       students" must not quietly confer the second.

    Both answer 404, so the distinction is invisible from outside and cannot
    be used to map which students a teacher merely lacks write access to.
    """
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found.")

    permitted = (principal.may_edit_student(student) if writing
                 else principal.may_see_student(student))

    if not permitted:
        raise HTTPException(status_code=404, detail="Student not found.")
    return student


def require_class_scope(principal: Principal, class_id):
    if not principal.may_see_class_id(class_id):
        raise HTTPException(status_code=404, detail="Session not found.")
    return class_id
