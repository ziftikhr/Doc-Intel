/**
 * Exact decimal arithmetic for showing the invoice math ("39 × 4.12 = 160.68").
 * Amounts arrive from the API as decimal strings; using JS numbers would show
 * things like 160.67999999. The pass/fail verdict always comes from the backend -
 * these helpers only display the calculation.
 */

export interface Dec {
  /** value = v / 10^s */
  v: bigint
  s: number
}

export function dec(x: string | null | undefined): Dec | null {
  if (x == null) return null
  const m = /^(-)?(\d+)(?:\.(\d+))?$/.exec(x.trim())
  if (!m) return null
  const frac = m[3] ?? ''
  const v = BigInt(m[2] + frac)
  return { v: m[1] ? -v : v, s: frac.length }
}

const at = (a: Dec, s: number) => a.v * 10n ** BigInt(s - a.s)

export function add(...xs: Dec[]): Dec {
  const s = Math.max(0, ...xs.map((x) => x.s))
  return { v: xs.reduce((acc, x) => acc + at(x, s), 0n), s }
}

export const neg = (a: Dec): Dec => ({ v: -a.v, s: a.s })
export const abs = (a: Dec): Dec => ({ v: a.v < 0n ? -a.v : a.v, s: a.s })
export const mul = (a: Dec, b: Dec): Dec => ({ v: a.v * b.v, s: a.s + b.s })
export const sub = (a: Dec, b: Dec): Dec => add(a, neg(b))
export const isNeg = (a: Dec) => a.v < 0n

/** |a - b| <= tol */
export function within(a: Dec, b: Dec, tol: string): boolean {
  const d = abs(sub(a, b))
  const t = dec(tol)!
  const s = Math.max(d.s, t.s)
  return at(d, s) <= at(t, s)
}

/** "225.5" -> "225.50"; extra decimals kept only when they are not zero ("12.345"). */
export function str(a: Dec): string {
  const negative = a.v < 0n
  let digits = (negative ? -a.v : a.v).toString().padStart(a.s + 1, '0')
  let s = a.s
  while (s > 2 && digits.endsWith('0')) {
    digits = digits.slice(0, -1)
    s--
  }
  let whole = s ? digits.slice(0, -s) : digits
  let frac = s ? digits.slice(-s) : ''
  if (frac.length < 2) frac = frac.padEnd(2, '0')
  whole = whole.replace(/^0+(?=\d)/, '')
  return `${negative ? '-' : ''}${whole}.${frac}`
}
