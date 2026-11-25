"""Deterministic extraction from spreadsheets (XLSX/XLS/CSV).

Spreadsheet data is already structured, so it is mapped by column header instead
of being sent to an LLM: cheaper, instant and exact. One row of an invoice
register becomes one invoice; a line-item sheet is joined to it by invoice number.

Sheets whose headers cannot be mapped (e.g. an invoice *form* laid out in cells)
are left to the text path (rules + LLM) by the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..processing.models import PageResult, Table
from .normalize import compact, detect_decimal_separator

# Strong synonyms are matched first; weak ones only fill fields still unmapped.
HEADER_SYNONYMS: dict[str, tuple[list[str], list[str]]] = {
    "invoice_number": (["invoice no", "invoice number", "invoice #", "invoice nr", "invoice id", "inv no",
                        "bill no", "bill number", "receipt no", "document no", "rechnungsnr",
                        "rechnungsnummer", "rechnung nr", "belegnr", "facture no", "invoice"],
                       ["rechnung", "number", "no", "doc no", "reference"]),
    "invoice_date": (["invoice date", "issue date", "document date", "bill date", "rechnungsdatum",
                      "date of issue"], ["date", "datum"]),
    "supplier_name": (["supplier", "supplier name", "vendor", "vendor name", "seller", "lieferant",
                       "issuer"], ["from", "company"]),
    "customer_name": (["customer", "customer name", "client", "client name", "buyer", "bill to",
                       "sold to", "kunde"], ["to"]),
    "subtotal": (["subtotal", "sub total", "net amount", "net total", "nettobetrag", "amount excl tax",
                  "amount before tax"], ["net", "netto"]),
    "tax": (["tax", "tax amount", "vat", "vat amount", "gst", "sales tax", "mwst", "ust",
             "steuer"], []),
    "total": (["total", "total amount", "invoice total", "grand total", "amount due", "gross amount",
               "bruttobetrag", "gesamtbetrag", "gesamt"], ["gross", "brutto", "amount", "betrag", "sum"]),
    "currency": (["currency", "curr", "ccy", "wahrung", "waehrung", "währung", "devise"], []),
}
ITEM_SYNONYMS: dict[str, tuple[list[str], list[str]]] = {
    "description": (["description", "item", "item description", "product", "service", "beschreibung",
                     "bezeichnung", "artikel", "details"], []),
    "quantity": (["qty", "quantity", "menge", "anzahl", "units"], []),
    "unit_price": (["unit price", "price", "unit cost", "rate", "einzelpreis", "preis"], []),
    "amount": (["amount", "line total", "line amount", "total", "betrag", "gesamt", "net amount"], []),
}


def _key(header: Any) -> str:
    s = str(header or "").casefold().replace("_", " ").replace(".", " ")
    s = re.sub(r"[^\w#äöüß ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def map_columns(header: list[Any], synonyms: dict[str, tuple[list[str], list[str]]]) -> dict[str, int]:
    keys = [_key(h) for h in header]
    mapping: dict[str, int] = {}
    for tier in (0, 1):
        for fname, tiers in synonyms.items():
            if fname in mapping:
                continue
            for syn in tiers[tier]:
                hits = [i for i, k in enumerate(keys) if k == syn and i not in mapping.values()]
                if hits:
                    mapping[fname] = hits[0]
                    break
    return mapping


@dataclass
class Cell:
    raw: Any
    evidence: str
    page: int


@dataclass
class SheetInvoice:
    page: int
    label: str
    cells: dict[str, Cell] = field(default_factory=dict)
    line_items: list[dict[str, Cell]] = field(default_factory=list)
    decimal_hints: dict[str, Optional[str]] = field(default_factory=dict)


def _row_text(row: list[Any]) -> str:
    return " | ".join("" if c is None else str(c) for c in row)


def _column_hint(table: Table, col: int) -> Optional[str]:
    strings = [r[col] for r in table.rows if col < len(r) and isinstance(r[col], str)]
    return detect_decimal_separator("\n".join(strings)) if strings else None


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def extract_spreadsheet(pages: list[PageResult]) -> list[SheetInvoice]:
    headers: list[tuple[PageResult, Table, dict[str, int]]] = []
    items: list[tuple[PageResult, Table, dict[str, int], Optional[int]]] = []
    for page in pages:
        for table in page.tables:
            if not table.header or not table.rows:
                continue
            item_map = map_columns(table.header, ITEM_SYNONYMS)
            head_map = map_columns(table.header, HEADER_SYNONYMS)
            if "description" in item_map and len(item_map) >= 2:
                items.append((page, table, item_map, head_map.get("invoice_number")))
            elif ("invoice_number" in head_map and len(head_map) >= 2) or (
                    "total" in head_map and len(head_map) >= 3):
                headers.append((page, table, head_map))

    invoices: list[SheetInvoice] = []
    for page, table, cmap in headers:
        hints = {f: _column_hint(table, i) for f, i in cmap.items()}
        for r, row in enumerate(table.rows, 1):
            values = {f: row[i] if i < len(row) else None for f, i in cmap.items()}
            if all(_is_blank(v) for v in values.values()):
                continue
            ev = _row_text(row)
            inv = SheetInvoice(page=page.number, label=f"{table.name} data row {r}", decimal_hints=hints)
            inv.cells = {f: Cell(v, ev, page.number) for f, v in values.items() if not _is_blank(v)}
            invoices.append(inv)

    by_number = {}
    for inv in invoices:
        num = inv.cells.get("invoice_number")
        if num is not None:
            by_number.setdefault(compact(str(num.raw)), inv)
    for page, table, imap, join_col in items:
        for row in table.rows:
            target = None
            if join_col is not None and join_col < len(row) and not _is_blank(row[join_col]):
                target = by_number.get(compact(str(row[join_col])))
            elif len(invoices) == 1:
                target = invoices[0]
            if target is None:
                continue
            ev = _row_text(row)
            item = {f: Cell(row[i], ev, page.number) for f, i in imap.items()
                    if i < len(row) and not _is_blank(row[i])}
            if item:
                target.line_items.append(item)
    return invoices
