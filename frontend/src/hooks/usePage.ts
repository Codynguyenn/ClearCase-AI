import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PageText } from '../api/types'

interface Loaded {
  key: string
  page: PageText | null
  error: string | null
}

/**
 * Loads the extracted text + word boxes for one page.
 *
 * The result is stored together with the request key it belongs to, so when the
 * document/page/version changes we never show stale text from the previous
 * page while the new one is loading (the key no longer matches).
 */
export function usePage(documentId: string | null, pageNumber: number, version: number) {
  const key = documentId ? `${documentId}:${pageNumber}:${version}` : ''
  const [loaded, setLoaded] = useState<Loaded>({ key: '', page: null, error: null })

  useEffect(() => {
    if (!documentId) return
    let cancelled = false
    api
      .getPage(documentId, pageNumber)
      .then((page) => {
        if (!cancelled) setLoaded({ key, page, error: null })
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setLoaded({ key, page: null, error: e instanceof Error ? e.message : 'Failed to load page text' })
        }
      })
    return () => {
      cancelled = true
    }
  }, [documentId, pageNumber, key])

  const current = loaded.key === key && key !== ''
  return {
    page: current ? loaded.page : null,
    error: current ? loaded.error : null,
    loading: key !== '' && !current,
  }
}
