"""Deterministic parsing of the raw strings found in documents.

The LLM is only ever trusted to *locate* a value; turning "1.234,50 €" into
``Decimal("1234.50")`` and "15/03/2024" into ``2024-03-15`` happens here, in code,
so numbers are never rounded, reformatted or "fixed" by the model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# ---------------------------------------------------------------- text ----------

_WS = re.compile(r"\s+")


def norm_text(s: str) -> str:
    """Case/whitespace/Unicode-insensitive form used for grounding comparisons."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    return _WS.sub(" ", s).strip()


def compact(s: str) -> str:
    """``norm_text`` without any whitespace (tolerates OCR spacing glitches)."""
    return _WS.sub("", norm_text(s))


def clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = _WS.sub(" ", str(value)).strip(" \t:;,-|")
    return s or None


# ----------------------------------------------------------- currency ----------

ISO_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CNY", "INR", "PKR", "AUD", "CAD", "CHF", "SEK", "NOK",
    "DKK", "PLN", "CZK", "HUF", "RON", "TRY", "AED", "SAR", "QAR", "KWD", "BHD", "OMR",
    "ZAR", "NZD", "SGD", "HKD", "KRW", "MXN", "BRL", "RUB", "ILS", "EGP", "NGN", "KES",
    "THB", "MYR", "IDR", "PHP", "VND", "BDT", "LKR",
}
# Symbol -> (code, unambiguous?). "$" is used by many countries, so it is a guess.
CURRENCY_SYMBOLS: dict[str, tuple[str, bool]] = {
    "€": ("EUR", True), "£": ("GBP", True), "₹": ("INR", True), "₨": ("PKR", False),
    "¥": ("JPY", False), "₩": ("KRW", True), "₺": ("TRY", True), "₽": ("RUB", True),
    "₪": ("ILS", True), "₦": ("NGN", True), "฿": ("THB", True), "₫": ("VND", True),
    "US$": ("USD", True), "A$": ("AUD", True), "C$": ("CAD", True), "NZ$": ("NZD", True),
    "S$": ("SGD", True), "HK$": ("HKD", True), "R$": ("BRL", True), "$": ("USD", False),
    "৳": ("BDT", True),
}
# Abbreviations written with letters ("Tk 1,800.00"); matched only as whole words.
CURRENCY_ABBREVIATIONS: dict[str, tuple[str, bool]] = {"tk": ("BDT", True), "rs": ("INR", False)}
_ABBR_RE = re.compile(r"(?<![A-Za-z])(Tk|TK|Rs|RS)\.?(?![A-Za-z])")
CURRENCY_WORDS = {
    "euro": "EUR", "euros": "EUR", "dollar": "USD", "dollars": "USD", "pound": "GBP",
    "pounds": "GBP", "sterling": "GBP", "rupee": "INR", "rupees": "INR", "yen": "JPY",
    "franc": "CHF", "francs": "CHF", "dirham": "AED", "riyal": "SAR",
}
_SYMBOL_RE = re.compile("|".join(re.escape(s) for s in sorted(CURRENCY_SYMBOLS, key=len, reverse=True)))
_CODE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(ISO_CURRENCIES)) + r")(?![A-Za-z])")


@dataclass
class CurrencyGuess:
    code: Optional[str]
    certain: bool
    raw: Optional[str] = None


def parse_currency(text: Optional[str]) -> CurrencyGuess:
    """Find a currency in a short string ("USD", "€ 12,00", "225.50 US Dollars")."""
    if not text:
        return CurrencyGuess(None, False)
    upper = text.upper()
    m = _CODE_RE.search(upper)
    if m:
        return CurrencyGuess(m.group(1), True, m.group(1))
    m = _SYMBOL_RE.search(text)
    if m:
        code, certain = CURRENCY_SYMBOLS[m.group(0)]
        return CurrencyGuess(code, certain, m.group(0))
    m = _ABBR_RE.search(text)
    if m:
        code, certain = CURRENCY_ABBREVIATIONS[m.group(1).lower()]
        return CurrencyGuess(code, certain, m.group(0))
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in CURRENCY_WORDS:
            # "US dollars" is certain, a bare "dollars" is not
            code = CURRENCY_WORDS[word]
            certain = code != "USD" or "us" in text.lower().split()
            return CurrencyGuess(code, certain, word)
    return CurrencyGuess(None, False)


