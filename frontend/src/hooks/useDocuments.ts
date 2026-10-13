import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { DocumentRecord } from '../api/types'

const POLL_INTERVAL_MS = 1500

function isInFlight(doc: DocumentRecord): boolean {
  return doc.status === 'uploaded' || doc.status === 'processing'
}

/**
 * Document list with upload/delete/retry, plus polling while any document is
 * still being processed. Upload returns 202 immediately; the list keeps
 * refreshing until every document has settled into ready/unsupported/error.
 */
export function useDocuments() {
  const [documents, setDocuments] = useState<DocumentRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const res = await api.listDocuments()
      setDocuments(res.items)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load documents')
    } finally {
      setLoading(false)
    }
  }, [])

  // One scheduler covers both the initial load (delay 0 while `loading`) and
  // polling while any document is still being processed. It stops on its own
  // as soon as every document has settled.
  useEffect(() => {
    const anyInFlight = documents.some(isInFlight)
    if (!loading && !anyInFlight) return
    const timer = window.setTimeout(() => void refresh(), loading ? 0 : POLL_INTERVAL_MS)
    return () => window.clearTimeout(timer)
  }, [documents, loading, refresh])

  const upload = useCallback(
    async (file: File): Promise<DocumentRecord | null> => {
      setUploading(true)
      setUploadError(null)
      try {
        const created = await api.uploadDocument(file)
        await refresh()
        return created
      } catch (e) {
        setUploadError(e instanceof ApiError ? e.message : 'Upload failed')
        return null
      } finally {
        setUploading(false)
      }
    },
    [refresh],
  )

  const remove = useCallback(
    async (id: string) => {
      await api.deleteDocument(id)
      await refresh()
    },
    [refresh],
  )

  const reprocess = useCallback(
    async (id: string) => {
      await api.reprocessDocument(id)
      await refresh()
    },
    [refresh],
  )

  return { documents, loading, error, refresh, upload, uploading, uploadError, remove, reprocess }
}
