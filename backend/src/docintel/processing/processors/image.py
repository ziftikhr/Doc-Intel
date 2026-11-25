"""JPG / PNG / TIFF processing (TIFF may hold hundreds of frames, e.g. fax batches)."""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

from ..config import ProcessingConfig
from ..errors import ProcessingError
from ..models import ExtractionMethod, Issue, IssueCode, PageResult
from ..ocr import ocr_image

# We enforce our own (configurable) pixel limit before decoding anything.
Image.MAX_IMAGE_PIXELS = None


@contextmanager
def _open(path: Path, config: ProcessingConfig):
    try:
        img = Image.open(path)
    except UnidentifiedImageError as exc:
        raise ProcessingError(IssueCode.CORRUPTED, "Image data is unreadable or corrupted") from exc
    except OSError as exc:
        raise ProcessingError(IssueCode.CORRUPTED, f"Cannot open image: {exc}") from exc
    try:
        w, h = img.size
        if w <= 0 or h <= 0:
            raise ProcessingError(IssueCode.CORRUPTED, f"Invalid image dimensions {w}x{h}")
        if w * h > config.max_image_pixels:
            raise ProcessingError(
                IssueCode.DECOMPRESSION_BOMB,
                f"Image is {w}x{h} = {w * h / 1e6:.0f} MP (limit {config.max_image_pixels / 1e6:.0f} MP)",
            )
        yield img
    finally:
        img.close()


def inspect_image(path: Path, config: ProcessingConfig) -> tuple[int, dict, list[Issue]]:
    with _open(path, config) as img:
        frames = getattr(img, "n_frames", 1) or 1
        if frames > config.max_pages:
            raise ProcessingError(IssueCode.TOO_MANY_PAGES,
                                  f"Image has {frames} frames (limit {config.max_pages})")
        props = {"format": img.format, "mode": img.mode, "width": img.width,
                 "height": img.height, "frames": frames}
        dpi = _dpi(img)
        if dpi:
            props["dpi"] = dpi
    return frames, props, []


def process_image_frames(path: Path, start: int, end: int, config: ProcessingConfig) -> list[PageResult]:
    results = []
    with _open(path, config) as img:
        frames = getattr(img, "n_frames", 1) or 1
        multi = frames > 1
        for idx in range(start, min(end, frames)):
            t0 = time.perf_counter()
            number = idx + 1
            label = f"Frame {number}" if multi else "Image"
            try:
                result = _process_frame(path, img, idx, config, label)
            except ProcessingError as exc:
                result = PageResult(number=number, label=label, method=ExtractionMethod.NONE,
                                    issues=[Issue.error(exc.code, exc.message, number)])
            except Exception as exc:
                result = PageResult(number=number, label=label, method=ExtractionMethod.NONE,
                                    issues=[Issue.error(IssueCode.PAGE_FAILED,
                                                        f"{type(exc).__name__}: {exc}", number)])
            result.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            results.append(result)
    return results


def _process_frame(path: Path, img: Image.Image, idx: int, config: ProcessingConfig,
                   label: str) -> PageResult:
    number = idx + 1
    issues: list[Issue] = []
    try:
        frame = _load_frame(img, idx)
    except OSError as exc:
        # Truncated download / interrupted scan: decode what is there and flag it.
        frame = _load_truncated(path, idx, config)
        issues.append(Issue.warning(IssueCode.TRUNCATED,
                                    f"Image data is incomplete ({exc}); decoded the readable part",
                                    number))

    dpi = _dpi(frame) or _dpi(img)
    gray = to_gray_array(frame)
    gray = _limit_pixels(gray, config.max_render_pixels)

    ocr = ocr_image(gray, dpi, config, page=number)
    issues.extend(ocr.issues)
    text = ocr.text.strip()
    if not text:
        issues.append(Issue.warning(IssueCode.NO_TEXT_FOUND, "No text detected in image", number))
    return PageResult(number=number, label=label,
                      method=ExtractionMethod.OCR if text else ExtractionMethod.NONE,
                      text=text, char_count=len(text), ocr=ocr.info, quality=ocr.quality,
                      issues=issues, extra={"mode": frame.mode})


def _load_frame(img: Image.Image, idx: int) -> Image.Image:
    img.seek(idx)
    frame = img.copy()  # forces a full decode of this frame
    if idx == 0:
        frame = ImageOps.exif_transpose(frame)  # phone photos are often stored sideways
    return frame


def _load_truncated(path: Path, idx: int, config: ProcessingConfig) -> Image.Image:
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with _open(path, config) as img:
            return _load_frame(img, idx)
    except OSError as exc:
        raise ProcessingError(IssueCode.CORRUPTED, f"Image data cannot be decoded: {exc}") from exc
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous


def _dpi(img: Image.Image) -> float | None:
    dpi = img.info.get("dpi")
    if not dpi:
        return None
    try:
        value = float(dpi[0] if isinstance(dpi, (tuple, list)) else dpi)
    except (TypeError, ValueError):
        return None
    if value < 30:
        return None  # JFIF "aspect ratio only" files report 1x1
    if max(img.size) / value > 20:
        return None  # e.g. a 4000px phone photo tagged 72 DPI - metadata is meaningless
    return round(value, 1)


def to_gray_array(frame: Image.Image) -> np.ndarray:
    """Any PIL mode -> uint8 grayscale, compositing transparency onto white."""
    mode = frame.mode
    if mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in frame.info):
        rgba = frame.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        canvas.alpha_composite(rgba)
        return np.asarray(canvas.convert("L"))
    if mode.startswith("I") or mode == "F":  # 16/32-bit scans
        arr = np.asarray(frame, dtype=np.float32)
        lo, hi = float(arr.min()), float(arr.max())
        if hi <= lo:
            return np.full(arr.shape, 255, dtype=np.uint8)
        return ((arr - lo) * (255.0 / (hi - lo))).astype(np.uint8)
    return np.asarray(frame.convert("L"))


def _limit_pixels(gray: np.ndarray, max_pixels: int) -> np.ndarray:
    h, w = gray.shape
    if h * w <= max_pixels:
        return gray
    import cv2

    f = math.sqrt(max_pixels / (h * w))
    return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