# ------------------------------------------------------------- amounts ----------

_AMOUNT_RE = re.compile(r"[-−(]?\s*\d[\d.,'\s  ]*\)?-?")


@dataclass
class AmountParse:
    value: Optional[Decimal]
    currency: Optional[str] = None
    issues: list[str] = field(default_factory=list)


def detect_decimal_separator(text: str) -> Optional[str]:
    """Guess the document's decimal separator from its amounts ("1.234,50" -> ",")."""
    comma = len(re.findall(r"\d,\d{2}(?!\d)", text))
    dot = len(re.findall(r"\d\.\d{2}(?!\d)", text))
    if comma > dot:
        return ","
    if dot > comma:
        return "."
    return None


def _to_decimal(num: str, decimal_hint: Optional[str]) -> Optional[Decimal]:
    num = num.replace("'", "").replace(" ", "").replace(" ", "").replace(" ", "")
    if not num or not re.fullmatch(r"\d[\d.,]*", num):
        return None
    has_dot, has_comma = "." in num, "," in num
    if has_dot and has_comma:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        groups = num.split(sep)
        if len(groups) > 2:
            # 1.234.567 / 1,234,567: thousands only. But "634.225.90" (OCR read the comma
            # as a dot) ends in a 2-digit group, which can never be a thousands group.
            middle_ok = all(len(g) == 3 for g in groups[1:-1])
            dec = sep if middle_ok and len(groups[-1]) == 2 else None
        elif len(groups[1]) == 3:
            # 1,234 / 1.234 - thousands unless the document says this is the decimal mark
            dec = sep if decimal_hint == sep else None
        else:
            dec = sep
    else:
        dec = None
    if dec is None:
        digits = num.replace(".", "").replace(",", "")
    else:
        thousands = "," if dec == "." else "."
        whole, _, frac = num.rpartition(dec)
        if thousands in frac:
            return None
        # "634.225.90": the earlier dots are (misread) thousands separators too
        digits = whole.replace(thousands, "").replace(dec, "") + "." + frac
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def parse_amount(raw: Any, decimal_hint: Optional[str] = None) -> AmountParse:
    """Parse "1,234.50", "1.234,50 €", "(12.00)", "USD 99", 225.5 -> Decimal."""
    if raw is None or raw == "":
        return AmountParse(None)
    if isinstance(raw, bool):
        return AmountParse(None, issues=["INVALID_NUMBER"])
    if isinstance(raw, (int, float, Decimal)):
        if isinstance(raw, float) and raw != raw:  # NaN
            return AmountParse(None, issues=["INVALID_NUMBER"])
        return AmountParse(Decimal(str(raw)))
    text = str(raw).strip()
    cur = parse_currency(text)
    # Strict: apart from a currency marker the string must be just the number, so
    # "#DIV/0!", "N/A" or "see note 3" are rejected instead of yielding 0 or 3.
    bare = _CODE_RE.sub("", _ABBR_RE.sub("", text).upper())
    bare = _SYMBOL_RE.sub("", bare)
    bare = re.sub(r"\b(?:" + "|".join(CURRENCY_WORDS) + r")\b", "", bare, flags=re.I)
    bare = bare.replace("US", "").strip()
    m = _AMOUNT_RE.fullmatch(bare) or _AMOUNT_RE.fullmatch(bare.rstrip("-").strip())
    if not m or not re.fullmatch(r"[-−(]?\s*[\d.,'\s  ]+\)?-?", bare):
        return AmountParse(None, cur.code, ["INVALID_NUMBER"])
    token = bare.strip()
    negative = token.startswith(("-", "−", "(")) or text.rstrip().endswith("-")
    token = token.lstrip("-−( ").rstrip(" .,)-")
    value = _to_decimal(token, decimal_hint)
    if value is None:
        return AmountParse(None, cur.code, ["INVALID_NUMBER"])
    return AmountParse(-value if negative else value, cur.code)


