# DocIntel: AI-Powered Document Intelligence System

DocIntel reads invoices in any common format (digital and scanned PDFs, phone photos, PNG/TIFF images, Excel and CSV), extracts the fields and line items, and checks the arithmetic in code. It stores every result in PostgreSQL with hybrid semantic + keyword search. A React app lets a person review only the invoices that need attention.

```
upload ─► queue ─► read file (text / OCR / spreadsheet) ─► extract (rules + LLM, grounded)
                                                            ─► validate (math, dates, duplicates)
                                                            ─► store + index (pgvector) ─► review & search UI
```

## Deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | Git repository with the source code | this repository: [`backend/`](backend) (Python) and [`frontend/`](frontend) (React) |
| 2 | README and setup instructions | this file, section [Setup](#setup) |
| 3 | Architecture diagram and database schema | [`backend/docs/ARCHITECTURE.md`](backend/docs/ARCHITECTURE.md) (diagrams, ER diagram) and [`backend/docs/schema.sql`](backend/docs/schema.sql) (DDL) |
| 4 | Working backend API and frontend | [`backend/src/docintel/api`](backend/src/docintel/api) (FastAPI, docs at `/docs`) and [`frontend/src`](frontend/src) |
| 5 | PDF, image, Excel and CSV processing pipelines | [`backend/src/docintel/processing`](backend/src/docintel/processing), described in [ARCHITECTURE.md](backend/docs/ARCHITECTURE.md#1--document-processing-srcdocintelprocessing) |
| 6 | Automated tests and test results | [`backend/tests`](backend/tests), [`frontend/src/test`](frontend/src/test) and [`backend/docs/TEST_RESULTS.md`](backend/docs/TEST_RESULTS.md) |
| 7 | Evaluation report with measured metrics | [`backend/docs/EVALUATION.md`](backend/docs/EVALUATION.md) |
| 8 | Recorded walkthrough | *(video link)*; the recording script is in [`backend/docs/WALKTHROUGH.md`](backend/docs/WALKTHROUGH.md) |

## Results at a glance

Measured on a labeled dataset the code had never seen: 95 documents, 138 invoices. Details are in [EVALUATION.md](backend/docs/EVALUATION.md).

| Metric | Result |
|---|---|
| Field extraction accuracy (8 fields, exact match, text rules only) | **91.3%**, with 99–100% on every layout the rules know |
| Document success / failure rate | 86% / 14%; every failure is the deliberately *unseen* layout |
| Broken files rejected cleanly | **100%**, with 0 crashes |
| Planted errors caught | **100%** of those on invoices that were found |
| Clean invoices wrongly marked invalid | **0%** |
| Processing time | ~25 ms per digital document, ~1.4 s per scan or photo; 1,130 pages in 11 s |
| Automated tests | 272 backend tests (90% coverage) + 22 frontend tests, all passing |

## Features

| Task | What it does |
|---|---|
| **1 · Processing** | Detects the real file type from its content. Reads PDF text, or OCRs scanned pages; the two are mixed per page. Cleans up photos before OCR (deskew, rotation, upscaling). Reads XLSX/XLS/CSV/TSV with any encoding and delimiter. Handles multi-page TIFFs and 1,000+ page PDFs in parallel. Broken, locked or empty files fail with a clear reason. |
| **2 · Extraction** | Splits batch files into separate invoices. Text rules and an LLM (Gemini or OpenAI) read the same text; every LLM value must be found in the document, or it is dropped. Each field gets a confidence score, its source and the exact line it came from. Amounts are exact decimals in every number format; shipping, discounts and fees are captured. Without an AI key or quota it keeps working with rules only. |
| **3 · Validation** | Plain code, no AI: subtotal + charges + tax = total, qty × price per line, the line sum, dates, currency, tax rate, and duplicates across every stored document. Each invoice ends up `valid`, `needs_review` or `invalid`, with readable reasons. |
| **4 · API** | FastAPI REST API. Uploads are processed by a background job queue held in Postgres, with retries and crash recovery. Covers documents, invoices with filters, sorting and paging, review (correct, approve, reject), page images and stats. OpenAPI docs at `/docs`. |
| **5 · Storage + search** | PostgreSQL 18 + pgvector. Hybrid search merges embeddings, full-text, exact invoice number and fuzzy supplier name. |
| **6 · Frontend** | React app: dashboard, drag-and-drop upload with live progress, document and page viewer, invoice list with filters, review queue, side-by-side review (click a field to see where it was read), amount checks, and search. |
| **7 · Tests + evaluation** | Unit, integration and end-to-end tests, plus a labeled-dataset generator and an evaluation harness. |

## Setup

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ (tested on 3.13) | |
| Node.js | 22.12+ (tested on 22.20) | for the frontend |
| Docker Desktop | any recent | runs PostgreSQL 18 + pgvector |
| Tesseract OCR | optional | without it, RapidOCR is used; it installs with pip |
| Gemini API key | optional, free | [aistudio.google.com/apikey](https://aistudio.google.com/apikey); enables AI extraction and semantic search |

### 1. Backend

Commands are for Windows PowerShell. On macOS or Linux, use `.venv/bin/` instead of `.\.venv\Scripts\`, and `cp` instead of `Copy-Item`.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,rapidocr]"

Copy-Item .env.example .env
# Edit .env:
#   - choose a password and put it in POSTGRES_PASSWORD *and* DATABASE_URL
#   - optional: GEMINI_API_KEY=...

docker compose up -d                       # PostgreSQL + pgvector on 127.0.0.1:5433
.\.venv\Scripts\docintel.exe serve         # API + background worker on http://127.0.0.1:8000
```

The tables are created automatically on first start. Check that it works by opening http://127.0.0.1:8000/health (it should show `"status":"ok"`) and http://127.0.0.1:8000/docs.

### 2. Frontend

In a second terminal:

```powershell
cd frontend
npm ci                                     # if npm reports a peer-dependency error: npm install --legacy-peer-deps
npm run dev                                # http://127.0.0.1:5173
```

Open http://127.0.0.1:5173 and drag in files from [`backend/samples/demo/`](backend/samples/demo).

### 3. Tests

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest              # 272 tests; database tests use a separate docintel_test database
cd ..\frontend
npm test                                          # 22 tests
```

### 4. Evaluation

```powershell
cd backend
.\.venv\Scripts\python.exe -m evaluation.dataset --out eval_data_test --seed 2026 --per-layout 12   # build the labeled set
.\.venv\Scripts\python.exe -m evaluation.run --data eval_data_test --name rules_test               # measure
.\.venv\Scripts\python.exe -m evaluation.report                                                    # write docs/EVALUATION.md
```

## Using it

**Web app.** Upload files, watch them being processed, then work through the **Review** queue. Each field shows the line it was read from; correct what's wrong, then approve or reject.

**REST API.** All routes are under `/api/v1`:

| Method and path | Purpose |
|---|---|
| `POST /documents` | upload one or more files (multipart); returns a job per file |
| `GET /jobs/{id}` | job status and stage |
| `GET /documents`, `GET /documents/{id}` | list and detail, including how each page was read |
| `GET /documents/{id}/pages/{n}/image`, `GET /documents/{id}/file` | page image and original file |
| `POST /documents/{id}/reprocess`, `DELETE /documents/{id}` | run the pipeline again, or delete |
| `GET /invoices` | filter by status, supplier, dates, amounts or issue code; sort and page |
| `GET /invoices/{id}` | fields with evidence and confidence, line items, charges, findings |
| `POST /invoices/{id}/review` | correct fields, approve or reject |
| `GET /search?q=` | hybrid semantic + keyword search over invoices and page text |
| `GET /stats` | dashboard counts |

```powershell
curl.exe -F "files=@backend/samples/demo/1_four_invoices_in_one.pdf" http://127.0.0.1:8000/api/v1/documents
curl.exe "http://127.0.0.1:8000/api/v1/search?q=computer%20screens"
```

**Command line.** These commands work without the API:

```powershell
.\.venv\Scripts\docintel.exe extract invoice.pdf -o result.json   # process + extract + validate one file
.\.venv\Scripts\docintel.exe process inbox\ --out results\        # process a folder in parallel
.\.venv\Scripts\docintel.exe search "office chairs"               # search the database
.\.venv\Scripts\docintel.exe check-llm                            # test the AI connection
```

### Configuration

Settings come from `backend/.env`; every option is listed in [`.env.example`](backend/.env.example). The main ones:

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | empty | AI extraction and embeddings. Empty means rules only, with offline search. |
| `DOCINTEL_LLM_PROVIDER` | `auto` | `gemini`, `openai` or `none` |
| `DATABASE_URL` | – | PostgreSQL connection. Use `127.0.0.1`, not `localhost`, on Windows. |
| `DOCINTEL_MAX_UPLOAD_MB` | 50 | upload size limit |
| `DOCINTEL_API_KEY` | empty | if set, the API requires an `X-API-Key` header |
| `DOCINTEL_INLINE_WORKER` | true | run the worker inside `serve`; set false and run `docintel worker` processes to scale |

## Project layout

```
backend/
  src/docintel/
    processing/     Task 1: file detection, PDF, OCR, image clean-up, spreadsheets, batch runner, CLI
    extraction/     Task 2: invoice splitting, text rules, LLM client, grounding, merge
    validation/     Task 3: checks, duplicate index, statuses
    api/            Task 4: FastAPI app, routes, schemas, services
    jobs/           Task 4: Postgres job queue and worker
    db/             Task 5: SQLAlchemy models, repository, schema set-up
    search/         Task 5: embeddings, indexing, hybrid search
  tests/            pytest suite
  evaluation/       Task 7: labeled-dataset generator, evaluation runner, report
  samples/          good, degraded, broken and demo files
  docs/             architecture, schema, test results, evaluation, walkthrough
  docker-compose.yml
frontend/
  src/pages/        dashboard, upload, documents, invoices, review, search
  src/components/   page viewer, math checks, UI kit
  src/test/         Vitest + Testing Library tests
```

## Known limits

- **Free-tier AI quota.**
  - Gemini allows 20 extraction requests a day. After that, extraction falls back to rules only, which reads known layouts well (99–100% field accuracy on them) but not new ones.
  - Embeddings are limited to 1,000 texts a day, so a very large file may take more than a day to become semantically searchable; until then it is keyword-searchable.
- **New layouts.** Rules alone found nothing on the deliberately unseen layout; reading new layouts is the LLM's job. LLM accuracy could only be measured on one document (8/8 fields correct) before the day's quota ran out.
- **Schema changes.** These are applied by `init_db`; production would use Alembic migrations.
- **Single machine.** Scaling out means more worker processes plus shared file storage (e.g. S3).

## AI usage disclosure

- **At runtime,** the system uses Google Gemini (`gemini-3.5-flash` for extraction, `gemini-embedding-001` for search) or, optionally, OpenAI. All validation and every valid / needs-review / invalid verdict come from plain code, not AI.
- **During development,** Claude Code (Anthropic) was used as a coding assistant for implementation, tests and documentation. Design decisions, requirements and the review of results were mine.
