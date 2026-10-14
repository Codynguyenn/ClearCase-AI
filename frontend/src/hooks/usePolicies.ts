import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PolicyRecord } from '../api/types'

/** The policy library: list plus the actions on it. Errors are shown, never swallowed. */
export function usePolicies() {
  const [policies, setPolicies] = useState<PolicyRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setPolicies(await api.listPolicies())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load policies')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const t = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(t)
  }, [refresh])

  /** Run an action, then reload the list. Returns false (and shows the message) on failure. */
  const run = useCallback(
    async (action: () => Promise<unknown>): Promise<boolean> => {
      setBusy(true)
      setError(null)
      try {
        await action()
        await refresh()
        return true
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Something went wrong')
        return false
      } finally {
        setBusy(false)
      }
    },
    [refresh],
  )

  return {
    policies,
    loading,
    busy,
    error,
    refresh,
    upload: (file: File) => run(() => api.uploadPolicy(file)),
    seed: () => run(() => api.seedPolicies()),
    activate: (p: PolicyRecord) => run(() => api.activatePolicy(p.policy_id, p.version)),
    reindex: (p: PolicyRecord) => run(() => api.reindexPolicy(p.policy_id, p.version)),
    remove: (p: PolicyRecord) => run(() => api.deletePolicy(p.policy_id)),
  }
}
