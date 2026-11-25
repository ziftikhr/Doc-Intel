"""Pluggable OCR engines.

* Tesseract - the production default (installed in the Docker image), multi-language.
* RapidOCR  - PaddleOCR models on ONNX Runtime; pure pip install, used when the
              Tesseract binary is absent (e.g. a Windows dev box without admin rights).

Engines are created once per worker process and cached: model loading is far more
expensive than a single page.
"""

from __future__ import annotations

import logging
import os
import shutil
import statistics
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np

from ..errors import ProcessingError
from ..models import IssueCode

log = logging.getLogger(__name__)


@dataclass
class OCRLine:
    text: str
    confidence: float  # 0-1
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1


@dataclass
class OCRResult:
    text: str
    confidence: float  # character-weighted mean, 0-1
    engine: str
    lines: list[OCRLine] = field(default_factory=list)
    # Share of text found in tall, narrow boxes: ~1.0 means the page is sideways.
    vertical_ratio: float = 0.0

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


class OCREngine(Protocol):
    name: str

    def recognize(self, image: np.ndarray, timeout_s: int) -> OCRResult:
        """``image`` is a uint8 grayscale (H, W) or RGB (H, W, 3) array."""


def weighted_confidence(lines: list[OCRLine]) -> float:
    total = sum(len(line.text) for line in lines)
    if not total:
        return 0.0
    return sum(line.confidence * len(line.text) for line in lines) / total


def vertical_ratio(boxes: list[OCRLine]) -> float:
    total = sum(len(b.text) for b in boxes)
    if not total:
        return 0.0
    vertical = sum(len(b.text) for b in boxes
                   if len(b.text) >= 3 and (b.bbox[3] - b.bbox[1]) > 1.2 * (b.bbox[2] - b.bbox[0]))
    return vertical / total


# ----------------------------------------------------------------------------------
# Tesseract
# ----------------------------------------------------------------------------------

_WINDOWS_TESSERACT = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
]


def find_tesseract() -> str | None:
    explicit = os.environ.get("TESSERACT_CMD")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _WINDOWS_TESSERACT:
        if candidate.exists():
            return str(candidate)
    return None


class TesseractEngine:
    name = "tesseract"

    def __init__(self, languages: str = "eng"):
        import pytesseract

        cmd = find_tesseract()
        if not cmd:
            raise RuntimeError("tesseract binary not found")
        pytesseract.pytesseract.tesseract_cmd = cmd
        self._pt = pytesseract
        self.languages = languages
        self.version = str(pytesseract.get_tesseract_version())

    def recognize(self, image: np.ndarray, timeout_s: int) -> OCRResult:
        # psm 3 = automatic page segmentation; preserve_interword_spaces keeps
        # column alignment of invoice tables in the text output.
        cfg = "--oem 1 --psm 3 -c preserve_interword_spaces=1"
        try:
            data = self._pt.image_to_data(
                image, lang=self.languages, config=cfg,
                output_type=self._pt.Output.DICT, timeout=timeout_s,
            )
        except RuntimeError as exc:  # pytesseract signals timeouts with RuntimeError
            if "timeout" in str(exc).lower():
                raise ProcessingError(IssueCode.OCR_TIMEOUT, f"Tesseract exceeded {timeout_s}s") from exc
            raise

        groups: dict[tuple[int, int, int], list[int]] = {}
        for i, word in enumerate(data["text"]):
            if word.strip() and float(data["conf"][i]) >= 0:
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                groups.setdefault(key, []).append(i)

        lines: list[OCRLine] = []
        for idxs in groups.values():
            words = [data["text"][i] for i in idxs]
            confs = [float(data["conf"][i]) / 100.0 for i in idxs]
            x0 = min(data["left"][i] for i in idxs)
            y0 = min(data["top"][i] for i in idxs)
            x1 = max(data["left"][i] + data["width"][i] for i in idxs)
            y1 = max(data["top"][i] + data["height"][i] for i in idxs)
            lines.append(OCRLine(" ".join(words), statistics.fmean(confs), (x0, y0, x1, y1)))

        lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        return OCRResult(
            text="\n".join(ln.text for ln in lines),
            confidence=weighted_confidence(lines),
            engine=self.name,
            lines=lines,
            vertical_ratio=vertical_ratio(lines),
        )


# ----------------------------------------------------------------------------------
# RapidOCR
# ----------------------------------------------------------------------------------


