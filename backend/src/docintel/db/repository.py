"""Moving pipeline results in and out of the database."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from docintel.extraction.normalize import fmt_decimal
from docintel.extraction.schema import (
    HEADER_FIELDS,
    Charge,
    ExtractedField,
    InvoiceRecord,
    LineItem,
)
from docintel.processing.models import ProcessedDocument
from docintel.validation import InvoiceRef, InvoiceValidation, ValidatedDocument
from docintel.validation.checks import amount, norm_invoice_number, norm_party

from .models import Document, Invoice, LineItemRow, Page, ValidationIssueRow


def _dec(v: Optional[str]) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except InvalidOperation:
        return None


def _date(f: ExtractedField) -> Optional[date]:
    if f.status.value != "found" or not f.value:
        return None
    try:
        return date.fromisoformat(f.value[:10])
    except ValueError:
        return None


def _found(f: ExtractedField) -> Optional[str]:
    return f.value if f.status.value == "found" else None


# ------------------------------------------------------------------ writing ----------


def save_processing(session: Session, doc: Document, processed: ProcessedDocument) -> None:
    """Replace the document's pages with the Task 1 output."""
    session.execute(delete(Page).where(Page.document_id == doc.id))
    for p in processed.pages:
        session.add(Page(
            document_id=doc.id, number=p.number, label=p.label, method=p.method.value, text=p.text,
            char_count=p.char_count, ocr_confidence=p.ocr.confidence if p.ocr else None,
            tables=[t.model_dump(mode="json") for t in p.tables],
            issues=[i.model_dump(mode="json") for i in p.issues]))
    doc.detected_type = processed.metadata.detected_type.value
    doc.page_count = processed.metadata.page_count or len(processed.pages)
    doc.processing_status = processed.status.value
    doc.processing = {
        "metadata": processed.metadata.model_dump(mode="json", exclude={"path"}),
        "stats": processed.stats.model_dump(mode="json"),
        "issues": [i.model_dump(mode="json") for i in processed.issues],
        "needs_review": processed.needs_review,
        "review_reasons": processed.review_reasons,
        "duration_ms": processed.duration_ms,
        "processor_version": processed.processor_version,
    }


def _issue_rows(doc_id, invoice_id, issues) -> list[ValidationIssueRow]:
    return [ValidationIssueRow(document_id=doc_id, invoice_id=invoice_id, code=i.code, severity=i.severity,
                               field=i.field, message=i.message, details=i.details) for i in issues]


def invoice_from_record(doc_id: uuid.UUID, rec: InvoiceRecord, validation: Optional[InvoiceValidation]) -> Invoice:
    inv = Invoice(document_id=doc_id, index=rec.index)
    apply_record(inv, rec)
    inv.line_items = [LineItemRow(
        position=i, description=li.description, quantity=_dec(li.quantity), unit_price=_dec(li.unit_price),
        amount=_dec(li.amount), page=li.page, evidence=li.evidence, source=li.source, confidence=li.confidence,
        issues=[x.value for x in li.issues]) for i, li in enumerate(rec.line_items)]
    apply_validation_row(inv, validation, rec.needs_review)
    return inv


def apply_record(inv: Invoice, rec: InvoiceRecord) -> None:
    """Copy header fields (typed columns + full JSON detail) from a record."""
    inv.document_type = rec.document_type
    inv.invoice_number = _found(rec.invoice_number)
    inv.invoice_number_norm = norm_invoice_number(inv.invoice_number)
    inv.invoice_date = _date(rec.invoice_date)
    inv.supplier_name = _found(rec.supplier_name)
    inv.supplier_norm = norm_party(inv.supplier_name)
    inv.customer_name = _found(rec.customer_name)
    inv.subtotal, inv.tax, inv.total = amount(rec.subtotal), amount(rec.tax), amount(rec.total)
    cur = _found(rec.currency)
    inv.currency = cur.upper()[:3] if cur else None
    inv.method, inv.confidence = rec.method, rec.confidence
    inv.pages, inv.source_label = rec.pages, rec.source_label
    inv.fields = {n: rec.field(n).model_dump(mode="json") for n in HEADER_FIELDS}
    inv.charges = [c.model_dump(mode="json") for c in rec.charges]
    inv.extraction_notes = list(rec.extraction_notes)


