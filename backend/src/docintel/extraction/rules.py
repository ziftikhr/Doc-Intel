"""Rule-based (regex) invoice extractor.

Runs on every segment, with or without an LLM:

* without an API key it is the extractor (lower confidence);
* with an LLM it is an independent second opinion - agreement raises confidence,
  disagreement is flagged ``SOURCE_CONFLICT``.

It only reads values next to well-known labels ("Invoice No", "Total", "Bill To",
...), so it rarely finds a wrong value; it just finds fewer of them.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from .normalize import ISO_CURRENCIES, parse_currency, parse_date
from .schema import RawCharge, RawField, RawInvoice, RawLineItem, empty_raw_field

PageText = tuple[int, str]  # (page number, text)

_I = re.IGNORECASE

# ------------------------------------------------------------- labels ----------

INVOICE_NO_RE = re.compile(
    r"(?<![-\w])(?:tax\s+invoice|invoice|inv|bill|receipt|credit\s+note|rechnungs(?:nr|nummer)|rechnung|"
    r"facture|factura)\b"
    # [ \t] not \s: "...   INVOICE\n346 Station Road" must not read "346" as the number
    r"[ \t]*(?:(?:no|number|num|nr|nummer|n[°o]|id)\b\.?|#)?[ \t]*[:#.]{0,2}[ \t]*"
    r"(?P<v>(?=[A-Za-z0-9\-/_.]*\d)[A-Za-z0-9][A-Za-z0-9\-/_.]{0,30}[A-Za-z0-9])",
    _I,
)
_DATE_ONLY_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")

DATE_LABEL_RE = re.compile(
    r"\b(?P<label>invoice\s+date|date\s+of\s+issue|issue\s+date|date\s+issued|issued\s+on|"
    r"billing\s+date|rechnungsdatum|datum|date)\b\s*[:.]?\s*(?P<v>.*)", _I)
_OTHER_DATE_RE = re.compile(
    r"\b(?:due|delivery|order|ship\w*|service|period|payment|expiry|valid)\s+date\b|\bdate\s+due\b", _I)

CUSTOMER_RE = re.compile(
    r"\b(?:bill(?:ed)?\s+to|sold\s+to|invoice\s+to|customer(?:\s+name)?|client(?:\s+name)?|buyer|"
    r"kunde|rechnungsempf[äa]nger)\b(?!\s*(?:no|nr|number|id|#|code)\b)\s*[:]?\s*(?P<v>.*)", _I)
SUPPLIER_RE = re.compile(
    r"\b(?:from|supplier|vendor|seller|sold\s+by|issued\s+by|lieferant|verk[äa]ufer)\b"
    r"(?!\s*(?:no|nr|number|id|#|code)\b)\s*:\s*(?P<v>.*)", _I)

SUBTOTAL_RE = re.compile(
    r"\b(?:sub[\s-]?total|net\s+amount|net\s+total|total\s+net|total\s+excl\w*|amount\s+before\s+tax|"
    r"zwischensumme|nettobetrag|netto)\b", _I)
TAX_RE = re.compile(r"\b(?:sales\s+tax|tax|vat|gst|hst|mwst|ust|tva|iva)\b", _I)
TAX_NOT_AMOUNT_RE = re.compile(
    r"\b(?:tax|vat|gst|ust)[\s-]*(?:id|no|nr|number|reg\w*|code)\b|steuernummer|ust-?id|tax\s+rate", _I)
TOTAL_STRONG_RE = re.compile(
    r"\b(?:grand\s+total|total\s+due|amount\s+due|balance\s+due|total\s+amount|invoice\s+total|"
    r"total\s+payable|amount\s+payable|gesamtbetrag|rechnungsbetrag|montant\s+total)\b", _I)
TOTAL_RE = re.compile(r"\b(?:total|gesamt|summe|amount)\b", _I)
TOTAL_OF_TAX_RE = re.compile(r"\btotal\s+(?:tax|vat|gst|mwst)\b", _I)
CURRENCY_LABEL_RE = re.compile(r"\b(?:currency|w[äa]hrung|devise)\b\s*[:]?\s*(?P<v>\S.*)", _I)

INVOICE_KEYWORD_RE = re.compile(
    r"\b(?:invoice|receipt|credit\s+note|rechnung|facture|factura|bill)\b", _I)

# A number that may be an amount; "(10%" etc. are excluded by the caller.
_NUM_RE = re.compile(r"\(?[-−]?\d[\d.,' ]*\d\)?|\d")
_SYM_BEFORE_RE = re.compile(r"(?:US\$|[A-Z]{1,2}\$|[$€£¥₹₨৳]|(?<![A-Za-z])(?:Tk|TK|Rs)\.?)\s?$")
_SYM_AFTER_RE = re.compile(r"^\s?(?:[A-Z]{3}(?![A-Za-z])|[$€£¥₹₨৳])")

# Unit printed between quantity and unit price ("39  pcs  4.12  160.68").
_UNIT = (r"(?i:pcs?|pieces?|ea|each|units?|nos?|hrs?|hours?|h|days?|kg|g|m|m2|m²|l|ltr|box(?:es)?|"
         r"sets?|pairs?|rolls?|packs?|lots?|stk|stück|std|tage?)\.?")
# Optional row number and article code before the description:
# "1    IT-5526        Cat6 Patch Cable 2m     39  pcs     4.12     160.68"
ITEM_RE = re.compile(
    r"^\s*(?:\d{1,4}[.)]?\s+)?(?:[A-Z][A-Z0-9]*[-/.]?\d[\w\-/.]*\s{2,})?"
    # the description may start with a number ('27" IPS Monitor', '1TB SSD')
    r"(?P<desc>\d*[^\d\s].*?[^\s])\s+(?P<qty>\d[\d.,']*)\s+(?:" + _UNIT + r"\s+)?(?:x\s+)?"
    r"(?P<price>[$€£]?\s?\d[\d.,']*)\s+(?P<amt>[$€£]?\s?\d[\d.,']*(?:\s?[A-Z]{3})?)\s*$")
_ITEM_SKIP_RE = re.compile(
    r"\b(?:sub[\s-]?total|total|tax|vat|gst|amount\s+due|balance|page|invoice|date|phone|tel|fax|"
    r"iban|bic|swift|a/c|acct|account|bank|routing|sort\s+code)\b", _I)

# ------------------------------------------------- charges (shipping, discount...) ----------

CHARGE_KINDS: list[tuple[str, re.Pattern]] = [
    ("discount", re.compile(r"\b(?:discount|rebate|coupon|voucher|promo\w*|markdown|rabatt|skonto|nachlass|"
                            r"remise|descuento)\b|^\s*less\b", _I)),
    ("shipping", re.compile(r"\b(?:shipping|freight|delivery|postage|carriage|courier|versand\w*|fracht\w*|"
                            r"porto|livraison|env[ií]o)\b", _I)),
    ("rounding", re.compile(r"\b(?:round(?:ing|ed)?(?:\s+off)?|rundung|arrondi)\b", _I)),
    ("fee", re.compile(r"\b(?:fees?|surcharge|handling|service\s+charge|insurance|packaging|packing|levy|"
                       r"geb[üu]hr\w*|zuschlag|verpackung|frais)\b", _I)),
]
# Lines between subtotal and total that are not charges.
_NOT_CHARGE_RE = re.compile(
    r"\b(?:total|gesamt\w*|summe|amount\s+due|balance|paid|payment|deposit|advance|in\s+words)\b", _I)


def charge_kind(label: str) -> str:
    for kind, rx in CHARGE_KINDS:
        if rx.search(label):
            return kind
    return "other"


def is_charge_label(label: Optional[str]) -> bool:
    """True for "Shipping & Handling", "Discount (2%)"; false for tax, subtotal, total, payments."""
    if not label or sum(c.isalpha() for c in label) < 2:
        return False
    if SUBTOTAL_RE.search(label) or _NOT_CHARGE_RE.search(label) or TOTAL_STRONG_RE.search(label):
        return False
    if TAX_RE.search(label) and charge_kind(label) == "other":
        return False
    return True


def amounts_in(line: str) -> list[str]:
    """Amount-like substrings of a line, with an adjacent currency symbol/code."""
    out = []
    for m in _NUM_RE.finditer(line):
        after = line[m.end():m.end() + 2].lstrip()
        if after.startswith("%"):
            continue
        start, end = m.start(), m.end()
        before = _SYM_BEFORE_RE.search(line[max(0, start - 4):start])
        if before:
            start -= len(before.group(0))
            if start > 0 and line[start - 1] in "-−":
                start -= 1  # "-$24.71": the sign is printed before the currency symbol
        tail = _SYM_AFTER_RE.match(line[end:end + 5])
        if tail and (not tail.group(0).strip().isalpha() or tail.group(0).strip() in ISO_CURRENCIES):
            end += len(tail.group(0))
        out.append(line[start:end].strip())
    return out


def _cut(v: str) -> str:
    """Values printed in side-by-side columns: stop at a wide gap."""
    return re.split(r"\s{3,}|\t", v.strip(), maxsplit=1)[0].strip(" :;,|")


def _lines(pages: Iterable[PageText]) -> list[tuple[int, str]]:
    return [(n, line) for n, text in pages for line in text.splitlines()]


def _field(value: Optional[str], line: str, page: int) -> RawField:
    return RawField(value=value, evidence=line.strip(), page=page)


# ------------------------------------------------------- field finders ----------


def _cells(line: str) -> list[tuple[int, str]]:
    """Cells of a line printed in columns (separated by 2+ spaces) with their start positions."""
    return [(m.start(), m.group(0)) for m in re.finditer(r"\S+(?: \S+)*", line)]


_HEADER_LABELS = {
    "invoice_number": re.compile(
        r"^(?:(?:tax\s+)?invoice|inv|bill|rechnung)\s*(?:#|no\.?|number|nr\.?)\s*:?$|^rechnungsnr\.?\s*:?$", _I),
    "invoice_date": re.compile(
        r"^(?:invoice\s+date|date|issued(?:\s+on)?|issue\s+date|date\s+of\s+issue|datum|rechnungsdatum)\s*:?$", _I),
    "currency": re.compile(r"^(?:currency|w[äa]hrung)\s*:?$", _I),
}


def header_values(lines: list[tuple[int, str]]) -> dict[str, RawField]:
    """Labels printed as a row with the values on the line below, e.g.

        INVOICE #          ISSUED            CURRENCY:
        KC-2026-01727      18 Jan 2026       USD

    Each value is matched to the label whose column starts nearest to it."""
    out: dict[str, RawField] = {}
    for i, (_, line) in enumerate(lines[:-1]):
        head = _cells(line)
        if len(head) < 2 or any(re.search(r"\d", c) for _, c in head):
            continue
        wanted = {k: j for k, rx in _HEADER_LABELS.items() for j, (_, c) in enumerate(head) if rx.match(c)}
        if not wanted:
            continue
        nxt = next(((p, ln) for p, ln in lines[i + 1:i + 3] if ln.strip()), None)
        if nxt is None:
            continue
        by_column: dict[int, str] = {}
        for start, value in _cells(nxt[1]):
            j = min(range(len(head)), key=lambda j: abs(head[j][0] - start))
            by_column.setdefault(j, value)
        for key, j in wanted.items():
            if j in by_column and key not in out:
                out[key] = _field(by_column[j], nxt[1], nxt[0])
    return out


def find_invoice_numbers(text: str) -> list[str]:
    out = []
    for m in INVOICE_NO_RE.finditer(text):
        v = m.group("v").rstrip(".")
        if _DATE_ONLY_RE.match(v) or len(v) < 2:
            continue
        out.append(v)
    return out


def _invoice_number(lines) -> RawField:
    for page, line in lines:
        nums = find_invoice_numbers(line)
        if nums:
            return _field(nums[0], line, page)
    return empty_raw_field()


def _invoice_date(lines) -> RawField:
    best: Optional[tuple[int, RawField]] = None
    for page, line in lines:
        if _OTHER_DATE_RE.search(line):
            continue
        m = DATE_LABEL_RE.search(line)
        if not m:
            continue
        raw = _cut(m.group("v"))
        parsed = parse_date(raw)
        if parsed.value is None and not parsed.candidates:
            continue
        rank = 0 if m.group("label").lower().startswith(("invoice", "rechnungs")) else 1
        if best is None or rank < best[0]:
            best = (rank, _field(raw, line, page))
    return best[1] if best else empty_raw_field()


def _party(lines, regex) -> RawField:
    for i, (page, line) in enumerate(lines):
        m = regex.search(line)
        if not m:
            continue
        # "BILL TO            Invoice No: ED-1" - a wide gap without a colon means the
        # text on the right is another column, and the name is on the next line.
        gap = re.search(r"[\s:]*$", line[:m.start("v")]).group(0)
        v = "" if ":" not in gap and len(gap) >= 3 else _cut(m.group("v"))
        if v:
            return _field(v, line, page)
        for page2, nxt in lines[i + 1:i + 3]:  # label on its own line, name below
            if nxt.strip():
                return _field(_cut(nxt), nxt, page2)
    return empty_raw_field()


def _supplier_from_heading(lines) -> RawField:
    """Fallback: an invoice usually starts with the issuer's name."""
    for page, line in lines[:5]:
        s = _cut(line)  # "Ember Distribution Pvt. Ltd.          INVOICE" -> the left column
        if not s:
            continue
        letters = sum(c.isalpha() for c in s)
        if (letters >= 3 and len(s) <= 60 and letters / len(s) > 0.6
                and not INVOICE_KEYWORD_RE.search(s) and ":" not in s):
            return _field(_cut(s), line, page)
        return empty_raw_field()
    return empty_raw_field()


