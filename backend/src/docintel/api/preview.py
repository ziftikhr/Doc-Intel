"""Page images for the review screen.

Browsers cannot display TIFF (or 16-bit scans), and a reviewer needs to jump to the
page a value was read from, so every PDF page / image frame is rendered to PNG here.
"""

from __future__ import annotations

import io
import mimetypes
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

from docintel.processing.processors.image import to_gray_array

from .services import ServiceError

MAX_WIDTH = 2000
PREVIEWABLE = ("pdf", "png", "jpeg", "tiff")


def media_type(filename: str, detected_type: str | None) -> str:
    by_type = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg", "tiff": "image/tiff",
               "csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "xls": "application/vnd.ms-excel"}
    return by_type.get(detected_type or "") or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def render_page(path: str | Path, file_type: str | None, number: int, width: int = 1200) -> bytes:
    width = max(200, min(width, MAX_WIDTH))
    if file_type == "pdf":
        return _pdf_page(Path(path), number, width)
    if file_type in ("png", "jpeg", "tiff"):
        return _image_frame(Path(path), number, width)
    raise ServiceError(404, "this document type has no page images (spreadsheets are shown as text)")


def _pdf_page(path: Path, number: int, width: int) -> bytes:
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001
        raise ServiceError(404, f"the PDF cannot be rendered ({type(e).__name__})") from e
    with doc:
        if doc.needs_pass:
            raise ServiceError(404, "the PDF is password-protected")
        if not 1 <= number <= doc.page_count:
            raise ServiceError(404, f"page {number} does not exist (the document has {doc.page_count})")
        page = doc[number - 1]
        zoom = width / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


def _image_frame(path: Path, number: int, width: int) -> bytes:
    try:
        return _render_frame(path, number, width)
    except OSError:
        # truncated files: show what is there (same tolerance as Task 1)
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            return _render_frame(path, number, width)
        except OSError as e:
            raise ServiceError(404, f"the image cannot be rendered ({e})") from e
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous


def _render_frame(path: Path, number: int, width: int) -> bytes:
    with Image.open(path) as img:
        frames = getattr(img, "n_frames", 1)
        if not 1 <= number <= frames:
            raise ServiceError(404, f"page {number} does not exist (the image has {frames})")
        img.seek(number - 1)
        frame = ImageOps.exif_transpose(img.copy())
    if frame.mode.startswith("I") or frame.mode == "F" or frame.mode in ("RGBA", "LA", "PA") or (
            frame.mode == "P" and "transparency" in frame.info):
        frame = Image.fromarray(to_gray_array(frame))  # 16-bit / transparency -> white paper
    else:
        frame = frame.convert("RGB")
    if frame.width > width:
        frame = frame.resize((width, round(frame.height * width / frame.width)), Image.LANCZOS)
    out = io.BytesIO()
    frame.save(out, format="PNG", optimize=True)
    return out.getvalue()
