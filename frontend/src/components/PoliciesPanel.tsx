import { useRef, useState } from 'react'
import { api } from '../api/client'
import type { EmbeddingState, PolicyRecord, RetrievalResult } from '../api/types'
import { usePolicies } from '../hooks/usePolicies'

const EMBEDDING_STYLES: Record<EmbeddingState, string> = {
  ready: 'bg-emerald-100 text-emerald-900',
  partial: 'bg-amber-100 text-amber-900',
  stale: 'bg-amber-100 text-amber-900',
  pending: 'bg-amber-100 text-amber-900',
  unavailable: 'bg-red-100 text-red-800',
}

const EMBEDDING_TEXT: Record<EmbeddingState, string> = {
  ready: 'searchable',
  partial: 'partly indexed',
  stale: 'needs re-index',
  pending: 'not indexed yet',
  unavailable: 'search unavailable',
}

interface Props {
  onOpenCitation: (citationId: string) => void
}

export function PoliciesPanel({ onOpenCitation }: Props) {
  const lib = usePolicies()
  const fileRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [asking, setAsking] = useState(false)
  const [result, setResult] = useState<RetrievalResult | null>(null)
  const [askError, setAskError] = useState<string | null>(null)

  const ask = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return
    setAsking(true)
    setAskError(null)
    try {
      setResult(await api.searchPolicies(query.trim()))
    } catch (err) {
      setResult(null)
      setAskError(err instanceof Error ? err.message : 'Policy search failed')
    } finally {
      setAsking(false)
    }
  }

  const onFile = async (file: File | undefined) => {
    if (!file) return
    await lib.upload(file)
    if (fileRef.current) fileRef.current.value = ''
  }

  const confirmDelete = (p: PolicyRecord) => {
    if (window.confirm(`Delete "${p.title}" (all versions)? Findings that cite it will no longer show the passage.`)) {
      void lib.remove(p)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-auto">
      <section className="space-y-2 border-b border-slate-200 p-3" aria-labelledby="ask-policy">
        <h2 id="ask-policy" className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Ask the policy
        </h2>
        <form onSubmit={ask} className="flex gap-2">
          <label htmlFor="policy-question" className="sr-only">
            Question for the active policies
          </label>
          <input
            id="policy-question"
            value={query}
            maxLength={500}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. Can company names stay visible?"
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
          <button
            type="submit"
            disabled={asking || !query.trim()}
            className="rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {asking ? 'Searching…' : 'Ask'}
          </button>
        </form>
        {(asking || lib.busy) && (
          <p className="text-[11px] text-slate-500">
            The first request after the app starts loads the embedding model, which can take 15-20 seconds.
          </p>
        )}
        {askError && (
          <p role="alert" className="rounded bg-red-50 p-2 text-xs text-red-800">
            {askError}
          </p>
        )}
        {result && <RetrievalView result={result} onOpenCitation={onOpenCitation} />}
      </section>

      <section className="space-y-2 p-3" aria-labelledby="policy-library">
        <div className="flex items-center justify-between">
          <h2 id="policy-library" className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Policy library
          </h2>
          <button
            type="button"
            disabled={lib.busy}
            onClick={() => void lib.refresh()}
            className="text-xs text-indigo-700 hover:underline disabled:opacity-50"
          >
            Refresh
          </button>
        </div>

        {lib.error && (
          <p role="alert" className="rounded bg-red-50 p-2 text-xs text-red-800">
            {lib.error}
          </p>
        )}
        {lib.loading && <p className="text-xs text-slate-500">Loading policies…</p>}

        {!lib.loading && lib.policies.length === 0 && (
          <div className="rounded border border-dashed border-slate-300 p-3 text-center text-sm text-slate-600">
            <p>No policies yet. Policies are the reference rules the assistant and your findings can cite.</p>
            <button
              type="button"
              disabled={lib.busy}
              onClick={() => void lib.seed()}
              className="mt-2 rounded bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              {lib.busy ? 'Loading…' : 'Load the fictional sample policies'}
            </button>
          </div>
        )}

        <ul className="space-y-2">
          {lib.policies.map((p) => (
            <PolicyRow
              key={p.id}
              policy={p}
              busy={lib.busy}
              onActivate={() => void lib.activate(p)}
              onReindex={() => void lib.reindex(p)}
              onDelete={() => confirmDelete(p)}
            />
          ))}
        </ul>

        <div className="rounded border border-slate-200 bg-slate-50 p-2">
          <label htmlFor="policy-file" className="block text-xs font-medium text-slate-700">
            Upload a policy (.md or .txt, up to 200 KB)
          </label>
          <input
            id="policy-file"
            ref={fileRef}
            type="file"
            accept=".md,.markdown,.txt,text/markdown,text/plain"
            disabled={lib.busy}
            onChange={(e) => void onFile(e.target.files?.[0])}
            className="mt-1 block w-full text-xs"
          />
          <p className="mt-1 text-[11px] text-slate-500">
            A new version of an existing policy becomes the active one; older versions stay readable.
          </p>
        </div>
      </section>
    </div>
  )
}