def fmt_decimal(d: Optional[Decimal]) -> Optional[str]:
    """Exact JSON-safe form: Decimal('205') -> '205.00', '225.5000' -> '225.50',
    '12.345' -> '12.345' (extra decimals kept only when they carry information)."""
    if d is None:
        return None
    n = d.normalize()
    if n.as_tuple().exponent >= -2:
        return f"{d:.2f}"
    return format(n, "f")


# --------------------------------------------------------------- dates ----------

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTHS.update({"sept": 9, "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5,
                "juni": 6, "juli": 7, "okt": 10, "dez": 12, "janvier": 1, "février": 2,
                "mars": 3, "avril": 4, "juin": 6, "juillet": 7, "août": 8, "octobre": 10,
                "décembre": 12})


def _month(word: str) -> Optional[int]:
    w = word.lower().rstrip(".")
    return _MONTHS.get(w) or _MONTHS.get(w[:4]) or _MONTHS.get(w[:3])


@dataclass
class DateParse:
    value: Optional[date]
    candidates: list[date] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _year(y: str) -> int:
    n = int(y)
    return n + (2000 if n < 70 else 1900) if len(y) == 2 else n


def _safe(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(raw: Any) -> DateParse:
    """Parse a printed date. Day/month order that cannot be decided is reported, not guessed."""
    if raw is None or raw == "":
        return DateParse(None)
    if isinstance(raw, datetime):
        return DateParse(raw.date())
    if isinstance(raw, date):
        return DateParse(raw)
    text = str(raw).strip()

    # ISO / year-first: 2024-03-15, 2024/03/15, 2024.03.15, 2024-03-15T00:00:00
    m = re.search(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text)
    if m:
        d = _safe(int(m[1]), int(m[2]), int(m[3]))
        return DateParse(d) if d else DateParse(None, issues=["INVALID_DATE"])

    # Month names: 15 March 2024, 15-Mar-24, March 15, 2024, 15. März 2024
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s.\-/]+([A-Za-zÀ-ÿ]{3,9})\.?[\s.\-/,]+(\d{2,4})\b", text)
    if m and _month(m[2]):
        d = _safe(_year(m[3]), _month(m[2]), int(m[1]))
        return DateParse(d) if d else DateParse(None, issues=["INVALID_DATE"])
    m = re.search(r"\b([A-Za-zÀ-ÿ]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{2,4})\b", text)
    if m and _month(m[1]):
        d = _safe(_year(m[3]), _month(m[1]), int(m[2]))
        return DateParse(d) if d else DateParse(None, issues=["INVALID_DATE"])

    # Numeric: 15.03.2024 (dots are day-first by convention), 03/04/2024 (ambiguous)
    m = re.search(r"\b(\d{1,2})([./-])(\d{1,2})\2(\d{2,4})\b", text)
    if m:
        a, sep, b, y = int(m[1]), m[2], int(m[3]), _year(m[4])
        if sep == ".":
            d = _safe(y, b, a)
            return DateParse(d) if d else DateParse(None, issues=["INVALID_DATE"])
        options = {d for d in (_safe(y, b, a), _safe(y, a, b)) if d}
        if not options:
            return DateParse(None, issues=["INVALID_DATE"])
        if len(options) == 1:
            return DateParse(options.pop())
        return DateParse(None, sorted(options), ["AMBIGUOUS_DATE"])

    return DateParse(None, issues=["INVALID_DATE"])
