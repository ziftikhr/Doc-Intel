"""Command line interface.

    docintel process ./inbox --out ./results --workers 8
    docintel inspect invoice.pdf            # one file, JSON to stdout
    docintel engines                        # which OCR engines are usable
    docintel extract invoice.pdf -o out.json # Task 2+3: fields + validation
    docintel validate out1.json out2.json   # re-check saved results, find duplicates
    docintel check-llm                      # test the LLM connection
    docintel serve                          # Task 4: REST API (+ background worker) on :8000
    docintel worker                         # extra background worker process
    docintel search "office furniture"      # Task 5: semantic + keyword search
"""

from __future__ import annotations

import argparse
import json
import sys

from .batch import BatchProcessor, discover_files
from .config import ProcessingConfig
from .models import ProcessedDocument
from .ocr import available_engines
from .pipeline import process_file


def _config(args) -> ProcessingConfig:
    return ProcessingConfig.from_env(
        ocr_engine=getattr(args, "ocr_engine", None),
        ocr_dpi=getattr(args, "dpi", None),
        ocr_languages=getattr(args, "lang", None),
        chunk_pages=getattr(args, "chunk_pages", None),
        extract_pdf_tables=True if getattr(args, "tables", False) else None,
    )


def cmd_process(args) -> int:
    files = discover_files(args.inputs)
    if not files:
        print("No supported files found.", file=sys.stderr)
        return 2
    config = _config(args)

    def progress(doc: ProcessedDocument, done: int, total: int) -> None:
        flag = " [review]" if doc.needs_review else ""
        print(f"[{done}/{total}] {doc.status.value:<7} {doc.metadata.filename} "
              f"({doc.stats.pages_total} pages, {doc.duration_ms / 1000:.1f}s){flag}", flush=True)

    runner = BatchProcessor(config, output_dir=args.out, workers=args.workers,
                            resume=not args.no_resume, on_document=progress)
    print(f"Processing {len(files)} file(s) with {runner.workers} worker(s)...", flush=True)
    summary = runner.run(files)
    print(json.dumps(summary.model_dump(mode="json", exclude={"failures"}), indent=2))
    if summary.failures:
        print(f"\n{len(summary.failures)} document(s) failed or partially failed:")
        for f in summary.failures[:50]:
            print(f"  {f.status.value:<7} {f.path}: {', '.join(f.codes)} - {f.message}")
    print(f"\nResults written to {args.out}")
    return 0


def cmd_inspect(args) -> int:
    doc = process_file(args.file, _config(args))
    data = doc.model_dump(mode="json")
    if not args.full:
        for page in data["pages"]:
            if len(page["text"]) > 500:
                page["text"] = page["text"][:500] + " ...[truncated]"
            for t in page["tables"]:
                t["rows"] = t["rows"][:10]
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.output:
        # Write UTF-8 directly: shell redirection on Windows PowerShell 5.1 (`>`)
        # produces UTF-16 and mangles non-ASCII characters via the console codepage.
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"{doc.status.value}: {doc.stats.pages_total} page(s), "
              f"{doc.stats.total_chars} chars -> {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0 if doc.status.value != "failed" else 1


def _load_processed(path: str) -> ProcessedDocument:
    """Read a Task 1 JSON result (UTF-8 or the UTF-16 that PowerShell's `>` writes)."""
    raw = open(path, "rb").read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    return ProcessedDocument.model_validate_json(text)


