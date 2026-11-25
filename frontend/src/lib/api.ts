// Typed client for the FastAPI backend (see backend/src/docintel/api/schemas.py).
// Amounts arrive as decimal strings ("225.50") and are kept as strings: the UI only
// formats them, it never does arithmetic on money.

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined
export const API = '/api/v1'

export type DocumentStatus = 'queued' | 'processing' | 'processed' | 'failed'
export type ValidationStatus = 'valid' | 'needs_review' | 'invalid'
export type DocValidationStatus = ValidationStatus | 'no_invoices' | 'failed'
export type ReviewStatus = 'pending' | 'approved' | 'rejected'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'
export type Severity = 'error' | 'warning' | 'info'
export type CheckOutcome = 'passed' | 'warning' | 'failed' | 'skipped'

export interface PageOf<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Job {
  id: string
  document_id: string
  status: JobStatus
  stage: string | null
  progress: number
  attempts: number
  max_attempts: number
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface DocumentSummary {
  id: string
  filename: string
  status: DocumentStatus
  detected_type: string | null
  content_type: string | null
  size_bytes: number
  page_count: number
  sha256: string
  invoice_count: number
  validation_status: DocValidationStatus | null
  needs_review: boolean
  search_status: string
  error: string | null
  created_at: string
  processed_at: string | null
}

export interface Issue {
  code: string
  severity: Severity
  field: string | null
  message: string
  details: Record<string, unknown>
}

export interface InvoiceSummary {
  id: string
  document_id: string
  filename: string | null
  index: number
  document_type: string
  invoice_number: string | null
  invoice_date: string | null
  supplier_name: string | null
  customer_name: string | null
  subtotal: string | null
  tax: string | null
  total: string | null
  currency: string | null
  confidence: number
  method: string
  validation_status: ValidationStatus
  needs_review: boolean
  review_status: ReviewStatus
  errors: number
  warnings: number
  created_at: string
}

export interface ExtractedField {
  value: string | null
  raw: string | null
  status: 'found' | 'missing' | 'uncertain'
  confidence: number
  source: string | null
  page: number | null
  evidence: string | null
  candidates: string[]
  issues: string[]
}

export interface LineItem {
  position: number
  description: string | null
  quantity: string | null
  unit_price: string | null
  amount: string | null
  page: number | null
  evidence: string | null
  source: string | null
  confidence: number
  issues: string[]
}

export const FIELD_NAMES = [
  'invoice_number', 'invoice_date', 'supplier_name', 'customer_name',
  'subtotal', 'tax', 'total', 'currency',
] as const
export type FieldName = (typeof FIELD_NAMES)[number]

/** Shipping, discount, fee... printed between subtotal and total. Discounts are negative. */
export interface Charge {
  kind: 'shipping' | 'discount' | 'fee' | 'rounding' | 'other'
  label: string | null
  amount: string | null
  raw: string | null
  page: number | null
  evidence: string | null
  source: string | null
  confidence: number
  issues: string[]
}

export interface InvoiceDetail extends InvoiceSummary {
  fields: Partial<Record<FieldName, ExtractedField>>
  line_items: LineItem[]
  /** Missing in responses from a backend older than this field. */
  charges?: Charge[]
  issues: Issue[]
  checks: Record<string, CheckOutcome>
  extraction_notes: string[]
  pages: number[]
  source_label: string | null
  review_note: string | null
  reviewed_at: string | null
  corrected_fields: string[]
}

export interface DocumentDetail extends DocumentSummary {
  processing: {
    metadata?: Record<string, unknown>
    stats?: Record<string, number | null>
    issues?: { code: string; severity: string; message: string; page?: number | null }[]
    duration_ms?: number
  }
  issues: Issue[]
  invoices: InvoiceSummary[]
  jobs: Job[]
}

export interface PageText {
  number: number
  label: string
  method: string
  char_count: number
  ocr_confidence: number | null
  text: string
}

export interface UploadResult {
  document: DocumentSummary
  job: Job | null
  duplicate_upload: boolean
  message: string
}

export interface InvoiceHit {
  invoice_id: string
  document_id: string
  filename: string
  invoice_number: string | null
  supplier_name: string | null
  customer_name: string | null
  invoice_date: string | null
  total: string | null
  currency: string | null
  validation_status: ValidationStatus
  review_status: ReviewStatus
  summary: string
  score: number
  similarity: number | null
  matched_by: string[]
}

export interface PassageHit {
  chunk_id: number
  document_id: string
  filename: string
  page_number: number
  snippet: string
  score: number
  similarity: number | null
  matched_by: string[]
}

export interface SearchResponse {
  query: string
  mode: 'hybrid' | 'keyword'
  embedding_model: string | null
  note: string | null
  invoices: InvoiceHit[]
  passages: PassageHit[]
}

export interface Stats {
  documents: Record<string, number>
  invoices: Record<string, number>
  review: Record<string, number>
  needs_review: number
  totals_by_currency: Record<string, string>
  top_issues: { code: string; severity: Severity; count: number }[]
  queue: Record<JobStatus, number>
}

export interface Health {
  status: 'ok' | 'degraded'
  database: string
  llm: string
  embeddings: string
  workers: number
  queue: Record<string, number>
}

export interface ReviewRequest {
  action?: 'approve' | 'reject' | 'reopen'
  corrections?: Partial<Record<FieldName, string | null>>
  note?: string
  force?: boolean
}

// ---------------------------------------------------------------- transport ----

export class ApiError extends Error {
  status: number
  details: Record<string, unknown>

