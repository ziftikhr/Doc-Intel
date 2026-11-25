import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, ClipboardCheck, Receipt, X } from 'lucide-react'
import { Link, useSearchParams } from 'react-router'
import { Button, Card, Empty, ErrorBox, PageHeader, Pagination, Spinner, StatusBadge } from '../components/ui'
import { cx, inputClass } from '../lib/styles'
import { api } from '../lib/api'
import { date, humanize, money, percent } from '../lib/format'

const LIMIT = 25
const FILTERS = ['validation_status', 'review_status', 'supplier', 'currency', 'date_from', 'date_to',
  'min_total', 'max_total', 'issue_code', 'document_id'] as const

function SortHeader({ label, col, sort, order, onSort, right }: {
  label: string; col: string; sort: string; order: string; onSort: (col: string) => void; right?: boolean
}) {
  const active = sort === col
  return (
    <th className={cx('px-4 py-2', right && 'text-right')}>
      <button className={cx('inline-flex items-center gap-1 uppercase', active && 'text-slate-800')} onClick={() => onSort(col)}>
        {label}
        {active && (order === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
      </button>
    </th>
  )
}

/** Invoice list. ``reviewQueue`` = the work list of invoices that still need a person. */
export function InvoicesPage({ reviewQueue = false }: { reviewQueue?: boolean }) {
  const [sp, setSp] = useSearchParams()
  const get = (k: string) => sp.get(k) ?? ''
  const sort = get('sort') || 'created'
  const order = get('order') || (reviewQueue ? 'asc' : 'desc')
  const offset = Number(get('offset') || 0)
  const filters = Object.fromEntries(FILTERS.map((k) => [k, get(k)]))

  const set = (patch: Record<string, string>) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
    if (!('offset' in patch)) next.delete('offset')
    setSp(next, { replace: true })
  }
  const onSort = (col: string) => set({ sort: col, order: sort === col && order === 'desc' ? 'asc' : 'desc' })

  const query = { ...filters, sort, order, offset, limit: LIMIT, ...(reviewQueue ? { needs_review: true } : {}) }
  const { data, error, isPending, isFetching } = useQuery({
    queryKey: ['invoices', query],
    queryFn: () => api.invoices(query),
    placeholderData: (prev) => prev,
  })
  const active = FILTERS.filter((k) => filters[k])

  return (
    <>
      <PageHeader
        title={reviewQueue ? 'Review queue' : 'Invoices'}
        subtitle={reviewQueue
          ? 'Invoices that failed a check or that the system was not sure about. Oldest first.'
          : data ? `${data.total} invoice(s)` : undefined}
      />
      <Card padded={false}>
        <div className="grid gap-3 border-b border-slate-100 p-3 sm:grid-cols-3 lg:grid-cols-6">
          {!reviewQueue && (
            <select className={inputClass} value={filters.validation_status} onChange={(e) => set({ validation_status: e.target.value })} aria-label="Validation">
              <option value="">Any validation</option>
              <option value="valid">Valid</option>
              <option value="needs_review">Needs review</option>
              <option value="invalid">Invalid</option>
            </select>
          )}
          {!reviewQueue && (
            <select className={inputClass} value={filters.review_status} onChange={(e) => set({ review_status: e.target.value })} aria-label="Review">
              <option value="">Any review state</option>
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
            </select>
          )}
          <input className={inputClass} placeholder="Supplier contains…" defaultValue={filters.supplier} onChange={(e) => set({ supplier: e.target.value })} aria-label="Supplier" />
          <input className={inputClass} placeholder="Currency (EUR)" maxLength={3} defaultValue={filters.currency} onChange={(e) => set({ currency: e.target.value.toUpperCase() })} aria-label="Currency" />
          <div className="flex items-center gap-1">
            <input type="date" className={inputClass} value={filters.date_from} onChange={(e) => set({ date_from: e.target.value })} aria-label="Invoice date from" title="Invoice date from" />
            <span className="text-slate-400">–</span>
            <input type="date" className={inputClass} value={filters.date_to} onChange={(e) => set({ date_to: e.target.value })} aria-label="Invoice date to" title="Invoice date to" />
          </div>
          <div className="flex items-center gap-1">
            <input className={inputClass} inputMode="decimal" placeholder="Min total" defaultValue={filters.min_total} onChange={(e) => set({ min_total: e.target.value })} aria-label="Minimum total" />
            <input className={inputClass} inputMode="decimal" placeholder="Max total" defaultValue={filters.max_total} onChange={(e) => set({ max_total: e.target.value })} aria-label="Maximum total" />
          </div>
        </div>
        {active.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-3 py-2 text-xs">
            {active.map((k) => (
              <button key={k} onClick={() => set({ [k]: '' })} className="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2 py-0.5 text-indigo-700 hover:bg-indigo-100">
                {humanize(k)}: {k === 'issue_code' ? humanize(filters[k]) : filters[k]} <X className="size-3" />
              </button>
            ))}
            <Button variant="ghost" className="py-0.5 text-xs" onClick={() => set(Object.fromEntries(FILTERS.map((k) => [k, ''])))}>Clear all</Button>
            {isFetching && <span className="text-slate-400">updating…</span>}
          </div>
        )}

        {error ? <div className="p-4"><ErrorBox error={error} /></div>
          : isPending ? <Spinner />
            : !data.items.length ? (
              reviewQueue
                ? <Empty icon={<ClipboardCheck className="size-8" />} title="Nothing to review">Every invoice has passed validation or has been reviewed.</Empty>
                : <Empty icon={<Receipt className="size-8" />} title="No invoices match these filters" />
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead className="bg-slate-50 text-left text-xs font-medium tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-2 uppercase">Invoice</th>
                      <SortHeader label="Supplier" col="supplier" {...{ sort, order, onSort }} />
                      <SortHeader label="Date" col="date" {...{ sort, order, onSort }} />
                      <SortHeader label="Total" col="total" right {...{ sort, order, onSort }} />
                      <SortHeader label="Confidence" col="confidence" right {...{ sort, order, onSort }} />
                      <th className="px-4 py-2 uppercase">Validation</th>
                      <th className="px-4 py-2 uppercase">Review</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.items.map((inv) => (
                      <tr key={inv.id} className="hover:bg-slate-50">
                        <td className="max-w-[16rem] px-4 py-2.5">
                          <Link to={`/invoices/${inv.id}${reviewQueue ? '?from=review' : ''}`} className="block truncate font-medium text-indigo-700 hover:underline">
                            {inv.invoice_number ?? '(no number)'}
                          </Link>
                          <div className="truncate text-xs text-slate-400">{inv.filename}</div>
                        </td>
                        <td className="max-w-[14rem] truncate px-4 py-2.5">{inv.supplier_name ?? <span className="text-slate-400">—</span>}</td>
                        <td className="whitespace-nowrap px-4 py-2.5">{date(inv.invoice_date)}</td>
                        <td className="whitespace-nowrap px-4 py-2.5 text-right tabular-nums">{money(inv.total, inv.currency)}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-slate-500">{percent(inv.confidence)}</td>
                        <td className="px-4 py-2.5">
                          <div className="flex items-center gap-2">
                            <StatusBadge status={inv.validation_status} />
                            {(inv.errors > 0 || inv.warnings > 0) && (
                              <span className="text-xs text-slate-400">{inv.errors ? `${inv.errors}E ` : ''}{inv.warnings ? `${inv.warnings}W` : ''}</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-2.5"><StatusBadge status={inv.review_status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: String(o) })} />
              </div>
            )}
      </Card>
    </>
  )
}
