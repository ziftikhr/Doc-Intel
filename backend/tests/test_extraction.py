"""Task 2 - invoice extraction: parsing, rules, grounding, LLM handling, spreadsheets.

The LLM is replaced by ``FakeLLM`` so the suite is deterministic and needs no API
key. ``test_live_openai`` exercises the real API when DOCINTEL_LIVE_LLM=1.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import factory
import pytest

from docintel.extraction import (
    ExtractionConfig,
    FieldIssue,
    FieldStatus,
    LLMCallResult,
    LLMError,
    OpenAIInvoiceLLM,
    RawInvoice,
    extract_invoices,
)
from docintel.extraction.normalize import parse_amount, parse_currency, parse_date
from docintel.extraction.rules import extract_rules
from docintel.extraction.schema import RawCharge, RawField, RawLineItem
from docintel.extraction.segment import segment_pages
from docintel.processing import process_file
from docintel.processing.models import (
    DocumentMetadata,
    DocumentStatus,
    ExtractionMethod,
    FileType,
    OCRInfo,
    PageResult,
    ProcessedDocument,
)

INVOICE_TEXT = "\n".join(factory.INVOICE_LINES)
CFG = ExtractionConfig(llm_provider="none", llm_concurrency=1)


# ----------------------------------------------------------------- helpers ----------


def make_doc(*texts: str, method=ExtractionMethod.DIGITAL, ocr_conf: float | None = None,
             status=DocumentStatus.SUCCESS) -> ProcessedDocument:
    pages = [PageResult(number=i, label=f"Page {i}", method=method, text=t, char_count=len(t),
                        ocr=OCRInfo(engine="fake", confidence=ocr_conf, preprocessing="none")
                        if ocr_conf is not None else None)
             for i, t in enumerate(texts, 1)]
    meta = DocumentMetadata(filename="test.pdf", path="test.pdf", size_bytes=1, sha256="x" * 64,
                            declared_extension=".pdf", detected_type=FileType.PDF, mime_type="application/pdf",
                            page_count=len(pages))
    return ProcessedDocument(document_id="x" * 64, status=status, metadata=meta, pages=pages,
                             started_at=datetime.now(timezone.utc))


def F(value=None, evidence=None, page=None) -> RawField:
    return RawField(value=value, evidence=evidence, page=page)


def raw_invoice(**overrides) -> RawInvoice:
    """What a well-behaved LLM returns for factory.INVOICE_LINES."""
    base = dict(
        document_type="invoice",
        invoice_number=F("INV-2024-0042", "Invoice Number: INV-2024-0042", 1),
        invoice_date=F("2024-03-15", "Invoice Date: 2024-03-15", 1),
        supplier_name=F("ACME Supplies Ltd", "ACME Supplies Ltd", 1),
        customer_name=F("Globex Corporation", "Bill To: Globex Corporation", 1),
        subtotal=F("205.00", "Subtotal: 205.00", 1),
        tax=F("20.50", "Tax (10%): 20.50", 1),
        total=F("225.50", "Total: 225.50 USD", 1),
        currency=F("USD", "Total: 225.50 USD", 1),
        line_items=[
            RawLineItem(description="Widget A", quantity="10", unit_price="12.50", amount="125.00",
                        evidence="Widget A             10        12.50       125.00", page=1),
            RawLineItem(description="Gadget B", quantity="2", unit_price="40.00", amount="80.00",
                        evidence="Gadget B              2        40.00        80.00", page=1),
        ],
        other_charges=[],
    )
    base.update(overrides)
    return RawInvoice(**base)


class FakeLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, *responses, error: LLMError | None = None):
        self.responses = list(responses)
        self.error = error
        self.calls: list[list[tuple[int, str]]] = []

    def extract(self, pages):
        self.calls.append(pages)
        if self.error:
            raise self.error
        invoices = self.responses.pop(0) if self.responses else []
        return LLMCallResult(invoices=invoices, input_tokens=100, output_tokens=50)


# ---------------------------------------------------------------- normalize ----------


@pytest.mark.parametrize("raw,hint,expected", [
    ("205.00", None, "205.00"),
    ("1,234.50", None, "1234.50"),
    ("1.234,50 €", None, "1234.50"),
    ("€ 12,00", None, "12.00"),
    ("$1,234,567.89", None, "1234567.89"),
    ("1 234,50", None, "1234.50"),
    ("(12.00)", None, "-12.00"),
    ("12.00-", None, "-12.00"),
    ("USD 99", None, "99"),
    ("1.234", ",", "1234"),  # document uses ',' as decimal mark -> '.' groups thousands
    ("1.234", ".", "1.234"),  # document uses '.' as decimal mark
    ("12,5", None, "12.5"),
    (225.5, None, "225.5"),
    ("634.225.90", None, "634225.90"),  # OCR read "634,225.90" with a dot for the comma
    ("12.705.78", ",", "12705.78"),  # OCR read "12.705,78" the same way
    ("1.234.567", None, "1234567"),  # genuine thousands groups stay thousands
])
def test_parse_amount(raw, hint, expected):
    assert parse_amount(raw, hint).value == Decimal(expected)


@pytest.mark.parametrize("raw", ["#DIV/0!", "N/A", "see note 3", "", None, True, "12 apples"])
def test_parse_amount_rejects_non_numbers(raw):
    assert parse_amount(raw).value is None


@pytest.mark.parametrize("raw,expected", [
    ("2024-03-15", "2024-03-15"),
    ("2024-03-15T00:00:00", "2024-03-15"),
    ("15.03.2024", "2024-03-15"),
    ("15/03/2024", "2024-03-15"),  # 15 cannot be a month -> unambiguous
    ("March 15, 2024", "2024-03-15"),
    ("15 Mar 2024", "2024-03-15"),
    ("15. März 2024", "2024-03-15"),
    ("15-Mar-24", "2024-03-15"),
])
def test_parse_date(raw, expected):
    assert parse_date(raw).value.isoformat() == expected


def test_ambiguous_date_is_not_guessed():
    p = parse_date("03/04/2024")
    assert p.value is None
    assert "AMBIGUOUS_DATE" in p.issues
    assert {d.isoformat() for d in p.candidates} == {"2024-03-04", "2024-04-03"}


def test_invalid_date():
    assert parse_date("31/02/2024").value is None


@pytest.mark.parametrize("raw,code,certain", [
    ("USD", "USD", True), ("€ 12,00", "EUR", True), ("225.50 EUR", "EUR", True),
    ("$100", "USD", False), ("£5", "GBP", True), ("hello", None, False),
])
def test_parse_currency(raw, code, certain):
    g = parse_currency(raw)
    assert (g.code, g.certain) == (code, certain)


# -------------------------------------------------------------------- rules ----------


def test_rules_extract_sample_invoice():
    r = extract_rules([(1, INVOICE_TEXT)])
    assert r.invoice_number.value == "INV-2024-0042"
    assert r.invoice_date.value == "2024-03-15"
    assert r.supplier_name.value == "ACME Supplies Ltd"
    assert r.customer_name.value == "Globex Corporation"
    assert r.subtotal.value == "205.00"
    assert r.tax.value == "20.50"  # not the 10 % rate
    assert r.total.value.startswith("225.50")
    assert r.currency.value == "USD"
    assert [(i.description, i.amount) for i in r.line_items] == [("Widget A", "125.00"), ("Gadget B", "80.00")]


def test_rules_ignore_text_about_invoices():
    guide = ("Invoice guide\nA sample invoice is a structured record.\n"
             "Reference   SAMPLE-INVOICE-1001\nStatus   Fictional sample")
    assert extract_rules([(1, guide)]) is None


def test_rules_do_not_read_invoice_inside_other_words():
    r = extract_rules([(1, "Invoice\nRef: SAMPLE-INVOICE-1001\nInvoice Date: 2024-01-01\nTotal: 10.00")])
    assert r is not None and r.invoice_number.value is None


# ------------------------------------------------------------- segmentation ----------


def test_segments_split_on_new_invoice_number_and_keep_continuations():
    pages = make_doc(
        "Invoice No: A-100\nItems...", "Page 2 of 2\nmore items\nTotal: 10.00",
        "Invoice No: B-200\nTotal: 5.00", "Terms and conditions",
    ).pages
    segs = segment_pages(pages)
    assert [s.page_numbers for s in segs] == [[1, 2], [3, 4]]


def test_segments_are_capped():
    pages = make_doc(*["no invoice number here"] * 20).pages
    assert [len(s.pages) for s in segment_pages(pages, max_pages=8)] == [8, 8, 4]


# ------------------------------------------------------- pipeline (rules) ----------


def test_rules_only_extraction():
    res = extract_invoices(make_doc(INVOICE_TEXT), CFG, llm=None)
    assert res.status == "success"
    [inv] = res.invoices
    s = inv.to_simple()
    assert s["invoice_number"] == "INV-2024-0042"
    assert (s["subtotal"], s["tax"], s["total"], s["currency"]) == ("205.00", "20.50", "225.50", "USD")
    assert inv.total.source == "rules" and inv.total.page == 1
    assert inv.total.evidence == "Total: 225.50 USD"
    assert len(inv.line_items) == 2


def test_missing_total_is_not_computed():
    text = INVOICE_TEXT.replace("Total: 225.50 USD", "")
    [inv] = extract_invoices(make_doc(text), CFG, llm=None).invoices
    assert inv.subtotal.value == "205.00" and inv.tax.value == "20.50"
    assert inv.total.value is None  # NOT 225.50
    assert inv.total.status == FieldStatus.MISSING


def test_multi_invoice_pdf(tmp_path):
    doc = process_file(factory.digital_pdf(tmp_path / "three.pdf", pages=3))
    res = extract_invoices(doc, CFG, llm=None)
    assert [i.invoice_number.value for i in res.invoices] == ["INV-2024-0042", "INV-2024-0043", "INV-2024-0044"]
    assert [i.pages for i in res.invoices] == [[1], [2], [3]]


def test_document_without_invoice():
    res = extract_invoices(make_doc("Dear team,\nthe meeting moved to Tuesday."), CFG, llm=None)
    assert res.status == "no_invoice_found" and res.needs_review and not res.invoices


def test_failed_document():
    res = extract_invoices(make_doc(status=DocumentStatus.FAILED), CFG, llm=None)
    assert res.status == "failed" and res.needs_review


def test_low_ocr_confidence_lowers_confidence():
    [good] = extract_invoices(make_doc(INVOICE_TEXT, method=ExtractionMethod.OCR, ocr_conf=0.95),
                              CFG, llm=None).invoices
    [bad] = extract_invoices(make_doc(INVOICE_TEXT, method=ExtractionMethod.OCR, ocr_conf=0.4),
                             CFG, llm=None).invoices
    assert bad.total.confidence < good.total.confidence
    assert FieldIssue.LOW_OCR_CONFIDENCE in bad.total.issues
    assert bad.needs_review


# --------------------------------------------------------- pipeline (LLM) ----------


def test_llm_and_rules_agree():
    llm = FakeLLM([raw_invoice()])
    res = extract_invoices(make_doc(INVOICE_TEXT), CFG, llm=llm)
    [inv] = res.invoices
    assert res.status == "success" and not inv.needs_review
    assert inv.method == "llm+rules"
    assert inv.total.source == "llm+rules" and inv.total.confidence >= 0.95
    assert inv.line_items[0].source == "llm+rules"
    assert res.llm.calls == 1 and res.llm.input_tokens == 100


def test_invented_values_are_rejected():
    llm = FakeLLM([raw_invoice(
        customer_name=F("Wayne Enterprises", "Bill To: Wayne Enterprises", 1),  # not in the text
        total=F("999.00", "Total: 999.00", 1),
        line_items=[RawLineItem(description="Flux capacitor", quantity="1", unit_price="5", amount="5",
                                evidence="Flux capacitor 1 5 5", page=1)],
    )])
    [inv] = extract_invoices(make_doc(INVOICE_TEXT), CFG, llm=llm).invoices
    # customer: LLM value rejected, but the rules' grounded value is used
    assert inv.customer_name.value == "Globex Corporation" and inv.customer_name.source == "rules"
    assert inv.total.value == "225.50" and inv.total.source == "rules"
    assert all(li.description != "Flux capacitor" for li in inv.line_items)
    assert any("discarded" in r for r in inv.review_reasons)


def test_invented_value_with_no_alternative_is_flagged():
    text = INVOICE_TEXT.replace("Bill To: Globex Corporation", "")
    llm = FakeLLM([raw_invoice(customer_name=F("Globex Corporation", "Bill To: Globex Corporation", 1))])
    [inv] = extract_invoices(make_doc(text), CFG, llm=llm).invoices
    assert inv.customer_name.value is None
    assert inv.customer_name.status == FieldStatus.UNCERTAIN
    assert inv.customer_name.issues == [FieldIssue.UNGROUNDED]
    assert inv.needs_review


def test_page_comes_from_the_text_not_the_llm():
    doc = make_doc("ACME Supplies Ltd\nInvoice Number: X-1", "Subtotal: 10.00\nTotal: 11.00 EUR")
    llm = FakeLLM([raw_invoice(invoice_number=F("X-1", "Invoice Number: X-1", 7),
                               total=F("11.00", "Total: 11.00 EUR", 1), line_items=[])])
    [inv] = extract_invoices(doc, CFG, llm=llm).invoices
    assert inv.invoice_number.page == 1
    assert inv.total.page == 2


def test_reformatted_number_is_still_grounded():
    text = INVOICE_TEXT.replace("Total: 225.50 USD", "Total: 1,225.50 USD")
    llm = FakeLLM([raw_invoice(total=F("1225.50", "Total: 1,225.50 USD", 1))])
    [inv] = extract_invoices(make_doc(text), CFG, llm=llm).invoices
    assert inv.total.value == "1225.50" and inv.total.source == "llm+rules"


def test_conflict_between_llm_and_rules_is_flagged():
    text = INVOICE_TEXT + "\nAmount due: 200.00"
    llm = FakeLLM([raw_invoice(total=F("225.50", "Total: 225.50 USD", 1))])
    [inv] = extract_invoices(make_doc(text), CFG, llm=llm).invoices
    assert FieldIssue.SOURCE_CONFLICT in inv.total.issues
    assert set(inv.total.candidates) == {"225.50", "200.00"}
    assert inv.needs_review


def test_ambiguous_date_from_llm():
    text = INVOICE_TEXT.replace("2024-03-15", "03/04/2024")
    llm = FakeLLM([raw_invoice(invoice_date=F("03/04/2024", "Invoice Date: 03/04/2024", 1))])
    [inv] = extract_invoices(make_doc(text), CFG, llm=llm).invoices
    assert inv.invoice_date.value is None and inv.invoice_date.status == FieldStatus.UNCERTAIN
    assert FieldIssue.AMBIGUOUS_DATE in inv.invoice_date.issues
    assert set(inv.invoice_date.candidates) == {"2024-03-04", "2024-04-03"}


def test_llm_finds_two_invoices_on_one_page():
    text = (INVOICE_TEXT + "\n\nBeta Corp\nInvoice Number: B-7\nInvoice Date: 2024-05-01\n"
            "Bill To: Globex Corporation\nTotal: 50.00 EUR")
    second = raw_invoice(invoice_number=F("B-7", "Invoice Number: B-7", 1),
                         invoice_date=F("2024-05-01", "Invoice Date: 2024-05-01", 1),
                         supplier_name=F("Beta Corp", "Beta Corp", 1), subtotal=F(), tax=F(),
                         total=F("50.00", "Total: 50.00 EUR", 1), currency=F("EUR", "Total: 50.00 EUR", 1),
                         line_items=[])
    res = extract_invoices(make_doc(text), CFG, llm=FakeLLM([raw_invoice(), second]))
    assert [(i.invoice_number.value, i.total.value, i.currency.value) for i in res.invoices] == [
        ("INV-2024-0042", "225.50", "USD"), ("B-7", "50.00", "EUR")]


def test_llm_says_not_an_invoice():
    guide = "Invoice guide\nReference   SAMPLE-INVOICE-1001\nTotal pages 10"
    res = extract_invoices(make_doc(guide), CFG, llm=FakeLLM([]))
    assert res.status == "no_invoice_found"


def test_fatal_llm_error_falls_back_to_rules_and_stops_calling():
    doc = make_doc(INVOICE_TEXT, INVOICE_TEXT.replace("0042", "0043"), INVOICE_TEXT.replace("0042", "0044"))
    llm = FakeLLM(error=LLMError("LLM_NO_CREDIT", "no credit", fatal=True))
    res = extract_invoices(doc, CFG, llm=llm)
    assert len(llm.calls) == 1  # circuit breaker: no point retrying a dead account
    assert res.status == "partial"
    assert len(res.invoices) == 3 and all(i.method == "rules" for i in res.invoices)
    assert any("LLM_NO_CREDIT" in i for i in res.issues)
    assert all(i.needs_review for i in res.invoices)


def test_transient_llm_error_only_affects_that_segment():
    class Flaky(FakeLLM):
        def extract(self, pages):
            if len(self.calls) == 0:
                self.calls.append(pages)
                raise LLMError("LLM_TIMEOUT", "slow")
            return super().extract(pages)

    doc = make_doc(INVOICE_TEXT, INVOICE_TEXT.replace("0042", "0043"))
    second = raw_invoice(invoice_number=F("INV-2024-0043", "Invoice Number: INV-2024-0043", 2))
    res = extract_invoices(doc, CFG, llm=Flaky([second]))
    assert [i.method for i in res.invoices] == ["rules", "llm+rules"]
    assert res.status == "partial"


def test_per_minute_rate_limit_is_waited_out_not_fatal():
    class Limited(FakeLLM):
        """Rejects the first two calls with a per-minute limit, like Gemini's free tier."""
        rejected = 0

        def extract(self, pages):
            if Limited.rejected < 2:
                Limited.rejected += 1
                raise LLMError("LLM_RATE_LIMITED", "per-minute limit", retry_after=0.01)
            return super().extract(pages)

    doc = make_doc(INVOICE_TEXT, INVOICE_TEXT.replace("0042", "0043"), INVOICE_TEXT.replace("0042", "0044"))
    answers = [[raw_invoice(invoice_number=F(f"INV-2024-004{n}", f"Invoice Number: INV-2024-004{n}", n - 1))]
               for n in (2, 3, 4)]
    llm = Limited(*answers)
    res = extract_invoices(doc, CFG.with_overrides(llm_concurrency=1), llm=llm)
    assert [i.method for i in res.invoices] == ["llm+rules"] * 3  # every segment still reached the LLM
    assert res.status == "success" and res.llm.failed_calls == 0


