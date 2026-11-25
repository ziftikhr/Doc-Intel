"""Output schema of the validation stage (Task 3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from docintel.extraction.schema import ExtractionResult

VALIDATOR_VERSION = "1.0.0"

Severity = Literal["error", "warning", "info"]
# valid: every check passed, safe to process automatically
# needs_review: something looks off - a person should confirm it
# invalid: broken (required value missing, totals do not add up, duplicate) - must be fixed
InvoiceStatus = Literal["valid", "needs_review", "invalid"]
DocumentStatus = Literal["valid", "needs_review", "invalid", "no_invoices", "failed"]
CheckOutcome = Literal["passed", "warning", "failed", "skipped"]


class ValidationIssue(BaseModel):
    code: str = Field(description="Stable machine-readable code, e.g. TOTAL_MISMATCH")
    severity: Severity
    message: str = Field(description="Plain-English explanation for the reviewer")
    field: Optional[str] = Field(default=None, description='e.g. "total" or "line_items[2].amount"')
    details: dict[str, Any] = Field(default_factory=dict, description="Numbers behind the finding")


class InvoiceValidation(BaseModel):
    index: int = Field(description="Matches InvoiceRecord.index")
    invoice_number: Optional[str] = None
    status: InvoiceStatus
    needs_review: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    checks: dict[str, CheckOutcome] = Field(
        default_factory=dict, description="Outcome of every check, so a pass is visible too")

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class DocumentValidation(BaseModel):
    document_id: str
    filename: str
    status: DocumentStatus
    needs_review: bool
    invoices: list[InvoiceValidation] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list, description="Document-level findings")
    validated_at: datetime
    validator_version: str = VALIDATOR_VERSION

    def counts(self) -> dict[str, int]:
        out = {"valid": 0, "needs_review": 0, "invalid": 0}
        for inv in self.invoices:
            out[inv.status] += 1
        return out


class ValidatedDocument(ExtractionResult):
    """Task 2 output plus the Task 3 verdict. ``needs_review``/``review_reasons`` on the
    document and on each invoice are updated to reflect validation."""

    validation: Optional[DocumentValidation] = None
