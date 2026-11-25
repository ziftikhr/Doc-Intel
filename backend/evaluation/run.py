"""Run the labeled dataset through the full pipeline and measure it.

    python -m evaluation.run --data eval_data --name rules            # rules only (no API calls)
    python -m evaluation.run --data eval_data --name ai --llm auto --only "digital_pdf" --limit 8
    python -m evaluation.run --stress path/to/big.pdf --name stress   # unlabeled throughput run
    python -m evaluation.report                                       # docs/EVALUATION.md

Every document goes through exactly what the API worker does: Task 1 processing ->
Task 2 extraction -> Task 3 validation (with one duplicate index shared across the
whole run, like a real inbox). Results are written to docs/eval/<name>.json.

How things are scored
* An extracted invoice is matched to a labeled one by invoice number, then by total.
  A labeled invoice with no match counts as missed - all of its fields count as wrong.
* Header fields: exact match after harmless normalisation only (case, spaces and
  punctuation in names/numbers; amounts compared as decimals; dates as ISO dates).
  A field that is *not printed* (label null) is correct only if nothing was extracted.
* Line items: matched by amount + description; precision and recall over all lines.
* Other charges: the signed sum must equal the labeled sum.
* Validation: each planted error must produce its finding (recall); clean invoices
  must not be marked invalid (false-alarm rate).
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import time
import traceback
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from docintel.extraction import ExtractionConfig, extract_invoices
from docintel.extraction.schema import HEADER_FIELDS, FieldStatus, InvoiceRecord
from docintel.processing import ProcessingConfig, process_file
from docintel.settings import load_env
from docintel.validation import DuplicateIndex, ValidationConfig, validate_document

OUT_DIR = Path("docs/eval")
TEXT_FIELDS = ("invoice_number", "supplier_name", "customer_name")
AMOUNTS = ("subtotal", "tax", "total")
FINDING_ALIASES = {"DUPLICATE_INVOICE": {"DUPLICATE_INVOICE", "DUPLICATE_NUMBER_CONFLICT"}}


# ------------------------------------------------------------------ helpers ----------


def norm_text(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    return re.sub(r"[\W_]+", "", s) or None


def dec(s) -> Optional[Decimal]:
    try:
        return None if s is None else Decimal(str(s))
    except InvalidOperation:
        return None


def same(name: str, truth, got) -> bool:
    if truth is None:
        return got is None
    if got is None:
        return False
    if name in AMOUNTS:
        return dec(truth) == dec(got)
    if name in TEXT_FIELDS:
        return norm_text(truth) == norm_text(got)
    return str(truth).strip().upper() == str(got).strip().upper()


def value(rec: InvoiceRecord, name: str):
    f = rec.field(name)
    return f.value if f.status == FieldStatus.FOUND else None


def pct(a: float, b: float) -> Optional[float]:
    return round(100 * a / b, 1) if b else None


def quantiles(xs: list[float]) -> dict:
    if not xs:
        return {}
    xs = sorted(xs)
    p = lambda q: xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]  # noqa: E731
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 1), "p50": round(p(0.5), 1), "p95": round(p(0.95), 1),
            "max": round(xs[-1], 1)}


# ------------------------------------------------------------------ matching ----------


def match(labels: list[dict], got: list[InvoiceRecord]) -> list[tuple[dict, Optional[InvoiceRecord]]]:
    free = list(got)
    pairs: list[tuple[dict, Optional[InvoiceRecord]]] = []
    for lab in labels:
        hit = None
        key = norm_text(lab["invoice_number"])
        if key:
            hit = next((r for r in free if norm_text(value(r, "invoice_number")) == key), None)
        if hit is None:
            hit = next((r for r in free if dec(value(r, "total")) == dec(lab["total"])
                        and (not key or value(r, "invoice_number") is None)), None)
        if hit is None and len(labels) == 1 and len(free) == 1:
            hit = free[0]
        if hit is not None:
            free.remove(hit)
        pairs.append((lab, hit))
    return pairs


def line_match(truth: list[dict], got) -> tuple[int, int, int, int]:
    """(matched, labeled, extracted, matched with correct qty and unit price)"""
    free = list(got)
    matched = exact = 0
    for t in truth:
        hit = next((g for g in free if dec(g.amount) == dec(t["amount"])
                    and (norm_text(g.description) == norm_text(t["description"])
                         or (norm_text(t["description"]) or "") in (norm_text(g.description) or ""))), None)
        if hit:
            free.remove(hit)
            matched += 1
            exact += dec(hit.quantity) == dec(t["quantity"]) and dec(hit.unit_price) == dec(t["unit_price"])
    return matched, len(truth), len(got), exact


# ------------------------------------------------------------------ run ----------


@dataclass
class DocResult:
    file: str
    format: str
    layout: str
    expect: str
    pages: int = 0
    crashed: Optional[str] = None
    processing_status: Optional[str] = None
    extraction_status: Optional[str] = None
    validation_status: Optional[str] = None
    ms: dict = field(default_factory=dict)
    invoices: list = field(default_factory=list)  # per labeled invoice: scoring detail
    extra_invoices: int = 0
    reason: Optional[str] = None


def evaluate(data: Path, *, llm_provider: str, only: Optional[str], limit: Optional[int]) -> dict:
    labels = json.loads((data / "labels.json").read_text(encoding="utf-8"))
    docs = labels["documents"]
    if only:
        rx = re.compile(only)
        docs = [d for d in docs if rx.search(d["file"]) or rx.search(d["format"]) or rx.search(d["layout"])]
    if limit:
        docs = docs[:limit]
    pcfg = ProcessingConfig()
    ecfg = ExtractionConfig.from_env(llm_provider=llm_provider)
    vcfg = ValidationConfig()
    index = DuplicateIndex()
    llm = None if llm_provider == "none" else _make_llm(ecfg)
    usage = Counter()
    results: list[DocResult] = []
    for n, d in enumerate(docs, 1):
        r = DocResult(file=d["file"], format=d["format"], layout=d["layout"], expect=d["expect"])
        try:
            t0 = time.perf_counter()
            doc = process_file(data / d["file"], pcfg)
            t1 = time.perf_counter()
            res = extract_invoices(doc, ecfg, llm=llm)
            t2 = time.perf_counter()
            report = validate_document(res, vcfg, duplicates=index, source=d["file"])
            t3 = time.perf_counter()
        except Exception as e:  # noqa: BLE001 - a crash is a result, not the end of the run
            r.crashed = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            results.append(r)
            continue
        r.pages = len(doc.pages)
        r.ms = {"processing": (t1 - t0) * 1000, "extraction": (t2 - t1) * 1000, "validation": (t3 - t2) * 1000,
                "total": (t3 - t0) * 1000}
        r.processing_status, r.extraction_status, r.validation_status = doc.status.value, res.status, report.status
        r.reason = (res.issues or [None])[0]
        for k in ("calls", "failed_calls", "input_tokens", "output_tokens"):
            usage[k] += getattr(res.llm, k)
        by_index = {v.index: v for v in report.invoices}
        pairs = match(d["invoices"], res.invoices)
        r.extra_invoices = len(res.invoices) - sum(1 for _, g in pairs if g is not None)
        for lab, got in pairs:
            v = by_index.get(got.index) if got is not None else None
            codes = {i.code for i in v.issues} if v else set()
            entry = {"invoice_number": lab["invoice_number"], "found": got is not None,
                     "method": got.method if got else None,
                     "fields": {f: same(f, lab[f], value(got, f) if got else None) for f in HEADER_FIELDS},
                     "wrong": {f: [lab[f], value(got, f)] for f in HEADER_FIELDS
                               if got is not None and not same(f, lab[f], value(got, f))},
                     "expected_findings": lab["expected_findings"],
                     "detected": {e: bool(codes & FINDING_ALIASES.get(e, {e})) for e in lab["expected_findings"]},
                     "status": v.status if v else None, "codes": sorted(codes)}
            if lab["line_items"]:
                m, t, g, ex = line_match(lab["line_items"], got.line_items if got else [])
                entry["lines"] = {"matched": m, "labeled": t, "extracted": g, "qty_price_ok": ex}
            truth_charges = sum((Decimal(c["amount"]) for c in lab["charges"]), Decimal(0))
            got_charges = sum((Decimal(c.amount) for c in (got.charges if got else []) if c.amount), Decimal(0))
            entry["charges"] = {"labeled": bool(lab["charges"]), "ok": got is not None and truth_charges == got_charges,
                                "extracted": bool(got and got.charges)}
            r.invoices.append(entry)
        results.append(r)
        print(f"[{n}/{len(docs)}] {d['file']:40} {r.extraction_status or 'CRASH':16} {r.ms.get('total', 0):8.0f} ms"
              f"  fields ok {sum(sum(e['fields'].values()) for e in r.invoices)}/{len(r.invoices) * len(HEADER_FIELDS)}")
    return {"results": results, "usage": dict(usage), "llm": llm_provider,
            "model": getattr(llm, "model", None), "dataset": {"seed": labels.get("seed"), "documents": len(docs)}}


def _make_llm(cfg: ExtractionConfig):
    from docintel.extraction.llm import make_llm

    llm, why = make_llm(cfg)
    if llm is None:
        raise SystemExit(f"no LLM available: {why}")
    return llm


# ------------------------------------------------------------------ metrics ----------


def metrics(run: dict) -> dict:
    results: list[DocResult] = run["results"]
    ok_docs = [r for r in results if r.expect == "ok"]
    bad_docs = [r for r in results if r.expect == "fail"]
    succeeded = [r for r in ok_docs if not r.crashed and r.extraction_status in ("success", "partial")
                 and any(e["found"] for e in r.invoices)]
    rejected = [r for r in bad_docs if not r.crashed and r.extraction_status in ("failed", "no_invoice_found")]
    invs = [e for r in ok_docs for e in r.invoices]

    def field_table(entries) -> dict:
        out = {}
        for f in HEADER_FIELDS:
            out[f] = pct(sum(e["fields"][f] for e in entries), len(entries))
        cells = [e["fields"][f] for e in entries for f in HEADER_FIELDS]
        out["all_fields"] = pct(sum(cells), len(cells))
        out["invoices_fully_correct"] = pct(sum(all(e["fields"].values()) for e in entries), len(entries))
        out["invoices_found"] = pct(sum(e["found"] for e in entries), len(entries))
        out["n_invoices"] = len(entries)
        return out

    by = lambda key: {k: field_table([e for r in ok_docs if getattr(r, key) == k for e in r.invoices])  # noqa: E731
                      for k in sorted({getattr(r, key) for r in ok_docs})}
    lines = [e["lines"] for e in invs if "lines" in e]
    m, t, g, ex = (sum(x[k] for x in lines) for k in ("matched", "labeled", "extracted", "qty_price_ok"))
    with_charges = [e for e in invs if e["charges"]["labeled"]]
    without = [e for e in invs if not e["charges"]["labeled"]]

    planted = defaultdict(lambda: [0, 0])
    for e in invs:
        for code, hit in e["detected"].items():
            planted[code][0] += hit
            planted[code][1] += 1
    clean = [e for e in invs if not e["expected_findings"] and e["found"]]
    times = [r.ms["total"] for r in results if r.ms]
    per_format_time = {f: quantiles([r.ms["total"] for r in results if r.ms and r.format == f])
                       for f in sorted({r.format for r in results})}
    pages = sum(r.pages for r in results if r.ms)
    total_s = sum(times) / 1000
    wrong = Counter((f) for e in invs for f in e.get("wrong", {}))
    examples = defaultdict(list)
    for r in ok_docs:
        for e in r.invoices:
            for f, (want, got) in e.get("wrong", {}).items():
                if len(examples[f]) < 4:
                    examples[f].append({"file": r.file, "expected": want, "got": got})
    return {
        "documents": {
            "total": len(results), "valid_inputs": len(ok_docs), "broken_inputs": len(bad_docs),
            "success_rate": pct(len(succeeded), len(ok_docs)),
            "failure_rate": pct(len(ok_docs) - len(succeeded), len(ok_docs)),
            "failed_files": [r.file for r in ok_docs if r not in succeeded],
            "broken_correctly_rejected": pct(len(rejected), len(bad_docs)),
            "crashes": sum(1 for r in results if r.crashed),
        },
        "extraction": {"overall": field_table(invs), "by_format": by("format"), "by_layout": by("layout"),
                       "extra_invoices": sum(r.extra_invoices for r in ok_docs),
                       "errors_by_field": dict(wrong.most_common()), "examples": dict(examples)},
        "line_items": {"recall": pct(m, t), "precision": pct(m, g), "qty_and_price_correct": pct(ex, m),
                       "labeled": t, "extracted": g},
        "charges": {"invoices_with_charges": len(with_charges),
                    "sum_correct": pct(sum(e["charges"]["ok"] for e in with_charges), len(with_charges)),
                    "false_charges_on_invoices_without": sum(e["charges"]["extracted"] for e in without)},
        "validation": {
            "planted_detected": {k: {"detected": v[0], "planted": v[1], "recall": pct(*v)} for k, v in planted.items()},
            "planted_recall_overall": pct(sum(v[0] for v in planted.values()), sum(v[1] for v in planted.values())),
            "clean_invoices": len(clean),
            "clean_marked_invalid": pct(sum(e["status"] == "invalid" for e in clean), len(clean)),
            "clean_marked_valid": pct(sum(e["status"] == "valid" for e in clean), len(clean)),
            "false_alarm_codes": dict(Counter(c for e in clean if e["status"] == "invalid" for c in e["codes"])
                                      .most_common(8)),
        },
        "timing_ms": {
            "per_document": quantiles(times), "per_format": per_format_time,
            "stage_mean": {s: round(statistics.fmean([r.ms[s] for r in results if r.ms]), 1)
                           for s in ("processing", "extraction", "validation")} if times else {},
            "pages": pages, "pages_per_minute": round(pages / total_s * 60, 1) if total_s else None,
            "documents_per_minute": round(len(times) / total_s * 60, 1) if total_s else None,
        },
        "llm_usage": run["usage"],
    }


# ------------------------------------------------------------------ stress ----------


def stress(path: Path, llm_provider: str) -> dict:
    """Unlabeled run on a large real file: success, time, invoices and verdicts only."""
    ecfg = ExtractionConfig.from_env(llm_provider=llm_provider)
    llm = None if llm_provider == "none" else _make_llm(ecfg)
    t0 = time.perf_counter()
    doc = process_file(path, ProcessingConfig())
    t1 = time.perf_counter()
    res = extract_invoices(doc, ecfg, llm=llm)
    t2 = time.perf_counter()
    report = validate_document(res, ValidationConfig())
    t3 = time.perf_counter()
    return {"file": path.name, "pages": len(doc.pages), "processing_status": doc.status.value,
            "extraction_status": res.status, "invoices": len(res.invoices),
            "seconds": {"processing": round(t1 - t0, 1), "extraction": round(t2 - t1, 1),
                        "validation": round(t3 - t2, 1), "total": round(t3 - t0, 1)},
            "pages_per_minute": round(len(doc.pages) / (t3 - t0) * 60),
            "verdicts": dict(Counter(v.status for v in report.invoices)),
            "findings": dict(Counter(i.code for v in report.invoices for i in v.issues).most_common(10)),
            "llm_usage": res.llm.model_dump()}


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate the pipeline on the labeled dataset")
    ap.add_argument("--data", default="eval_data")
    ap.add_argument("--name", default="rules")
    ap.add_argument("--llm", default="none", help="none (rules only) | auto | gemini | openai")
    ap.add_argument("--only", help="regex on file name / format / layout")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--stress", help="large unlabeled file to time instead of the dataset")
    args = ap.parse_args()
    load_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"name": args.name, "llm": args.llm, "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": f"{platform.system()} {platform.release()}, {platform.processor() or platform.machine()}, "
                       f"Python {platform.python_version()}"}
    if args.stress:
        out = {**meta, "stress": stress(Path(args.stress), args.llm)}
    else:
        run = evaluate(Path(args.data), llm_provider=args.llm, only=args.only, limit=args.limit)
        out = {**meta, "model": run["model"], "dataset": run["dataset"], "metrics": metrics(run),
               "documents": [r.__dict__ for r in run["results"]]}
        m = out["metrics"]
        print(json.dumps({k: m[k] for k in ("documents", "line_items", "charges")}, indent=1))
        print("fields:", json.dumps(m["extraction"]["overall"]))
        print("validation:", json.dumps(m["validation"]["planted_detected"]), "clean invalid %:",
              m["validation"]["clean_marked_invalid"])
        print("timing:", json.dumps(m["timing_ms"]["per_document"]), m["timing_ms"]["pages_per_minute"], "pages/min")
    path = OUT_DIR / f"{args.name}.json"
    path.write_text(json.dumps(out, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