def test_rate_limit_that_never_clears_stops_like_a_used_up_quota():
    llm = FakeLLM(error=LLMError("LLM_RATE_LIMITED", "per-minute limit", retry_after=0.001))
    pages = [INVOICE_TEXT.replace("0042", f"00{n}") for n in range(50, 56)]
    cfg = CFG.with_overrides(llm_concurrency=1, llm_rate_limit_retries=1)
    res = extract_invoices(make_doc(*pages), cfg, llm=llm)
    assert len(llm.calls) == 3 * 2  # 3 segments x (1 try + 1 retry), then it gives up
    assert all(i.method == "rules" for i in res.invoices) and len(res.invoices) == 6


def test_currency_inferred_from_dollar_sign_is_uncertain():
    text = "Shop\nReceipt No: R-9\nDate: 2024-01-02\nTotal: $12.00"
    res = extract_invoices(make_doc(text), CFG, llm=None)
    [inv] = res.invoices
    assert inv.currency.value == "USD"
    assert FieldIssue.CURRENCY_INFERRED in inv.currency.issues
    assert inv.needs_review


# ------------------------------------------------------------- spreadsheets ----------


def test_invoice_register_xlsx(tmp_path):
    doc = process_file(factory.multisheet_xlsx(tmp_path / "reg.xlsx"))
    res = extract_invoices(doc, CFG, llm=FakeLLM())
    nums = [i.invoice_number.value for i in res.invoices]
    assert nums == ["00123", "INV-2024-0043", "INV-2024-0044", "INV-2024-0045"]  # leading zeros kept
    first, _, third, fourth = res.invoices
    assert (first.subtotal.value, first.tax.value, first.total.value) == ("205.00", "20.50", "225.50")
    assert first.invoice_date.value == "2024-03-15" and first.method == "spreadsheet"
    assert [li.description for li in first.line_items] == ["Widget A", "Gadget B"]
    assert third.total.status == FieldStatus.MISSING  # formula without a cached value: not computed
    assert fourth.tax.status == FieldStatus.UNCERTAIN and fourth.tax.value is None  # "#DIV/0!"
    assert FieldIssue.INVALID_NUMBER in fourth.tax.issues
    assert first.currency.value is None  # never assumed


