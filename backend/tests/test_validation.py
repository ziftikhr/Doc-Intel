"""Task 3 - validation tests."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import factory
import pytest

from docintel.extraction import ExtractionConfig, extract_invoices
from docintel.extraction.schema import (
    Charge,
    ExtractedField,
    ExtractionResult,
    FieldIssue,
    FieldStatus,
    InvoiceRecord,
    LineItem,
)
from docintel.processing import process_file
from docintel.validation import (
    DuplicateIndex,
    InvoiceRef,
    ValidatedDocument,
    ValidationConfig,
    apply_validation,
    validate_batch,
    validate_document,
    validate_invoice,
)
from docintel.validation.checks import norm_invoice_number, norm_party

TODAY = date(2026, 11, 24)
MISSING = object()


def F(value, *, status=FieldStatus.FOUND, conf=0.97, raw=None, issues=(), candidates=()):
    return ExtractedField(value=value, raw=raw if raw is not None else value, status=status,
                          confidence=conf, source="llm+rules", page=1, evidence=f"... {value} ...",
                          issues=list(issues), candidates=list(candidates))


def item(desc, qty, unit, amt, issues=()):
    return LineItem(description=desc, quantity=qty, unit_price=unit, amount=amt, page=1,
                    evidence=f"{desc} {qty} {unit} {amt}", source="llm+rules", confidence=0.97,
                    issues=list(issues))


GOOD_ITEMS = [item("Widget A", "10", "12.50", "125.00"), item("Widget B", "4", "20.00", "80.00")]


def charge(label, amt, kind="other"):
    return Charge(kind=kind, label=label, amount=amt, raw=amt, page=1, evidence=f"{label} {amt}",
                  source="llm+rules", confidence=0.97)


def inv(index=0, document_type="invoice", items=MISSING, review_reasons=(), charges=(), **fields) -> InvoiceRecord:
    """A correct invoice (205.00 + 20.50 = 225.50); override fields, None = missing."""
    base = dict(invoice_number=F("INV-2024-0042"), invoice_date=F("2024-03-15"),
                supplier_name=F("ACME Supplies Ltd"), customer_name=F("Globex Corporation"),
                subtotal=F("205.00"), tax=F("20.50"), total=F("225.50"), currency=F("USD"))
    for k, v in fields.items():
        base[k] = ExtractedField() if v is None else (F(v) if isinstance(v, str) else v)
    return InvoiceRecord(index=index, document_type=document_type, charges=list(charges),
                         line_items=list(GOOD_ITEMS if items is MISSING else items),
                         review_reasons=list(review_reasons), pages=[1], method="llm+rules", **base)


def doc(*invoices, filename="a.pdf", document_id="doc-a", status="success", issues=()) -> ExtractionResult:
    return ExtractionResult(document_id=document_id, filename=filename, status=status,
                            invoices=list(invoices), issues=list(issues),
                            extracted_at=datetime(2026, 11, 24, tzinfo=timezone.utc))


def check(record, **cfg):
    return validate_invoice(record, ValidationConfig(**cfg), today=TODAY)


def codes(v):
    return [i.code for i in v.issues]


# ------------------------------------------------------------- happy path ----------


def test_correct_invoice_is_valid_and_every_check_reports():
    v = check(inv())
    assert v.status == "valid" and not v.needs_review and v.issues == []
    assert set(v.checks) == {"required_fields", "field_quality", "amounts", "dates", "currency",
                             "totals", "tax_rate", "line_items", "parties"}
    assert set(v.checks.values()) == {"passed"}


# --------------------------------------------------------- required fields ----------


def test_missing_total_makes_invoice_invalid():
    v = check(inv(total=None))
    assert v.status == "invalid"
    [i] = [i for i in v.issues if i.code == "MISSING_REQUIRED_FIELD"]
    assert i.field == "total" and i.severity == "error" and "an invoice" in i.message
    assert v.checks["totals"] == "skipped"  # no total -> arithmetic cannot run, not a fake pass


@pytest.mark.parametrize("name", ["invoice_number", "invoice_date", "supplier_name"])
def test_each_required_field(name):
    v = check(inv(**{name: None}))
    assert v.status == "invalid" and ("MISSING_REQUIRED_FIELD", name) in [(i.code, i.field) for i in v.issues]


def test_missing_recommended_field_only_needs_review():
    v = check(inv(customer_name=None))
    assert v.status == "needs_review" and codes(v) == ["MISSING_FIELD"]


def test_receipt_without_number_or_customer_is_valid():
    v = check(inv(document_type="receipt", invoice_number=None, customer_name=None))
    assert v.status == "valid", v.issues


def test_unreadable_required_amount_is_an_error():
    total = F(None, status=FieldStatus.UNCERTAIN, raw="#DIV/0!", issues=[FieldIssue.INVALID_NUMBER])
    v = check(inv(total=total))
    assert v.status == "invalid"
    [i] = v.issues
    assert i.code == "UNREADABLE_FIELD" and i.details["raw"] == "#DIV/0!"


def test_ambiguous_date_needs_review_not_invalid():
    d = F(None, status=FieldStatus.UNCERTAIN, raw="03/04/2024", issues=[FieldIssue.AMBIGUOUS_DATE],
          candidates=["2024-03-04", "2024-04-03"])
    v = check(inv(invoice_date=d))
    assert v.status == "needs_review"
    [i] = v.issues
    assert i.code == "AMBIGUOUS_DATE" and "2024-03-04 or 2024-04-03" in i.message


def test_unreadable_optional_field_is_a_warning():
    tax = F(None, status=FieldStatus.UNCERTAIN, raw="see note", issues=[FieldIssue.INVALID_NUMBER])
    v = check(inv(tax=tax))
    assert v.status == "needs_review" and "UNREADABLE_FIELD" in codes(v)


# -------------------------------------------------------------- arithmetic ----------


def test_total_mismatch_explains_the_numbers():
    v = check(inv(total="252.50"))
    assert v.status == "invalid" and v.checks["totals"] == "failed"
    [i] = [i for i in v.issues if i.code == "TOTAL_MISMATCH"]
    assert i.details == {"subtotal": "205.00", "tax": "20.50", "expected_total": "225.50",
                         "total": "252.50", "difference": "27.00"}
    assert "205.00 + tax 20.50 = 225.50" in i.message


def test_one_cent_rounding_is_tolerated_two_cents_is_not():
    assert check(inv(total="225.51")).checks["totals"] == "passed"
    assert check(inv(total="225.52")).checks["totals"] == "failed"
    assert check(inv(total="225.52"), amount_tolerance=Decimal("0.05")).checks["totals"] == "passed"


def test_no_tax_line():
    assert check(inv(tax=None, total="205.00")).status == "valid"
    v = check(inv(tax=None))
    assert v.status == "needs_review" and "TOTAL_UNVERIFIED" in codes(v)


def test_tax_inclusive_amounts_are_a_warning_not_a_mismatch():
    v = check(inv(subtotal="225.50", tax="20.50", total="225.50", items=[]))
    assert codes(v) == ["TAX_POSSIBLY_INCLUDED"] and v.status == "needs_review"


def test_unusual_tax_rate_and_tax_above_total():
    v = check(inv(subtotal="100.00", tax="50.00", total="150.00", items=[]))
    assert codes(v) == ["UNUSUAL_TAX_RATE"] and v.issues[0].details["rate_percent"] == "50.0"
    v = check(inv(subtotal=None, tax="300.00", total="225.50", items=[]))
    assert "TAX_EXCEEDS_TOTAL" in codes(v) and v.status == "invalid"


def test_line_item_quantity_times_price():
    items = [item("Widget A", "10", "12.50", "152.00"), item("Widget B", "4", "20.00", "80.00")]
    v = check(inv(items=items, subtotal="232.00", total="252.50"))
    assert "LINE_ITEM_MISMATCH" in codes(v)
    [i] = [i for i in v.issues if i.code == "LINE_ITEM_MISMATCH"]
    assert i.field == "line_items[0].amount" and i.details["expected"] == "125.00"


def test_rounded_unit_price_is_tolerated():
    # 3 x 33.333 printed as 33.33 -> 99.99 vs a line amount of 100.00
    v = check(inv(items=[item("Hours", "3", "33.33", "100.00")], subtotal="100.00", tax="0", total="100.00"))
    assert "LINE_ITEM_MISMATCH" not in codes(v)


def test_line_items_must_add_up_to_subtotal():
    v = check(inv(items=[GOOD_ITEMS[0]]))  # 125.00 of 205.00 captured
    [i] = [i for i in v.issues if i.code == "LINE_ITEMS_SUM_MISMATCH"]
    assert i.details["difference"] == "80.00" and v.status == "needs_review"


def test_line_items_compared_with_total_minus_tax_when_no_subtotal():
    assert check(inv(subtotal=None)).status == "valid"


def test_tax_inclusive_line_items_are_accepted():
    items = [item("Widget A", "1", "225.50", "225.50")]
    v = check(inv(items=items))
    assert codes(v) == ["LINE_ITEMS_INCLUDE_TAX"] and v.status == "valid"  # info only


def test_line_item_without_amount_skips_the_sum():
    v = check(inv(items=[GOOD_ITEMS[0], item("Shipping", None, None, None)]))
    assert codes(v) == ["LINE_ITEMS_INCOMPLETE"] and v.status == "valid"


def test_unreadable_line_item():
    bad = item("Digital design package", "1", None, None, issues=[FieldIssue.INVALID_NUMBER])
    v = check(inv(items=[bad], subtotal=None, tax=None, total="4500.00"))
    assert "LINE_ITEM_UNREADABLE" in codes(v) and v.status == "needs_review"


# ---------------------------------------------- other charges (shipping, discount) ----------


def test_shipping_is_part_of_the_total():
    # the batch invoice ED-2026-01487: 12,540.11 + 15.00 shipping + 1,034.56 tax = 13,589.67
    ship = charge("Shipping & Handling", "15.00", "shipping")
    items = [item("Server RAM", "1", "12540.11", "12540.11")]
    rec = inv(items=items, subtotal="12540.11", tax="1034.56", total="13589.67", charges=[ship])
    v = check(rec)
    assert v.status == "valid" and v.checks["totals"] == "passed", v.issues
    # without the shipping line the same numbers do not add up
    v = check(inv(items=items, subtotal="12540.11", tax="1034.56", total="13589.67"))
    [i] = [i for i in v.issues if i.code == "TOTAL_MISMATCH"]
    assert i.details["difference"] == "15.00" and "shipping line was not captured" in i.message


def test_discount_reduces_the_total():
    disc = charge("Discount (2%)", "-4.10", "discount")
    v = check(inv(charges=[disc], tax="20.09", total="221.00"))  # 205.00 - 4.10 + 20.10 -> 221.00 within 1 cent
    assert v.checks["totals"] == "passed", v.issues


def test_mismatch_message_lists_the_charges():
    ship = charge("Freight", "15.00", "shipping")
    disc = charge("Discount", "-5.00", "discount")
    v = check(inv(charges=[ship, disc], total="240.00"))
    [i] = [i for i in v.issues if i.code == "TOTAL_MISMATCH"]
    assert "Subtotal 205.00 + Freight 15.00 - Discount 5.00 + tax 20.50 = 235.50" in i.message
    assert i.details["charges"] == [{"label": "Freight", "amount": "15.00"}, {"label": "Discount", "amount": "-5.00"}]


def test_discount_printed_above_the_subtotal():
    # items 205.00, discount -5.00, subtotal 200.00 (already net of the discount), tax 20.00
    disc = charge("Discount", "-5.00", "discount")
    v = check(inv(charges=[disc], subtotal="200.00", tax="20.00", total="220.00"))
    assert v.checks["totals"] == "passed" and v.checks["line_items"] == "passed", v.issues


def test_shipping_without_tax_line():
    ship = charge("Shipping", "10.00", "shipping")
    assert check(inv(charges=[ship], tax=None, total="215.00", items=[])).checks["totals"] == "passed"


def test_unreadable_charge_is_reported():
    bad = Charge(kind="fee", label="Handling fee", amount=None, raw="n/a", issues=[FieldIssue.INVALID_NUMBER])
    v = check(inv(charges=[bad]))
    assert "CHARGE_UNREADABLE" in codes(v) and v.checks["totals"] == "warning"


def test_no_line_items_is_skipped_not_failed():
    v = check(inv(items=[]))
    assert v.status == "valid" and v.checks["line_items"] == "skipped"


# ----------------------------------------------------------------- amounts ----------


def test_negative_amounts():
    v = check(inv(subtotal="-205.00", tax="-20.50", total="-225.50", items=[]))
    assert v.status == "invalid" and codes(v).count("NEGATIVE_AMOUNT") == 3
    credit = check(inv(document_type="credit_note", subtotal="-205.00", tax="-20.50", total="-225.50", items=[]))
    assert credit.status == "valid", credit.issues


def test_zero_and_huge_amounts():
    assert "ZERO_TOTAL" in codes(check(inv(subtotal="0", tax="0", total="0", items=[])))
    huge = check(inv(subtotal="205002050.00", tax="0", total="205002050.00", items=[]))
    assert "AMOUNT_TOO_LARGE" in codes(huge)


# -------------------------------------------------------- dates / currency ----------


@pytest.mark.parametrize("value,code,status", [
    ("2027-03-01", "DATE_IN_FUTURE", "needs_review"),
    ("1985-01-01", "IMPLAUSIBLE_DATE", "invalid"),
    ("2019-01-01", "OLD_INVOICE", "needs_review"),
    ("2026-11-25", None, "valid"),  # tomorrow: time-zone tolerance
])
def test_dates(value, code, status):
    v = check(inv(invoice_date=value))
    assert v.status == status and (codes(v) == [code] if code else not v.issues)


def test_currency_checks():
    v = check(inv(currency=F("USD", raw="$", issues=[FieldIssue.CURRENCY_INFERRED])))
    assert codes(v) == ["CURRENCY_INFERRED"] and v.status == "needs_review"
    assert check(inv(currency="XYZ")).status == "invalid"
    assert check(inv(currency=None)).checks["currency"] == "skipped"


# ------------------------------------------------------ extraction doubts ----------


def test_low_confidence_and_conflicts_from_extraction():
    v = check(inv(total=F("225.50", conf=0.45)))
    assert codes(v) == ["LOW_CONFIDENCE"]
    v = check(inv(total=F("225.50", conf=0.55, issues=[FieldIssue.SOURCE_CONFLICT], candidates=["225.50", "228.50"])))
    assert codes(v) == ["SOURCE_CONFLICT"]
    v = check(inv(total=F("225.50", conf=0.8, issues=[FieldIssue.LOW_OCR_CONFIDENCE])))
    assert codes(v) == ["LOW_OCR_QUALITY"]


def test_extraction_notes_are_carried_over():
    v = check(inv(review_reasons=["rules-only extraction (LLM_NO_CREDIT)", "total: SOURCE_CONFLICT"]))
    assert codes(v) == ["EXTRACTION_NOTE"] and "LLM_NO_CREDIT" in v.issues[0].message


def test_revalidating_a_validated_result_gives_the_same_findings():
    result = doc(inv(total="252.50", customer_name=None, review_reasons=["rules-only extraction (LLM_NO_CREDIT)"]))
    result.invoices[0].extraction_notes = ["rules-only extraction (LLM_NO_CREDIT)"]
    first = validate_document(result, today=TODAY)
    saved = apply_validation(result, first)  # rewrites review_reasons with "[error] ..." messages
    again = validate_document(ExtractionResult.model_validate_json(saved.model_dump_json()), today=TODAY)
    assert codes(again.invoices[0]) == codes(first.invoices[0])
    assert codes(first.invoices[0]).count("EXTRACTION_NOTE") == 1


def test_spreadsheet_row_missing_customer_is_only_info():
    row = inv(customer_name=None, currency=None, items=[])
    row.method = "spreadsheet"
    v = check(row)
    assert v.status == "valid" and codes(v) == ["MISSING_FIELD", "MISSING_FIELD"]
    assert {i.severity for i in v.issues} == {"info"}


def test_party_checks():
    v = check(inv(customer_name="Acme Supplies Limited"))
    assert codes(v) == ["SUPPLIER_EQUALS_CUSTOMER"]
    assert codes(check(inv(invoice_number="Description"))) == ["SUSPICIOUS_INVOICE_NUMBER"]


# -------------------------------------------------------------- duplicates ----------


def test_normalisation_for_matching():
    assert norm_invoice_number("INV-0042") == norm_invoice_number("inv 42") == norm_invoice_number("INV/00042")
    assert norm_invoice_number("INV-0042") != norm_invoice_number("INV-0420")
    assert norm_invoice_number("INV-2024-0042") == norm_invoice_number("inv 2024/042")
    assert norm_invoice_number("INV-2024-0042") != norm_invoice_number("INV-42")
    assert norm_party("ACME Supplies Ltd.") == norm_party("Acme Supplies Limited") == norm_party("ACME SUPPLIES")


def _batch(*docs):
    return validate_batch(docs, today=TODAY, sources=[f"src{i}" for i in range(len(docs))])


def test_same_invoice_in_two_documents():
    first, second = _batch(doc(inv(), filename="a.pdf", document_id="a"),
                           doc(inv(invoice_number="inv 2024/042", supplier_name="Acme Supplies Limited"),
                               filename="b.pdf", document_id="b"))
    assert first.status == "valid"  # the first occurrence is never flagged
    [i] = second.invoices[0].issues
    assert i.code == "DUPLICATE_INVOICE" and i.details["duplicate_of"] == "a.pdf (invoice 1)"
    assert second.status == "invalid" and second.invoices[0].checks["duplicates"] == "failed"


def test_same_number_different_total_is_a_conflict():
    _, second = _batch(doc(inv(), document_id="a"),
                       doc(inv(subtotal="305.00", tax="30.50", total="335.50", items=[]), document_id="b"))
    [i] = second.invoices[0].issues
    assert i.code == "DUPLICATE_NUMBER_CONFLICT" and "335.50 vs 225.50" in i.message


def test_same_number_from_different_suppliers_is_fine():
    _, second = _batch(doc(inv(), document_id="a"), doc(inv(supplier_name="Initech GmbH"), document_id="b"))
    assert second.status == "valid"


def test_weak_duplicate_evidence_is_a_warning():
    _, second = _batch(doc(inv(), document_id="a"), doc(inv(supplier_name=None), document_id="b"))
    assert "POSSIBLE_DUPLICATE" in codes(second.invoices[0])
    _, second = _batch(doc(inv(invoice_number=None), document_id="a"),
                       doc(inv(invoice_number=None), document_id="b"))
    assert "POSSIBLE_DUPLICATE" in codes(second.invoices[0])


def test_duplicate_inside_one_document():
    rep = validate_document(doc(inv(index=0), inv(index=1)), today=TODAY)
    assert rep.invoices[0].status == "valid" and codes(rep.invoices[1]) == ["DUPLICATE_INVOICE"]


def test_same_file_twice_is_reported_at_document_level():
    a = doc(inv(), document_id="same-hash")
    _, second = _batch(a, a)
    assert [i.code for i in second.issues] == ["DUPLICATE_DOCUMENT"]


def test_known_invoices_from_storage_and_no_self_match():
    stored = InvoiceRef.from_record(inv(), source="db:17", document_id="old", filename="march.pdf")
    [rep] = validate_batch([doc(inv())], known=[stored], today=TODAY)
    assert rep.invoices[0].issues[0].details["duplicate_of"] == "march.pdf (invoice 1)"
    # re-validating the stored record itself must not flag it against itself
    index = DuplicateIndex([stored])
    assert index.check(stored) == []


# --------------------------------------------------------------- documents ----------


def test_failed_and_empty_documents():
    rep = validate_document(doc(status="failed", issues=["SOURCE_FAILED: broken"]), today=TODAY)
    assert rep.status == "failed" and rep.needs_review and rep.issues[0].code == "EXTRACTION_FAILED"
    # the AI read it and found no invoice -> a normal outcome
    rep = validate_document(doc(status="no_invoice_found", issues=["NO_INVOICE_FOUND: none"]), today=TODAY)
    assert rep.status == "no_invoices" and not rep.needs_review
    # only the rules looked at it -> doubtful
    rep = validate_document(doc(status="no_invoice_found",
                                issues=["LLM_NOT_CONFIGURED: extracted with rules only", "NO_INVOICE_FOUND: none"]),
                            today=TODAY)
    assert rep.status == "no_invoices" and rep.needs_review


def test_document_status_is_the_worst_invoice():
    rep = validate_document(doc(inv(index=0), inv(index=1, invoice_number="INV-2", customer_name=None),
                                inv(index=2, invoice_number="INV-3", total="1.00")), today=TODAY)
    assert [v.status for v in rep.invoices] == ["valid", "needs_review", "invalid"]
    assert rep.status == "invalid" and rep.counts() == {"valid": 1, "needs_review": 1, "invalid": 1}


def test_apply_validation_updates_review_flags_and_round_trips():
    result = doc(inv(index=0), inv(index=1, invoice_number="INV-2", total="252.50"))
    out = apply_validation(result, validate_document(result, today=TODAY))
    assert [r.needs_review for r in out.invoices] == [False, True] and out.needs_review
    assert out.invoices[1].review_reasons[0].startswith("[error] Subtotal 205.00 + tax 20.50")
    back = ValidatedDocument.model_validate_json(out.model_dump_json())
    assert back.validation.invoices[1].status == "invalid"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("DOCINTEL_VALIDATION_AMOUNT_TOLERANCE", "0.05")
    monkeypatch.setenv("DOCINTEL_VALIDATION_MAX_AGE_DAYS", "30")
    cfg = ValidationConfig.from_env()
    assert cfg.amount_tolerance == Decimal("0.05") and cfg.max_age_days == 30


# ---------------------------------------------------------- end to end / CLI ----------


def test_pipeline_output_validates(tmp_path):
    processed = process_file(factory.digital_pdf(tmp_path / "inv.pdf", pages=2))
    result = extract_invoices(processed, ExtractionConfig(llm_provider="none"), llm=None)
    rep = validate_document(result, today=TODAY)
    assert rep.status == "valid", [(v.invoice_number, v.issues) for v in rep.invoices]
    assert len(rep.invoices) == 2


def test_cli_extract_then_validate_finds_cross_file_duplicate(tmp_path, capsys):
    from docintel.processing.cli import main

    pdf = factory.digital_pdf(tmp_path / "inv.pdf", pages=1)
    out1, out2, report = tmp_path / "r1.json", tmp_path / "r2.json", tmp_path / "report.json"
    assert main(["extract", str(pdf), "--no-llm", "-o", str(out1)]) == 0
    data = json.loads(out1.read_text(encoding="utf-8"))
    assert data["validation"]["status"] == "valid"
    assert "1 valid, 0 need review, 0 invalid" in capsys.readouterr().err

    # the same invoice photographed / re-sent: a second extraction result
    data["document_id"], data["filename"] = "other-hash", "copy.pdf"
    out2.write_text(json.dumps(data), encoding="utf-16")  # PowerShell-style file
    assert main(["validate", str(out1), str(out2), "-o", str(report)]) == 0
    err = capsys.readouterr().err
    assert "DUPLICATE" not in err and "already received in" in err
    reports = json.loads(report.read_text(encoding="utf-8"))
    assert [r["status"] for r in reports] == ["valid", "invalid"]
    assert reports[1]["invoices"][0]["issues"][0]["code"] == "DUPLICATE_INVOICE"
