"""Grounding: every extracted value must be traceable to the source text.

An extractor (especially an LLM) returns a value plus the verbatim line it read it
from. ``SourceText.ground`` accepts the value only if

1. the evidence line really occurs in the document text, and
2. the value really occurs in that evidence (numbers/dates compared by meaning,
   so "1,234.50" in the evidence grounds a value of "1234.50"),

and otherwise falls back to finding the value itself in the text. Anything that
cannot be found is rejected as ``UNGROUNDED`` - this is what stops invented values.
The page number is taken from where the text was actually found, never from the
extractor's claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..processing.models import ExtractionMethod, PageResult
from .normalize import compact, norm_text, parse_amount, parse_date
from .rules import amounts_in


@dataclass
class SourcePage:
    number: int
    text: str
    ocr_confidence: Optional[float] = None
    lines: list[str] = field(default_factory=list)
    norm: str = ""
    compact: str = ""
    norm_lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lines = [ln for ln in self.text.splitlines() if ln.strip()]
        self.norm = norm_text(self.text)
        self.compact = compact(self.text)
        self.norm_lines = [norm_text(ln) for ln in self.lines]


@dataclass
class Grounded:
    raw: str
    evidence: str
    page: int
    ocr_confidence: Optional[float]


class SourceText:
    def __init__(self, pages: list[SourcePage]):
        self.pages = pages
        self.by_number = {p.number: p for p in pages}

    @classmethod
    def from_pages(cls, pages: list[PageResult]) -> "SourceText":
        out = []
        for p in pages:
            conf = None
            if p.method in (ExtractionMethod.OCR, ExtractionMethod.HYBRID) and p.ocr:
                conf = p.ocr.confidence
            out.append(SourcePage(p.number, p.text, conf))
        return cls(out)

    @property
    def page_texts(self) -> list[tuple[int, str]]:
        return [(p.number, p.text) for p in self.pages]

    def locate(self, snippet: Optional[str], prefer_page: Optional[int] = None) -> Optional[tuple[SourcePage, str]]:
        """Find ``snippet`` in the text. Returns (page, the line containing it)."""
        if not snippet or not snippet.strip():
            return None
        n, c = norm_text(snippet), compact(snippet)
        order = sorted(self.pages, key=lambda p: p.number != prefer_page)
        for page in order:
            if n in page.norm:
                for line, nl in zip(page.lines, page.norm_lines):
                    if n in nl:
                        return page, line.strip()
                return page, snippet.strip()  # spans several lines
        if len(c) >= 4:  # OCR may have dropped/added spaces
            for page in order:
                if c in page.compact:
                    for line in page.lines:
                        if c in compact(line):
                            return page, line.strip()
                    return page, snippet.strip()
        return None

    def ground(self, value: Optional[str], evidence: Optional[str], kind: str = "text",
               prefer_page: Optional[int] = None) -> Optional[Grounded]:
        """Return where ``value`` is printed, or ``None`` if it cannot be found."""
        if value is None or not str(value).strip():
            return None
        value = str(value).strip()
        if evidence:
            loc = self.locate(evidence, prefer_page)
            if loc and value_in(value, loc[1], kind):
                page, line = loc
                return Grounded(value, line, page.number, page.ocr_confidence)
        loc = self.locate(value, prefer_page)
        if loc and _distinctive(value, kind):
            page, line = loc
            return Grounded(value, line, page.number, page.ocr_confidence)
        if kind == "amount":  # e.g. value "1234.50" printed as "1,234.50"
            target = parse_amount(value).value
            if target is not None:
                for page in sorted(self.pages, key=lambda p: p.number != prefer_page):
                    for line in page.lines:
                        if any(parse_amount(a).value == target for a in amounts_in(line)) and abs(target) >= 10:
                            return Grounded(value, line.strip(), page.number, page.ocr_confidence)
        return None


def _distinctive(value: str, kind: str) -> bool:
    """Short values like "10" occur everywhere; only accept them with real evidence."""
    c = compact(value)
    if kind == "amount":
        return len(c) >= 4 or any(ch in value for ch in ".,")
    return len(c) >= 3


def value_in(value: str, evidence: str, kind: str = "text") -> bool:
    if compact(value) in compact(evidence):
        return True
    if kind == "amount":
        target = parse_amount(value).value
        return target is not None and any(parse_amount(a).value == target for a in amounts_in(evidence))
    if kind == "date":
        a, b = parse_date(value), parse_date(evidence)
        return a.value is not None and a.value == b.value
    return False
