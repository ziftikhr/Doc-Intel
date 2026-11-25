"""Individual validation checks.

Each check takes one ``InvoiceRecord`` and returns a list of ``ValidationIssue``
(empty = passed) or ``None`` when it does not apply (e.g. no line items to add up),
which is reported as "skipped" rather than silently counted as a pass.

All arithmetic is done with ``Decimal`` on the exact values Task 2 extracted. The
checks never fill in or correct a value - they only say what is wrong and why.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from docintel.extraction.normalize import ISO_CURRENCIES, fmt_decimal
from docintel.extraction.schema import (
    HEADER_FIELDS,
    ExtractedField,
    FieldIssue,
    FieldStatus,
    InvoiceRecord,
)

from .config import ValidationConfig
from .models import ValidationIssue

LABELS = {
    "invoice_number": "invoice number", "invoice_date": "invoice date",
    "supplier_name": "supplier", "customer_name": "customer", "subtotal": "subtotal",
    "tax": "tax", "total": "total", "currency": "currency",
}


@dataclass
class Context:
    config: ValidationConfig
    today: date


Check = Callable[[InvoiceRecord, Context], Optional[list[ValidationIssue]]]


# ------------------------------------------------------------------ helpers ----------


def to_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def amount(f: ExtractedField) -> Optional[Decimal]:
    """The field's value as a Decimal, only if Task 2 trusted it (status found)."""
    return to_decimal(f.value) if f.status == FieldStatus.FOUND else None


def as_date(f: ExtractedField) -> Optional[date]:
    if f.status != FieldStatus.FOUND or not f.value:
        return None
    try:
        return date.fromisoformat(f.value[:10])
    except ValueError:
        return None


def money(d: Optional[Decimal]) -> str:
    return fmt_decimal(d) if d is not None else "-"


def issue(code: str, severity: str, message: str, field: Optional[str] = None, **details) -> ValidationIssue:
    clean = {k: (money(v) if isinstance(v, Decimal) else v) for k, v in details.items() if v is not None}
    return ValidationIssue(code=code, severity=severity, message=message, field=field, details=clean)


_LEGAL_SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "gmbh", "ag", "co", "corp", "corporation",
    "company", "plc", "sa", "sarl", "srl", "bv", "nv", "pvt", "private", "pty", "the", "kg", "ug",
}


def norm_party(name: Optional[str]) -> Optional[str]:
    """'ACME Supplies Ltd.' and 'Acme Supplies Limited' -> 'acmesupplies'."""
    if not name:
        return None
    s = unicodedata.normalize("NFKC", name).casefold()
    words = [w for w in re.findall(r"[^\W_]+", s) if w not in _LEGAL_SUFFIXES]
    return "".join(words) or None


def norm_invoice_number(number: Optional[str]) -> Optional[str]:
    """'INV-0042', 'inv 42' and 'INV/00042' -> 'inv42'.

    Leading zeros are dropped per printed group *before* the separators are removed,
    so 'INV-2024-0042' -> 'inv202442' (not 'inv20240042')."""
    if not number:
        return None
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", number).casefold())
    s = "".join(re.sub(r"(?<!\d)0+(?=\d)", "", t) for t in tokens)
    return s or None


# ------------------------------------------------------------------- checks ----------


def check_required_fields(inv: InvoiceRecord, ctx: Context):
    """Required fields must be present and readable; recommended ones should be."""
    required = ctx.config.required_for(inv.document_type)
    recommended = ctx.config.recommended_for(inv.document_type)
    out = []
    for name in (*required, *recommended):
        f = inv.field(name)
        is_required = name in required
        label = LABELS[name]
        if f.status == FieldStatus.MISSING:
            if is_required:
                kind = {"invoice": "an invoice", "credit_note": "a credit note",
                        "receipt": "a receipt"}.get(inv.document_type, "this document")
                out.append(issue("MISSING_REQUIRED_FIELD", "error",
                                 f"The {label} is missing; it is required for {kind}.", name))
            else:
                # A register row normally has no customer (it is the company's own export)
                # and often no currency column (house currency) - worth noting, not reviewing.
                severity = "info" if inv.method == "spreadsheet" else "warning"
                out.append(issue("MISSING_FIELD", severity, f"No {label} was found on the document.", name))
        elif f.status == FieldStatus.UNCERTAIN:
            out.append(_unreadable(name, f, severity="error" if is_required else "warning"))
    return out


