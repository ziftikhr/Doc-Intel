import type { Issue } from '../lib/api'
import { FIELD_LABELS, humanize } from '../lib/format'
import { SeverityIcon } from './ui'

const ORDER = { error: 0, warning: 1, info: 2 }

export function IssueList({ issues, empty = 'No issues found.', onFieldClick }: {
  issues: Issue[]
  empty?: string
  onFieldClick?: (field: string) => void
}) {
  if (!issues.length) return <p className="text-sm text-slate-500">{empty}</p>
  const sorted = [...issues].sort((a, b) => ORDER[a.severity] - ORDER[b.severity])
  return (
    <ul className="space-y-2">
      {sorted.map((issue, i) => {
        const field = issue.field?.split('[')[0].split('.')[0]
        return (
          <li key={i} className="flex gap-2.5 text-sm">
            <SeverityIcon severity={issue.severity} className="mt-0.5 size-4 shrink-0" />
            <div className="min-w-0">
              <div className="text-slate-800">{issue.message}</div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-slate-400">
                <span className="font-mono">{issue.code}</span>
                {field && FIELD_LABELS[field] && (
                  onFieldClick
                    ? <button className="text-indigo-600 hover:underline" onClick={() => onFieldClick(field)}>{FIELD_LABELS[field]}</button>
                    : <span>{FIELD_LABELS[field]}</span>
                )}
                {field === 'line_items' && <span>{humanize(field)}</span>}
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}
