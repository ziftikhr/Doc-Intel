"""Task 2 pipeline: ProcessedDocument (Task 1) -> ExtractionResult.

    spreadsheet ─► column mapping ───────────────────────────────┐
    PDF / image ─► segment ─► rules ─┐                          ├─► parse ─► confidence ─► InvoiceRecord
                              └─► LLM ─┴─► ground (drop invented) ─┘

Guarantees:
* every value in the output is printed in the source document (grounded) - the
  page and the exact line are reported as evidence;
* nothing is computed or guessed: a missing total stays missing, an ambiguous
  date like 03/04/2024 is reported with both readings instead of picking one;
* extraction never raises on bad input; LLM outages degrade to rules-only
  extraction and are reported in ``issues``.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..processing.models import DocumentStatus, ProcessedDocument
from .config import ExtractionConfig
from .grounding import SourceText, value_in
from .llm import InvoiceLLM, LLMCallResult, LLMError, make_llm
from .normalize import (
    clean_str,
    compact,
    detect_decimal_separator,
    fmt_decimal,
    parse_amount,
    parse_currency,
    parse_date,
)
from .rules import SUPPLIER_RE, amounts_in, charge_kind, extract_rules, is_charge_label
from .schema import (
    AMOUNT_FIELDS,
    HEADER_FIELDS,
    Charge,
    ExtractedField,
    ExtractionResult,
    FieldIssue,
    FieldStatus,
    InvoiceRecord,
    LineItem,
    LLMUsage,
    RawInvoice,
)
from .segment import Segment, segment_pages
from .spreadsheet import SheetInvoice, extract_spreadsheet

# Base confidence by where a value came from (before OCR quality is applied).
CONF_AGREE = 0.97  # LLM and rules found the same value
CONF_SPREADSHEET = 0.97
CONF_LLM = 0.88
CONF_RULES_ONLY = 0.78  # no LLM available
CONF_RULES_LLM_MISSED = 0.62  # LLM ran but did not report it
CONF_CONFLICT = 0.55

_AUTO = object()


def _kind(name: str) -> str:
    if name in AMOUNT_FIELDS:
        return "amount"
    if name == "invoice_date":
        return "date"
    if name == "currency":
        return "currency"
    return "text"


@dataclass
class Cand:
    raw: Any
    evidence: Optional[str]
    page: Optional[int]
    ocr_conf: Optional[float]
    source: str  # llm | rules | spreadsheet
    weak: bool = False  # heuristic guess (e.g. supplier = first line of the page)


@dataclass
class Parsed:
    value: Optional[str]
    issues: list[FieldIssue]
    candidates: list[str]
    certain: bool = True


def parse_value(name: str, raw: Any, hint: Optional[str]) -> Parsed:
    kind = _kind(name)
    if kind == "amount":
        p = parse_amount(raw, hint)
        return Parsed(fmt_decimal(p.value), [FieldIssue(i) for i in p.issues], [])
    if kind == "date":
        p = parse_date(raw)
        return Parsed(p.value.isoformat() if p.value else None, [FieldIssue(i) for i in p.issues],
                      [d.isoformat() for d in p.candidates])
    if kind == "currency":
        g = parse_currency(str(raw))
        if g.code is None:
            return Parsed(None, [FieldIssue.NOT_FOUND], [])
        return Parsed(g.code, [] if g.certain else [FieldIssue.CURRENCY_INFERRED], [], g.certain)
    return Parsed(clean_str(raw), [], [])


def _same(name: str, a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    if _kind(name) == "text":
        strip = lambda s: "".join(ch for ch in compact(s) if ch.isalnum())  # noqa: E731
        return strip(a) == strip(b)
    return a == b


def _ocr_adjust(conf: float, cand: Cand, cfg: ExtractionConfig, issues: list[FieldIssue]) -> float:
    if cand.ocr_conf is None:
        return conf
    if cand.ocr_conf < cfg.low_ocr_confidence:
        issues.append(FieldIssue.LOW_OCR_CONFIDENCE)
    return conf * (0.5 + 0.5 * cand.ocr_conf)


def build_field(name: str, llm: Optional[Cand], rules: Optional[Cand], *, llm_ok: bool,
                rejected_raw: Optional[str], hint: Optional[str], cfg: ExtractionConfig,
                sheet: Optional[Cand] = None) -> ExtractedField:
    issues: list[FieldIssue] = []
    candidates: list[str] = []
    pl = parse_value(name, llm.raw, hint) if llm else None
    pr = parse_value(name, rules.raw, hint) if rules else None

    chosen: Optional[Cand] = None
    parsed: Optional[Parsed] = None
    source = None
    if sheet is not None:
        chosen, parsed, source, conf = sheet, parse_value(name, sheet.raw, hint), "spreadsheet", CONF_SPREADSHEET
    elif llm and rules and pl.value is not None and pr.value is not None:
        if _same(name, pl.value, pr.value):
            chosen, parsed, source, conf = llm, pl, "llm+rules", CONF_AGREE
        else:
            chosen, parsed, source, conf = llm, pl, "llm", CONF_CONFLICT
            issues.append(FieldIssue.SOURCE_CONFLICT)
            candidates = [pl.value, pr.value]
    elif llm and (pl.value is not None or not rules or pr.value is None):
        chosen, parsed, source, conf = llm, pl, "llm", CONF_LLM
    elif rules:
        chosen, parsed, source = rules, pr, "rules"
        conf = CONF_RULES_LLM_MISSED if llm_ok else CONF_RULES_ONLY
        if rules.weak:
            conf *= 0.8
    else:
        if rejected_raw:
            return ExtractedField(raw=rejected_raw, status=FieldStatus.UNCERTAIN,
                                  issues=[FieldIssue.UNGROUNDED])
        return ExtractedField(status=FieldStatus.MISSING, issues=[FieldIssue.NOT_FOUND])

    issues.extend(i for i in parsed.issues if i not in issues)
    if not candidates:
        candidates = parsed.candidates
    if parsed.value is None:
        # Something is printed but it is not a valid number/date: keep it as raw, never guess.
        return ExtractedField(raw=str(chosen.raw), status=FieldStatus.UNCERTAIN, source=source,
                              page=chosen.page, evidence=chosen.evidence, candidates=candidates,
                              issues=issues or [FieldIssue.INVALID_NUMBER], confidence=0.0)
    if not parsed.certain:
        conf *= 0.6
    conf = round(_ocr_adjust(conf, chosen, cfg, issues), 3)
    if conf < cfg.review_confidence and FieldIssue.LOW_CONFIDENCE not in issues:
        issues.append(FieldIssue.LOW_CONFIDENCE)
    return ExtractedField(value=parsed.value, raw=str(chosen.raw), status=FieldStatus.FOUND,
                          confidence=conf, source=source, page=chosen.page, evidence=chosen.evidence,
                          candidates=candidates, issues=issues)


def _infer_currency(rec: InvoiceRecord, cfg: ExtractionConfig) -> None:
    """Fill / cross-check currency from symbols printed next to the amounts."""
    seen: dict[str, tuple[bool, ExtractedField]] = {}
    for name in ("total", "subtotal", "tax"):
        f = rec.field(name)
        if f.raw:
            g = parse_currency(f.raw)
            if g.code:
                seen.setdefault(g.code, (g.certain, f))
    cur = rec.currency
    if cur.status == FieldStatus.FOUND:
        if seen and cur.value not in seen and all(certain for certain, _ in seen.values()):
            cur.issues.append(FieldIssue.CURRENCY_CONFLICT)
            cur.candidates = sorted({cur.value, *seen})
            cur.confidence = min(cur.confidence, 0.4)
        return
    if len(seen) == 1:
        code, (certain, src) = next(iter(seen.items()))
        conf = (0.85 if certain else 0.45) * (src.confidence / CONF_AGREE if src.confidence else 1)
        issues = [] if certain else [FieldIssue.CURRENCY_INFERRED]
        if conf < cfg.review_confidence:
            issues.append(FieldIssue.LOW_CONFIDENCE)
        rec.currency = ExtractedField(value=code, raw=src.raw, status=FieldStatus.FOUND,
                                      confidence=round(min(conf, 0.95), 3), source=src.source,
                                      page=src.page, evidence=src.evidence, issues=issues)


def _finish(rec: InvoiceRecord, cfg: ExtractionConfig, extra_reasons: list[str]) -> InvoiceRecord:
    found = [rec.field(n) for n in HEADER_FIELDS if rec.field(n).status == FieldStatus.FOUND]
    rec.confidence = round(sum(f.confidence for f in found) / len(found), 3) if found else 0.0
    reasons = list(extra_reasons)
    for n in HEADER_FIELDS:
        f = rec.field(n)
        flagged = [i.value for i in f.issues if i not in (FieldIssue.NOT_FOUND,)]
        if f.status == FieldStatus.UNCERTAIN or (f.status == FieldStatus.FOUND and (
                f.confidence < cfg.review_confidence or FieldIssue.SOURCE_CONFLICT in f.issues
                or FieldIssue.CURRENCY_CONFLICT in f.issues)):
            reasons.append(f"{n}: {', '.join(flagged) or f.status.value}")
    pages = {f.page for f in found if f.page} | {li.page for li in rec.line_items if li.page}
    rec.pages = sorted(pages | set(rec.pages))
    rec.review_reasons = reasons
    rec.extraction_notes = list(extra_reasons)
    rec.needs_review = bool(reasons)
    return rec


# ------------------------------------------------------------- text path ----------


def _ground_raw(raw: Optional[RawInvoice], src: SourceText, source: str):
    """Ground every field of an extractor's claim. Returns ({field: Cand}, {field: rejected raw})."""
    cands: dict[str, Cand] = {}
    rejected: dict[str, str] = {}
    if raw is None:
        return cands, rejected
    for name in HEADER_FIELDS:
        rf = getattr(raw, name)
        if rf.value is None or not str(rf.value).strip():
            continue
        g = src.ground(rf.value, rf.evidence, _kind(name), prefer_page=rf.page)
        if g:
            weak = source == "rules" and name == "supplier_name" and not SUPPLIER_RE.search(g.evidence)
            cands[name] = Cand(g.raw, g.evidence, g.page, g.ocr_confidence, source, weak)
        else:
            rejected[name] = rf.value
    return cands, rejected


