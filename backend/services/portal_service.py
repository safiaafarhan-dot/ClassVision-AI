"""
ClassVision AI - Student Portal
--------------------------------
A student signing in to see their OWN attendance, and nothing else.

Why this is a second front door rather than a third role

    The obvious design is a STUDENT value in users.role. It was not taken,
    for two reasons that are both about what a mistake would cost.

    The schema reason is written out above USER_ROLES in
    database/database.py: that column carries a CHECK constraint, SQLite
    cannot widen one without rebuilding the table, and no migration in this
    project drops a table that a term of attendance points at.

    The security reason is the one that matters more. Every staff permission
    in backend/services/authz.py begins by resolving a row in `users`. A
    portal credential lives in `student_accounts`, which those guards never
    read, so there is no sequence of edits to role handling that turns a
    fifteen-year-old's login into a teacher's. The separation is a property
    of the schema rather than of the code that happens to read it today.

    The cost is a second cookie and the small amount of duplication below.
    That is a good trade for a portal handed out to every child in a school.

What a portal session can reach

    Exactly two endpoints, both scoped to the signed-in student by the SERVER
    from the session token - never from a path or a query parameter. There is
    no /api/portal/students/{id}, because an endpoint that takes an id is an
    endpoint somebody will try their friend's id in.

Password handling

    hash_password, verify_password and check_password_strength are imported
    from the staff auth service. There is no second password implementation
    in this project, and a portal account is protected exactly as well as an
    administrator's.

    Accounts are issued BY a Super Admin with a first password, and
    must_change is set. The portal refuses to show anything until that first
    password has been replaced, so the credential an administrator typed on
    a handout does not stay valid for a term.
"""

import secrets
import sqlite3
import time
from datetime import timedelta

from repositories import (
    AuditRepository,
    StudentAccountRepository,
    StudentSessionRepository,
    public_student_account,
)

from backend.services.auth_service import (
    AuthConflict,
    AuthFailed,
    AuthForbidden,
    AuthInvalid,
    FAILURE_DELAY_SECONDS,
    MIN_PASSWORD_LENGTH,
    check_password_strength,
    check_username,
    hash_password,
    new_token,
    token_fingerprint,
    utc_now,
    verify_password,
)

# A DIFFERENT cookie from the staff one. Two names rather than one means a
# browser signed in as a teacher and a browser signed in as a student are
# distinguishable states, and a portal token presented to a staff endpoint is
# not merely rejected - it is not even looked at.
PORTAL_COOKIE = "classvision_portal"

# Shorter than the staff session. A portal login is used briefly and often on
# a shared or personal phone, and a token that outlives the look at it was
# opened for is a token somebody else finds still valid.
PORTAL_SESSION_HOURS = 12

ACCOUNT_ACTIVE = "ACTIVE"
ACCOUNT_INACTIVE = "INACTIVE"

# The alphabet a first password is generated from.
#
# No l, I, 1, O or 0. These passwords are read off a printed slip by somebody
# who did not choose them and typed into a phone keyboard, and a school office
# should not spend its morning on the difference between l and 1.
#
# Ambiguity is the only thing removed. The alphabet is still 54 characters, so
# a 14-character password from it carries about 80 bits - more than the
# ten-character minimum a human would have picked, which is the point of
# generating it rather than asking for one.
FIRST_PASSWORD_ALPHABET = ("abcdefghijkmnopqrstuvwxyz"
                           "ABCDEFGHJKLMNPQRSTUVWXYZ"
                           "23456789")
FIRST_PASSWORD_LENGTH = max(14, MIN_PASSWORD_LENGTH)


def generate_first_password():
    """
    A random first password, for a credential that must be replaced on use.

    secrets, not random: this is a credential. It is returned to the Super
    Admin who asked for it EXACTLY ONCE and never stored in plaintext - the
    row holds a PBKDF2 hash like every other password in this project - so a
    lost slip means a reset, not a recovery.
    """
    return "".join(secrets.choice(FIRST_PASSWORD_ALPHABET)
                   for _ in range(FIRST_PASSWORD_LENGTH))


def _iso(moment):
    return moment.replace(microsecond=0).isoformat()


