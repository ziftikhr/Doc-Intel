# Test results

Run on 2026-11-25 · Windows 11 · Python 3.13.5 · Node 22.20 · PostgreSQL 18 + pgvector 0.8.7 (Docker) · OCR: RapidOCR 3.10

| Suite | Result |
|---|---|
| Backend: `pytest` | **272 passed**, 1 skipped, 0 failed (54 s) |
| Backend code coverage | **90%** of 5,612 statements |
| Backend lint: `ruff check` | all checks passed |
| Frontend: `npm test` (Vitest + Testing Library) | **22 passed**, 0 failed |
| Frontend lint and types: `npm run lint`, `tsc -b` | no errors |
| Frontend production build: `npm run build` | OK (130 kB gzipped JS) |

The skipped test calls the real LLM API. It runs with `DOCINTEL_LIVE_LLM=1` (it passed with Gemini on 2026-11-24). Every other AI test uses a fake LLM client, so the suite runs offline and gives the same result every time.

Measured accuracy, speed and success rates are in [EVALUATION.md](EVALUATION.md).

## Backend tests by area

| Area | File | Tests | Time | What is covered |
|---|---|---|---|---|
| Document processing | `test_pdf.py` | 11 | 11.8 s | digital, scanned and mixed PDFs (OCR only where a page needs it); page order; tables; owner-password PDFs; garbage-text detection; soft hyphens; no OCR engine fails cleanly |
| | `test_detection.py` | 15 | 0.4 s | file type from content, not extension; wrong extensions; unsupported types |
| | `test_batch.py` | 7 | 7.3 s | a mixed inbox in parallel, resume and de-duplication, a large PDF split across workers, a crashing worker is retried and only the bad file fails |
| OCR | `test_images_ocr.py` | 20 | 21.0 s | photos, PNG, multi-page and 16-bit TIFF, transparency, sideways and skewed scans, blank pages, OCR confidence |
| Spreadsheet parsing | `test_spreadsheets.py` | 12 | 1.7 s | XLSX and legacy XLS with several sheets and dates; CSV with UTF-8 BOM and with cp1252 plus `;`; ragged rows; TSV disguised as `.xls`; large files streamed with a row limit; header detection |
| AI extraction | `test_extraction.py` | 89 | 2.6 s | text rules per layout, LLM output parsing and grounding (made-up values rejected), rules/LLM merge and confidence, number and date formats, currencies, charges and discounts, multi-invoice splitting, rate-limit waiting, quota fallback to rules |
| Validation | `test_validation.py` | 55 | 0.1 s | subtotal + charges + tax = total, qty × price per line, line sum, dates, currency, tax rate, duplicates across documents, statuses |
| API endpoints | `test_api.py` | 30 | 11.9 s | upload → background job → result; same file twice; several files in one request; rejected uploads; path tricks in filenames; duplicates in a later upload; filters, sorting and paging; review (correct, approve, reject, force); delete; page images; search by number, by meaning and with a misspelt supplier; embedding outage falls back to keyword search; embedding batching and rate limits; job retries; two workers never share a job; a dead worker's job is re-queued; API key |
| Failure scenarios | `test_failures.py` | 29 | 1.7 s | empty, random-bytes, password-protected and truncated files, wrong formats, damaged spreadsheets; each runs end to end to a `failed` or `needs_review` verdict with a reason and never crashes |
| Evaluation harness | `test_evaluation.py` | 5 | 1.9 s | labeled data is complete, broken files are rejected, accuracy floor on known layouts, scoring rules |

The database tests run against a separate `docintel_test` database, which is created and reset automatically, so your data is never touched. They are skipped if PostgreSQL is not running.

## Coverage by package

| Package | Statements | Covered |
|---|---|---|
| `validation` | 512 | 96.3% |
| `db` | 310 | 95.8% |
| `search` | 437 | 92.9% |
| `extraction` | 1,490 | 91.8% |
| `api` | 621 | 90.8% |
| `processing` | 1,999 | 86.1% (90.7% without the command-line interface) |
| `jobs` | 184 | 85.9% |
| **Total** | **5,612** | **90.2%** |

## Frontend tests

| Screen | Tests |
|---|---|
| Dashboard | shows counts, recent documents and the most common problems; explains how to start the backend when it is down |
| Documents | lists documents and sends filters to the API; shows a document with its invoices and the page viewer |
| Invoice review | shows values, their source and the validation findings; clicking a field jumps to its evidence line; asks before approving an invoice that still has errors; sends only corrected fields |
| Amount check | subtotal + shipping + tax = total; a discount shown as a subtraction; the difference shown when totals disagree; qty × price checked on each line and the sum checked against the subtotal |
| Search | runs a query from the URL and explains how each result matched; offers examples before anything is typed |
| Review queue | lists only invoices that need review |
| Helpers | exact decimal arithmetic, formatting, API error messages, unreachable backend |

The API responses in `src/test/fixtures.json` were recorded from the real backend, so the tests use the same data shapes the API returns.

## Reproduce

```powershell
# backend (needs: docker compose up -d)
cd backend
.\.venv\Scripts\python.exe -m pytest --cov=docintel
.\.venv\Scripts\ruff.exe check src tests evaluation scripts

# frontend
cd ..\frontend
npm test
npm run lint
npm run build
```
