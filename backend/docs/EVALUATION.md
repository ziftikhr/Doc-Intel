# Evaluation (Task 7)

Measured on 2026-11-25 · Windows 11, AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD, Python 3.13.5 · extraction: **text rules only (no AI)** unless stated otherwise.

## Headline numbers

| Metric | Result | What it means |
|---|---|---|
| Extraction accuracy (all header fields) | 91.2% → **91.3%** after fix | 138 labeled invoices × 8 fields, exact match; missed invoices count as wrong |
| Invoices found | 91.3% | every miss is the *unseen* layout, which rules alone cannot read (see below) |
| Invoices fully correct (all 8 fields) | 90.6% → **91.3%** |  |
| Line items (precision / recall) | 100% / 85.7% | 440 labeled rows; qty & unit price correct on 100% of matched rows |
| Document success rate | **86%** | 86 valid documents processed end to end |
| Document failure rate | 14% | valid documents where no invoice was extracted (all *unseen* layout) |
| Broken files rejected cleanly | **100%** | 9 broken/unsupported files |
| Crashes | 0 | exceptions escaping the pipeline |
| Planted errors detected | 85% | 100% on every invoice that was found (misses = unseen-layout invoices) |
| Clean invoices wrongly marked invalid | **0%** | 109 clean invoices |
| Processing time per document | median 24.6 ms, p95 1524.6 ms | digital ≈ 20–30 ms, OCR ≈ 1.3–1.5 s |
| Large real file (1,130 pages) | 11.0 s, 835 invoices | 6,142 pages/min, rules only |

## How it was measured

**Labeled dataset.** No answer key came with the assignment, so `evaluation/dataset.py` generates one: each invoice is first created as data (supplier, items, quantities, prices, shipping, discounts, tax, currency) and then printed into a document; the data is the ground truth (`labels.json`). It covers 6 layouts × digital, scanned, phone-photo and PNG formats, multi-invoice PDFs (one invoice spans two pages), Excel and CSV registers, 4 currencies, `1,234.56` and `1.234,56` number styles, 5 date styles, credit notes and receipts, planted errors, and 9 broken files.

**The *unseen* layout** uses labels that were never used while writing the text rules ("Invoice ref", "Tax point", "Goods total", "Total payable"). It measures how the system copes with a layout nobody prepared it for - that is the AI's job; with rules only it is expected to fail.

**Development vs held-out set.** Bugs were found and fixed while looking at the development set (seed 7). The code was then frozen and a *new* set (seed 2026, 95 documents) was generated and measured once - those are the numbers above. One bug found on the held-out set was fixed afterwards; both figures are shown.

**Scoring.** Extracted invoices are matched to labels by invoice number, then total. Fields must match exactly (only case/spacing/punctuation in names are ignored, amounts compared as decimals, dates as ISO dates). A field that is not printed counts as correct only if nothing was extracted. Each document runs the same path as the API worker: processing → extraction → validation, with one duplicate index for the whole run.

Reproduce:

```powershell
python -m evaluation.dataset --out eval_data_test --seed 2026 --per-layout 12
python -m evaluation.run --data eval_data_test --name rules_test
python -m evaluation.report
```

## Automated tests

`pytest` runs **273 tests** (272 pass offline; 1 live-AI test runs with `DOCINTEL_LIVE_LLM=1`), plus 22 frontend tests (`npm test`).

| Area | Test files | Tests |
|---|---|---|
| Document processing (PDF, type detection, batches) | `test_pdf.py`, `test_detection.py`, `test_batch.py` | 33 |
| OCR (scans, photos, rotation, TIFF, transparency) | `test_images_ocr.py` | 20 |
| Spreadsheet parsing (XLSX, XLS, CSV encodings) | `test_spreadsheets.py` | 12 |
| AI extraction (LLM with fakes, grounding, rules, charges) | `test_extraction.py` | 89 |
| Validation (math, dates, duplicates, damaged files) | `test_validation.py` | 55 |
| API endpoints, background jobs, search | `test_api.py` | 30 |
| Failure scenarios (broken files, crashes, limits, end to end) | `test_failures.py` | 29 |
| Evaluation harness (labeled set, scoring) | `test_evaluation.py` | 5 |

## Results in detail (held-out set)

### Extraction accuracy by field

| Field | invoice number | invoice date | supplier name | customer name | subtotal | tax | total | currency |
|---|---|---|---|---|---|---|---|---|
| Accuracy | 91.3% | 91.3% | 91.3% | 91.3% | 91.3% | 91.3% | 90.6% | 91.3% |
| After fix | 91.3% | 91.3% | 91.3% | 91.3% | 91.3% | 91.3% | 91.3% | 91.3% |

### By document format

