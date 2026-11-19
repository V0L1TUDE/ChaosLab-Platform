"""
Bounded archive inspection (ZIP / TAR / TAR.GZ)
===============================================
Uploads are inspected IN MEMORY and never extracted to disk. Every check is bounded, so a hostile archive
(zip bomb, gzip bomb, millions of entries, lying headers, deep paths) costs at most a fixed amount of CPU,
memory and time before it is rejected:

  * compressed size      -> the HTTP layer and uploads.MAX_UPLOAD_BYTES (checked before we get here)
  * entry count          -> MAX_ENTRIES (central directory / tar headers)
  * declared size        -> sum of header sizes <= MAX_UNCOMPRESSED_BYTES, per-entry ratio <= MAX_RATIO
  * actual size          -> decompression is streamed in chunks and stopped at the budget (headers can lie)
  * paths                -> no absolute / drive / '..' / NUL / control characters, bounded length and depth
  * entry types          -> no symlinks, hard links, devices or FIFOs; no encrypted ZIP entries
  * nested archives      -> treated as opaque bytes: never opened or decompressed
  * time                 -> a deadline for the whole inspection

Every failure raises ArchiveRejected with a short, user-facing message (a controlled 4xx, never a 500).
"""

import gzip
import io
import re
import stat
import tarfile
import time
import zipfile
import zlib
from typing import Callable, Iterator, List, Tuple

MAX_ENTRIES = 1000
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024      # total, after decompression
MAX_ENTRY_BYTES = 16 * 1024 * 1024             # a single member, after decompression
MAX_RATIO = 200                                # per-entry decompressed : compressed
MAX_PATH_LENGTH = 255
MAX_PATH_DEPTH = 16
TIME_BUDGET_SECONDS = 5.0
CHUNK = 64 * 1024

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ArchiveRejected(Exception):
    """The archive is malformed, unsafe or exceeds an inspection limit."""


class Budget:
    """Decompressed-byte and wall-clock budget shared by one inspection."""

    def __init__(self, max_bytes: int = None, seconds: float = None):
        self.limit = MAX_UNCOMPRESSED_BYTES if max_bytes is None else max_bytes
        self.left = self.limit
        self.deadline = time.monotonic() + (TIME_BUDGET_SECONDS if seconds is None else seconds)

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise ArchiveRejected("Archive inspection took too long; the archive was rejected.")

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise ArchiveRejected(f"Archive expands beyond the {max(1, self.limit // (1024 * 1024))} MB "
                                  "decompressed limit.")
        self.tick()


def check_member_path(name: str) -> None:
    norm = name.replace("\\", "/")
    if not norm or len(norm) > MAX_PATH_LENGTH:
        raise ArchiveRejected("Archive contains an entry with an empty or overly long path.")
    if _CONTROL.search(norm):
        raise ArchiveRejected("Archive contains an entry name with control characters.")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
        raise ArchiveRejected("Archive contains unsafe paths (absolute or '..').")
    if len([p for p in norm.split("/") if p]) > MAX_PATH_DEPTH:
        raise ArchiveRejected(f"Archive paths are nested deeper than {MAX_PATH_DEPTH} levels.")


# ----------------------------------------------------------------------------- ZIP
def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return bool(mode) and (stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode)
                           or stat.S_ISSOCK(mode))