  constructor(status: number, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.details = details
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return API_KEY ? { 'X-API-Key': API_KEY, ...extra } : extra
}

/** Turn FastAPI's error bodies ({detail: str} or {detail: [{msg, loc}]}) into one message. */
export function errorMessage(body: unknown, fallback: string): { message: string; details: Record<string, unknown> } {
  if (body && typeof body === 'object' && 'detail' in body) {
    const b = body as { detail: unknown; details?: Record<string, unknown> }
    if (typeof b.detail === 'string') return { message: b.detail, details: b.details ?? {} }
    if (Array.isArray(b.detail)) {
      const msg = b.detail
        .map((d: { msg?: string; loc?: unknown[] }) => `${(d.loc ?? []).slice(1).join('.')}: ${d.msg ?? ''}`)
        .join('; ')
      return { message: msg || fallback, details: {} }
    }
  }
  return { message: fallback, details: {} }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, { ...init, headers: headers(init.headers as Record<string, string>) })
  } catch {
    throw new ApiError(0, 'Cannot reach the backend. Is `docintel serve` running?')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const { message, details } = errorMessage(body, `${res.status} ${res.statusText}`)
    throw new ApiError(res.status, message, details)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

type Params = Record<string, string | number | boolean | null | undefined>

export function qs(params: Params): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

// --------------------------------------------------------------- endpoints ----

export const api = {
  health: () => request<Health>('/health'),
  stats: () => request<Stats>(`${API}/stats`),

  documents: (params: Params = {}) => request<PageOf<DocumentSummary>>(`${API}/documents${qs(params)}`),
  document: (id: string) => request<DocumentDetail>(`${API}/documents/${id}`),
  pages: (id: string) => request<PageText[]>(`${API}/documents/${id}/pages`),
  reprocess: (id: string) => request<Job>(`${API}/documents/${id}/reprocess`, { method: 'POST' }),
  deleteDocument: (id: string) => request<void>(`${API}/documents/${id}`, { method: 'DELETE' }),

  job: (id: string) => request<Job>(`${API}/jobs/${id}`),

  invoices: (params: Params = {}) => request<PageOf<InvoiceSummary>>(`${API}/invoices${qs(params)}`),
  invoice: (id: string) => request<InvoiceDetail>(`${API}/invoices/${id}`),
  review: (id: string, body: ReviewRequest) =>
    request<InvoiceDetail>(`${API}/invoices/${id}/review`, {
      method: 'POST',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    }),

  search: (params: Params) => request<SearchResponse>(`${API}/search${qs(params)}`),
}

export const fileUrl = (id: string, inline = true) => `${API}/documents/${id}/file${inline ? '?inline=true' : ''}`
export const pageImageUrl = (id: string, page: number, width = 1400) =>
  `${API}/documents/${id}/pages/${page}/image?width=${width}`
export const needsAuthedMedia = Boolean(API_KEY)

/** Upload one file with progress (fetch cannot report upload progress, XHR can). */
export function uploadFile(file: File, onProgress: (fraction: number) => void, reprocess = false): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API}/documents${reprocess ? '?reprocess=true' : ''}`)
    for (const [k, v] of Object.entries(headers())) xhr.setRequestHeader(k, v)
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
    xhr.onerror = () => reject(new ApiError(0, 'Network error while uploading'))
    xhr.onload = () => {
      let body: unknown = null
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300 && Array.isArray(body)) {
        resolve(body[0] as UploadResult)
      } else {
        const { message, details } = errorMessage(body, `${xhr.status} ${xhr.statusText}`)
        reject(new ApiError(xhr.status, message, details))
      }
    }
    const form = new FormData()
    form.append('files', file, file.name)
    xhr.send(form)
  })
}

/** Fetch a protected binary (page image / file) when an API key is required. */
export async function fetchBlobUrl(url: string): Promise<string> {
  const res = await fetch(url, { headers: headers() })
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`)
  return URL.createObjectURL(await res.blob())
}
