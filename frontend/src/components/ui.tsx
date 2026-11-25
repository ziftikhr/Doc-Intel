import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle } from 'lucide-react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../lib/api'
import { cx } from '../lib/styles'

// ------------------------------------------------------------------- badges ----

type Tone = 'green' | 'amber' | 'red' | 'blue' | 'slate' | 'indigo' | 'violet'

const TONES: Record<Tone, string> = {
  green: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  amber: 'bg-amber-50 text-amber-800 ring-amber-600/25',
  red: 'bg-rose-50 text-rose-700 ring-rose-600/20',
  blue: 'bg-sky-50 text-sky-700 ring-sky-600/20',
  slate: 'bg-slate-100 text-slate-600 ring-slate-500/20',
  indigo: 'bg-indigo-50 text-indigo-700 ring-indigo-600/20',
  violet: 'bg-violet-50 text-violet-700 ring-violet-600/20',
}

export function Badge({ tone = 'slate', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title}
      className={cx('inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset', TONES[tone])}>
      {children}
    </span>
  )
}

const STATUS: Record<string, { tone: Tone; label: string; spin?: boolean }> = {
  valid: { tone: 'green', label: 'Valid' },
  needs_review: { tone: 'amber', label: 'Needs review' },
  invalid: { tone: 'red', label: 'Invalid' },
  no_invoices: { tone: 'slate', label: 'No invoices' },
  failed: { tone: 'red', label: 'Failed' },
  queued: { tone: 'slate', label: 'Queued', spin: true },
  processing: { tone: 'blue', label: 'Processing', spin: true },
  running: { tone: 'blue', label: 'Running', spin: true },
  processed: { tone: 'green', label: 'Processed' },
  succeeded: { tone: 'green', label: 'Done' },
  pending: { tone: 'slate', label: 'Pending' },
  approved: { tone: 'green', label: 'Approved' },
  rejected: { tone: 'red', label: 'Rejected' },
  indexed: { tone: 'green', label: 'Indexed' },
  keyword_only: { tone: 'amber', label: 'Keyword only' },
  empty: { tone: 'slate', label: 'Nothing to index' },
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <Badge>—</Badge>
  const s = STATUS[status] ?? { tone: 'slate' as Tone, label: status }
  return (
    <Badge tone={s.tone}>
      {s.spin && <Loader2 className="size-3 animate-spin" />}
      {s.label}
    </Badge>
  )
}

export function SeverityIcon({ severity, className = 'size-4' }: { severity: string; className?: string }) {
  if (severity === 'error') return <XCircle className={cx(className, 'text-rose-600')} />
  if (severity === 'warning') return <AlertTriangle className={cx(className, 'text-amber-600')} />
  return <Info className={cx(className, 'text-slate-400')} />
}

export function SourceBadge({ source }: { source: string | null | undefined }) {
  if (!source) return null
  const tone: Tone = source === 'llm+rules' ? 'green' : source === 'manual' ? 'violet' : source === 'llm' ? 'indigo' : 'slate'
  const label = { 'llm+rules': 'AI + rules', llm: 'AI', rules: 'Rules', spreadsheet: 'Spreadsheet', manual: 'Corrected' }[source] ?? source
  const title = {
    'llm+rules': 'The AI model and the text rules found the same value',
    llm: 'Found by the AI model only',
    rules: 'Found by the text rules only',
    spreadsheet: 'Read from a spreadsheet column',
    manual: 'Corrected by a reviewer',
  }[source]
  return <Badge tone={tone} title={title}>{label}</Badge>
}

// ------------------------------------------------------------------ layout ----

