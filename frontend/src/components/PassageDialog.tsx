import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { PolicyPassage } from '../api/types'

interface Props {
  /** The citation to show, or null when closed. */
  citationId: string | null
  onClose: () => void
}

type State =
  | { kind: 'loading' }
  | { kind: 'ready'; passage: PolicyPassage }
  | { kind: 'missing' }
  | { kind: 'error'; message: string }

/**
 * Shows the real text behind a citation. The id is looked up on the server, so a
 * made-up or deleted citation can never display text: it shows "not available".
 * Uses the native <dialog> element, which traps focus and closes on Escape.
 */
export function PassageDialog({ citationId, onClose }: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const [loaded, setLoaded] = useState<{ id: string; state: State } | null>(null)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (citationId && !dialog.open) dialog.showModal()
    if (!citationId && dialog.open) dialog.close()
  }, [citationId])

  useEffect(() => {
    if (!citationId) return
    let cancelled = false
    api
      .getPassage(citationId)
      .then((passage) => !cancelled && setLoaded({ id: citationId, state: { kind: 'ready', passage } }))
      .catch((e: unknown) => {
        if (cancelled) return
        const notFound = typeof e === 'object' && e !== null && 'status' in e && (e as { status: number }).status === 404
        setLoaded({
          id: citationId,
          state: notFound
            ? { kind: 'missing' }
            : { kind: 'error', message: e instanceof Error ? e.message : 'Could not load the passage' },
        })
      })
    return () => {
      cancelled = true
    }
  }, [citationId])

  // Derived: anything not yet loaded for the current id is "loading".
  const state: State = loaded && loaded.id === citationId ? loaded.state : { kind: 'loading' }

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="passage-title"
      className="m-auto w-[min(34rem,92vw)] rounded-lg border border-slate-300 bg-white p-0 shadow-xl backdrop:bg-slate-900/40"
    >
      <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
        <div>
          <h2 id="passage-title" className="text-sm font-semibold text-slate-900">
            Policy passage
          </h2>
          <p className="mt-0.5 break-all font-mono text-[11px] text-slate-500">{citationId}</p>
        </div>
        <button
          type="button"
          onClick={() => ref.current?.close()}
          className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-50"
        >
          Close
        </button>
      </div>
      <div className="max-h-[60vh] overflow-auto p-4 text-sm">
        {state.kind === 'loading' && <p className="text-slate-500">Loading…</p>}
        {state.kind === 'missing' && (
          <p role="alert" className="rounded bg-amber-50 p-2 text-amber-900">
            This passage is not available. The policy may have been deleted, so the citation cannot be shown.
          </p>
        )}
        {state.kind === 'error' && (
          <p role="alert" className="rounded bg-red-50 p-2 text-red-800">
            {state.message}
          </p>
        )}
        {state.kind === 'ready' && <PassageBody passage={state.passage} />}
      </div>
    </dialog>
  )
}

export function PassageBody({ passage }: { passage: PolicyPassage }) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="font-semibold text-slate-900">{passage.policy_title}</span>
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">v{passage.policy_version}</span>
        {passage.policy_status === 'archived' && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-900">older version (not searched)</span>
        )}
        {passage.is_fictional_example && (
          <span className="rounded bg-violet-100 px-1.5 py-0.5 font-medium text-violet-900">FICTIONAL EXAMPLE</span>
        )}
      </div>
      <p className="text-[11px] text-slate-500">
        Section: {passage.section} · Source file: {passage.source}
      </p>
      <blockquote className="whitespace-pre-wrap rounded border-l-4 border-indigo-300 bg-slate-50 p-3 text-slate-800">
        {passage.text}
      </blockquote>
      {passage.is_fictional_example && (
        <p className="text-[11px] text-slate-500">
          This is an invented example policy for a portfolio project, not legal advice.
        </p>
      )}
    </div>
  )
}
