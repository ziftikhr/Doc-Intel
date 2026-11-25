// Display helpers. Money strings are formatted for reading only.

export function money(value: string | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return value
  const decimals = Math.max(2, (value.split('.')[1] ?? '').length)
  try {
    if (currency && /^[A-Z]{3}$/.test(currency)) {
      return new Intl.NumberFormat(undefined, {
        style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: decimals,
      }).format(n)
    }
  } catch {
    /* unknown currency code: fall through */
  }
  return new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: decimals }).format(n)
}

export function date(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value.length === 10 ? `${value}T00:00:00` : value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function timeAgo(value: string | null | undefined, now = Date.now()): string {
  if (!value) return '—'
  const s = Math.round((now - new Date(value).getTime()) / 1000)
  if (!Number.isFinite(s)) return value
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function percent(fraction: number | null | undefined, digits = 0): string {
  return fraction === null || fraction === undefined ? '—' : `${(fraction * 100).toFixed(digits)}%`
}

export const FIELD_LABELS: Record<string, string> = {
  invoice_number: 'Invoice number',
  invoice_date: 'Invoice date',
  supplier_name: 'Supplier',
  customer_name: 'Customer',
  subtotal: 'Subtotal',
  tax: 'Tax',
  total: 'Total',
  currency: 'Currency',
}

export const CHECK_LABELS: Record<string, string> = {
  required_fields: 'Required fields',
  field_quality: 'Extraction quality',
  amounts: 'Amounts',
  dates: 'Dates',
  currency: 'Currency',
  totals: 'Subtotal + tax = total',
  tax_rate: 'Tax rate',
  line_items: 'Line items',
  parties: 'Supplier / customer',
  duplicates: 'Duplicates',
}

export function humanize(code: string): string {
  return code.toLowerCase().replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}
