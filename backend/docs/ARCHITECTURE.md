# Architecture

DocIntel turns invoices in any common format (PDF, scans, phone photos, PNG/TIFF, Excel, CSV) into checked, searchable records. A person reviews only the invoices that need it.

The diagrams are [Mermaid](https://mermaid.js.org/); GitHub and VS Code render them directly.

## System overview

```mermaid
flowchart LR
    user([Reviewer in a browser])

    subgraph FE["Frontend (React 19 + Vite)"]
        ui["Dashboard · Upload · Documents<br/>Invoices · Review queue · Search"]
    end

    subgraph API["Backend (FastAPI, Python 3.13)"]
        routes["REST API /api/v1<br/>upload · documents · invoices<br/>review · search · stats"]
        worker["Background worker<br/>(thread in the API, or a separate<br/>`docintel worker` process)"]
        subgraph PIPE["Processing pipeline"]
            t1["1 · Read the file<br/>PDF text / OCR / spreadsheet"]
            t2["2 · Extract invoices<br/>text rules + LLM, grounded"]
            t3["3 · Validate<br/>math · dates · duplicates"]
            t5["5 · Index for search<br/>chunks + embeddings"]
        end
    end

    subgraph DB["PostgreSQL 18 + pgvector (Docker)"]
        tables[("documents · pages · invoices<br/>line_items · validation_issues<br/>chunks · jobs")]
    end

    files[("File storage<br/>storage/ab/&lt;sha256&gt;/")]
    llm{{"Gemini / OpenAI<br/>(optional)"}}
    ocr{{"OCR engine<br/>Tesseract or RapidOCR"}}

    user --> ui -- "/api via Vite proxy" --> routes
    routes -- "save original" --> files
    routes -- "insert job" --> tables
    worker -- "claim job<br/>FOR UPDATE SKIP LOCKED" --> tables
    worker --> t1 --> t2 --> t3 --> t5
    t1 -. scans & photos .-> ocr
    t2 -. "if a key is set" .-> llm
    t5 -. embeddings .-> llm
    t3 --> tables
    t5 --> tables
    routes -- "queries, hybrid search" --> tables
```

## What happens to one upload

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as API
    participant S as File storage
    participant Q as jobs table
    participant W as Worker
    participant D as Database

    B->>A: POST /api/v1/documents (one or more files)
    A->>S: stream to disk, compute SHA-256
    A->>D: documents row (same hash already there = return it, no reprocessing)
    A->>Q: jobs row (queued)
    A-->>B: 202 Accepted + job id
    loop every few seconds
        B->>A: GET /api/v1/jobs/{id}
        A-->>B: status + stage
    end
    W->>Q: claim next job (SKIP LOCKED), heartbeat every 20 s
    W->>W: 1 read file → pages of text (+ OCR confidence)
    W->>D: save pages
    W->>W: 2 split into invoices, extract fields
    W->>D: lock, look up known invoices (duplicates)
    W->>W: 3 validate each invoice
    W->>D: save invoices, line items, issues (one transaction)
    W->>D: 5 chunks + embeddings (an outage leaves keyword search working)
    W->>Q: succeeded (or retried up to 3 times, then failed)
```

A worker that dies mid-job stops sending heartbeats, so `requeue_stale` puts its job back in the queue. Several workers can run at once: `SKIP LOCKED` makes sure each job is claimed by exactly one of them.

## The pipeline, stage by stage

### 1 · Document processing (`src/docintel/processing`)

| Input | How it is read |
|---|---|
| Digital PDF | PyMuPDF text layer, in reading order. Soft hyphens and Unicode minus signs are turned into `-`, so invoice numbers and negative amounts survive. |
| Scanned PDF / mixed PDF | Each page without usable text is rendered at 300 dpi and OCR'd. Digital pages keep their text. |
| Photo / PNG / JPEG / TIFF (multi-page, 16-bit, transparent) | OpenCV clean-up (deskew, sideways/upside-down rotation, upscaling of low-resolution images, a stronger clean-up pass for noisy ones), then OCR. |
| XLSX / XLS / CSV / TSV | Read cell by cell with no OCR. Detects the encoding (UTF-8, cp1252, UTF-16) and the delimiter, and reads files with the wrong extension correctly (e.g. TSV saved as `.xls`). |
| Broken files | The file type is detected from its content, not its name. Damaged, password-protected, empty or unsupported files get a `failed` status with a reason; nothing crashes. Repaired or truncated PDFs are processed but flagged, so their invoices always go to review. |

OCR uses Tesseract when it is installed, otherwise RapidOCR (pip-only, ONNX). Every OCR page records its confidence.

### 2 · Extraction (`src/docintel/extraction`)

1. **Split** the document into invoices. A new invoice number, or a `Page 1 / n` footer, starts a new invoice. A 1,130-page batch file becomes 835 invoices.
2. **Text rules** (regular expressions over labelled lines and tables) read the invoice number, dates, parties, subtotal, tax, total, currency, line items, and charges such as shipping, discounts and fees.
3. **LLM** (Gemini `gemini-3.5-flash` or OpenAI) reads the same text and returns JSON that matches a fixed schema.
4. **Grounding**: each LLM value must be found in the page text, or it is discarded. This blocks invented numbers.
5. **Merge and score**:

   | Outcome | Confidence |
   |---|---|
   | Rules and LLM agree | 0.97 |
   | LLM only | 0.88 |
   | Rules only | 0.78 |
   | They disagree | 0.55, flagged for review |

   Amounts are `Decimal` and never `float`. They are normalised from `1.234,56`, `1,234.56`, `1'234.56`, `-$24.71`, `৳`, `Tk` and `Rs`.
6. **Spreadsheets** are mapped by their column headers and need no LLM.

Without an API key, or once the free quota is used up, extraction continues with rules only and says so in the invoice's notes. Per-minute rate limits are waited out; a daily limit switches to rules only.

### 3 · Validation (`src/docintel/validation`)

Validation is plain code with no AI, so every verdict can be explained:

- **Required fields:** invoice number, date, supplier and total are present.
- **Arithmetic** (exact decimals, ±0.01 tolerance, configurable):
  - subtotal + charges + tax = total;
  - quantity × unit price = amount on every line;
  - the lines add up to the subtotal.
- **Dates:** the date parses; it is not in the future and not implausibly old; an ambiguous day/month order (03/04) is flagged.
- **Other checks:** currency code, tax rate (at most 30%), tax not above total, negative amounts outside credit notes, supplier ≠ customer, low OCR quality, and rules/LLM disagreements.
- **Duplicates:** same supplier and invoice number across *all* stored documents (`DUPLICATE_INVOICE`), and a reissued number with a different total (`DUPLICATE_NUMBER_CONFLICT`). An advisory lock makes two workers check duplicates one at a time.

| Status | Rule |
|---|---|
| `invalid` | any error |
| `needs_review` | any warning, low confidence, or a damaged source file |
| `valid` | none of the above |

### 4 · API and review (`src/docintel/api`)

The API is FastAPI with OpenAPI docs at `/docs`. A reviewer can correct fields (validation runs again immediately), and approve or reject. Approving an invoice that still has errors needs `force=true`.

Optional `X-API-Key` protection is off by default. Uploads are capped at 50 MB, and filenames are sanitised before storage.

### 5 · Search (`src/docintel/search`)

Search is hybrid: three rankings merged with Reciprocal Rank Fusion (k = 60).

| Ranking | What it is good at |
|---|---|
| **Semantic**: pgvector HNSW, cosine, `gemini-embedding-001`, 768 dimensions | "computer screens" finds invoices for a *27" IPS Monitor* |
| **Full-text**: PostgreSQL `tsvector` | exact words |
| **Exact / fuzzy**: normalised invoice number and `pg_trgm` supplier similarity | `INV 2024/43` finds `INV-2024-0043`; "Mueller" finds "Müller" |

Each invoice is embedded as one sentence: number, supplier, customer, date, total and item names. Page text is embedded in overlapping chunks of 1,200 characters. Every vector records the model that made it, so changing the model never mixes vectors from different models. Without an embedding key, an offline lexical embedder is used so the system still runs.

## Database schema

The full DDL, exported from the running database, is in [`schema.sql`](schema.sql). The model definitions are in `src/docintel/db/models.py`.

```mermaid
erDiagram
    documents ||--o{ pages : "has"
    documents ||--o{ invoices : "contains"
    documents ||--o{ chunks : "split into"
    documents ||--o{ jobs : "processed by"
    documents ||--o{ validation_issues : "document-level"
    invoices ||--o{ line_items : "has"
    invoices ||--o{ validation_issues : "invoice-level"

    documents {
        uuid id PK
        varchar sha256 UK "same file is stored once"
        varchar filename
        varchar status "queued, processing, processed, failed"
        varchar detected_type "pdf, image, xlsx, csv..."
        int page_count
        varchar validation_status
        bool needs_review
        varchar search_status "indexed, keyword_only, empty"
        jsonb processing "Task 1 metadata and issues"
        jsonb result "raw Task 2 and 3 output"
    }
    pages {
        bigint id PK
        uuid document_id FK
        int number
        varchar method "digital, ocr, spreadsheet"
        text text
        float ocr_confidence
        jsonb tables
    }
    invoices {
        uuid id PK
        uuid document_id FK
        int index "position in the document"
        varchar invoice_number
        varchar invoice_number_norm "for duplicate checks"
        date invoice_date
        varchar supplier_name
        varchar supplier_norm
        varchar customer_name
        numeric subtotal "NUMERIC(18,4)"
        numeric tax
        numeric total
        varchar currency "ISO 4217"
        jsonb charges "shipping, discount, fee (signed)"
        jsonb fields "per field: value, evidence, page, source, confidence"
        float confidence
        varchar validation_status "valid, needs_review, invalid"
        varchar review_status "pending, approved, rejected"
        jsonb corrected_fields
        text summary_text "what gets embedded"
        tsvector search_tsv "generated"
        vector embedding "768 dims, HNSW index"
    }
    line_items {
        bigint id PK
        uuid invoice_id FK
        int position
        text description
        numeric quantity
        numeric unit_price
        numeric amount
        text evidence
        float confidence
    }
    validation_issues {
        bigint id PK
        uuid document_id FK
        uuid invoice_id FK "null = whole document"
        varchar code "e.g. TOTAL_MISMATCH"
        varchar severity "error, warning, info"
        varchar field
        text message
        jsonb details
    }
    chunks {
        bigint id PK
        uuid document_id FK
        int chunk_index
        int page_number
        text text
        tsvector search_tsv "generated"
        vector embedding "768 dims, HNSW index"
    }
    jobs {
        uuid id PK
        uuid document_id FK
        varchar status "queued, running, succeeded, failed"
        varchar stage "processing, extracting, validating, indexing"
        int attempts
        timestamptz heartbeat_at
        timestamptz run_after
    }
```

**Design choices:**

- **Typed columns for the facts you filter on, JSONB for the detail.**
  - Typed: dates, `NUMERIC` amounts, normalised supplier and number. Filtering, sorting, totals and duplicate checks run on these.
  - JSONB: per-field evidence and confidence, charges, and the raw machine output. The review screen needs these, but nothing queries inside them.
- **Indexes:**
  - B-tree: status, dates, totals and normalised keys;
  - GIN: full-text, plus trigram on supplier;
  - HNSW: both embedding columns;
  - a claim index on `jobs (status, run_after)`.
- **Cascade deletes:** deleting a document removes everything derived from it.
- **The queue lives in Postgres:** no Redis or RabbitMQ to run. `SKIP LOCKED` and heartbeats give at-least-once processing with retries.

## Technology choices

| Need | Choice | Why |
|---|---|---|
| API | FastAPI | typed request and response models, OpenAPI docs for free |
| Database and vectors | PostgreSQL 18 + pgvector | one system for records, full-text, fuzzy and vector search, and the job queue |
| PDF | PyMuPDF | fast; text in reading order; renders pages for OCR |
| OCR | Tesseract / RapidOCR | Tesseract when installed; RapidOCR installs with pip alone (no admin rights needed) |
| LLM | Gemini 3.5 Flash (OpenAI supported) | structured JSON output; free tier available |
| Embeddings | `gemini-embedding-001` | measured better than `gemini-embedding-2` on these invoices: right invoice first in 6/6 test queries vs 4/6 |
| Frontend | React 19, Vite, TypeScript, Tailwind, TanStack Query | live job progress through polling; filters kept in the URL |

## Known limits

- **Free-tier AI quota.**
  - **Extraction:** Gemini allows 20 requests a day. Beyond that, extraction is rules only, which handles known layouts (91% field accuracy) but not new ones.
  - **Embeddings:** Gemini allows 1,000 texts a day, so a file of 1,000+ pages needs several days to index for semantic search. Until then it is searchable by keyword.
- **Schema changes:** handled by `UPGRADE_SQL` in `init_db`. A production deployment would use Alembic.
- **Single machine:** API, worker and database run on one machine. Scaling out means more `docintel worker` processes against the same database; files would then need shared storage such as S3.
