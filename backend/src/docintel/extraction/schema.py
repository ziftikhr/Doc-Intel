"""Output schema of the extraction stage (Task 2), consumed by validation (Task 3).

Two layers:

* ``Raw*`` models - what an extractor (LLM, rules or spreadsheet mapper) *claims*:
  values exactly as printed plus the verbatim text they were read from.
* ``InvoiceRecord`` / ``ExtractionResult`` - what survives grounding and
  normalisation: typed values, per-field confidence, source page and issues.

Amounts are serialised as decimal strings ("205.00") so no float rounding can
ever creep into financial values.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

EXTRACTOR_VERSION = "1.0.0"

HEADER_FIELDS = (
    "invoice_number", "invoice_date", "supplier_name", "customer_name",
    "subtotal", "tax", "total", "currency",
)
AMOUNT_FIELDS = ("subtotal", "tax", "total")
TEXT_FIELDS = ("invoice_number", "supplier_name", "customer_name")

DocumentType = Literal["invoice", "receipt", "credit_note", "other"]


# ------------------------------------------------ raw (extractor output) ----------
# These are also the LLM's structured-output schema: every field is required and
# nullable, so the model must state "null" explicitly instead of omitting a field.


class RawField(BaseModel):
    value: Optional[str] = Field(
        description="The value exactly as printed in the document, or null if it is not present.")
    evidence: Optional[str] = Field(
        description="Verbatim excerpt (one line, copied character for character) that contains the value.")
    page: Optional[int] = Field(description="Page number the evidence is on.")


class RawLineItem(BaseModel):
    description: Optional[str]
    quantity: Optional[str]
    unit_price: Optional[str]
    amount: Optional[str]
    evidence: Optional[str] = Field(description="The verbatim line (or row) this item was read from.")
    page: Optional[int]


class RawCharge(BaseModel):
    label: Optional[str] = Field(description='The label as printed, e.g. "Shipping & Handling", "Discount (2%)".')
    amount: Optional[str] = Field(
        description="The amount as printed, keeping a minus sign or brackets if the document shows one.")
    evidence: Optional[str] = Field(description="The verbatim line this charge was read from.")
    page: Optional[int]


class RawInvoice(BaseModel):
    document_type: DocumentType
    invoice_number: RawField
    invoice_date: RawField
    supplier_name: RawField
    customer_name: RawField
    subtotal: RawField
    tax: RawField
    total: RawField
    currency: RawField
    line_items: list[RawLineItem]
    other_charges: list[RawCharge] = Field(
        description="Amounts printed between the subtotal and the total other than tax: shipping, handling, "
                    "freight, discounts, fees, rounding. Empty if there are none.")


class LLMExtraction(BaseModel):
    """Structured output requested from the LLM for one segment of pages."""

    invoices: list[RawInvoice] = Field(
        description="One entry per distinct invoice/receipt in the text; empty if there is none.")


def empty_raw_field() -> RawField:
    return RawField(value=None, evidence=None, page=None)


# -------------------------------------------------------- final output ----------


class FieldStatus(str, Enum):
    FOUND = "found"  # grounded in the source and parsed
    MISSING = "missing"  # not present in the document
    UNCERTAIN = "uncertain"  # something was found but it could not be trusted/parsed


class FieldIssue(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    UNGROUNDED = "UNGROUNDED"  # extractor's value is not in the source text -> discarded
    INVALID_NUMBER = "INVALID_NUMBER"
    INVALID_DATE = "INVALID_DATE"
    AMBIGUOUS_DATE = "AMBIGUOUS_DATE"
    CURRENCY_INFERRED = "CURRENCY_INFERRED"  # e.g. "$" -> USD
    CURRENCY_CONFLICT = "CURRENCY_CONFLICT"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"  # LLM and rules found different values
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


Source = Literal["llm", "rules", "llm+rules", "spreadsheet", "manual"]  # manual = corrected by a reviewer


class ExtractedField(BaseModel):
    value: Optional[str] = None  # str / ISO date / decimal string / ISO currency code
    raw: Optional[str] = None  # as printed
    status: FieldStatus = FieldStatus.MISSING
    confidence: float = 0.0
    source: Optional[Source] = None
    page: Optional[int] = None
    evidence: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)  # e.g. both readings of 03/04/2024
    issues: list[FieldIssue] = Field(default_factory=list)


class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[str] = None
    unit_price: Optional[str] = None
    amount: Optional[str] = None
    page: Optional[int] = None
    evidence: Optional[str] = None
    source: Optional[Source] = None
    confidence: float = 0.0
    issues: list[FieldIssue] = Field(default_factory=list)


ChargeKind = Literal["shipping", "discount", "fee", "rounding", "other"]


class Charge(BaseModel):
    """An amount between subtotal and total other than tax (shipping, discount, fee...).

    ``amount`` is signed as it affects the total: discounts are negative."""

    kind: ChargeKind = "other"
    label: Optional[str] = None
    amount: Optional[str] = None  # decimal string; None if printed but unreadable
    raw: Optional[str] = None
    page: Optional[int] = None
    evidence: Optional[str] = None
    source: Optional[Source] = None
    confidence: float = 0.0
    issues: list[FieldIssue] = Field(default_factory=list)


class InvoiceRecord(BaseModel):
    index: int = Field(description="0-based position of this invoice within the document")
    document_type: DocumentType = "invoice"
    invoice_number: ExtractedField = Field(default_factory=ExtractedField)
    invoice_date: ExtractedField = Field(default_factory=ExtractedField)
    supplier_name: ExtractedField = Field(default_factory=ExtractedField)
    customer_name: ExtractedField = Field(default_factory=ExtractedField)
    subtotal: ExtractedField = Field(default_factory=ExtractedField)
    tax: ExtractedField = Field(default_factory=ExtractedField)
    total: ExtractedField = Field(default_factory=ExtractedField)
    currency: ExtractedField = Field(default_factory=ExtractedField)
    line_items: list[LineItem] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    pages: list[int] = Field(default_factory=list)
    source_label: Optional[str] = None  # e.g. "Sheet: Invoices row 4"
    method: str = "rules"
    confidence: float = 0.0
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    # Notes about how the invoice was extracted (not tied to one field), e.g.
    # "rules-only extraction (LLM_NO_CREDIT)". Kept apart from review_reasons,
    # which validation rewrites.
    extraction_notes: list[str] = Field(default_factory=list)

    def field(self, name: str) -> ExtractedField:
        return getattr(self, name)

    def to_simple(self) -> dict:
        """Flat {field: value} view - handy for evaluation and display."""
        out: dict = {f: self.field(f).value for f in HEADER_FIELDS}
        out["line_items"] = [
            {k: getattr(li, k) for k in ("description", "quantity", "unit_price", "amount")}
            for li in self.line_items
        ]
        out["charges"] = [{"kind": c.kind, "label": c.label, "amount": c.amount} for c in self.charges]
        return out


class LLMUsage(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    duration_ms: float = 0.0


ExtractionStatus = Literal["success", "partial", "no_invoice_found", "failed"]


class ExtractionResult(BaseModel):
    document_id: str
    filename: str
    status: ExtractionStatus
    invoices: list[InvoiceRecord] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list, description="Document-level issues")
    needs_review: bool = False
    llm: LLMUsage = Field(default_factory=LLMUsage)
    extracted_at: datetime
    duration_ms: float = 0.0
    extractor_version: str = EXTRACTOR_VERSION
