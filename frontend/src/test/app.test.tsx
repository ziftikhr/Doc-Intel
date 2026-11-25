import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { fixtures, renderApp } from './harness'

const inv = fixtures.invoice

describe('dashboard', () => {
  it('shows counts, recent documents and the most common problems', async () => {
    renderApp('/')
    expect(await screen.findByText('Recent documents')).toBeTruthy()
    const needReview = screen.getByText('Need review').closest('a')!
    expect(within(needReview).getByText(String(fixtures.stats.needs_review))).toBeTruthy()
    expect(needReview.getAttribute('href')).toBe('/review')
    expect(screen.getAllByText(fixtures.documents.items[0].filename).length).toBeGreaterThan(0)
    const top = fixtures.stats.top_issues[0]
    const link = screen.getAllByRole('link').find((a) => a.getAttribute('href') === `/invoices?issue_code=${top.code}`)
    expect(link).toBeTruthy()
  })

  it('tells the user how to start the backend when it is down', async () => {
    renderApp('/', ({ path }) => (path === '/health' ? { status: 502, body: {} } : undefined))
    expect(await screen.findByText('Backend offline')).toBeTruthy()
  })
})

describe('documents', () => {
  it('lists documents and sends filters to the API', async () => {
    const { calls } = renderApp('/documents')
    expect(await screen.findByText('invoice_digital.pdf')).toBeTruthy()
    await userEvent.selectOptions(screen.getByLabelText('Processing status'), 'failed')
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/documents' && c.params.get('status') === 'failed')).toBe(true))
  })

  it('shows a document with its invoices and the page viewer', async () => {
    renderApp(`/documents/${fixtures.document.id}`)
    expect(await screen.findByText(`Invoices found (${fixtures.document.invoices.length})`)).toBeTruthy()
    const img = await screen.findByRole('img', { name: 'Page 1' })
    expect(img.getAttribute('src')).toBe(`/api/v1/documents/${fixtures.document.id}/pages/1/image?width=1400`)
  })
})

