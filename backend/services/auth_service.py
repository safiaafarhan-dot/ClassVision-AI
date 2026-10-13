"""
ClassVision AI - Authentication and Accounts
---------------------------------------------
Local sign-in for the dashboard: password hashing, sign-in sessions, and the
Super Admin / Teacher accounts themselves.

    Until now the dashboard said "Authentication is not implemented", which
    was true and was the honest thing to say. This module implements it, and
    the disclaimer changes to describe what actually exists rather than
    disappearing.

What is actually protecting anything

    Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user random salt and
    a deliberately slow iteration count. That is a standard, well-understood
    construction from the Python standard library - NO new dependency - and it
    is what makes a stolen database file not immediately a list of passwords.

    Sign-in tokens are 256 bits from secrets.token_urlsafe. Only the SHA-256
    of a token is stored, so reading user_sessions does not let anybody sign
    in as somebody else. Comparisons use compare_digest, so a wrong token
    takes the same time to reject as a nearly-right one.

What is NOT claimed
    This has not been penetration-tested or independently reviewed. There is
    no rate limiting beyond a per-account failure delay, no two-factor
    authentication, no password-reset email, and cookies are only marked
    Secure when the request arrived over HTTPS. It is real authentication for
    a local classroom appliance; it is not evaluated production security, and
    the dashboard says so.

The unconfigured state, and why it is allowed

    A database with NO accounts in it is treated as unconfigured: the API
    stays open, and the dashboard shows a first-run screen asking for the
    first Super Admin. That is the state every existing installation is in
    right now, and the alternative - shipping a default administrator password
    - is worse in every way that matters.

    The moment one account exists, enforcement is total and there is no way
    back to the open state through the API. See backend/services/authz.py.

Privacy
    A password never reaches a log, an audit row, an exception message or an
    API response, and neither does a hash. public_user() in the repository
    layer is the only shape an account leaves this process in.
"""

import hashlib
import hmac
import os
import secrets
import threading
import time
import sqlite3
from datetime import datetime, timedelta, timezone

from repositories import (
    ROLE_SUPER_ADMIN,
    ROLE_TEACHER,
    USER_ACTIVE,
    USER_INACTIVE,
    AuditRepository,
    SessionTokenRepository,
    UserRepository,
    public_user,
)

# PBKDF2 cost. High enough to make offline guessing expensive, low enough that
# a teacher signing in on classroom hardware does not notice. Stored in the
# hash string, so raising it later does not invalidate existing passwords.
PBKDF2_ITERATIONS = 240_000
PBKDF2_ALGORITHM = "sha256"
SALT_BYTES = 16

HASH_PREFIX = "pbkdf2_sha256"

# Sign-in lifetime. A classroom machine is often left signed in, so this is
# deliberately not generous.
SESSION_HOURS = 12

TOKEN_BYTES = 32
SESSION_COOKIE = "classvision_session"

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 64

# A wrong password costs this long, always. It is not rate limiting - it is a
# floor under how fast an attacker can work through a list on a machine that
# is answering nothing else.
FAILURE_DELAY_SECONDS = 0.35


class AuthError(Exception):
    """Base class. Every message is safe to show the person who typed it."""


class AuthInvalid(AuthError):
    """The request cannot describe a usable account or credential."""


class AuthFailed(AuthError):
    """The credentials were not accepted."""


class AuthConflict(AuthError):
    """Well-formed, but it collides with something that already exists."""


class AuthForbidden(AuthError):
    """The caller is known, and is not allowed to do this."""


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds")


# -- passwords --------------------------------------------------------------

def hash_password(password, iterations=PBKDF2_ITERATIONS, salt=None):
    """
    Derive a storable hash. The salt and cost travel with it.

    Format:  pbkdf2_sha256$<iterations>$<salt hex>$<derived hex>

    Self-describing on purpose: raising the iteration count later must not
    invalidate every existing password, and a hash that does not say how it
    was made cannot be verified after the constant changes.
    """
    if not isinstance(password, str):
        raise AuthInvalid("A password is required.")

    salt = salt or os.urandom(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"),
                                  salt, iterations)
    return "%s$%d$%s$%s" % (HASH_PREFIX, iterations, salt.hex(), derived.hex())