def _line_items(raw: Optional[RawInvoice], src: SourceText, hint: Optional[str], source: str,
                cfg: ExtractionConfig) -> tuple[list[LineItem], int]:
    items, dropped = [], 0
    if raw is None:
        return items, dropped
    for it in raw.line_items:
        loc = src.locate(it.evidence, it.page) if it.evidence else None
        if loc is None and it.description:
            loc = src.locate(it.description, it.page)
        if loc is None:
            dropped += 1
            continue
        page, line = loc
        if it.description and not value_in(it.description, line):
            dropped += 1
            continue
        issues: list[FieldIssue] = []
        nums = {}
        for k in ("quantity", "unit_price", "amount"):
            v = getattr(it, k)
            if v is None:
                nums[k] = None
                continue
            if not value_in(v, line, "amount"):
                issues.append(FieldIssue.UNGROUNDED)
                nums[k] = None
                continue
            p = parse_amount(v, hint)
            if p.value is None:
                issues.append(FieldIssue.INVALID_NUMBER)
            nums[k] = fmt_decimal(p.value) if k != "quantity" or p.value is None else format(p.value.normalize(), "f")
        conf = CONF_LLM if source == "llm" else CONF_RULES_ONLY
        if page.ocr_confidence is not None:
            conf *= 0.5 + 0.5 * page.ocr_confidence
        items.append(LineItem(description=clean_str(it.description), page=page.number, evidence=line,
                              source=source, confidence=round(conf, 3), issues=issues, **nums))
    return items, dropped


