"""
Teacher and administrator accounts. Super Admin only.

Every endpoint here depends on require_super_admin, which is enforced on the
SERVER. Hiding the Administration tab from a teacher's navigation is a
courtesy; this is the actual permission, and a teacher who calls these by hand
gets 403 rather than a result.

Two lockout guards, and why they are refusals rather than warnings
    The last active Super Admin cannot be deactivated, demoted or deleted, and
    nobody can delete the account they are signed in with. An installation
    with no usable administrator cannot be repaired through the dashboard -
    only by editing the database by hand, which is exactly the thing this
    whole feature exists to stop people having to do.

Class assignments
    A teacher's assignments narrow what they can see: their classes' students,
    sessions, attendance and analytics. A teacher with NO assignments is
    unrestricted within the teacher role, because somebody who has been given
    an account but not yet a timetable should be able to work rather than see
    an empty dashboard and conclude the system is broken.

Never returned by anything in this module: a password, or a password hash.
"""

from typing import List

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
)

from repositories import AuditRepository, StaffDocumentRepository, utc_now_iso

from backend.schemas.models import (
    AuditEntry,
    SimpleResult,
    UserAccount,
    UserCreateRequest,
    UserUpdateRequest,
)
from backend.services.auth_service import (
    AuthConflict,
    AuthForbidden,
    AuthInvalid,
    AuthService,
)
from backend.services.authz import (
    Principal,
    require_super_admin,
    require_user,
)
from backend.services.state import get_database

router = APIRouter(prefix="/api/users", tags=["administration"])


def _service():
    return AuthService(get_database())


def _assignments(payload_assignments):
    if payload_assignments is None:
        return None
    return [{"class_name": item.class_name, "section": item.section}
            for item in payload_assignments]


@router.get("", response_model=List[UserAccount],
            summary="All accounts")
def list_users(principal: Principal = Depends(require_super_admin)):
    return _service().list_users(principal.actor())


@router.post("", response_model=UserAccount, status_code=201,
             summary="Create a teacher or administrator",
             responses={400: {"description": "The account is not usable"},
                        403: {"description": "Super Admin only"},
                        409: {"description": "That username is taken"}})
def create_user(payload: UserCreateRequest,
                principal: Principal = Depends(require_super_admin)):
    """
    Create an account and, optionally, assign its classes in one step.

    The password is set here and never shown again - there is no endpoint that
    returns it, and no endpoint that returns its hash. A Super Admin who needs
    to reset a forgotten password sets a new one; they cannot read the old.
    """
    try:
        return _service().create_user(
            principal.actor(), payload.username, payload.display_name,
            payload.role, payload.password,
            assignments=_assignments(payload.assignments))
    except AuthConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.get("/audit", response_model=List[AuditEntry],
            summary="Recent administrative actions")
def audit(limit: int = Query(100, ge=1, le=500),
          principal: Principal = Depends(require_super_admin)):
    """
    What has been done, by whom, and when.

    Holds no password, no embedding and nothing biometric - only that an
    action happened to a named subject.
    """
    from repositories import AuditRepository
    return AuditRepository(get_database()).recent(limit)


@router.get("/{user_id}", response_model=UserAccount, summary="One account")
def get_user(user_id: int = Path(ge=1),
             principal: Principal = Depends(require_super_admin)):
    user = _service().describe_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")
    return user


@router.patch("/{user_id}", response_model=UserAccount,
              summary="Edit an account, its role, status or classes",
              responses={400: {"description": "The change is not usable"},
                         403: {"description": "Super Admin only, or the "
                                              "change would lock everybody "
                                              "out"},
                         404: {"description": "No such user"}})
def update_user(payload: UserUpdateRequest,
                user_id: int = Path(ge=1),
                principal: Principal = Depends(require_super_admin)):
    """
    Change a name, a role, a status, a password or a class assignment.

    Any of those ends the account's open sessions, so a revoked teacher's
    browser tab stops working at its next request rather than whenever the
    token happens to expire.
    """
    try:
        user = _service().update_user(
            principal.actor(), user_id,
            display_name=payload.display_name,
            role=payload.role,
            status=payload.status,
            password=payload.password,
            assignments=_assignments(payload.assignments),
            can_view_all_students=payload.can_view_all_students)
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthInvalid as error:
        if "No such user" in str(error):
            raise HTTPException(status_code=404, detail=str(error))
        raise HTTPException(status_code=400, detail=str(error))

    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")
    return user


@router.delete("/{user_id}", response_model=SimpleResult,
               summary="Delete an account",
               responses={403: {"description": "Super Admin only, the last "
                                               "administrator, or your own "
                                               "account"},
                          404: {"description": "No such user"}})
