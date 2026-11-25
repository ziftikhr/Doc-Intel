"""Labeled evaluation dataset.

No answer key came with the assignment, so the ground truth is created together with
the documents: every invoice is first generated as data (supplier, items, charges,
tax...), then *printed* into a document, and the data is written to ``labels.json``.
Accuracy is measured against that data, so it is independent of how the system reads.

What the set covers (all reproducible from ``--seed``):

* 6 layouts - classic "Label: value", two-column (meta on the right), German
  (comma decimals, Rechnung/Nettobetrag/USt), header-row ("INVOICE #  DATE" with
  values below, price before quantity), receipt, and an *unseen* layout whose labels
  ("Invoice ref", "Tax point", "Goods total", "Total payable") were never used while
  writing the rules - a fair test of generalisation;
* formats - born-digital PDF, scanned PDF (OCR), phone photo JPEG (skew, blur, noise,
  JPEG artefacts), clean PNG scan, multi-invoice PDFs incl. an invoice spanning two
  pages, XLSX / CSV registers;
* currencies USD/EUR/GBP/BDT, "1,234.56" and "1.234,56" number styles, five date
  styles (no ambiguous day/month dates - those are reported, not guessed, by design);
* shipping, freight, discounts; credit notes and receipts;
* planted errors with the finding validation must raise: TOTAL_MISMATCH,
  LINE_ITEM_MISMATCH, MISSING_REQUIRED_FIELD, DATE_IN_FUTURE, DUPLICATE_INVOICE;
* broken files (empty, random bytes, corrupt, password-protected, wrong type).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
import pymupdf
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import factory  # noqa: E402

CENT = Decimal("0.01")
FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]

SUPPLIERS = [
    ("Ember Distribution Pvt. Ltd.", "231 Industrial Way, Austin, TX 73301"),
    ("Northgate Office Supplies Ltd", "14 Mill Lane, Leeds LS1 4AB"),
    ("Kestrel Components GmbH", "Hafenstraße 12, 20457 Hamburg"),
    ("Bluewater Logistics LLC", "88 Harbor Blvd, Long Beach, CA 90802"),
    ("Sundarban Traders Ltd.", "House 7, Road 2, Gulshan, Dhaka 1212"),
    ("Alpine Werkzeug AG", "Bahnhofstrasse 5, 8001 Zürich"),
    ("Crescent IT Services Inc.", "500 Market St, San Francisco, CA 94105"),
    ("Meridian Foods Co.", "9 Dock Road, Cardiff CF10 5AB"),
    ("Orion Print & Media", "42 Fleet Street, London EC4Y 1AA"),
    ("Tamarind Hospitality Pvt Ltd", "Plot 22, Banani, Dhaka 1213"),
    ("Brightline Electrical Supply", "310 Ohm Avenue, Denver, CO 80202"),
    ("Falcon Facility Management", "Unit 3, Riverside Park, Bristol BS1 6QP"),
]
CUSTOMERS = ["Northwind Holdings Ltd.", "Globex Corporation", "Initech LLC", "Umbrella Retail GmbH",
             "Stark Engineering Ltd", "Wayne Hospitality Group"]
ITEMS = [
    ("Cat6 Patch Cable 2m", 3, 9), ("Wi-Fi 6 Access Point", 120, 240), ("Wireless Keyboard & Mouse Set", 35, 70),
    ('27" IPS Monitor', 160, 320), ("Server RAM 32GB DDR5", 95, 160), ("UPS 1500VA", 180, 260),
    ("Office Chair Ergonomic", 140, 390), ("A4 Copy Paper (box of 5 reams)", 18, 32), ("Toner Cartridge HP 59A", 60, 110),
    ("Consulting Services (hourly)", 75, 180), ("Monthly Cloud Hosting", 200, 1800), ("Safety Helmet EN397", 9, 25),
    ("Hydraulic Hose 1/2\" per m", 6, 18), ("Stainless Bolt M8x40 (box 100)", 11, 30), ("Pallet Wrap Film 500mm", 9, 20),
    ("Coffee Beans 1kg", 14, 28), ("Cleaning Service (weekly)", 90, 220), ("Desk Lamp LED", 19, 45),
    ("Training Workshop (per participant)", 150, 400), ("USB-C Docking Station", 120, 210),
]
CURRENCIES = {"USD": "$", "EUR": "€", "GBP": "£", "BDT": "Tk "}


# ----------------------------------------------------------------- data model ----------


@dataclass
class Item:
    description: str
    sku: str
    quantity: int
    unit_price: Decimal
    amount: Decimal  # as printed (a planted error may make it wrong)


@dataclass
class Charge:
    kind: str
    label: str
    amount: Decimal  # signed


@dataclass
class Inv:
    number: Optional[str]
    issued: date
    supplier: tuple[str, str]
    customer: Optional[str]
    items: list[Item]
    charges: list[Charge]
    tax_rate: Decimal
    subtotal: Decimal
    tax: Decimal
    total: Decimal  # as printed
    currency: str
    doc_type: str = "invoice"
    errors: list[str] = field(default_factory=list)

    def label(self) -> dict:
        return {
            "document_type": self.doc_type,
            "invoice_number": self.number,
            "invoice_date": self.issued.isoformat(),
            "supplier_name": self.supplier[0],
            "customer_name": self.customer,
            "subtotal": str(self.subtotal),
            "tax": str(self.tax),
            "total": str(self.total),
            "currency": self.currency,
            "line_items": [{"description": i.description, "quantity": str(i.quantity),
                            "unit_price": str(i.unit_price), "amount": str(i.amount)} for i in self.items],
            "charges": [{"kind": c.kind, "label": c.label, "amount": str(c.amount)} for c in self.charges],
            "expected_findings": list(self.errors),
        }


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


class Maker:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.used_numbers: set[str] = set()

    def number(self, style: str) -> str:
        while True:
            n = self.rng.randint(100, 9999)
            s = {"dash": f"INV-2026-{n:04d}", "slash": f"2026/{n:04d}", "de": f"RE{26000000 + n}",
                 "prefix": f"{self.rng.choice('ABCDEFGHKMNPRSTW')}{self.rng.choice('ABCDEFGHKMNPRSTW')}-{n:05d}",
                 "receipt": f"R-{n:06d}"}[style]
            if s not in self.used_numbers:
                self.used_numbers.add(s)
                return s

    def invoice(self, *, number_style: str, currency: str, n_items: tuple[int, int] = (1, 8),
                doc_type: str = "invoice", charges: bool = True) -> Inv:
        r = self.rng
        items = []
        for _ in range(r.randint(*n_items)):
            desc, lo, hi = r.choice(ITEMS)
            qty = r.choice([1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 24, 30, 40, 50])
            unit = q2(Decimal(str(r.uniform(lo, hi))))
            if currency == "BDT":
                unit = q2(unit * 110)
            items.append(Item(desc, f"{r.choice(['IT', 'OF', 'SV', 'IND'])}-{r.randint(1000, 9999)}", qty, unit,
                              q2(unit * qty)))
        subtotal = sum((i.amount for i in items), Decimal(0))
        chg: list[Charge] = []
        if charges and r.random() < 0.35:
            amt = q2(Decimal(r.choice([5, 9.5, 12, 15, 19.75, 25, 40])) * (110 if currency == "BDT" else 1))
            chg.append(Charge("shipping", r.choice(["Shipping & Handling", "Shipping", "Freight", "Delivery"]), amt))
        if charges and r.random() < 0.25:
            pct = r.choice([2, 3, 5])
            chg.append(Charge("discount", f"Discount ({pct}%)", -q2(subtotal * pct / 100)))
        rate = Decimal(r.choice(["0", "5", "7.5", "8.25", "10", "15", "19", "20"]))
        tax = q2((subtotal + sum((c.amount for c in chg), Decimal(0))) * rate / 100)
        total = subtotal + sum((c.amount for c in chg), Decimal(0)) + tax
        issued = date(2026, 1, 1) + timedelta(days=r.randint(0, 300))
        return Inv(self.number(number_style), issued, r.choice(SUPPLIERS), r.choice(CUSTOMERS), items, chg, rate,
                   subtotal, tax, total, currency, doc_type)

    def plant(self, inv: Inv, error: str) -> Inv:
        """Make the printed document wrong in a known way; labels keep what is printed."""
        if error == "TOTAL_MISMATCH":
            inv.total = inv.total + Decimal(self.rng.choice(["10.00", "100.00", "-50.00", "0.90", "1000.00"]))
        elif error == "LINE_ITEM_MISMATCH":
            k = self.rng.randrange(len(inv.items))
            inv.items[k].amount = inv.items[k].amount + Decimal(self.rng.choice(["10.00", "9.00", "100.00", "-1.00"]))
        elif error == "MISSING_REQUIRED_FIELD":
            inv.number = None
        elif error == "DATE_IN_FUTURE":
            inv.issued = date(2027, self.rng.randint(1, 12), self.rng.randint(13, 28))
        inv.errors.append(error)
        return inv


# --------------------------------------------------------------- formatting ----------


def money(d: Decimal, style: str) -> str:
    s = f"{abs(d):,.2f}"
    if style == "de":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if d < 0 else "") + s


def with_currency(d: Decimal, inv: Inv, style: str, placement: str) -> str:
    m = money(abs(d), style)
    sign = "-" if d < 0 else ""
    if placement == "symbol":
        return f"{sign}{CURRENCIES[inv.currency]}{m}"
    if placement == "suffix_symbol":
        return f"{sign}{m} {CURRENCIES[inv.currency].strip()}"
    if placement == "code":
        return f"{sign}{inv.currency} {m}"
    return f"{sign}{m}"


def fmt_date(d: date, style: str) -> str:
    if style == "uk" and d.day <= 12:
        style = "iso"  # 07/10/2026 is ambiguous; the system reports those instead of guessing
    return {"iso": d.isoformat(), "de": d.strftime("%d.%m.%Y"), "long": d.strftime("%B %d, %Y").replace(" 0", " "),
            "short": d.strftime("%d %b %Y"), "uk": d.strftime("%d/%m/%Y")}[style]


# ------------------------------------------------------------------ canvas ----------


class Canvas:
    """Positioned text on PDF pages with a real TrueType font (so €, £ print)."""

    def __init__(self):
        self.doc = pymupdf.open()
        self.regular = self.bold = None
        for d in FONT_DIRS:
            if (d / "arial.ttf").exists():
                self.regular, self.bold = str(d / "arial.ttf"), str(d / ("arialbd.ttf" if (d / "arialbd.ttf").exists()
                                                                         else "arial.ttf"))
                break
            if (d / "DejaVuSans.ttf").exists():
                self.regular, self.bold = str(d / "DejaVuSans.ttf"), str(d / "DejaVuSans-Bold.ttf")
                break
        if self.regular:
            self.fonts = {False: ("R", pymupdf.Font(fontfile=self.regular)), True: ("B", pymupdf.Font(fontfile=self.bold))}
        else:
            self.fonts = {False: ("helv", pymupdf.Font("helv")), True: ("hebo", pymupdf.Font("hebo"))}
        self.page = None
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=595, height=842)
        if self.regular:
            self.page.insert_font(fontname="R", fontfile=self.regular)
            self.page.insert_font(fontname="B", fontfile=self.bold)
        return self.page

    def _font(self, bold: bool):
        return self.fonts[bold]

    def text(self, x: float, y: float, s: str, size: float = 9, bold: bool = False, align: str = "left"):
        name, font = self._font(bold)
        if align == "right":
            x -= font.text_length(s, fontsize=size)
        self.page.insert_text((x, y), s, fontsize=size, fontname=name)

    def line(self, x0, y, x1):
        self.page.draw_line((x0, y), (x1, y), width=0.6)

    def save(self, path: Path):
        self.doc.save(path)


# ------------------------------------------------------------------ layouts ----------
# Each layout draws one invoice starting on the canvas' current page.


def _items_paged(c: Canvas, inv: Inv, y: float, draw_row: Callable, header: Callable, cont: Callable,
                 rows_per_page: int = 34) -> float:
    for i, it in enumerate(inv.items):
        if i and i % rows_per_page == 0:
            c.new_page()
            y = cont()
            y = header(y)
        draw_row(i, it, y)
        y += 13
    return y


def layout_classic(c: Canvas, inv: Inv, *, date_style="iso", style="en", placement="symbol", pages_total=1):
    s, a = inv.supplier
    c.text(50, 60, s, 15, True)
    c.text(50, 76, a, 9)
    c.text(545, 60, {"credit_note": "CREDIT NOTE", "receipt": "RECEIPT"}.get(inv.doc_type, "INVOICE"), 18, True, "right")
    y = 120
    label = {"credit_note": "Credit Note Number", "receipt": "Receipt Number"}.get(inv.doc_type, "Invoice Number")
    if inv.number:
        c.text(50, y, f"{label}: {inv.number}", 10)
        y += 15
    c.text(50, y, f"Invoice Date: {fmt_date(inv.issued, date_style)}", 10)
    y += 15
    c.text(50, y, f"Bill To: {inv.customer}", 10)
    y += 30

    def header(y):
        c.text(50, y, "Description", 9, True)
        c.text(330, y, "Qty", 9, True, "right")
        c.text(440, y, "Unit Price", 9, True, "right")
        c.text(545, y, "Amount", 9, True, "right")
        c.line(50, y + 4, 545)
        return y + 17

    def row(i, it, y):
        c.text(50, y, it.description, 9)
        c.text(330, y, str(it.quantity), 9, align="right")
        c.text(440, y, money(it.unit_price, style), 9, align="right")
        c.text(545, y, money(it.amount, style), 9, align="right")

    y = _items_paged(c, inv, header(y), row, header, lambda: 80)
    y += 15
    tot = [("Subtotal:", inv.subtotal)] + [(f"{ch.label}:", ch.amount) for ch in inv.charges]
    tot += [(f"Tax ({inv.tax_rate.normalize():f}%):", inv.tax), ("Total:", inv.total)]
    for lab, v in tot:
        c.text(440, y, lab, 10, lab == "Total:", "right")
        c.text(545, y, with_currency(v, inv, style, placement), 10, lab == "Total:", "right")
        y += 15


def layout_two_column(c: Canvas, inv: Inv, *, date_style="iso", style="en", placement="symbol", pages_total=1):
    s, a = inv.supplier
    page_no = [1]

    def top():
        c.text(50, 55, s, 14, True)
        c.text(545, 55, "INVOICE", 18, True, "right")
        c.text(50, 70, a, 8)

    def footer():
        c.text(300, 815, f"{s}  ·  Page {page_no[0]} / {pages_total}", 7, align="right")

    top()
    c.text(50, 120, "BILL TO", 8, True)
    c.text(50, 132, inv.customer, 9)
    c.text(50, 144, "ap@" + inv.customer.split()[0].lower() + ".example", 8)
    meta = []
    if inv.number:
        meta.append(("Invoice No:", inv.number))
    meta += [("Invoice Date:", fmt_date(inv.issued, date_style)),
             ("Due Date:", fmt_date(inv.issued + timedelta(days=30), date_style)), ("Currency:", inv.currency)]
    for k, (lab, v) in enumerate(meta):
        c.text(440, 120 + 12 * k, lab, 8, True, "right")
        c.text(446, 120 + 12 * k, v, 8)

    def header(y):
        for x, t, al in [(50, "#", "left"), (70, "SKU", "left"), (130, "Description", "left"), (380, "Qty", "right"),
                         (395, "Unit", "left"), (480, "Unit Price", "right"), (545, "Amount", "right")]:
            c.text(x, y, t, 8, True, al)
        c.line(50, y + 4, 545)
        return y + 16

    def row(i, it, y):
        c.text(50, y, str(i + 1), 8)
        c.text(70, y, it.sku, 8)
        c.text(130, y, it.description, 8)
        c.text(380, y, str(it.quantity), 8, align="right")
        c.text(395, y, "pcs", 8)
        c.text(480, y, money(it.unit_price, style), 8, align="right")
        c.text(545, y, money(it.amount, style), 8, align="right")

    def cont():
        footer()
        page_no[0] += 1
        top()
        c.text(50, 110, f"Invoice No: {inv.number} (continued)", 9)
        return 135

    y = _items_paged(c, inv, header(210), row, header, cont)
    y += 14
    c.line(380, y - 10, 545)
    tot = [("Subtotal", inv.subtotal)] + [(ch.label, ch.amount) for ch in inv.charges]
    tot += [(f"Sales Tax ({inv.tax_rate.normalize():f}%)", inv.tax), ("TOTAL DUE", inv.total)]
    for lab, v in tot:
        big = lab == "TOTAL DUE"
        c.text(470, y, lab, 9 if big else 8, big, "right")
        c.text(545, y, with_currency(v, inv, style, placement), 9 if big else 8, big, "right")
        y += 13
    c.text(50, y + 30, "Payment Details", 8, True)
    c.text(50, y + 42, f"Citibank N.A.  -  IBAN: GB29 NWBK 6016 1331 9268 {inv.issued.day:02d}", 7)
    footer()


def layout_german(c: Canvas, inv: Inv, *, date_style="de", style="de", placement="suffix_symbol", pages_total=1):
    s, a = inv.supplier
    c.text(50, 55, s, 14, True)
    c.text(545, 55, "RECHNUNG", 18, True, "right")
    c.text(50, 70, a, 8)
    c.text(50, 120, "Rechnungsempfänger", 8, True)
    c.text(50, 132, inv.customer, 9)
    y = 120
    if inv.number:
        c.text(380, y, f"Rechnungsnr.: {inv.number}", 9)
        y += 12
    c.text(380, y, f"Datum: {fmt_date(inv.issued, date_style)}", 9)
    c.text(380, y + 12, f"Währung: {inv.currency}", 9)

    def header(y):
        for x, t, al in [(50, "Pos.", "left"), (80, "Bezeichnung", "left"), (370, "Menge", "right"),
                         (460, "Einzelpreis", "right"), (545, "Gesamt", "right")]:
            c.text(x, y, t, 8, True, al)
        c.line(50, y + 4, 545)
        return y + 16

    def row(i, it, y):
        c.text(50, y, str(i + 1), 8)
        c.text(80, y, it.description, 8)
        c.text(370, y, str(it.quantity), 8, align="right")
        c.text(460, y, money(it.unit_price, style), 8, align="right")
        c.text(545, y, money(it.amount, style), 8, align="right")

    y = _items_paged(c, inv, header(200), row, header, lambda: 80) + 14
    names = {"shipping": "Versandkosten", "discount": "Rabatt"}
    tot = [("Nettobetrag", inv.subtotal)]
    tot += [(names[ch.kind] + (ch.label[ch.label.find(" ("):] if "(" in ch.label else ""), ch.amount)
            for ch in inv.charges]
    tot += [(f"USt. ({money(inv.tax_rate, 'de')}%)", inv.tax), ("Gesamtbetrag", inv.total)]
    for lab, v in tot:
        big = lab == "Gesamtbetrag"
        c.text(470, y, lab, 9 if big else 8, big, "right")
        c.text(545, y, with_currency(v, inv, style, placement), 9 if big else 8, big, "right")
        y += 13


def layout_header_row(c: Canvas, inv: Inv, *, date_style="short", style="en", placement="symbol", pages_total=1):
    s, a = inv.supplier
    c.text(50, 55, s, 14, True)
    c.text(545, 55, "TAX INVOICE", 16, True, "right")
    c.text(50, 70, a, 8)
    cols = [(52, "INVOICE #", inv.number or ""), (190, "ISSUED", fmt_date(inv.issued, date_style)),
            (320, "PAYMENT DUE", fmt_date(inv.issued + timedelta(days=45), date_style)), (460, "CURRENCY:", inv.currency)]
    for x, lab, v in cols:
        if lab == "INVOICE #" and not v:
            continue
        c.text(x, 115, lab, 8, True)
        c.text(x, 128, v, 9)
    c.text(50, 160, "Invoice To", 8, True)
    c.text(50, 172, inv.customer, 9)

    def header(y):
        for x, t, al in [(50, "Item Description", "left"), (300, "Code", "left"), (430, "Rate", "right"),
                         (475, "Qty", "right"), (545, "Line Total", "right")]:
            c.text(x, y, t, 8, True, al)
        c.line(50, y + 4, 545)
        return y + 16

    def row(i, it, y):
        c.text(50, y, it.description, 8)
        c.text(300, y, it.sku, 8)
        c.text(430, y, money(it.unit_price, style), 8, align="right")
        c.text(475, y, str(it.quantity), 8, align="right")
        c.text(545, y, money(it.amount, style), 8, align="right")

    y = _items_paged(c, inv, header(215), row, header, lambda: 80) + 14
    tot = [("Net Amount", inv.subtotal)] + [(ch.label, ch.amount) for ch in inv.charges]
    tot += [(f"VAT {inv.tax_rate.normalize():f}%", inv.tax), ("Amount Payable", inv.total)]
    for lab, v in tot:
        big = lab == "Amount Payable"
        c.text(470, y, lab, 9 if big else 8, big, "right")
        c.text(545, y, with_currency(v, inv, style, placement), 9 if big else 8, big, "right")
        y += 13


def layout_receipt(c: Canvas, inv: Inv, *, date_style="uk", style="en", placement="symbol", pages_total=1):
    s, a = inv.supplier
    c.text(50, 60, s, 13, True)
    c.text(50, 75, a, 8)
    y = 105
    if inv.number:
        c.text(50, y, f"Receipt No: {inv.number}", 9)
        y += 12
    c.text(50, y, f"Date: {fmt_date(inv.issued, date_style)}", 9)
    y += 25
    for it in inv.items:
        c.text(50, y, it.description, 9)
        c.text(330, y, f"{it.quantity} x {money(it.unit_price, style)}", 9, align="right")
        c.text(420, y, money(it.amount, style), 9, align="right")
        y += 13
    y += 10
    tot = [("Subtotal", inv.subtotal)] + [(ch.label, ch.amount) for ch in inv.charges]
    tot += [(f"Tax {inv.tax_rate.normalize():f}%", inv.tax), ("TOTAL", inv.total)]
    for lab, v in tot:
        c.text(50, y, lab, 10, lab == "TOTAL")
        c.text(420, y, with_currency(v, inv, style, placement), 10, lab == "TOTAL", "right")
        y += 14
    c.text(50, y + 20, "Thank you for your purchase!", 9)


def layout_unseen(c: Canvas, inv: Inv, *, date_style="long", style="en", placement="code", pages_total=1):
    """Labels never used while the text rules were written."""
    s, a = inv.supplier
    c.text(50, 60, "Invoice", 20, True)
    c.text(545, 60, s, 11, True, "right")
    c.text(545, 74, a, 8, align="right")
    y = 110
    if inv.number:
        c.text(50, y, "Invoice ref", 8, True)
        c.text(130, y, inv.number, 9)
        y += 13
    c.text(50, y, "Tax point", 8, True)
    c.text(130, y, fmt_date(inv.issued, date_style), 9)
    c.text(330, 110, "Invoice to", 8, True)
    c.text(330, 123, inv.customer, 9)

    def header(y):
        for x, t, al in [(50, "Details", "left"), (360, "Units", "right"), (450, "Each", "right"), (545, "Net", "right")]:
            c.text(x, y, t, 8, True, al)
        c.line(50, y + 4, 545)
        return y + 16

    def row(i, it, y):
        c.text(50, y, it.description, 9)
        c.text(360, y, str(it.quantity), 9, align="right")
        c.text(450, y, money(it.unit_price, style), 9, align="right")
        c.text(545, y, money(it.amount, style), 9, align="right")

    y = _items_paged(c, inv, header(170), row, header, lambda: 80) + 14
    tot = [("Goods total", inv.subtotal)] + [(ch.label, ch.amount) for ch in inv.charges]
    tot += [(f"VAT @ {inv.tax_rate.normalize():f}%", inv.tax), ("Total payable", inv.total)]
    for lab, v in tot:
        c.text(400, y, lab, 9, lab == "Total payable")
        c.text(545, y, with_currency(v, inv, style, placement), 9, lab == "Total payable", "right")
        y += 14


LAYOUTS: dict[str, tuple[Callable, dict, str]] = {
    # name: (renderer, kwargs, invoice-number style)
    "classic": (layout_classic, {}, "dash"),
    "two_column": (layout_two_column, {}, "prefix"),
    "german": (layout_german, {}, "de"),
    "header_row": (layout_header_row, {}, "prefix"),
    "receipt": (layout_receipt, {}, "receipt"),
    "unseen": (layout_unseen, {}, "slash"),
}
LAYOUT_OPTIONS = {  # per-layout variety: (date styles, currencies, amount placement)
    "classic": (["iso", "long", "short"], ["USD", "GBP", "EUR"], ["symbol", "code"]),
    "two_column": (["iso"], ["USD", "BDT", "GBP"], ["symbol"]),
    "german": (["de"], ["EUR"], ["suffix_symbol"]),
    "header_row": (["short", "long"], ["USD", "GBP", "EUR"], ["symbol"]),
    "receipt": (["uk", "iso"], ["GBP", "USD"], ["symbol"]),
    "unseen": (["long", "short"], ["USD", "GBP"], ["code"]),
}


def draw(c: Canvas, inv: Inv, layout: str, rng: random.Random, pages_total: int = 1) -> None:
    fn, _, _ = LAYOUTS[layout]
    dates, _, placements = LAYOUT_OPTIONS[layout]
    style = "de" if layout == "german" else "en"
    fn(c, inv, date_style=rng.choice(dates), style=style, placement=rng.choice(placements), pages_total=pages_total)


# ------------------------------------------------------------------ formats ----------


def rasterize(pdf: Path, dpi: int) -> list[np.ndarray]:
    doc = pymupdf.open(pdf)
    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy())
    return out


def to_scanned_pdf(src: Path, dst: Path, seed: int) -> None:
    doc = pymupdf.open()
    for i, img in enumerate(rasterize(src, 200)):
        img = factory.degrade(img, angle=0.4, noise=6, seed=seed + i)
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=cv2.imencode(".png", img)[1].tobytes())
    doc.save(dst)


def to_photo(src: Path, dst: Path, seed: int) -> None:
    img = rasterize(src, 170)[0]
    img = factory.degrade(img, angle=2.5, blur=0.7, noise=9, contrast=0.7, jpeg_quality=55, seed=seed)
    Image.fromarray(img).save(dst, quality=92, dpi=(170, 170))


def to_png(src: Path, dst: Path) -> None:
    Image.fromarray(rasterize(src, 200)[0]).save(dst, dpi=(200, 200))


# ------------------------------------------------------------------ builder ----------

ERROR_TYPES = ["TOTAL_MISMATCH", "LINE_ITEM_MISMATCH", "MISSING_REQUIRED_FIELD", "DATE_IN_FUTURE"]


def build(out: Path, seed: int = 7, per_layout: int = 10) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_tmp"
    tmp.mkdir(exist_ok=True)
    m = Maker(seed)
    rng = m.rng
    docs: list[dict] = []
    printed: list[tuple[Inv, str]] = []  # for duplicates

    def add(file: str, fmt: str, layout: str, invoices: list[Inv], pages: int = 1):
        docs.append({"file": file, "format": fmt, "layout": layout, "expect": "ok", "pages": pages,
                     "invoices": [i.label() for i in invoices]})

    # 1) one invoice per file, every layout x format
    formats = ["digital_pdf"] * 5 + ["scanned_pdf", "scanned_pdf", "photo_jpg", "photo_jpg", "png"]
    k = 0
    for layout in LAYOUTS:
        dates, currencies, _ = LAYOUT_OPTIONS[layout]
        for j in range(per_layout):
            fmt = formats[j % len(formats)]
            doc_type = "receipt" if layout == "receipt" else ("credit_note" if rng.random() < 0.06 else "invoice")
            inv = m.invoice(number_style=LAYOUTS[layout][2], currency=rng.choice(currencies),
                            n_items=(1, 5) if layout == "receipt" else (1, 9), doc_type=doc_type)
            if layout == "receipt":
                inv.customer = None  # a till receipt names no customer
            if rng.random() < 0.22:
                m.plant(inv, rng.choice(ERROR_TYPES if layout != "receipt" else ERROR_TYPES[:2] + ERROR_TYPES[3:]))
            k += 1
            stem = f"{k:03d}_{layout}_{fmt}"
            src = tmp / f"{stem}.pdf"
            c = Canvas()
            draw(c, inv, layout, rng)
            c.save(src)
            if fmt == "digital_pdf":
                dst = out / f"{stem}.pdf"
                src.replace(dst)
            elif fmt == "scanned_pdf":
                dst = out / f"{stem}.pdf"
                to_scanned_pdf(src, dst, k)
            elif fmt == "photo_jpg":
                dst = out / f"{stem}.jpg"
                to_photo(src, dst, k)
            else:
                dst = out / f"{stem}.png"
                to_png(src, dst)
            add(dst.name, fmt, layout, [inv])
            printed.append((inv, layout))

    # 2) multi-invoice PDFs (a batch export), one with an invoice spanning two pages
    for b in range(5):
        c = Canvas()
        invs, first = [], True
        for j in range(rng.randint(3, 5)):
            layout = rng.choice(["classic", "two_column", "german", "header_row"])
            big = b == 0 and j == 1
            inv = m.invoice(number_style=LAYOUTS[layout][2], currency=rng.choice(LAYOUT_OPTIONS[layout][1]),
                            n_items=(40, 50) if big else (2, 8))
            if big:
                layout = "two_column"
            if rng.random() < 0.15:
                m.plant(inv, rng.choice(["TOTAL_MISMATCH", "LINE_ITEM_MISMATCH"]))
            if not first:
                c.new_page()
            first = False
            draw(c, inv, layout, rng, pages_total=2 if big else 1)
            invs.append(inv)
        name = f"batch_{b + 1:02d}_{len(invs)}_invoices.pdf"
        c.save(out / name)
        add(name, "multi_invoice_pdf", "mixed", invs, pages=len(pymupdf.open(out / name)))

    # 3) duplicates: invoices already seen, re-sent as a different file / format
    for j in range(4):
        orig, layout = printed[rng.randrange(0, min(20, len(printed)))]
        if orig.errors:
            continue
        dup = Inv(**{**orig.__dict__, "errors": ["DUPLICATE_INVOICE"]})
        c = Canvas()
        draw(c, dup, layout, rng)
        name = f"dup_{j + 1:02d}_{layout}.pdf"
        c.save(out / name)
        add(name, "digital_pdf", layout, [dup])

    # 4) spreadsheet registers (header row + one invoice per row)
    for j in range(3):
        rows = [m.invoice(number_style="dash", currency=rng.choice(["USD", "EUR", "GBP"]), charges=False)
                for _ in range(rng.randint(6, 12))]
        for r in rows:
            r.customer = None
            r.items = []
        name = f"register_{j + 1:02d}.xlsx"
        _xlsx(out / name, rows)
        add(name, "xlsx", "register", rows)
    for j in range(2):
        rows = [m.invoice(number_style="de", currency="EUR", charges=False) for _ in range(rng.randint(5, 9))]
        for r in rows:
            r.customer = None
            r.items = []
        name = f"export_{j + 1:02d}.csv"
        _csv_de(out / name, rows)
        add(name, "csv", "register", rows)

    # 5) broken / unsupported files: must be rejected cleanly
    for maker, name in [(factory.empty_file, "broken_empty.pdf"), (factory.random_bytes, "broken_noise.pdf"),
                        (factory.garbage_with_pdf_header, "broken_fake.pdf"), (factory.encrypted_pdf, "broken_locked.pdf"),
                        (factory.corrupted_xlsx, "broken_damaged.xlsx"), (factory.encrypted_xlsx_container,
                                                                         "broken_locked.xlsx"),
                        (factory.docx_like, "broken_letter.docx"), (factory.html_as_xls, "broken_report.xls"),
                        (factory.blank_png, "broken_blank_scan.png")]:
        maker(out / name)
        docs.append({"file": name, "format": "broken", "layout": "-", "expect": "fail", "pages": 0, "invoices": []})

    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    labels = {"seed": seed, "generator": "evaluation.dataset", "documents": docs}
    (out / "labels.json").write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
    return labels


def _xlsx(path: Path, rows: list[Inv]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws.append(["Invoice No", "Invoice Date", "Supplier", "Net Amount", "VAT", "Total", "Currency"])
    for r in rows:
        ws.append([r.number, r.issued, r.supplier[0], float(r.subtotal), float(r.tax), float(r.total), r.currency])
    wb.save(path)


def _csv_de(path: Path, rows: list[Inv]) -> None:
    with open(path, "w", encoding="cp1252", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Rechnungsnr", "Rechnungsdatum", "Lieferant", "Nettobetrag", "MwSt", "Gesamtbetrag", "Währung"])
        for r in rows:
            w.writerow([r.number, r.issued.strftime("%d.%m.%Y"), r.supplier[0], money(r.subtotal, "de"),
                        money(r.tax, "de"), money(r.total, "de"), r.currency])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="eval_data")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--per-layout", type=int, default=10)
    args = ap.parse_args()
    labels = build(Path(args.out), args.seed, args.per_layout)
    docs = labels["documents"]
    n_inv = sum(len(d["invoices"]) for d in docs)
    print(f"wrote {len(docs)} documents ({n_inv} labeled invoices) + labels.json to {args.out}")


if __name__ == "__main__":
    main()