def _charges(raw: Optional[RawInvoice], src: SourceText, hint: Optional[str], source: str) -> tuple[list[Charge], int]:
    """Ground each claimed charge (amount must be printed on the cited line) and sign it:
    discounts reduce the total even when printed without a minus sign."""
    out: list[Charge] = []
    dropped = 0
    if raw is None:
        return out, dropped
    for ch in raw.other_charges:
        if ch.amount is None or not str(ch.amount).strip():
            continue
        g = src.ground(ch.amount, ch.evidence, "amount", prefer_page=ch.page)
        if g is None:
            dropped += 1
            continue
        value = parse_amount(g.raw, hint).value
        printed_label, negative = _label_and_sign(g.evidence, value, hint)
        label = clean_str(ch.label) if ch.label and value_in(ch.label, g.evidence) else None
        label = label or printed_label
        if not is_charge_label(label):
            continue  # the extractor reported tax / subtotal / a payment as a charge
        kind = charge_kind(label)
        # The sign comes from the page, not from the extractor's copy of the number.
        if value is not None and (kind == "discount" or negative):
            value = -abs(value)
        conf = CONF_LLM if source == "llm" else CONF_RULES_ONLY
        if g.ocr_confidence is not None:
            conf *= 0.5 + 0.5 * g.ocr_confidence
        out.append(Charge(kind=kind, label=label, amount=fmt_decimal(value), raw=str(g.raw), page=g.page,
                          evidence=g.evidence, source=source, confidence=round(conf, 3),
                          issues=[] if value is not None else [FieldIssue.INVALID_NUMBER]))
    # the same line reported twice
    unique = {(c.evidence, c.amount): c for c in out}
    return list(unique.values()), dropped


