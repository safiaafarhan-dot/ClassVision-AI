"""
ClassVision AI - Create an Administrator Account
-------------------------------------------------
The command-line half of first-run setup.

    python database/create_admin.py
    python database/create_admin.py --username priya
    python database/create_admin.py --list
    python database/create_admin.py --reset-password priya

The dashboard asks for the first Super Admin on its own, and that is the
normal way in. This exists for the cases a browser cannot cover:

  - the server is running older code and has no /api/auth endpoints yet;
  - somebody needs an account created without a browser;
  - the only administrator's password has been forgotten, and the
    alternative would be editing the database by hand.

    There is NO default account and NO default password anywhere in this
    project. Every account exists because a person ran this, or filled in the
    dashboard's first-run form. A shipped default that nobody changes is the
    same as no password at all.

What this does NOT do
    It does not print, recover or display an existing password. Passwords are
    stored only as PBKDF2-HMAC-SHA256 hashes with a per-user salt, and a hash
    cannot be turned back into the password that made it. --reset-password
    sets a NEW one; it cannot tell you the old one.

Security note
    Running this requires write access to the database file. On a local
    classroom machine that is the same permission as editing the file with any
    SQLite tool, so this adds no privilege that was not already there - it
    just does the job correctly, with the project's own hashing, instead of
    inviting somebody to improvise.
"""

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for directory in (PROJECT_ROOT, PROJECT_ROOT / "database", PROJECT_ROOT / "ai"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from database import DEFAULT_DATABASE_PATH, Database  # noqa: E402


def _service(database):
    """
    The same AuthService the API uses.

    Imported here rather than at module level so that --help works on a
    machine where the backend's dependencies are not importable, and so this
    file cannot become a second implementation of password hashing.
    """
    from backend.services.auth_service import AuthService
    return AuthService(database)


def ask_password(confirm=True):
    """
    Read a password without echoing it, and never from a command-line
    argument by default - an argument ends up in the shell history.
    """
    password = getpass.getpass("Password (at least 10 characters): ")

    if confirm:
        again = getpass.getpass("Confirm password: ")
        if password != again:
            print("\nThe two passwords do not match. Nothing was created.")
            return None

    return password


def list_accounts(service):
    accounts = service.users.list_all()

    if not accounts:
        print("No accounts exist yet.")
        print("Create the first one with:  python database/create_admin.py")
        print("\nOr open the dashboard - it asks for the first Super Admin on "
              "its own.")
        return 0

    print("%-6s %-24s %-12s %-10s %s"
          % ("ID", "USERNAME", "ROLE", "STATUS", "LAST SIGNED IN"))
    print("-" * 78)
    for account in accounts:
        print("%-6s %-24s %-12s %-10s %s"
              % (account["user_id"], account["username"], account["role"],
                 account["status"], account["last_login_at"] or "never"))

    print("\n%d account(s). Passwords are stored as PBKDF2 hashes and cannot "
          "be displayed." % len(accounts))
    print("Forgotten one? Set a new password with:")
    print("    python database/create_admin.py --reset-password <username>")
    return 0


def reset_password(service, username, password=None):
    from backend.services.auth_service import AuthInvalid, hash_password

    account = service.users.get_by_username(username)
    if account is None:
        print("No account called %r." % username)
        print("List the accounts with:  python database/create_admin.py --list")
        return 1

    password = password or ask_password()
    if password is None:
        return 1

    try:
        from backend.services.auth_service import check_password_strength
        check_password_strength(password)
    except AuthInvalid as error:
        print("\n%s" % error)
        return 1

    with service.database.transaction():
        service.users.update(account["user_id"],
                             password_hash=hash_password(password))
        # Every open session for this account stops working, because a
        # password reset is usually a response to losing control of one.
        service.tokens.delete_for_user(account["user_id"])

    service.audit.record("user.password_reset", actor="create_admin.py",
                         subject=username, detail="reset from the command line")

    print("\nPassword changed for %s." % username)
    print("Any sessions that account had open have been signed out.")
    return 0


def create(service, username, role, password=None, name=None):
    from backend.services.auth_service import (
        AuthConflict,
        AuthInvalid,
        ROLE_SUPER_ADMIN,
    )

    first = not service.is_configured()

    if not first and role == ROLE_SUPER_ADMIN:
        # Not a refusal, just honesty about what is happening: the bootstrap
        # endpoint is one-shot, but this tool is administrative and may
        # legitimately add a second administrator.
        print("This installation already has %d account(s); adding another."
              % service.users.count())

    password = password or ask_password()
    if password is None:
        return 1

    try:
        if first:
            account = service.bootstrap_super_admin(username, name or username,
                                                    password)
        else:
            account = service._create_user(username, name or username, role,
                                           password, created_by="create_admin.py")
            service.audit.record("user.create", actor="create_admin.py",
                                 subject=username, detail="role=%s" % role)
    except AuthConflict as error:
        print("\n%s" % error)
        return 1
    except AuthInvalid as error:
        print("\n%s" % error)
        return 1

    print("\nAccount created.")
    print("  username : %s" % account["username"])
    print("  role     : %s" % account["role"])
    print("  status   : %s" % account["status"])
    print("\nThe password is stored only as a PBKDF2 hash. It is not written "
          "to this terminal,")
    print("to a log, or to any file, and it cannot be recovered - only "
          "replaced.")
    print("\nSign in at http://127.0.0.1:8000/")
    return 0


def main():
    from paths import enable_safe_console, relative
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Create or repair a ClassVision AI administrator account. "
                    "Never prints or recovers an existing password.")
    parser.add_argument("--path", default=None,
                        help="database file (default: data/classvision.db)")
    parser.add_argument("--username", default=None,
                        help="the username to create (asked for if omitted)")
    parser.add_argument("--name", default=None,
                        help="display name (defaults to the username)")
    parser.add_argument("--role", default="SUPER_ADMIN",
                        choices=["SUPER_ADMIN", "TEACHER"])
    parser.add_argument("--password", default=None,
                        help="NOT RECOMMENDED - a password given here is "
                             "visible in your shell history. Omit it and you "
                             "will be prompted without echo.")
    parser.add_argument("--list", action="store_true",
                        help="list the accounts that exist (no passwords)")
    parser.add_argument("--reset-password", default=None, metavar="USERNAME",
                        help="set a NEW password for an existing account")
    args = parser.parse_args()

    path = Path(args.path) if args.path else DEFAULT_DATABASE_PATH

    if not path.exists():
        print("No database at %s." % relative(path))
        print("Create it with:  python database/init_db.py")
        return 1

    database = Database(path)

    try:
        database.connect()
        # Brings a pre-accounts database up to schema version 2 so the users
        # table exists. Additive only; nothing is dropped or reset.
        database.initialize()

        service = _service(database)

        print("ClassVision AI accounts")
        print("  database : %s\n" % relative(path))

        if args.list:
            return list_accounts(service)

        if args.reset_password:
            return reset_password(service, args.reset_password, args.password)

        username = args.username
        if not username:
            username = input("Username for the new account: ").strip()
        if not username:
            print("A username is required. Nothing was created.")
            return 1

        return create(service, username, args.role, args.password, args.name)

    except KeyboardInterrupt:
        print("\nCancelled. Nothing was created.")
        return 1
    except Exception as error:                           # noqa: BLE001
        print("ERROR: %s: %s" % (type(error).__name__, error))
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    sys.exit(main())
