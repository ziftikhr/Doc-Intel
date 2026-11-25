"""Business logic behind the routes: uploads, reviews, derived document flags."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docintel.db.models import Document, Invoice, Job
from docintel.db.repository import (
    apply_record,
    apply_validation_row,
    known_invoice_refs,
    record_from_row,
    utcnow,
)
from docintel.extraction.normalize import clean_str, fmt_decimal, parse_amount, parse_currency, parse_date
from docintel.extraction.schema import ExtractedField, FieldStatus
from docintel.jobs.queue import enqueue
from docintel.search.embeddings import Embedder, EmbeddingError
from docintel.search.indexer import invoice_summary
from docintel.settings import Settings
from docintel.validation import DuplicateIndex, InvoiceRef, ValidationConfig, validate_invoice

CHUNK = 1024 * 1024
_STATUS_RANK = {"valid": 0, "needs_review": 1, "invalid": 2}


class ServiceError(Exception):
    def __init__(self, status: int, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details or {}


# ------------------------------------------------------------------- upload ----------


@dataclass
class StoredFile:
    sha256: str
    size: int
    path: Path


def safe_filename(name: Optional[str]) -> str:
    base = os.path.basename((name or "upload").replace("\\", "/")).strip() or "upload"
    return "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')[:255] or "upload"


def store_upload(stream: BinaryIO, filename: str, settings: Settings) -> StoredFile:
    """Stream to disk while hashing; enforce the size limit without reading it all into memory.

    Files are stored as ``<root>/ab/<sha256>/<sanitised name>``: the hash directory makes
    identical uploads share one copy and keeps user input out of the directory path; the
    original name is kept because Task 1 uses it (e.g. to label CSV sheets).
    """
    ext = Path(filename).suffix.lower()
    if ext not in settings.allowed_extensions:
        raise ServiceError(415, f"unsupported file type '{ext or '(none)'}'",
                           {"allowed": list(settings.allowed_extensions)})
    root = settings.storage_path
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    fd, tmp_name = tempfile.mkstemp(dir=root / "tmp", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise ServiceError(413, f"file is larger than {settings.max_upload_mb} MB")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ServiceError(400, "the file is empty")
        sha = digest.hexdigest()
        folder = root / sha[:2] / sha
        existing = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
        if existing:
            os.unlink(tmp_name)
            return StoredFile(sha, size, existing[0])
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / filename
        os.replace(tmp_name, final)
        return StoredFile(sha, size, final)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def register_upload(session: Session, stored: StoredFile, filename: str, content_type: Optional[str],
                    settings: Settings, reprocess: bool = False) -> tuple[Document, Optional[Job], bool]:
    """Create the document + job, or return the existing document for a repeated upload."""
    existing = session.scalar(select(Document).where(Document.sha256 == stored.sha256))
    if existing is None:
        doc = Document(sha256=stored.sha256, filename=filename, content_type=content_type,
                       size_bytes=stored.size, storage_path=str(stored.path), status="queued")
        session.add(doc)
        try:
            session.flush()
        except IntegrityError:  # the same file uploaded concurrently
            session.rollback()
            existing = session.scalar(select(Document).where(Document.sha256 == stored.sha256))
        else:
            return doc, enqueue(session, doc.id, settings.job_max_attempts), False
    if reprocess:
        if any(j.status in ("queued", "running") for j in existing.jobs):
            raise ServiceError(409, "this document is already being processed")
        existing.status, existing.error = "queued", None
        return existing, enqueue(session, existing.id, settings.job_max_attempts), True
    latest = existing.jobs[-1] if existing.jobs else None
    return existing, latest, True


# ------------------------------------------------------------------- review ----------


def _parse_correction(name: str, value: Optional[str]) -> ExtractedField:
    raw = clean_str(value)
    if raw is None:
        return ExtractedField(status=FieldStatus.MISSING, source="manual", confidence=1.0)
    if name in ("subtotal", "tax", "total"):
        p = parse_amount(raw)
        if p.value is None:
            raise ServiceError(422, f"'{raw}' is not a valid amount for {name}")
        normalized = fmt_decimal(p.value)
    elif name == "invoice_date":
        d = parse_date(raw)
        if d.value is None:
            hint = f"; it could be {' or '.join(c.isoformat() for c in d.candidates)} - use YYYY-MM-DD" \
                if d.candidates else ""
            raise ServiceError(422, f"'{raw}' is not an unambiguous date{hint}")
        normalized = d.value.isoformat()
    elif name == "currency":
        c = parse_currency(raw)
        if c.code is None:
            raise ServiceError(422, f"'{raw}' is not a recognised currency")
        normalized = c.code
    else:
        normalized = raw
    return ExtractedField(value=normalized, raw=raw, status=FieldStatus.FOUND, confidence=1.0,
                          source="manual", evidence=None, page=None)


def review_invoice(session: Session, inv: Invoice, *, action: Optional[str], corrections: dict,
                   note: Optional[str], force: bool, config: ValidationConfig,
                   embedder: Optional[Embedder]) -> Invoice:
    if corrections:
        rec = record_from_row(inv)
        for name, value in corrections.items():
            setattr(rec, name, _parse_correction(name, value))
        apply_record(inv, rec)
        known = known_invoice_refs(session, [rec], exclude_invoice=inv.id, created_before=inv.created_at)
        index = DuplicateIndex(known)
        ref = InvoiceRef.from_record(rec, source=f"invoice:{inv.id}", document_id=str(inv.document_id),
                                     filename=inv.document.filename)
        apply_validation_row(inv, validate_invoice(rec, config, duplicate_issues=index.check(ref)))
        inv.corrected_fields = sorted(set(inv.corrected_fields or []) | set(corrections))
        inv.summary_text = invoice_summary(inv)
        _reembed(inv, embedder)
        if inv.review_status != "pending" and action is None:
            inv.review_status = "pending"  # the data changed after the decision

    if action == "approve":
        if inv.validation_status == "invalid" and not force:
            errors = [i.message for i in inv.issues if i.severity == "error"]
            raise ServiceError(409, "the invoice still has validation errors; correct them or set force=true",
                               {"errors": errors})
        inv.review_status, inv.needs_review = "approved", False
    elif action == "reject":
        inv.review_status, inv.needs_review = "rejected", False
    elif action == "reopen":
        inv.review_status = "pending"
        inv.needs_review = inv.validation_status != "valid"
    if action:
        inv.reviewed_at = utcnow()
    if note is not None:
        inv.review_note = note
    session.flush()
    refresh_document(inv.document)
    return inv


def _reembed(inv: Invoice, embedder: Optional[Embedder]) -> None:
    if embedder is None:
        inv.embedding, inv.embedding_model = None, None
        return
    try:
        inv.embedding = embedder.embed_documents([inv.summary_text])[0]
        inv.embedding_model = embedder.model
    except EmbeddingError:
        inv.embedding, inv.embedding_model = None, None  # keyword search still finds it


def refresh_document(doc: Document) -> None:
    """Document flags follow its invoices: worst validation status, any open review."""
    invoices = doc.invoices
    if not invoices:
        return
    doc.validation_status = max((i.validation_status for i in invoices), key=_STATUS_RANK.__getitem__)
    doc.needs_review = any(i.needs_review for i in invoices)


def get_or_404(session: Session, model, id_: uuid.UUID, what: str):
    obj = session.get(model, id_)
    if obj is None:
        raise ServiceError(404, f"{what} not found")
    return obj