def delete_user(user_id: int = Path(ge=1),
                principal: Principal = Depends(require_super_admin)):
    """
    Remove an account entirely.

    Deactivation is the gentler option and the dashboard offers it first, but
    an account created by mistake should be removable rather than kept forever
    as clutter. The audit trail survives: it records usernames as text rather
    than as foreign keys, so deleting a user never rewrites history.
    """
    try:
        _service().delete_user(principal.actor(), user_id)
    except AuthForbidden as error:
        raise HTTPException(status_code=403, detail=str(error))
    except AuthInvalid as error:
        raise HTTPException(status_code=404, detail=str(error))

    return {"ok": True, "detail": "Account deleted."}


# -- personnel photograph and documents -------------------------------------
#
# A teacher's staff record: the picture on their ID and the documents a
# school office keeps. NONE of this is biometric and none of it reaches the
# recognition pipeline - see the module docstring in
# backend/services/staff_files_service.py, which imports nothing from ai/ so
# that a change making it biometric would be visible in review.
#
# Everything here is Super Admin only. A personnel file is not something a
# colleague browses, and a teacher does not upload their own contract.

@router.put("/{user_id}/photo", summary="Set a teacher's photograph",
            responses={400: {"description": "Not a usable image"},
                       403: {"description": "Super Admin only"},
                       404: {"description": "No such account"}})
def set_user_photo(user_id: int = Path(ge=1),
                   payload: dict = Body(...),
                   principal: Principal = Depends(require_super_admin)):
    """
    Upload or replace the photograph on a staff account.

    Takes a data URL in JSON rather than a multipart upload:

        {"image": "data:image/jpeg;base64,/9j/4AAQ..."}

    That is the shape the dashboard already has. A photograph is captured
    from the webcam into a <canvas> and read back with toDataURL(), so a data
    URL is what the browser is holding either way, and accepting it directly
    avoids adding python-multipart to a project that currently needs no
    parser beyond JSON.

    The image is checked against its declared type by its LEADING BYTES, not
    by what the browser called it: a file sent as image/jpeg that does not
    begin with JPEG's signature is refused. Replacing a photograph deletes
    the previous file rather than orphaning it on disk.
    """
    from backend.services import staff_files_service as files

    service = _service()
    if service.describe_user(user_id) is None:
        raise HTTPException(status_code=404, detail="No such account.")

    try:
        data, content_type = files.decode_data_url((payload or {}).get("image"))
        stored = files.save_photo(data, content_type)
    except files.StaffFileError as error:
        raise HTTPException(status_code=400, detail=str(error))

    database = get_database()
    previous = database.query_one(
        "SELECT photo_name FROM users WHERE user_id = ?", (user_id,))

    database.execute("UPDATE users SET photo_name = ?, updated_at = ? "
                     "WHERE user_id = ?",
                     (stored, utc_now_iso(), user_id))

    if previous and previous["photo_name"]:
        files.delete_file(previous["photo_name"])

    AuditRepository(database).record(
        "user.photo", actor=principal.username, actor_role=principal.role,
        subject=str(user_id), detail="%d bytes" % len(data))

    return {"user_id": user_id, "photo": True}


@router.get("/{user_id}/photo", summary="A teacher's photograph",
            response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}},
                       404: {"description": "No photograph on file"}})
def get_user_photo(user_id: int = Path(ge=1),
                   principal: Principal = Depends(require_user)):
    """
    Serve the photograph back.

    Readable by any signed-in staff member - a teacher seeing who else works
    at their school is not a disclosure - while the DOCUMENTS below stay
    Super Admin only, because a contract is not a face.

    The file is served from its generated name, never from a path supplied by
    the caller, so there is no traversal to defend against.
    """
    from backend.services import staff_files_service as files

    row = get_database().query_one(
        "SELECT photo_name FROM users WHERE user_id = ?", (user_id,))

    if row is None or not row["photo_name"]:
        raise HTTPException(status_code=404, detail="No photograph on file.")

    data = files.read_file(row["photo_name"])
    if data is None:
        raise HTTPException(status_code=404, detail="No photograph on file.")

    return Response(content=data,
                    media_type=files.content_type_for(row["photo_name"]),
                    headers={"Cache-Control": "private, max-age=300"})


@router.delete("/{user_id}/photo", summary="Remove a teacher's photograph",
               responses={403: {"description": "Super Admin only"},
                          404: {"description": "No photograph on file"}})