| Format | Invoices | Found | Field accuracy | Time p50 | Time p95 |
|---|---|---|---|---|---|
| CSV export | 14 | 100% | 100% | 10.3 ms | 107.2 ms |
| Digital PDF | 46 | 84.8% | 84.5% | 22.3 ms | 28.8 ms |
| Multi-invoice PDF | 20 | 100% | 100% | 30 ms | 52.7 ms |
| Phone photo JPG (OCR) | 12 | 83.3% | 83.3% | 1348 ms | 1640.4 ms |
| PNG scan (OCR) | 6 | 83.3% | 83.3% | 1265.6 ms | 1829.4 ms |
| Scanned PDF (OCR) | 12 | 83.3% | 83.3% | 1370.9 ms | 1576.6 ms |
| Excel register | 28 | 100% | 100% | 17.3 ms | 163.5 ms |
| Broken / unsupported | 0 | – | rejected 100% | 1.6 ms | 31.4 ms |

### By layout

| Layout | Invoices | Found | Field accuracy | Fully correct |
|---|---|---|---|---|
| classic | 15 | 100% | 100% | 100% |
| german | 12 | 100% | 100% | 100% |
| header_row | 12 | 100% | 99% | 91.7% |
| mixed | 20 | 100% | 100% | 100% |
| receipt | 12 | 100% | 100% | 100% |
| register | 42 | 100% | 100% | 100% |
| two_column | 13 | 100% | 100% | 100% |
| unseen | 12 | 0% | 0% | 0% |

### Line items and other charges

- Line items: precision **100%**, recall **85.7%** (377 extracted of 440 labeled); quantity × unit price correct on 100% of matched rows.
- Shipping / discounts: signed sum correct on **88.5%** of 52 invoices that have them; 0 invented on invoices without.

### Validation (planted errors)

| Planted error | Detected | Planted | Recall |
|---|---|---|---|
| MISSING_REQUIRED_FIELD | 4 | 4 | 100% |
| TOTAL_MISMATCH | 4 | 4 | 100% |
| LINE_ITEM_MISMATCH | 3 | 3 | 100% |
| DATE_IN_FUTURE | 2 | 5 | 40% |
| DUPLICATE_INVOICE | 4 | 4 | 100% |

Clean invoices: **91.7%** marked valid, **0%** wrongly marked invalid; the rest were sent to review because the currency was printed only as `$` (US, Canadian or Australian dollar?) - asking is the intended behaviour. Every undetected planted error belongs to an unseen-layout invoice that was not extracted at all.

### Processing time

- Mean per stage: processing 459.3 ms, extraction 1.6 ms, validation 0.5 ms - reading the file (OCR) is almost all of the cost.
- This run: 103 pages, 141.0 pages/min, 130.0 documents/min (one process; OCR documents dominate).
- **Real 1,130-page batch** (`invoice_batch_2026_Q1-Q4.pdf`, unlabeled): 1130 pages → 835 invoices in **11.0 s** (processing 6.9 s, extraction 2.4 s, validation 1.7 s). Verdicts: valid 771, invalid 42, needs_review 22.
- Task 1 benchmark (16 CPUs, OCR at 300 DPI): 100-page scanned PDF in 117 s (0.85 pages/s); mixed inbox of 200 files with ~10% deliberately broken: 109.6 documents/min, success 90%, failures were exactly the broken files.

## AI extraction (Gemini)

Measured with `gemini-3.5-flash` on the unseen layout, where rules alone find nothing. The Gemini free tier allows 20 requests/day and the day's quota ran out after **1 document(s)**: that document was read with **all 8 fields correct** in 4.5 s. One document is not a measurement, so no AI accuracy figure is claimed here. To measure it (needs quota or a paid key):

```powershell
python -m evaluation.run --data eval_data_test --name ai_unseen --llm auto --only unseen
python -m evaluation.report
```

In production the AI and the rules run together: when both read the same value its confidence is highest; when the AI is unavailable the rules' results above are what the system delivers.

## Bugs the evaluation found (and fixed)

Field accuracy on the development set went **77.0% → 90.3% → 92.0%** as these were fixed:

| Problem found | Effect | Fix |
|---|---|---|
| PDFs encode `-` as a soft hyphen (U+00AD) | `INV-2026-0042` read as `INV­2026…`: numbers and dates on whole layouts missed | Task 1 maps soft/Unicode hyphens to `-` |
| OCR reads `634,225.90` as `634.225.90` | amount read 100× too large | a final 2-digit group is always the decimal part |
| Minus before currency symbol (`-$24.71`) | negative total read as positive (held-out set) | sign kept in amount detection |
| Repaired/truncated file looked clean | damaged document marked *valid* | Task 1 damage codes → `SOURCE_INCOMPLETE`, document needs review |

Found earlier on the real 1,130-page batch: shipping/discount lines ignored in the total check, item rows with row number + SKU + unit not read, invoices merged across pages, header-row layouts, Taka amounts.

## Limitations

- The labeled set is synthetic and was written by the same author as the rules. The held-out seed and the *unseen* layout reduce, but do not remove, that bias; real-world accuracy on unfamiliar layouts without AI will be lower - the unseen layout (0% with rules) shows the ceiling.
- Scans and photos are simulated degradations (skew, blur, noise, JPEG artefacts), not real camera photos.
- Ambiguous dates such as `03/04/2026` are left out of the labels: the system reports them for review instead of guessing, by design.
- AI accuracy is not established (free-tier quota); see above.
- Timings are single-process on one Windows machine; the API worker and Task 1 batch processor run in parallel.