describe('invoice review', () => {
  it('shows extracted values, their source and the validation findings', async () => {
    renderApp(`/invoices/${inv.id}`)
    expect(await screen.findByText('Extracted fields')).toBeTruthy()
    expect(screen.getByText(inv.fields.supplier_name!.value!)).toBeTruthy()
    for (const issue of inv.issues) expect(screen.getByText(issue.message)).toBeTruthy()
    expect(screen.getByText('Subtotal + tax = total')).toBeTruthy()
  })

  it('jumps to the evidence line when a field is clicked', async () => {
    renderApp(`/invoices/${inv.id}`)
    const row = (await screen.findByText('Total', { selector: 'th' })).closest('tr')!
    await userEvent.click(row)
    const mark = await screen.findByText(inv.fields.total!.evidence!, { selector: 'mark' })
    expect(mark.className).toContain('evidence')
  })

  it('asks before approving an invoice that still has errors, then forces it', async () => {
    const posted: unknown[] = []
    renderApp(`/invoices/${inv.id}`, ({ method, path, body }) => {
      if (method !== 'POST' || !path.endsWith('/review')) return undefined
      posted.push(body)
      const b = body as { force?: boolean }
      if (!b.force) return { status: 409, body: { detail: 'still has validation errors', details: { errors: ['Duplicate of march.pdf'] } } }
      return { body: { ...inv, review_status: 'approved', needs_review: false, reviewed_at: '2026-11-24T12:00:00Z' } }
    })
    await userEvent.click(await screen.findByRole('button', { name: /Approve/ }))
    expect(await screen.findByText('Duplicate of march.pdf')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Approve anyway' }))
    await waitFor(() => expect(posted).toEqual([
      { action: 'approve', force: false },
      { action: 'approve', force: true },
    ]))
    expect(await screen.findByRole('button', { name: /Reopen/ })).toBeTruthy()
  })

  it('sends only the corrected fields', async () => {
    let sent: unknown
    renderApp(`/invoices/${inv.id}`, ({ method, path, body }) => {
      if (method === 'POST' && path.endsWith('/review')) {
        sent = body
        return { body: inv }
      }
      return undefined
    })
    await userEvent.click(await screen.findByRole('button', { name: /Correct/ }))
    const total = screen.getByLabelText('Total')
    await userEvent.clear(total)
    await userEvent.type(total, '230.00')
    await userEvent.click(screen.getByRole('button', { name: 'Save corrections' }))
    await waitFor(() => expect(sent).toEqual({ force: false, corrections: { total: '230.00' } }))
  })
})

describe('amount checks', () => {
  const shipping = {
    kind: 'shipping', label: 'Shipping & Handling', amount: '15.00', raw: '$15.00', page: 1,
    evidence: 'Shipping & Handling $15.00', source: 'rules', confidence: 0.78, issues: [],
  }

  it('writes out subtotal + shipping + tax = total', async () => {
    const withShipping = { ...inv, charges: [shipping], fields: { ...inv.fields, total: { ...inv.fields.total!, value: '240.50' } } }
    renderApp(`/invoices/${inv.id}`, ({ method, path }) =>
      method === 'GET' && path === `/api/v1/invoices/${inv.id}` ? { body: withShipping } : undefined)
    expect(await screen.findByText('Shipping & Handling')).toBeTruthy()
    // calculated 205.00 + 15.00 + 20.50, and the printed total
    expect(screen.getAllByText('$240.50', { selector: 'td' })).toHaveLength(2)
    expect(screen.getByText(/Calculated and printed totals match/)).toBeTruthy()
  })

  it('shows a discount as a subtraction and the difference when totals disagree', async () => {
    const discount = { ...shipping, kind: 'discount', label: 'Discount (2%)', amount: '-4.10', evidence: 'Discount (2%) -$4.10' }
    const wrong = {
      ...inv, charges: [discount], checks: { ...inv.checks, totals: 'failed' },
    }
    renderApp(`/invoices/${inv.id}`, ({ method, path }) =>
      method === 'GET' && path === `/api/v1/invoices/${inv.id}` ? { body: wrong } : undefined)
    const label = await screen.findByText('Discount (2%)')
    expect(within(label.closest('tr')!).getByText('−')).toBeTruthy()
    expect(screen.getByText(/do not add up/)).toBeTruthy()
    expect(screen.getByText(/Difference: \$4\.10/)).toBeTruthy() // 225.50 printed vs 221.40 calculated
  })

  it('checks quantity × unit price on every line and the sum against the subtotal', async () => {
    const bad = {
      ...inv,
      line_items: [{ ...inv.line_items[0], amount: '152.00' }, inv.line_items[1]],
      issues: [...inv.issues, {
        code: 'LINE_ITEM_MISMATCH', severity: 'warning', field: 'line_items[0].amount',
        message: "Line 'Widget A': 10 x 12.50 = 125.00, but the line amount is 152.00.", details: {},
      }, {
        code: 'LINE_ITEMS_SUM_MISMATCH', severity: 'warning', field: 'line_items',
        message: 'The 2 line items add up to 232.00, but the subtotal is 205.00.', details: {},
      }],
    }
    renderApp(`/invoices/${inv.id}`, ({ method, path }) =>
      method === 'GET' && path === `/api/v1/invoices/${inv.id}` ? { body: bad } : undefined)
    const row = (await screen.findByText('Widget A', { selector: 'td' })).closest('tr')!
    expect(within(row).getByText('should be $125.00')).toBeTruthy()
    expect(within(row).getByTitle('10 × 12.50 = 125.00')).toBeTruthy()
    const good = screen.getByText('Gadget B', { selector: 'td' }).closest('tr')!
    expect(within(good).getByTitle('2 × 40.00 = 80.00')).toBeTruthy()
    expect(within(good).queryByText(/should be/)).toBeNull()
    expect(screen.getByText('differs by $27.00')).toBeTruthy()
  })
})

describe('search', () => {
  it('runs a query from the URL and explains how each result matched', async () => {
    const { calls } = renderApp('/search?q=bakery')
    const first = fixtures.search.invoices[0]
    expect(await screen.findByText(first.supplier_name!)).toBeTruthy()
    expect(screen.getAllByText('meaning').length).toBeGreaterThan(0)
    expect(calls.find((c) => c.path === '/api/v1/search')!.params.get('q')).toBe('bakery')
    const passage = fixtures.search.passages[0]
    const link = screen.getAllByRole('link').find((a) => a.getAttribute('href') === `/documents/${passage.document_id}?page=${passage.page_number}`)
    expect(link).toBeTruthy()
  })

  it('offers examples before anything is typed', async () => {
    renderApp('/search')
    await userEvent.click(await screen.findByRole('button', { name: 'bakery' }))
    expect(await screen.findByText(fixtures.search.invoices[0].supplier_name!)).toBeTruthy()
  })
})

describe('review queue', () => {
  it('only asks for invoices that need review', async () => {
    const { calls } = renderApp('/review')
    await screen.findByText('Review queue', { selector: 'h1' })
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/invoices' && c.params.get('needs_review') === 'true')).toBe(true))
  })
})
