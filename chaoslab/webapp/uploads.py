"""
Application upload validation and storage
=========================================
Uploaded files are registered and stored, NEVER extracted or executed. Validation:
extension allow-list, size limit, content sniffing (magic bytes / UTF-8 / JSON parse),
bounded archive inspection (archive_guard: entry count, decompressed size and ratio, paths, links,
time budget), executable rejection, and server-generated storage names.

`inspect()` validates without storing anything; `store()` writes bytes that already passed `inspect()` (and,
for manifests, manifests.analyze()). The upload route always validates first, then stores.
"""

import json
import re
import uuid
from typing import Dict

from . import archive_guard, config, storage

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


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


def _validate_content(ext: str, data: bytes) -> None:
    if not data:
        raise UploadError("The uploaded file is empty.")
    # Reject native executables and scripts regardless of declared extension.
    if data[:2] == b"MZ" or data[:4] == b"\x7fELF" or data[:2] == b"#!" or data[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        raise UploadError("Executable files are not accepted.", 415)

    try:
        if ext == ".zip":
            archive_guard.inspect_zip(data)
        elif ext in (".tar", ".tar.gz", ".tgz"):
            archive_guard.inspect_tar(data, gz=ext != ".tar")
    except archive_guard.ArchiveRejected as e:
        raise UploadError(str(e), 415)
    if ext in (".zip", ".tar", ".tar.gz", ".tgz"):
        return
    # text formats
    if b"\x00" in data:
        raise UploadError("File does not look like a text file.", 415)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadError("Text files must be UTF-8 encoded.", 415)
    if ext == ".json":
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            raise UploadError("File is not valid JSON.", 415)


def inspect(filename: str, data: bytes) -> str:
    """Validate an upload without storing it. Returns the detected extension (e.g. '.zip')."""
    if not filename:
        raise UploadError("A file is required.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise UploadError(f"File exceeds the {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.", 413)
    ext = detect_extension(filename)
    _validate_content(ext, data)
    return ext


def store(user_id: int, filename: str, data: bytes, ext: str) -> Dict[str, object]:
    """Hand already-inspected bytes to the configured storage backend. Returns metadata for the DB row."""
    key = f"user_{int(user_id)}/{uuid.uuid4().hex}{ext}"      # server-generated: no user input in the key
    try:
        backend = storage.get_storage()
        ref = backend.put(key, data)
    except storage.StorageError as e:
        raise UploadError(f"File storage is unavailable: {e}", 503)
    return {
        "file_path": ref,
        "storage_backend": backend.name,
        "original_filename": safe_display_name(filename),
        "file_size": len(data),
        "upload_type": ext.lstrip("."),
    }


def validate_and_store(user_id: int, filename: str, data: bytes) -> Dict[str, object]:
    """inspect() then store(): for uploads that are not deployable manifests."""
    return store(user_id, filename, data, inspect(filename, data))


def delete_stored_file(ref: str, backend_name: str = "local") -> None:
    """Delete a stored upload from the backend that holds it. Raises UploadError(502) on backend failure."""
    try:
        storage.get_storage(backend_name).delete(ref)
    except storage.StorageError as e:
        raise UploadError(str(e), 502)