def cmd_extract(args) -> int:
    from ..extraction import ExtractionConfig, extract_invoices

    if args.file.lower().endswith(".json"):
        doc = _load_processed(args.file)
    else:
        doc = process_file(args.file, _config(args))
    cfg = ExtractionConfig.from_env(
        llm_provider="none" if args.no_llm else args.provider, llm_model=args.model,
    )
    result = extract_invoices(doc, cfg)
    report = None
    if not args.no_validate:
        from ..validation import ValidationConfig, apply_validation, validate_document

        report = validate_document(result, ValidationConfig.from_env())
        result = apply_validation(result, report)
    if args.simple:
        status_of = {v.index: v.status for v in report.invoices} if report else {}
        data = {"filename": result.filename, "status": result.status, "issues": result.issues,
                "validation_status": report.status if report else None,
                "invoices": [{**r.to_simple(), "validation_status": status_of.get(r.index),
                              "needs_review": r.needs_review, "review_reasons": r.review_reasons}
                             for r in result.invoices]}
    else:
        data = result.model_dump(mode="json")
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if report:
        c = report.counts()
        verdict = f"{c['valid']} valid, {c['needs_review']} need review, {c['invalid']} invalid"
    else:
        verdict = f"{sum(r.needs_review for r in result.invoices)} need review (not validated)"
    summary = (f"{result.status}: {len(result.invoices)} invoice(s) - {verdict}; "
               f"LLM calls {result.llm.calls} ({result.llm.model or 'none'}), {result.duration_ms / 1000:.1f}s")
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"{summary} -> {args.output}", file=sys.stderr)
    else:
        print(text)
        print(summary, file=sys.stderr)
    for issue in result.issues:
        print(f"  ! {issue}", file=sys.stderr)
    if report:
        _print_findings([report])
    return 1 if result.status == "failed" else 0


def _print_findings(reports) -> None:
    for rep in reports:
        for i in rep.issues:
            if i.severity != "info":
                print(f"  {rep.filename}: [{i.severity}] {i.message}", file=sys.stderr)
        for v in rep.invoices:
            for i in v.issues:
                if i.severity != "info":
                    print(f"  {rep.filename} #{v.index + 1} {v.invoice_number or ''}: "
                          f"[{i.severity}] {i.message}", file=sys.stderr)


def _extraction_jsons(inputs: list[str]) -> list[str]:
    from pathlib import Path

    files = []
    for item in inputs:
        p = Path(item)
        files.extend(sorted(str(f) for f in p.rglob("*.json")) if p.is_dir() else [str(p)])
    return files


def cmd_validate(args) -> int:
    """Validate saved `docintel extract` results, catching duplicates across all of them."""
    from ..extraction.schema import ExtractionResult
    from ..validation import ValidationConfig, validate_batch

    results, sources = [], []
    for path in _extraction_jsons(args.inputs):
        try:
            raw = open(path, "rb").read()
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
            data = json.loads(text)
            if "invoices" not in data or "extracted_at" not in data:
                print(f"  skipped {path}: not a `docintel extract` result", file=sys.stderr)
                continue
            results.append(ExtractionResult.model_validate(data))
            sources.append(path)
        except Exception as e:  # noqa: BLE001 - report and continue with the other files
            print(f"  skipped {path}: {type(e).__name__}: {e}", file=sys.stderr)
    if not results:
        print("no extraction results to validate", file=sys.stderr)
        return 1
    reports = validate_batch(results, ValidationConfig.from_env(), sources=sources)

    width = max(len(r.filename) for r in reports)
    for r in reports:
        c = r.counts()
        print(f"{r.filename:<{width}}  {r.status:<12}  {len(r.invoices)} invoice(s): "
              f"{c['valid']} valid, {c['needs_review']} need review, {c['invalid']} invalid", file=sys.stderr)
    _print_findings(reports)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            json.dump([r.model_dump(mode="json") for r in reports], fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"-> {args.output}", file=sys.stderr)
    return 0


