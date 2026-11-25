import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Check, ChevronRight, Pencil, RotateCcw, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { IssueList } from '../components/IssueList'
import { AmountCheck, LineItemsTable } from '../components/MathChecks'
import { PageViewer } from '../components/PageViewer'
import { Badge, Button, Card, CheckIcon, ConfidenceBar, ErrorBox, PageHeader, SourceBadge, Spinner, StatusBadge } from '../components/ui'
import { cx, inputClass } from '../lib/styles'
import {
  ApiError, FIELD_NAMES, api, type DocumentDetail, type ExtractedField, type FieldName, type InvoiceDetail, type ReviewRequest,
} from '../lib/api'
import { CHECK_LABELS, FIELD_LABELS, date, dateTime, humanize, money } from '../lib/format'

const AMOUNTS = new Set<FieldName>(['subtotal', 'tax', 'total'])

function displayValue(name: FieldName, f: ExtractedField | undefined, currency: string | null) {
  if (!f || f.status === 'missing') return <span className="text-slate-400">Not found</span>
  if (f.status === 'uncertain') {
    return (
      <span className="text-amber-700">
        Unclear{f.raw ? <>: <span className="font-mono">“{f.raw}”</span></> : null}
        {f.candidates.length > 0 && <span className="block text-xs">could be {f.candidates.map(date).join(' or ')}</span>}
      </span>
    )
  }
  if (AMOUNTS.has(name)) return <span className="tabular-nums">{money(f.value, currency)}</span>
  if (name === 'invoice_date') return <>{date(f.value)}</>
  return <>{f.value}</>
}

/** Find the next invoice in the review queue after this one. */
function useNextInQueue(currentId: string) {
  const { data } = useQuery({
    queryKey: ['invoices', 'queue-ids'],
    queryFn: () => api.invoices({ needs_review: true, sort: 'created', order: 'asc', limit: 200 }),
  })
  if (!data) return undefined
  const ids = data.items.map((i) => i.id)
  const pos = ids.indexOf(currentId)
  return (pos >= 0 ? ids[pos + 1] : ids[0]) ?? null
}

export function InvoiceReviewPage() {
  const { id = '' } = useParams()
  const [sp] = useSearchParams()
  const { data: inv, error } = useQuery({ queryKey: ['invoice', id], queryFn: () => api.invoice(id) })
  const nextId = useNextInQueue(id)
  // the page viewer needs the document's file type and page count
  const { data: doc } = useQuery({
    queryKey: ['document', inv?.document_id],
    queryFn: () => api.document(inv!.document_id),
    enabled: Boolean(inv),
  })
  if (error) return <ErrorBox error={error} title="Cannot load the invoice" />
  if (!inv) return <Spinner />
  // keyed by invoice: opening another invoice starts with a fresh form and viewer
  return <ReviewView key={inv.id} inv={inv} doc={doc} nextId={nextId} fromQueue={sp.get('from') === 'review'} />
}