def _amount_after(lines, i: int) -> Optional[tuple[str, str, int]]:
    page, line = lines[i]
    found = amounts_in(line)
    if found:
        return found[-1], line, page
    for page2, nxt in lines[i + 1:i + 2]:  # value printed on the next line
        found = amounts_in(nxt)
        if found and len(nxt.strip()) <= 30:
            return found[-1], nxt, page2
    return None


def _amount_field(lines, kind: str) -> RawField:
    return _amount_hit(lines, kind)[0]


def _amount_hit(lines, kind: str) -> tuple[RawField, Optional[int]]:
    """The amount for ``kind`` and the index of its label line."""
    best: Optional[tuple[int, int, RawField]] = None
    for i, (_page, line) in enumerate(lines):
        is_sub = bool(SUBTOTAL_RE.search(line))
        if kind == "subtotal":
            ok, rank = is_sub, 0
        elif kind == "total":
            strong = bool(TOTAL_STRONG_RE.search(line))
            ok = (strong or bool(TOTAL_RE.search(line))) and not is_sub and not TOTAL_OF_TAX_RE.search(line)
            rank = 0 if strong else 1
        else:  # tax
            ok = (bool(TAX_RE.search(line)) and not is_sub and not TOTAL_STRONG_RE.search(line)
                  and not re.search(r"\btotal\b(?!\s+(?:tax|vat|gst))", line, _I)
                  and not TAX_NOT_AMOUNT_RE.search(line))
            rank = 0
        if not ok:
            continue
        hit = _amount_after(lines, i)
        if not hit:
            continue
        value, ev_line, ev_page = hit
        # same rank: the later line wins (totals are printed at the bottom)
        if best is None or rank <= best[0]:
            best = (rank, i, _field(value, ev_line, ev_page))
    return (best[2], best[1]) if best else (empty_raw_field(), None)


