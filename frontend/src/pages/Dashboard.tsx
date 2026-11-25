import { useQuery } from '@tanstack/react-query'
import { ArrowRight, FileText, Upload } from 'lucide-react'
import { Link } from 'react-router'
import { Card, Empty, ErrorBox, PageHeader, SeverityIcon, Spinner, Stat, StatusBadge } from '../components/ui'
import { api } from '../lib/api'
import { bytes, humanize, money, timeAgo } from '../lib/format'

export function Dashboard() {
  const stats = useQuery({ queryKey: ['stats'], queryFn: api.stats, refetchInterval: 5000 })
  const recent = useQuery({ queryKey: ['documents', { limit: 6 }], queryFn: () => api.documents({ limit: 6 }), refetchInterval: 5000 })

  if (stats.error) return <ErrorBox error={stats.error} title="Cannot load the dashboard" />
  if (!stats.data) return <Spinner />
  const s = stats.data
  const docs = Object.values(s.documents).reduce((a, b) => a + b, 0)
  const invoices = Object.values(s.invoices).reduce((a, b) => a + b, 0)
  const working = (s.queue.queued ?? 0) + (s.queue.running ?? 0)

  return (
    <>
      <PageHeader title="Dashboard" subtitle="Everything uploaded so far, at a glance."
        actions={<Link to="/upload" className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-500"><Upload className="size-4" /> Upload documents</Link>} />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat label="Documents" value={docs} to="/documents"
          hint={`${s.documents.processed ?? 0} processed · ${s.documents.failed ?? 0} failed`} />
        <Stat label="Invoices" value={invoices} to="/invoices" hint={working ? `${working} document(s) in progress` : 'queue idle'} />
        <Stat label="Valid" value={s.invoices.valid ?? 0} tone="green" to="/invoices?validation_status=valid" hint="passed every check" />
        <Stat label="Need review" value={s.needs_review} tone="amber" to="/review" hint="waiting for a person" />
        <Stat label="Invalid" value={s.invoices.invalid ?? 0} tone="red" to="/invoices?validation_status=invalid" hint="errors to fix" />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <Card title="Recent documents" className="lg:col-span-2" padded={false}
          actions={<Link to="/documents" className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:underline">All documents <ArrowRight className="size-3" /></Link>}>
          {recent.data?.items.length ? (
            <ul className="divide-y divide-slate-100">
              {recent.data.items.map((d) => (
                <li key={d.id}>
                  <Link to={`/documents/${d.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-slate-50">
                    <FileText className="size-4 shrink-0 text-slate-400" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-slate-800">{d.filename}</div>
                      <div className="text-xs text-slate-500">
                        {(d.detected_type ?? '?').toUpperCase()} · {bytes(d.size_bytes)} · {d.invoice_count} invoice(s) · {timeAgo(d.created_at)}
                      </div>
                    </div>
                    {d.status === 'processed' ? <StatusBadge status={d.validation_status} /> : <StatusBadge status={d.status} />}
                  </Link>
                </li>
              ))}
            </ul>
          ) : recent.isPending ? <Spinner /> : (
            <Empty icon={<Upload className="size-8" />} title="Nothing uploaded yet">
              <Link to="/upload" className="text-indigo-600 hover:underline">Upload your first invoices</Link> — PDFs, scans, photos or spreadsheets.
            </Empty>
          )}
        </Card>

        <div className="space-y-6">
          <Card title="Most common problems">
            {s.top_issues.length ? (
              <ul className="space-y-2">
                {s.top_issues.slice(0, 6).map((i) => (
                  <li key={`${i.code}-${i.severity}`}>
                    <Link to={`/invoices?issue_code=${i.code}`} className="flex items-center gap-2 rounded-md text-sm hover:text-indigo-700">
                      <SeverityIcon severity={i.severity} />
                      <span className="flex-1 truncate">{humanize(i.code)}</span>
                      <span className="tabular-nums text-slate-500">{i.count}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-slate-500">No problems found.</p>}
          </Card>
          <Card title="Invoice totals (not rejected)">
            {Object.keys(s.totals_by_currency).length ? (
              <dl className="space-y-1.5 text-sm">
                {Object.entries(s.totals_by_currency).map(([cur, total]) => (
                  <div key={cur} className="flex justify-between">
                    <dt className="text-slate-500">{cur === 'unknown' ? 'No currency' : cur}</dt>
                    <dd className="font-medium tabular-nums">{money(total, cur === 'unknown' ? null : cur)}</dd>
                  </div>
                ))}
              </dl>
            ) : <p className="text-sm text-slate-500">No amounts yet.</p>}
          </Card>
        </div>
      </div>
    </>
  )
}
