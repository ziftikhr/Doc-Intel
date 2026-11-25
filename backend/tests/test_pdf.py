from pathlib import Path

import factory
import pymupdf
from conftest import codes, found_tokens, requires_ocr

from docintel.processing import ExtractionMethod, ProcessingConfig, process_file
from docintel.processing.models import DocumentStatus
from docintel.processing.processors.pdf import garbage_ratio


def test_digital_pdf_multi_page(tmp_path, config):
    doc = process_file(factory.digital_pdf(tmp_path / "inv.pdf", pages=3), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert doc.metadata.page_count == 3
    assert [p.number for p in doc.pages] == [1, 2, 3]
    assert all(p.method is ExtractionMethod.DIGITAL for p in doc.pages)
    assert "INV-2024-0042" in doc.pages[0].text
    assert "INV-2024-0044" in doc.pages[2].text
    assert doc.stats.pages_digital == 3
    assert not doc.needs_review
    assert len(doc.document_id) == 64


def test_digital_pdf_needs_no_ocr_engine(tmp_path):
    cfg = ProcessingConfig(ocr_engine="none", workers=1)
    doc = process_file(factory.digital_pdf(tmp_path / "inv.pdf"), cfg)
    assert doc.status is DocumentStatus.SUCCESS


def test_page_chunking_preserves_order(tmp_path):
    cfg = ProcessingConfig(chunk_pages=7, workers=1)
    doc = process_file(factory.large_digital_pdf(tmp_path / "big.pdf", 30), cfg)
    assert [p.number for p in doc.pages] == list(range(1, 31))
    for p in doc.pages:
        assert f"PAGE-MARKER-{p.number:05d}" in p.text


def test_optional_table_extraction(tmp_path):
    import pymupdf

    path = tmp_path / "table.pdf"
    d = pymupdf.open()
    page = d.new_page()
    y = 100
    for row in [["Item", "Qty", "Amount"], ["Widget", "10", "125.00"], ["Gadget", "2", "80.00"]]:
        for c, val in enumerate(row):
            page.insert_text((80 + c * 150, y), val, fontsize=11)
        y += 30
    for i in range(4):
        page.draw_line((70, 82 + i * 30), (520, 82 + i * 30))
    for x in (70, 220, 370, 520):
        page.draw_line((x, 82), (x, 172))
    d.save(path)
    doc = process_file(path, ProcessingConfig(extract_pdf_tables=True, workers=1))
    tables = doc.pages[0].tables
    assert tables and tables[0].n_cols == 3
    assert any("Widget" in str(r) for r in tables[0].rows)


def test_owner_password_only_pdf_opens(tmp_path, config):
    doc = process_file(factory.owner_only_encrypted_pdf(tmp_path / "restricted.pdf"), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert "INV-2024-0042" in doc.pages[0].text


def test_garbage_ratio():
    assert garbage_ratio("Invoice total 12.50") == 0
    assert garbage_ratio("���ab") > 0.5
    assert garbage_ratio("") == 0


@requires_ocr
def test_scanned_pdf_uses_ocr(tmp_path, config):
    doc = process_file(factory.scanned_pdf(tmp_path / "scan.pdf", pages=2), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert all(p.method is ExtractionMethod.OCR for p in doc.pages)
    assert len(found_tokens(doc.pages[0].text)) >= 5
    assert doc.pages[0].ocr.confidence > 0.8
    assert doc.pages[0].extra["source_image_dpi"] > 150
    assert doc.stats.mean_ocr_confidence > 0.8


@requires_ocr
def test_mixed_pdf_classifies_each_page(tmp_path, config):
    doc = process_file(factory.mixed_pdf(tmp_path / "mixed.pdf"), config)
    methods = [p.method for p in doc.pages]
    assert methods == [ExtractionMethod.DIGITAL, ExtractionMethod.OCR,
                       ExtractionMethod.NONE, ExtractionMethod.HYBRID]
    assert doc.pages[2].extra.get("blank") is True
    assert "Page 4 of 4" in doc.pages[3].text  # digital stamp merged with OCR text
    assert "INV-2024-0004" in doc.pages[3].text.replace(" ", "")
    assert doc.status is DocumentStatus.SUCCESS


@requires_ocr
def test_degraded_scan_flagged_but_read(tmp_path, config):
    path = factory.scanned_pdf(tmp_path / "bad.pdf", pages=1, dpi=200,
                               angle=3, blur=1.6, noise=20, contrast=0.5)
    doc = process_file(path, config)
    page = doc.pages[0]
    assert page.quality.is_low_quality
    assert "LOW_IMAGE_QUALITY" in codes(doc)
    assert doc.needs_review
    assert len(found_tokens(page.text)) >= 3


def test_scanned_pdf_without_ocr_engine_fails_cleanly(tmp_path):
    cfg = ProcessingConfig(ocr_engine="none", workers=1)
    doc = process_file(factory.scanned_pdf(tmp_path / "scan.pdf", pages=2), cfg)
    assert doc.status is DocumentStatus.FAILED
    assert codes(doc) >= {"OCR_UNAVAILABLE"}
    assert doc.stats.pages_failed == 2


def test_hyphens_encoded_as_soft_hyphens_are_read_as_hyphens(tmp_path, config):
    """Word/Arial PDFs often encode "-" as U+00AD; "INV-2026-0042" must stay matchable."""
    from docintel.processing.processors.pdf import canonical_text

    assert canonical_text("INV\u00ad2026\u20110042 \u2212 5") == "INV-2026-0042 - 5"
    doc = pymupdf.open()
    page = doc.new_page()
    font = next((f for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
                 if Path(f).exists()), None)
    if font:
        page.insert_font(fontname="F", fontfile=font)
    page.insert_text((72, 72), "Invoice No: INV-2026-0042", fontname="F" if font else "helv", fontsize=12)
    doc.save(tmp_path / "hyphen.pdf")
    text = process_file(tmp_path / "hyphen.pdf", config).pages[0].text
    assert "INV-2026-0042" in text and "\u00ad" not in text
