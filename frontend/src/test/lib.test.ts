import { describe, expect, it, vi } from 'vitest'
import { ApiError, api, errorMessage, qs } from '../lib/api'
import { add, dec, mul, str, within } from '../lib/decimal'
import { money, timeAgo } from '../lib/format'

describe('format', () => {
  it('formats decimal strings without losing extra precision', () => {
    expect(money('225.50', 'USD')).toMatch(/225\.50/)
    expect(money('12.345', 'EUR')).toMatch(/12\.345/)
    expect(money(null)).toBe('—')
    expect(money('not a number')).toBe('not a number')
    expect(money('10', 'XX')).toMatch(/10\.00/) // invalid currency code falls back to plain number
  })

  it('describes times relatively', () => {
    const now = Date.parse('2026-11-24T12:00:00Z')
    expect(timeAgo('2026-11-24T11:59:40Z', now)).toBe('just now')
    expect(timeAgo('2026-11-24T11:30:00Z', now)).toBe('30 min ago')
    expect(timeAgo('2026-11-22T12:00:00Z', now)).toBe('2 d ago')
  })
})

describe('decimal', () => {
  it('multiplies and adds exactly', () => {
    expect(str(mul(dec('39')!, dec('4.12')!))).toBe('160.68')
    expect(str(mul(dec('3')!, dec('33.333')!))).toBe('99.999')
    expect(str(add(dec('0.1')!, dec('0.2')!))).toBe('0.30') // not 0.30000000000000004
    expect(str(add(dec('12540.11')!, dec('15.00')!, dec('1034.56')!))).toBe('13589.67')
    expect(str(add(dec('53367.39')!, dec('-1067.35')!))).toBe('52300.04')
    expect(str(dec('-0.5')!)).toBe('-0.50')
    expect(dec('1,234.00')).toBeNull()
  })

  it('compares within a tolerance', () => {
    expect(within(dec('225.51')!, dec('225.50')!, '0.01')).toBe(true)
    expect(within(dec('225.52')!, dec('225.50')!, '0.01')).toBe(false)
  })
})

describe('api client', () => {
  it('drops empty query parameters', () => {
    expect(qs({ a: 'x', b: '', c: undefined, d: 0, e: false })).toBe('?a=x&d=0&e=false')
    expect(qs({})).toBe('')
  })

  it('turns FastAPI errors into readable messages', () => {
    expect(errorMessage({ detail: 'invoice not found' }, 'x').message).toBe('invoice not found')
    expect(errorMessage({ detail: [{ loc: ['query', 'limit'], msg: 'too big' }] }, 'x').message).toBe('limit: too big')
    expect(errorMessage(null, 'fallback').message).toBe('fallback')
  })

  it('raises ApiError with status and details', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: 'nope', details: { errors: ['a'] } }), { status: 409 })))
    const err = await api.invoice('x').catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect([err.status, err.message, err.details]).toEqual([409, 'nope', { errors: ['a'] }])
  })

  it('explains an unreachable backend', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('fetch failed') }))
    await expect(api.stats()).rejects.toThrow(/Cannot reach the backend/)
  })
})