def test_german_csv(tmp_path):
    doc = process_file(factory.csv_semicolon_cp1252(tmp_path / "de.csv"))
    res = extract_invoices(doc, CFG, llm=None)
    assert [(i.invoice_number.value, i.supplier_name.value, i.total.value, i.currency.value)
            for i in res.invoices] == [("R-001", "Müller & Söhne", "1234.50", "EUR"),
                                       ("R-002", "Bäckerei Groß", "12.00", "EUR")]


def test_spreadsheet_without_invoice_columns(tmp_path):
    doc = process_file(factory.csv_tab_ragged(tmp_path / "x.tsv"))
    assert extract_invoices(doc, CFG, llm=None).status == "no_invoice_found"


# ------------------------------------------------------------- OpenAI adapter ----------


def _openai_with(fake_parse):
    llm = OpenAIInvoiceLLM("gpt-test", api_key="sk-test")
    llm.client = SimpleNamespace(responses=SimpleNamespace(parse=fake_parse))
    return llm


def test_openai_adapter_parses_structured_output():
    from docintel.extraction import LLMExtraction

    seen = {}

    def parse(**kwargs):
        seen.update(kwargs)
        usage = SimpleNamespace(input_tokens=900, output_tokens=120,
                                input_tokens_details=SimpleNamespace(cached_tokens=800))
        return SimpleNamespace(output_parsed=LLMExtraction(invoices=[raw_invoice()]), usage=usage, status="completed")

    res = _openai_with(parse).extract([(1, INVOICE_TEXT)])
    assert res.invoices[0].invoice_number.value == "INV-2024-0042"
    assert (res.input_tokens, res.output_tokens, res.cached_input_tokens) == (900, 120, 800)
    assert seen["text_format"] is LLMExtraction and seen["store"] is False
    assert '<page number="1">' in seen["input"][1]["content"]
    assert seen["reasoning"] == {"effort": "low"}