def _unreadable(name: str, f: ExtractedField, severity: str) -> ValidationIssue:
    label = LABELS[name]
    reasons = [i.value for i in f.issues]
    if FieldIssue.AMBIGUOUS_DATE in f.issues:
        # Not broken - one of two known readings is right, a person just has to pick it.
        return issue("AMBIGUOUS_DATE", "warning",
                     f"The {label} '{f.raw}' can be read as {' or '.join(f.candidates)}; "
                     "confirm the day/month order.", name, raw=f.raw, candidates=f.candidates)
    if FieldIssue.UNGROUNDED in f.issues:
        msg = f"The {label} reported by the extractor could not be found in the document text."
    else:
        msg = f"The {label} was found but could not be read as a valid value: '{f.raw}'."
    return issue("UNREADABLE_FIELD", severity, msg, name, raw=f.raw, reasons=reasons, page=f.page)


def check_field_quality(inv: InvoiceRecord, ctx: Context):
    """Carry Task 2's doubts forward: unreadable optional fields, conflicts, low confidence."""
    covered = set(ctx.config.required_for(inv.document_type)) | set(ctx.config.recommended_for(inv.document_type))
    out = []
    for name in HEADER_FIELDS:
        f = inv.field(name)
        label = LABELS[name]
        if f.status == FieldStatus.UNCERTAIN and name not in covered:
            out.append(_unreadable(name, f, severity="warning"))
            continue
        if f.status != FieldStatus.FOUND:
            continue
        if FieldIssue.SOURCE_CONFLICT in f.issues:
            out.append(issue("SOURCE_CONFLICT", "warning",
                             f"The AI and the text rules read different {label} values "
                             f"({', '.join(f.candidates) or f.value}); '{f.value}' was kept.",
                             name, value=f.value, candidates=f.candidates))
        elif f.confidence < ctx.config.review_confidence:
            out.append(issue("LOW_CONFIDENCE", "warning",
                             f"The {label} '{f.value}' was extracted with low confidence ({f.confidence:.2f}).",
                             name, value=f.value, confidence=f.confidence))
        if FieldIssue.LOW_OCR_CONFIDENCE in f.issues:
            out.append(issue("LOW_OCR_QUALITY", "warning",
                             f"The {label} comes from a poorly scanned page (page {f.page}); "
                             "check it against the original.", name, page=f.page))
    # Non-field notes from extraction, e.g. "rules-only extraction (LLM_NO_CREDIT)".
    for note in _extraction_notes(inv):
        out.append(issue("EXTRACTION_NOTE", "warning", f"Extraction: {note.rstrip('.')}."))
    return out


def _extraction_notes(inv: InvoiceRecord) -> list[str]:
    if inv.extraction_notes:
        return inv.extraction_notes
    # Results saved before extraction_notes existed: the notes are the review reasons
    # that are neither "field: ISSUE" entries nor messages written by validation ("[error] ...").
    return [r for r in inv.review_reasons
            if not r.startswith("[") and r.split(":", 1)[0] not in HEADER_FIELDS]


