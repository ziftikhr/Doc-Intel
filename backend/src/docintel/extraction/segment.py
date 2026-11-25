"""Split a long document into per-invoice segments before extraction.

A 1,000-page PDF is usually hundreds of invoices, not one. Sending each invoice
separately keeps LLM prompts small (cheap, fast, accurate) and lets segments be
extracted in parallel.

Boundaries are found from the printed invoice number: a page whose invoice
number differs from the current segment's starts a new segment; pages without
one (continuation pages, terms & conditions) stay with the current invoice.
Segments are also capped in pages/characters so no single prompt grows unbounded;
when nothing looks like an invoice number the document is cut into fixed windows
and the LLM decides how many invoices each window holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..processing.models import PageResult
from .normalize import compact
from .rules import find_invoice_numbers, header_values


@dataclass
class Segment:
    index: int
    pages: list[PageResult] = field(default_factory=list)
    invoice_hint: Optional[str] = None
    chars: int = 0

    @property
    def page_numbers(self) -> list[int]:
        return [p.number for p in self.pages]


def _first_invoice_number(text: str) -> Optional[str]:
    # Only the top of the page: references to *other* invoices further down
    # ("credit for invoice INV-9") must not split the document.
    head = text.splitlines()[:15]
    for line in head:
        nums = find_invoice_numbers(line)
        if nums:
            return compact(nums[0])
    # "INVOICE #   ISSUED ..." with the values on the next line
    col = header_values([(0, line) for line in head]).get("invoice_number")
    if col and col.value and any(ch.isdigit() for ch in col.value):
        return compact(col.value)
    return None


# "Page 1 / 3", "Page 1 of 2", "Seite 1 / 1", "Sheet 1 / 2": the first page of an invoice
_FIRST_PAGE_RE = re.compile(r"\b(?:page|seite|sheet|blatt|pg\.?)\s*1\s*(?:/|of|von|de)\s*\d+\b", re.IGNORECASE)


def segment_pages(pages: list[PageResult], max_pages: int = 8, max_chars: int = 40_000) -> list[Segment]:
    segments: list[Segment] = []
    current: Optional[Segment] = None
    for page in pages:
        if not page.text.strip():
            continue
        hint = _first_invoice_number(page.text)
        start_new = (
            current is None
            or (hint is not None and current.invoice_hint is not None and hint != current.invoice_hint)
            # a page marked "Page 1 / n" starts an invoice even when the number repeats
            # (a re-issued invoice with the same number is a separate document)
            or bool(_FIRST_PAGE_RE.search(page.text))
            or len(current.pages) >= max_pages
            or current.chars + len(page.text) > max_chars
        )
        if start_new:
            current = Segment(index=len(segments), invoice_hint=hint)
            segments.append(current)
        elif current.invoice_hint is None and hint is not None:
            current.invoice_hint = hint
        current.pages.append(page)
        current.chars += len(page.text)
    return segments
