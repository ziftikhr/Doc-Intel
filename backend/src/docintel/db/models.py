"""Database schema (PostgreSQL + pgvector).

documents ─┬─ pages                 Task 1 text per page
           ├─ invoices ─┬─ line_items
           │            └─ validation_issues (invoice-level)
           ├─ validation_issues    (document-level)
           ├─ chunks                page text split for semantic search
           └─ jobs                  background processing queue

Typed columns (dates, NUMERIC amounts, normalised keys) are what filters, sorting and
duplicate checks run on. The full Task 2/3 detail (evidence, per-field confidence)
is kept in JSONB next to them, and the untouched machine output in documents.result.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from docintel.settings import EMBEDDING_DIM


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now_col():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[Optional[str]] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(String(1024))

    # queued -> processing -> processed | failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    detected_type: Mapped[Optional[str]] = mapped_column(String(32))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    processing_status: Mapped[Optional[str]] = mapped_column(String(20))  # Task 1
    extraction_status: Mapped[Optional[str]] = mapped_column(String(20))  # Task 2
    validation_status: Mapped[Optional[str]] = mapped_column(String(20), index=True)  # Task 3
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    invoice_count: Mapped[int] = mapped_column(Integer, default=0)
    # pending | indexed (semantic + keyword) | keyword_only (embeddings unavailable) | empty
    search_status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[Optional[str]] = mapped_column(Text)
    processing: Mapped[dict[str, Any]] = mapped_column(default=dict)  # Task 1 metadata/stats/issues
    result: Mapped[Optional[dict[str, Any]]] = mapped_column()  # Task 2+3 output as produced

    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    pages: Mapped[list["Page"]] = relationship(back_populates="document", cascade="all, delete-orphan",
                                               passive_deletes=True, order_by="Page.number")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="document", cascade="all, delete-orphan",
                                                     passive_deletes=True, order_by="Invoice.index")
    issues: Mapped[list["ValidationIssueRow"]] = relationship(
        primaryjoin="and_(Document.id == ValidationIssueRow.document_id, ValidationIssueRow.invoice_id.is_(None))",
        viewonly=True)
    jobs: Mapped[list["Job"]] = relationship(back_populates="document", cascade="all, delete-orphan",
                                             passive_deletes=True, order_by="Job.created_at")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float)
    tables: Mapped[list[Any]] = mapped_column(default=list)
    issues: Mapped[list[Any]] = mapped_column(default=list)

    document: Mapped[Document] = relationship(back_populates="pages")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("document_id", "index"),
        Index("ix_invoices_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_invoices_supplier_trgm", "supplier_name", postgresql_using="gin",
              postgresql_ops={"supplier_name": "gin_trgm_ops"}),
        Index("ix_invoices_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    document_type: Mapped[str] = mapped_column(String(20), default="invoice")

    invoice_number: Mapped[Optional[str]] = mapped_column(String(255))
    invoice_number_norm: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    invoice_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    supplier_name: Mapped[Optional[str]] = mapped_column(String(512))
    supplier_norm: Mapped[Optional[str]] = mapped_column(String(512), index=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(512))
    subtotal: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    total: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), index=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3))

    method: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    pages: Mapped[list[Any]] = mapped_column(default=list)
    source_label: Mapped[Optional[str]] = mapped_column(String(255))
    fields: Mapped[dict[str, Any]] = mapped_column(default=dict)  # ExtractedField per field (evidence...)
    # Shipping, discounts, fees... between subtotal and total (signed, with evidence)
    charges: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    extraction_notes: Mapped[list[Any]] = mapped_column(default=list)

    validation_status: Mapped[str] = mapped_column(String(20), index=True)  # valid|needs_review|invalid
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)

    # Human review (Task 4): pending -> approved | rejected
    review_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    corrected_fields: Mapped[list[Any]] = mapped_column(default=list)

    # Search (Task 5)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', coalesce(summary_text, ''))", persisted=True))
    embedding: Mapped[Optional[Any]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[Optional[str]] = mapped_column(String(64))

    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)

    document: Mapped[Document] = relationship(back_populates="invoices")
    line_items: Mapped[list["LineItemRow"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True,
        order_by="LineItemRow.position")
    issues: Mapped[list["ValidationIssueRow"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ValidationIssueRow.id")


class LineItemRow(Base):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[Optional[str]] = mapped_column(Text)
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    page: Mapped[Optional[int]] = mapped_column(Integer)
    evidence: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    issues: Mapped[list[Any]] = mapped_column(default=list)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")


class ValidationIssueRow(Base):
    __tablename__ = "validation_issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"),
                                                            index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(10), index=True)
    field: Mapped[Optional[str]] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)

    invoice: Mapped[Optional[Invoice]] = relationship(back_populates="issues")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        Index("ix_chunks_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_chunks_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True))
    embedding: Mapped[Optional[Any]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[Optional[str]] = mapped_column(String(64))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="process")
    # queued -> running -> succeeded | failed   (running -> queued again on retry)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # processing -> extracting -> validating -> indexing -> done
    stage: Mapped[Optional[str]] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[Optional[str]] = mapped_column(Text)
    worker_id: Mapped[Optional[str]] = mapped_column(String(128))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = _now_col()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="jobs")


EXTENSIONS_SQL = [text("CREATE EXTENSION IF NOT EXISTS vector"), text("CREATE EXTENSION IF NOT EXISTS pg_trgm")]

# Columns added after the first release. create_all() does not alter existing tables,
# so these bring an older database up to date (Alembic would do this in production).
UPGRADE_SQL = [
    text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS charges JSONB NOT NULL DEFAULT '[]'::jsonb"),
]