def _label_and_sign(line: str, value, hint: Optional[str]) -> tuple[Optional[str], bool]:
    """Text printed before the amount on its line, and whether a minus sign precedes it."""
    if value is None:
        return None, False
    for a in reversed(amounts_in(line)):
        p = parse_amount(a, hint).value
        if p is not None and abs(p) == abs(value):
            head = line[:line.rfind(a)]
            negative = p < 0 or head.rstrip().endswith(("-", "−"))
            return clean_str(re.sub(r"[\s\-−]+$", "", head)), negative
    return None, value < 0


def _merge_charges(llm_charges: list[Charge], rule_charges: list[Charge], llm_ok: bool) -> list[Charge]:
    """LLM charges first; a rules charge with the same amount confirms it, one the LLM
    missed is still kept (rules only look between the subtotal and total lines)."""
    out = list(llm_charges)
    for rc in rule_charges:
        same = next((c for c in out if c.amount is not None and c.amount == rc.amount), None)
        if same is not None:
            if same.source == "llm":
                same.source = "llm+rules"
                same.confidence = round(min(0.97, same.confidence * CONF_AGREE / CONF_LLM), 3)
            continue
        if llm_ok:
            rc.confidence = round(rc.confidence * CONF_RULES_LLM_MISSED / CONF_RULES_ONLY, 3)
        out.append(rc)
    return out


def _merge_items(llm_items: list[LineItem], rule_items: list[LineItem], llm_ok: bool) -> list[LineItem]:
    if not llm_items:
        if llm_ok:
            for it in rule_items:
                it.confidence = round(it.confidence * CONF_RULES_LLM_MISSED / CONF_RULES_ONLY, 3)
        return rule_items
    keys = {(compact(r.description or ""), r.amount) for r in rule_items}
    for it in llm_items:
        if (compact(it.description or ""), it.amount) in keys:
            it.source = "llm+rules"
            it.confidence = round(min(0.97, it.confidence * CONF_AGREE / CONF_LLM), 3)
    return llm_items


def _match_rules(llm_invoices: list[RawInvoice], rules: Optional[RawInvoice]) -> Optional[int]:
    if rules is None or not llm_invoices:
        return None
    if rules.invoice_number.value:
        key = compact(rules.invoice_number.value)
        for i, inv in enumerate(llm_invoices):
            if inv.invoice_number.value and compact(inv.invoice_number.value) == key:
                return i
    return 0 if len(llm_invoices) == 1 else None