def check_amounts(inv: InvoiceRecord, ctx: Context):
    """Amounts must be plausible numbers: not negative (unless a credit note), not zero, not huge."""
    values = {n: amount(inv.field(n)) for n in ("subtotal", "tax", "total")}
    if all(v is None for v in values.values()):
        return None
    out = []
    for name, v in values.items():
        if v is None:
            continue
        if v < 0 and inv.document_type != "credit_note":
            out.append(issue("NEGATIVE_AMOUNT", "error",
                             f"The {LABELS[name]} is negative ({money(v)}) on a document that is not a credit note.",
                             name, value=v))
        if abs(v) > ctx.config.max_amount:
            out.append(issue("AMOUNT_TOO_LARGE", "warning",
                             f"The {LABELS[name]} {money(v)} is unusually large; it may be a misread.",
                             name, value=v, limit=ctx.config.max_amount))
    total, tax = values["total"], values["tax"]
    if total is not None and total == 0:
        out.append(issue("ZERO_TOTAL", "warning", "The total is 0.00.", "total"))
    if total is not None and tax is not None and total > 0 and tax > total:
        out.append(issue("TAX_EXCEEDS_TOTAL", "error",
                         f"The tax ({money(tax)}) is larger than the total ({money(total)}).",
                         "tax", tax=tax, total=total))
    return out


def check_dates(inv: InvoiceRecord, ctx: Context):
    d = as_date(inv.invoice_date)
    if d is None:
        return None
    out = []
    cfg = ctx.config
    if d.year < cfg.min_year:
        out.append(issue("IMPLAUSIBLE_DATE", "error",
                         f"The invoice date {d.isoformat()} is before {cfg.min_year}; it is probably a misread.",
                         "invoice_date", value=d.isoformat()))
    elif d > ctx.today + timedelta(days=cfg.future_tolerance_days):
        out.append(issue("DATE_IN_FUTURE", "warning",
                         f"The invoice date {d.isoformat()} is in the future.",
                         "invoice_date", value=d.isoformat(), today=ctx.today.isoformat()))
    elif d < ctx.today - timedelta(days=cfg.max_age_days):
        out.append(issue("OLD_INVOICE", "warning",
                         f"The invoice date {d.isoformat()} is more than {cfg.max_age_days // 365} years ago.",
                         "invoice_date", value=d.isoformat()))
    return out


def check_currency(inv: InvoiceRecord, ctx: Context):
    f = inv.currency
    if f.status != FieldStatus.FOUND or not f.value:
        return None
    out = []
    if f.value.upper() not in ISO_CURRENCIES:
        out.append(issue("INVALID_CURRENCY", "error",
                         f"'{f.value}' is not a recognised currency code.", "currency", value=f.value))
    if FieldIssue.CURRENCY_INFERRED in f.issues:
        out.append(issue("CURRENCY_INFERRED", "warning",
                         f"The currency {f.value} was inferred from the symbol '{f.raw}', which several "
                         "currencies share; confirm it.", "currency", value=f.value, raw=f.raw))
    if FieldIssue.CURRENCY_CONFLICT in f.issues:
        out.append(issue("CURRENCY_CONFLICT", "warning",
                         f"The document mentions more than one currency; {f.value} was kept.",
                         "currency", value=f.value, candidates=f.candidates))
    return out


def charges_of(inv: InvoiceRecord) -> tuple[Decimal, list[tuple[str, Decimal]], list[str]]:
    """Sum of the other charges (shipping, discounts...), each (label, signed amount),
    and the labels of charges whose amount could not be read."""
    parts, unreadable = [], []
    for c in inv.charges:
        v = to_decimal(c.amount)
        label = c.label or c.kind
        if v is None:
            unreadable.append(label)
        else:
            parts.append((label, v))
    return sum((v for _, v in parts), Decimal(0)), parts, unreadable


def _formula(sub: Decimal, parts: list[tuple[str, Decimal]], tax: Optional[Decimal]) -> str:
    """'Subtotal 100.00 + Shipping 15.00 - Discount (2%) 2.00 + tax 8.00'."""
    s = f"Subtotal {money(sub)}"
    for label, v in parts:
        s += f" {'-' if v < 0 else '+'} {label} {money(abs(v))}"
    if tax is not None:
        s += f" + tax {money(tax)}"
    return s