def cmd_check_llm(args) -> int:
    """Send one tiny invoice to the configured LLM and report whether it answered correctly."""
    from docintel.extraction.config import ExtractionConfig
    from docintel.extraction.llm import LLMError, make_llm

    config = ExtractionConfig.from_env(llm_provider=args.provider, llm_model=args.model)
    llm, reason = make_llm(config)
    if llm is None:
        hint = {"LLM_NOT_CONFIGURED": "no API key found (OPENAI_API_KEY / GEMINI_API_KEY in .env)",
                "LLM_DISABLED": "provider is 'none'"}.get(reason, reason)
        print(f"NOT CONNECTED: {hint}", file=sys.stderr)
        return 1
    print(f"provider={llm.provider} model={llm.model} ... ", end="", flush=True, file=sys.stderr)
    sample = "\n".join([
        "ACME Supplies Ltd", "Invoice Number: INV-TEST-7", "Invoice Date: 2024-03-15",
        "Bill To: Globex Corporation", "Widget A   2   10.00   20.00", "Subtotal: 20.00",
        "Tax (10%): 2.00", "Total: 22.00 USD",
    ])
    try:
        r = llm.extract([(1, sample)])
    except LLMError as e:
        print(f"FAILED\n{e.code}: {e}", file=sys.stderr)
        return 1
    print(f"OK ({r.duration_ms:.0f} ms, {r.input_tokens} in / {r.output_tokens} out tokens)", file=sys.stderr)
    if not r.invoices:
        print("connected, but the model returned no invoice", file=sys.stderr)
        return 1
    inv = r.invoices[0]
    got = {k: getattr(inv, k).value for k in ("invoice_number", "invoice_date", "supplier_name",
                                              "customer_name", "subtotal", "tax", "total", "currency")}
    got["line_items"] = len(inv.line_items)
    expected = {"invoice_number": "INV-TEST-7", "total": "22.00", "customer_name": "Globex Corporation"}
    for k, v in got.items():
        print(f"  {k:15} {v}")
    ok = all(str(got[k] or "").strip() .startswith(v) for k, v in expected.items())
    print("RESULT: working" if ok else "RESULT: connected but answers look wrong", file=sys.stderr)
    return 0 if ok else 1


def cmd_engines(_args) -> int:
    engines = available_engines()
    print(json.dumps({"available": engines, "auto_selects": engines[0] if engines else None}))
    return 0 if engines else 1


def _app_settings():
    from ..settings import Settings

    return Settings.from_env()


def cmd_serve(args) -> int:
    import uvicorn

    from ..api import create_app

    settings = _app_settings().with_overrides(
        inline_worker=False if args.no_worker else None, worker_threads=args.workers)
    print(f"API on http://{args.host}:{args.port}  (docs: http://{args.host}:{args.port}/docs)", file=sys.stderr)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(args) -> int:
    import logging
    import signal
    import threading

    from ..db import get_engine, init_db, session_factory
    from ..jobs import Pipeline, Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = _app_settings()
    init_db(get_engine(settings.database_url))
    pipeline = Pipeline.from_settings(settings)
    print(f"embeddings: {pipeline.embedder_info}", file=sys.stderr)
    worker = Worker(session_factory(settings.database_url), pipeline)
    if args.once:
        n = worker.drain()
        print(f"processed {n} job(s)", file=sys.stderr)
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker.run_forever(stop)
    return 0


def cmd_db_init(_args) -> int:
    from ..db import check_connection, get_engine, init_db

    settings = _app_settings()
    engine = get_engine(settings.database_url)
    ok, info = check_connection(engine)
    if not ok:
        print(f"cannot connect: {info}\n(is the database running? `docker compose up -d`)", file=sys.stderr)
        return 1
    init_db(engine)
    print(f"schema ready ({info})", file=sys.stderr)
    return 0


def cmd_reindex(args) -> int:
    """Rebuild chunks + embeddings for every document (e.g. after changing the embedding model)."""
    from sqlalchemy import select

    from ..db import Document, session_factory, session_scope
    from ..search import index_document, make_embedder

    settings = _app_settings()
    embedder, info = make_embedder(settings)
    factory = session_factory(settings.database_url)
    with session_scope(factory) as s:
        ids = list(s.scalars(select(Document.id).where(Document.status == "processed")))
    print(f"re-indexing {len(ids)} document(s) with {info}", file=sys.stderr)
    for i, doc_id in enumerate(ids, 1):
        with session_scope(factory) as s:
            r = index_document(s, doc_id, embedder)
        print(f"  [{i}/{len(ids)}] {doc_id}: {r.status}, {r.chunks} chunks"
              + (f" ({r.error})" if r.error else ""), file=sys.stderr)
    return 0


