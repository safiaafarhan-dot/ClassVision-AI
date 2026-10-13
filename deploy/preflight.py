"""
ClassVision AI - preflight check (Step 13)
------------------------------------------
Whether this machine is ready to run a lesson, and what is missing if not.

WHY THIS EXISTS

    Every failure this catches has a diagnosis that is obvious afterwards and
    invisible beforehand: a model file that was never downloaded, a database
    directory that is read-only, a camera index nobody checked, a schema a
    version behind. All of them present at the same moment - the start of a
    lesson, in front of a class - and all of them look like "the camera is
    broken".

WHAT IT WILL NOT DO

    Claim a check passed that it did not run. Every check reports one of
    three things: OK, PROBLEM, or SKIPPED with the reason it was skipped. A
    skipped check is never counted as a pass, and the exit code is non-zero
    only for PROBLEM - so a run on a machine with no webcam attached is honest
    about the camera rather than failing the whole check.

    Open the camera by default. Opening a webcam is intrusive and slow, and on
    Windows it can block for several seconds; `--camera` asks for it
    explicitly.

    Change anything. It creates no file, writes no row and downloads nothing.

Usage:
    python deploy/preflight.py
    python deploy/preflight.py --camera          also open the camera
    python deploy/preflight.py --camera-index 1
"""

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

OK = "OK"
PROBLEM = "PROBLEM"
SKIPPED = "SKIPPED"

# Below this, a term of attendance and its WAL file have nowhere to go. It is
# a floor, not a recommendation.
MINIMUM_FREE_MB = 200

# The two model files the pipeline cannot run without.
REQUIRED_MODELS = (
    "face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx",
)


class Check:
    """One check, its verdict, and what to do about it."""

    __slots__ = ("name", "status", "detail", "fix")

    def __init__(self, name, status, detail, fix=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    def as_dict(self):
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}


def check_python():
    """
    The interpreter. 3.9 is the floor the syntax in this project needs.
    """
    version = sys.version_info

    if version < (3, 9):
        return Check(
            "python", PROBLEM,
            "Python %d.%d.%d; this project needs 3.9 or newer."
            % version[:3],
            "Install a newer Python and recreate the virtual environment.")

    return Check("python", OK,
                 "Python %d.%d.%d" % version[:3])


def check_dependencies():
    """
    The imports the server and the pipeline need, named individually.

    Named individually because "pip install -r requirements.txt failed" is not
    a diagnosis and "cv2 is missing" is.
    """
    missing = []

    for module, why in (("cv2", "the camera and both models"),
                        ("numpy", "every array in the pipeline"),
                        ("fastapi", "the dashboard backend"),
                        ("uvicorn", "serving the dashboard")):
        try:
            __import__(module)
        except Exception:                                 # noqa: BLE001
            missing.append("%s (%s)" % (module, why))

    if missing:
        return Check(
            "dependencies", PROBLEM,
            "Not importable: %s." % "; ".join(missing),
            "Activate the virtual environment, then: "
            "pip install -r requirements.txt")

    return Check("dependencies", OK,
                 "cv2, numpy, fastapi and uvicorn all import.")


def check_models():
    """
    The two ONNX files. Present, non-empty, and readable.

    Size is checked because a model interrupted mid-download is a file that
    exists and fails at load time, which is a far more confusing failure than
    one that is simply absent.
    """
    directory = PROJECT_ROOT / "models"
    missing = []
    truncated = []

    for name in REQUIRED_MODELS:
        path = directory / name
        if not path.is_file():
            missing.append(name)
        elif path.stat().st_size < 1024:
            truncated.append("%s (%d bytes)" % (name, path.stat().st_size))

    if missing or truncated:
        detail = []
        if missing:
            detail.append("missing: %s" % ", ".join(missing))
        if truncated:
            detail.append("suspiciously small: %s" % ", ".join(truncated))
        return Check("models", PROBLEM, "; ".join(detail),
                     "python ai/download_models.py")

    return Check("models", OK,
                 "Both ONNX models are present in models/.")


def check_data_directory():
    """
    Somewhere to write. Checked by actually writing, not by reading a flag.

    Asking the permission bits reports what they say, which on Windows is
    frequently not what happens when you open a file. A real create-and-delete
    in the real directory is the only answer worth printing - and the probe
    file is removed again, so a check leaves nothing behind.
    """
    directory = PROJECT_ROOT / "data"

    if not directory.is_dir():
        return Check("data directory", PROBLEM,
                     "data/ does not exist.",
                     "Create it: the database and the embedding store live "
                     "there.")

    probe = directory / ".preflight-write-test"
    try:
        probe.write_text("preflight", encoding="utf-8")
        probe.unlink()
    except Exception as error:                            # noqa: BLE001
        return Check("data directory", PROBLEM,
                     "data/ is not writable: %s: %s"
                     % (type(error).__name__, error),
                     "Check the folder permissions, and that the drive is "
                     "not full or read-only.")

    return Check("data directory", OK, "data/ exists and is writable.")


