"""Safe ZIP reading (SEC-13): zip-slip, symlinks, zip bombs, file type sniffing.

Nothing is written to disk here; entries are validated and returned as bytes
so the caller decides where they are stored.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

ALLOWED_EXT = {"jpg", "jpeg", "png", "mp4", "mov", "pdf"}
MAX_ENTRIES = 10_000
MAX_RATIO = 100  # compressed -> uncompressed
MAX_TOTAL_BYTES = 1_073_741_824  # 1 GB per batch


class UnsafeZipError(ValueError):
    """The archive is unsafe or malformed."""


@dataclass(frozen=True)
class MediaFile:
    name: str  # base name, e.g. C001.jpg
    stem: str  # C001
    ext: str  # jpg
    mime: str
    data: bytes
    sha256: str


@dataclass
class ZipReadResult:
    files: list[MediaFile] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)


def sniff(data: bytes) -> str | None:
    """Return a mime type from magic bytes, or None if unrecognised."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    return None


_EXT_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}
_COMPATIBLE = {
    "video/mp4": {"video/mp4", "video/quicktime"},
    "video/quicktime": {"video/quicktime", "video/mp4"},
}


def _bad_path(name: str) -> bool:
    if name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
        return True
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return ".." in parts


def read_zip(raw: bytes, max_total: int = MAX_TOTAL_BYTES) -> ZipReadResult:
    """Validate and read all media entries of a ZIP. Raises UnsafeZipError for hostile archives."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise UnsafeZipError("not a valid ZIP file") from exc

    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise UnsafeZipError(f"too many entries ({len(infos)} > {MAX_ENTRIES})")

    result = ZipReadResult()
    total = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        if _bad_path(name):
            raise UnsafeZipError(f"unsafe path in archive: {name!r}")
        # symlink: unix mode bits in external_attr
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise UnsafeZipError(f"symlink in archive: {name!r}")
        if info.flag_bits & 0x1:
            raise UnsafeZipError("encrypted archives are not supported")
        if (
            info.compress_size
            and info.file_size / info.compress_size > MAX_RATIO
            and info.file_size > 1_000_000
        ):
            raise UnsafeZipError(f"suspicious compression ratio for {name!r}")
        total += info.file_size
        if total > max_total:
            raise UnsafeZipError("archive is too large when extracted")

        base = PurePosixPath(name.replace("\\", "/")).name
        if base.startswith(".") or base.lower() in ("thumbs.db",):
            continue  # OS metadata, silently ignored
        stem, _, ext = base.rpartition(".")
        ext = ext.lower()
        if not stem or ext not in ALLOWED_EXT:
            result.rejected.append((base, "unsupported file type"))
            continue
        with zf.open(info) as fh:
            data = fh.read(info.file_size + 1)
        if len(data) != info.file_size:
            raise UnsafeZipError(f"size mismatch for {name!r}")
        detected = sniff(data)
        expected = _EXT_MIME[ext]
        if detected is None or detected not in _COMPATIBLE.get(expected, {expected}):
            result.rejected.append((base, "content does not match file extension (corrupt?)"))
            continue
        result.files.append(
            MediaFile(base, stem, ext, detected, data, hashlib.sha256(data).hexdigest())
        )
    return result