def verify_password(password, stored):
    """
    Check a password against a stored hash, in constant time.

    Returns False rather than raising for a malformed stored value: a corrupt
    row must fail the sign-in, not crash the endpoint.
    """
    if not isinstance(password, str) or not isinstance(stored, str):
        return False

    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != HASH_PREFIX:
        return False

    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except (ValueError, TypeError):
        return False

    derived = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"),
                                  salt, iterations)
    return hmac.compare_digest(derived, expected)


def check_password_strength(password):
    """
    The smallest rule set that is worth enforcing.

    Length does more for a password than a character-class rule does, so this
    asks for length and refuses the handful of strings people actually pick
    when told to hurry.
    """
    if not isinstance(password, str) or not password:
        raise AuthInvalid("A password is required.")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthInvalid(
            "The password must be at least %d characters long."
            % MIN_PASSWORD_LENGTH)

    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthInvalid(
            "The password is too long (limit %d characters)."
            % MAX_PASSWORD_LENGTH)

    lowered = password.strip().lower()
    if lowered in ("password", "password123", "classvision", "12345678910",
                   "administrator", "changeme", "letmein123"):
        raise AuthInvalid(
            "That password is too easy to guess. Choose something else.")

    return password


def check_username(username):
    username = (username or "").strip()

    if len(username) < MIN_USERNAME_LENGTH:
        raise AuthInvalid(
            "The username must be at least %d characters long."
            % MIN_USERNAME_LENGTH)

    if len(username) > MAX_USERNAME_LENGTH:
        raise AuthInvalid(
            "The username is too long (limit %d characters)."
            % MAX_USERNAME_LENGTH)

    if any(character.isspace() for character in username):
        raise AuthInvalid("The username cannot contain spaces.")

    return username


# -- tokens -----------------------------------------------------------------

