"""File-type detection from content (magic bytes), never from the extension alone.

Trusting extensions is both a reliability bug (systems export HTML/TSV as ``.xls``,
users rename scans) and a security hole (a crafted file reaching the wrong parser).
"""

from __future__ import annotations

import csv
import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import ProcessingConfig
from .errors import ProcessingError
from .models import FileType, IssueCode

SNIFF_BYTES = 8192

MIME_TYPES = {
    FileType.PDF: "application/pdf",
    FileType.JPEG: "image/jpeg",
    FileType.PNG: "image/png",
    FileType.TIFF: "image/tiff",
    FileType.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileType.XLS: "application/vnd.ms-excel",
    FileType.CSV: "text/csv",
    FileType.UNKNOWN: "application/octet-stream",
}

EXTENSION_TYPES = {
    ".pdf": FileType.PDF,
    ".jpg": FileType.JPEG,
    ".jpeg": FileType.JPEG,
    ".jpe": FileType.JPEG,
    ".png": FileType.PNG,
    ".tif": FileType.TIFF,
    ".tiff": FileType.TIFF,
    ".xlsx": FileType.XLSX,
    ".xlsm": FileType.XLSX,
    ".xls": FileType.XLS,
    ".csv": FileType.CSV,
    ".tsv": FileType.CSV,
    ".txt": FileType.CSV,
}

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Password-protected OOXML files are stored inside an OLE container with this stream.
ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")


@dataclass
class Detection:
    file_type: FileType
    declared_type: FileType | None
    mismatch: bool
    note: str = ""

    @property
    def mime_type(self) -> str:
        return MIME_TYPES[self.file_type]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def detect_file_type(path: Path) -> Detection:
    with open(path, "rb") as fh:
        head = fh.read(SNIFF_BYTES)
    declared = EXTENSION_TYPES.get(path.suffix.lower())
    detected, note = _sniff(path, head, declared)
    mismatch = declared is not None and detected is not FileType.UNKNOWN and declared is not detected
    return Detection(detected, declared, mismatch, note)


def _sniff(path: Path, head: bytes, declared: FileType | None) -> tuple[FileType, str]:
    # The PDF spec tolerates junk before the header; real-world files do have it.
    if b"%PDF-" in head[:1024]:
        return FileType.PDF, ""
    if head.startswith(b"\xff\xd8\xff"):
        return FileType.JPEG, ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType.PNG, ""
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return FileType.TIFF, ""
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(path)
    if head.startswith(OLE_MAGIC):
        if ENCRYPTED_PACKAGE in _read_head(path, 1 << 20):
            raise ProcessingError(
                IssueCode.ENCRYPTED, "Workbook is password-protected (encrypted OOXML package)"
            )
        return FileType.XLS, ""
    if _looks_like_html(head):
        return FileType.UNKNOWN, "HTML document (not a supported spreadsheet/PDF/image)"
    if _looks_like_text(head) and (declared is FileType.CSV or declared is FileType.XLS
                                   or _has_delimited_structure(head)):
        # Many ERP systems export tab/semicolon-delimited text with an .xls extension.
        return FileType.CSV, ""
    return FileType.UNKNOWN, ""


def _read_head(path: Path, n: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def _sniff_zip(path: Path) -> tuple[FileType, str]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ProcessingError(IssueCode.CORRUPTED, f"Damaged ZIP container: {exc}") from exc
    if "xl/workbook.xml" in names or any(n.startswith("xl/") for n in names):
        return FileType.XLSX, ""
    if "word/document.xml" in names:
        return FileType.UNKNOWN, "Word document (.docx) is not a supported input"
    return FileType.UNKNOWN, "ZIP archive that is not an Excel workbook"


def _looks_like_html(head: bytes) -> bool:
    start = head.lstrip()[:200].lower()
    return start.startswith(b"<!doctype html") or start.startswith(b"<html") or b"<table" in start


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 BOM
        return True
    if b"\x00" in head:
        return False
    control = sum(1 for b in head if b < 9 or 13 < b < 32)
    return control / len(head) < 0.01


def _has_delimited_structure(head: bytes) -> bool:
    sample = head.decode("utf-8", errors="replace")
    try:
        csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return True
    except csv.Error:
        return False


def check_zip_safety(path: Path, config: ProcessingConfig) -> None:
    """Reject ZIP bombs before openpyxl decompresses anything."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ProcessingError(IssueCode.CORRUPTED, f"Damaged XLSX container: {exc}") from exc
    total = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos) or 1
    if total > config.max_zip_uncompressed_mb * 1024 * 1024:
        raise ProcessingError(
            IssueCode.DECOMPRESSION_BOMB,
            f"Workbook expands to {total / 2**20:.0f} MB (limit {config.max_zip_uncompressed_mb} MB)",
        )
    if total > 10 * 1024 * 1024 and total / compressed > config.max_zip_ratio:
        raise ProcessingError(
            IssueCode.DECOMPRESSION_BOMB,
            f"Suspicious compression ratio {total / compressed:.0f}:1",
        )