def _charges(lines, after: Optional[int], before: Optional[int]) -> list[RawCharge]:
    """Every labelled amount printed between the subtotal and the total line, other
    than tax: shipping, discounts, fees. Unknown labels are kept too (kind "other")."""
    if after is None or before is None or before <= after:
        return []
    out = []
    for page, line in lines[after + 1:before]:
        s = line.strip()
        found = amounts_in(s)
        if not found:
            continue
        raw = found[-1]
        head = s[:s.rfind(raw)]
        if head.rstrip().endswith(("-", "−")) and not raw.startswith(("-", "−", "(")):
            raw = "-" + raw  # "Discount (2%)   -$1,067.35"
        label = re.sub(r"\s+", " ", head).strip(" :-−\t")
        if not is_charge_label(label):
            continue
        out.append(RawCharge(label=label, amount=raw, evidence=s, page=page))
    return out


def _currency(lines, total: RawField, subtotal: RawField, text: str,
              column: Optional[RawField] = None) -> RawField:
    for page, line in lines:
        m = CURRENCY_LABEL_RE.search(line)
        if m and parse_currency(m.group("v")).code:
            return _field(_cut(m.group("v")), line, page)
    if column and column.value and parse_currency(column.value).code:
        return column
    for f in (total, subtotal):
        if f.value and parse_currency(f.value).code:
            return RawField(value=parse_currency(f.value).raw, evidence=f.evidence, page=f.page)
    codes = {c for c in re.findall(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])", text) if c in ISO_CURRENCIES}
    if len(codes) == 1:
        code = codes.pop()
        for page, line in lines:
            if re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", line):
                return _field(code, line, page)
    return empty_raw_field()


