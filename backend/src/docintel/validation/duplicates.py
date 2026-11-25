"""Duplicate-invoice detection across documents.

``DuplicateIndex`` remembers every invoice it has seen (in this batch, plus any
``known`` invoices handed in - e.g. loaded from the database in Task 5) and reports,
for each new invoice, whether it was seen before:

* DUPLICATE_INVOICE (error)          same supplier + invoice number, same amount/date:
                                     the same invoice submitted twice - do not pay twice.
* DUPLICATE_NUMBER_CONFLICT (error)  same supplier + invoice number but a different
                                     total or date: a misread, an altered copy, or fraud.
* POSSIBLE_DUPLICATE (warning)       matches on weaker evidence (number + total with a
                                     supplier missing, or supplier + date + total with
                                     no invoice number).

Numbers and names are compared loosely ("INV-0042" = "inv 42", "ACME Ltd" = "Acme
Limited"). Different suppliers may legitimately reuse the same invoice number, so a
number match alone with two *different* known suppliers is not a duplicate.
The first occurrence is never flagged; only the later ones are.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from docintel.extraction.schema import FieldStatus, InvoiceRecord

from .checks import amount, issue, money, norm_invoice_number, norm_party
from .models import ValidationIssue


@dataclass(frozen=True)
class InvoiceRef:
    """The few facts needed to recognise an invoice again."""

    source: str  # unique per occurrence, e.g. file path or database row id
    document_id: str
    filename: str
    index: int
    invoice_number: Optional[str]
    supplier: Optional[str]
    invoice_date: Optional[str]
    total: Optional[Decimal]
    currency: Optional[str]

    @classmethod
    def from_record(cls, rec: InvoiceRecord, *, source: str, document_id: str, filename: str) -> "InvoiceRef":
        def val(f):
            return f.value if f.status == FieldStatus.FOUND else None

        return cls(source=source, document_id=document_id, filename=filename, index=rec.index,
                   invoice_number=val(rec.invoice_number), supplier=val(rec.supplier_name),
                   invoice_date=val(rec.invoice_date), total=amount(rec.total), currency=val(rec.currency))

    @property
    def label(self) -> str:
        return f"{self.filename} (invoice {self.index + 1})"


def _same(a, b) -> Optional[bool]:
    """True/False when both are known, None when either is missing."""
    if a is None or b is None:
        return None
    return a == b


class DuplicateIndex:
    def __init__(self, known: Iterable[InvoiceRef] = ()):
        self._refs: list[InvoiceRef] = []
        for ref in known:
            self.add(ref)

    def __len__(self) -> int:
        return len(self._refs)

    def add(self, ref: InvoiceRef) -> None:
        self._refs.append(ref)

    def check(self, ref: InvoiceRef) -> list[ValidationIssue]:
        """Compare ``ref`` with everything seen so far (does not add it)."""
        number = norm_invoice_number(ref.invoice_number)
        supplier = norm_party(ref.supplier)
        strongest: Optional[ValidationIssue] = None
        rank = {"error": 2, "warning": 1}

        for prev in self._refs:
            if prev.source == ref.source and prev.index == ref.index:
                continue  # the very same occurrence (re-validation)
            found = self._compare(ref, prev, number, supplier)
            if found and (strongest is None or rank[found.severity] > rank[strongest.severity]):
                strongest = found
                if found.code == "DUPLICATE_INVOICE":
                    break
        return [strongest] if strongest else []

    def check_and_add(self, ref: InvoiceRef) -> list[ValidationIssue]:
        found = self.check(ref)
        self.add(ref)
        return found

    @staticmethod
    def _compare(ref: InvoiceRef, prev: InvoiceRef, number, supplier) -> Optional[ValidationIssue]:
        same_number = _same(number, norm_invoice_number(prev.invoice_number))
        same_supplier = _same(supplier, norm_party(prev.supplier))
        same_total = _same(ref.total, prev.total)
        same_date = _same(ref.invoice_date, prev.invoice_date)
        same_currency = _same(ref.currency, prev.currency)
        details = dict(duplicate_of=prev.label, duplicate_of_document_id=prev.document_id,
                       duplicate_of_index=prev.index, invoice_number=ref.invoice_number)

        if same_number and same_supplier:
            differences = []
            if same_total is False:
                differences.append(f"total {money(ref.total)} vs {money(prev.total)}")
            if same_date is False:
                differences.append(f"date {ref.invoice_date} vs {prev.invoice_date}")
            if same_currency is False:
                differences.append(f"currency {ref.currency} vs {prev.currency}")
            if not differences:
                return issue("DUPLICATE_INVOICE", "error",
                             f"Invoice {ref.invoice_number} from {ref.supplier} was already received in "
                             f"{prev.label}; it looks like the same invoice submitted twice.",
                             "invoice_number", **details)
            return issue("DUPLICATE_NUMBER_CONFLICT", "error",
                         f"Invoice number {ref.invoice_number} from {ref.supplier} was already received in "
                         f"{prev.label} with different details ({'; '.join(differences)}).",
                         "invoice_number", **details, differences=differences)

        if same_number and same_supplier is None and same_total:
            return issue("POSSIBLE_DUPLICATE", "warning",
                         f"Invoice {ref.invoice_number} with the same total ({money(ref.total)}) was already "
                         f"received in {prev.label}; the supplier is missing on one of them, so confirm.",
                         "invoice_number", **details)

        if (number is None or prev.invoice_number is None) and same_supplier and same_date and same_total:
            return issue("POSSIBLE_DUPLICATE", "warning",
                         f"An invoice from {ref.supplier} dated {ref.invoice_date} for {money(ref.total)} was "
                         f"already received in {prev.label} (no invoice number to compare).",
                         None, **details)
        return None
