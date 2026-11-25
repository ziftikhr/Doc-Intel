"""Task 5 - semantic (pgvector) + keyword search."""

from .embeddings import Embedder, EmbeddingError, GeminiEmbedder, HashingEmbedder, make_embedder
from .indexer import chunk_pages, index_document, invoice_summary
from .query import InvoiceHit, PassageHit, SearchFilters, SearchResponse, search

__all__ = [
    "Embedder", "EmbeddingError", "GeminiEmbedder", "HashingEmbedder", "InvoiceHit", "PassageHit",
    "SearchFilters", "SearchResponse", "chunk_pages", "index_document", "invoice_summary",
    "make_embedder", "search",
]
