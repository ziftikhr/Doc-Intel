import factory
from conftest import codes

from docintel.processing import ExtractionMethod, ProcessingConfig, process_file
from docintel.processing.models import DocumentStatus
from docintel.processing.processors.spreadsheet import (
    coerce_text_cell,
    detect_header,
    normalize_cell,
)


def _sheet(doc, name):
    return next(p for p in doc.pages if p.label == f"Sheet: {name}")


def test_xlsx_multiple_sheets(tmp_path, config):
    doc = process_file(factory.multisheet_xlsx(tmp_path / "book.xlsx"), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert doc.metadata.properties["sheet_names"] == ["Invoices", "Line Items", "Empty", "Hidden Notes"]
    assert all(p.method is ExtractionMethod.SPREADSHEET for p in doc.pages)

    inv = _sheet(doc, "Invoices").tables[0]
    assert inv.header == ["Invoice No", "Date", "Supplier", "Subtotal", "Tax", "Total"]
    assert inv.n_rows == 4
    first = inv.rows[0]
    assert first[0] == "00123"  # leading zeros preserved
    assert first[1] == "2024-03-15"  # dates normalised to ISO
    assert first[3:] == [205, 20.5, 225.5]
    assert _sheet(doc, "Invoices").extra["preamble"] == ["Invoice Register Q1 2024"]
    assert _sheet(doc, "Invoices").extra["merged_ranges"] == 1

    items = _sheet(doc, "Line Items").tables[0]
    assert items.rows[1] == ["00123", "Gadget B", 2, 40, 80]
    assert "Widget A" in _sheet(doc, "Line Items").text


def test_xlsx_warnings(tmp_path, config):
    doc = process_file(factory.multisheet_xlsx(tmp_path / "book.xlsx"), config)
    assert "EMPTY_SHEET" in codes(doc)
    assert "CELL_ERRORS" in codes(doc)  # the #DIV/0! cell
    assert _sheet(doc, "Hidden Notes").extra["hidden"] is True
    assert doc.needs_review


def test_xlsx_streaming_mode_matches_full_mode(tmp_path):
    path = factory.multisheet_xlsx(tmp_path / "book.xlsx")
    full = process_file(path, ProcessingConfig(workers=1))
    streamed = process_file(path, ProcessingConfig(workers=1, xlsx_full_load_max_mb=0))
    assert _sheet(streamed, "Line Items").extra["streamed"] is True
    assert _sheet(full, "Line Items").tables[0].rows == _sheet(streamed, "Line Items").tables[0].rows


def test_large_xlsx_row_limit(tmp_path):
    path = factory.large_xlsx(tmp_path / "big.xlsx", rows=5000)
    doc = process_file(path, ProcessingConfig(workers=1, max_rows_per_sheet=1000))
    t = doc.pages[0].tables[0]
    assert t.truncated and t.n_rows == 999  # 1000 rows read incl. header
    assert "ROWS_TRUNCATED" in codes(doc)


def test_xls_multiple_sheets_and_dates(tmp_path, config):
    doc = process_file(factory.multisheet_xls(tmp_path / "old.xls"), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert len(doc.pages) == 2
    t = doc.pages[0].tables[0]
    assert t.header == ["Invoice No", "Date", "Supplier", "Total"]
    assert t.rows[0] == ["00123", "2024-03-15", "ACME Supplies Ltd", 225.5]
    summary = doc.pages[1]
    assert "Grand Total" in summary.text and "335.5" in summary.text


def test_csv_utf8_bom(tmp_path, config):
    doc = process_file(factory.csv_utf8_bom(tmp_path / "a.csv"), config)
    t = doc.pages[0].tables[0]
    assert t.header == ["Invoice No", "Supplier", "Total", "Currency"]
    assert t.rows[0] == ["00123", "Müller GmbH", 1234.5, "EUR"]
    assert doc.pages[0].extra["encoding"] == "utf-8-sig"
    assert not doc.needs_review


def test_csv_semicolon_cp1252(tmp_path, config):
    doc = process_file(factory.csv_semicolon_cp1252(tmp_path / "de.csv"), config)
    page = doc.pages[0]
    assert page.extra["delimiter"] == ";"
    assert page.tables[0].rows[0] == ["R-001", "Müller & Söhne", "1.234,50 €"]
    assert "ENCODING_GUESSED" in codes(doc)


def test_csv_ragged_rows(tmp_path, config):
    doc = process_file(factory.csv_tab_ragged(tmp_path / "r.tsv"), config)
    t = doc.pages[0].tables[0]
    assert t.n_cols == 4
    assert t.rows[1] == [2, "beta", None, None]
    assert "MALFORMED_ROWS" in codes(doc)


def test_text_export_with_xls_extension(tmp_path, config):
    doc = process_file(factory.csv_as_xls(tmp_path / "erp.xls"), config)
    assert doc.status is DocumentStatus.SUCCESS
    assert doc.metadata.detected_type.value == "csv"
    assert "TYPE_MISMATCH" in codes(doc)
    assert doc.pages[0].tables[0].rows == [["INV-1", 10.0], ["INV-2", 20.0]]


def test_large_csv_streams(tmp_path, config):
    doc = process_file(factory.large_csv(tmp_path / "big.csv", rows=50_000), config)
    t = doc.pages[0].tables[0]
    assert t.n_rows == 50_000
    assert t.rows[-1][0] == "INV-0049999"


def test_cell_normalisation():
    from datetime import datetime

    assert normalize_cell(3.0) == 3
    assert normalize_cell(float("nan")) is None
    assert normalize_cell("  x ") == "x"
    assert normalize_cell("   ") is None
    assert normalize_cell(datetime(2024, 1, 2, 13, 5)) == "2024-01-02T13:05:00"
    assert coerce_text_cell("00123") == "00123"
    assert coerce_text_cell("1,234.50") == 1234.5
    assert coerce_text_cell("-7") == -7
    assert coerce_text_cell("12,5") == "12,5"  # ambiguous decimal comma stays text


def test_header_detection():
    rows = [["Report"], [None], ["Name", "Qty", "Price"], ["a", 1, 2.0], ["b", 3, 4.0]]
    assert detect_header(rows) == 2
    assert detect_header([[1, 2, 3], [4, 5, 6]]) is None
