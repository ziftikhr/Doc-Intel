import { QueryClient } from '@tanstack/react-query'
import type { RouteObject } from 'react-router'
import { Layout } from './components/Layout'
import { ApiError } from './lib/api'
import { Dashboard } from './pages/Dashboard'
import { DocumentDetailPage } from './pages/DocumentDetailPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { InvoiceReviewPage } from './pages/InvoiceReviewPage'
import { InvoicesPage } from './pages/InvoicesPage'
import { NotFound } from './pages/NotFound'
import { SearchPage } from './pages/SearchPage'
import { UploadPage } from './pages/UploadPage'

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: true,
        // 4xx answers will not change by retrying
        retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
      },
    },
  })
}

// Shared by the browser router (App) and the memory router used in tests.
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'upload', element: <UploadPage /> },
      { path: 'documents', element: <DocumentsPage /> },
      { path: 'documents/:id', element: <DocumentDetailPage /> },
      { path: 'invoices', element: <InvoicesPage /> },
      { path: 'invoices/:id', element: <InvoiceReviewPage /> },
      { path: 'review', element: <InvoicesPage reviewQueue key="review" /> },
      { path: 'search', element: <SearchPage /> },
      { path: '*', element: <NotFound /> },
    ],
  },
]
