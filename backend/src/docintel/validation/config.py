"""Validation settings. Every value can be overridden with a DOCINTEL_VALIDATION_* env var
(e.g. ``DOCINTEL_VALIDATION_AMOUNT_TOLERANCE=0.05``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from decimal import Decimal

# Fields an invoice cannot be processed without, per document type.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "invoice": ("invoice_number", "invoice_date", "supplier_name", "total"),
    "credit_note": ("invoice_number", "invoice_date", "supplier_name", "total"),
    # Till receipts often have no number and no customer.
    "receipt": ("invoice_date", "supplier_name", "total"),
    "other": ("total",),
}
# Fields that should be there; missing ones are a warning, not an error.
RECOMMENDED_FIELDS: dict[str, tuple[str, ...]] = {
    "invoice": ("customer_name", "currency"),
    "credit_note": ("customer_name", "currency"),
    "receipt": ("currency",),
    "other": (),
}


@dataclass(frozen=True)
class ValidationConfig:
    # Largest difference still accepted as rounding when comparing amounts.
    amount_tolerance: Decimal = Decimal("0.01")
    # tax / subtotal above this is unusual (highest standard VAT rates are ~27%).
    max_tax_rate: Decimal = Decimal("0.30")
    # Amounts above this are probably two numbers merged by OCR.
    max_amount: Decimal = Decimal("100000000")
    # Invoice dates more than this many days in the future are suspicious
    # (1 day tolerates time-zone differences).
    future_tolerance_days: int = 1
    # Invoices older than this (in days) are flagged as unusually old.
    max_age_days: int = 5 * 365
    # Dates before this year are treated as misreads.
    min_year: int = 1990
    # Fields Task 2 scored below this confidence need a human look.
    review_confidence: float = 0.6

    def required_for(self, document_type: str) -> tuple[str, ...]:
        return REQUIRED_FIELDS.get(document_type, REQUIRED_FIELDS["invoice"])

    def recommended_for(self, document_type: str) -> tuple[str, ...]:
        return RECOMMENDED_FIELDS.get(document_type, RECOMMENDED_FIELDS["invoice"])

    @classmethod
    def from_env(cls, **overrides) -> "ValidationConfig":
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f"DOCINTEL_VALIDATION_{f.name.upper()}")
            if raw is None:
                continue
            kind = f.type if isinstance(f.type, str) else f.type.__name__
            values[f.name] = {"int": int, "float": float, "Decimal": Decimal}.get(kind, str)(raw.strip())
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def with_overrides(self, **overrides) -> "ValidationConfig":
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})
