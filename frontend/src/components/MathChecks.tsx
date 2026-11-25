import type { ReactNode } from 'react'
import type { InvoiceDetail } from '../lib/api'
import { add, dec, isNeg, mul, str, sub, within, type Dec, abs } from '../lib/decimal'
import { humanize, money } from '../lib/format'
import { cx } from '../lib/styles'
import { Badge, CheckIcon } from './ui'

type Jump = (page: number | null, evidence: string | null) => void

function found(inv: InvoiceDetail, name: 'subtotal' | 'tax' | 'total'): Dec | null {
  const f = inv.fields[name]
  return f?.status === 'found' ? dec(f.value) : null
}

const VERDICT: Record<string, string> = {
  passed: 'The amounts add up.',
  warning: 'Probably fine, but worth a look (see the validation findings).',
  failed: 'The amounts do not add up.',
}

/**
 * Subtotal ± each other charge + tax = total, written out line by line. The verdict
 * comes from the backend's "totals" check; the numbers shown are exact decimals.
 */
export function AmountCheck({ inv, onJump }: { inv: InvoiceDetail; onJump: Jump }) {
  const subtotal = found(inv, 'subtotal')
  const tax = found(inv, 'tax')
  const total = found(inv, 'total')
  const outcome = inv.checks.totals ?? 'skipped'
  const charges = (inv.charges ?? []).map((c) => ({ ...c, value: dec(c.amount) }))
  const cur = inv.currency

  if (!subtotal || !total) {
    return (
      <div className="flex items-start gap-2 px-4 pt-2 text-sm text-slate-500">
        <CheckIcon outcome="skipped" />
        Not checked: the {!subtotal ? 'subtotal' : 'total'} was not found, so there is nothing to add up.
      </div>
    )
  }

  const parts = [subtotal, ...charges.flatMap((c) => (c.value ? [c.value] : [])), ...(tax ? [tax] : [])]
  const expected = add(...parts)
  const matches = within(expected, total, '0.01')
  // backend also accepts charges that are already included in the subtotal
  const includedInSubtotal = outcome === 'passed' && !matches && charges.length > 0
    && within(add(subtotal, ...(tax ? [tax] : [])), total, '0.01')

  const row = (key: string, sign: string, label: ReactNode, value: Dec | null, page: number | null, evidence: string | null, extra?: ReactNode) => (
    <tr key={key} className={cx(evidence && 'cursor-pointer hover:bg-slate-50')} onClick={() => evidence && onJump(page, evidence)}>
      <td className="w-6 py-1 pl-4 font-mono text-slate-400">{sign}</td>
      <td className="py-1 pr-2 text-slate-700">{label} {extra}</td>
      <td className="py-1 pr-4 text-right tabular-nums">{value ? money(str(value), cur) : <span className="text-amber-700">unreadable</span>}</td>
    </tr>
  )

  return (
    <div>
      <table className="w-full text-sm">
        <tbody>
          {row('subtotal', '', 'Subtotal', subtotal, inv.fields.subtotal?.page ?? null, inv.fields.subtotal?.evidence ?? null)}
          {charges.map((c, i) => row(
            `charge-${i}`, c.value && isNeg(c.value) ? '−' : '+', c.label ?? humanize(c.kind), c.value ? abs(c.value) : null,
            c.page, c.evidence, <Badge tone={c.kind === 'discount' ? 'green' : 'slate'}>{humanize(c.kind)}</Badge>,
          ))}
          {tax
            ? row('tax', '+', 'Tax', tax, inv.fields.tax?.page ?? null, inv.fields.tax?.evidence ?? null)
            : row('tax', '+', <span className="text-slate-400">Tax (none found)</span>, dec('0'), null, null)}
          <tr className="border-t border-slate-200 font-medium">
            <td className="py-1.5 pl-4 font-mono text-slate-400">=</td>
            <td className="py-1.5 pr-2 text-slate-700">Calculated total</td>
            <td className="py-1.5 pr-4 text-right tabular-nums">{money(str(expected), cur)}</td>
          </tr>
          {row('total', '', 'Total printed on the invoice', total, inv.fields.total?.page ?? null, inv.fields.total?.evidence ?? null)}
        </tbody>
      </table>
      <div className={cx('mx-4 mt-3 flex items-start gap-2 rounded-lg px-3 py-2 text-sm',
        outcome === 'passed' ? 'bg-emerald-50 text-emerald-800' : outcome === 'failed' ? 'bg-rose-50 text-rose-800' : 'bg-amber-50 text-amber-800')}>
        <CheckIcon outcome={outcome} />
        <span>
          {VERDICT[outcome] ?? 'Not checked.'}{' '}
          {matches
            ? 'Calculated and printed totals match.'
            : includedInSubtotal
              ? 'The other charges are already included in the subtotal.'
              : `Difference: ${money(str(sub(total, expected)), cur)}.`}
        </span>
      </div>
    </div>
  )
}

