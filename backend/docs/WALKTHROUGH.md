# Walkthrough recording script (about 8 minutes)

A shot-by-shot plan for recording the working application. The files to upload are in [`samples/demo/`](../samples/demo); each one shows one feature.

## Before you record

1. Start everything:

   ```powershell
   cd backend
   docker compose up -d                  # database
   .\.venv\Scripts\docintel.exe serve    # API + background worker, http://127.0.0.1:8000
   # second terminal
   cd frontend
   npm run dev                           # http://127.0.0.1:5173
   ```

2. Check that `http://127.0.0.1:8000/health` shows `"status":"ok"`.

3. **AI quota:** the Gemini free tier allows 20 extraction requests a day. The demo set needs about 9. Record after the quota has reset, so fields show **AI + rules**. With the quota used up, everything still works with rules only, but invoices show a "rules-only extraction" note.

4. **Recording tool:**
   - **Xbox Game Bar** (`Win + Alt + R`) records one app window. Open the browser and the terminal in the same window, or switch between them.
   - **Clipchamp** or **OBS** can record the whole screen.
   - Record at 1080p and zoom the browser to 110–125% so text is readable.

## Script

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00 | `docs/ARCHITECTURE.md` (rendered diagram) | "This system reads invoices in any common format and pulls out the fields. It checks the numbers with plain code, stores everything in PostgreSQL with pgvector, and lets a person review only what needs it. Uploads go to a queue, a background worker reads, extracts, validates and indexes each file, and the React app follows the progress live." |
| 2 | 0:40 | **Upload** page: drag in all 9 files from `samples/demo/` at once | "A digital PDF with four invoices, a German phone photo and scan, a PNG with shipping and a discount, an Excel register, a CSV export, two invoices with deliberate mistakes, and a password-protected spreadsheet." Point at the live stages: processing → extracting → validating → indexing. |
| 3 | 1:40 | **Documents** list | "Each file is listed with its type, pages, invoices and validation result; opening one shows how each page was read (digital text, OCR with its confidence, or spreadsheet). `9_broken_password_protected.xlsx` failed cleanly with a reason; nothing crashed. `1_four_invoices_in_one.pdf` was split into 4 invoices." |
| 4 | 2:20 | Open `2_german_phone_photo.jpg` → its invoice | "A photo of a German invoice: `1.234,56` amounts, German labels, *Versandkosten* (shipping). Every field shows where it came from: page, exact line, and whether the AI and the text rules agreed. Clicking a field highlights that line on the page image." Click 2–3 fields. Point at the confidence bars. |
| 5 | 3:20 | Same page, **Amount check** card | "The math is checked with exact decimals: subtotal plus shipping plus tax equals the total, and quantity times unit price on every line." |
| 6 | 3:50 | Open `7_error_total_wrong.pdf` → invoice | "This one is **invalid**: subtotal 852.44 plus tax 127.87 is 980.31, but the printed total is 981.21, 90 cents off. The AI never decides this; plain code does, so every verdict can be explained." |
| 7 | 4:30 | Open `8_error_line_item_wrong.pdf` → invoice | "Here one line is wrong: 8 × 10.27 is 82.16, but the invoice says 92.16. The table marks the exact row and what it should be." |
| 8 | 5:00 | **Correct** a field on a needs-review invoice (e.g. confirm the currency on `4_shipping_and_discount.png`) → save → **Approve** | "A reviewer can fix a value; validation runs again straight away. Then approve. Approving an invoice that still has errors asks for confirmation." |
| 9 | 5:40 | **Invoices** page: filter by status *invalid*, then by supplier; sort by total | "All extracted invoices, filterable and sortable. Filters live in the URL, so a view can be shared." |
| 10 | 6:10 | **Search**: type `INV-2026` or an exact invoice number, then `computer screens` | "Search combines three methods. An invoice number matches exactly. `computer screens` finds invoices for a *27-inch IPS Monitor*, although none of those words are on the invoice. That's semantic search with pgvector; the purple badge shows a match by meaning." |
| 11 | 6:50 | Browser: `http://127.0.0.1:8000/docs` | "Everything the UI does is a REST API, documented with OpenAPI: upload, job status, invoices, review, search." |
| 12 | 7:10 | Terminal: `.\.venv\Scripts\python.exe -m pytest -q` (or show `docs/TEST_RESULTS.md`), then `docs/EVALUATION.md` | "272 automated tests, 90% coverage. On a labeled set it never saw, rules alone get 91% of fields exactly right, reject 100% of broken files, and never mark a clean invoice invalid. The misses are a layout the rules were never written for, which is the AI's job." |
| 13 | 7:50 | — | "Thanks for watching." |

## If something goes wrong while recording

| Problem | Fix |
|---|---|
| A file is not processed again | The same file (same content) is already stored, so the existing result is shown. To show processing again, open the document, click **Delete**, then upload the file again. |
| Fields show **Rules**, not **AI + rules** | The daily Gemini quota is used up. Say so, or record after it resets. |
| Search shows no purple "meaning" badges | Indexing hasn't finished yet, or the embedding quota is used up. The demo files are small, so indexing finishes in seconds when quota is available. |
| Page shows "backend not reachable" | Start `docintel serve` and refresh. |
