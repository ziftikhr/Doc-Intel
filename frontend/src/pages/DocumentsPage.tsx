import { useQuery } from '@tanstack/react-query'
import { FileText, Upload } from 'lucide-react'
import { Link, useSearchParams } from 'react-router'
import { Card, Empty, ErrorBox, PageHeader, Pagination, Spinner, StatusBadge } from '../components/ui'
import { inputClass } from '../lib/styles'
import { api } from '../lib/api'
import { bytes, timeAgo } from '../lib/format'

const LIMIT = 25

export function DocumentsPage() {
  const [sp, setSp] = useSearchParams()
  const params = {
    status: sp.get('status') ?? '',
    validation_status: sp.get('validation_status') ?? '',
    q: sp.get('q') ?? '',
    offset: Number(sp.get('offset') ?? 0),
  }
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(sp)
    if (v) next.set(k, v)
    else next.delete(k)
    if (k !== 'offset') next.delete('offset')
    setSp(next, { replace: true })
  }
  const { data, error, isPending } = useQuery({
    queryKey: ['documents', params],
    queryFn: () => api.documents({ ...params, limit: LIMIT }),
    // keep refreshing while anything on screen is still being processed
    refetchInterval: (q) => (q.state.data?.items.some((d) => d.status === 'queued' || d.status === 'processing') ? 2000 : false),
    placeholderData: (prev) => prev,
  })

  return (
    <>
      <PageHeader title="Documents" subtitle={data ? `${data.total} document(s)` : undefined}
        actions={<Link to="/upload" className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-500"><Upload className="size-4" /> Upload</Link>} />
      <Card padded={false}>
        <div className="flex flex-wrap gap-3 border-b border-slate-100 p-3">
          <input className={`${inputClass} max-w-xs`} placeholder="Filter by file name…" defaultValue={params.q}
            onChange={(e) => set('q', e.target.value)} aria-label="Filter by file name" />
          <select className={`${inputClass} w-auto`} value={params.status} onChange={(e) => set('status', e.target.value)} aria-label="Processing status">
            <option value="">Any processing status</option>
            <option value="queued">Queued</option>
            <option value="processing">Processing</option>
            <option value="processed">Processed</option>
            <option value="failed">Failed</option>
          </select>
          <select className={`${inputClass} w-auto`} value={params.validation_status} onChange={(e) => set('validation_status', e.target.value)} aria-label="Validation result">
            <option value="">Any validation result</option>
            <option value="valid">Valid</option>
            <option value="needs_review">Needs review</option>
            <option value="invalid">Invalid</option>
            <option value="no_invoices">No invoices</option>
            <option value="failed">Failed</option>
          </select>
        </div>
        {error ? <div className="p-4"><ErrorBox error={error} /></div>
          : isPending ? <Spinner />
            : !data.items.length ? (
              <Empty icon={<FileText className="size-8" />} title="No documents match">
                Change the filters, or <Link to="/upload" className="text-indigo-600 hover:underline">upload documents</Link>.
              </Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-2">File</th>
                      <th className="px-4 py-2">Type</th>
                      <th className="px-4 py-2 text-right">Pages</th>
                      <th className="px-4 py-2">Processing</th>
                      <th className="px-4 py-2 text-right">Invoices</th>
                      <th className="px-4 py-2">Validation</th>
                      <th className="px-4 py-2">Uploaded</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.items.map((d) => (
                      <tr key={d.id} className="hover:bg-slate-50">
                        <td className="max-w-xs px-4 py-2.5">
                          <Link to={`/documents/${d.id}`} className="block truncate font-medium text-indigo-700 hover:underline">{d.filename}</Link>
                          {d.error && <div className="truncate text-xs text-rose-600" title={d.error}>{d.error}</div>}
                        </td>
                        <td className="px-4 py-2.5 text-slate-600">{(d.detected_type ?? '—').toUpperCase()} <span className="text-xs text-slate-400">{bytes(d.size_bytes)}</span></td>
                        <td className="px-4 py-2.5 text-right tabular-nums">{d.page_count || '—'}</td>
                        <td className="px-4 py-2.5"><StatusBadge status={d.status} /></td>
                        <td className="px-4 py-2.5 text-right tabular-nums">{d.status === 'processed' ? d.invoice_count : '—'}</td>
                        <td className="px-4 py-2.5">{d.validation_status ? <StatusBadge status={d.validation_status} /> : '—'}</td>
                        <td className="whitespace-nowrap px-4 py-2.5 text-slate-500" title={d.created_at}>{timeAgo(d.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Pagination total={data.total} limit={LIMIT} offset={params.offset} onChange={(o) => set('offset', String(o))} />
              </div>
            )}
      </Card>
    </>
  )
}
