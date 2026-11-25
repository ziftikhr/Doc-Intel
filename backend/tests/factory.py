"""Synthetic test documents: realistic invoices in every supported format, plus
degraded scans and deliberately broken files. Used by the tests and by
``scripts/generate_samples.py``.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image

INVOICE_LINES = [
    "ACME Supplies Ltd",
    "Invoice Number: INV-2024-0042",
    "Invoice Date: 2024-03-15",
    "Bill To: Globex Corporation",
    "",
    "Description          Qty    Unit Price     Amount",
    "Widget A             10        12.50       125.00",
    "Gadget B              2        40.00        80.00",
    "",
    "Subtotal: 205.00",
    "Tax (10%): 20.50",
    "Total: 225.50 USD",
]
KEY_TOKENS = ["INV-2024-0042", "2024-03-15", "Globex", "205.00", "20.50", "225.50"]


def invoice_lines(n: int = 42) -> list[str]:
    return [line.replace("0042", f"{n:04d}") for line in INVOICE_LINES]


def _write_invoice(page: pymupdf.Page, lines: list[str], fontsize: float = 12) -> None:
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=fontsize, fontname="cour")
        y += fontsize * 1.7


# ---------------------------------------------------------------- PDFs ----------


def digital_pdf(path: Path, pages: int = 3) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        _write_invoice(doc.new_page(), invoice_lines(42 + i))
    doc.save(path)
    return path


def render_invoice(dpi: int = 200, n: int = 42, fontsize: float = 12) -> np.ndarray:
    doc = pymupdf.open()
    page = doc.new_page()
    _write_invoice(page, invoice_lines(n), fontsize)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()


def degrade(gray: np.ndarray, *, angle: float = 0.0, blur: float = 0.0, noise: float = 0.0,
            contrast: float = 1.0, jpeg_quality: int | None = None, seed: int = 0) -> np.ndarray:
    img = gray.astype(np.float32)
    if contrast != 1.0:  # wash out: ink gets lighter, paper darker
        img = 128 + (img - 128) * contrast
    if angle:
        h, w = img.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        img = cv2.warpAffine(img, m, (w, h), borderValue=float(img.max()))
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if noise:
        img = img + np.random.default_rng(seed).normal(0, noise, img.shape)
    out = np.clip(img, 0, 255).astype(np.uint8)
    if jpeg_quality:
        out = cv2.imdecode(cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])[1], 0)
    return out


def _png_bytes(gray: np.ndarray) -> bytes:
    return cv2.imencode(".png", gray)[1].tobytes()


def scanned_pdf(path: Path, pages: int = 2, dpi: int = 200, **degradation) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        img = render_invoice(dpi, 42 + i)
        if degradation:
            img = degrade(img, seed=i, **degradation)
        page = doc.new_page()
        page.insert_image(page.rect, stream=_png_bytes(img))
    doc.save(path)
    return path


def mixed_pdf(path: Path) -> Path:
    """Page 1 digital, page 2 scanned, page 3 blank, page 4 scan + digital stamp."""
    doc = pymupdf.open()
    _write_invoice(doc.new_page(), invoice_lines(1))
    p2 = doc.new_page()
    p2.insert_image(p2.rect, stream=_png_bytes(render_invoice(200, 2)))
    doc.new_page()
    p4 = doc.new_page()
    p4.insert_image(p4.rect, stream=_png_bytes(render_invoice(200, 4)))
    p4.insert_text((450, 820), "Page 4 of 4", fontsize=8)
    doc.save(path)
    return path


def large_digital_pdf(path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        _write_invoice(page, invoice_lines(i + 1))
        page.insert_text((72, 800), f"PAGE-MARKER-{i + 1:05d}", fontsize=8)
    doc.save(path, garbage=3, deflate=True)
    return path


def encrypted_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    _write_invoice(doc.new_page(), invoice_lines())
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    return path


def owner_only_encrypted_pdf(path: Path) -> Path:
    """Restricted (owner password) but openable without a password - must succeed."""
    doc = pymupdf.open()
    _write_invoice(doc.new_page(), invoice_lines())
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner",
             permissions=pymupdf.PDF_PERM_PRINT)
    return path


def truncated_pdf(path: Path, keep: float = 0.6, pages: int = 5) -> Path:
    buf = io.BytesIO()
    doc = pymupdf.open()
    for i in range(pages):
        _write_invoice(doc.new_page(), invoice_lines(i))
    doc.save(buf)  # uncompressed so truncation leaves whole page objects behind
    data = buf.getvalue()
    path.write_bytes(data[: int(len(data) * keep)])
    return path


def garbage_with_pdf_header(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\n" + np.random.default_rng(1).bytes(4000))
    return path


# ---------------------------------------------------------------- images --------


def png_invoice(path: Path, dpi: int = 200) -> Path:
    Image.fromarray(render_invoice(dpi)).save(path, dpi=(dpi, dpi))
    return path


def poor_quality_jpeg(path: Path) -> Path:
    """Phone-photo-like: 150 DPI, skewed 4 deg, blurred, noisy, washed out, JPEG q=35."""
    img = degrade(render_invoice(150), angle=4, blur=0.8, noise=12, contrast=0.6, jpeg_quality=35)
    Image.fromarray(img).save(path, quality=95, dpi=(150, 150))
    return path


def rotated_png(path: Path, rotation: int = cv2.ROTATE_90_CLOCKWISE) -> Path:
    img = cv2.rotate(render_invoice(200), rotation)
    Image.fromarray(img).save(path, dpi=(200, 200))
    return path


def transparent_png(path: Path) -> Path:
    """Black text on a transparent background - naive conversion gives black-on-black."""
    gray = render_invoice(200)
    alpha = 255 - gray
    rgba = np.dstack([np.zeros_like(gray)] * 3 + [alpha])
    Image.fromarray(rgba, "RGBA").save(path)
    return path


def multipage_tiff(path: Path, frames: int = 3) -> Path:
    imgs = [Image.fromarray(render_invoice(200, 100 + i)).convert("1") for i in range(frames)]
    imgs[0].save(path, save_all=True, append_images=imgs[1:], compression="group4", dpi=(200, 200))
    return path


def sixteen_bit_tiff(path: Path) -> Path:
    img = render_invoice(200).astype(np.uint16) * 257
    Image.fromarray(img).save(path, dpi=(200, 200))
    return path


def truncated_jpeg(path: Path, keep: float = 0.7) -> Path:
    buf = io.BytesIO()
    Image.fromarray(render_invoice(200)).save(buf, "JPEG", quality=90, dpi=(200, 200))
    data = buf.getvalue()
    path.write_bytes(data[: int(len(data) * keep)])
    return path


def blank_png(path: Path) -> Path:
    Image.new("L", (1700, 2200), 255).save(path)
    return path


# ---------------------------------------------------------------- spreadsheets --


def multisheet_xlsx(path: Path) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws["A1"] = "Invoice Register Q1 2024"  # title row above the header
    ws.merge_cells("A1:F1")
    ws.append([])
    ws.append(["Invoice No", "Date", "Supplier", "Subtotal", "Tax", "Total"])
    ws.append(["00123", datetime(2024, 3, 15), "ACME Supplies Ltd", 205.0, 20.5, 225.5])
    ws.append(["INV-2024-0043", datetime(2024, 3, 16), "Initech", 100, 10, 110])
    ws.append(["INV-2024-0044", date(2024, 3, 17), "Umbrella Corp", 50.25, 5.03, "=D6+E6"])
    ws.append(["INV-2024-0045", datetime(2024, 3, 18), "Hooli", 10, "#DIV/0!", None])

    items = wb.create_sheet("Line Items")
    items.append(["Invoice No", "Description", "Qty", "Unit Price", "Amount"])
    items.append(["00123", "Widget A", 10, 12.5, 125.0])
    items.append(["00123", "Gadget B", 2, 40.0, 80.0])

    wb.create_sheet("Empty")
    hidden = wb.create_sheet("Hidden Notes")
    hidden.append(["note", "value"])
    hidden.append(["approved", True])
    hidden.sheet_state = "hidden"
    wb.save(path)
    return path


def large_xlsx(path: Path, rows: int) -> Path:
    import openpyxl

    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Data")
    ws.append(["Invoice No", "Amount", "Currency"])
    for i in range(rows):
        ws.append([f"INV-{i:07d}", round(i * 1.5, 2), "EUR"])
    wb.save(path)
    return path


def multisheet_xls(path: Path) -> Path:
    import xlwt

    wb = xlwt.Workbook()
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    ws = wb.add_sheet("Invoices")
    for c, h in enumerate(["Invoice No", "Date", "Supplier", "Total"]):
        ws.write(0, c, h)
    ws.write(1, 0, "00123")
    ws.write(1, 1, datetime(2024, 3, 15), date_style)
    ws.write(1, 2, "ACME Supplies Ltd")
    ws.write(1, 3, 225.5)
    ws.write(2, 0, "INV-2024-0043")
    ws.write(2, 1, datetime(2024, 3, 16), date_style)
    ws.write(2, 2, "Initech")
    ws.write(2, 3, 110)
    ws2 = wb.add_sheet("Summary")
    ws2.write_merge(0, 0, 0, 1, "Totals")
    ws2.write(1, 0, "Grand Total")
    ws2.write(1, 1, 335.5)
    wb.save(str(path))
    return path


def csv_utf8_bom(path: Path) -> Path:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice No", "Supplier", "Total", "Currency"])
        w.writerow(["00123", "Müller GmbH", "1,234.50", "EUR"])
        w.writerow(["INV-2", "Zoë Café", "99.90", "EUR"])
    return path


def csv_semicolon_cp1252(path: Path) -> Path:
    text = "Rechnung;Lieferant;Betrag\nR-001;Müller & Söhne;1.234,50 €\nR-002;Bäckerei Groß;€ 12,00\n"
    path.write_bytes(text.encode("cp1252"))
    return path


def csv_tab_ragged(path: Path) -> Path:
    path.write_text("id\tname\tamount\n1\talpha\t10\n2\tbeta\n3\tgamma\t30\textra\n", encoding="utf-8")
    return path


def csv_as_xls(path: Path) -> Path:
    """ERP-style export: tab-separated text with an .xls extension."""
    path.write_text("Invoice\tTotal\nINV-1\t10.00\nINV-2\t20.00\n", encoding="utf-8")
    return path


def large_csv(path: Path, rows: int) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice No", "Amount", "Currency"])
        for i in range(rows):
            w.writerow([f"INV-{i:07d}", f"{i * 1.5:.2f}", "EUR"])
    return path


# ---------------------------------------------------------------- broken files --


def corrupted_xlsx(path: Path) -> Path:
    good = io.BytesIO()
    multisheet_xlsx_bytes = multisheet_xlsx(path.with_suffix(".tmp.xlsx")).read_bytes()
    path.with_suffix(".tmp.xlsx").unlink()
    good.write(multisheet_xlsx_bytes)
    path.write_bytes(good.getvalue()[:1500])
    return path


def zip_bomb_xlsx(path: Path, mb: int = 30) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (mb * 1024 * 1024))
    return path


def encrypted_xlsx_container(path: Path) -> Path:
    """Minimal OLE header plus an EncryptedPackage stream name (as Excel writes)."""
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504
                     + "EncryptedPackage".encode("utf-16-le") + b"\0" * 2048)
    return path


def corrupted_xls(path: Path) -> Path:
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + np.random.default_rng(2).bytes(3000))
    return path


def random_bytes(path: Path, n: int = 5000) -> Path:
    path.write_bytes(np.random.default_rng(3).bytes(n))
    return path


def docx_like(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
    return path


def html_as_xls(path: Path) -> Path:
    path.write_text("<html><body><table><tr><td>1</td></tr></table></body></html>")
    return path


def empty_file(path: Path) -> Path:
    path.write_bytes(b"")
    return path
