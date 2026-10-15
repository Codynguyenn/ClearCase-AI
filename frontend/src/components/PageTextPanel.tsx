import { useMemo, useState } from 'react'
import type { PageText } from '../api/types'

interface Props {
  page: PageText | null
  loading: boolean
  error: string | null
}

/**
 * Shows the extracted text for the current page with a simple client-side
 * find box. This proves the extraction worked and lets a reviewer read the
 * page even when the PDF rendering is small. This is a client-side find only;
 * the Search tab does the server-side search with occurrence ids and rectangles.
 */
export function PageTextPanel({ page, loading, error }: Props) {
  const [query, setQuery] = useState('')

  const segments = useMemo(() => {
    if (!page) return []
    const text = page.text
    const q = query.trim()
    if (!q) return [{ text, hit: false }]
    const out: { text: string; hit: boolean }[] = []
    const lower = text.toLowerCase()
    const needle = q.toLowerCase()
    let i = 0
    while (i < text.length) {
      const idx = lower.indexOf(needle, i)
      if (idx === -1) {
        out.push({ text: text.slice(i), hit: false })
        break
      }
      if (idx > i) out.push({ text: text.slice(i, idx), hit: false })
      out.push({ text: text.slice(idx, idx + needle.length), hit: true })
      i = idx + needle.length
    }
    return out
  }, [page, query])

  const hitCount = segments.filter((s) => s.hit).length

  return (
    <div className="flex h-full min-h-0 flex-col" role="region" aria-label="Page text">
      <div className="border-b border-slate-200 p-3">
        <h2 className="text-sm font-semibold text-slate-800">Extracted text</h2>
        {page && (
          <p className="text-xs text-slate-500">
            Page {page.page_number} · {page.word_count} words · {page.kind}
            {!page.supported && <span className="ml-1 font-medium text-amber-700">(not analysable)</span>}
          </p>
        )}
        <label htmlFor="find-in-page" className="sr-only">
          Find in page
        </label>
        <input
          id="find-in-page"
          type="search"
          placeholder="Find in page…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="mt-2 w-full rounded border border-slate-300 px-2 py-1 text-sm"
          disabled={!page}
        />
        {query && page && (
          <p className="mt-1 text-xs text-slate-500" aria-live="polite">
            {hitCount} match{hitCount === 1 ? '' : 'es'} on this page
          </p>
        )}
      </div>
      <div className="flex-1 overflow-auto p-3">
        {loading && <p className="text-sm text-slate-500">Loading text…</p>}
        {error && (
          <p role="alert" className="rounded bg-red-50 p-2 text-xs text-red-700">
            {error}
          </p>
        )}
        {!loading && !error && page && page.kind !== 'text' && (
          <p className="mb-2 rounded bg-amber-50 p-2 text-xs text-amber-800">
            {page.kind === 'scanned' &&
              'This page is an image with no extractable text. OCR is not implemented, so it cannot be searched or redacted by text.'}
            {page.kind === 'mixed' &&
              'This page mixes text with a large image. Only the text layer is searchable; content inside the image is not analysed.'}
            {page.kind === 'empty' && 'This page has no extractable text.'}
          </p>
        )}
        {!loading && !error && page && (
          <pre className="whitespace-pre-wrap font-sans text-[13px] leading-relaxed text-slate-800" data-testid="page-text">
            {segments.map((s, i) =>
              s.hit ? (
                <mark key={i} className="rounded bg-yellow-200 px-0.5">
                  {s.text}
                </mark>
              ) : (
                <span key={i}>{s.text}</span>
              ),
            )}
          </pre>
        )}
        {!loading && !error && !page && <p className="text-sm text-slate-500">Select a document to view its text.</p>}
      </div>
    </div>
  )
}
