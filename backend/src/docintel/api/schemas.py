"""Request/response models of the REST API (also drive the OpenAPI docs at /docs)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from docintel.extraction.normalize import fmt_decimal

T = TypeVar("T")

# The database keeps NUMERIC(18,4); the API shows money as "225.50" (more decimals
# only when they carry information) and quantities without trailing zeros, as strings
# so no client ever turns them into floats.
Money = Annotated[Decimal, PlainSerializer(fmt_decimal, return_type=str, when_used="json")]
Quantity = Annotated[Decimal, PlainSerializer(lambda d: format(d.normalize(), "f"), return_type=str,
                                             when_used="json")]

STAGE_PROGRESS = {None: 0, "processing": 10, "extracting": 40, "validating": 75, "indexing": 90, "done": 100}


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class JobOut(ORM):
    id: uuid.UUID
    document_id: uuid.UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    stage: Optional[str] = None
    progress: int = Field(0, description="Rough percentage based on the stage")
    attempts: int
    max_attempts: int
    error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @classmethod
    def of(cls, job) -> "JobOut":
        out = cls.model_validate(job)
        out.progress = 100 if job.status in ("succeeded", "failed") else STAGE_PROGRESS.get(job.stage, 0)
        if job.status == "queued" and job.attempts:
            out.stage = "waiting to retry"
        return out


class DocumentSummary(ORM):
    id: uuid.UUID
    filename: str
    status: Literal["queued", "processing", "processed", "failed"]
    detected_type: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: int
    page_count: int
    sha256: str
    invoice_count: int
    validation_status: Optional[str] = None
    needs_review: bool
    search_status: str
    error: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None


class UploadResult(BaseModel):
    document: DocumentSummary
    job: Optional[JobOut] = None
    duplicate_upload: bool = Field(False, description="This exact file was uploaded before")
    message: str


class IssueOut(ORM):
    code: str
    severity: Literal["error", "warning", "info"]
    field: Optional[str] = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class InvoiceSummary(ORM):
    id: uuid.UUID
    document_id: uuid.UUID
    filename: Optional[str] = None
    index: int
    document_type: str
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    supplier_name: Optional[str] = None
    customer_name: Optional[str] = None
    subtotal: Optional[Money] = None
    tax: Optional[Money] = None
    total: Optional[Money] = None
    currency: Optional[str] = None
    confidence: float
    method: str
    validation_status: str
    needs_review: bool
    review_status: str
    errors: int = 0
    warnings: int = 0
    created_at: datetime


class LineItemOut(ORM):
    position: int
    description: Optional[str] = None
    quantity: Optional[Quantity] = None
    unit_price: Optional[Money] = None
    amount: Optional[Money] = None
    page: Optional[int] = None
    evidence: Optional[str] = None
    source: Optional[str] = None
    confidence: float
    issues: list[str] = Field(default_factory=list)


class ChargeOut(BaseModel):
    kind: Literal["shipping", "discount", "fee", "rounding", "other"] = "other"
    label: Optional[str] = None
    amount: Optional[str] = Field(None, description="Signed as it affects the total: discounts are negative")
    raw: Optional[str] = None
    page: Optional[int] = None
    evidence: Optional[str] = None
    source: Optional[str] = None
    confidence: float = 0.0
    issues: list[str] = Field(default_factory=list)


class InvoiceDetail(InvoiceSummary):
    fields: dict[str, Any] = Field(description="Per field: value, raw text, status, confidence, source, page, "
                                               "evidence line, issues")
    line_items: list[LineItemOut] = Field(default_factory=list)
    charges: list[ChargeOut] = Field(default_factory=list,
                                     description="Shipping, discounts, fees... printed between subtotal and total")
    issues: list[IssueOut] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict)
    extraction_notes: list[str] = Field(default_factory=list)
    pages: list[int] = Field(default_factory=list)
    source_label: Optional[str] = None
    review_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    corrected_fields: list[str] = Field(default_factory=list)


class DocumentDetail(DocumentSummary):
    processing: dict[str, Any] = Field(default_factory=dict, description="Task 1 metadata, stats and issues")
    issues: list[IssueOut] = Field(default_factory=list, description="Document-level validation findings")
    invoices: list[InvoiceSummary] = Field(default_factory=list)
    jobs: list[JobOut] = Field(default_factory=list)


class PageOut(ORM):
    number: int
    label: str
    method: str
    char_count: int
    ocr_confidence: Optional[float] = None
    text: str


CorrectableField = Literal["invoice_number", "invoice_date", "supplier_name", "customer_name",
                           "subtotal", "tax", "total", "currency"]


class ReviewRequest(BaseModel):
    action: Optional[Literal["approve", "reject", "reopen"]] = Field(
        None, description="Omit to only save corrections")
    corrections: dict[CorrectableField, Optional[str]] = Field(
        default_factory=dict, description='e.g. {"total": "225.50", "invoice_date": "15.03.2024"}; '
                                          "null clears a field. Validation runs again afterwards.")
    note: Optional[str] = Field(None, max_length=2000)
    force: bool = Field(False, description="Approve even though validation still reports errors")


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: str
    llm: str
    embeddings: str
    workers: int
    queue: dict[str, int]


class StatsOut(BaseModel):
    documents: dict[str, int]
    invoices: dict[str, int]
    review: dict[str, int]
    needs_review: int
    totals_by_currency: dict[str, Money]
    top_issues: list[dict[str, Any]]
    queue: dict[str, int]
