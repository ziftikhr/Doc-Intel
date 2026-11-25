"""Hybrid search over invoices and document text.

Two retrievers run for every query and their rankings are merged with Reciprocal
Rank Fusion (score = sum over lists of 1 / (60 + rank)):

* semantic - pgvector cosine distance between the query embedding and stored
  embeddings (finds "MacBook" for "laptop");
* keyword  - PostgreSQL full-text search, exact (normalised) invoice-number match and
  trigram similarity on supplier names (finds "INV-2024-0042", tolerates typos).

Filters (supplier, dates, amounts, status...) apply to both. If the query cannot be
embedded (no key, outage) the search degrades to keyword-only and says so.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, PlainSerializer
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from docintel.db.models import Chunk, Document, Invoice
from docintel.extraction.normalize import fmt_decimal
from docintel.validation.checks import norm_invoice_number

from .embeddings import Embedder, EmbeddingError

log = logging.getLogger(__name__)

RRF_K = 60
Money = Annotated[Decimal, PlainSerializer(fmt_decimal, return_type=str, when_used="json")]
CANDIDATES = 50


class SearchFilters(BaseModel):
    supplier: Optional[str] = Field(None, description="Supplier name contains (case-insensitive)")
    customer: Optional[str] = None
    currency: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    min_total: Optional[Decimal] = None
    max_total: Optional[Decimal] = None
    validation_status: Optional[Literal["valid", "needs_review", "invalid"]] = None
    review_status: Optional[Literal["pending", "approved", "rejected"]] = None
    needs_review: Optional[bool] = None
    document_id: Optional[uuid.UUID] = None

    def invoice_conditions(self) -> list:
        c = []
        if self.supplier:
            c.append(Invoice.supplier_name.ilike(f"%{_escape_like(self.supplier)}%", escape="\\"))
        if self.customer:
            c.append(Invoice.customer_name.ilike(f"%{_escape_like(self.customer)}%", escape="\\"))
        if self.currency:
            c.append(Invoice.currency == self.currency.upper())
        if self.date_from:
            c.append(Invoice.invoice_date >= self.date_from)
        if self.date_to:
            c.append(Invoice.invoice_date <= self.date_to)
        if self.min_total is not None:
            c.append(Invoice.total >= self.min_total)
        if self.max_total is not None:
            c.append(Invoice.total <= self.max_total)
        if self.validation_status:
            c.append(Invoice.validation_status == self.validation_status)
        if self.review_status:
            c.append(Invoice.review_status == self.review_status)
        if self.needs_review is not None:
            c.append(Invoice.needs_review == self.needs_review)
        if self.document_id:
            c.append(Invoice.document_id == self.document_id)
        return c

    def has_invoice_filters(self) -> bool:
        return any(v is not None for k, v in self.model_dump().items() if k != "document_id")


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class InvoiceHit(BaseModel):
    invoice_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    invoice_number: Optional[str]
    supplier_name: Optional[str]
    customer_name: Optional[str]
    invoice_date: Optional[date]
    total: Optional[Money]
    currency: Optional[str]
    validation_status: str
    review_status: str
    summary: str
    score: float
    similarity: Optional[float] = Field(None, description="Cosine similarity of the semantic match")
    matched_by: list[str]


class PassageHit(BaseModel):
    chunk_id: int
    document_id: uuid.UUID
    filename: str
    page_number: int
    snippet: str
    score: float
    similarity: Optional[float] = None
    matched_by: list[str]


class SearchResponse(BaseModel):
    query: str
    mode: Literal["hybrid", "keyword"]
    embedding_model: Optional[str] = None
    note: Optional[str] = None
    invoices: list[InvoiceHit] = Field(default_factory=list)
    passages: list[PassageHit] = Field(default_factory=list)


def _semantic_keep(rows, threshold) -> list[tuple]:
    """(id, similarity) pairs that pass the absolute floor and the relative margin."""
    floor, margin = threshold
    sims = [(i, 1 - d) for i, d in rows]
    if not sims:
        return []
    best = max(s for _, s in sims)
    cut = max(floor, best - margin) if margin is not None else floor
    return [(i, s) for i, s in sims if s >= cut]


def _rrf(rankings: dict[str, list]) -> dict:
    scores: dict = {}
    for ids in rankings.values():
        for rank, key in enumerate(ids):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def search(session: Session, query: str, embedder: Optional[Embedder], filters: Optional[SearchFilters] = None,
           scope: Literal["all", "invoices", "passages"] = "all", limit: int = 10,
           min_similarity: Optional[float] = None) -> SearchResponse:
    query = query.strip()
    filters = filters or SearchFilters()
    qvec, note = None, None
    if embedder is not None and query:
        try:
            qvec = embedder.embed_query(query)
        except EmbeddingError as e:
            note = f"semantic search unavailable ({e}); showing keyword matches only"
    elif embedder is None:
        note = "no embedding model configured; showing keyword matches only"
    floor = min_similarity if min_similarity is not None else (embedder.min_similarity if embedder else 0)
    margin = getattr(embedder, "relative_margin", None) if min_similarity is None else None
    threshold = (floor, margin)

    # Let HNSW keep scanning when filters remove candidates (pgvector >= 0.8).
    session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

    resp = SearchResponse(query=query, mode="hybrid" if qvec is not None else "keyword",
                          embedding_model=embedder.model if qvec is not None else None, note=note)
    if scope in ("all", "invoices"):
        resp.invoices = _search_invoices(session, query, qvec, embedder, filters, threshold, limit)
    if scope in ("all", "passages"):
        resp.passages = _search_passages(session, query, qvec, embedder, filters, threshold, limit)
    return resp


# ---------------------------------------------------------------- invoices ----------


def _search_invoices(session, query, qvec, embedder, filters, threshold, limit) -> list[InvoiceHit]:
    conds = filters.invoice_conditions()
    rankings: dict[str, list] = {}
    similarity: dict = {}

    if qvec is not None:
        dist = Invoice.embedding.cosine_distance(qvec)
        rows = session.execute(
            select(Invoice.id, dist.label("d")).where(Invoice.embedding_model == embedder.model, *conds)
            .order_by(dist).limit(CANDIDATES)).all()
        keep = _semantic_keep(rows, threshold)
        rankings["semantic"] = [i for i, _ in keep]
        similarity.update(keep)

    keyword: list = []
    number = norm_invoice_number(query)
    if number and re.search(r"\d", query):
        keyword += session.scalars(select(Invoice.id).where(Invoice.invoice_number_norm == number, *conds)).all()
    tsq = func.websearch_to_tsquery("simple", query)
    rank = func.ts_rank_cd(Invoice.search_tsv, tsq)
    keyword += session.scalars(select(Invoice.id).where(Invoice.search_tsv.op("@@")(tsq), *conds)
                               .order_by(rank.desc()).limit(CANDIDATES)).all()
    if len(query) >= 3:
        sim = func.similarity(Invoice.supplier_name, query)
        keyword += session.scalars(select(Invoice.id).where(Invoice.supplier_name.op("%")(query), *conds)
                                   .order_by(sim.desc()).limit(CANDIDATES)).all()
    rankings["keyword"] = list(dict.fromkeys(keyword))

    scores = _rrf(rankings)
    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    if not top:
        return []
    rows = {inv.id: (inv, fn) for inv, fn in session.execute(
        select(Invoice, Document.filename).join(Document).where(Invoice.id.in_(top))).all()}
    hits = []
    for iid in top:
        inv, filename = rows[iid]
        hits.append(InvoiceHit(
            invoice_id=inv.id, document_id=inv.document_id, filename=filename,
            invoice_number=inv.invoice_number, supplier_name=inv.supplier_name, customer_name=inv.customer_name,
            invoice_date=inv.invoice_date, total=inv.total, currency=inv.currency,
            validation_status=inv.validation_status, review_status=inv.review_status, summary=inv.summary_text,
            score=round(scores[iid], 5),
            similarity=round(similarity[iid], 4) if iid in similarity else None,
            matched_by=[name for name, ids in rankings.items() if iid in ids]))
    return hits


# ---------------------------------------------------------------- passages ----------


def _chunk_scope(filters: SearchFilters) -> list:
    conds = []
    if filters.document_id:
        conds.append(Chunk.document_id == filters.document_id)
    if filters.has_invoice_filters():
        docs: Select = select(Invoice.document_id).where(*filters.invoice_conditions())
        conds.append(Chunk.document_id.in_(docs))
    return conds


def _search_passages(session, query, qvec, embedder, filters, threshold, limit) -> list[PassageHit]:
    conds = _chunk_scope(filters)
    rankings: dict[str, list] = {}
    similarity: dict = {}
    if qvec is not None:
        dist = Chunk.embedding.cosine_distance(qvec)
        rows = session.execute(select(Chunk.id, dist.label("d"))
                               .where(Chunk.embedding_model == embedder.model, *conds)
                               .order_by(dist).limit(CANDIDATES)).all()
        keep = _semantic_keep(rows, threshold)
        rankings["semantic"] = [i for i, _ in keep]
        similarity.update(keep)
    tsq = func.websearch_to_tsquery("simple", query)
    rank = func.ts_rank_cd(Chunk.search_tsv, tsq)
    rankings["keyword"] = list(session.scalars(
        select(Chunk.id).where(Chunk.search_tsv.op("@@")(tsq), *conds).order_by(rank.desc()).limit(CANDIDATES)))

    scores = _rrf(rankings)
    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    if not top:
        return []
    rows = {c.id: (c, fn) for c, fn in session.execute(
        select(Chunk, Document.filename).join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(top))).all()}
    return [PassageHit(chunk_id=cid, document_id=rows[cid][0].document_id, filename=rows[cid][1],
                       page_number=rows[cid][0].page_number, snippet=snippet(rows[cid][0].text, query),
                       score=round(scores[cid], 5),
                       similarity=round(similarity[cid], 4) if cid in similarity else None,
                       matched_by=[n for n, ids in rankings.items() if cid in ids])
            for cid in top]


def snippet(text_: str, query: str, width: int = 280) -> str:
    """The part of the chunk around the first query word, or its start."""
    flat = re.sub(r"\s+", " ", text_).strip()
    lower = flat.lower()
    pos = -1
    for word in sorted(re.findall(r"\w{3,}", query.lower()), key=len, reverse=True):
        pos = lower.find(word)
        if pos >= 0:
            break
    if pos < 0 or len(flat) <= width:
        return flat[:width] + ("..." if len(flat) > width else "")
    start = max(0, pos - width // 3)
    end = min(len(flat), start + width)
    return ("..." if start else "") + flat[start:end] + ("..." if end < len(flat) else "")