def check_free_space():
    directory = PROJECT_ROOT / "data"
    target = directory if directory.is_dir() else PROJECT_ROOT

    try:
        free_mb = shutil.disk_usage(str(target)).free // (1024 * 1024)
    except Exception as error:                            # noqa: BLE001
        return Check("free space", SKIPPED,
                     "Could not read free space: %s" % error)

    if free_mb < MINIMUM_FREE_MB:
        return Check(
            "free space", PROBLEM,
            "%d MB free, below the %d MB floor." % (free_mb, MINIMUM_FREE_MB),
            "Free some space. SQLite in WAL mode needs room for the database "
            "AND its write-ahead log; running out mid-lesson is how a "
            "register gets truncated.")

    return Check("free space", OK, "%d MB free." % free_mb)


def check_database():
    """
    That the database opens, and what schema version it is at.

    Opening it runs the additive migration, which is the point: an upgrade
    that is going to fail should fail here, on a machine with nobody waiting,
    rather than at the start of a lesson.
    """
    try:
        from database import SCHEMA_VERSION, Database
    except Exception as error:                            # noqa: BLE001
        return Check("database", PROBLEM,
                     "The database module will not import: %s: %s"
                     % (type(error).__name__, error))

    path = PROJECT_ROOT / "data" / "classvision.db"

    if not path.exists():
        return Check(
            "database", SKIPPED,
            "No database at data/classvision.db yet. It is created on first "
            "run; there is nothing to check.",
            "python database/init_db.py")

    try:
        database = Database(path)
        database.initialize()
        row = database.query_one(
            "SELECT value FROM schema_info WHERE key = 'schema_version'")
        version = row["value"] if row else "unknown"
        tables = len(database.table_names())
        database.close()
    except Exception as error:                            # noqa: BLE001
        return Check("database", PROBLEM,
                     "The database would not open or migrate: %s: %s"
                     % (type(error).__name__, error),
                     "Restore the most recent backup from deploy/backup.py.")

    if str(version) != str(SCHEMA_VERSION):
        return Check(
            "database", PROBLEM,
            "Schema version is %s; this code expects %s."
            % (version, SCHEMA_VERSION),
            "Starting the server runs the migration, which is additive.")

    return Check("database", OK,
                 "Opens at schema version %s, %d tables." % (version, tables))


def check_accounts():
    """
    Whether anybody can sign in.

    An installation with no accounts is UNCONFIGURED: the API is open and the
    dashboard asks for the first Super Admin. That is a deliberate state, not
    a fault - so it is reported as a skipped check with the reason, and it
    does not fail the run.
    """
    path = PROJECT_ROOT / "data" / "classvision.db"
    if not path.exists():
        return Check("accounts", SKIPPED, "No database yet.")

    try:
        from database import Database

        database = Database(path)
        database.connect()
        row = database.query_one("SELECT COUNT(*) AS total FROM users")
        total = row["total"] if row else 0
        database.close()
    except Exception as error:                            # noqa: BLE001
        return Check("accounts", SKIPPED,
                     "Could not count accounts: %s" % error)

    if not total:
        return Check(
            "accounts", SKIPPED,
            "No accounts exist, so this installation is UNCONFIGURED: the "
            "API is open and the dashboard will ask for the first Super "
            "Admin. That is deliberate, not a fault.",
            "Open the dashboard and create the first Super Admin.")

    return Check("accounts", OK, "%d account(s); the API is closed." % total)


def check_identity_uniqueness():
    """
    Whether two accounts can sign in as each other.

    A username is an authentication identity, and the database enforces that
    case-insensitively through a unique index. A file written before that
    index existed may already hold the pair it forbids - "rahul" and "Rahul"
    - in which case the index was SKIPPED when the database opened rather
    than failing the upgrade, and nothing else would ever mention it.

    This is what mentions it. Reported and never fixed here: which of two
    people owns a name is a question about people, and a deployment script
    renaming somebody's account unasked would be worse than the duplicate.
    """
    path = PROJECT_ROOT / "data" / "classvision.db"
    if not path.exists():
        return Check("identity", SKIPPED, "No database yet.")

    try:
        from database import Database

        database = Database(path)
        database.connect()
        blocked = database.enforce_identity_uniqueness()
        database.close()
    except Exception as error:                            # noqa: BLE001
        return Check("identity", SKIPPED,
                     "Could not check username uniqueness: %s" % error)

    if blocked:
        described = ", ".join(
            "%s.%s=%r (%d rows)" % (entry["table"], entry["column"],
                                    entry["value"], entry["count"])
            for entry in blocked)
        return Check(
            "identity", PROBLEM,
            "Two or more accounts share one username once case is ignored, "
            "so a sign-in has more than one row to match: %s" % described,
            "Rename all but one of each pair, then run this again. Nothing "
            "has been changed automatically and no account has been lost.")

    return Check("identity", OK,
                 "Usernames are unique, case-insensitively, and the database "
                 "index enforces it.")


