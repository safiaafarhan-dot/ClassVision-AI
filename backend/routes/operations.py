"""
Alerts, parent notifications, the recognition model registry, and cameras.

Three small administrative areas that share one property: none of them can
change an attendance record, and none of them can change what the camera
recognises without an explicit, typed confirmation.

Parent contact detail is the one genuinely sensitive thing here, and it is
NOT returned by the notification endpoints at all - a notification says who it
was about and whether it was delivered, and the address is looked up at send
time. Reading or writing a parent's phone number goes through the student
endpoints, behind the same class scoping as everything else.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from backend.services.authz import (
    Principal,
    require_student_scope,
    require_super_admin,
    require_user,
)
from backend.services.notification_service import (
    NotificationError,
    NotificationService,
)
from backend.services.state import get_data_service, get_database

router = APIRouter(prefix="/api", tags=["operations"])


def _notifications():
    """
    The alert service, wired to whatever delivery this installation has.

    configured_provider() returns an SMTP sender when CLASSVISION_SMTP_HOST
    and CLASSVISION_SMTP_FROM are set in the environment, and the existing
    RecordingProvider otherwise. An installation with no mail account keeps
    behaving exactly as it did: alerts are raised, recorded and shown, and
    nothing leaves the building.

    Credentials are read from the environment on each call rather than held
    on a module global, so setting them does not need a restart and clearing
    them takes effect immediately.
    """
    from backend.services.mail_provider import configured_provider
    return NotificationService(get_database(), provider=configured_provider())


def _registry():
    from model_registry import ModelRegistry
    return ModelRegistry(get_database())


def _audit_camera(principal, action, camera_id, detail=None):
    """Record a device change, best effort. Never fails the operation."""
    try:
        from repositories import AuditRepository
        AuditRepository(get_database()).record(
            action, actor=principal.username, actor_role=principal.role,
            subject=camera_id, detail=detail)
    except Exception:                                    # noqa: BLE001
        pass


# -- alert rules ------------------------------------------------------------

@router.get("/alerts/rules", summary="Attendance alert rules")
def list_rules(principal: Principal = Depends(require_user)):
    """
    Every rule, with its threshold.

    Thresholds are rows, not constants. A school that wants 60% instead of
    75% edits one of these; nothing in the engine knows any particular number.
    """
    return {"items": _notifications().list_rules()}


@router.put("/alerts/rules/{rule_id}", summary="Create or update a rule",
            responses={400: {"description": "Not a usable rule"}})
def save_rule(payload: Dict[str, Any] = Body(...),
              rule_id: str = Path(max_length=120),
              principal: Principal = Depends(require_super_admin)):
    try:
        return _notifications().save_rule(
            rule_id=rule_id,
            name=payload.get("name") or rule_id,
            rule_type=payload.get("rule_type"),
            threshold=payload.get("threshold"),
            window_days=payload.get("window_days"),
            enabled=payload.get("enabled", True),
            class_name=payload.get("class_name", ""),
            section=payload.get("section", ""),
            message=payload.get("message", ""),
            actor=principal.username)
    except NotificationError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/alerts/rules/defaults",
             summary="Install the suggested starting rules")
def apply_defaults(principal: Principal = Depends(require_super_admin)):
    """
    Common rules, applied only when an administrator asks.

    An installation that never asks has no rules and sends nothing - there is
    no threshold anywhere in this project that fires on its own.
    """
    created = _notifications().apply_default_rules(actor=principal.username)
    return {"created": created, "count": len(created)}


@router.delete("/alerts/rules/{rule_id}", summary="Remove a rule")
def delete_rule(rule_id: str = Path(max_length=120),
                principal: Principal = Depends(require_super_admin)):
    removed = _notifications().notifications.delete_rule(rule_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No such rule.")
    return {"rule_id": rule_id, "deleted": True}


# -- evaluation and delivery ------------------------------------------------

@router.post("/alerts/evaluate", summary="Check the rules against attendance",
             responses={400: {"description": "Not a usable request"}})
def evaluate(payload: Optional[Dict[str, Any]] = Body(None),
             principal: Principal = Depends(require_super_admin)):
    """
    Run every enabled rule over every student it applies to.

    `dry_run` evaluates identically and writes nothing, so an administrator
    can see exactly who would be contacted before anybody is.
    """
    payload = payload or {}
    try:
        return _notifications().evaluate(
            student_ids=payload.get("student_ids"),
            dry_run=bool(payload.get("dry_run", False)),
            actor=principal.username)
    except NotificationError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.get("/alerts/delivery", summary="How alerts are delivered, if at all")
def delivery_configuration(principal: Principal = Depends(require_super_admin)):
    """
    Whether this installation can actually send a parent an email.

    Reports the host, the port, the From address and WHETHER a username and
    password are set - never the password itself, and never any part of it.
    A masked secret with a visible prefix is still a hint to somebody who
    should not have one.

    The dashboard uses this to say "alerts are recorded but not sent" out
    loud, rather than letting a school believe parents are being told.
    """
    from backend.services.mail_provider import describe_configuration
    return describe_configuration()


@router.get("/alerts/notifications", summary="What was raised, and whether "
                                             "it was delivered")
def notification_history(
        student_id: Optional[str] = Query(None, max_length=120),
        limit: int = Query(100, ge=1, le=500),
        principal: Principal = Depends(require_user)):
    """
    The delivery log. Carries no phone number and no email address.
    """
    if student_id:
        require_student_scope(principal,
                              get_data_service().get_student(student_id))

    return {"items": _notifications().history(student_id, limit)}


@router.post("/alerts/notifications/{notification_id}/retry",
             summary="Try delivering a notification again",
             responses={404: {"description": "No such notification"}})
def retry_delivery(notification_id: int = Path(ge=1),
                   principal: Principal = Depends(require_super_admin)):
    try:
        return _notifications().deliver(notification_id)
    except NotificationError as error:
        raise HTTPException(status_code=404, detail=str(error))


# -- the model registry -----------------------------------------------------

@router.get("/models", summary="Recognition models and which is in production")
def list_models(principal: Principal = Depends(require_user)):
    """
    SFace is the baseline and normally the active model.

    A custom model appears here with its measured numbers and is experimental
    until it has been validated against a held-out set and explicitly
    activated. Registering or training one changes nothing about recognition.
    """
    registry = _registry()
    registry.ensure_baseline()
    return registry.describe()


@router.post("/models/{model_id}/validate",
             summary="Check a model against the activation gate",
             responses={400: {"description": "It does not meet the "
                                             "requirements"}})
def validate_model(model_id: str = Path(max_length=120),
                   payload: Optional[Dict[str, Any]] = Body(None),
                   principal: Principal = Depends(require_super_admin)):
    from model_registry import ModelRegistryError

    try:
        return _registry().validate(model_id,
                                    metrics=(payload or {}).get("metrics"),
                                    actor=principal.username)
    except ModelRegistryError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/models/{model_id}/activate",
             summary="Put a model into production",
             responses={400: {"description": "Refused - not validated, or "
                                             "not confirmed"}})
def activate_model(model_id: str = Path(max_length=120),
                   confirm: Optional[str] = Query(
                       None, max_length=120,
                       description="The model id, typed out. This changes "
                                   "what the camera recognises."),
                   principal: Principal = Depends(require_super_admin)):
    from model_registry import ModelRegistryError

    try:
        return _registry().activate(model_id, actor=principal.username,
                                    confirm=confirm)
    except ModelRegistryError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/models/rollback", summary="Put the SFace baseline back")
def rollback_model(principal: Principal = Depends(require_super_admin)):
    """
    Always available and never gated.

    Deliberately asymmetric with activation: promoting an experiment takes a
    validation and a typed confirmation, and undoing it takes one click. When
    something is going wrong in a live classroom, the safe direction should be
    the easy one.
    """
    return _registry().rollback(actor=principal.username)


# -- cameras ----------------------------------------------------------------

@router.get("/cameras", summary="Cameras and whether they are answering")
def list_cameras(principal: Principal = Depends(require_user)):
    """
    Operational only. A camera row says where a device is and when it last
    delivered a frame; it says nothing about anybody it has ever seen.

    The live session's own camera is reported alongside, from the runtime,
    so the health view reflects what is actually running rather than what was
    last written down.
    """
    from repositories import CameraRepository

    from backend.services.live_service import get_live_manager

    runtime = get_live_manager().runtime.snapshot()

    return {
        "items": CameraRepository(get_database()).list_all(),
        "live": {
            "state": runtime.get("state"),
            "camera": runtime.get("camera"),
            "camera_index": runtime.get("camera_index"),
            "fps": runtime.get("fps"),
            "processed": runtime.get("processed_count"),
            "dropped": runtime.get("dropped_count"),
            "last_event_at": runtime.get("last_event_at"),
        },
        "note": ("Only values the hardware actually reported are shown. A "
                 "camera that reports no frame rate is left blank rather "
                 "than credited with a number nobody measured."),
    }


@router.get("/cameras/available",
            summary="Which camera indexes this machine can actually open",
            responses={409: {"description": "A live session is using a "
                                            "camera"}})
def available_cameras(principal: Principal = Depends(require_user)):
    """
    Probe the capture indexes and report what answered.

    Reuses the probe the command-line camera test already used - see
    ai/camera_test.probe_cameras - so the dashboard and the CLI cannot disagree
    about what a working camera is.

    REFUSED while a live session is running. Probing opens each device in turn,
    and doing that to the camera feeding a lesson would at best fail and at
    worst take the device away from the class.

    An index that OPENS but delivers no frame is reported as exactly that,
    because it is a different fault from an empty index: it is almost always a
    camera another application is holding.
    """
    from backend.services.live_service import get_live_registry

    running = get_live_registry().busy()
    if running:
        raise HTTPException(
            status_code=409,
            detail="A live session is running on this machine, so the cameras "
                   "cannot be probed without interrupting it. Finish the "
                   "session first.")

    try:
        from camera_test import probe_cameras
    except Exception as error:                           # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The camera layer is not available on this machine: %s"
                   % type(error).__name__)

    try:
        findings = probe_cameras()
    except Exception as error:                           # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The cameras could not be probed: %s"
                   % type(error).__name__)

    usable = [entry for entry in findings if entry["available"]]

    return {
        "items": findings,
        "available": [entry["index"] for entry in usable],
        "total": len(usable),
        "note": ("Each index was opened and asked for one frame. An index that "
                 "opened without delivering a frame is reported as such - that "
                 "is a camera in use, not a camera that is absent."),
    }


@router.put("/cameras/{camera_id}", summary="Register a camera device",
            responses={400: {"description": "Not a usable device"},
                       403: {"description": "Only a Super Admin can do this"}})
def save_camera(payload: Dict[str, Any] = Body(...),
                camera_id: str = Path(max_length=120),
                principal: Principal = Depends(require_super_admin)):
    """
    Register a classroom camera, or correct the details of one.

    Registration records WHAT and WHERE the device is - its id, its name, the
    OpenCV index it answers on and the room it is in - and nothing about its
    health. A camera that has just been registered reports status "unknown"
    with no FPS and no latency, because nothing has been heard from it yet.
    Showing a plausible-looking zero there would be inventing a reading.

    Re-registering an existing device updates its details and deliberately
    leaves its health alone: fixing a typo in a classroom name must not
    report a working camera as offline.
    """
    from repositories import CameraRepository

    camera_id = (camera_id or "").strip()
    if not camera_id:
        raise HTTPException(status_code=400, detail="A device id is required.")

    index = payload.get("camera_index")
    if index is not None:
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="The camera index must be a whole number.")
        if not 0 <= index <= 64:
            raise HTTPException(
                status_code=400,
                detail="The camera index must be between 0 and 64.")

    record = CameraRepository(get_database()).upsert(
        camera_id=camera_id,
        label=payload.get("label", ""),
        classroom=payload.get("classroom", ""),
        camera_index=index,
        registered_by=principal.username)

    _audit_camera(principal, "camera.register", camera_id,
                  "index=%s classroom=%s" % (index,
                                             payload.get("classroom", "")))
    return record


@router.post("/cameras/{camera_id}/heartbeat",
             summary="Record that a camera answered")
def camera_heartbeat(payload: Optional[Dict[str, Any]] = Body(None),
                     camera_id: str = Path(max_length=120),
                     principal: Principal = Depends(require_user)):
    from repositories import CameraRepository

    payload = payload or {}
    repository = CameraRepository(get_database())

    if repository.get(camera_id) is None:
        raise HTTPException(status_code=404, detail="No such camera.")

    return repository.heartbeat(
        camera_id,
        status=payload.get("status", "online"),
        fps=payload.get("fps"),
        latency_ms=payload.get("latency_ms"),
        error=payload.get("error"),
        pipeline_state=payload.get("pipeline_state"),
        session_id=payload.get("session_id"))


@router.delete("/cameras/{camera_id}", summary="Remove a camera")
def delete_camera(camera_id: str = Path(max_length=120),
                  principal: Principal = Depends(require_super_admin)):
    """
    Remove a device from the register.

    Safe in a way the other deletions in this project are not: a camera row is
    operational only. It records where a device is and when it last delivered
    a frame, and nothing about any person it has ever seen. Removing it
    destroys no attendance, no presence segment and no image, because none of
    those reference it - attendance is keyed on a session, never on the
    hardware that watched it.

    That is why there is no typed-id confirmation here, and why there is one
    on students and sessions.
    """
    from repositories import CameraRepository

    if not CameraRepository(get_database()).delete(camera_id):
        raise HTTPException(status_code=404, detail="No such camera.")

    _audit_camera(principal, "camera.remove", camera_id)
    return {"camera_id": camera_id, "deleted": True}
