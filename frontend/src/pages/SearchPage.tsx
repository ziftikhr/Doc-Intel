import { useQuery } from '@tanstack/react-query'
import { FileText, Receipt, Search as SearchIcon, Sparkles } from 'lucide-react'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router'
import { Badge, Button, Card, Empty, ErrorBox, PageHeader, Spinner, StatusBadge } from '../components/ui'
import { inputClass } from '../lib/styles'
import { api } from '../lib/api'
import { date, money } from '../lib/format'

const EXAMPLES = ['bakery', 'office hardware', 'Mueller', 'INV-2024-0043', 'design services']

function escapeRegExp(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Bold the query words inside a snippet. */
function Highlight({ text, query }: { text: string; query: string }): ReactNode {
  const words = query.split(/\s+/).filter((w) => w.length >= 3).map(escapeRegExp)
  if (!words.length) return text
  const parts = text.split(new RegExp(`(${words.join('|')})`, 'gi'))
  return parts.map((p, i) => (i % 2 ? <mark key={i} className="rounded bg-yellow-100 px-0.5">{p}</mark> : p))
}

function MatchedBy({ by, similarity }: { by: string[]; similarity: number | null }) {
  return (
    <span className="flex gap-1">
      {by.includes('semantic') && (
        <Badge tone="violet" title={`Similar in meaning${similarity !== null ? ` (similarity ${similarity.toFixed(2)})` : ''}`}>
          <Sparkles className="size-3" /> meaning
        </Badge>
      )}
      {by.includes('keyword') && <Badge tone="blue" title="Contains the words, invoice number or a close spelling">words</Badge>}
    </span>
  )
}

export function SearchPage() {
  const [sp, setSp] = useSearchParams()
  const q = sp.get('q') ?? ''
  const scope = sp.get('scope') ?? 'all'
  const filters = {
    supplier: sp.get('supplier') ?? '',
    date_from: sp.get('date_from') ?? '',
    date_to: sp.get('date_to') ?? '',
    min_total: sp.get('min_total') ?? '',
    max_total: sp.get('max_total') ?? '',
  }
  const [draft, setDraft] = useState(q)
  const [showFilters, setShowFilters] = useState(Object.values(filters).some(Boolean))

  const set = (patch: Record<string, string>) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
    setSp(next)
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    set({ q: draft.trim() })
  }

  const { data, error, isFetching } = useQuery({
    queryKey: ['search', q, scope, filters],
    queryFn: () => api.search({ q, scope, limit: 10, ...filters }),
    enabled: q.length > 0,
    staleTime: 30_000,
  })

  return (
    <>
      <PageHeader title="Search"
        subtitle="Search by meaning, not just exact words: “bakery” also finds “Bäckerei”, “hardware” finds server racks. Invoice numbers and names match exactly too." />
      <form onSubmit={submit} className="flex flex-wrap gap-2">
        <div className="relative min-w-[16rem] flex-1">
          <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" />
          <input autoFocus className={`${inputClass} py-2.5 pl-9 text-base`} value={draft} onChange={(e) => setDraft(e.target.value)}
            placeholder="What are you looking for?" aria-label="Search query" />
        </div>
        <select className={`${inputClass} w-auto`} value={scope} onChange={(e) => set({ scope: e.target.value === 'all' ? '' : e.target.value })} aria-label="Search in">
          <option value="all">Invoices and text</option>
          <option value="invoices">Invoices only</option>
          <option value="passages">Document text only</option>
        </select>
        <Button type="button" variant="ghost" onClick={() => setShowFilters((s) => !s)}>Filters</Button>
        <Button type="submit" variant="primary" loading={isFetching}>Search</Button>
      </form>
      {showFilters && (
        <div className="mt-3 grid gap-3 rounded-xl border border-slate-200 bg-white p-3 sm:grid-cols-5">
          <input className={inputClass} placeholder="Supplier contains…" defaultValue={filters.supplier} onBlur={(e) => set({ supplier: e.target.value })} aria-label="Supplier" />
          <input type="date" className={inputClass} value={filters.date_from} onChange={(e) => set({ date_from: e.target.value })} aria-label="Date from" />
          <input type="date" className={inputClass} value={filters.date_to} onChange={(e) => set({ date_to: e.target.value })} aria-label="Date to" />
          <input className={inputClass} placeholder="Min total" defaultValue={filters.min_total} onBlur={(e) => set({ min_total: e.target.value })} aria-label="Minimum total" />
          <input className={inputClass} placeholder="Max total" defaultValue={filters.max_total} onBlur={(e) => set({ max_total: e.target.value })} aria-label="Maximum total" />
        </div>
      )}

      {!q ? (
        <Card className="mt-6">
          <Empty icon={<SearchIcon className="size-8" />} title="Try a search">
            <div className="mt-2 flex flex-wrap justify-center gap-2">
              {EXAMPLES.map((ex) => (
                <button key={ex} onClick={() => { setDraft(ex); set({ q: ex }) }}
                  className="rounded-full bg-slate-100 px-3 py-1 text-sm text-slate-700 hover:bg-indigo-50 hover:text-indigo-700">{ex}</button>
              ))}
            </div>
          </Empty>
        </Card>
      ) : error ? (
        <div className="mt-6"><ErrorBox error={error} title="Search failed" /></div>
      ) : !data ? (
        <Spinner label="Searching…" />
      ) : (
        <div className="mt-6 space-y-6">
          {data.note && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">{data.note}</div>
          )}
          {scope !== 'passages' && (
            <Card title={<span className="flex items-center gap-2"><Receipt className="size-4 text-slate-400" /> Invoices ({data.invoices.length})</span>} padded={false}>
              {data.invoices.length ? (
                <ul className="divide-y divide-slate-100">
                  {data.invoices.map((h) => (
                    <li key={h.invoice_id}>
                      <Link to={`/invoices/${h.invoice_id}`} className="block px-4 py-3 hover:bg-slate-50">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium text-slate-900">{h.invoice_number ?? '(no number)'}</span>
                          <span className="text-slate-600">{h.supplier_name ?? 'unknown supplier'}</span>
                          <span className="tabular-nums text-slate-600">{money(h.total, h.currency)}</span>
                          <span className="text-sm text-slate-500">{date(h.invoice_date)}</span>
                          <span className="ml-auto flex items-center gap-2">
                            <MatchedBy by={h.matched_by} similarity={h.similarity} />
                            <StatusBadge status={h.validation_status} />
                          </span>
                        </div>
                        <div className="mt-1 text-sm text-slate-500"><Highlight text={h.summary} query={q} /></div>
                        <div className="mt-0.5 text-xs text-slate-400">{h.filename}</div>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : <p className="p-4 text-sm text-slate-500">No matching invoices.</p>}
            </Card>
          )}
          {scope !== 'invoices' && (
            <Card title={<span className="flex items-center gap-2"><FileText className="size-4 text-slate-400" /> Passages in documents ({data.passages.length})</span>} padded={false}>
              {data.passages.length ? (
                <ul className="divide-y divide-slate-100">
                  {data.passages.map((h) => (
                    <li key={h.chunk_id}>
                      <Link to={`/documents/${h.document_id}?page=${h.page_number}`} className="block px-4 py-3 hover:bg-slate-50">
                        <div className="flex items-center gap-2 text-sm">
                          <span className="font-medium text-slate-800">{h.filename}</span>
                          <span className="text-slate-400">page {h.page_number}</span>
                          <span className="ml-auto"><MatchedBy by={h.matched_by} similarity={h.similarity} /></span>
                        </div>
                        <p className="mt-1 text-sm text-slate-600"><Highlight text={h.snippet} query={q} /></p>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : <p className="p-4 text-sm text-slate-500">No matching text.</p>}
            </Card>
          )}
          <p className="text-xs text-slate-400">
            Mode: {data.mode === 'hybrid' ? `meaning + words (${data.embedding_model})` : 'words only'}
          </p>
        </div>
      )}
    </>
  )
}