def cmd_search(args) -> int:
    from ..db import session_factory, session_scope
    from ..extraction.normalize import fmt_decimal
    from ..search import SearchFilters, make_embedder, search

    settings = _app_settings()
    embedder, _ = make_embedder(settings)
    with session_scope(session_factory(settings.database_url)) as s:
        r = search(s, args.query, embedder, SearchFilters(supplier=args.supplier), limit=args.limit)
    print(f"mode: {r.mode}" + (f" ({r.embedding_model})" if r.embedding_model else "")
          + (f" - {r.note}" if r.note else ""))
    print("\nInvoices:")
    for h in r.invoices or []:
        sim = f" sim={h.similarity:.2f}" if h.similarity is not None else ""
        print(f"  {h.score:.4f}{sim} [{'+'.join(h.matched_by)}] {h.invoice_number or '-'} | "
              f"{h.supplier_name or '-'} | {h.invoice_date or '-'} | {fmt_decimal(h.total) or '-'} {h.currency or ''} "
              f"| {h.validation_status} | {h.filename}")
    print("\nPassages:")
    for h in r.passages or []:
        sim = f" sim={h.similarity:.2f}" if h.similarity is not None else ""
        print(f"  {h.score:.4f}{sim} [{'+'.join(h.matched_by)}] {h.filename} p.{h.page_number}: {h.snippet[:150]}")
    return 0


def _load_env() -> None:
    """Load OPENAI_API_KEY etc. from a .env file in the working directory, if present."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    load_dotenv(find_dotenv(usecwd=True), override=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docintel", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def ocr_opts(p):
        p.add_argument("--ocr-engine", choices=["auto", "tesseract", "rapidocr", "none"])
        p.add_argument("--dpi", type=int, help="render DPI for scanned PDF pages (default 300)")
        p.add_argument("--lang", help="Tesseract languages, e.g. eng+deu")
        p.add_argument("--tables", action="store_true", help="detect tables on digital PDF pages")

    p = sub.add_parser("process", help="process files/directories in parallel")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--out", default="results")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--chunk-pages", type=int, help="pages per work unit (default 25)")
    p.add_argument("--no-resume", action="store_true", help="reprocess files already in --out")
    ocr_opts(p)
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("inspect", help="process one file and print the JSON result")
    p.add_argument("file")
    p.add_argument("--full", action="store_true", help="do not truncate text/tables")
    p.add_argument("-o", "--output", help="write the JSON to this file (UTF-8) instead of stdout")
    ocr_opts(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("extract", help="extract invoice fields (Task 2) from a file or a Task 1 JSON")
    p.add_argument("file", help="document, or a JSON written by `docintel inspect`")
    p.add_argument("-o", "--output", help="write the JSON to this file (UTF-8) instead of stdout")
    p.add_argument("--no-llm", action="store_true", help="rules only, no API calls")
    p.add_argument("--provider", choices=["auto", "openai", "gemini", "none"],
                   help="LLM provider (default DOCINTEL_LLM_PROVIDER or auto: Gemini if its key is set)")
    p.add_argument("--model", help="LLM model (default DOCINTEL_LLM_MODEL, else the provider default)")
    p.add_argument("--simple", action="store_true", help="plain field values only, no evidence/confidence")
    p.add_argument("--no-validate", action="store_true", help="skip the Task 3 validation checks")
    ocr_opts(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("validate", help="validate saved extract results and find duplicates across them")
    p.add_argument("inputs", nargs="+", help="JSON files written by `docintel extract -o`, or folders of them")
    p.add_argument("-o", "--output", help="write the validation reports to this JSON file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("check-llm", help="test the LLM connection with a tiny sample invoice")
    p.add_argument("--provider", choices=["auto", "openai", "gemini"])
    p.add_argument("--model")
    p.set_defaults(func=cmd_check_llm)

    p = sub.add_parser("engines", help="list usable OCR engines")
    p.set_defaults(func=cmd_engines)

    p = sub.add_parser("serve", help="run the REST API (Task 4) with an inline background worker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, help="inline worker threads (default DOCINTEL_WORKER_THREADS or 1)")
    p.add_argument("--no-worker", action="store_true", help="API only; run `docintel worker` separately")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="run a background worker that processes queued uploads")
    p.add_argument("--once", action="store_true", help="process everything queued, then exit")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("db-init", help="create the database tables and indexes")
    p.set_defaults(func=cmd_db_init)

    p = sub.add_parser("reindex", help="rebuild search chunks and embeddings for all documents")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("search", help="semantic + keyword search (Task 5)")
    p.add_argument("query")
    p.add_argument("--supplier")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_search)

    _load_env()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