function PolicyRow({
  policy: p,
  busy,
  onActivate,
  onReindex,
  onDelete,
}: {
  policy: PolicyRecord
  busy: boolean
  onActivate: () => void
  onReindex: () => void
  onDelete: () => void
}) {
  const e = p.embedding
  return (
    <li className="rounded border border-slate-200 p-2 text-sm" data-policy-id={p.policy_id} data-version={p.version}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-medium text-slate-900">{p.title}</span>
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">v{p.version}</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
            p.status === 'active' ? 'bg-indigo-100 text-indigo-900' : 'bg-slate-100 text-slate-600'
          }`}
        >
          {p.status}
        </span>
        {p.is_fictional_example && (
          <span className="rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium text-violet-900">
            FICTIONAL EXAMPLE
          </span>
        )}
      </div>
      <p className="mt-1 text-[11px] text-slate-500">
        {p.chunk_count} passages · {p.source}
      </p>
      <p className="mt-1 text-[11px]">
        <span className={`rounded px-1.5 py-0.5 font-medium ${EMBEDDING_STYLES[e.state]}`}>{EMBEDDING_TEXT[e.state]}</span>
        {e.state !== 'ready' && (
          <span className="ml-1 text-slate-600">
            {e.chunks_current}/{p.chunk_count} indexed
            {e.chunks_stale > 0 && `, ${e.chunks_stale} from an older model`}
          </span>
        )}
      </p>
      {e.detail && <p className="mt-1 text-[11px] text-red-800">{e.detail}</p>}
      {p.truncated_chunks > 0 && (
        <p className="mt-1 text-[11px] text-amber-800">
          {p.truncated_chunks} passage(s) are longer than the model reads; only their start is searched.
        </p>
      )}
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {p.status === 'archived' && (
          <RowButton disabled={busy} onClick={onActivate}>
            Make active
          </RowButton>
        )}
        {e.state !== 'ready' && (
          <RowButton disabled={busy} onClick={onReindex}>
            Re-index
          </RowButton>
        )}
        <RowButton disabled={busy} onClick={onDelete}>
          Delete
        </RowButton>
      </div>
    </li>
  )
}

function RowButton({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
    >
      {children}
    </button>
  )
}

function RetrievalView({ result, onOpenCitation }: { result: RetrievalResult; onOpenCitation: (id: string) => void }) {
  const m = result.method
  if (result.status === 'ok') {
    return (
      <div className="space-y-2" data-testid="policy-results">
        <ul className="space-y-2">
          {result.passages.map((p) => (
            <li key={p.citation_id} className="rounded border border-slate-200 bg-white p-2 text-sm">
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="font-semibold text-slate-900">{p.policy_title}</span>
                <span className="text-slate-500">v{p.policy_version} · {p.section}</span>
                {p.is_fictional_example && (
                  <span className="rounded bg-violet-100 px-1.5 py-0.5 font-medium text-violet-900">FICTIONAL EXAMPLE</span>
                )}
              </div>
              <p className="mt-1 whitespace-pre-wrap text-slate-800">{p.text}</p>
              <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-500">
                <span title="Cosine similarity: a heuristic, not a probability">similarity {p.score.toFixed(2)}</span>
                <button
                  type="button"
                  onClick={() => onOpenCitation(p.citation_id)}
                  className="font-mono text-indigo-700 hover:underline"
                  aria-label={`Open policy passage ${p.citation_id}`}
                >
                  {p.citation_id}
                </button>
              </div>
            </li>
          ))}
        </ul>
        <p className="text-[11px] text-slate-500">
          Semantic search · {m.model.split('/').pop()} · similarity cutoff {m.min_score} (a heuristic tuned on a small
          synthetic set).
        </p>
      </div>
    )
  }
  const tone = result.status === 'unavailable' ? 'bg-red-50 text-red-800' : 'bg-amber-50 text-amber-900'
  return (
    <div role="status" className={`rounded p-2 text-xs ${tone}`} data-testid="policy-results" data-status={result.status}>
      <p>{result.message}</p>
      {result.top_score_below_cutoff !== null && (
        <p className="mt-1 text-[11px]">
          Best similarity was {result.top_score_below_cutoff.toFixed(2)}, below the cutoff of {m.min_score}.
        </p>
      )}
    </div>
  )
}
