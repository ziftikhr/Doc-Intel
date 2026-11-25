import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Download, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { IssueList } from '../components/IssueList'
import { PageViewer } from '../components/PageViewer'
import { Button, Card, ErrorBox, PageHeader, SeverityIcon, Spinner, StatusBadge } from '../components/ui'
import { api, fileUrl } from '../lib/api'
import { bytes, dateTime, money, percent } from '../lib/format'

export function DocumentDetailPage() {
  const { id = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const page = Number(sp.get('page') ?? 1)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { data: doc, error } = useQuery({
    queryKey: ['document', id],
    queryFn: () => api.document(id),
    refetchInterval: (q) => (q.state.data && ['queued', 'processing'].includes(q.state.data.status) ? 1500 : false),
  })
  const reprocess = useMutation({
    mutationFn: () => api.reprocess(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['document', id] }),
  })
  const remove = useMutation({
    mutationFn: () => api.deleteDocument(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['documents'] })
      navigate('/documents')
    },
  })

  if (error) return <ErrorBox error={error} title="Cannot load the document" />
  if (!doc) return <Spinner />
  const busy = doc.status === 'queued' || doc.status === 'processing'
  const job = doc.jobs[doc.jobs.length - 1]
  const stats = doc.processing.stats ?? {}
  const procIssues = doc.processing.issues ?? []

  return (
    <>
      <Link to="/documents" className="mb-3 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800"><ArrowLeft className="size-4" /> Documents</Link>
      <PageHeader
        title={doc.filename}
        subtitle={<span className="flex flex-wrap items-center gap-2">
          <StatusBadge status={doc.status} />
          {doc.validation_status && <StatusBadge status={doc.validation_status} />}
          <span>{(doc.detected_type ?? '?').toUpperCase()} · {doc.page_count} page(s) · {bytes(doc.size_bytes)} · uploaded {dateTime(doc.created_at)}</span>
        </span>}
        actions={<>
          <a href={fileUrl(doc.id, false)} className="inline-flex items-center gap-1.5 rounded-lg bg-white px-3 py-1.5 text-sm font-medium text-slate-700 shadow-sm ring-1 ring-inset ring-slate-300 hover:bg-slate-50"><Download className="size-4" /> Original</a>
          <Button onClick={() => reprocess.mutate()} loading={reprocess.isPending} disabled={busy}><RotateCcw className="size-4" /> Reprocess</Button>
          {confirmDelete
            ? <><Button variant="danger" onClick={() => remove.mutate()} loading={remove.isPending}>Delete for good</Button><Button variant="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Button></>
            : <Button variant="ghost" onClick={() => setConfirmDelete(true)} disabled={busy}><Trash2 className="size-4" /> Delete</Button>}
        </>}
      />
      {(reprocess.error || remove.error) && <div className="mb-4"><ErrorBox error={reprocess.error ?? remove.error} /></div>}
      {busy && job && (
        <div className="mb-4 rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-800">
          Processing in the background — {job.stage ?? 'waiting for a worker'} ({job.progress}%). This page updates by itself.
        </div>
      )}
      {doc.status === 'failed' && doc.error && <div className="mb-4"><ErrorBox error={doc.error} title="The document could not be processed" /></div>}

      <div className="grid gap-6 xl:grid-cols-5">
        <div className="space-y-6 xl:col-span-2">
          <Card title={`Invoices found (${doc.invoices.length})`} padded={false}>
            {doc.invoices.length ? (
              <ul className="divide-y divide-slate-100">
                {doc.invoices.map((inv) => (
                  <li key={inv.id}>
                    <Link to={`/invoices/${inv.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-slate-50">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium text-slate-800">{inv.invoice_number ?? '(no number)'} · {inv.supplier_name ?? 'unknown supplier'}</div>
                        <div className="text-xs text-slate-500">{money(inv.total, inv.currency)} · {inv.errors} error(s), {inv.warnings} warning(s)</div>
                      </div>
                      <StatusBadge status={inv.review_status === 'pending' ? inv.validation_status : inv.review_status} />
                    </Link>
                  </li>
                ))}
              </ul>
            ) : <p className="p-4 text-sm text-slate-500">{busy ? 'Not processed yet.' : 'No invoice was recognised in this document.'}</p>}
          </Card>

          {doc.issues.length > 0 && <Card title="Document findings"><IssueList issues={doc.issues} /></Card>}

          <Card title="How it was read">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <dt className="text-slate-500">Digital text pages</dt><dd className="tabular-nums">{stats.pages_digital ?? '—'}</dd>
              <dt className="text-slate-500">OCR pages</dt><dd className="tabular-nums">{stats.pages_ocr ?? '—'}</dd>
              <dt className="text-slate-500">Spreadsheet sheets</dt><dd className="tabular-nums">{stats.pages_spreadsheet ?? '—'}</dd>
              <dt className="text-slate-500">Mean OCR confidence</dt><dd className="tabular-nums">{percent(stats.mean_ocr_confidence as number | null)}</dd>
              <dt className="text-slate-500">Search index</dt><dd><StatusBadge status={doc.search_status} /></dd>
              <dt className="text-slate-500">Processed</dt><dd>{dateTime(doc.processed_at)}</dd>
            </dl>
            {procIssues.length > 0 && (
              <ul className="mt-4 space-y-1.5 border-t border-slate-100 pt-3">
                {procIssues.map((i, n) => (
                  <li key={n} className="flex gap-2 text-xs text-slate-600">
                    <SeverityIcon severity={i.severity} className="mt-0.5 size-3.5 shrink-0" />
                    <span>{i.page ? `Page ${i.page}: ` : ''}{i.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Processing history">
            <ul className="space-y-2 text-sm">
              {doc.jobs.map((j) => (
                <li key={j.id} className="flex items-center justify-between gap-2">
                  <span className="text-slate-600">{dateTime(j.created_at)}{j.attempts > 1 ? ` · ${j.attempts} attempts` : ''}</span>
                  <StatusBadge status={j.status} />
                </li>
              ))}
            </ul>
          </Card>
        </div>

        <PageViewer className="h-[78vh] xl:col-span-3" documentId={doc.id} detectedType={doc.detected_type}
          pageCount={doc.page_count} page={page}
          onPageChange={(p) => setSp((prev) => { const n = new URLSearchParams(prev); n.set('page', String(p)); return n }, { replace: true })} />
      </div>
    </>
  )
}
