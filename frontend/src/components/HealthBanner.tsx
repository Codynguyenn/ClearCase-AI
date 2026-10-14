import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { HealthResponse } from '../api/types'

/** Tells the user when the backend or database is unreachable, and offers a retry. */
export function HealthBanner() {
  const [health, setHealth] = useState<HealthResponse | null | 'unreachable'>(null)
  const [dismissedEmbeddings, setDismissedEmbeddings] = useState(false)

  const check = async () => {
    try {
      setHealth(await api.health())
    } catch {
      setHealth('unreachable')
    }
  }

  useEffect(() => {
    // Schedule rather than call directly so the first check is clearly async.
    const first = window.setTimeout(() => void check(), 0)
    const t = window.setInterval(() => void check(), 30_000)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(t)
    }
  }, [])

  if (health === null) return null
  if (health === 'unreachable' || !health.mongodb) {
    const msg =
      health === 'unreachable'
        ? 'The ClearCase backend is not reachable at /api. Start it with the command in the README.'
        : 'The backend is running but MongoDB is unreachable. Start it with `scripts/dev_mongo.sh start`.'
    return (
      <div role="alert" className="flex items-center justify-between gap-4 bg-red-600 px-4 py-2 text-sm text-white">
        <span>{msg}</span>
        <button
          type="button"
          onClick={() => void check()}
          className="rounded border border-white/60 px-2 py-0.5 text-xs hover:bg-white/10"
        >
          Retry
        </button>
      </div>
    )
  }
  if (!health.embeddings.available && !dismissedEmbeddings) {
    return (
      <div role="status" className="flex items-center justify-between gap-4 bg-amber-100 px-4 py-1.5 text-xs text-amber-900">
        <span>
          Semantic policy search is unavailable: {health.embeddings.reason ?? 'the embedding model is not ready.'} The rest
          of ClearCase works normally.
        </span>
        <button
          type="button"
          onClick={() => setDismissedEmbeddings(true)}
          className="rounded border border-amber-700/40 px-2 py-0.5 hover:bg-amber-200"
        >
          Dismiss
        </button>
      </div>
    )
  }
  return null
}
