import cv2
import factory
import numpy as np
import pytest
from conftest import ENGINES, codes, found_tokens, requires_ocr

from docintel.processing import ExtractionMethod, ProcessingConfig, process_file
from docintel.processing.models import DocumentStatus
from docintel.processing.ocr import preprocess as pp
from docintel.processing.ocr.engines import OCRLine, _merge_into_rows, get_engine
from docintel.processing.processors.image import to_gray_array

pytestmark = requires_ocr


@pytest.mark.parametrize("engine", ENGINES)
def test_each_engine_reads_clean_png(tmp_path, engine):
    cfg = ProcessingConfig(ocr_engine=engine, workers=1)
    doc = process_file(factory.png_invoice(tmp_path / "inv.png"), cfg)
    assert doc.status is DocumentStatus.SUCCESS
    page = doc.pages[0]
    assert page.method is ExtractionMethod.OCR
    assert page.ocr.engine == engine
    assert len(found_tokens(page.text)) == len(factory.KEY_TOKENS)
    assert not page.quality.is_low_quality


def test_poor_quality_photo(tmp_path, config):
    doc = process_file(factory.poor_quality_jpeg(tmp_path / "photo.jpg"), config)
    page = doc.pages[0]
    assert doc.status is DocumentStatus.SUCCESS
    assert page.quality.is_low_quality
    assert abs(page.quality.skew_angle - 4) < 1.5  # skew detected...
    assert len(found_tokens(page.text)) >= 4  # ...and corrected enough to read
    # Deskew must straighten rows: each line item keeps its amount on the same line.
    lines = page.text.splitlines()
    assert any("Widget" in ln and "125" in ln for ln in lines), page.text
    assert any("Gadget" in ln and "80" in ln for ln in lines), page.text


@pytest.mark.parametrize("cv_rotation,expected", [
    (cv2.ROTATE_90_CLOCKWISE, 90),
    (cv2.ROTATE_90_COUNTERCLOCKWISE, 270),
    (cv2.ROTATE_180, 180),
])
def test_rotated_scan_is_corrected(tmp_path, config, cv_rotation, expected):
    doc = process_file(factory.rotated_png(tmp_path / "rot.png", cv_rotation), config)
    page = doc.pages[0]
    assert page.ocr.rotation == expected
    assert len(found_tokens(page.text)) >= 5
    # Reading order restored: the header comes before the totals.
    assert page.text.index("ACME") < page.text.index("Total")


def test_transparent_png_composited_on_white(tmp_path, config):
    doc = process_file(factory.transparent_png(tmp_path / "t.png"), config)
    assert len(found_tokens(doc.pages[0].text)) >= 5


def test_multipage_tiff(tmp_path, config):
    doc = process_file(factory.multipage_tiff(tmp_path / "fax.tiff", frames=3), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert [p.label for p in doc.pages] == ["Frame 1", "Frame 2", "Frame 3"]
    for i, p in enumerate(doc.pages):
        assert f"INV-2024-{100 + i:04d}" in p.text.replace(" ", "")


def test_tiff_frames_chunked(tmp_path):
    cfg = ProcessingConfig(ocr_chunk_pages=2, workers=1)
    from docintel.processing.pipeline import plan_document

    plan = plan_document(factory.multipage_tiff(tmp_path / "fax.tiff", frames=5), cfg)
    assert [(u.start, u.end) for u in plan.units] == [(0, 2), (2, 4), (4, 5)]


def test_16bit_tiff(tmp_path, config):
    doc = process_file(factory.sixteen_bit_tiff(tmp_path / "deep.tif"), config)
    assert len(found_tokens(doc.pages[0].text)) >= 5


def test_truncated_jpeg_recovers_partial_text(tmp_path, config):
    doc = process_file(factory.truncated_jpeg(tmp_path / "cut.jpg"), config)
    assert "TRUNCATED" in codes(doc)
    assert doc.needs_review
    assert doc.status is DocumentStatus.SUCCESS
    assert "INV-2024-0042" in doc.pages[0].text.replace(" ", "")  # top of page survived


def test_blank_image_reports_no_text(tmp_path, config):
    doc = process_file(factory.blank_png(tmp_path / "blank.png"), config)
    assert "NO_TEXT_FOUND" in codes(doc)
    assert doc.needs_review
    assert doc.pages[0].ocr.attempts == 0  # blank detected before any OCR pass


# ---- unit tests for the building blocks -------------------------------------------


@pytest.mark.parametrize("angle", [-6, -2.5, 3, 7])
def test_skew_estimation(angle):
    img = factory.degrade(factory.render_invoice(200), angle=angle)
    assert abs(pp.estimate_skew(img) - angle) < 0.8


def test_quality_metrics_separate_clean_from_degraded():
    clean = factory.render_invoice(300)
    assert not pp.assess_quality(clean, 300).is_low_quality
    blurry = pp.assess_quality(factory.degrade(clean, blur=2.5), 300)
    assert any("blurry" in r for r in blurry.reasons)
    faint = pp.assess_quality(factory.degrade(clean, contrast=0.25), 300)
    assert any("contrast" in r for r in faint.reasons)
    noisy = pp.assess_quality(factory.degrade(clean, noise=30), 300)
    assert any("noisy" in r for r in noisy.reasons)
    assert any("resolution" in r for r in pp.assess_quality(clean, 100).reasons)


def test_upscale_is_bounded():
    assert pp.upscale_factor(np.zeros((500, 400), np.uint8), None) > 1
    assert pp.upscale_factor(np.zeros((3000, 2400), np.uint8), 300) == 1
    huge = np.zeros((6000, 6000), np.uint8)
    assert pp.upscale_factor(huge, 72) * 6000 <= 40_000_000 ** 0.5 + 1


def test_rows_merge_and_keep_column_gaps():
    boxes = [OCRLine("Total", 0.9, (500, 100, 560, 120)),
             OCRLine("Widget", 0.9, (10, 52, 80, 70)),
             OCRLine("125.00", 0.9, (500, 50, 570, 68)),
             OCRLine("Subtotal:", 0.9, (10, 101, 100, 119))]
    rows = _merge_into_rows(boxes)
    assert [r.text.split()[0] for r in rows] == ["Widget", "Subtotal:"]
    assert "    " in rows[0].text


def test_gray_conversion_modes():
    from PIL import Image

    assert to_gray_array(Image.new("RGBA", (4, 4), (0, 0, 0, 0))).min() == 255
    assert to_gray_array(Image.new("1", (4, 4), 1)).max() == 255
    assert to_gray_array(Image.new("CMYK", (4, 4))).dtype == np.uint8


def test_engine_cached_per_process():
    assert get_engine("auto") is get_engine("auto")
