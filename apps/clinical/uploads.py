"""Checks for uploaded lab reports.

Lab files are private patient documents. Before one is stored we make sure that:
  * the extension is allowed (settings.LAB_UPLOAD_EXTENSIONS),
  * the size is within settings.LAB_UPLOAD_MAX_MB,
  * the first bytes really are a PDF / JPEG / PNG ("magic bytes"), so a renamed
    file (e.g. a web page saved as "report.pdf") is rejected.
"""

import os
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError

# The first bytes every genuine file of each type starts with.
FILE_SIGNATURES = {
    "pdf": (b"%PDF",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG",),
}

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}

# Characters we never keep in a filename shown to staff or sent in a download header.
UNSAFE_FILENAME_CHARS = set('"\\/:*?<>|;')
MAX_FILENAME_LENGTH = 255


def file_extension(name):
    """'Report.PDF' -> 'pdf'."""
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def allowed_extensions():
    return [ext.lower().lstrip(".") for ext in settings.LAB_UPLOAD_EXTENSIONS]


def content_type_for(name):
    """The Content-Type to serve a stored lab file with."""
    return CONTENT_TYPES.get(file_extension(name), "application/octet-stream")


def validate_lab_file(upload):
    """Raise ValidationError unless `upload` is a genuine, allowed file within the size limit."""
    extension = file_extension(upload.name)
    allowed = allowed_extensions()
    if extension not in allowed:
        nice = ", ".join(ext.upper() for ext in allowed)
        raise ValidationError(f"Please upload a {nice} file.", code="extension")

    max_mb = settings.LAB_UPLOAD_MAX_MB
    if upload.size > max_mb * 1024 * 1024:
        raise ValidationError(f"This file is too large. The limit is {max_mb} MB.", code="too_large")
    if upload.size == 0:
        raise ValidationError("This file is empty.", code="empty")

    upload.seek(0)
    first_bytes = upload.read(8)
    upload.seek(0)
    signatures = FILE_SIGNATURES.get(extension)
    # Unknown types (an extension added to settings without a signature here) are refused on purpose.
    if not signatures or not first_bytes.startswith(signatures):
        raise ValidationError(
            "This file is not a real PDF, JPG or PNG (its contents don't match its name). "
            "Please upload the original report.",
            code="signature",
        )


def safe_filename(name, default="lab-report"):
    """A clean display name for an uploaded file: no folders, quotes or control characters, max 255."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in UNSAFE_FILENAME_CHARS)
    name = " ".join(name.split()).strip(" .")
    if not name:
        return default
    if len(name) > MAX_FILENAME_LENGTH:
        stem, extension = os.path.splitext(name)
        extension = extension[:20]
        name = stem[: MAX_FILENAME_LENGTH - len(extension)] + extension
    return name