def zip_entries(data: bytes) -> Tuple[zipfile.ZipFile, List[zipfile.ZipInfo]]:
    """Open a ZIP and validate its central directory (no decompression). Caller closes the ZipFile."""
    if not data.startswith(b"PK\x03\x04"):
        raise ArchiveRejected("File content is not a valid ZIP archive.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError, NotImplementedError):
        raise ArchiveRejected("File content is not a valid ZIP archive.")
    if len(infos) > MAX_ENTRIES:
        zf.close()
        raise ArchiveRejected(f"Archive has too many entries (max {MAX_ENTRIES}).")
    declared = 0
    try:
        for info in infos:
            check_member_path(info.filename)
            if info.flag_bits & 0x1:
                raise ArchiveRejected("Encrypted ZIP entries are not accepted.")
            if _zip_is_symlink(info):
                raise ArchiveRejected("Archive contains symlinks or special files.")
            if info.file_size > MAX_ENTRY_BYTES:
                raise ArchiveRejected(f"{info.filename}: larger than {MAX_ENTRY_BYTES // (1024 * 1024)} MB uncompressed.")
            if info.file_size > max(info.compress_size, 1) * MAX_RATIO and info.file_size > 1024 * 1024:
                raise ArchiveRejected(f"{info.filename}: suspicious compression ratio (possible zip bomb).")
            declared += info.file_size
        if declared > MAX_UNCOMPRESSED_BYTES:
            raise ArchiveRejected(f"Archive expands beyond the {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB "
                                  "decompressed limit.")
    except ArchiveRejected:
        zf.close()
        raise
    return zf, infos


def read_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: Budget, limit: int) -> bytes:
    """Decompress one member in chunks, never more than `limit` bytes (or the shared budget). The CRC is verified
    by zipfile when the member is read to the end."""
    out = bytearray()
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                out += chunk
                budget.spend(len(chunk))
                if len(out) > limit:
                    raise ArchiveRejected(f"{info.filename}: larger than its declared size or the per-file limit.")
    except ArchiveRejected:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError, RuntimeError):
        raise ArchiveRejected(f"{info.filename}: corrupted ZIP entry.")
    return bytes(out)


def inspect_zip(data: bytes, budget: Budget = None) -> List[str]:
    """Validate a ZIP fully (headers + bounded decompression + CRC) without keeping the contents."""
    budget = budget or Budget()
    zf, infos = zip_entries(data)
    with zf:
        for info in infos:
            budget.tick()
            if not info.is_dir():
                read_zip_member(zf, info, budget, min(info.file_size, MAX_ENTRY_BYTES))
    return [i.filename for i in infos]


# ----------------------------------------------------------------------------- TAR / TAR.GZ
class _BoundedReader(io.RawIOBase):
    """Wraps a decompressing stream and stops it once the budget is spent."""

    def __init__(self, raw, budget: Budget):
        self.raw, self.budget = raw, budget

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        chunk = self.raw.read(min(len(b), CHUNK))
        self.budget.spend(len(chunk))
        b[:len(chunk)] = chunk
        return len(chunk)


def _tar_stream(data: bytes, gz: bool, budget: Budget):
    src = gzip.GzipFile(fileobj=io.BytesIO(data)) if gz else io.BytesIO(data)
    return io.BufferedReader(_BoundedReader(src, budget), buffer_size=CHUNK)


def iter_tar(data: bytes, gz: bool, budget: Budget = None,
             want: Callable[[tarfile.TarInfo], bool] = lambda m: False) -> Iterator[Tuple[tarfile.TarInfo, bytes]]:
    """Stream the members of a tar (optionally gzip) archive in order, with every check applied. Yields
    (member, contents) for members where `want(member)` is true; others are skipped (their bytes still count
    against the decompression budget, because a streamed tar has to be read through)."""
    budget = budget or Budget()
    if gz and data[:2] != b"\x1f\x8b":
        raise ArchiveRejected("File content is not a valid gzip archive.")
    count = 0
    try:
        with tarfile.open(fileobj=_tar_stream(data, gz, budget), mode="r|") as tf:
            for member in tf:
                count += 1
                budget.tick()
                if count > MAX_ENTRIES:
                    raise ArchiveRejected(f"Archive has too many entries (max {MAX_ENTRIES}).")
                check_member_path(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ArchiveRejected("Archive contains symlinks, hard links or special files.")
                if member.size > MAX_ENTRY_BYTES:
                    raise ArchiveRejected(f"{member.name}: larger than {MAX_ENTRY_BYTES // (1024 * 1024)} MB.")
                if member.isfile() and want(member):
                    fh = tf.extractfile(member)
                    yield member, fh.read(member.size) if fh else b""
    except ArchiveRejected:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError):
        raise ArchiveRejected("File content is not a valid TAR archive.")


def inspect_tar(data: bytes, gz: bool, budget: Budget = None) -> int:
    n = 0
    for _ in iter_tar(data, gz, budget):
        n += 1
    return n