_QTY_HEADER_RE = re.compile(r"\b(?:qty|quantity|menge|anzahl)\b", _I)
_PRICE_HEADER_RE = re.compile(r"\b(?:unit\s+price|unit\s+cost|price|rate|einzelpreis|preis)\b", _I)
_TRAILING_CODE_RE = re.compile(r"\s{2,}[A-Z][A-Z0-9]*[-/]?\d[\w\-/]*$")


def _line_items(lines) -> list[RawLineItem]:
    items = []
    price_first = False  # "Description  Code  Rate  Qty  Line Total"
    for page, line in lines:
        q, p = _QTY_HEADER_RE.search(line), _PRICE_HEADER_RE.search(line)
        if q and p and not re.search(r"\d", line):
            price_first = p.start() < q.start()
            continue
        m = ITEM_RE.match(line)
        if not m or _ITEM_SKIP_RE.search(m.group("desc")):
            continue
        desc = _TRAILING_CODE_RE.sub("", m.group("desc")).strip()  # article code printed after the description
        if sum(c.isalpha() for c in desc) < 2:
            continue
        qty, price = m.group("qty"), m.group("price").strip()
        if price_first:
            qty, price = price, qty
        items.append(RawLineItem(
            description=re.sub(r"\s{2,}", " ", desc), quantity=qty, unit_price=price,
            amount=m.group("amt").strip(), evidence=line.strip(), page=page))
    return items


