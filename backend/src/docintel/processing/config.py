"""Processing configuration. Every limit can be overridden with a DOCINTEL_* env var."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class ProcessingConfig:
    # --- Input safety limits -------------------------------------------------------
    max_file_size_mb: int = 1024
    max_pages: int = 20_000
    # Pixel cap for any single decoded image (decompression-bomb guard).
    max_image_pixels: int = 200_000_000
    # XLSX files are ZIP archives: cap total uncompressed size and compression ratio.
    max_zip_uncompressed_mb: int = 2048
    max_zip_ratio: int = 250

    # --- Work splitting -------------------------------------------------------------
    # Large PDFs / multi-frame TIFFs are split into page ranges so a single
    # 1,000-page scan is spread across all workers instead of pinning one core.
    chunk_pages: int = 25
    # OCR costs ~1-4 s/page, so OCR-heavy documents get small units for even load
    # balancing (a 100-page scan in 25-page units would occupy only 4 workers).
    ocr_chunk_pages: int = 2
    workers: int = max(1, (os.cpu_count() or 2) - 1)

    # --- PDF text-layer classification ---------------------------------------------
    # A page with fewer extractable characters than this is treated as scanned.
    min_text_chars: int = 30
    # Text layers with more broken glyphs than this ratio (bad font encodings) are OCR'd.
    max_garbage_ratio: float = 0.25
    # Pages mostly covered by an image but carrying only a little digital text
    # (e.g. a scan with a digital page stamp) are OCR'd and merged ("hybrid").
    hybrid_image_coverage: float = 0.5
    hybrid_max_chars: int = 200
    extract_pdf_tables: bool = False

    # --- OCR ------------------------------------------------------------------------
    ocr_engine: str = "auto"  # auto | tesseract | rapidocr | none
    ocr_languages: str = "eng"  # Tesseract language codes, e.g. "eng+deu"
    ocr_dpi: int = 300
    # Pages larger than this (posters, drawings) are rendered at a lower DPI.
    max_render_pixels: int = 50_000_000
    # Below this mean confidence (0-1) the page is retried with heavier preprocessing.
    ocr_retry_confidence: float = 0.75
    # Below this the page is flagged LOW_OCR_CONFIDENCE for manual review.
    ocr_review_confidence: float = 0.60
    ocr_page_timeout_s: int = 180
    ocr_try_rotations: bool = True

    # --- Spreadsheets ----------------------------------------------------------------
    max_rows_per_sheet: int = 1_000_000
    # Workbooks up to this size are loaded fully so merged cells can be filled in;
    # bigger ones are streamed in read-only mode to keep memory flat.
    xlsx_full_load_max_mb: int = 20
    max_sheet_text_chars: int = 5_000_000

    @classmethod
    def from_env(cls, **overrides) -> "ProcessingConfig":
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f"DOCINTEL_{f.name.upper()}")
            if raw is None:
                continue
            if f.type in ("bool", bool):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif f.type in ("int", int):
                values[f.name] = int(raw)
            elif f.type in ("float", float):
                values[f.name] = float(raw)
            else:
                values[f.name] = raw
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def with_overrides(self, **overrides) -> "ProcessingConfig":
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})