def _records_for_segment(seg: Segment, llm_result: Optional[LLMCallResult], llm_ok: bool,
                         llm_error: Optional[str], cfg: ExtractionConfig) -> list[InvoiceRecord]:
    src = SourceText.from_pages(seg.pages)
    hint = detect_decimal_separator("\n".join(p.text for p in seg.pages))
    rules_raw = extract_rules(src.page_texts)
    llm_invoices = llm_result.invoices if llm_result else []
    matched = _match_rules(llm_invoices, rules_raw)

    plans: list[tuple[Optional[RawInvoice], Optional[RawInvoice], list[str]]] = []
    for i, inv in enumerate(llm_invoices):
        plans.append((inv, rules_raw if i == matched else None, []))
    if rules_raw is not None and matched is None:
        if not llm_ok:
            reasons = [f"rules-only extraction ({llm_error})"] if llm_error else []
            plans.append((None, rules_raw, reasons))
        elif not llm_invoices and rules_raw.invoice_number.value and rules_raw.total.value:
            plans.append((None, rules_raw, ["LLM did not recognise an invoice in this text"]))

    records = []
    for llm_raw, rules_r, reasons in plans:
        lc, lrej = _ground_raw(llm_raw, src, "llm")
        rc, _ = _ground_raw(rules_r, src, "rules")
        rec = InvoiceRecord(index=0, pages=seg.page_numbers,
                            document_type=(llm_raw or rules_r).document_type)
        for name in HEADER_FIELDS:
            setattr(rec, name, build_field(name, lc.get(name), rc.get(name), llm_ok=llm_raw is not None,
                                           rejected_raw=lrej.get(name), hint=hint, cfg=cfg))
        li_llm, dropped = _line_items(llm_raw, src, hint, "llm", cfg)
        li_rules, _ = _line_items(rules_r, src, hint, "rules", cfg)
        rec.line_items = _merge_items(li_llm, li_rules, llm_raw is not None)
        ch_llm, ch_dropped = _charges(llm_raw, src, hint, "llm")
        ch_rules, _ = _charges(rules_r, src, hint, "rules")
        rec.charges = _merge_charges(ch_llm, ch_rules, llm_raw is not None)
        if dropped:
            reasons = [*reasons, f"{dropped} line item(s) discarded: not found in the text"]
        if ch_dropped:
            reasons = [*reasons, f"{ch_dropped} other charge(s) discarded: not found in the text"]
        rec.method = "+".join(s for s, on in (("llm", llm_raw), ("rules", rules_r)) if on is not None)
        _infer_currency(rec, cfg)
        records.append(_finish(rec, cfg, reasons))
    return records


def _sheet_records(sheets: list[SheetInvoice], cfg: ExtractionConfig) -> list[InvoiceRecord]:
    records = []
    for s in sheets:
        rec = InvoiceRecord(index=0, pages=[s.page], source_label=s.label, method="spreadsheet")
        for name in HEADER_FIELDS:
            cell = s.cells.get(name)
            if cell is None:
                setattr(rec, name, ExtractedField(status=FieldStatus.MISSING, issues=[FieldIssue.NOT_FOUND]))
                continue
            cand = Cand(cell.raw, cell.evidence, cell.page, None, "spreadsheet")
            setattr(rec, name, build_field(name, None, None, llm_ok=False, rejected_raw=None,
                                           hint=s.decimal_hints.get(name), cfg=cfg, sheet=cand))
        for item in s.line_items:
            li = LineItem(source="spreadsheet", confidence=CONF_SPREADSHEET)
            for k, cell in item.items():
                if k == "description":
                    li.description = clean_str(cell.raw)
                else:
                    p = parse_amount(cell.raw)
                    if p.value is None:
                        li.issues.append(FieldIssue.INVALID_NUMBER)
                    setattr(li, k, fmt_decimal(p.value) if k != "quantity" or p.value is None
                            else format(p.value.normalize(), "f"))
                li.page, li.evidence = cell.page, cell.evidence
            rec.line_items.append(li)
        _infer_currency(rec, cfg)
        records.append(_finish(rec, cfg, []))
    return records


