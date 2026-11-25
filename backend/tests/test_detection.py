import factory
import pytest

from docintel.processing.config import ProcessingConfig
from docintel.processing.detection import check_zip_safety, detect_file_type
from docintel.processing.errors import ProcessingError
from docintel.processing.models import FileType, IssueCode


@pytest.mark.parametrize("maker,name,expected", [
    (factory.digital_pdf, "a.pdf", FileType.PDF),
    (factory.png_invoice, "a.png", FileType.PNG),
    (factory.poor_quality_jpeg, "a.jpg", FileType.JPEG),
    (factory.multipage_tiff, "a.tiff", FileType.TIFF),
    (factory.multisheet_xlsx, "a.xlsx", FileType.XLSX),
    (factory.multisheet_xls, "a.xls", FileType.XLS),
    (factory.csv_utf8_bom, "a.csv", FileType.CSV),
    (factory.csv_tab_ragged, "a.tsv", FileType.CSV),
])
def test_detects_supported_types(tmp_path, maker, name, expected):
    det = detect_file_type(maker(tmp_path / name))
    assert det.file_type is expected
    assert not det.mismatch


def test_content_wins_over_extension(tmp_path):
    src = factory.png_invoice(tmp_path / "scan.png")
    disguised = tmp_path / "scan.pdf"
    disguised.write_bytes(src.read_bytes())
    det = detect_file_type(disguised)
    assert det.file_type is FileType.PNG
    assert det.mismatch


def test_tab_text_with_xls_extension_is_csv(tmp_path):
    det = detect_file_type(factory.csv_as_xls(tmp_path / "export.xls"))
    assert det.file_type is FileType.CSV
    assert det.mismatch


@pytest.mark.parametrize("maker,name", [
    (factory.random_bytes, "x.bin"),
    (factory.docx_like, "x.docx"),
    (factory.html_as_xls, "x.xls"),
])
def test_unsupported_content(tmp_path, maker, name):
    assert detect_file_type(maker(tmp_path / name)).file_type is FileType.UNKNOWN


def test_encrypted_ooxml_detected(tmp_path):
    with pytest.raises(ProcessingError) as exc:
        detect_file_type(factory.encrypted_xlsx_container(tmp_path / "locked.xlsx"))
    assert exc.value.code is IssueCode.ENCRYPTED


def test_zip_bomb_rejected(tmp_path):
    path = factory.zip_bomb_xlsx(tmp_path / "bomb.xlsx", mb=30)
    with pytest.raises(ProcessingError) as exc:
        check_zip_safety(path, ProcessingConfig(max_zip_uncompressed_mb=10))
    assert exc.value.code is IssueCode.DECOMPRESSION_BOMB
    with pytest.raises(ProcessingError) as exc:  # ratio check alone also catches it
        check_zip_safety(path, ProcessingConfig(max_zip_ratio=100))
    assert exc.value.code is IssueCode.DECOMPRESSION_BOMB
