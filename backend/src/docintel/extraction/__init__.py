"""Task 2 - AI information extraction.

Turns a ``ProcessedDocument`` (Task 1) into structured invoices: invoice number,
date, supplier, customer, subtotal, tax, total, currency and line items - each
with the page and exact line it was read from, a confidence score, and issues.
Values that cannot be found in the source text are never invented.

Public entry point: ``extract_invoices(processed_document)``.
"""

from .config import ExtractionConfig
from .llm import InvoiceLLM, LLMCallResult, LLMError, OpenAIInvoiceLLM
from .pipeline import extract_invoices
from .schema import (
    ExtractedField,
    ExtractionResult,
    FieldIssue,
    FieldStatus,
    InvoiceRecord,
    LineItem,
    LLMExtraction,
    RawInvoice,
)

__all__ = [
    "ExtractedField",
    "ExtractionConfig",
    "ExtractionResult",
    "FieldIssue",
    "FieldStatus",
    "InvoiceLLM",
    "InvoiceRecord",
    "LLMCallResult",
    "LLMError",
    "LLMExtraction",
    "LineItem",
    "OpenAIInvoiceLLM",
    "RawInvoice",
    "extract_invoices",
]