def check_totals(inv: InvoiceRecord, ctx: Context):
    """subtotal + other charges (shipping, discounts...) + tax must equal total."""
    sub, tax, total = amount(inv.subtotal), amount(inv.tax), amount(inv.total)
    if sub is None or total is None:
        return None  # nothing to compare against
    tol = ctx.config.amount_tolerance
    charges, parts, unreadable = charges_of(inv)
    out = []
    if unreadable:
        out.append(issue("CHARGE_UNREADABLE", "warning",
                         f"The amount of '{', '.join(unreadable)}' could not be read, so it is left out of "
                         "the total check.", "charges", labels=unreadable))
    named = [{"label": label, "amount": money(v)} for label, v in parts]
    if tax is None:
        expected = sub + charges
        if abs(expected - total) <= tol:
            return out
        if parts and abs(sub - total) <= tol:
            return out  # the charges are already part of the subtotal
        return [*out, issue("TOTAL_UNVERIFIED", "warning",
                            f"{_formula(sub, parts, None)} = {money(expected)}, but the total says {money(total)} "
                            f"(difference {money(total - expected)}), and no tax amount was found to explain it.",
                            "total", subtotal=sub, charges=named or None, total=total, difference=total - expected)]
    expected = sub + charges + tax
    diff = total - expected
    if abs(diff) <= tol:
        return out
    if parts and abs(sub + tax - total) <= tol:
        return out  # discount/shipping printed above the subtotal and already included in it
    if abs(sub + charges - total) <= tol and tax != 0:
        return [*out, issue("TAX_POSSIBLY_INCLUDED", "warning",
                            f"The total ({money(total)}) does not add the tax of {money(tax)} on top; "
                            "the amounts are probably tax-inclusive. Confirm.",
                            "total", subtotal=sub, tax=tax, total=total)]
    hint = ("One of the amounts may be misread." if parts else
            "One of the amounts may be misread, or a discount/shipping line was not captured.")
    return [*out, issue("TOTAL_MISMATCH", "error",
                        f"{_formula(sub, parts, tax)} = {money(expected)}, but the total says {money(total)} "
                        f"(difference {money(diff)}). {hint}",
                        "total", subtotal=sub, charges=named or None, tax=tax, expected_total=expected, total=total,
                        difference=diff)]


def check_tax_rate(inv: InvoiceRecord, ctx: Context):
    sub, tax = amount(inv.subtotal), amount(inv.tax)
    if sub is None or tax is None or sub == 0:
        return None
    rate = tax / sub
    if rate < 0 or rate > ctx.config.max_tax_rate:
        pct = (rate * 100).quantize(Decimal("0.1"))
        return [issue("UNUSUAL_TAX_RATE", "warning",
                      f"The tax is {pct}% of the subtotal, which is outside the usual "
                      f"0-{(ctx.config.max_tax_rate * 100).normalize()}% range.",
                      "tax", tax=tax, subtotal=sub, rate_percent=str(pct))]
    return []