def apply_validation_row(inv: Invoice, v: Optional[InvoiceValidation], fallback_review: bool = True) -> None:
    if v is None:
        inv.validation_status, inv.needs_review, inv.checks = "needs_review", fallback_review, {}
        inv.issues = []
        return
    inv.validation_status, inv.needs_review, inv.checks = v.status, v.needs_review, v.checks
    inv.issues = _issue_rows(inv.document_id, None, v.issues)  # invoice_id set by the relationship


def save_results(session: Session, doc: Document, validated: ValidatedDocument) -> list[Invoice]:
    """Replace the document's invoices and findings with a new Task 2+3 result."""
    session.execute(delete(ValidationIssueRow).where(ValidationIssueRow.document_id == doc.id))
    session.execute(delete(Invoice).where(Invoice.document_id == doc.id))
    session.flush()
    report = validated.validation
    by_index = {v.index: v for v in report.invoices} if report else {}
    invoices = [invoice_from_record(doc.id, rec, by_index.get(rec.index)) for rec in validated.invoices]
    session.add_all(invoices)
    if report:
        session.add_all(_issue_rows(doc.id, None, report.issues))
    doc.extraction_status = validated.status
    doc.validation_status = report.status if report else None
    doc.needs_review = validated.needs_review
    doc.invoice_count = len(invoices)
    doc.result = validated.model_dump(mode="json")
    session.flush()
    return invoices


# ------------------------------------------------------------------ reading ----------


def record_from_row(inv: Invoice) -> InvoiceRecord:
    """Rebuild the Task 2 record from the stored row (used to re-validate after review)."""
    fields = {n: ExtractedField.model_validate(inv.fields[n]) if n in inv.fields else ExtractedField()
              for n in HEADER_FIELDS}
    items = [LineItem(description=li.description,
                      quantity=None if li.quantity is None else format(li.quantity.normalize(), "f"),
                      unit_price=fmt_decimal(li.unit_price), amount=fmt_decimal(li.amount),
                      page=li.page, evidence=li.evidence, source=li.source, confidence=li.confidence,
                      issues=li.issues or [])
             for li in inv.line_items]
    charges = [Charge.model_validate(c) for c in inv.charges or []]
    return InvoiceRecord(index=inv.index, document_type=inv.document_type, line_items=items, charges=charges,
                         pages=inv.pages or [],
                         source_label=inv.source_label, method=inv.method, confidence=inv.confidence,
                         extraction_notes=inv.extraction_notes or [], **fields)


def known_invoice_refs(session: Session, records: list[InvoiceRecord], *,
                       exclude_document: Optional[uuid.UUID] = None,
                       exclude_invoice: Optional[uuid.UUID] = None,
                       created_before: Optional[datetime] = None) -> list[InvoiceRef]:
    """Stored invoices that could be duplicates of ``records``.

    Only candidates sharing a normalised invoice number, or supplier + total, are
    loaded, so this stays cheap however large the table grows. ``created_before``
    keeps "the first occurrence is never the duplicate" true when an older invoice
    is re-validated after a review correction.
    """
    numbers = {norm_invoice_number(_found(r.invoice_number)) for r in records} - {None}
    suppliers = {norm_party(_found(r.supplier_name)) for r in records} - {None}
    totals = {amount(r.total) for r in records} - {None}
    conds = []
    if numbers:
        conds.append(Invoice.invoice_number_norm.in_(numbers))
    if suppliers and totals:
        conds.append(Invoice.supplier_norm.in_(suppliers) & Invoice.total.in_(totals))
    if not conds:
        return []
    stmt = select(Invoice, Document.filename).join(Document).where(or_(*conds))
    if exclude_document is not None:
        stmt = stmt.where(Invoice.document_id != exclude_document)
    if exclude_invoice is not None:
        stmt = stmt.where(Invoice.id != exclude_invoice)
    if created_before is not None:
        stmt = stmt.where(Invoice.created_at < created_before)
    stmt = stmt.order_by(Invoice.created_at)
    return [ref_from_row(inv, filename) for inv, filename in session.execute(stmt).all()]


def ref_from_row(inv: Invoice, filename: str) -> InvoiceRef:
    return InvoiceRef(source=f"invoice:{inv.id}", document_id=str(inv.document_id), filename=filename,
                      index=inv.index, invoice_number=inv.invoice_number, supplier=inv.supplier_name,
                      invoice_date=inv.invoice_date.isoformat() if inv.invoice_date else None,
                      total=inv.total, currency=inv.currency)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

