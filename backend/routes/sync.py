"""
The outbound queue: what is waiting, and making it move.

Reading the queue's state is available to any signed-in user - a teacher who
has just finished a lesson has a legitimate reason to know whether it has gone
anywhere. Draining it, retrying abandoned items and exporting a session are
SUPER_ADMIN, because they are operations on the installation rather than on a
class.

No endpoint here returns a payload body by default. The queue holds copies of
attendance records, and a listing that dumped them would be a second, less
guarded way to read the register.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend.services.authz import (
    Principal,
    require_super_admin,
    require_user,
)
from backend.services.state import get_database
from backend.services.sync_service import (
    SessionExporter,
    SyncError,
    SyncService,
)

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _service():
    return SyncService(get_database())


@router.get("/status", summary="What is waiting to be sent, and for how long")
def status(principal: Principal = Depends(require_user)):
    """
    The counts, the age of the oldest waiting item, and the last error.

    The age is the number that matters. A queue of 400 items that is draining
    is healthy; a queue of 3 whose oldest is eleven days old means nobody has
    noticed that the link has been down since term started.
    """
    return _service().status()


@router.get("/pending", summary="The items waiting, in the order they will go")
def pending(limit: int = Query(50, ge=1, le=500),
            principal: Principal = Depends(require_super_admin)):
    """
    What is queued, without the payload bodies.

    The queue holds copies of attendance records. Returning them here would be
    a second way to read the register with different scoping, so each item is
    described by what it is rather than by what it contains.
    """
    items = _service().pending(limit=limit)

    return {
        "items": [{
            "queue_id": item["queue_id"],
            "item_key": item["item_key"],
            "kind": item["kind"],
            "status": item["status"],
            "attempts": item["attempts"],
            "last_error": item["last_error"],
            "queued_at": item["queued_at"],
        } for item in items],
        "total": len(items),
        "note": ("Payload bodies are deliberately not returned here. They are "
                 "copies of attendance records, and the Reports tab is where "
                 "attendance is read."),
    }


@router.post("/drain", summary="Try to send what is waiting")
def drain(limit: int = Query(100, ge=1, le=500),
          principal: Principal = Depends(require_super_admin)):
    """
    Hand the waiting items to the configured target.

    With no upstream configured this always reports that nothing was sent and
    says why, which is the honest result rather than an error: the attendance
    is safe where it is, and there is simply nowhere to copy it to.
    """
    return _service().drain(limit=limit)


@router.post("/retry", summary="Put abandoned items back in the queue")
def retry(principal: Principal = Depends(require_super_admin)):
    """
    Deliberate, never automatic.

    An item is abandoned because retrying did not fix whatever was wrong.
    Retrying on a timer would hide that; retrying when somebody presses this
    means somebody has looked.
    """
    return _service().retry_abandoned()


@router.post("/sessions/{session_id}/export",
             summary="Queue a completed session for sending",
             responses={404: {"description": "No such session"}})
def export_session(session_id: str = Path(max_length=200),
                   principal: Principal = Depends(require_super_admin)):
    """
    Queue a copy of one session and everything recorded against it.

    Safe to run twice. The item keys are derived from what each item IS, so a
    second run updates the same rows rather than queueing the session again -
    which is what makes this usable for catching up after an appliance has
    been offline.
    """
    try:
        return SessionExporter(get_database()).export_session(session_id)
    except SyncError as error:
        raise HTTPException(status_code=404, detail=str(error))
