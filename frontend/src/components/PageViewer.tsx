import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, ExternalLink, FileImage, FileText as FileTextIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, fetchBlobUrl, fileUrl, needsAuthedMedia, pageImageUrl } from '../lib/api'
import { percent } from '../lib/format'
import { Button, Empty, ErrorBox, Spinner } from './ui'
import { cx } from '../lib/styles'

const PREVIEWABLE = new Set(['pdf', 'png', 'jpeg', 'tiff'])

const IMG_CLASS = 'mx-auto block h-auto max-w-full bg-white shadow'

function PlainImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false)
  if (failed) return <Empty title="The page image could not be rendered" />
  return <img src={src} alt={alt} onError={() => setFailed(true)} className={IMG_CLASS} />
}

/** <img> cannot send headers, so with an API key the image is fetched as a blob first. */
function AuthedImage({ src, alt }: { src: string; alt: string }) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let revoke: string | null = null
    fetchBlobUrl(src)
      .then((u) => { revoke = u; setUrl(u) })
      .catch(() => setFailed(true))
    return () => { if (revoke) URL.revokeObjectURL(revoke) }
  }, [src])
  if (failed) return <Empty title="The page image could not be rendered" />
  if (!url) return <Spinner label="Rendering page…" />
  return <img src={url} alt={alt} className={IMG_CLASS} />
}

// keyed by src: a new page gets fresh state instead of resetting it in an effect
function PageImage({ src, alt }: { src: string; alt: string }) {
  return needsAuthedMedia ? <AuthedImage key={src} src={src} alt={alt} /> : <PlainImage key={src} src={src} alt={alt} />
}

function escapeRegExp(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Page text with the evidence line highlighted (whitespace-insensitive match). */
function HighlightedText({ text, highlight }: { text: string; highlight?: string | null }) {
  const ref = useRef<HTMLPreElement>(null)
  const parts = useMemo(() => {
    const h = highlight?.trim()
    if (!h) return [text]
    const pattern = h.split(/\s+/).map(escapeRegExp).join('\\s+')
    return text.split(new RegExp(`(${pattern})`, 'i'))
  }, [text, highlight])
  useEffect(() => {
    ref.current?.querySelector('mark')?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [parts])
  return (
    <pre ref={ref} className="h-full overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed text-slate-700">
      {parts.map((p, i) => (i % 2 === 1 ? <mark key={i} className="evidence">{p}</mark> : p))}
      {!text.trim() && <span className="text-slate-400">(no text was extracted from this page)</span>}
    </pre>
  )
}

export function PageViewer({ documentId, detectedType, pageCount, page, onPageChange, highlight, className }: {
  documentId: string
  detectedType: string | null
  pageCount: number
  page: number
  onPageChange: (page: number) => void
  highlight?: string | null
  className?: string
}) {
  const canPreview = PREVIEWABLE.has(detectedType ?? '')
  const [mode, setMode] = useState<'image' | 'text'>(canPreview ? 'image' : 'text')
  const pages = useQuery({ queryKey: ['pages', documentId], queryFn: () => api.pages(documentId) })
  const total = Math.max(pageCount, pages.data?.length ?? 0, 1)
  const current = pages.data?.find((p) => p.number === page)

  // Jumping to an evidence line is most useful in the text view, where it can be
  // highlighted: switch when a new highlight arrives (adjusting state during render,
  // as React recommends, instead of in an effect).
  const [lastHighlight, setLastHighlight] = useState(highlight)
  if (highlight !== lastHighlight) {
    setLastHighlight(highlight)
    if (highlight) setMode('text')
  }

  return (
    <div className={cx('flex min-h-0 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm', className)}>
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-3 py-2">
        <div className="flex rounded-lg bg-slate-100 p-0.5 text-xs font-medium">
          <button disabled={!canPreview} onClick={() => setMode('image')}
            className={cx('flex items-center gap-1 rounded-md px-2 py-1', mode === 'image' ? 'bg-white shadow-sm' : 'text-slate-500', !canPreview && 'opacity-40')}
            title={canPreview ? 'Original page' : 'No page image for spreadsheets'}>
            <FileImage className="size-3.5" /> Original
          </button>
          <button onClick={() => setMode('text')}
            className={cx('flex items-center gap-1 rounded-md px-2 py-1', mode === 'text' ? 'bg-white shadow-sm' : 'text-slate-500')}>
            <FileTextIcon className="size-3.5" /> Extracted text
          </button>
        </div>
        <div className="ml-auto flex items-center gap-1 text-xs text-slate-600">
          <Button variant="ghost" className="px-1.5" disabled={page <= 1} onClick={() => onPageChange(page - 1)} aria-label="Previous page">
            <ChevronLeft className="size-4" />
          </Button>
          <span className="tabular-nums">Page {page} / {total}</span>
          <Button variant="ghost" className="px-1.5" disabled={page >= total} onClick={() => onPageChange(page + 1)} aria-label="Next page">
            <ChevronRight className="size-4" />
          </Button>
          <a href={fileUrl(documentId)} target="_blank" rel="noreferrer" className="ml-1 inline-flex items-center gap-1 rounded-md px-1.5 py-1 hover:bg-slate-100" title="Open the original file">
            <ExternalLink className="size-3.5" />
          </a>
        </div>
      </div>
      {current && (
        <div className="flex gap-3 border-b border-slate-100 bg-slate-50 px-3 py-1.5 text-[11px] text-slate-500">
          <span>{current.label}</span>
          <span>read as: {current.method.replace('_', ' ')}</span>
          {current.ocr_confidence !== null && <span>OCR confidence {percent(current.ocr_confidence)}</span>}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto bg-slate-100/60">
        {mode === 'image' && canPreview ? (
          <div className="p-3"><PageImage src={pageImageUrl(documentId, page)} alt={`Page ${page}`} /></div>
        ) : pages.isPending ? (
          <Spinner />
        ) : pages.error ? (
          <div className="p-3"><ErrorBox error={pages.error} /></div>
        ) : (
          <div className="h-full bg-white"><HighlightedText text={current?.text ?? ''} highlight={highlight} /></div>
        )}
      </div>
    </div>
  )
}