def test_openai_no_credit_is_fatal():
    import httpx2
    import openai

    def parse(**_):
        resp = httpx2.Response(429, request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
        raise openai.RateLimitError("You exceeded your current quota", response=resp,
                                    body={"code": "insufficient_quota"})

    with pytest.raises(LLMError) as ei:
        _openai_with(parse).extract([(1, "x")])
    assert ei.value.code == "LLM_NO_CREDIT" and ei.value.fatal


def test_openai_empty_output_is_an_error():
    def parse(**_):
        return SimpleNamespace(output_parsed=None, usage=None, status="incomplete")

    with pytest.raises(LLMError) as ei:
        _openai_with(parse).extract([(1, "x")])
    assert ei.value.code == "LLM_NO_OUTPUT" and not ei.value.fatal


# ------------------------------------------------------------- Gemini adapter ----------


def _gemini_with(fake_generate, model="gemini-2.5-flash"):
    from docintel.extraction.llm import GeminiInvoiceLLM

    llm = GeminiInvoiceLLM(model, api_key="AIza-test")
    llm.client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    return llm


def test_gemini_adapter_parses_json_output():
    from docintel.extraction import LLMExtraction

    seen = {}

    def generate(**kwargs):
        seen.update(kwargs)
        usage = SimpleNamespace(prompt_token_count=900, candidates_token_count=100,
                                thoughts_token_count=50, cached_content_token_count=0)
        text = LLMExtraction(invoices=[raw_invoice()]).model_dump_json()
        return SimpleNamespace(text=text, usage_metadata=usage, candidates=[], prompt_feedback=None)

    res = _gemini_with(generate).extract([(1, INVOICE_TEXT)])
    assert res.invoices[0].invoice_number.value == "INV-2024-0042"
    assert (res.input_tokens, res.output_tokens) == (900, 150)  # thinking tokens count as output
    cfg = seen["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == LLMExtraction.model_json_schema()
    assert cfg.thinking_config.thinking_budget == 1024  # 2.x models: "low" -> token budget
    assert '<page number="1">' in seen["contents"]
    gemini3 = _gemini_with(generate, model="gemini-3-flash")
    assert gemini3._thinking().thinking_level.value == "LOW"


@pytest.mark.parametrize("code,status,message,expected,fatal", [
    (400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", "LLM_AUTH_FAILED", True),
    (404, "NOT_FOUND", "models/gemini-x is not found", "LLM_MODEL_NOT_FOUND", True),
    (429, "RESOURCE_EXHAUSTED", "Quota exceeded for metric: requests per day", "LLM_NO_CREDIT", True),
    (429, "RESOURCE_EXHAUSTED", "Resource has been exhausted (e.g. check quota).", "LLM_RATE_LIMITED", False),
    # Every Gemini 429 mentions billing; a per-minute limit must not stop the document
    (429, "RESOURCE_EXHAUSTED", "You exceeded your current quota, please check your plan and billing details. "
     "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
     "limit: 5, model: gemini-3.5-flash, description: Generate content requests per minute limit exceeded. "
     "Please retry in 41.2s.", "LLM_RATE_LIMITED", False),
    (429, "RESOURCE_EXHAUSTED", "Quota exceeded for metric: ..._free_tier_requests, limit: 0, model: x",
     "LLM_NO_CREDIT", True),
    (503, "UNAVAILABLE", "The model is overloaded", "LLM_SERVER_ERROR", False),
])
def test_gemini_error_mapping(code, status, message, expected, fatal):
    from google.genai import errors

    def generate(**_):
        raise errors.APIError(code, {"error": {"code": code, "status": status, "message": message}})

    with pytest.raises(LLMError) as ei:
        _gemini_with(generate).extract([(1, "x")])
    assert (ei.value.code, ei.value.fatal) == (expected, fatal)


def test_gemini_invalid_json_is_an_error():
    def generate(**_):
        return SimpleNamespace(text='{"invoices": [{"broken"', usage_metadata=None, candidates=[], prompt_feedback=None)

    with pytest.raises(LLMError) as ei:
        _gemini_with(generate).extract([(1, "x")])
    assert ei.value.code == "LLM_INVALID_RESPONSE" and not ei.value.fatal


def test_auto_provider_prefers_gemini_when_its_key_is_set(monkeypatch):
    from docintel.extraction.llm import make_llm

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ExtractionConfig().resolved_provider() == "openai"
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    llm, reason = make_llm(ExtractionConfig())
    assert reason is None and (llm.provider, llm.model) == ("gemini", "gemini-3.5-flash")
    assert ExtractionConfig(llm_provider="openai").resolved_provider() == "openai"


# ---------------------------------------------------------------------- CLI ----------


def test_cli_extract_from_utf16_task1_json(tmp_path, capsys):
    """`docintel inspect x > out.json` in Windows PowerShell writes UTF-16; extract must still read it."""
    from docintel.processing.cli import main

    src = tmp_path / "task1.json"
    src.write_text(make_doc(INVOICE_TEXT).model_dump_json(), encoding="utf-16")
    out = tmp_path / "invoices.json"
    assert main(["extract", str(src), "--no-llm", "-o", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["invoices"][0]["total"]["value"] == "225.50"


# --------------------------------------------------------------------- live ----------


@pytest.mark.llm
@pytest.mark.skipif(os.environ.get("DOCINTEL_LIVE_LLM") != "1", reason="set DOCINTEL_LIVE_LLM=1 to call the API")
def test_live_llm(tmp_path):
    """Uses whichever provider is configured (auto: Gemini if GEMINI_API_KEY is set)."""
    from dotenv import load_dotenv

    load_dotenv(".env")
    doc = process_file(factory.digital_pdf(tmp_path / "inv.pdf", pages=1))
    res = extract_invoices(doc, ExtractionConfig.from_env())
    assert res.status == "success", res.issues
    [inv] = res.invoices
    assert inv.method == "llm+rules"
    assert inv.to_simple()["total"] == "225.50"


def test_gemini_quota_message_is_short_and_stable():
    from docintel.extraction.llm import _short_quota

    raw = ("You exceeded your current quota, please check your plan and billing details. For more "
           "information on this error, head to: https://ai.google.dev/... \n* Quota exceeded for metric: "
           "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: "
           "gemini-3.6-flash, description: Generate content requests per day limit exceeded on Free Tier.\n"
           "Please retry in 16h48m0.887617009s.")
    msg = _short_quota(raw)
    assert msg == "Gemini daily request limit reached (20/day); try again in about 17h"
    assert _short_quota(raw.replace("16h48m0.88", "16h47m59.1")) == msg  # dedupes across calls


# ---------------------------------------------- other charges and item tables ----------

# Layout of the invoice batch (invoice_batch_2026_Q1-Q4.pdf, page 1), slightly shortened.
BATCH_PAGE = """\
Ember Distribution Pvt. Ltd.                    INVOICE
231 Industrial Way, Austin, TX 73301


BILL TO                                                                         Invoice No: ED-2026-01487
Northwind Holdings Ltd.                                                         Invoice Date: 2025-12-25
House 42, Road 11, Banani, Dhaka 1213, Bangladesh                          Due Date: 2026-01-24
ap@northwind-holdings.example                                                 Currency: USD


#   SKU           Description                                       Qty  Unit         Unit Price         Amount

1    IT-5526        Cat6 Patch Cable 2m                                  39  pcs                4.12             160.68
2    IT-2402         Wi-Fi 6 Access Point                                   13  pcs             140.91            1,831.83

                                                                                                Subtotal         $1,992.51
                                                                                Shipping & Handling             $15.00
                                                                                     Discount (2%)          -$39.85
                                                                               Sales Tax (8.25%)          $160.39
                                                     TOTAL DUE      $2,128.05


Payment Details
ING Bank  - IBAN: DE2677 7573 6216 4422 9125 3598
"""


def test_rules_read_item_rows_with_row_number_code_and_unit():
    r = extract_rules([(1, BATCH_PAGE)])
    assert [(i.description, i.quantity, i.unit_price, i.amount) for i in r.line_items] == [
        ("Cat6 Patch Cable 2m", "39", "4.12", "160.68"), ("Wi-Fi 6 Access Point", "13", "140.91", "1,831.83")]
    # the bank line is not an item, the customer is not the "Invoice No" column next to BILL TO
    assert r.customer_name.value == "Northwind Holdings Ltd."
    assert r.supplier_name.value == "Ember Distribution Pvt. Ltd."


def test_rules_read_charges_between_subtotal_and_total():
    r = extract_rules([(1, BATCH_PAGE)])
    assert [(c.label, c.amount) for c in r.other_charges] == [
        ("Shipping & Handling", "$15.00"), ("Discount (2%)", "-$39.85")]
    assert r.tax.value == "$160.39"  # tax stays tax


def test_charges_are_signed_and_the_invoice_validates():
    from docintel.validation import ValidationConfig, validate_invoice

    [inv] = extract_invoices(make_doc(BATCH_PAGE), CFG, llm=None).invoices
    assert [(c.kind, c.label, c.amount) for c in inv.charges] == [
        ("shipping", "Shipping & Handling", "15.00"), ("discount", "Discount (2%)", "-39.85")]
    v = validate_invoice(inv, ValidationConfig())
    assert v.checks["totals"] == "passed" and v.checks["line_items"] == "passed", v.issues


def test_llm_charges_grounded_signed_and_confirmed_by_rules():
    raw = raw_invoice(
        invoice_number=F("ED-2026-01487", "Invoice No: ED-2026-01487", 1),
        subtotal=F("$1,992.51", "Subtotal $1,992.51", 1), tax=F("$160.39", "Sales Tax (8.25%) $160.39", 1),
        total=F("$2,128.05", "TOTAL DUE $2,128.05", 1), line_items=[],
        other_charges=[
            RawCharge(label="Discount (2%)", amount="$39.85", evidence="Discount (2%) -$39.85", page=1),  # no minus
            RawCharge(label="Sales Tax", amount="$160.39", evidence="Sales Tax (8.25%) $160.39", page=1),  # not a charge
            RawCharge(label="Insurance", amount="$99.00", evidence="Insurance $99.00", page=1),  # invented
        ])
    [inv] = extract_invoices(make_doc(BATCH_PAGE), CFG, llm=FakeLLM([raw])).invoices
    got = {c.label: (c.amount, c.source) for c in inv.charges}
    assert got == {"Discount (2%)": ("-39.85", "llm+rules"), "Shipping & Handling": ("15.00", "rules")}
    assert any("1 other charge(s) discarded" in n for n in inv.extraction_notes)


def test_minus_before_currency_symbol_is_kept():
    from docintel.extraction.rules import amounts_in

    assert amounts_in("Amount Payable    -$24.71") == ["-$24.71"]
    assert amounts_in("Total due  $24.71") == ["$24.71"]
    assert parse_amount("-$24.71").value == Decimal("-24.71")


def test_taka_amounts():
    assert parse_amount("Tk 1,800.00").value == Decimal("1800.00")
    assert parse_amount("-Tk 1,234.00").value == Decimal("-1234.00")
    assert parse_currency("Tk 902,934.49").code == "BDT"
    assert parse_currency("Stk. 4").code is None  # "Tk" only as a whole word