function ReviewView({ inv, doc, nextId, fromQueue }: {
  inv: InvoiceDetail
  doc: DocumentDetail | undefined
  nextId: string | null | undefined
  fromQueue: boolean
}) {
  const id = inv.id
  const navigate = useNavigate()
  const qc = useQueryClient()

  // start on the page the invoice was read from
  const [page, setPage] = useState(inv.pages[0] ?? 1)
  const [highlight, setHighlight] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [values, setValues] = useState<Partial<Record<FieldName, string>>>({})
  const [note, setNote] = useState(inv.review_note ?? '')
  const [blocked, setBlocked] = useState<string[] | null>(null)

  const initial = useMemo(() => Object.fromEntries(
    FIELD_NAMES.map((n) => [n, inv.fields[n]?.status === 'found' ? inv.fields[n]?.value ?? '' : ''])) as Record<FieldName, string>,
  [inv])

  const review = useMutation({
    mutationFn: (body: ReviewRequest) => api.review(id, body),
    onSuccess: (updated: InvoiceDetail, body) => {
      qc.setQueryData(['invoice', id], updated)
      qc.invalidateQueries({ queryKey: ['invoices'] })
      qc.invalidateQueries({ queryKey: ['stats'] })
      qc.invalidateQueries({ queryKey: ['document', updated.document_id] })
      setEditing(false)
      setValues({})
      setBlocked(null)
      // in the queue, a decision moves straight on to the next invoice
      if (fromQueue && body.action && body.action !== 'reopen' && nextId) navigate(`/invoices/${nextId}?from=review`)
    },
    onError: (e: unknown, body) => {
      if (e instanceof ApiError && e.status === 409 && body.action === 'approve') {
        setBlocked((e.details.errors as string[] | undefined) ?? [e.message])
      }
    },
  })

  const corrections = Object.fromEntries(
    Object.entries(values)
      .filter(([k, v]) => v !== initial[k as FieldName])
      .map(([k, v]) => [k, v?.trim() ? v.trim() : null]),
  ) as Partial<Record<FieldName, string | null>>
  const changed = Object.keys(corrections).length > 0
  const noteChanged = note !== (inv.review_note ?? '')

  const jumpTo = (name: string) => {
    const f = inv.fields[name as FieldName]
    setSelected(name)
    if (f?.page) setPage(f.page)
    setHighlight(f?.evidence ?? null)
  }

  const jumpToLine = (p: number | null, evidence: string | null) => {
    if (p) setPage(p)
    setHighlight(evidence)
    setSelected(null)
  }

  const submit = (action?: ReviewRequest['action'], force = false) =>
    review.mutate({
      action,
      force,
      corrections: changed ? corrections : undefined,
      note: noteChanged ? note : undefined,
    })

  const errors = inv.issues.filter((i) => i.severity === 'error').length
  const warnings = inv.issues.filter((i) => i.severity === 'warning').length
  const decided = inv.review_status !== 'pending'

  return (
    <>
      <div className="mb-3 flex items-center justify-between">
        <Link to={fromQueue ? '/review' : '/invoices'} className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800">
          <ArrowLeft className="size-4" /> {fromQueue ? 'Review queue' : 'Invoices'}
        </Link>
        {nextId && nextId !== id && (
          <Link to={`/invoices/${nextId}?from=review`} className="inline-flex items-center gap-1 text-sm font-medium text-indigo-600 hover:underline">
            Next to review <ChevronRight className="size-4" />
          </Link>
        )}
      </div>

      <PageHeader
        title={<>{humanize(inv.document_type)} {inv.invoice_number ?? '(no number)'}</>}
        subtitle={<span className="flex flex-wrap items-center gap-2">
          <StatusBadge status={inv.validation_status} />
          <StatusBadge status={inv.review_status} />
          <span>
            from <Link to={`/documents/${inv.document_id}`} className="text-indigo-600 hover:underline">{inv.filename}</Link>
            {inv.source_label ? ` · ${inv.source_label}` : ''} · read by {inv.method.replace('+', ' + ')}
          </span>
        </span>}
        actions={decided ? (
          <Button onClick={() => submit('reopen')} loading={review.isPending}><RotateCcw className="size-4" /> Reopen</Button>
        ) : (
          <>
            <Button variant="danger" onClick={() => submit('reject')} loading={review.isPending && review.variables?.action === 'reject'}>
              <X className="size-4" /> Reject
            </Button>
            <Button variant="success" onClick={() => submit('approve')} loading={review.isPending && review.variables?.action === 'approve'}>
              <Check className="size-4" /> {changed ? 'Save & approve' : 'Approve'}
            </Button>
          </>
        )}
      />

      {blocked && (
        <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
          <div className="font-medium">This invoice still fails validation:</div>
          <ul className="mt-1 list-disc pl-5">{blocked.map((m, i) => <li key={i}>{m}</li>)}</ul>
          <div className="mt-2 flex gap-2">
            <Button onClick={() => { setBlocked(null); setEditing(true) }}><Pencil className="size-4" /> Correct the values</Button>
            <Button variant="danger" onClick={() => submit('approve', true)}>Approve anyway</Button>
          </div>
        </div>
      )}
      {review.error && !blocked && <div className="mb-4"><ErrorBox error={review.error} title="Could not save" /></div>}
      {inv.reviewed_at && (
        <div className="mb-4 text-xs text-slate-500">
          {humanize(inv.review_status)} {dateTime(inv.reviewed_at)}{inv.review_note ? ` — “${inv.review_note}”` : ''}
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-2">
        <div className="space-y-6">
          <Card title="Extracted fields" padded={false}
            actions={editing
              ? <>
                <Button variant="ghost" onClick={() => { setEditing(false); setValues({}) }}>Cancel</Button>
                <Button variant="primary" disabled={!changed && !noteChanged} loading={review.isPending && !review.variables?.action} onClick={() => submit()}>
                  Save corrections
                </Button>
              </>
              : <Button onClick={() => setEditing(true)}><Pencil className="size-4" /> Correct</Button>}>
            {/* table-fixed: long evidence text truncates instead of pushing the badge column out of the card */}
            <table className="w-full table-fixed text-sm">
              <tbody className="divide-y divide-slate-100">
                {FIELD_NAMES.map((name) => {
                  const f = inv.fields[name]
                  const flagged = inv.issues.some((i) => i.field?.startsWith(name) && i.severity !== 'info')
                  return (
                    <tr key={name} onClick={() => !editing && jumpTo(name)}
                      className={cx(!editing && 'cursor-pointer hover:bg-slate-50', selected === name && 'bg-indigo-50/60')}>
                      <th className="w-32 px-4 py-2.5 text-left align-top text-xs font-medium text-slate-500">
                        {FIELD_LABELS[name]}
                        {flagged && <span className="ml-1 inline-block size-1.5 rounded-full bg-amber-500 align-middle" title="Has a validation finding" />}
                      </th>
                      <td className="px-2 py-2.5 align-top">
                        {editing ? (
                          <input className={inputClass} value={values[name] ?? initial[name]}
                            placeholder={f?.raw ? `printed: ${f.raw}` : FIELD_LABELS[name]}
                            onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
                            aria-label={FIELD_LABELS[name]} />
                        ) : (
                          <div className="break-words font-medium text-slate-900">{displayValue(name, f, inv.currency)}</div>
                        )}
                        {!editing && f?.evidence && (
                          <div className="mt-1 truncate font-mono text-xs text-slate-400" title={f.evidence}>
                            {f.page ? `p.${f.page} · ` : ''}“{f.evidence}”
                          </div>
                        )}
                      </td>
                      <td className="w-40 py-2.5 pl-2 pr-4 align-top">
                        {f && f.status !== 'missing' && (
                          <div className="flex flex-col items-end gap-1">
                            <ConfidenceBar value={f.confidence} />
                            <div className="flex flex-wrap justify-end gap-1">
                              <SourceBadge source={f.source} />
                              {f.issues.filter((x) => x !== 'NOT_FOUND').map((x) => <Badge key={x} tone="amber">{humanize(x)}</Badge>)}
                            </div>
                          </div>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {editing && (
              <div className="border-t border-slate-100 p-4 text-xs text-slate-500">
                Type values as printed or normalised (e.g. <span className="font-mono">1.234,50</span> or <span className="font-mono">15.03.2024</span>).
                Leave a field empty to clear it. Validation runs again after saving.
              </div>
            )}
            <div className="border-t border-slate-100 p-4">
              <label className="block text-xs font-medium text-slate-600" htmlFor="note">Reviewer note</label>
              <textarea id="note" rows={2} className={cx(inputClass, 'mt-1')} value={note} onChange={(e) => setNote(e.target.value)}
                placeholder="Optional — e.g. “total confirmed with supplier”" />
            </div>
          </Card>

          <Card title="Amount check" padded={false}>
            <p className="px-4 pb-1 pt-3 text-xs text-slate-500">Subtotal, plus or minus other charges (shipping, discounts…), plus tax, should equal the total.</p>
            <div className="pb-4"><AmountCheck inv={inv} onJump={jumpToLine} /></div>
          </Card>

          <Card title={<>Validation <span className="ml-1 font-normal text-slate-500">{errors} error(s) · {warnings} warning(s)</span></>}>
            <IssueList issues={inv.issues} empty="Every check passed." onFieldClick={jumpTo} />
            <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1.5 border-t border-slate-100 pt-3 text-xs sm:grid-cols-3">
              {Object.entries(inv.checks).map(([name, outcome]) => (
                <div key={name} className="flex items-center gap-1.5" title={outcome}>
                  <CheckIcon outcome={outcome} />
                  <span className={outcome === 'skipped' ? 'text-slate-400' : 'text-slate-700'}>{CHECK_LABELS[name] ?? humanize(name)}</span>
                </div>
              ))}
            </div>
            {inv.extraction_notes.length > 0 && (
              <div className="mt-3 border-t border-slate-100 pt-3 text-xs text-slate-500">
                {inv.extraction_notes.map((n, i) => <div key={i}>Extraction: {n}</div>)}
              </div>
            )}
          </Card>

          <Card title={`Line items (${inv.line_items.length})`} padded={false}>
            {inv.line_items.length
              ? <LineItemsTable inv={inv} onJump={jumpToLine} />
              : <p className="p-4 text-sm text-slate-500">No line items were extracted.</p>}
          </Card>
        </div>

        <div className="xl:sticky xl:top-6 xl:self-start">
          {doc ? (
            <PageViewer className="h-[82vh]" documentId={inv.document_id} detectedType={doc.detected_type}
              pageCount={doc.page_count} page={page} onPageChange={(p) => { setPage(p); setHighlight(null) }}
              highlight={highlight} />
          ) : <Spinner />}
          <p className="mt-2 text-xs text-slate-500">Click a field or line to jump to the line it was read from.</p>
        </div>
      </div>
    </>
  )
}
