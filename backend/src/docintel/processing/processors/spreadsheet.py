"""XLSX (openpyxl), XLS (xlrd) and CSV (stdlib csv) parsing.

Design choice - no pandas here: pandas' type inference silently turns invoice numbers
like ``00123`` into ``123`` and raises on ragged rows. Business exports are full of
both, so cells are read as-is and only unambiguous numbers are converted.

Each sheet becomes one ``PageResult`` with a ``Table`` (header + rows) and a
pipe-delimited text rendering used downstream for LLM extraction and search.
"""

from __future__ import annotations

import codecs
import csv
import math
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from ..config import ProcessingConfig
from ..detection import check_zip_safety
from ..errors import ProcessingError
from ..models import Cell, ExtractionMethod, Issue, IssueCode, PageResult, Table

EXCEL_ERRORS = {"#DIV/0!", "#N/A", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!",
                "#SPILL!", "#CALC!", "#GETTING_DATA"}
HEADER_SCAN_ROWS = 25

# Cell normalisation ---------------------------------------------------------------


def normalize_cell(value: Any) -> Cell:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        # Excel stores every number as a float; 3.0 means the integer 3.
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, Decimal):
        return normalize_cell(float(value))
    if isinstance(value, datetime):
        if value.time() == dtime(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    text = str(value).strip()
    return text or None


_PLAIN_NUMBER = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")
_THOUSANDS_NUMBER = re.compile(r"^-?[1-9]\d{0,2}(,\d{3})+(\.\d+)?$")


def coerce_text_cell(value: str | None) -> Cell:
    """CSV cells: convert only values that cannot be identifiers (no leading zeros)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _PLAIN_NUMBER.match(text) or _THOUSANDS_NUMBER.match(text):
        plain = text.replace(",", "")
        return float(plain) if "." in plain else int(plain)
    return text


# Table building -------------------------------------------------------------------


def _is_labelish(v: Cell) -> bool:
    return isinstance(v, str) and not _PLAIN_NUMBER.match(v) and len(v) <= 80


def detect_header(rows: list[list[Cell]]) -> int | None:
    """Index of the header row: the first dense row made of short text labels."""
    if len(rows) < 2:
        return None
    width = max((sum(c is not None for c in r) for r in rows[:HEADER_SCAN_ROWS]), default=0)
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        filled = [c for c in row if c is not None]
        # Distinct values: a merged title banner filled across columns is not a header.
        if len(set(map(str, filled))) < max(2, math.ceil(width * 0.5)):
            continue
        if sum(_is_labelish(c) for c in filled) / len(filled) >= 0.8 and i + 1 < len(rows):
            return i
    return None


def _unique_names(raw: list[Cell], width: int) -> list[str]:
    names, seen = [], {}
    for i in range(width):
        base = str(raw[i]).strip() if i < len(raw) and raw[i] is not None else f"column_{i + 1}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        names.append(base if n == 0 else f"{base}_{n + 1}")
    return names


def _fmt(v: Cell) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    return str(v).replace("\n", " ").replace("|", "/")


def _preamble_line(row: list[Cell]) -> str:
    # Merged cells repeat their value across the range - show it once.
    out: list[str] = []
    for c in row:
        if c is not None and (not out or out[-1] != _fmt(c)):
            out.append(_fmt(c))
    return " ".join(out)


def build_sheet_page(number: int, name: str, raw_rows: Iterable[Iterable[Any]],
                     config: ProcessingConfig, *, coerce=normalize_cell,
                     extra: dict | None = None) -> PageResult:
    issues: list[Issue] = []
    rows: list[list[Cell]] = []
    truncated = False
    cell_errors = 0
    for raw in raw_rows:
        row = [coerce(v) for v in raw]
        while row and row[-1] is None:
            row.pop()
        if not row:
            continue  # skip blank rows
        cell_errors += sum(1 for c in row if isinstance(c, str) and c in EXCEL_ERRORS)
        rows.append(row)
        if len(rows) >= config.max_rows_per_sheet:
            truncated = True
            break

    label = f"Sheet: {name}"
    extra = dict(extra or {})
    if not rows:
        issues.append(Issue.warning(IssueCode.EMPTY_SHEET, f"Sheet '{name}' is empty", number))
        return PageResult(number=number, label=label, method=ExtractionMethod.SPREADSHEET,
                          tables=[Table(name=name)], issues=issues, extra=extra)

    width = max(len(r) for r in rows)
    header_idx = detect_header(rows)
    if header_idx is not None:
        preamble = rows[:header_idx]
        header_raw = rows[header_idx]
        data = rows[header_idx + 1:]
        overflow = sum(1 for r in data if len(r) > len(header_raw))
        if overflow:
            issues.append(Issue.warning(
                IssueCode.MALFORMED_ROWS,
                f"{overflow} row(s) have more cells than the header ({len(header_raw)} columns)",
                number,
            ))
    else:
        preamble, header_raw, data = [], [], rows
    header = _unique_names(header_raw, width)
    data = [r + [None] * (width - len(r)) for r in data]

    if truncated:
        issues.append(Issue.warning(IssueCode.ROWS_TRUNCATED,
                                    f"Sheet '{name}' truncated at {config.max_rows_per_sheet} rows",
                                    number))
    if cell_errors:
        issues.append(Issue.warning(IssueCode.CELL_ERRORS,
                                    f"{cell_errors} cell(s) contain Excel errors (e.g. #DIV/0!, #REF!)",
                                    number))

    preamble_text = [_preamble_line(r) for r in preamble]
    lines = [f"Sheet: {name}", *preamble_text]
    lines.append(" | ".join(header))
    budget = config.max_sheet_text_chars
    size = sum(len(x) for x in lines)
    for r in data:
        line = " | ".join(_fmt(c) for c in r)
        size += len(line) + 1
        if size > budget:
            issues.append(Issue.warning(IssueCode.TEXT_TRUNCATED,
                                        "Text rendering truncated (full data is in the table)", number))
            break
        lines.append(line)
    text = "\n".join(lines)

    if preamble:
        extra["preamble"] = preamble_text
    table = Table(name=name, header=header, rows=data, n_rows=len(data), n_cols=width,
                  header_row_index=header_idx, truncated=truncated)
    return PageResult(number=number, label=label, method=ExtractionMethod.SPREADSHEET,
                      text=text, char_count=len(text), tables=[table], issues=issues, extra=extra)


def _timed(fn, *args, **kwargs) -> PageResult:
    t0 = time.perf_counter()
    page = fn(*args, **kwargs)
    page.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    return page


def _sheet_failure(number: int, name: str, exc: Exception) -> PageResult:
    return PageResult(number=number, label=f"Sheet: {name}", method=ExtractionMethod.NONE,
                      issues=[Issue.error(IssueCode.PAGE_FAILED,
                                          f"{type(exc).__name__}: {exc}", number)])


# XLSX -----------------------------------------------------------------------------


def _load_xlsx(path: Path, read_only: bool):
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        return openpyxl.load_workbook(path, read_only=read_only, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ProcessingError(IssueCode.CORRUPTED, f"Unreadable XLSX: {exc}") from exc
    except Exception as exc:  # XML parse errors from damaged parts
        raise ProcessingError(IssueCode.CORRUPTED, f"Unreadable XLSX: {type(exc).__name__}: {exc}") from exc


def inspect_xlsx(path: Path, config: ProcessingConfig) -> tuple[int, dict, list[Issue]]:
    check_zip_safety(path, config)
    wb = _load_xlsx(path, read_only=True)
    try:
        names = wb.sheetnames
    finally:
        wb.close()
    return len(names), {"sheet_names": names}, []


def process_xlsx(path: Path, config: ProcessingConfig) -> list[PageResult]:
    check_zip_safety(path, config)
    full = path.stat().st_size <= config.xlsx_full_load_max_mb * 1024 * 1024
    wb = _load_xlsx(path, read_only=not full)
    pages = []
    try:
        for number, ws in enumerate(wb.worksheets, 1):
            name = ws.title
            extra = {"hidden": ws.sheet_state != "visible", "streamed": not full}
            try:
                if full:
                    rows, merged = _grid_with_merged_cells(ws)
                    extra["merged_ranges"] = merged
                else:
                    ws.reset_dimensions()  # stored dimensions are often wrong in exports
                    rows = ws.iter_rows(values_only=True)
                pages.append(_timed(build_sheet_page, number, name, rows, config, extra=extra))
            except Exception as exc:
                pages.append(_sheet_failure(number, name, exc))
    finally:
        wb.close()
    return pages


def _grid_with_merged_cells(ws) -> tuple[list[list[Any]], int]:
    if ws.max_row is None or ws.max_column is None:
        return [], 0
    grid = [list(r) for r in ws.iter_rows(min_row=1, min_col=1, max_row=ws.max_row,
                                           max_col=ws.max_column, values_only=True)]
    ranges = list(ws.merged_cells.ranges)
    for rng in ranges:
        value = grid[rng.min_row - 1][rng.min_col - 1] if rng.min_row <= len(grid) else None
        for r in range(rng.min_row - 1, min(rng.max_row, len(grid))):
            for c in range(rng.min_col - 1, min(rng.max_col, len(grid[r]))):
                grid[r][c] = value
    return grid, len(ranges)


# XLS ------------------------------------------------------------------------------


def _open_xls(path: Path, formatting_info: bool):
    import xlrd

    try:
        return xlrd.open_workbook(str(path), on_demand=True, formatting_info=formatting_info)
    except xlrd.XLRDError as exc:
        msg = str(exc)
        if "encrypt" in msg.lower():
            raise ProcessingError(IssueCode.ENCRYPTED, "XLS workbook is password-protected") from exc
        raise ProcessingError(IssueCode.CORRUPTED, f"Unreadable XLS: {msg}") from exc
    except Exception as exc:  # struct errors, truncated OLE streams, ...
        raise ProcessingError(IssueCode.CORRUPTED, f"Unreadable XLS: {type(exc).__name__}: {exc}") from exc


def _xls_book(path: Path, config: ProcessingConfig):
    # formatting_info is needed for merged-cell ranges, but costs memory: small files only.
    if path.stat().st_size <= config.xlsx_full_load_max_mb * 1024 * 1024:
        try:
            return _open_xls(path, formatting_info=True), True
        except ProcessingError as exc:
            if exc.code is IssueCode.ENCRYPTED:
                raise
    return _open_xls(path, formatting_info=False), False


def inspect_xls(path: Path, config: ProcessingConfig) -> tuple[int, dict, list[Issue]]:
    book = _open_xls(path, formatting_info=False)
    try:
        names = book.sheet_names()
    finally:
        book.release_resources()
    return len(names), {"sheet_names": names}, []


def process_xls(path: Path, config: ProcessingConfig) -> list[PageResult]:
    import xlrd

    book, with_merges = _xls_book(path, config)
    pages = []
    try:
        for number, name in enumerate(book.sheet_names(), 1):
            try:
                sheet = book.sheet_by_name(name)
                grid = [[_xls_value(sheet.cell(r, c), book.datemode, xlrd)
                         for c in range(sheet.ncols)] for r in range(sheet.nrows)]
                merged = sheet.merged_cells if with_merges else []
                for r0, r1, c0, c1 in merged:
                    value = grid[r0][c0]
                    for r in range(r0, r1):
                        for c in range(c0, c1):
                            grid[r][c] = value
                extra = {"hidden": bool(sheet.visibility), "merged_ranges": len(merged)}
                pages.append(_timed(build_sheet_page, number, name, grid, config, extra=extra))
                book.unload_sheet(name)
            except Exception as exc:
                pages.append(_sheet_failure(number, name, exc))
    finally:
        book.release_resources()
    return pages


def _xls_value(cell, datemode: int, xlrd) -> Any:
    ctype = cell.ctype
    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except Exception:
            return cell.value
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERROR")
    return cell.value


# CSV ------------------------------------------------------------------------------

csv.field_size_limit(2**31 - 1)
ENCODING_SAMPLE = 512 * 1024


def detect_encoding(sample: bytes) -> tuple[str, bool]:
    """Return (encoding, was_guessed)."""
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", False
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", False
    try:
        # Incremental decoder tolerates a multi-byte char cut off at the sample's end.
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8", False
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(sample).best()
    if best is not None and best.encoding:
        return best.encoding, True
    return "cp1252", True


def sniff_dialect(text: str, suffix: str) -> type[csv.Dialect] | csv.Dialect:
    lines = text.splitlines()[:50]
    sample = "\n".join(lines)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        # The sniffer can pick a delimiter that appears in only a few lines; check it.
        counts = [ln.count(dialect.delimiter) for ln in lines if ln.strip()]
        if counts and sum(1 for c in counts if c > 0) / len(counts) >= 0.8:
            return dialect
    except csv.Error:
        pass
    if suffix == ".tsv":
        return csv.excel_tab
    best = max(",;\t|", key=lambda d: sum(ln.count(d) for ln in lines))
    dialect = csv.excel()
    dialect.delimiter = best if any(best in ln for ln in lines) else ","
    return dialect


def process_csv(path: Path, config: ProcessingConfig) -> list[PageResult]:
    with open(path, "rb") as fh:
        sample = fh.read(ENCODING_SAMPLE)
    encoding, guessed = detect_encoding(sample)
    text_sample = sample.decode(encoding, errors="replace")
    dialect = sniff_dialect(text_sample, path.suffix.lower())

    replaced = 0

    def rows() -> Iterable[list[str]]:
        nonlocal replaced
        with open(path, "r", encoding=encoding, errors="replace", newline="") as fh:
            clean = (line.replace("\x00", "") for line in fh)
            for row in csv.reader(clean, dialect):
                replaced += sum(cell.count("�") for cell in row)
                yield row

    extra = {"encoding": encoding, "delimiter": dialect.delimiter}
    try:
        page = _timed(build_sheet_page, 1, path.stem, rows(), config,
                      coerce=coerce_text_cell, extra=extra)
    except csv.Error as exc:
        raise ProcessingError(IssueCode.CORRUPTED, f"Malformed CSV: {exc}") from exc
    if guessed or replaced:
        msg = f"Encoding detected as {encoding}"
        if replaced:
            msg += f"; {replaced} undecodable character(s) replaced"
        page.issues.append(Issue.warning(IssueCode.ENCODING_GUESSED, msg, 1))
    return [page]
