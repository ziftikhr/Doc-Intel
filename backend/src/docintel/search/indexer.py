"""Build what search runs on: invoice summaries, text chunks and their embeddings."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from docintel.db.models import Chunk, Document, Invoice, Page

from .embeddings import Embedder, EmbeddingError

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200


def invoice_summary(inv: Invoice) -> str:
    """One sentence that describes the invoice; this is what gets embedded."""
    kind = {"credit_note": "Credit note", "receipt": "Receipt"}.get(inv.document_type, "Invoice")
    parts = [kind]
    if inv.invoice_number:
        parts.append(inv.invoice_number)
    if inv.supplier_name:
        parts.append(f"from {inv.supplier_name}")
    if inv.customer_name:
        parts.append(f"to {inv.customer_name}")
    if inv.invoice_date:
        parts.append(f"dated {inv.invoice_date.isoformat()}")
    text = " ".join(parts) + "."
    if inv.total is not None:
        text += f" Total {inv.total:.2f}{' ' + inv.currency if inv.currency else ''}."
    items = [li.description for li in inv.line_items if li.description]
    if items:
        text += " Items: " + "; ".join(items[:40]) + "."
    return text


@dataclass
class TextChunk:
    page_number: int
    text: str


def chunk_pages(pages: list[Page], size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[TextChunk]:
    """Split each page's text into overlapping windows, cut at line breaks where possible."""
    out: list[TextChunk] = []
    for page in pages:
        text = re.sub(r"[ \t]+", " ", page.text or "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                cut = text.rfind("\n", start + size // 2, end)
                if cut > start:
                    end = cut
            piece = text[start:end].strip()
            if piece:
                out.append(TextChunk(page.number, piece))
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
    return out


@dataclass
class IndexResult:
    status: str  # indexed | keyword_only | failed
    chunks: int
    invoices: int
    model: Optional[str]
    error: Optional[str] = None


def index_document(session: Session, document_id: uuid.UUID, embedder: Optional[Embedder]) -> IndexResult:
    """(Re)build chunks and embeddings for one document. Keyword search works even
    when embedding fails, so an embedding outage never loses a document."""
    doc = session.get(Document, document_id)
    pages = list(session.scalars(select(Page).where(Page.document_id == document_id).order_by(Page.number)))
    invoices = list(session.scalars(select(Invoice).where(Invoice.document_id == document_id)))

    session.execute(delete(Chunk).where(Chunk.document_id == document_id))
    chunks = [Chunk(document_id=document_id, chunk_index=i, page_number=c.page_number, text=c.text)
              for i, c in enumerate(chunk_pages(pages))]
    session.add_all(chunks)
    for inv in invoices:
        inv.summary_text = invoice_summary(inv)
        inv.embedding, inv.embedding_model = None, None

    status, error, model = "keyword_only", None, None
    if embedder is not None:
        texts = [inv.summary_text for inv in invoices] + [c.text for c in chunks]
        try:
            vectors = embedder.embed_documents(texts) if texts else []
            for obj, vec in zip([*invoices, *chunks], vectors):
                obj.embedding, obj.embedding_model = vec, embedder.model
            status, model = "indexed", embedder.model
        except EmbeddingError as e:
            log.warning("embedding failed for %s: %s", document_id, e)
            error = str(e)
    if not chunks and not invoices:
        status = "empty"  # nothing to search (e.g. a file that could not be read)
    if doc is not None:
        doc.search_status = status
    session.flush()
    return IndexResult(status, len(chunks), len(invoices), model, error)
