import { useQuery } from '@tanstack/react-query'
import { ClipboardCheck, FileText, LayoutDashboard, Receipt, Search, Upload } from 'lucide-react'
import { NavLink, Outlet } from 'react-router'
import { api } from '../lib/api'
import { cx } from '../lib/styles'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/upload', label: 'Upload', icon: Upload },
  { to: '/documents', label: 'Documents', icon: FileText },
  { to: '/invoices', label: 'Invoices', icon: Receipt },
  { to: '/review', label: 'Review queue', icon: ClipboardCheck, badge: true },
  { to: '/search', label: 'Search', icon: Search },
]

function HealthDot() {
  const { data, error } = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15_000, retry: false })
  const ok = data?.status === 'ok'
  const label = error ? 'Backend offline' : !data ? 'Connecting…' : ok ? 'Backend online' : 'Backend degraded'
  return (
    <div className="space-y-1 rounded-lg bg-slate-800/60 p-3 text-xs text-slate-400">
      <div className="flex items-center gap-2 font-medium text-slate-200">
        <span className={cx('size-2 rounded-full', error ? 'bg-rose-500' : ok ? 'bg-emerald-400' : 'bg-amber-400')} />
        {label}
      </div>
      {data && (
        <>
          <div title="Extraction model">AI: {data.llm}</div>
          <div title="Embedding model for semantic search">Search: {data.embeddings}</div>
          <div>Queue: {data.queue.queued ?? 0} waiting · {data.queue.running ?? 0} running</div>
        </>
      )}
      {error && <div>Start it with <code className="text-slate-300">docintel serve</code></div>}
    </div>
  )
}

function ReviewCount() {
  const { data } = useQuery({ queryKey: ['stats'], queryFn: api.stats, refetchInterval: 10_000 })
  if (!data?.needs_review) return null
  return <span className="ml-auto rounded-full bg-amber-500 px-1.5 text-[11px] font-semibold text-white">{data.needs_review}</span>
}

export function Layout() {
  return (
    <div className="flex h-full">
      <aside className="flex w-60 shrink-0 flex-col bg-slate-900 px-3 py-4">
        <div className="mb-6 flex items-center gap-2 px-2">
          <img src="/favicon.svg" alt="" className="size-7" />
          <div>
            <div className="text-sm font-semibold text-white">DocIntel</div>
            <div className="text-[11px] text-slate-400">Invoice intelligence</div>
          </div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5">
          {NAV.map(({ to, label, icon: Icon, end, badge }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) => cx(
                'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition',
                isActive ? 'bg-slate-800 text-white' : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200',
              )}>
              <Icon className="size-4" /> {label}
              {badge && <ReviewCount />}
            </NavLink>
          ))}
        </nav>
        <HealthDot />
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