def check_line_items(inv: InvoiceRecord, ctx: Context):
    """Each line: quantity x unit price = amount. All lines: sum = subtotal."""
    items = inv.line_items
    if not items:
        return None
    tol = ctx.config.amount_tolerance
    out = []
    amounts: list[Optional[Decimal]] = []
    for i, li in enumerate(items):
        where = f"line_items[{i}]"
        name = li.description or f"line {i + 1}"
        qty, unit, amt = to_decimal(li.quantity), to_decimal(li.unit_price), to_decimal(li.amount)
        bad = [k for k in ("quantity", "unit_price", "amount") if getattr(li, k) and to_decimal(getattr(li, k)) is None]
        if FieldIssue.INVALID_NUMBER in li.issues or bad:
            out.append(issue("LINE_ITEM_UNREADABLE", "warning",
                             f"Line '{name}' has a number that could not be read: '{li.evidence}'.",
                             where, page=li.page, evidence=li.evidence))
        amounts.append(amt)
        if qty is not None and unit is not None and amt is not None:
            expected = qty * unit
            # unit prices are often printed rounded, so allow half a cent per unit
            line_tol = max(tol, abs(qty) * Decimal("0.005"))
            if abs(expected - amt) > line_tol:
                q = format(qty.normalize(), "f")  # "50", not "5E+1"
                out.append(issue("LINE_ITEM_MISMATCH", "warning",
                                 f"Line '{name}': {q} x {money(unit)} = {money(expected)}, "
                                 f"but the line amount is {money(amt)}.",
                                 f"{where}.amount", quantity=q, unit_price=unit,
                                 expected=expected, amount=amt))

    if any(a is None for a in amounts):
        out.append(issue("LINE_ITEMS_INCOMPLETE", "info",
                         "Some line items have no amount, so their sum could not be checked.", "line_items"))
        return out
    items_sum = sum(amounts, Decimal(0))
    sub, tax, total = amount(inv.subtotal), amount(inv.tax), amount(inv.total)
    charges, parts, _ = charges_of(inv)
    extra = " and other charges" if parts else ""
    if sub is not None:
        target, target_name = sub, "subtotal"
    elif total is not None and tax is not None:
        target, target_name = total - tax - charges, f"total minus tax{extra}"
    elif total is not None:
        target, target_name = total - charges, "total minus other charges" if parts else "total"
    else:
        return out
    # each printed line amount may itself be rounded by up to half a cent
    sum_tol = max(tol, Decimal("0.005") * len(amounts))
    if abs(items_sum - target) <= sum_tol:
        return out
    if parts and sub is not None and abs(items_sum + charges - sub) <= sum_tol:
        return out  # e.g. a discount printed above the subtotal line
    if total is not None and abs(items_sum - total) <= sum_tol:
        out.append(issue("LINE_ITEMS_INCLUDE_TAX", "info",
                         "The line items add up to the total rather than the subtotal (tax-inclusive prices).",
                         "line_items", items_sum=items_sum, total=total))
        return out
    out.append(issue("LINE_ITEMS_SUM_MISMATCH", "warning",
                     f"The {len(amounts)} line items add up to {money(items_sum)}, but the {target_name} is "
                     f"{money(target)} (difference {money(target - items_sum)}). A line may be missing or misread.",
                     "line_items", items_sum=items_sum, expected=target, difference=target - items_sum,
                     compared_with=target_name))
    return out


def check_parties(inv: InvoiceRecord, ctx: Context):
    out = []
    supplier, customer = inv.supplier_name, inv.customer_name
    checked = False
    if supplier.status == FieldStatus.FOUND and customer.status == FieldStatus.FOUND:
        checked = True
        if norm_party(supplier.value) and norm_party(supplier.value) == norm_party(customer.value):
            out.append(issue("SUPPLIER_EQUALS_CUSTOMER", "warning",
                             f"The supplier and the customer are the same ('{supplier.value}'); "
                             "one of them is probably wrong.", "customer_name"))
    number = inv.invoice_number
    if number.status == FieldStatus.FOUND and number.value:
        checked = True
        v = number.value
        if len(v) > 40 or not re.search(r"[0-9A-Za-z]", v) or not re.search(r"\d", v):
            out.append(issue("SUSPICIOUS_INVOICE_NUMBER", "warning",
                             f"'{v}' does not look like an invoice number.", "invoice_number", value=v))
    return out if checked else None


CHECKS: list[tuple[str, Check]] = [
    ("required_fields", check_required_fields),
    ("field_quality", check_field_quality),
    ("amounts", check_amounts),
    ("dates", check_dates),
    ("currency", check_currency),
    ("totals", check_totals),
    ("tax_rate", check_tax_rate),
    ("line_items", check_line_items),
    ("parties", check_parties),
]