def delete_user_photo(user_id: int = Path(ge=1),
                      principal: Principal = Depends(require_super_admin)):
    from backend.services import staff_files_service as files

    database = get_database()
    row = database.query_one(
        "SELECT photo_name FROM users WHERE user_id = ?", (user_id,))

    if row is None or not row["photo_name"]:
        raise HTTPException(status_code=404, detail="No photograph on file.")

    files.delete_file(row["photo_name"])
    database.execute("UPDATE users SET photo_name = '', updated_at = ? "
                     "WHERE user_id = ?", (utc_now_iso(), user_id))

    AuditRepository(database).record(
        "user.photo_removed", actor=principal.username,
        actor_role=principal.role, subject=str(user_id))

    return {"user_id": user_id, "photo": False}


@router.get("/{user_id}/documents", summary="A teacher's documents")
def list_user_documents(user_id: int = Path(ge=1),
                        principal: Principal = Depends(require_super_admin)):
    """
    What is on file, by name and size. Never the contents.

    Super Admin only - unlike the photograph. A personnel document is a
    contract, a qualification or an identity scan, and "any signed-in
    colleague" is not who those are for.
    """
    return {"items": StaffDocumentRepository(get_database())
            .list_for_user(user_id)}


@router.post("/{user_id}/documents", status_code=201,
             summary="Upload a document to a teacher's record",
             responses={400: {"description": "Not a usable document"},
                        403: {"description": "Super Admin only"},
                        404: {"description": "No such account"}})
def add_user_document(user_id: int = Path(ge=1),
                      payload: dict = Body(...),
                      principal: Principal = Depends(require_super_admin)):
    """
    Store a PDF or an image against a staff account.

        {"file": "data:application/pdf;base64,JVBERi0...",
         "filename": "teaching-certificate.pdf",
         "label": "Qualification"}

    Checked the same way the photograph is: the declared type has to match
    the leading bytes. SVG is not accepted at any size, because it is a
    document format that executes script and no identity document has to be
    one.
    """
    from backend.services import staff_files_service as files

    if _service().describe_user(user_id) is None:
        raise HTTPException(status_code=404, detail="No such account.")

    body = payload or {}

    try:
        data, content_type = files.decode_data_url(body.get("file"))
        stored = files.save_document(data, content_type)
    except files.StaffFileError as error:
        raise HTTPException(status_code=400, detail=str(error))

    database = get_database()
    record = StaffDocumentRepository(database).add(
        user_id,
        filename=files.clean_filename(body.get("filename")),
        stored_name=stored,
        label=(body.get("label") or "")[:120],
        content_type=files.content_type_for(stored),
        byte_size=len(data),
        uploaded_by=principal.username)

    AuditRepository(database).record(
        "user.document_added", actor=principal.username,
        actor_role=principal.role, subject=str(user_id),
        detail=record["filename"])

    return record


@router.get("/{user_id}/documents/{document_id}",
            summary="Download one document", response_class=Response,
            responses={404: {"description": "No such document"}})
def get_user_document(user_id: int = Path(ge=1),
                      document_id: int = Path(ge=1),
                      principal: Principal = Depends(require_super_admin)):
    """
    Serve a document.

    The user_id in the path has to match the one on the row. Without that
    check the id alone would be enough to read any document in the school,
    and the path would be lying about whose record it was.
    """
    from backend.services import staff_files_service as files

    record = StaffDocumentRepository(get_database()).get(document_id)

    if record is None or record["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="No such document.")

    data = files.read_file(record["stored_name"])
    if data is None:
        raise HTTPException(status_code=404, detail="No such document.")

    return Response(
        content=data,
        media_type=record["content_type"] or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            # Always an attachment. A PDF rendered inline is a PDF running in
            # the dashboard's own origin.
            "Content-Disposition": 'attachment; filename="%s"'
                                   % record["filename"],
        })


@router.delete("/{user_id}/documents/{document_id}",
               summary="Remove a document",
               responses={403: {"description": "Super Admin only"},
                          404: {"description": "No such document"}})
def delete_user_document(user_id: int = Path(ge=1),
                         document_id: int = Path(ge=1),
                         principal: Principal = Depends(require_super_admin)):
    from backend.services import staff_files_service as files

    database = get_database()
    repository = StaffDocumentRepository(database)
    record = repository.get(document_id)

    if record is None or record["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="No such document.")

    files.delete_file(record["stored_name"])
    repository.delete(document_id)

    AuditRepository(database).record(
        "user.document_removed", actor=principal.username,
        actor_role=principal.role, subject=str(user_id),
        detail=record["filename"])

    return {"document_id": document_id, "removed": True}
