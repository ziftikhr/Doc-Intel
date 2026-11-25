"""Throughput benchmark for the processing stage.

Generates a workload, runs it through ``BatchProcessor`` and records measured numbers:

1. one large born-digital PDF (default 1,000 pages)       - text-layer throughput
2. one large scanned PDF (default 100 pages, 200 DPI)     - OCR throughput
3. a mixed inbox of small files incl. ~10% broken ones    - documents/minute, success rate

    python scripts/benchmark.py --digital-pages 1000 --scanned-pages 100 --mixed-files 200
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import factory  # noqa: E402

from docintel.processing import ProcessingConfig  # noqa: E402
from docintel.processing.batch import BatchProcessor  # noqa: E402
from docintel.processing.ocr import available_engines  # noqa: E402

MIXED_MAKERS = [  # (maker, extension, weight)
    (lambda p: factory.digital_pdf(p, pages=2), ".pdf", 30),
    (lambda p: factory.scanned_pdf(p, pages=1), ".pdf", 15),
    (factory.png_invoice, ".png", 10),
    (factory.poor_quality_jpeg, ".jpg", 10),
    (lambda p: factory.multipage_tiff(p, frames=2), ".tiff", 5),
    (factory.multisheet_xlsx, ".xlsx", 10),
    (factory.multisheet_xls, ".xls", 5),
    (factory.csv_utf8_bom, ".csv", 5),
    (factory.encrypted_pdf, ".pdf", 3),
    (factory.corrupted_xlsx, ".xlsx", 3),
    (factory.random_bytes, ".pdf", 2),
    (factory.empty_file, ".csv", 2),
]


def uniquify(path: Path, i: int) -> None:
    """Make every generated file byte-unique so content-hash dedup skips nothing."""
    if not path.stat().st_size:
        return
    ext = path.suffix
    if ext == ".csv":
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"UNIQ-{i},x,0,EUR\n")
        return
    if ext == ".xlsx":
        try:
            import openpyxl

            wb = openpyxl.load_workbook(path)
            wb.worksheets[0].append([f"UNIQ-{i}"])
            wb.save(path)
            return
        except Exception:
            pass  # deliberately corrupted workbook: trailing bytes are fine
    if ext == ".xls":
        import xlwt

        wb = xlwt.Workbook()
        ws = wb.add_sheet("Invoices")
        for r, row in enumerate([["Invoice No", "Total"], [f"INV-{i:05d}", 100 + i]]):
            for c, v in enumerate(row):
                ws.write(r, c, v)
        wb.save(str(path))
        return
    # PDF / PNG / JPEG / TIFF readers ignore bytes after the end marker.
    with open(path, "ab") as fh:
        fh.write(f"\n%unique-{i}\n".encode())


def build_mixed(root: Path, n: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    pool = [m for m in MIXED_MAKERS for _ in range(m[2])]
    files = []
    for i in range(n):
        maker, ext, _ = pool[i % len(pool)]
        path = root / f"doc_{i:05d}{ext}"
        maker(path)
        uniquify(path, i)
        files.append(path)
    return files


def run(name: str, files: list[Path], workers: int, out: Path, config: ProcessingConfig) -> dict:
    t0 = time.time()
    summary = BatchProcessor(config, output_dir=out / name, workers=workers, resume=False).run(files)
    wall = time.time() - t0
    s = summary.model_dump(mode="json", exclude={"failures"})
    s["failure_codes"] = sorted({c for f in summary.failures for c in f.codes})
    print(f"\n== {name}: {summary.pages_total} pages / {len(files)} files in {wall:.1f}s "
          f"-> {summary.pages_per_second} pages/s, {summary.documents_per_minute} docs/min")
    print(json.dumps(s, indent=2))
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--digital-pages", type=int, default=1000)
    ap.add_argument("--scanned-pages", type=int, default=100)
    ap.add_argument("--mixed-files", type=int, default=200)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--report", default="docs/benchmark_results.json")
    ap.add_argument("--keep", action="store_true", help="keep generated files")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="docintel_bench_"))
    config = ProcessingConfig()
    results = {
        "machine": {"platform": platform.platform(), "python": platform.python_version(),
                    "cpus": os.cpu_count(), "workers": args.workers,
                    "ocr_engine": (available_engines() or ["none"])[0]},
        "config": {"chunk_pages": config.chunk_pages, "ocr_dpi": config.ocr_dpi},
    }
    try:
        print(f"Generating workload in {work} ...")
        t = time.time()
        digital = factory.large_digital_pdf(work / "digital_big.pdf", args.digital_pages)
        scanned = factory.scanned_pdf(work / "scanned_big.pdf", pages=args.scanned_pages, dpi=200)
        mixed = build_mixed(work / "mixed", args.mixed_files)
        print(f"generated in {time.time() - t:.1f}s")

        out = work / "out"
        results["digital_pdf"] = run("digital_pdf", [digital], args.workers, out, config)
        results["scanned_pdf"] = run("scanned_pdf", [scanned], args.workers, out, config)
        results["mixed_inbox"] = run("mixed_inbox", mixed, args.workers, out, config)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(results, indent=2))
    print(f"\nReport written to {report}")


if __name__ == "__main__":
    main()