# ---------------------------------------------------------------- entry ----------


def extract_rules(pages: list[PageText]) -> Optional[RawInvoice]:
    """Extract one invoice from a segment, or ``None`` if it does not look like one."""
    lines = _lines(pages)
    text = "\n".join(t for _, t in pages)
    columns = header_values(lines)
    inv_no = _invoice_number(lines)
    hdr_no = columns.get("invoice_number")
    if hdr_no and hdr_no.value and re.search(r"\d", hdr_no.value):
        inv_no = hdr_no  # an explicit "INVOICE #" column beats a number found in running text
    total, total_at = _amount_hit(lines, "total")
    subtotal, subtotal_at = _amount_hit(lines, "subtotal")
    date = _invoice_date(lines)
    hdr_date = columns.get("invoice_date")
    if not date.value and hdr_date and (parse_date(hdr_date.value).value or parse_date(hdr_date.value).candidates):
        date = hdr_date
    has_amount = bool(total.value or subtotal.value)
    # Need more than one weak signal: a guide *about* invoices mentions numbers and
    # dates, a real invoice also prints amounts against them.
    if not INVOICE_KEYWORD_RE.search(text) or not has_amount or not (inv_no.value or date.value):
        return None
    supplier = _party(lines, SUPPLIER_RE)
    if not supplier.value:
        supplier = _supplier_from_heading(lines)
    lowered = text.lower()
    if "credit note" in lowered:
        doc_type = "credit_note"
    elif "receipt" in lowered and "invoice" not in lowered:
        doc_type = "receipt"
    else:
        doc_type = "invoice"
    return RawInvoice(
        document_type=doc_type,
        invoice_number=inv_no,
        invoice_date=date,
        supplier_name=supplier,
        customer_name=_party(lines, CUSTOMER_RE),
        subtotal=subtotal,
        tax=_amount_field(lines, "tax"),
        total=total,
        currency=_currency(lines, total, subtotal, text, columns.get("currency")),
        line_items=_line_items(lines),
        other_charges=_charges(lines, subtotal_at, total_at),
    )
