import { useQueries, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, FileUp, Loader2, RotateCcw, XCircle } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router'
import { Badge, Button, Card, PageHeader } from '../components/ui'
import { cx } from '../lib/styles'
import { ApiError, api, uploadFile, type Job, type UploadResult } from '../lib/api'
import { bytes } from '../lib/format'

const ACCEPT = '.pdf,.png,.jpg,.jpeg,.tif,.tiff,.xlsx,.xlsm,.xls,.csv,.tsv,.txt'

interface Item {
  key: string
  file: File
  progress: number
  result?: UploadResult
  error?: string
}

const STAGES: Record<string, string> = {
  processing: 'Reading the file (OCR)…',
  extracting: 'Extracting invoice fields…',
  validating: 'Validating…',
  indexing: 'Indexing for search…',
  'waiting to retry': 'Retrying after an error…',
}

function JobProgress({ job }: { job: Job | undefined }) {
  if (!job) return null
  const done = job.status === 'succeeded' || job.status === 'failed'
  return (
    <div className="mt-2">
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-200">
        <div className={cx('h-full rounded-full transition-all', job.status === 'failed' ? 'bg-rose-500' : done ? 'bg-emerald-500' : 'bg-indigo-500')}
          style={{ width: `${Math.max(job.progress, 4)}%` }} />
      </div>
      <div className="mt-1 text-xs text-slate-500">
        {job.status === 'queued' && job.stage !== 'waiting to retry' ? 'Waiting for a worker…'
          : job.status === 'failed' ? `Failed: ${job.error?.split('\n')[0] ?? 'unknown error'}`
            : done ? 'Finished' : STAGES[job.stage ?? ''] ?? 'Working…'}
      </div>
    </div>
  )
}

export function UploadPage() {
  const [items, setItems] = useState<Item[]>([])
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const qc = useQueryClient()

  const update = (key: string, patch: Partial<Item>) =>
    setItems((xs) => xs.map((x) => (x.key === key ? { ...x, ...patch } : x)))

  const start = useCallback((files: FileList | File[], reprocess = false) => {
    const added = Array.from(files).map((file) => ({ key: `${file.name}-${file.size}-${Math.random()}`, file, progress: 0 }))
    setItems((xs) => [...added, ...xs])
    for (const it of added) {
      uploadFile(it.file, (p) => update(it.key, { progress: p }), reprocess)
        .then((result) => {
          update(it.key, { result, progress: 1 })
          qc.invalidateQueries({ queryKey: ['documents'] })
        })
        .catch((e: unknown) => update(it.key, { error: e instanceof ApiError ? e.message : String(e) }))
    }
  }, [qc])

  // Poll every running job until it finishes.
  const jobIds = items.map((i) => i.result?.job?.id).filter((x): x is string => Boolean(x))
  const jobs = useQueries({
    queries: jobIds.map((id) => ({
      queryKey: ['job', id],
      queryFn: () => api.job(id),
      refetchInterval: (q: { state: { data?: Job } }) =>
        q.state.data && ['succeeded', 'failed'].includes(q.state.data.status) ? false : 1500,
    })),
  })
  const jobById = Object.fromEntries(jobs.map((q) => q.data).filter(Boolean).map((j) => [j!.id, j!]))

  return (
    <>
      <PageHeader title="Upload documents"
        subtitle="PDFs (digital or scanned), photos and scans (PNG, JPEG, TIFF) and spreadsheets (XLSX, XLS, CSV). Processing runs in the background — you can leave this page." />

      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); if (e.dataTransfer.files.length) start(e.dataTransfer.files) }}
        onClick={() => input.current?.click()}
        role="button" tabIndex={0} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
        className={cx('flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-14 text-center transition',
          dragging ? 'border-indigo-500 bg-indigo-50' : 'border-slate-300 bg-white hover:border-indigo-400 hover:bg-slate-50')}>
        <FileUp className={cx('size-10', dragging ? 'text-indigo-500' : 'text-slate-400')} />
        <div className="mt-3 text-sm font-medium text-slate-800">Drop files here, or click to choose</div>
        <div className="mt-1 text-xs text-slate-500">Several files at once are fine · up to 50 MB each</div>
        <input ref={input} type="file" multiple accept={ACCEPT} className="hidden"
          onChange={(e) => { if (e.target.files?.length) start(e.target.files); e.target.value = '' }} />
      </div>

      {items.length > 0 && (
        <Card title={`Uploads (${items.length})`} className="mt-6" padded={false}
          actions={<Button variant="ghost" onClick={() => setItems([])}>Clear list</Button>}>
          <ul className="divide-y divide-slate-100">
            {items.map((it) => {
              const job = it.result?.job ? jobById[it.result.job.id] ?? it.result.job : undefined
              const doc = it.result?.document
              return (
                <li key={it.key} className="flex items-start gap-3 px-4 py-3">
                  <div className="mt-0.5">
                    {it.error ? <XCircle className="size-5 text-rose-500" />
                      : job?.status === 'succeeded' ? <CheckCircle2 className="size-5 text-emerald-500" />
                        : job?.status === 'failed' ? <XCircle className="size-5 text-rose-500" />
                          : <Loader2 className="size-5 animate-spin text-indigo-500" />}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-sm font-medium text-slate-800">{it.file.name}</span>
                      <span className="text-xs text-slate-400">{bytes(it.file.size)}</span>
                      {it.result?.duplicate_upload && <Badge tone="amber">Already uploaded</Badge>}
                    </div>
                    {it.error ? (
                      <div className="mt-1 text-xs text-rose-600">{it.error}</div>
                    ) : !it.result ? (
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200">
                        <div className="h-full bg-slate-400 transition-all" style={{ width: `${Math.round(it.progress * 100)}%` }} />
                      </div>
                    ) : (
                      <>
                        {it.result.duplicate_upload && <div className="mt-1 text-xs text-amber-700">{it.result.message}</div>}
                        {it.result.duplicate_upload && !job ? null : <JobProgress job={job} />}
                      </>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {it.result?.duplicate_upload && (
                      <Button variant="ghost" title="Process this file again" onClick={() => start([it.file], true)}>
                        <RotateCcw className="size-4" /> Reprocess
                      </Button>
                    )}
                    {doc && (
                      <Link to={`/documents/${doc.id}`} className="text-sm font-medium text-indigo-600 hover:underline">Open</Link>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
        </Card>
      )}
    </>
  )
}
