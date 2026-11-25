"""Failure scenarios: every bad input must produce a classified result, never an exception."""

import factory
import pytest
from conftest import codes

from docintel.processing import ProcessingConfig, process_file
from docintel.processing.models import DocumentStatus


@pytest.mark.parametrize("maker,name,expected_code", [
    (factory.empty_file, "empty.pdf", "EMPTY_FILE"),
    (factory.random_bytes, "noise.pdf", "UNSUPPORTED_TYPE"),
    (factory.garbage_with_pdf_header, "fake.pdf", "CORRUPTED"),
    (factory.encrypted_pdf, "locked.pdf", "ENCRYPTED"),
    (factory.corrupted_xlsx, "broken.xlsx", "CORRUPTED"),
    (factory.encrypted_xlsx_container, "locked.xlsx", "ENCRYPTED"),
    (factory.corrupted_xls, "broken.xls", "CORRUPTED"),
    (factory.docx_like, "letter.docx", "UNSUPPORTED_TYPE"),
    (factory.html_as_xls, "report.xls", "UNSUPPORTED_TYPE"),
])
def test_bad_files_fail_cleanly(tmp_path, config, maker, name, expected_code):
    doc = process_file(maker(tmp_path / name), config)
    assert doc.status is DocumentStatus.FAILED
    assert expected_code in codes(doc)
    assert doc.needs_review
    doc.model_dump_json()  # result is always serialisable


def test_missing_file(tmp_path, config):
    doc = process_file(tmp_path / "nope.pdf", config)
    assert doc.status is DocumentStatus.FAILED
    assert codes(doc) == {"FILE_NOT_FOUND"}


def test_truncated_pdf_is_repaired_or_partial(tmp_path, config):
    doc = process_file(factory.truncated_pdf(tmp_path / "cut.pdf", keep=0.6, pages=5), config)
    assert doc.status in (DocumentStatus.SUCCESS, DocumentStatus.PARTIAL)
    assert "REPAIRED" in codes(doc)
    assert doc.needs_review
    readable = [p for p in doc.pages if "INV-2024" in p.text]
    assert readable, "pages before the cut must still be extracted"


def test_file_size_limit(tmp_path):
    path = factory.digital_pdf(tmp_path / "a.pdf")
    doc = process_file(path, ProcessingConfig(max_file_size_mb=0, workers=1))
    assert codes(doc) == {"FILE_TOO_LARGE"}


def test_page_limit(tmp_path):
    path = factory.digital_pdf(tmp_path / "a.pdf", pages=3)
    doc = process_file(path, ProcessingConfig(max_pages=2, workers=1))
    assert codes(doc) == {"TOO_MANY_PAGES"}


def test_decompression_bomb_image(tmp_path):
    path = factory.png_invoice(tmp_path / "a.png")
    doc = process_file(path, ProcessingConfig(max_image_pixels=1000, workers=1))
    assert doc.status is DocumentStatus.FAILED
    assert codes(doc) == {"DECOMPRESSION_BOMB"}


def test_zip_bomb_xlsx(tmp_path):
    path = factory.zip_bomb_xlsx(tmp_path / "bomb.xlsx", mb=30)
    doc = process_file(path, ProcessingConfig(max_zip_uncompressed_mb=10, workers=1))
    assert codes(doc) == {"DECOMPRESSION_BOMB"}


def test_one_bad_page_does_not_fail_document(tmp_path, config, monkeypatch):
    from docintel.processing.processors import pdf as pdf_proc

    original = pdf_proc._process_page

    def flaky(doc, idx, cfg):
        if idx == 1:
            raise RuntimeError("simulated renderer crash")
        return original(doc, idx, cfg)

    monkeypatch.setattr(pdf_proc, "_process_page", flaky)
    doc = process_file(factory.digital_pdf(tmp_path / "a.pdf", pages=3), config)
    assert doc.status is DocumentStatus.PARTIAL
    assert doc.stats.pages_failed == 1
    assert doc.pages[1].issues[0].code.value == "PAGE_FAILED"
    assert "INV-2024-0044" in doc.pages[2].text


def test_unexpected_processor_exception_is_contained(tmp_path, config, monkeypatch):
    from docintel.processing.processors import spreadsheet

    def boom(*_a, **_k):
        raise MemoryError("simulated")

    monkeypatch.setattr(spreadsheet, "process_csv", boom)
    doc = process_file(factory.csv_utf8_bom(tmp_path / "a.csv"), config)
    assert doc.status is DocumentStatus.FAILED
    assert "INTERNAL_ERROR" in codes(doc)


# ------------------------------------------- end to end: processing -> extraction -> validation ----------

BROKEN_MAKERS = [
    (factory.empty_file, "empty.pdf"), (factory.random_bytes, "noise.pdf"),
    (factory.garbage_with_pdf_header, "fake.pdf"), (factory.encrypted_pdf, "locked.pdf"),
    (factory.corrupted_xlsx, "broken.xlsx"), (factory.encrypted_xlsx_container, "locked.xlsx"),
    (factory.corrupted_xls, "broken.xls"), (factory.docx_like, "letter.docx"), (factory.html_as_xls, "report.xls"),
]


def _pipeline(path, config):
    from docintel.extraction import ExtractionConfig, extract_invoices
    from docintel.validation import validate_document

    doc = process_file(path, config)
    res = extract_invoices(doc, ExtractionConfig(llm_provider="none"), llm=None)
    return doc, res, validate_document(res)


@pytest.mark.parametrize("maker,name", BROKEN_MAKERS)
def test_broken_file_gets_a_failed_verdict_with_the_reason(tmp_path, config, maker, name):
    _, res, report = _pipeline(maker(tmp_path / name), config)
    assert res.status == "failed" and report.status == "failed" and report.needs_review
    [finding] = report.issues
    assert finding.code == "EXTRACTION_FAILED" and finding.severity == "error"
    assert "SOURCE_FAILED" in finding.message  # the reason from Task 1 is carried through


def test_damaged_pdf_is_never_reported_as_clean(tmp_path, config):
    _, res, report = _pipeline(factory.truncated_pdf(tmp_path / "cut.pdf", keep=0.6, pages=5), config)
    assert res.invoices, "the invoices before the damage are still extracted"
    assert all(v.status == "valid" for v in report.invoices)  # each one read is fine...
    assert report.status == "needs_review" and report.needs_review  # ...but some may be missing
    assert [i.code for i in report.issues] == ["SOURCE_INCOMPLETE"]


def test_cut_off_photo_needs_review(tmp_path, config):
    _, _, report = _pipeline(factory.truncated_jpeg(tmp_path / "cut.jpg"), config)
    assert report.needs_review and "SOURCE_INCOMPLETE" in [i.code for i in report.issues]


def test_blank_scan_is_no_invoice_not_a_crash(tmp_path, config):
    _, res, report = _pipeline(factory.blank_png(tmp_path / "blank.png"), config)
    assert res.status == "failed" and report.status == "failed" and report.needs_review
