"""
Live classroom session endpoints (Steps 8-9).

Four operations, polled rather than pushed:

    POST /api/live/start      begin a run against an existing session
    GET  /api/live/status     safe operational state, cheap enough to poll
    GET  /api/live/presence   who has been verified, and where they are now
    POST /api/live/stop       stop the camera and finalise attendance

Why polling and not WebSockets
    The dashboard needs one small JSON object about once a second. A WebSocket
    would add a connection lifecycle, a reconnect policy and a second server
    protocol to maintain, in exchange for latency nobody is waiting on. The
    existing dashboard already fetches JSON and has no build step; keeping it
    that way was worth more than the milliseconds.

Step 9 added liveness to these same four operations rather than a fifth
    /status gained liveness counts, /presence gained a per-track view and a
    per-student liveness column, and /stop reports what the eligibility layer
    held back. There is no separate liveness API, because liveness is not a
    separate session - it is another fact about the one that is running.

What these endpoints cannot return
    No frame, no face crop, no embedding, no similarity score, no liveness cue
    value, no model or file path. /status and /presence are built from the
    runtime snapshot, which has no field for any of them.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from backend.schemas.models import (
    LivePresenceResponse,
    LiveStartRequest,
    LiveStatusResponse,
    LiveStopResponse,
)
from backend.services.live_service import (
    LiveSessionConflict,
    LiveSessionNotFound,
    LiveSessionUnavailable,
    get_live_manager,
    get_live_registry,
)
from backend.services.break_service import BreakError, BreakService
from backend.services.state import get_database
from backend.services.authz import Principal, current_principal, require_user

# Authentication is applied to the WHOLE router rather than endpoint by
# endpoint, so a route added later is protected by default instead of by
# somebody remembering. On an installation with no accounts yet, require_user
# lets everything through and the dashboard asks for the first Super Admin -
# see backend/services/authz.py for why that state exists at all.
router = APIRouter(prefix="/api/live", tags=["live"],
                   dependencies=[Depends(require_user)])


@router.post("/start", response_model=LiveStatusResponse,
             summary="Start a live classroom session",
             responses={404: {"description": "Session not found"},
                        409: {"description": "A live session is already "
                                             "running, or this session cannot "
                                             "be run live"},
                        503: {"description": "The camera or a model is not "
                                             "available"}})
def start_live(payload: LiveStartRequest):
    """
    Open the camera and begin processing.

    The camera is opened before this returns, so a missing or busy camera is
    reported immediately instead of appearing to start and failing silently.

    Starting does NOT create a session. The session must already exist and be
    SCHEDULED or ACTIVE - Step 7 already manages sessions, and a second way to
    create them would be a second source of truth.
    """
    database = get_database()
    camera_index = payload.camera_index
    camera_id = (payload.camera_id or "").strip()

    # A registered device is resolved to the index it answers on, so a teacher
    # picks "Lab 2 front camera" rather than remembering that it is index 1.
    #
    # An explicit camera_index still wins. That is what keeps every existing
    # caller working unchanged - the CLI, the tests, and anybody who already
    # knows the number - and it is why registering a device adds a way to
    # start a session without replacing the one that was already there.
    if camera_id:
        from repositories import CameraRepository

        device = CameraRepository(database).get(camera_id)
        if device is None:
            raise HTTPException(
                status_code=404,
                detail="No camera is registered as %s." % camera_id)

        if camera_index is None:
            if device.get("camera_index") is None:
                raise HTTPException(
                    status_code=400,
                    detail="%s is registered without a camera index, so there "
                           "is nothing to open. Edit the device and give it "
                           "the index it answers on." % camera_id)
            camera_index = device["camera_index"]

    try:
        # Keyed by session id, so starting a second class creates a SECOND
        # manager with its own camera, pipeline and worker rather than being
        # refused for clashing with the first.
        status = get_live_manager(payload.session_id).start(
            database, payload.session_id,
            camera_index=camera_index,
            width=payload.width, height=payload.height)
    except LiveSessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except LiveSessionConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except LiveSessionUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))

    # Record against the device that it is feeding this session. Best effort
    # and deliberately AFTER the start succeeded: a camera that failed to open
    # must not be left claiming it is running a lesson.
    if camera_id:
        try:
            from repositories import CameraRepository
            CameraRepository(database).heartbeat(
                camera_id, status="online",
                pipeline_state=status.get("state")
                if isinstance(status, dict) else getattr(status, "state", None),
                session_id=payload.session_id)
        except Exception:                                # noqa: BLE001
            pass

    return status


@router.get("/sessions", summary="Every live session running right now")
def live_sessions():
    """
    One entry per running session, each carrying that session's OWN figures.

    This exists because GET /api/sessions?status=ACTIVE reads the database -
    class, times, who was expected - and knows nothing about detection counts,
    frame rate or liveness, which live in the runtime and are never written to
    a row.

    Nothing here is merged across sessions. The totals at the end are sums the
    caller would otherwise compute itself, and each is labelled as a total;
    every per-class figure stays inside its own entry, because a detection
    count that belonged to two classrooms would be a count of nothing.
    """
    registry = get_live_registry()
    entries = [_with_break_state(entry) for entry in registry.active()]

    return {
        "items": entries,
        "total": len(entries),
        "totals": {
            "active_sessions": len(entries),
            "recognized_students": sum(entry.get("recognized_students") or 0
                                       for entry in entries),
            "active_tracks": sum(entry.get("active_tracks") or 0
                                 for entry in entries),
            "unknown_tracks": sum(entry.get("unknown_tracks") or 0
                                  for entry in entries),
            "cameras_connected": sum(1 for entry in entries
                                     if entry.get("camera") == "connected"),
            "on_break": sum(1 for entry in entries if entry.get("on_break")),
            "failed": sum(1 for entry in entries
                          if entry.get("state") == "FAILED"),
        },
        "note": ("Each entry is one classroom's own runtime. No figure here is "
                 "shared between sessions."),
    }


@router.get("/status", response_model=LiveStatusResponse,
            summary="Live runtime status")
def live_status(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session. Omit for the one that is running.")):
    """
    Operational state only, safe to poll once a second.

    Always answers, including when nothing is running - an IDLE runtime is a
    valid answer, not an error.

    The break state is folded in here rather than served separately, so the
    page's existing one-second poll carries it and there is only ever one
    answer to "is this session on a break".

    With a session_id this answers about THAT session only. Without one it
    answers about the session that is running, which is what it has always
    meant - before the registry there could only be one.
    """
    status = get_live_manager(session_id).status()
    return _with_break_state(status)


def _with_break_state(status):
    """
    Add the session's break state to a runtime status.

    Best effort with respect to the STATUS: a break table that cannot be read
    must not take down the poll the whole live page depends on. It reports no
    break rather than failing, and no break is also the honest default - the
    attendance arithmetic reads the rows itself at finalisation and does not
    trust anything cached here.
    """
    session_id = (status.get("session_id") if isinstance(status, dict)
                  else getattr(status, "session_id", None))

    try:
        state = BreakService(get_database()).state(session_id)
    except Exception:                                    # noqa: BLE001
        return status

    running = state.get("break") or {}

    fields = {
        "on_break": bool(state["active"]),
        "break_remaining_seconds": float(running.get("remaining_seconds") or 0.0),
        "break_started_at": running.get("started_at"),
        "break_ends_at": running.get("ends_at"),
        "break_count": int(state.get("count") or 0),
        "break_excluded_seconds": float(state.get("excluded_seconds") or 0.0),
    }

    if isinstance(status, dict):
        status.update(fields)
    else:
        for name, value in fields.items():
            setattr(status, name, value)

    return status


def _running_session_id():
    """
    The session a break request with no session_id is about.

    Breaks belong to one session each - starting one for CSE-A must not pause
    CSE-B - so a caller with more than one class running is expected to name
    the session. This resolves the single-session case only.
    """
    status = get_live_manager().status()
    return (status.get("session_id") if isinstance(status, dict)
            else getattr(status, "session_id", None))


@router.post("/break", summary="Start a ten-minute break",
             responses={409: {"description": "No running session to "
                                             "interrupt"}})
def start_break(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session. Omit for the one running."),
        principal: Principal = Depends(current_principal)):
    """
    Begin a break in the running session.

    Ten minutes, measured by the SERVER. Idempotent while one is running: the
    answer is the break that is already there, so a double-clicked button
    cannot create two overlapping intervals - which would be subtracted from
    attendance twice.

    Students are not marked absent for a break. The interval is removed from
    both the numerator and the denominator of the attendance ratio, which is
    why this is a database row and not a countdown in the page.
    """
    session_id = session_id or _running_session_id()

    if not session_id:
        raise HTTPException(
            status_code=409,
            detail="No session is running, so there is nothing to interrupt.")

    try:
        return BreakService(get_database()).start(
            session_id, actor=principal.username)
    except BreakError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.post("/break/end", summary="End the break early",
             responses={409: {"description": "No break is running"}})
def end_break(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session. Omit for the one running."),
        principal: Principal = Depends(current_principal)):
    """
    End the running break before its ten minutes are up.

    Supported because a class back in their seats after four minutes should not
    have six minutes of teaching excluded from their attendance. What reaches
    the arithmetic is the interval that actually happened.
    """
    session_id = session_id or _running_session_id()

    if not session_id:
        raise HTTPException(status_code=409,
                            detail="No session is running.")

    try:
        return BreakService(get_database()).end(session_id,
                                                actor=principal.username)
    except BreakError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.get("/presence", response_model=LivePresenceResponse,
            summary="Live presence of verified students")
def live_presence(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session. Omit for the one that is running.")):
    """
    Who has been verified in this session, and where they are right now.

    Four things this response is careful NOT to say:

      - an unverified person is a count and a track row, never a name and
        never a guess;
      - a student who is not visible right now is TEMPORARILY_LOST or
        SEEN_EARLIER, never absent;
      - a track that is not confirmed live is held back for review, which is
        also never absent. Identity and liveness are reported as two separate
        columns precisely so neither can be read as the other;
      - the durations are provisional. Attendance is calculated once, by the
        rule engine, after the session has been stopped and completed.

    Scoped to one session. A student verified in CSE-A is in CSE-A's answer and
    in no other, because the answer comes from that session's own manager.
    """
    return get_live_manager(session_id).presence()


@router.post("/stop", response_model=LiveStopResponse,
             summary="Stop the session and finalise attendance",
             responses={409: {"description": "There is no live session to "
                                             "stop"}})
def stop_live(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session to finish. Omit for the one running.")):
    """
    Stop the camera, run the attendance rules, and store the result.

    The Step 9 eligibility layer runs in front of the rule engine. In the
    default `report` mode it changes nothing and reports what it would have
    held back; in `enforce` mode only trustworthy evidence reaches the engine.
    Either way the evidence itself is still stored, because a teacher cannot
    review a decision whose evidence was deleted.

    Idempotent: calling it again returns the same summary with
    `already_finalized: true` and writes nothing further. A manual correction
    made after finalisation is never overwritten by this call.

    Scoped: finishing CSE-A finishes CSE-A. The manager that is asked is the
    one keyed to the session named, so a Finish button in one panel cannot
    reach into another classroom's lesson.
    """
    try:
        return get_live_manager(session_id).stop(get_database())
    except LiveSessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except LiveSessionConflict as error:
        raise HTTPException(status_code=409, detail=str(error))


# -- the classroom view -----------------------------------------------------
#
# The one place in this API that returns pixels, and it is deliberate: a
# teacher running a live lesson has to be able to see the room. What is served
# is the COMPLETE camera frame with the pipeline's boxes drawn on top - never a
# face crop, never a zoom, never an aligned 112x112 patch. Those exist inside
# the recogniser and stop there.
#
# No second camera is opened. These endpoints read the single slot the capture
# worker publishes into, so the picture costs one JPEG encode per frame and no
# extra inference at all.
#
# Nothing is stored. The bytes live in memory, are replaced by the next frame,
# and are dropped when the camera is released. There is no endpoint that can
# return a PAST frame, because none is kept.

PREVIEW_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    # It is a live view of a classroom. It should not be cached by anything,
    # or embedded by another site.
    "X-Content-Type-Options": "nosniff",
}

MJPEG_BOUNDARY = "classvision-frame"

# multipart/x-mixed-replace is specified in terms of CRLF, and a bare LF is
# tolerated by some browsers and not others. Naming it makes the part builder
# below readable instead of a thicket of escapes.
CRLF = b"\r\n"


@router.get("/frame.jpg", summary="The current classroom view",
            response_class=Response,
            responses={200: {"content": {"image/jpeg": {}},
                             "description": "The complete camera frame with "
                                            "the pipeline's overlays"},
                       503: {"description": "No session is running"}})
def live_frame(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session's camera. Omit for the one running."),
        principal=Depends(require_user)):
    """
    One JPEG: the whole classroom, as the camera sees it, annotated.

    Returns 503 rather than a stale picture when no session is running - a
    dashboard showing a room nobody is watching would be worse than one
    showing nothing.

    SCOPED TO ONE SESSION. The frame comes from the manager keyed to the
    session named, which is the only thing that stops one classroom's panel
    showing another classroom's camera. Each manager publishes into its own
    frame slot; there is no shared one to read from by mistake.
    """
    manager = get_live_manager(session_id)
    jpeg, index, taken_at = manager.runtime.latest_frame()

    if not jpeg:
        raise HTTPException(
            status_code=503,
            detail="No classroom view is available. Start a live session.")

    headers = dict(PREVIEW_HEADERS)
    headers["X-Frame-Index"] = str(index)
    if taken_at is not None:
        headers["X-Frame-At"] = taken_at.isoformat()

    return Response(content=jpeg, media_type="image/jpeg", headers=headers)


@router.get("/stream.mjpg", summary="The classroom view, as a live stream",
            response_class=StreamingResponse,
            responses={200: {"content": {"multipart/x-mixed-replace": {}},
                             "description": "motion JPEG"}})
def live_stream(session_id: Optional[str] = Query(
        None, max_length=200,
        description="Which session's camera. Omit for the one running."),
        principal=Depends(require_user)):
    """
    Motion JPEG, which an ordinary <img> tag can display with no JavaScript,
    no player and no external library.

    Chosen over WebRTC or HLS for the same reason the rest of this dashboard
    is hand-written: it works in every browser, adds no dependency, and the
    whole implementation is the loop below. The cost is bandwidth, which on a
    machine talking to itself is not a cost.

    The generator sends only frames the worker has actually published, so a
    stalled camera produces a still picture rather than a flood, and it stops
    as soon as the session does.
    """
    import time

    # Scoped like the single frame above: this streams ONE session's camera,
    # resolved once here, so a panel cannot start showing a different room if
    # another class starts while it is watching.
    manager = get_live_manager(session_id)

    def frames():
        last_index = -1
        idle_since = time.time()

        while True:
            jpeg, index, _ = manager.runtime.latest_frame()

            if jpeg and index != last_index:
                last_index = index
                idle_since = time.time()

                # A multipart part: boundary, headers, blank line, bytes.
                # Built from a list rather than one long literal so the CRLFs
                # that the format requires are visible and countable.
                header = CRLF.join([
                    b"--" + MJPEG_BOUNDARY.encode(),
                    b"Content-Type: image/jpeg",
                    b"Content-Length: " + str(len(jpeg)).encode(),
                    b"", b"",
                ])
                yield header + jpeg + CRLF
            else:
                # Nothing new. Stop once the session is over, so a forgotten
                # browser tab cannot hold a generator open indefinitely.
                if not manager.runtime.is_busy and time.time() - idle_since > 2:
                    return
                time.sleep(0.03)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=%s" % MJPEG_BOUNDARY,
        headers=PREVIEW_HEADERS)
