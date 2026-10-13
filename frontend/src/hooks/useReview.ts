import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ExportRecord, Finding, ReviewSummary } from '../api/types'

interface Loaded {
  documentId: string
  findings: Finding[]
  summary: ReviewSummary | null
  exports: ExportRecord[]
  error: string | null
}

const message = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong')

/**
 * Findings, review summary and exports for one document.
 *
 * The server is the source of truth: every action calls the API and then re-fetches
 * all three lists, so the screen can never drift from what export will actually use
 * (for example after a decision changes the review revision).
 */
export function useReview(documentId: string | null, enabled: boolean) {
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [tick, setTick] = useState(0)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<{ documentId: string; message: string } | null>(null)

  useEffect(() => {
    if (!documentId || !enabled) return
    let cancelled = false
    Promise.all([api.listFindings(documentId), api.reviewSummary(documentId), api.listExports(documentId)])
      .then(([findings, summary, exports]) => {
        if (!cancelled) setLoaded({ documentId, findings: findings.items, summary, exports, error: null })
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoaded({ documentId, findings: [], summary: null, exports: [], error: message(e) })
      })
    return () => {
      cancelled = true
    }
  }, [documentId, enabled, tick])

  const current = loaded && loaded.documentId === documentId ? loaded : null

  /** Run a mutation, surface its error, then refresh everything. */
  const run = useCallback(
    async <T,>(action: () => Promise<T>): Promise<T | undefined> => {
      if (!documentId) return undefined
      setBusy(true)
      setActionError(null)
      try {
        return await action()
      } catch (e) {
        setActionError({ documentId, message: message(e) })
        return undefined
      } finally {
        setBusy(false)
        setTick((t) => t + 1)
      }
    },
    [documentId],
  )

  return {
    findings: current?.findings ?? [],
    summary: current?.summary ?? null,
    exports: current?.exports ?? [],
    loading: enabled && documentId !== null && current === null,
    error: current?.error ?? (actionError?.documentId === documentId ? actionError?.message : null) ?? null,
    busy,
    run,
    refresh: () => setTick((t) => t + 1),
  }
}