def _dedupe_continuations(records: list[InvoiceRecord]) -> list[InvoiceRecord]:
    """Merge an invoice that was split across two adjacent segments (same number)."""
    out: list[InvoiceRecord] = []
    for rec in records:
        prev = out[-1] if out else None
        if (prev and prev.method != "spreadsheet" and rec.method != "spreadsheet"
                and prev.invoice_number.value and rec.invoice_number.value
                and compact(prev.invoice_number.value) == compact(rec.invoice_number.value)
                # two parts that each print a total are two invoices (maybe a duplicate), not one
                and (prev.total.value is None or rec.total.value is None)):
            for n in HEADER_FIELDS:
                if prev.field(n).status != FieldStatus.FOUND and rec.field(n).status == FieldStatus.FOUND:
                    setattr(prev, n, rec.field(n))
            prev.line_items.extend(rec.line_items)
            prev.charges.extend(c for c in rec.charges
                                if (c.evidence, c.amount) not in {(p.evidence, p.amount) for p in prev.charges})
            prev.pages = sorted(set(prev.pages) | set(rec.pages))
            prev.review_reasons = sorted(set(prev.review_reasons) | set(rec.review_reasons))
            prev.extraction_notes = list(dict.fromkeys(prev.extraction_notes + rec.extraction_notes))
            prev.needs_review = bool(prev.review_reasons)
            continue
        out.append(rec)
    return out


# ------------------------------------------------------------------ entry ----------


def extract_invoices(doc: ProcessedDocument, config: Optional[ExtractionConfig] = None,
                     llm: Any = _AUTO) -> ExtractionResult:
    """Extract invoices from a processed document. Never raises on bad input.

    ``llm``: an ``InvoiceLLM``; ``None`` for rules-only; default builds one from config/env.
    """
    cfg = config or ExtractionConfig.from_env()
    t0 = time.perf_counter()
    result = ExtractionResult(document_id=doc.document_id, filename=doc.metadata.filename,
                              status="failed", extracted_at=datetime.now(timezone.utc))

    if doc.status == DocumentStatus.FAILED or not any(p.text.strip() or p.tables for p in doc.pages):
        result.issues.append("SOURCE_FAILED: document processing failed or produced no text"
                             if doc.status == DocumentStatus.FAILED else "NO_TEXT: nothing to extract from")
        result.needs_review = True
        result.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        return result

    records: list[InvoiceRecord] = []
    text_pages = doc.pages
    if doc.metadata.detected_type.is_spreadsheet:
        sheets = extract_spreadsheet(doc.pages)
        records.extend(_sheet_records(sheets, cfg))
        mapped = {s.page for s in sheets}
        # Sheets that are not registers (e.g. an invoice form in cells) go through the text path.
        text_pages = [p for p in doc.pages if p.number not in mapped and not (records and p.tables)]

    llm_errors: list[str] = []
    if text_pages:
        if llm is _AUTO:
            llm, why = make_llm(cfg)
            if llm is None:
                result.issues.append(f"{why}: extracted with rules only")
        segments = segment_pages(text_pages, cfg.max_segment_pages, cfg.max_segment_chars)
        if llm is not None:
            result.llm = LLMUsage(provider=llm.provider, model=llm.model)
        outcomes = _run_llm(segments, llm, cfg, result.llm, llm_errors)
        for seg, (res, ok, err) in zip(segments, outcomes):
            records.extend(_records_for_segment(seg, res, ok, err, cfg))

    records = _dedupe_continuations(records)
    for i, rec in enumerate(records):
        rec.index = i
    result.invoices = records
    for err in dict.fromkeys(llm_errors):
        result.issues.append(err)
    # Task 1 repaired / truncated the file or lost pages: what was extracted may be
    # fine, but something may be missing, so the document must not look clean.
    lost = _source_losses(doc)
    result.issues.extend(lost)

    if not records:
        result.status = "no_invoice_found"
        result.issues.append("NO_INVOICE_FOUND: no invoice data recognised in this document")
    elif llm_errors:
        result.status = "partial"
    else:
        result.status = "success"
    result.needs_review = result.status != "success" or bool(lost) or any(r.needs_review for r in records)
    result.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    return result


