"""PDF processing with PyMuPDF.

Why PyMuPDF: it is the fastest Python PDF engine (~1-5 ms per page for text), renders
pages for OCR without a Poppler dependency, and repairs broken xref tables on open.

Every page is classified independently, so mixed documents (digital invoice pages +
scanned delivery notes in one 1,000-page file) get OCR only where needed.
"""

from __future__ import annotations

import math
import time
import unicodedata
from pathlib import Path

import numpy as np
import pymupdf

from ..config import ProcessingConfig
from ..errors import ProcessingError
from ..models import ExtractionMethod, Issue, IssueCode, PageResult, Table
from ..ocr import ocr_image

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)


def _open(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # FileDataError, RuntimeError, ValueError depending on damage
        raise ProcessingError(IssueCode.CORRUPTED, f"Unreadable PDF: {exc}") from exc
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise ProcessingError(IssueCode.ENCRYPTED, "PDF is password-protected")
    return doc


def inspect_pdf(path: Path, config: ProcessingConfig) -> tuple[int, dict, list[Issue]]:
    """Cheap pass: page count, metadata, repair/encryption status."""
    issues: list[Issue] = []
    with _open(path) as doc:
        n = doc.page_count
        if n == 0:
            raise ProcessingError(IssueCode.CORRUPTED, "PDF contains no readable pages")
        if n > config.max_pages:
            raise ProcessingError(
                IssueCode.TOO_MANY_PAGES, f"PDF has {n} pages (limit {config.max_pages})"
            )
        if doc.is_repaired:
            issues.append(Issue.warning(
                IssueCode.REPAIRED,
                "PDF structure was damaged and has been repaired; some content may be missing",
            ))
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        meta["is_repaired"] = bool(doc.is_repaired)
        meta["was_encrypted"] = bool(doc.is_encrypted)
        meta["estimated_ocr_share"] = estimate_ocr_share(doc, config)
    return n, meta, issues


def estimate_ocr_share(doc: pymupdf.Document, config: ProcessingConfig, samples: int = 12) -> float:
    """Share of evenly sampled pages without a usable text layer (~2 ms per sample).
    Used only to size work units; every page is still classified individually."""
    n = doc.page_count
    idxs = sorted({int(i * n / samples) for i in range(min(samples, n))})
    scanned = 0
    for i in idxs:
        try:
            text = doc.load_page(i).get_text("text").strip()
        except Exception:
            scanned += 1
            continue
        if len(text) < config.min_text_chars or garbage_ratio(text) > config.max_garbage_ratio:
            scanned += 1
    return round(scanned / len(idxs), 3) if idxs else 0.0


def process_pdf_pages(path: Path, start: int, end: int, config: ProcessingConfig) -> list[PageResult]:
    """Process pages [start, end) (0-based). Each page fails independently."""
    results: list[PageResult] = []
    with _open(path) as doc:
        for idx in range(start, min(end, doc.page_count)):
            t0 = time.perf_counter()
            number = idx + 1
            try:
                result = _process_page(doc, idx, config)
            except ProcessingError as exc:
                result = PageResult(number=number, label=f"Page {number}",
                                    method=ExtractionMethod.NONE,
                                    issues=[Issue.error(exc.code, exc.message, number)])
            except Exception as exc:  # damaged page object, renderer error, ...
                result = PageResult(number=number, label=f"Page {number}",
                                    method=ExtractionMethod.NONE,
                                    issues=[Issue.error(IssueCode.PAGE_FAILED,
                                                        f"{type(exc).__name__}: {exc}", number)])
            result.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            results.append(result)
    return results


def garbage_ratio(text: str) -> float:
    """Share of characters that indicate a broken font encoding."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(
        1 for c in chars
        if c == "�" or unicodedata.category(c) in ("Co", "Cc", "Cs", "Cn")
    )
    return bad / len(chars)


def image_coverage(page: pymupdf.Page) -> tuple[float, float | None]:
    """(fraction of the page covered by images, native DPI of the largest image)."""
    page_area = abs(page.rect) or 1.0
    covered = 0.0
    largest, native_dpi = 0.0, None
    for info in page.get_image_info():
        bbox = pymupdf.Rect(info["bbox"]) & page.rect
        area = abs(bbox)
        covered += area
        if area > largest and bbox.width > 0:
            largest = area
            native_dpi = info.get("width", 0) / (bbox.width / 72.0)
    return min(1.0, covered / page_area), native_dpi


# Many PDF producers encode the printed "-" as a soft hyphen (U+00AD, WinAnsi 0xAD) or
# as one of the Unicode hyphens; read literally, "INV-2026-0042" would become
# "INV­2026­0042" and match nothing downstream. What is printed is a hyphen.
_HYPHENS = str.maketrans({"­": "-", "‐": "-", "‑": "-", "‒": "-", "−": "-"})


def canonical_text(text: str) -> str:
    return text.translate(_HYPHENS)


def _process_page(doc: pymupdf.Document, idx: int, config: ProcessingConfig) -> PageResult:
    page = doc.load_page(idx)
    number = idx + 1
    digital = canonical_text(page.get_text("text", sort=True)).strip()
    chars = len(digital)
    garbage = garbage_ratio(digital)
    coverage, native_dpi = image_coverage(page)

    sparse_over_image = coverage >= config.hybrid_image_coverage and chars < config.hybrid_max_chars
    needs_ocr = chars < config.min_text_chars or garbage > config.max_garbage_ratio or sparse_over_image
    extra = {
        "digital_chars": chars,
        "garbage_ratio": round(garbage, 3),
        "image_coverage": round(coverage, 3),
        "rotation": page.rotation,
    }

    if not needs_ocr:
        tables = _extract_tables(page) if config.extract_pdf_tables else []
        return PageResult(number=number, label=f"Page {number}", method=ExtractionMethod.DIGITAL,
                          text=digital, char_count=chars, tables=tables, extra=extra)

    issues: list[Issue] = []
    if coverage == 0 and chars == 0 and not page.get_drawings():
        # Genuinely blank page (separator sheets are common in scanned batches).
        return PageResult(number=number, label=f"Page {number}", method=ExtractionMethod.NONE,
                          extra={**extra, "blank": True})

    # Render scans at their native resolution: upsampling a 200-DPI scan to 300 DPI
    # adds no information but ~2x OCR time (low-DPI scans are upscaled later, in
    # preprocessing, where the factor is chosen deliberately).
    dpi = config.ocr_dpi
    if native_dpi and coverage >= 0.5:
        dpi = int(min(config.ocr_dpi, max(150, native_dpi)))
    image, dpi = render_page(page, config, dpi)
    try:
        ocr = ocr_image(image, dpi, config, page=number)
    except ProcessingError as exc:
        if chars and garbage <= config.max_garbage_ratio:
            # OCR unavailable/failed, but a sparse text layer is still better than nothing.
            issues.append(Issue.warning(exc.code, exc.message, number))
            return PageResult(number=number, label=f"Page {number}", method=ExtractionMethod.DIGITAL,
                              text=digital, char_count=chars, issues=issues, extra=extra)
        raise

    issues.extend(ocr.issues)
    if native_dpi and native_dpi < 150 and coverage > 0.5:
        ocr.quality.reasons.append(f"embedded scan is {native_dpi:.0f} DPI")
        ocr.quality.is_low_quality = True
        if not any(i.code == IssueCode.LOW_IMAGE_QUALITY for i in issues):
            issues.append(Issue.warning(IssueCode.LOW_IMAGE_QUALITY,
                                        f"Embedded scan is only {native_dpi:.0f} DPI", number))
    extra["source_image_dpi"] = round(native_dpi, 1) if native_dpi else None

    text, method = ocr.text.strip(), ExtractionMethod.OCR
    if digital and garbage <= config.max_garbage_ratio:
        method = ExtractionMethod.HYBRID
        if digital not in text:
            text = f"{text}\n{digital}".strip()
    return PageResult(number=number, label=f"Page {number}", method=method, text=text,
                      char_count=len(text), ocr=ocr.info, quality=ocr.quality,
                      issues=issues, extra=extra)


def render_page(page: pymupdf.Page, config: ProcessingConfig,
                dpi: int | None = None) -> tuple[np.ndarray, int]:
    """Render a page to an 8-bit grayscale array, lowering DPI for oversized pages."""
    w_in, h_in = page.rect.width / 72.0, page.rect.height / 72.0
    dpi = dpi or config.ocr_dpi
    if w_in * h_in * dpi * dpi > config.max_render_pixels:
        dpi = max(72, int(math.sqrt(config.max_render_pixels / (w_in * h_in))))
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(pix.h, pix.stride)
    return arr[:, : pix.w * pix.n].copy(), dpi


def _extract_tables(page: pymupdf.Page) -> list[Table]:
    tables = []
    try:
        found = page.find_tables()
    except Exception:
        return tables
    for i, t in enumerate(found.tables, 1):
        rows = [[(c or "").strip() or None for c in r] for r in t.extract()]
        if not rows:
            continue
        # Ruling lines often create empty spacer columns; drop them.
        keep = [j for j in range(len(rows[0])) if any(r[j] for r in rows if j < len(r))]
        rows = [[r[j] if j < len(r) else None for j in keep] for r in rows]
        header = [str(c or f"column_{j + 1}") for j, c in enumerate(rows[0])]
        body = rows[1:]
        tables.append(Table(name=f"Table {i}", header=header, rows=body,
                            n_rows=len(body), n_cols=len(keep), header_row_index=0))
    return tables