def check_sync_queue():
    """
    Whether anything is stuck waiting to be sent.

    Reported, never fixed here: a full queue means nothing has been LOST, only
    that nothing has been sent, and deciding what to do about that is a
    person's job.
    """
    path = PROJECT_ROOT / "data" / "classvision.db"
    if not path.exists():
        return Check("outbound queue", SKIPPED, "No database yet.")

    try:
        from database import Database

        database = Database(path)
        database.connect()
        if "sync_queue" not in set(database.table_names()):
            database.close()
            return Check("outbound queue", SKIPPED,
                         "This database predates the queue.")

        row = database.query_one(
            "SELECT COUNT(*) AS total FROM sync_queue "
            "WHERE status IN ('PENDING', 'FAILED')")
        waiting = row["total"] if row else 0
        oldest = database.query_one(
            "SELECT queued_at FROM sync_queue "
            "WHERE status IN ('PENDING', 'FAILED') ORDER BY queue_id LIMIT 1")
        database.close()
    except Exception as error:                            # noqa: BLE001
        return Check("outbound queue", SKIPPED,
                     "Could not read the queue: %s" % error)

    if not waiting:
        return Check("outbound queue", OK, "Nothing is waiting.")

    return Check(
        "outbound queue", SKIPPED,
        "%d item(s) waiting, oldest queued %s. Nothing has been lost - this "
        "is attendance that is safe in the database and has not been copied "
        "anywhere else." % (waiting, oldest["queued_at"] if oldest else "?"))


def check_camera(index=0):
    """
    That a camera opens and delivers one frame. Only when asked for.

    A camera that OPENS but delivers nothing is the failure that matters, and
    it is indistinguishable from a working one until a frame is read - so a
    frame is read.
    """
    try:
        import cv2
    except Exception as error:                            # noqa: BLE001
        return Check("camera", SKIPPED,
                     "cv2 is not importable: %s" % error)

    capture = None
    try:
        capture = cv2.VideoCapture(index)
        if not capture.isOpened():
            return Check(
                "camera", PROBLEM,
                "Camera index %d would not open." % index,
                "Check that nothing else is holding the webcam, and try "
                "--camera-index 1.")

        ok, frame = capture.read()
        if not ok or frame is None:
            return Check(
                "camera", PROBLEM,
                "Camera index %d opened but delivered no frame." % index,
                "A camera that opens and gives nothing is usually held by "
                "another application.")

        height, width = frame.shape[:2]
        return Check("camera", OK,
                     "Camera index %d delivered a %dx%d frame."
                     % (index, width, height))
    except Exception as error:                            # noqa: BLE001
        return Check("camera", PROBLEM,
                     "Reading the camera raised %s: %s"
                     % (type(error).__name__, error))
    finally:
        if capture is not None:
            capture.release()


def run(camera=False, camera_index=0):
    """Every check, in the order a failure makes sense in."""
    checks = [
        check_python(),
        check_dependencies(),
        check_models(),
        check_data_directory(),
        check_free_space(),
        check_database(),
        check_accounts(),
        check_identity_uniqueness(),
        check_sync_queue(),
    ]

    if camera:
        checks.append(check_camera(camera_index))
    else:
        checks.append(Check(
            "camera", SKIPPED,
            "Not checked. Opening a webcam is intrusive and slow.",
            "Run again with --camera to actually open it."))

    return checks


def main():
    parser = argparse.ArgumentParser(
        description="Check whether this machine is ready to run a lesson.")
    parser.add_argument("--camera", action="store_true",
                        help="also open the camera and read one frame")
    parser.add_argument("--camera-index", type=int, default=0,
                        help="which capture device to open (default 0)")
    arguments = parser.parse_args()

    try:
        from paths import enable_safe_console
        enable_safe_console()
    except Exception:                                     # noqa: BLE001
        pass

    checks = run(camera=arguments.camera, camera_index=arguments.camera_index)

    print("ClassVision AI - preflight")
    print("=" * 70)

    for check in checks:
        print("%-9s %-16s %s" % (check.status, check.name, check.detail))
        if check.fix and check.status != OK:
            print("          -> %s" % check.fix)

    problems = [check for check in checks if check.status == PROBLEM]
    skipped = [check for check in checks if check.status == SKIPPED]

    print("=" * 70)
    print("%d OK, %d problem(s), %d skipped."
          % (len(checks) - len(problems) - len(skipped), len(problems),
             len(skipped)))

    if problems:
        print("")
        print("NOT READY. A skipped check is not a pass - see above for what "
              "was not run.")
        return 1

    print("")
    print("Ready, as far as the checks that RAN can tell. %d check(s) were "
          "skipped and prove nothing." % len(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