export function Card({ title, actions, children, className, padded = true }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; padded?: boolean
}) {
  return (
    <section className={cx('rounded-xl border border-slate-200 bg-white shadow-sm', className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={padded ? 'p-4' : undefined}>{children}</div>
    </section>
  )
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="truncate text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <div className="mt-1 text-sm text-slate-500">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

// ------------------------------------------------------------------ controls ----

type Variant = 'primary' | 'secondary' | 'danger' | 'success' | 'ghost'
const VARIANTS: Record<Variant, string> = {
  primary: 'bg-indigo-600 text-white hover:bg-indigo-500 shadow-sm',
  secondary: 'bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 shadow-sm',
  danger: 'bg-rose-600 text-white hover:bg-rose-500 shadow-sm',
  success: 'bg-emerald-600 text-white hover:bg-emerald-500 shadow-sm',
  ghost: 'text-slate-600 hover:bg-slate-100',
}

export function Button({ variant = 'secondary', loading, className, children, disabled, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }) {
  return (
    <button {...rest} disabled={disabled || loading}
      className={cx('inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50', VARIANTS[variant], className)}>
      {loading && <Loader2 className="size-4 animate-spin" />}
      {children}
    </button>
  )
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
    </label>
  )
}

// ----------------------------------------------------------------- feedback ----

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-slate-500">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  )
}

export function ErrorBox({ error, title = 'Something went wrong' }: { error: unknown; title?: string }) {
  const message = error instanceof ApiError || error instanceof Error ? error.message : String(error)
  return (
    <div role="alert" className="flex gap-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
      <XCircle className="mt-0.5 size-4 shrink-0" />
      <div>
        <div className="font-medium">{title}</div>
        <div className="mt-0.5 break-words">{message}</div>
      </div>
    </div>
  )
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      {icon && <div className="text-slate-300">{icon}</div>}
      <div className="text-sm font-medium text-slate-700">{title}</div>
      {children && <div className="max-w-md text-sm text-slate-500">{children}</div>}
    </div>
  )
}

export function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  const color = value >= 0.85 ? 'bg-emerald-500' : value >= 0.6 ? 'bg-amber-500' : 'bg-rose-500'
  return (
    <div className="flex items-center gap-2" title={`Confidence ${pct}%`}>
      <div className="h-1.5 w-14 overflow-hidden rounded-full bg-slate-200">
        <div className={cx('h-full rounded-full', color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-8 text-xs tabular-nums text-slate-500">{pct}%</span>
    </div>
  )
}

export function CheckIcon({ outcome }: { outcome: string }) {
  if (outcome === 'passed') return <CheckCircle2 className="size-4 text-emerald-600" />
  if (outcome === 'failed') return <XCircle className="size-4 text-rose-600" />
  if (outcome === 'warning') return <AlertTriangle className="size-4 text-amber-600" />
  return <span className="inline-block size-4 rounded-full border-2 border-dashed border-slate-300" />
}

export function Pagination({ total, limit, offset, onChange }: {
  total: number; limit: number; offset: number; onChange: (offset: number) => void
}) {
  if (total <= limit) return null
  const page = Math.floor(offset / limit) + 1
  const pages = Math.ceil(total / limit)
  return (
    <div className="flex items-center justify-between border-t border-slate-100 px-4 py-3 text-sm text-slate-600">
      <span>{offset + 1}–{Math.min(offset + limit, total)} of {total}</span>
      <div className="flex gap-2">
        <Button disabled={page <= 1} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</Button>
        <Button disabled={page >= pages} onClick={() => onChange(offset + limit)}>Next</Button>
      </div>
    </div>
  )
}

export function Stat({ label, value, tone, hint, to }: { label: string; value: ReactNode; tone?: 'green' | 'amber' | 'red'; hint?: ReactNode; to?: string }) {
  const color = tone === 'green' ? 'text-emerald-600' : tone === 'amber' ? 'text-amber-600' : tone === 'red' ? 'text-rose-600' : 'text-slate-900'
  const body = (
    <>
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={cx('mt-1 text-2xl font-semibold tabular-nums', color)}>{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </>
  )
  const cls = 'block rounded-xl border border-slate-200 bg-white p-4 shadow-sm'
  return to
    ? <Link to={to} className={cx(cls, 'transition hover:border-indigo-300 hover:shadow')}>{body}</Link>
    : <div className={cls}>{body}</div>
}
