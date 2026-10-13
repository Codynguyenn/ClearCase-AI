import { useState } from 'react'
import { api } from '../api/client'
import type { Category, Occurrence, SearchResult } from '../api/types'

interface Props {
  documentId: string
  busy: boolean
  /** Occurrences are lifted to App so the viewer can outline them. */
  result: SearchResult | null
  onResult: (r: SearchResult | null) => void
  onJump: (o: Occurrence) => void
  onPropose: (
    q: string,
    opts: { caseSensitive: boolean; wholeWord: boolean },
    category: Category,
    selection: { occurrenceIds?: string[]; allMatches?: boolean },
  ) => void
}

const CATEGORY_CHOICES: Category[] = ['CUSTOM', 'PERSON', 'ORGANIZATION', 'LOCATION', 'ADDRESS', 'EMAIL', 'PHONE', 'SSN']

export function SearchPanel({ documentId, busy, result, onResult, onJump, onPropose }: Props) {
  const [query, setQuery] = useState('')
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [wholeWord, setWholeWord] = useState(false)
  const [category, setCategory] = useState<Category>('CUSTOM')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [searching, setSearching] = useState(false)

  const opts = { caseSensitive, wholeWord }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return
    setSearching(true)
    setError(null)
    setPicked(new Set())
    try {
      onResult(await api.search(documentId, query, opts))
    } catch (err) {
      onResult(null)
      setError(err instanceof Error ? err.message : 'Search failed')
    } finally {
      setSearching(false)
    }
  }

  const togglePick = (id: string) =>
    setPicked((cur) => {
      const next = new Set(cur)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div className="flex h-full min-h-0 flex-col">
      <form onSubmit={submit} className="space-y-2 border-b border-slate-200 p-3">
        <label htmlFor="doc-search" className="sr-only">
          Search the document
        </label>
        <div className="flex gap-1.5">
          <input
            id="doc-search"
            type="search"
            placeholder="Search all pages (exact text)…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
          <button
            type="submit"
            disabled={searching || !query.trim()}
            className="rounded bg-slate-800 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            Search
          </button>
        </div>
        <div className="flex gap-3 text-xs text-slate-600">
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={caseSensitive} onChange={(e) => setCaseSensitive(e.target.checked)} />
            match case
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={wholeWord} onChange={(e) => setWholeWord(e.target.checked)} />
            whole word
          </label>
        </div>
        <p className="text-[11px] leading-snug text-slate-500">
          Literal search only. Line breaks, dashes and curly quotes still match. Matches are never redacted until you
          propose and then approve them.
        </p>
        {error && (
          <p role="alert" className="rounded bg-red-50 p-2 text-xs text-red-700">
            {error}
          </p>
        )}
      </form>

      {result && (
        <>
          <div className="space-y-1.5 border-b border-slate-200 p-3 text-xs" aria-live="polite">
            <p className="text-slate-700" data-testid="search-count">
              {result.total} match{result.total === 1 ? '' : 'es'}
              {result.truncated && ' (list truncated)'}
            </p>
            {result.unsupported_pages.length > 0 && (
              <p className="rounded bg-amber-50 p-1.5 text-amber-900">
                Pages {result.unsupported_pages.join(', ')} were not searched (scanned or mixed).
              </p>
            )}
            {result.total > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <select
                  aria-label="Category for proposals"
                  value={category}
                  onChange={(e) => setCategory(e.target.value as Category)}
                  className="rounded border border-slate-300 px-1 py-0.5"
                >
                  {CATEGORY_CHOICES.map((c) => (
                    <option key={c} value={c}>
                      {c.toLowerCase()}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={busy || picked.size === 0}
                  onClick={() => {
                    onPropose(result.query, opts, category, { occurrenceIds: [...picked] })
                    setPicked(new Set())
                  }}
                  className="rounded border border-slate-300 bg-white px-2 py-0.5 hover:bg-slate-50 disabled:opacity-50"
                >
                  Propose selected ({picked.size})
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onPropose(result.query, opts, category, { allMatches: true })}
                  className="rounded border border-slate-300 bg-white px-2 py-0.5 hover:bg-slate-50 disabled:opacity-50"
                >
                  Propose all {result.total}
                </button>
              </div>
            )}
          </div>
          <ul className="min-h-0 flex-1 divide-y divide-slate-100 overflow-auto" aria-label="Search results">
            {result.occurrences.map((o) => (
              <li key={o.occurrence_id} className="flex gap-2 p-2.5 text-sm">
                <input
                  type="checkbox"
                  aria-label={`Select match on page ${o.page_number}`}
                  checked={picked.has(o.occurrence_id)}
                  onChange={() => togglePick(o.occurrence_id)}
                  className="mt-1"
                />
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => onJump(o)}>
                  <span className="text-[11px] text-slate-500">
                    p.{o.page_number}
                    {!o.mappable && ' · cannot be located'}
                    {o.partial_word && ' · partial word'}
                  </span>
                  <p className="break-words text-[13px] text-slate-800">{o.context}</p>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {!result && <p className="p-3 text-sm text-slate-500">Search results appear here and on the page.</p>}
    </div>
  )
}