class PortalService:
    """
    Issue, revoke and authenticate student portal logins.

    Every administrative method takes `actor` - the staff Principal's user
    dict - and refuses anybody who is not a Super Admin. The route layer
    already guards these, and this is the second lock: a service that is safe
    on its own cannot be made unsafe by a route that forgets.
    """

    def __init__(self, database):
        self.database = database
        self.accounts = StudentAccountRepository(database)
        self.tokens = StudentSessionRepository(database)
        self.audit = AuditRepository(database)

    # -- administration ----------------------------------------------------

    @staticmethod
    def _require_super_admin(actor):
        """
        Only a Super Admin issues or revokes a student's login.

        A teacher is not refused here out of distrust. A portal credential is
        a child's account: issuing one is a school-office act with a record
        attached, and the audit row names who did it.
        """
        if not actor or actor.get("role") != "SUPER_ADMIN":
            raise AuthForbidden(
                "Only a Super Admin can manage student portal accounts.")

    def _student_or_fail(self, student_id):
        row = self.database.query_one(
            "SELECT student_id, name FROM students WHERE student_id = ?",
            (student_id,))
        if row is None:
            raise AuthInvalid("No student with id %s is registered." %
                              student_id)
        return dict(row)

    def issue(self, actor, student_id, username=None, password=None):
        """
        Create a portal login for a registered student.

        Refuses a student who is not in the registry: the account IS the
        student, and a credential pointing at nobody is a credential nobody
        can revoke by removing a person.

        `username` and `password` are both optional now, and the defaults are
        what make "every registered student has a Student ID and a password"
        true without an administrator inventing two strings per child:

            username  defaults to the STUDENT ID. It is already unique, it is
                      already on the register, and it is the thing a student
                      can be told once and remember. A school that wants
                      something else still passes one.

            password  defaults to a generated one. It is returned to the
                      caller in `password` on THIS RESPONSE ONLY and stored as
                      a PBKDF2 hash, so it can be printed once and never read
                      back.

        must_change stays on either way. A password an office typed - or one
        this method generated onto a printed slip - stops working the first
        time it is used properly.
        """
        self._require_super_admin(actor)

        student = self._student_or_fail(student_id)

        # The student id is the default username. Checked rather than trusted:
        # an id shorter than MIN_USERNAME_LENGTH is refused with the same
        # message a typed one would get, instead of creating an account nobody
        # can sign in to.
        username = check_username(username or student_id)

        generated = password is None
        if generated:
            password = generate_first_password()
        check_password_strength(password)

        if self.accounts.get(student_id) is not None:
            raise AuthConflict(
                "%s already has a portal login. Reset the password instead "
                "of issuing a second one." % student_id)

        if self.accounts.get_by_username(username) is not None:
            raise AuthConflict("That username is already taken.")

        # A username that collides with a STAFF username is refused too. The
        # two tables are separate and a duplicate would work, but a school
        # with one "jsmith" who is a teacher and another who is a pupil is a
        # support call nobody should have to take.
        # COLLATE NOCASE, to match how a staff username is looked up at
        # sign-in. Asking case-sensitively here would let a pupil take
        # "Rahul" while the teacher "rahul" existed, and the two would then
        # be competing for one identity.
        staff = self.database.query_one(
            "SELECT username FROM users WHERE username = ? COLLATE NOCASE",
            (username,))
        if staff is not None:
            raise AuthConflict("That username is already taken.")

        # Both checks above are for the message. student_accounts has a
        # PRIMARY KEY on student_id and UNIQUE on username, and those are
        # what actually stop a second login being minted for one student, or
        # one username being handed to two.
        try:
            with self.database.transaction():
                record = self.accounts.create(
                    student_id, username, hash_password(password),
                    created_by=actor.get("username"), must_change=True)
        except sqlite3.IntegrityError:
            raise AuthConflict(
                "That username is already taken, or %s was given a login by "
                "somebody else a moment ago. Nothing was changed."
                % student_id)

        self.audit.record("portal.issue", actor=actor.get("username"),
                          actor_role=actor.get("role"), subject=student_id,
                          detail="username=%s" % username)

        described = public_student_account(record)
        described["name"] = student["name"]

        # The one place in this project where a plaintext password appears in
        # a response, and only when this method generated it. An administrator
        # who chose the password already knows it, so echoing it back would be
        # a copy of a secret for no gain.
        #
        # public_student_account never carries a hash, and nothing downstream
        # stores this field - see the route, which hands it to the browser to
        # be shown once.
        if generated:
            described["password"] = password
            described["password_is_generated"] = True

        return described

    def issue_missing(self, actor):
        """
        Give every registered student who has no portal login one.

        The answer to "how does every student get a Student ID and a
        password" for a school that registered four hundred children before
        the portal existed. It is additive and idempotent: a student who
        already has a login is skipped and their password is NOT reset, so
        running this twice does not lock a class out of accounts they are
        already using.

        Returns the issued accounts, each carrying its generated password
        once, and the ids that were skipped with the reason. Nothing is
        invented - a student whose id is too short to be a username is
        reported as skipped rather than given a login nobody can use.
        """
        self._require_super_admin(actor)

        rows = self.database.query(
            "SELECT student_id, name FROM students ORDER BY student_id")

        issued = []
        skipped = []

        for row in rows:
            student_id = row["student_id"]

            if self.accounts.get(student_id) is not None:
                skipped.append({"student_id": student_id,
                                "reason": "already has a portal login"})
                continue

            try:
                issued.append(self.issue(actor, student_id))
            except (AuthConflict, AuthInvalid) as error:
                # One unusable id must not abandon the other three hundred
                # and ninety-nine. The reason is reported so the office can
                # fix it rather than wonder which students were missed.
                skipped.append({"student_id": student_id,
                                "reason": str(error)})

        self.audit.record("portal.issue_missing", actor=actor.get("username"),
                          actor_role=actor.get("role"),
                          detail="issued=%d skipped=%d" % (len(issued),
                                                           len(skipped)))

        return {
            "issued": issued,
            "skipped": skipped,
            "note": ("Each password is shown once and stored only as a hash. "
                     "Print or copy them now - they cannot be read back, only "
                     "reset."),
        }

    def reset_password(self, actor, student_id, password=None):
        """
        Replace a forgotten password and require it to be changed again.

        Every open portal session for that student is dropped, so a password
        reset is also a sign-out everywhere - which is what it has to mean
        when the reason for the reset is that somebody else knew the old one.

        `password` is optional, exactly as in issue(): omitted, one is
        generated and returned once. A forgotten password is the common case
        and the office should not have to think of a new one to type twice.
        """
        self._require_super_admin(actor)

        if self.accounts.get(student_id) is None:
            raise AuthInvalid("%s has no portal login." % student_id)

        generated = password is None
        if generated:
            password = generate_first_password()
        check_password_strength(password)

        with self.database.transaction():
            self.accounts.update(student_id,
                                 password_hash=hash_password(password),
                                 must_change=1)
            self.tokens.delete_for_student(student_id)

        self.audit.record("portal.reset", actor=actor.get("username"),
                          actor_role=actor.get("role"), subject=student_id,
                          detail="generated" if generated else "supplied")

        described = public_student_account(self.accounts.get(student_id))
        if generated:
            described["password"] = password
            described["password_is_generated"] = True
        return described

    def set_status(self, actor, student_id, status):
        """
        Suspend or restore portal access WITHOUT deleting anything.

        A suspended account keeps its row, its username and its history; the
        student simply cannot sign in. This is the reversible answer and the
        one the dashboard offers first.
        """
        self._require_super_admin(actor)

        if status not in (ACCOUNT_ACTIVE, ACCOUNT_INACTIVE):
            raise AuthInvalid("Status must be ACTIVE or INACTIVE.")

        if self.accounts.get(student_id) is None:
            raise AuthInvalid("%s has no portal login." % student_id)

        with self.database.transaction():
            self.accounts.update(student_id, status=status)
            if status == ACCOUNT_INACTIVE:
                self.tokens.delete_for_student(student_id)

        self.audit.record("portal.status", actor=actor.get("username"),
                          actor_role=actor.get("role"), subject=student_id,
                          detail=status)
        return public_student_account(self.accounts.get(student_id))

    def revoke(self, actor, student_id):
        """
        Delete the login. The STUDENT and their attendance are untouched.

        Worth being explicit about, because "delete" appears twice in this
        project with very different meanings: deleting a student destroys
        their history, and deleting their portal account destroys a password.
        Only the second one is offered here, and it is not the destructive
        operation the word usually names.
        """
        self._require_super_admin(actor)

        if self.accounts.get(student_id) is None:
            raise AuthInvalid("%s has no portal login." % student_id)

        with self.database.transaction():
            self.tokens.delete_for_student(student_id)
            self.accounts.delete(student_id)

        self.audit.record("portal.revoke", actor=actor.get("username"),
                          actor_role=actor.get("role"), subject=student_id)
        return {"student_id": student_id, "revoked": True}

    def list_accounts(self, actor):
        """Every portal login, with the student's name alongside it."""
        self._require_super_admin(actor)

        rows = self.database.query(
            """
            SELECT a.*, s.name, s.class_name, s.section
              FROM student_accounts a
              JOIN students s ON s.student_id = a.student_id
             ORDER BY s.class_name, s.section, s.name
            """)

        accounts = []
        for row in rows:
            record = dict(row)
            described = public_student_account(record)
            described["name"] = record["name"]
            described["class_name"] = record["class_name"]
            described["section"] = record["section"]
            accounts.append(described)
        return accounts

    # -- authentication ----------------------------------------------------

    def login(self, username, password):
        """
        Exchange a username and password for a portal token.

        The same refusal for an unknown username, a wrong password and a
        suspended account, for the same reason the staff side gives one: the
        difference between them is information nobody signing in has earned.
        """
        record = self.accounts.get_by_username((username or "").strip())

        if record is None or not verify_password(password,
                                                 record["password_hash"]):
            time.sleep(FAILURE_DELAY_SECONDS)
            self.audit.record("portal.login_failed",
                              actor=(username or "")[:64] or None,
                              detail="invalid credentials")
            raise AuthFailed("Incorrect username or password.")

        if record["status"] != ACCOUNT_ACTIVE:
            self.audit.record("portal.login_denied",
                              actor=record["username"],
                              subject=record["student_id"],
                              detail="account suspended")
            raise AuthFailed(
                "This portal account has been suspended. Ask the school "
                "office to restore it.")

        token = new_token()
        expires = utc_now() + timedelta(hours=PORTAL_SESSION_HOURS)

        with self.database.transaction():
            self.tokens.purge_expired()
            self.tokens.create(token_fingerprint(token), record["student_id"],
                               _iso(expires))
            self.accounts.touch_login(record["student_id"])

        self.audit.record("portal.login", actor=record["username"],
                          actor_role="STUDENT",
                          subject=record["student_id"])

        return token, self.describe(record["student_id"])

    def logout(self, token):
        if not token:
            return False
        return self.tokens.delete(token_fingerprint(token))

    def resolve(self, token):
        """
        The student behind a portal token, or None.

        Expiry and account status are checked on EVERY request rather than
        only at sign-in, so suspending an account takes effect immediately
        instead of whenever the token happens to run out.

        Returns a dict that is deliberately NOT shaped like a staff user: it
        has a student_id and no role, no user_id and no assignments, so code
        that mistakes one for the other fails rather than over-permits.
        """
        if not token:
            return None

        session = self.tokens.get(token_fingerprint(token))
        if session is None:
            return None

        if session["expires_at"] <= _iso(utc_now()):
            self.tokens.delete(session["token_hash"])
            return None

        record = self.accounts.get(session["student_id"])
        if record is None or record["status"] != ACCOUNT_ACTIVE:
            self.tokens.delete(session["token_hash"])
            return None

        self.tokens.touch(session["token_hash"])
        return self.describe(record["student_id"])

    def describe(self, student_id):
        """
        What the portal shows about its own user. Never a hash, never a
        contact detail, and never another student.
        """
        record = self.accounts.get(student_id)
        if record is None:
            return None

        student = self.database.query_one(
            "SELECT student_id, name, roll_number, class_name, section, "
            "status FROM students WHERE student_id = ?", (student_id,))

        described = public_student_account(record)
        described["must_change"] = bool(record["must_change"])
        described["student"] = dict(student) if student else None
        return described

    def change_own_password(self, student_id, current_password,
                            new_password):
        """
        A student replacing their own password, with the old one required.

        Every other portal session for that student is dropped. If the reason
        for the change is that somebody else learned the password, leaving
        their session alive would undo the point of changing it.
        """
        record = self.accounts.get(student_id)
        if record is None:
            raise AuthInvalid("No portal account.")

        if not verify_password(current_password, record["password_hash"]):
            time.sleep(FAILURE_DELAY_SECONDS)
            raise AuthFailed("The current password is not correct.")

        check_password_strength(new_password)

        if verify_password(new_password, record["password_hash"]):
            raise AuthInvalid("The new password must be different.")

        with self.database.transaction():
            self.accounts.update(student_id,
                                 password_hash=hash_password(new_password),
                                 must_change=0)
            self.tokens.delete_for_student(student_id)

        self.audit.record("portal.password_changed",
                          actor=record["username"], actor_role="STUDENT",
                          subject=student_id)
        return True


def get_portal_service(database):
    return PortalService(database)