class RapidOCREngine:
    name = "rapidocr"

    def __init__(self, threads: int = 1):
        from rapidocr import RapidOCR

        self._ocr = RapidOCR(
            params={
                "Global.log_level": "error",
                # Detection input size. Benchmarked on 7-14pt invoice text at 300 DPI:
                # 2500px found 23/24 lines in 3.0s, 4000px 21/24 in 4.5s, 2000px 23/24
                # with lower confidence. Recognition always runs on full-res crops.
                "Global.max_side_len": 2500,
                "Global.text_score": 0.3,
                # The per-line 180-degree classifier makes an upside-down page read
                # "successfully" (0.97 confidence) in reversed line order - a silent
                # error. Without it such pages score low and the runner rotates them.
                "Global.use_cls": False,
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
        )
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)

    def recognize(self, image: np.ndarray, timeout_s: int) -> OCRResult:
        if image.ndim == 2:
            image = np.stack([image] * 3, axis=-1)
        out = self._ocr(image)
        if out.boxes is None or not out.txts:
            return OCRResult("", 0.0, self.name)

        items = []
        for box, txt, score in zip(out.boxes, out.txts, out.scores):
            pts = np.asarray(box)
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            items.append(OCRLine(str(txt), float(score), (int(x0), int(y0), int(x1), int(y1))))
        lines = _merge_into_rows(items)
        return OCRResult(
            text="\n".join(ln.text for ln in lines),
            confidence=weighted_confidence(items),
            engine=self.name,
            lines=lines,
            vertical_ratio=vertical_ratio(items),
        )


def _merge_into_rows(boxes: list[OCRLine]) -> list[OCRLine]:
    """Group detected text boxes into visual rows, left to right.

    Large horizontal gaps are kept as runs of spaces so that the column layout of
    invoice line-item tables survives into the plain text.
    """
    if not boxes:
        return []
    heights = [b.bbox[3] - b.bbox[1] for b in boxes]
    tol = max(4.0, statistics.median(heights) * 0.5)
    rows: list[list[OCRLine]] = []
    for b in sorted(boxes, key=lambda b: (b.bbox[1] + b.bbox[3]) / 2):
        yc = (b.bbox[1] + b.bbox[3]) / 2
        if rows:
            last = rows[-1]
            ryc = statistics.fmean((x.bbox[1] + x.bbox[3]) / 2 for x in last)
            if abs(yc - ryc) <= tol:
                last.append(b)
                continue
        rows.append([b])

    merged = []
    for row in rows:
        row.sort(key=lambda b: b.bbox[0])
        parts = [row[0].text]
        for prev, cur in zip(row, row[1:]):
            char_w = max(1.0, (prev.bbox[2] - prev.bbox[0]) / max(1, len(prev.text)))
            gap = cur.bbox[0] - prev.bbox[2]
            parts.append("    " if gap > 3 * char_w else " ")
            parts.append(cur.text)
        merged.append(
            OCRLine(
                "".join(parts),
                weighted_confidence(row),
                (min(b.bbox[0] for b in row), min(b.bbox[1] for b in row),
                 max(b.bbox[2] for b in row), max(b.bbox[3] for b in row)),
            )
        )
    return merged


# ----------------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------------


def ocr_threads() -> int:
    """Threads per OCR call. Batch workers set DOCINTEL_OCR_THREADS so that
    workers x threads ~= cores; a single interactive process may use every core."""
    raw = os.environ.get("DOCINTEL_OCR_THREADS")
    return max(1, int(raw)) if raw else min(8, os.cpu_count() or 1)


@lru_cache(maxsize=4)
def get_engine(name: str = "auto", languages: str = "eng") -> OCREngine | None:
    """Return a cached engine, or ``None`` if no OCR engine is usable."""
    name = name.lower()
    if name == "none":
        return None
    order = ["tesseract", "rapidocr"] if name == "auto" else [name]
    for candidate in order:
        try:
            if candidate == "tesseract":
                return TesseractEngine(languages)
            if candidate == "rapidocr":
                return RapidOCREngine(threads=ocr_threads())
            raise ValueError(f"unknown OCR engine {candidate!r}")
        except ValueError:
            raise
        except Exception as exc:  # engine not installed / failed to load
            log.info("OCR engine %s unavailable: %s", candidate, exc)
    return None


def available_engines() -> list[str]:
    names = []
    if find_tesseract():
        names.append("tesseract")
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401

        names.append("rapidocr")
    except ImportError:
        pass
    return names