# Task 1 findings that mean part of the document may not have been read at all.
SOURCE_LOSS_CODES = ("REPAIRED", "TRUNCATED", "PAGE_FAILED", "OCR_FAILED", "OCR_TIMEOUT", "OCR_UNAVAILABLE",
                     "ROWS_TRUNCATED", "TEXT_TRUNCATED")
SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"


def _source_losses(doc: ProcessedDocument) -> list[str]:
    out: dict[str, str] = {}
    for i in doc.issues:
        code = getattr(i.code, "value", str(i.code))
        if code in SOURCE_LOSS_CODES:
            out.setdefault(code, f"{SOURCE_INCOMPLETE}: {code} - {i.message}")
    for code in doc.review_reasons:
        if code in SOURCE_LOSS_CODES:
            out.setdefault(code, f"{SOURCE_INCOMPLETE}: {code} - the file was damaged; some content may be missing")
    return list(out.values())


def _run_llm(segments: list[Segment], llm: Optional[InvoiceLLM], cfg: ExtractionConfig,
             usage: LLMUsage, errors: list[str]) -> list[tuple[Optional[LLMCallResult], bool, Optional[str]]]:
    """Call the LLM for every segment in parallel.

    * a per-minute rate limit (429) pauses *all* workers for the time the provider asks
      for, then the call is retried - the request did not count against any quota;
    * a fatal error (bad key, no credit, daily quota used up) stops further calls;
    * either way, segments without an LLM answer fall back to rules."""
    if llm is None or not segments:
        return [(None, False, None)] * len(segments)
    stop = threading.Event()
    lock = threading.Lock()
    fatal_code: list[str] = []
    resume_at = [0.0]  # monotonic time before which nobody calls (shared back-off)
    gave_up = [0]  # segments whose rate-limit retries ran out

    def wait_turn() -> None:
        while not stop.is_set():
            delay = resume_at[0] - time.monotonic()
            if delay <= 0:
                return
            stop.wait(min(delay, 5.0))

    class Stopped(Exception):
        pass

    def call(seg: Segment) -> LLMCallResult:
        for attempt in range(cfg.llm_rate_limit_retries + 1):
            wait_turn()
            if stop.is_set():
                raise Stopped
            try:
                return llm.extract([(p.number, p.text) for p in seg.pages])
            except LLMError as e:
                if e.code != "LLM_RATE_LIMITED" or attempt == cfg.llm_rate_limit_retries:
                    raise
                wait = min(float(e.retry_after or 10.0 * (attempt + 1)), cfg.llm_rate_limit_max_wait_s)
                with lock:
                    resume_at[0] = max(resume_at[0], time.monotonic() + wait)
        raise AssertionError("unreachable")

    def one(seg: Segment):
        if stop.is_set():
            return None, False, fatal_code[0] if fatal_code else "LLM_SKIPPED"
        try:
            res = call(seg)
        except Stopped:  # another segment hit a fatal error while this one waited
            return None, False, fatal_code[0] if fatal_code else "LLM_SKIPPED"
        except LLMError as e:
            with lock:
                if e.code == "LLM_RATE_LIMITED":
                    gave_up[0] += 1
                    # still limited after several waits: treat it like a used-up quota
                    if gave_up[0] >= 3 and not stop.is_set():
                        fatal_code.append("LLM_RATE_LIMITED")
                        stop.set()
                if e.fatal:
                    fatal_code.append(e.code)
                    stop.set()
                usage.calls += 1
                usage.failed_calls += 1
                errors.append(f"{e.code}: {e}")
            return None, False, e.code
        with lock:
            usage.calls += 1
            usage.input_tokens += res.input_tokens
            usage.output_tokens += res.output_tokens
            usage.cached_input_tokens += res.cached_input_tokens
            usage.duration_ms = round(usage.duration_ms + res.duration_ms, 2)
        return res, True, None

    workers = max(1, min(cfg.llm_concurrency, len(segments)))
    if workers == 1:
        return [one(s) for s in segments]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, segments))
