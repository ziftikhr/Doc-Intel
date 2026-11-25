# DocIntel frontend (Task 6)

React 19 + TypeScript + Vite, styled with Tailwind CSS. Data fetching and live
progress use TanStack Query; routing uses React Router.

## Run

The backend must be running first (from `../backend`):

```powershell
docker compose up -d                 # PostgreSQL + pgvector
.\.venv\Scripts\docintel.exe serve   # API on http://127.0.0.1:8000
```

Then, in this folder:

```powershell
npm install
npm run dev                          # http://127.0.0.1:5173
```

The dev server forwards `/api` and `/health` to the backend (see `vite.config.ts`),
so the browser only ever talks to one origin and no CORS setup is needed. Point it
at another backend with `BACKEND_URL` in `.env` (see `.env.example`).

## Screens

| Route | What it is for |
|---|---|
| `/` | Dashboard: counts, totals per currency, most common validation problems, recent uploads |
| `/upload` | Drag & drop several files; shows upload progress, then follows each background job live |
| `/documents`, `/documents/:id` | All uploads; one document with its invoices, how it was read (OCR/digital), page viewer |
| `/invoices` | Filter (status, supplier, dates, amounts, issue type) and sort; filters live in the URL |
| `/review` | Work queue of invoices that need a person, oldest first |
| `/invoices/:id` | Review: each field with confidence, source and the exact line it was read from (click to jump to it on the page), validation findings, corrections, approve / reject |
| `/search` | Semantic + keyword search over invoices and document text |

## Checks

```powershell
npm test          # component tests (Vitest + Testing Library, API answered from recorded responses)
npm run lint      # oxlint
npm run build     # type-check + production build
```

`src/test/fixtures.json` holds real responses recorded from the backend, so the
tests exercise the same data shapes the API returns.
