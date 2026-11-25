"""Task 3 - validation.

Checks each extracted invoice for missing required fields, implausible numbers and
dates, arithmetic that does not add up (subtotal + tax = total, line items = subtotal,
quantity x unit price = amount), and duplicates across documents. Every finding has a
code, a severity and a plain-English message; the result says whether the invoice is
``valid``, ``needs_review`` or ``invalid``.

Entry points: ``validate_document(extraction_result)`` for one document,
``validate_batch(results)`` to also catch duplicates between documents.
"""

from .config import ValidationConfig
from .duplicates import DuplicateIndex, InvoiceRef
from .models import (
    DocumentValidation,
    InvoiceValidation,
    ValidatedDocument,
    ValidationIssue,
)
from .validator import apply_validation, validate_batch, validate_document, validate_invoice

__all__ = [
    "DocumentValidation",
    "DuplicateIndex",
    "InvoiceRef",
    "InvoiceValidation",
    "ValidatedDocument",
    "ValidationConfig",
    "ValidationIssue",
    "apply_validation",
    "validate_batch",
    "validate_document",
    "validate_invoice",
]
