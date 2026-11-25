"""Adaptive OCR: assess quality -> light preprocessing -> fix orientation -> retry
harder only if needed. Clean pages cost exactly one OCR pass."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..config import ProcessingConfig
from ..errors import ProcessingError
from ..models import ImageQuality, Issue, IssueCode, OCRInfo
from . import preprocess as pp
from .engines import OCRResult, get_engine

# Fewer ink pixels than this (~90 px on a 2500px-scaled page) means there is no text.
BLANK_INK_RATIO = 2e-5


@dataclass
class PageOCR:
    text: str
    info: OCRInfo
    quality: ImageQuality
    issues: list[Issue]


def _score(r: OCRResult) -> float:
    # Prefer confident results, but never a confident empty one over real text;
    # sideways reads (vertical boxes) are heavily penalised.
    return r.confidence * math.log1p(r.char_count) * (1.0 - 0.8 * r.vertical_ratio)


class _Attempts:
    def __init__(self, engine, gray: np.ndarray, dpi: float | None, skew: float, timeout: int):
        self.engine, self.gray, self.dpi, self.timeout = engine, gray, dpi, timeout
        self.skews = {0: skew}
        self.count = 0
        self.best: OCRResult | None = None
        self.best_key = ("light", 0)

    def run(self, mode: str, rotation: int) -> OCRResult:
        img = pp.rotate_quadrant(self.gray, rotation) if rotation else self.gray
        if rotation not in self.skews:
            self.skews[rotation] = pp.estimate_skew(img)
        prep = pp.prepare_light if mode == "light" else pp.prepare_aggressive
        result = self.engine.recognize(prep(img, self.dpi, self.skews[rotation]), self.timeout)
        self.count += 1
        if self.best is None or _score(result) > _score(self.best) * (1.0 if rotation == self.best_key[1] else 1.15):
            self.best, self.best_key = result, (mode, rotation)
        return result


def ocr_image(image: np.ndarray, dpi: float | None, config: ProcessingConfig,
              page: int | None = None) -> PageOCR:
    engine = get_engine(config.ocr_engine, config.ocr_languages)
    if engine is None:
        raise ProcessingError(
            IssueCode.OCR_UNAVAILABLE,
            "Page needs OCR but no OCR engine is installed (install Tesseract or `pip install .[rapidocr]`)",
        )

    gray = pp.to_gray(image)
    quality = pp.assess_quality(gray, dpi)
    issues: list[Issue] = []
    if quality.is_low_quality:
        issues.append(Issue.warning(IssueCode.LOW_IMAGE_QUALITY, "; ".join(quality.reasons), page))

    if quality.ink_ratio < BLANK_INK_RATIO:
        # Blank separator sheet / empty photo: nothing to read, skip the OCR passes.
        info = OCRInfo(engine=engine.name, confidence=0.0, preprocessing="none",
                       attempts=0, dpi=int(dpi) if dpi else None)
        return PageOCR("", info, quality, issues)

    good = config.ocr_retry_confidence
    a = _Attempts(engine, gray, dpi, quality.skew_angle, config.ocr_page_timeout_s)
    first = a.run("light", 0)

    if config.ocr_try_rotations:
        if first.vertical_ratio > 0.5:
            # Text lines run vertically: the page was scanned sideways.
            a.run("light", 90)
            a.run("light", 270)
        elif first.confidence < good and first.char_count >= 10:
            a.run("light", 180)  # upside-down pages read as low-confidence garbage

    rotation = a.best_key[1]
    if a.best.confidence < good or a.best.char_count < 10:
        a.run("aggressive", rotation)

    if config.ocr_try_rotations and (a.best.confidence < 0.5 or a.best.char_count < 10):
        for degrees in (90, 270, 180):  # last resort: nothing readable yet
            if degrees != rotation:
                a.run("light", degrees)
            if a.best.confidence >= good and a.best.char_count >= 10:
                break

    best = a.best
    if best.char_count and best.confidence < config.ocr_review_confidence:
        issues.append(Issue.warning(
            IssueCode.LOW_OCR_CONFIDENCE,
            f"Mean OCR confidence {best.confidence:.2f} is below {config.ocr_review_confidence:.2f}",
            page,
        ))

    info = OCRInfo(
        engine=best.engine,
        confidence=round(min(1.0, max(0.0, best.confidence)), 4),
        preprocessing=a.best_key[0],
        rotation=a.best_key[1],
        attempts=a.count,
        dpi=int(dpi) if dpi else None,
    )
    return PageOCR(best.text, info, quality, issues)
