"""Task 7 - the evaluation harness itself, run on a small generated labeled set.

Doubles as an end-to-end regression test: if a change breaks reading of a layout
the rules handle today, the accuracy floor below fails.
"""

from __future__ import annotations

import json

import pytest

from evaluation.dataset import build
from evaluation.run import evaluate, match, metrics, same


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("eval")
    labels = build(out, seed=11, per_layout=1)  # one digital PDF per layout + batches, sheets, broken files
    run = evaluate(out, llm_provider="none", only=None, limit=None)
    return labels, metrics(run)


def test_labels_are_complete(small_run):
    labels, _ = small_run
    docs = labels["documents"]
    assert {d["expect"] for d in docs} == {"ok", "fail"}
    for d in docs:
        for inv in d["invoices"]:
            assert set(inv) >= {"invoice_number", "invoice_date", "supplier_name", "total", "currency",
                                "line_items", "charges", "expected_findings"}
    json.dumps(labels)


def test_broken_files_are_all_rejected_and_nothing_crashes(small_run):
    _, m = small_run
    assert m["documents"]["broken_correctly_rejected"] == 100.0
    assert m["documents"]["crashes"] == 0


def test_accuracy_floor_on_layouts_the_rules_know(small_run):
    _, m = small_run
    by_layout = m["extraction"]["by_layout"]
    for layout in ("classic", "two_column", "german", "header_row", "register", "mixed"):
        assert by_layout[layout]["all_fields"] >= 90, (layout, by_layout[layout])
    assert m["line_items"]["precision"] >= 95


def test_scoring_rules():
    assert same("total", "225.50", "225.5")
    assert same("supplier_name", "Müller & Söhne GmbH", "MÜLLER & SÖHNE GMBH")
    assert not same("supplier_name", "Initech LLC", "Initech")  # no fuzzy credit
    assert same("customer_name", None, None) and not same("customer_name", None, "Someone")
    assert not same("invoice_date", "2026-03-15", None)


def test_matching_prefers_invoice_number_then_total():
    from docintel.extraction.schema import ExtractedField, FieldStatus, InvoiceRecord

    def rec(number, total):
        f = lambda v: ExtractedField(value=v, status=FieldStatus.FOUND) if v else ExtractedField()  # noqa: E731
        return InvoiceRecord(index=0, invoice_number=f(number), total=f(total))

    a, b = rec("INV-1", "10.00"), rec(None, "20.00")
    pairs = match([{"invoice_number": "INV-2", "total": "20.00"}, {"invoice_number": "inv 1", "total": "99"}], [a, b])
    assert pairs[0][1] is b and pairs[1][1] is a
