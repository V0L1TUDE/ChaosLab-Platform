"""
Application upload validation and storage
=========================================
Uploaded files are registered and stored, NEVER extracted or executed. Validation:
extension allow-list, size limit, content sniffing (magic bytes / UTF-8 / JSON parse),
archive path-traversal check, executable rejection, and server-generated storage names.
"""

import io
import json
import os
import re
import tarfile
import uuid
import zipfile
from typing import Dict, Tuple

from . import config

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_ARCHIVE_MEMBER_LIMIT = 20000


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def safe_display_name(filename: str) -> str:
    """Strip any path and unsafe characters; for display only (never used to build paths)."""
    base = re.split(r"[\\/]", filename or "")[-1]
    base = _SAFE_CHARS.sub("_", base).strip(" .")[:100]
    return base or "upload"


def detect_extension(filename: str) -> str:
    lower = (filename or "").lower()
    for ext in sorted(config.ALLOWED_UPLOAD_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(ext):
            return ext
    allowed = ", ".join(config.ALLOWED_UPLOAD_EXTENSIONS)
    raise UploadError(f"File type not allowed. Allowed types: {allowed}.", 415)


def _is_unsafe_member(name: str) -> bool:
    norm = name.replace("\\", "/")
    return norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) is not None or ".." in norm.split("/")


def _validate_content(ext: str, data: bytes) -> None:
    if not data:
        raise UploadError("The uploaded file is empty.")
    # Reject native executables and scripts regardless of declared extension.
    if data[:2] == b"MZ" or data[:4] == b"\x7fELF" or data[:2] == b"#!" or data[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        raise UploadError("Executable files are not accepted.", 415)

    if ext == ".zip":
        if not data.startswith(b"PK\x03\x04"):
            raise UploadError("File content is not a valid ZIP archive.", 415)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                bad = zf.testzip()
        except zipfile.BadZipFile:
            raise UploadError("File content is not a valid ZIP archive.", 415)
        if bad:
            raise UploadError("ZIP archive is corrupted.", 415)
        if len(names) > _ARCHIVE_MEMBER_LIMIT or any(_is_unsafe_member(n) for n in names):
            raise UploadError("Archive contains unsafe or too many entries.", 415)
    elif ext in (".tar", ".tar.gz", ".tgz"):
        if ext != ".tar" and data[:2] != b"\x1f\x8b":
            raise UploadError("File content is not a valid gzip archive.", 415)
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                count = 0
                for member in tf:
                    count += 1
                    if count > _ARCHIVE_MEMBER_LIMIT or _is_unsafe_member(member.name) or member.issym() or member.islnk():
                        raise UploadError("Archive contains unsafe or too many entries.", 415)
        except UploadError:
            raise
        except (tarfile.TarError, OSError, EOFError):
            raise UploadError("File content is not a valid TAR archive.", 415)
    else:  # text formats
        if b"\x00" in data:
            raise UploadError("File does not look like a text file.", 415)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadError("Text files must be UTF-8 encoded.", 415)
        if ext == ".json":
            try:
                json.loads(text)
            except ValueError:
                raise UploadError("File is not valid JSON.", 415)


def validate_and_store(user_id: int, filename: str, data: bytes) -> Dict[str, object]:
    """Validate an upload and write it to disk. Returns storage metadata for the DB row."""
    if not filename:
        raise UploadError("A file is required.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise UploadError(f"File exceeds the {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.", 413)
    ext = detect_extension(filename)
    _validate_content(ext, data)

    user_dir = os.path.join(config.UPLOAD_DIR, f"user_{int(user_id)}")
    os.makedirs(user_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"          # server-generated: no user input in the path
    path = os.path.join(user_dir, stored_name)
    with open(path, "wb") as f:
        f.write(data)
    return {
        "file_path": path,
        "original_filename": safe_display_name(filename),
        "file_size": len(data),
        "upload_type": ext.lstrip("."),
    }


def delete_stored_file(path: str) -> None:
    """Delete a stored upload, but only if it really lives inside the upload directory."""
    if not path:
        return
    root = os.path.realpath(config.UPLOAD_DIR)
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) == root and os.path.isfile(real):
        os.remove(real)