def new_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token):
    """What gets stored. The token itself never reaches the database."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class AuthService:
    """
    Accounts, sign-in and the rules about who may change whom.

    Every guard in here exists because of a specific way an installation can
    be broken: deactivating the last administrator, a teacher promoting
    themselves, an account whose open browser tab outlives its access.
    """

    def __init__(self, database):
        self.database = database
        self.users = UserRepository(database)
        self.tokens = SessionTokenRepository(database)
        self.audit = AuditRepository(database)

    # -- state -------------------------------------------------------------

    def is_configured(self):
        """
        Whether anybody can sign in at all.

        False means a fresh installation with no accounts. The API is open in
        that state and the dashboard asks for the first Super Admin; see the
        module docstring for why that is the least bad first-run behaviour.
        """
        return self.users.count() > 0

    def auth_state(self):
        return {
            "configured": self.is_configured(),
            "user_count": self.users.count(),
            "roles": [ROLE_SUPER_ADMIN, ROLE_TEACHER],
        }

    # -- accounts ----------------------------------------------------------

    def bootstrap_super_admin(self, username, display_name, password):
        """
        Create the FIRST account, and only the first.

        Refuses outright once any account exists, so this can never be used to
        mint a second administrator without signing in. That check is the only
        thing standing between "first-run convenience" and "anybody can become
        an administrator", so it is a hard refusal rather than a permission.
        """
        if self.is_configured():
            raise AuthConflict(
                "This installation already has accounts. Sign in as a Super "
                "Admin to create more.")

        user = self._create_user(username, display_name, ROLE_SUPER_ADMIN,
                                 password, created_by="first-run")

        self.audit.record("user.bootstrap", actor=user["username"],
                          actor_role=ROLE_SUPER_ADMIN,
                          subject=user["username"],
                          detail="first Super Admin created")
        return user

    def create_user(self, actor, username, display_name, role, password,
                    assignments=None):
        """
        Create a teacher, or another administrator. Super Admin only.

        A TEACHER cannot reach this method - the route layer refuses first -
        but the role check is repeated here because a permission that exists
        only in a route is a permission that disappears the moment somebody
        adds a second route.
        """
        self._require_super_admin(actor)

        role = (role or ROLE_TEACHER).strip().upper()
        if role not in (ROLE_SUPER_ADMIN, ROLE_TEACHER):
            raise AuthInvalid(
                "Role must be SUPER_ADMIN or TEACHER.")

        user = self._create_user(username, display_name, role, password,
                                 created_by=actor.get("username"))

        if assignments:
            self.users.set_assignments(user["user_id"], assignments)

        self.audit.record("user.create", actor=actor.get("username"),
                          actor_role=actor.get("role"),
                          subject=user["username"], detail="role=%s" % role)

        return self.describe_user(user["user_id"])

    def _create_user(self, username, display_name, role, password,
                     created_by=None):
        username = check_username(username)
        display_name = (display_name or "").strip() or username

        if len(display_name) > 120:
            raise AuthInvalid("The name is too long (limit 120 characters).")

        check_password_strength(password)

        # The check is for the message; the UNIQUE constraint is the
        # protection. Two administrators creating the same username at the
        # same moment both read "available" here, and only one of them gets
        # to write it.
        if self.users.get_by_username(username) is not None:
            raise AuthConflict(
                "The username %s is already taken." % username)

        try:
            with self.database.transaction():
                record = self.users.create(username, display_name, role,
                                           hash_password(password),
                                           created_by=created_by)
        except sqlite3.IntegrityError:
            # Whoever lost the race is told the same thing the pre-check says,
            # rather than being shown a 500 for a condition the system
            # understands perfectly well. The account that was created first
            # is left exactly as it is; nothing is overwritten and nothing is
            # merged.
            raise AuthConflict(
                "The username %s is already taken." % username)

        return public_user(record)

    def describe_user(self, user_id):
        """One account, with its class assignments, and never its hash."""
        record = self.users.get(user_id)
        if record is None:
            return None

        described = public_user(record)
        described["assignments"] = self.users.assignments(user_id)
        return described

    def list_users(self, actor):
        self._require_super_admin(actor)
        return [self.describe_user(record["user_id"])
                for record in self.users.list_all()]

    def update_user(self, actor, user_id, display_name=None, role=None,
                    status=None, password=None, assignments=None,
                    can_view_all_students=None):
        """
        Change an account. Super Admin only, with two guards.

        The guards are about lockout, not about hierarchy: an installation
        with no usable administrator cannot be repaired through the dashboard,
        only by editing the database by hand, which is exactly the thing this
        whole feature exists to stop people having to do.
        """
        self._require_super_admin(actor)

        target = self.users.get(user_id)
        if target is None:
            raise AuthInvalid("No such user.")

        if role is not None:
            role = role.strip().upper()
            if role not in (ROLE_SUPER_ADMIN, ROLE_TEACHER):
                raise AuthInvalid("Role must be SUPER_ADMIN or TEACHER.")

        losing_admin = (
            target["role"] == ROLE_SUPER_ADMIN
            and ((status == USER_INACTIVE) or (role == ROLE_TEACHER)))

        if losing_admin and self.users.count_active_super_admins(
                excluding=user_id) == 0:
            raise AuthForbidden(
                "This is the last active Super Admin. Create or activate "
                "another one first, or nobody will be able to administer "
                "this system.")

        fields = {}
        if display_name is not None:
            fields["display_name"] = display_name.strip()
        if role is not None:
            fields["role"] = role
        if status is not None:
            fields["status"] = status
        if password is not None:
            check_password_strength(password)
            fields["password_hash"] = hash_password(password)
        if can_view_all_students is not None:
            # Stored as 0/1 so the column stays an INTEGER the SQL can filter
            # on, rather than a Python bool SQLite would coerce silently.
            fields["can_view_all_students"] = 1 if can_view_all_students else 0

        with self.database.transaction():
            if fields:
                self.users.update(user_id, **fields)
            if assignments is not None:
                self.users.set_assignments(user_id, assignments)

            # A revoked or re-roled account must stop working now, not at the
            # next restart. Its open tabs are signed out here.
            if (status == USER_INACTIVE or role is not None or password
                    or can_view_all_students is not None):
                self.tokens.delete_for_user(user_id)

        self.audit.record("user.update", actor=actor.get("username"),
                          actor_role=actor.get("role"),
                          subject=target["username"],
                          detail=",".join(sorted(
                              set(fields) - {"password_hash"}))
                          or ("password" if password else "assignments"))

        return self.describe_user(user_id)

    def delete_user(self, actor, user_id):
        """
        Remove an account entirely.

        Deactivation is the gentler option and the dashboard offers it first,
        but an account created by mistake should be removable rather than
        preserved forever as clutter. Audit rows survive: they record the
        username as text, not as a foreign key, so deleting a user never
        rewrites history.
        """
        self._require_super_admin(actor)

        target = self.users.get(user_id)
        if target is None:
            raise AuthInvalid("No such user.")

        if target["user_id"] == actor.get("user_id"):
            raise AuthForbidden(
                "You cannot delete the account you are signed in with.")

        if (target["role"] == ROLE_SUPER_ADMIN
                and self.users.count_active_super_admins(
                    excluding=user_id) == 0):
            raise AuthForbidden(
                "This is the last active Super Admin. Create another one "
                "before removing this account.")

        with self.database.transaction():
            self.tokens.delete_for_user(user_id)
            self.users.delete(user_id)

        self.audit.record("user.delete", actor=actor.get("username"),
                          actor_role=actor.get("role"),
                          subject=target["username"])
        return True

    def change_own_password(self, actor, current_password, new_password):
        record = self.users.get(actor.get("user_id"))
        if record is None:
            raise AuthFailed("Not signed in.")

        if not verify_password(current_password, record["password_hash"]):
            time.sleep(FAILURE_DELAY_SECONDS)
            raise AuthFailed("The current password is not correct.")

        check_password_strength(new_password)

        with self.database.transaction():
            self.users.update(record["user_id"],
                              password_hash=hash_password(new_password))
            self.tokens.delete_for_user(record["user_id"])

        self.audit.record("user.password", actor=record["username"],
                          actor_role=record["role"], subject=record["username"])
        return True

    # -- sign in and out ---------------------------------------------------

    def login(self, username, password):
        """
        Exchange a username and password for a session token.

        Returns (token, user). The same refusal is given for an unknown
        username, a wrong password and a deactivated account, because telling
        somebody which of the three it was tells them something they have not
        earned. A deactivated account gets its own message only AFTER the
        password has been checked, so the distinction cannot be used to
        enumerate accounts.
        """
        record = self.users.get_by_username((username or "").strip())

        if record is None or not verify_password(password,
                                                 record["password_hash"]):
            time.sleep(FAILURE_DELAY_SECONDS)
            self.audit.record("auth.login_failed",
                              actor=(username or "")[:64] or None,
                              detail="invalid credentials")
            raise AuthFailed("Incorrect username or password.")

        if record["status"] != USER_ACTIVE:
            self.audit.record("auth.login_denied", actor=record["username"],
                              actor_role=record["role"],
                              detail="account deactivated")
            raise AuthFailed(
                "This account has been deactivated. Ask a Super Admin to "
                "reactivate it.")

        token = new_token()
        expires = utc_now() + timedelta(hours=SESSION_HOURS)

        with self.database.transaction():
            self.tokens.purge_expired()
            self.tokens.create(token_fingerprint(token), record["user_id"],
                               _iso(expires))
            self.users.touch_login(record["user_id"])

        self.audit.record("auth.login", actor=record["username"],
                          actor_role=record["role"])

        return token, self.describe_user(record["user_id"])

    def logout(self, token):
        if not token:
            return False
        return self.tokens.delete(token_fingerprint(token))

    def resolve(self, token):
        """
        The account behind a token, or None.

        Checks expiry and account status on EVERY request rather than only at
        sign-in, so deactivating a teacher takes effect immediately instead of
        whenever their token happens to run out.
        """
        if not token:
            return None

        session = self.tokens.get(token_fingerprint(token))
        if session is None:
            return None

        if session["expires_at"] <= _iso(utc_now()):
            self.tokens.delete(session["token_hash"])
            return None

        record = self.users.get(session["user_id"])
        if record is None or record["status"] != USER_ACTIVE:
            self.tokens.delete(session["token_hash"])
            return None

        self.tokens.touch(session["token_hash"])

        described = public_user(record)
        described["assignments"] = self.users.assignments(record["user_id"])
        return described

    # -- guards ------------------------------------------------------------

    @staticmethod
    def _require_super_admin(actor):
        if not actor or actor.get("role") != ROLE_SUPER_ADMIN:
            raise AuthForbidden(
                "Only a Super Admin can manage user accounts.")


# -- a per-request service, same discipline as the data service -------------

_lock = threading.Lock()


def get_auth_service(database):
    return AuthService(database)
