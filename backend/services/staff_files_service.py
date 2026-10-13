"""
ClassVision AI - Staff Photographs and Documents
--------------------------------------------------
A teacher's personnel file: one photograph, and any number of documents.

What this is NOT

    It is not enrollment, and nothing here touches the recognition pipeline.
    No embedding is computed from a staff photograph, the face detector never
    opens one, and a teacher with a photograph on file is not recognisable to
    a camera. It is the picture on an ID card, stored because a school office
    needs one - the same category of thing as a phone number.

    Saying so in code as well as in a comment: this module imports nothing
    from ai/ and nothing from the embedding store. A future change that made
    a staff photograph biometric would have to add an import, which is
    visible in review in a way that a quiet behaviour change is not.

Where the bytes live

    Under data/staff/, never in SQLite. The database gets a filename; the
    file gets a random name. Two consequences that both matter:

        A copy of classvision.db taken for debugging carries nobody's
        photograph or identity document with it.

        The URL a document is served from cannot be guessed from a person's
        name, a user id, or the order they were uploaded in.

What is refused

    A file bigger than the limit, a type not on the allow-list, and anything
    whose declared type and actual leading bytes disagree. The last one is
    the one that matters: an "image/jpeg" that begins with `<?php` or `<svg`
    is not a photograph, and a server that stores it and serves it back has
    built a file-upload vulnerability out of a staff directory.

    SVG is deliberately NOT allowed even though it is an image. It is a
    document format that executes script, and there is no version of a
    teacher's ID photo that has to be one.
"""

import base64
import binascii
import re
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STAFF_DIR = PROJECT_ROOT / "data" / "staff"

MAX_PHOTO_BYTES = 4 * 1024 * 1024        # 4 MB - a portrait, not an album
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024    # 10 MB - a scan, not a video

# Declared type -> (extension, leading bytes that must be present).
#
# The signature check is what makes the allow-list mean anything. Trusting
# the browser's Content-Type would let any file through under any name.
PHOTO_TYPES = {
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/webp": (".webp", (b"RIFF",)),
}

DOCUMENT_TYPES = dict(PHOTO_TYPES)
DOCUMENT_TYPES.update({
    "application/pdf": (".pdf", (b"%PDF-",)),
})

SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class StaffFileError(ValueError):
    """Every message is safe to show the person who uploaded the file."""


def staff_directory():
    STAFF_DIR.mkdir(parents=True, exist_ok=True)
    return STAFF_DIR


def clean_filename(name):
    """
    The name as it will be SHOWN. It is never used to open anything.

    Path traversal is not defended against here by cleaning the string - it
    is defended against by never using this value as a path. The stored name
    is generated, and this is a label.
    """
    name = SAFE_NAME.sub("_", (name or "").strip())[:120]
    return name or "document"


# A data URL as a browser produces it from canvas.toDataURL() or FileReader.
DATA_URL = re.compile(
    r"^data:(?P<type>[a-z]+/[a-z0-9.+-]+)?(?P<extra>;[^,]*)?,(?P<body>.*)$",
    re.IGNORECASE | re.DOTALL)

# Base64 inflates by 4/3, and the limit is applied to the DECODED bytes. This
# cap is on the string, so an enormous body is rejected before it is decoded
# rather than after - decoding first would mean allocating it.
MAX_DATA_URL_CHARS = 20 * 1024 * 1024


def decode_data_url(value):
    """
    Bytes and a declared content type from a data: URL.

    The type here is only a CLAIM. It is passed to _check(), which verifies
    it against the actual leading bytes; nothing downstream trusts it on its
    own.
    """
    if not isinstance(value, str) or not value:
        raise StaffFileError("No file was supplied.")

    if len(value) > MAX_DATA_URL_CHARS:
        raise StaffFileError("That file is too large.")

    match = DATA_URL.match(value.strip())
    if not match:
        raise StaffFileError(
            "The file must be a data URL, for example "
            "data:image/jpeg;base64,...")

    if "base64" not in (match.group("extra") or "").lower():
        raise StaffFileError("The data URL must be base64-encoded.")

    try:
        data = base64.b64decode(match.group("body"), validate=False)
    except (binascii.Error, ValueError):
        raise StaffFileError("The file is not valid base64.")

    return data, (match.group("type") or "").lower()


def _check(data, content_type, allowed, limit, what):
    if not data:
        raise StaffFileError("The %s is empty." % what)

    if len(data) > limit:
        raise StaffFileError(
            "The %s is too large (%.1f MB; the limit is %d MB)."
            % (what, len(data) / 1048576.0, limit // 1048576))

    content_type = (content_type or "").split(";")[0].strip().lower()

    if content_type not in allowed:
        raise StaffFileError(
            "%s is not an accepted %s type. Accepted: %s."
            % (content_type or "That file", what,
               ", ".join(sorted(allowed))))

    extension, signatures = allowed[content_type]

    # The declared type has to match what the bytes actually are. This is the
    # check that stops a script being uploaded as a photograph.
    if not any(data.startswith(signature) for signature in signatures):
        raise StaffFileError(
            "That file does not look like a %s. It was sent as %s but its "
            "contents are something else." % (what, content_type))

    return extension


def save_photo(data, content_type):
    """
    Store a staff portrait and return its generated filename.

    Returns the NAME only. The caller writes that to users.photo_name; no
    path leaves this module, and no path is ever returned to a browser.
    """
    extension = _check(data, content_type, PHOTO_TYPES, MAX_PHOTO_BYTES,
                       "photograph")
    stored = "photo-%s%s" % (secrets.token_hex(16), extension)
    (staff_directory() / stored).write_bytes(data)
    return stored


def save_document(data, content_type):
    extension = _check(data, content_type, DOCUMENT_TYPES,
                       MAX_DOCUMENT_BYTES, "document")
    stored = "doc-%s%s" % (secrets.token_hex(16), extension)
    (staff_directory() / stored).write_bytes(data)
    return stored


def read_file(stored_name):
    """
    The bytes of a stored file, or None.

    stored_name is validated against the generated shape rather than merely
    cleaned. A value that did not come out of save_photo or save_document is
    refused outright, so a row whose stored_name was tampered with cannot be
    used to read an arbitrary file off the disk.
    """
    if not re.fullmatch(r"(photo|doc)-[0-9a-f]{32}\.[a-z]{3,4}",
                        stored_name or ""):
        return None

    path = staff_directory() / stored_name
    if not path.is_file():
        return None
    return path.read_bytes()


def delete_file(stored_name):
    """Remove a stored file. A missing file is a success, not an error."""
    if not re.fullmatch(r"(photo|doc)-[0-9a-f]{32}\.[a-z]{3,4}",
                        stored_name or ""):
        return False
    path = staff_directory() / stored_name
    if path.is_file():
        path.unlink()
        return True
    return False


def content_type_for(stored_name):
    """The type to serve a stored file back as, from its own extension."""
    suffix = Path(stored_name or "").suffix.lower()
    for mapping in (DOCUMENT_TYPES,):
        for content_type, (extension, _) in mapping.items():
            if extension == suffix:
                return content_type
    return "application/octet-stream"
