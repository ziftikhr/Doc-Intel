"""Run the checks and turn their findings into a verdict.

    invoice status = invalid       if any finding is an error
                     needs_review  if any finding is a warning
                     valid         otherwise ("info" findings never block)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional

from docintel.extraction.schema import ExtractionResult, InvoiceRecord

from .checks import CHECKS, Context, issue
from .config import ValidationConfig
from .duplicates import DuplicateIndex, InvoiceRef
from .models import CheckOutcome, DocumentValidation, InvoiceValidation, ValidatedDocument, ValidationIssue

_STATUS_RANK = {"valid": 0, "needs_review": 1, "invalid": 2}


def _outcome(issues: Optional[list[ValidationIssue]]) -> CheckOutcome:
    if issues is None:
        return "skipped"
    if any(i.severity == "error" for i in issues):
        return "failed"
    if any(i.severity == "warning" for i in issues):
        return "warning"
    return "passed"


def _status(issues: list[ValidationIssue]) -> str:
    if any(i.severity == "error" for i in issues):
        return "invalid"
    if any(i.severity == "warning" for i in issues):
        return "needs_review"
    return "valid"


def validate_invoice(inv: InvoiceRecord, config: Optional[ValidationConfig] = None, *,
                     today: Optional[date] = None,
                     duplicate_issues: Optional[list[ValidationIssue]] = None) -> InvoiceValidation:
    ctx = Context(config=config or ValidationConfig(), today=today or date.today())
    issues: list[ValidationIssue] = []
    checks: dict[str, CheckOutcome] = {}
    for name, check in CHECKS:
        found = check(inv, ctx)
        checks[name] = _outcome(found)
        issues.extend(found or [])
    if duplicate_issues is not None:
        checks["duplicates"] = _outcome(duplicate_issues)
        issues.extend(duplicate_issues)
    status = _status(issues)
    number = inv.invoice_number.value or inv.invoice_number.raw
    return InvoiceValidation(index=inv.index, invoice_number=number, status=status,
                             needs_review=status != "valid", issues=issues, checks=checks)


def validate_document(result: ExtractionResult, config: Optional[ValidationConfig] = None, *,
                      duplicates: Optional[DuplicateIndex] = None, source: Optional[str] = None,
                      today: Optional[date] = None) -> DocumentValidation:
    """Validate every invoice of one document.

    ``duplicates`` is shared across documents to catch repeats between them; when it
    is omitted a fresh index is used, so repeats *within* this document are still found.
    """
    index = duplicates if duplicates is not None else DuplicateIndex()
    source = source or result.document_id
    doc_issues: list[ValidationIssue] = []

    if result.status == "failed":
        doc_issues.append(issue("EXTRACTION_FAILED", "error",
                                "The document could not be read: " + "; ".join(result.issues or ["unknown error"])))
        return _document(result, "failed", True, [], doc_issues)

    notes = [n for n in result.issues if not n.startswith("NO_INVOICE_FOUND")]
    incomplete = False
    for note in notes:
        if note.startswith("SOURCE_INCOMPLETE"):
            incomplete = True
            doc_issues.append(issue("SOURCE_INCOMPLETE", "warning",
                                    "The file was damaged (" + note.split(":", 1)[1].strip()
                                    + "). Invoices or pages may be missing; compare with the original."))
        else:
            doc_issues.append(issue("EXTRACTION_ISSUE", "info", f"Extraction: {note}"))

    if not result.invoices:
        # When the AI read the document and found no invoice, that is a normal outcome
        # (a letter, a report...). If only the text rules looked at it (no key, outage),
        # the "no invoice" answer is doubtful - rules miss unusual layouts.
        doubtful = bool(notes)
        doc_issues.append(issue("NO_INVOICES", "warning" if doubtful else "info",
                                "No invoice was found in this document."
                                + (" Extraction had problems, so confirm." if doubtful else "")))
        return _document(result, "no_invoices", doubtful, [], doc_issues)

    validations = []
    for inv in result.invoices:
        ref = InvoiceRef.from_record(inv, source=source, document_id=result.document_id, filename=result.filename)
        validations.append(validate_invoice(inv, config, today=today, duplicate_issues=index.check_and_add(ref)))
    status = max((v.status for v in validations), key=_STATUS_RANK.__getitem__)
    if incomplete and status == "valid":
        status = "needs_review"  # every invoice read is fine, but some may not have been read
    return _document(result, status, status != "valid", validations, doc_issues)


def _document(result, status, needs_review, invoices, issues) -> DocumentValidation:
    return DocumentValidation(document_id=result.document_id, filename=result.filename, status=status,
                              needs_review=needs_review, invoices=invoices, issues=issues,
                              validated_at=datetime.now(timezone.utc))


def validate_batch(results: Iterable[ExtractionResult], config: Optional[ValidationConfig] = None, *,
                   known: Iterable[InvoiceRef] = (), sources: Optional[list[str]] = None,
                   today: Optional[date] = None) -> list[DocumentValidation]:
    """Validate several documents, detecting duplicates across all of them (and ``known``)."""
    index = DuplicateIndex(known)
    out = []
    seen_docs: dict[str, str] = {}
    for i, result in enumerate(results):
        source = sources[i] if sources else f"{result.document_id}#{i}"
        report = validate_document(result, config, duplicates=index, source=source, today=today)
        if result.document_id in seen_docs:
            report.issues.append(issue("DUPLICATE_DOCUMENT", "warning",
                                       f"This file is byte-for-byte identical to {seen_docs[result.document_id]}.",
                                       duplicate_of=seen_docs[result.document_id]))
            report.needs_review = True
            if report.status in ("valid", "no_invoices"):
                report.status = "needs_review"
        seen_docs.setdefault(result.document_id, result.filename)
        out.append(report)
    return out


def apply_validation(result: ExtractionResult, report: DocumentValidation) -> ValidatedDocument:
    """Attach the report and make the review flags reflect it.

    Each invoice's ``review_reasons`` becomes the list of error/warning messages, so a
    reviewer sees one consistent list (Task 2's own doubts are included as checks).
    """
    doc = ValidatedDocument(**result.model_dump(), validation=report)
    by_index = {v.index: v for v in report.invoices}
    for inv in doc.invoices:
        v = by_index.get(inv.index)
        if v is None:
            continue
        inv.needs_review = v.needs_review
        inv.review_reasons = [f"[{i.severity}] {i.message}" for i in v.issues if i.severity != "info"]
    doc.needs_review = report.needs_review
    return doc
