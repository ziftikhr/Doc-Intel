import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { vi } from 'vitest'
import { makeQueryClient, routes } from '../routes'
import fixtures from './fixtures.json'

export { fixtures }

export interface Call {
  method: string
  path: string
  params: URLSearchParams
  body: unknown
}

type Handler = (call: Call) => { status?: number; body: unknown } | undefined

/**
 * Mount the real app routes at ``path`` with ``fetch`` answered from recorded API
 * responses (fixtures.json was captured from the running backend). ``handler`` can
 * override any request; every request is recorded in ``calls``.
 */
export function renderApp(path: string, handler: Handler = () => undefined) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const call: Call = {
      method: (init?.method ?? 'GET').toUpperCase(),
      path: url.pathname,
      params: url.searchParams,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : init?.body ?? null,
    }
    calls.push(call)
    const answer = handler(call) ?? defaultAnswer(call)
    return new Response(JSON.stringify(answer.body), {
      status: answer.status ?? 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetchMock)

  const client = makeQueryClient()
  client.setDefaultOptions({ queries: { retry: false, staleTime: Infinity, refetchInterval: false } })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const utils = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...utils, calls, router }
}

function defaultAnswer({ path }: Call): { status?: number; body: unknown } {
  const f = fixtures as Record<string, unknown> & typeof fixtures
  if (path === '/health') return { body: f.health }
  if (path === '/api/v1/stats') return { body: f.stats }
  if (path === '/api/v1/documents') return { body: f.documents }
  if (path === '/api/v1/invoices') return { body: f.invoices }
  if (path === '/api/v1/search') return { body: f.search }
  if (/^\/api\/v1\/invoices\/[^/]+$/.test(path)) return { body: f.invoice }
  if (/^\/api\/v1\/documents\/[^/]+\/pages$/.test(path)) return { body: f.pages }
  const doc = path.match(/^\/api\/v1\/documents\/([^/]+)$/)
  if (doc) return { body: doc[1] === f.photoDocument.id ? f.photoDocument : f.document }
  return { status: 404, body: { detail: `no fixture for ${path}` } }
}