/** Line items with "quantity × unit price = amount" checked per row and the sum checked against the subtotal. */
export function LineItemsTable({ inv, onJump }: { inv: InvoiceDetail; onJump: Jump }) {
  const cur = inv.currency
  const mismatched = new Set(inv.issues.filter((i) => i.code === 'LINE_ITEM_MISMATCH').map((i) => i.field))
  const amounts = inv.line_items.map((li) => dec(li.amount))
  const complete = amounts.every((a) => a !== null)
  const total = complete && amounts.length ? add(...(amounts as Dec[])) : null
  const subtotal = found(inv, 'subtotal')
  const sumIssue = inv.issues.find((i) => i.code === 'LINE_ITEMS_SUM_MISMATCH')
  const sumOutcome = !complete || !subtotal ? 'skipped' : sumIssue ? 'warning' : 'passed'

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-4 py-2">Description</th>
            <th className="px-3 py-2 text-right">Qty</th>
            <th className="px-3 py-2 text-right">Unit price</th>
            <th className="px-3 py-2 text-right">Amount</th>
            <th className="px-3 py-2 text-right" title="Quantity × unit price = amount">Qty × price</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {inv.line_items.map((li, i) => {
            const q = dec(li.quantity)
            const u = dec(li.unit_price)
            const product = q && u ? mul(q, u) : null
            const failed = mismatched.has(`line_items[${i}].amount`)
            const outcome = !product || !amounts[i] ? 'skipped' : failed ? 'warning' : 'passed'
            const formula = product ? `${li.quantity} × ${li.unit_price} = ${str(product)}` : 'Not checked: quantity, unit price or amount is missing'
            return (
              <tr key={li.position} className="cursor-pointer hover:bg-slate-50" onClick={() => onJump(li.page, li.evidence)}>
                <td className="px-4 py-2">
                  {li.description ?? '—'}
                  {li.issues.length > 0 && <> <Badge tone="amber">{li.issues.map(humanize).join(', ')}</Badge></>}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{li.quantity ?? '—'}</td>
                <td className="px-3 py-2 text-right tabular-nums">{money(li.unit_price, cur)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{money(li.amount, cur)}</td>
                <td className="px-3 py-2 text-right" title={formula}>
                  <span className="inline-flex items-center gap-1.5">
                    {failed && product && <span className="text-xs tabular-nums text-amber-700">should be {money(str(product), cur)}</span>}
                    <CheckIcon outcome={outcome} />
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
        <tfoot className="border-t border-slate-200 bg-slate-50/60 text-sm">
          <tr>
            <td className="px-4 py-2 font-medium text-slate-700" colSpan={3}>
              Sum of lines {subtotal ? <span className="font-normal text-slate-500">vs. subtotal {money(str(subtotal), cur)}</span> : null}
            </td>
            <td className="px-3 py-2 text-right font-medium tabular-nums">{total ? money(str(total), cur) : '—'}</td>
            <td className="px-3 py-2 text-right" title={sumIssue?.message ?? (sumOutcome === 'skipped' ? 'Not checked' : 'The lines add up to the subtotal')}>
              <span className="inline-flex items-center gap-1.5">
                {sumIssue && total && subtotal && (
                  <span className="text-xs tabular-nums text-amber-700">differs by {money(str(abs(sub(subtotal, total))), cur)}</span>
                )}
                <CheckIcon outcome={sumOutcome} />
              </span>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  )
}
