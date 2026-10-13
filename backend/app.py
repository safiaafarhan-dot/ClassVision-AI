"""
ClassVision AI - Dashboard Backend (Step 7)
-------------------------------------------
    Step 7 is a local teacher/admin dashboard and deterministic analytics
    layer.

    Local password authentication IS implemented (PBKDF2 hashing,
    session cookies, SUPER_ADMIN / TEACHER roles enforced server-side).
    It has NOT been independently reviewed and is not evaluated
    production security.

    Attendance accuracy has NOT been validated on a real classroom population.

Run it:
    uvicorn backend.app:app --reload
    then open http://127.0.0.1:8000/

API documentation (development):
    http://127.0.0.1:8000/docs

What this service does and does not do
    It reads the SQLite database that Step 6 wrote and serves it to a local
    dashboard. It makes no attendance decisions, touches no camera, loads no
    model, and calls no external service.

    The single mutating endpoint is the manual override, which Step 6 already
    supports with a full audit trail.

Security boundary
    Local password authentication IS now implemented: PBKDF2-HMAC-SHA256
    password hashing, HttpOnly session cookies, and two roles - SUPER_ADMIN
    and TEACHER - enforced on the server rather than by hiding buttons.

    It has NOT been penetration-tested or independently reviewed, there is no
    two-factor authentication and no password reset, and it binds to localhost
    by default. It is real authentication for a local classroom appliance; it
    is not evaluated production security, and the dashboard says so.

    A database with no accounts in it is UNCONFIGURED: the API stays open and
    the dashboard asks for the first Super Admin. That is the state every
    installation predating this change is in, and the alternative - shipping a
    default administrator password - is worse in every way that matters. There
    is no route back into that state once an account exists.

    Also enforced: parameterised SQL everywhere, validated and bounded query
    parameters, whitelisted sort columns, no stack traces or SQL in responses,
    and no biometric field in any schema.
"""

import sys
from pathlib import Path

# Path bootstrap - see backend/__init__.py for why the order matters.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend  # noqa: F401,E402  (installs ai/ and database/ on sys.path)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from backend.routes import (  # noqa: E402
    academic,
    analytics,
    assistant,
    auth,
    health,
    live,
    operations,
    portal,
    reports,
    sessions,
    students,
    sync,
    users,
)
from backend.services.state import get_database  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

DESCRIPTION = """
Local teacher/admin dashboard for ClassVision AI.

Serves attendance decisions and deterministic analytics produced by Steps 1-6.

**Local authentication is implemented** - PBKDF2 password hashing, session
cookies, and SUPER_ADMIN / TEACHER roles enforced server-side. It has not been
independently reviewed and is not evaluated production security. An
installation with no accounts is open, and the dashboard asks for the first
Super Admin.

**Attendance accuracy has not been validated on a real classroom population.**

**Step 8 adds a live classroom session.** The camera runs in a background
worker; the API exposes only counters, states and verified student ids.

**Step 9 adds liveness / presentation attack detection.** It is a RESEARCH
PROTOTYPE, has not been evaluated against real presentation attacks, and is not
a security control.

**Step 10 adds video authenticity analysis.** It is a RESEARCH BASELINE, not a
deepfake detector, has never been evaluated against real manipulated video, and
is OFF by default.

**Step 13 adds an offline queue.** Attendance recorded while a network was
down is queued in the same database it was written to, with an idempotency key
per item so a retry cannot deliver the same fact twice. **Nothing in this
project has ever synchronised to a real server**: the default target is
configured with nowhere to send anything and delivers nothing, which is the
honest behaviour of an installation that has not been given an upstream.

**Step 12 adds an assistant over the analytics.** It answers questions by
CALLING the analytics functions, never by computing its own figure, and every
answer names the function behind it and where the dashboard shows the same
number. It is a pattern matcher over a published, closed list of questions -
there is no language model here and no network call - and a question outside
that list is answered with "not understood" rather than with a guess.

Identity, liveness and authenticity are reported as three separate facts: a
recognised student is not automatically live, a live face is not automatically
anybody, and video that came from this camera is a different question again.

No endpoint returns face embeddings, face crops, camera frames, video, file
paths, or credentials.
"""

app = FastAPI(
    title="ClassVision AI Dashboard API",
    description=DESCRIPTION,
    version="0.10.0",
    docs_url="/docs",
    redoc_url=None,
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(sessions.router)
app.include_router(students.router)
app.include_router(analytics.router)
app.include_router(live.router)
app.include_router(academic.router)
app.include_router(operations.router)
app.include_router(portal.router)
app.include_router(reports.router)
app.include_router(assistant.router)
app.include_router(sync.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, error: Exception):
    """
    Never leak a stack trace, a SQL statement, or a file path to a client.

    The real error still goes to the server log, where a developer can see it.
    """
    print("ERROR %s %s: %s: %s" % (request.method, request.url.path,
                                   type(error).__name__, error),
          file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"})


@app.on_event("startup")
def startup():
    """
    Open the database, bring its schema forward, and fill in the registry.

    get_database() runs the schema migration - additive ALTER TABLE only - so
    a database created before the student registry columns existed gains them
    on first start rather than failing on a missing column.

    The second step copies roll number, class and section out of existing face
    enrollments and into the students they belong to, for students who were
    enrolled before those columns existed. It fills blanks only, never
    overwrites, and does nothing at all on a database that is already in step,
    so it is safe on every start.

    Both are best effort with respect to STARTING: a migration that cannot run
    is reported to the log and the server still comes up, because a dashboard
    that refuses to start is harder to diagnose than one that starts and says
    what is wrong.
    """
    database = get_database()

    try:
        from migrate_students import synchronise_registry

        with database.transaction():
            report = synchronise_registry(database)

        if report["created"] or report["filled"]:
            print("Student registry: registered %d, filled metadata for %d."
                  % (len(report["created"]), len(report["filled"])),
                  file=sys.stderr)
    except Exception as error:                           # noqa: BLE001
        print("Student registry migration skipped: %s: %s"
              % (type(error).__name__, error), file=sys.stderr)

    try:
        from model_registry import ModelRegistry

        # Records SFace as the production recogniser, so the registry can
        # always answer "what is running". Idempotent, and it never demotes a
        # model somebody deliberately activated.
        ModelRegistry(database).ensure_baseline()
    except Exception as error:                           # noqa: BLE001
        print("Model registry unavailable: %s: %s"
              % (type(error).__name__, error), file=sys.stderr)


@app.on_event("shutdown")
def shutdown():
    """
    Give the camera back before the process exits.

    A webcam held by a dead worker stays locked until the process dies, so
    releasing it here is not tidiness - it is what lets the next run start.
    Shutting down deliberately does NOT finalise a live session: closing an
    application is not a teacher saying the lesson ended.
    """
    from backend.services.enrollment_service import get_enrollment_manager
    from backend.services.live_service import get_live_manager
    from backend.services.state import close_database

    get_live_manager().shutdown()
    get_enrollment_manager().shutdown()
    close_database()


# The dashboard itself. Mounted last so it never shadows an /api route.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(str(STATIC_DIR / "index.html"))
