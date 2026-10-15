import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { ChatMessage, RunDetail } from '../api/types'

const FINISHED = new Set(['completed', 'failed', 'cancelled'])
const PAGE_SIZE = 50
const MAX_POLL_FAILURES = 5

interface Loaded {
  documentId: string
  conversationId: string | null
  messages: ChatMessage[]
  hasMore: boolean
}

export interface ChatState {
  loading: boolean
  messages: ChatMessage[]
  hasMore: boolean
  /** The run that is working right now (queued or running), with the tool activity so far. */
  active: RunDetail | null
  /** The most recent run that finished in this session, kept so the panel can offer a hand-off. */
  lastFinished: RunDetail | null
  error: string | null
}

const newClientId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`

const describe = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong')

/**
 * One document's assistant conversation: history, sending, polling the run, cancelling.
 *
 * The server is the source of truth. After a reload the hook picks the newest conversation, loads its history, and if
 * the last user message belongs to a run that is still going it resumes watching that run.
 */
export function useChat(documentId: string | null, options: { pollMs?: number; onRunFinished?: () => void } = {}) {
  const pollMs = options.pollMs ?? 1000
  const onRunFinished = useRef(options.onRunFinished)
  useEffect(() => {
    onRunFinished.current = options.onRunFinished
  })

  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [active, setActive] = useState<RunDetail | null>(null)
  const [lastFinished, setLastFinished] = useState<RunDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumped whenever the document changes or the hook unmounts, so a slow request or poll from before is ignored.
  const generation = useRef(0)

  const append = useCallback((documentIdNow: string, extra: ChatMessage[]) => {
    setLoaded((prev) => {
      if (!prev || prev.documentId !== documentIdNow) return prev
      const known = new Set(prev.messages.map((m) => m.id))
      const fresh = extra.filter((m) => !known.has(m.id))
      return fresh.length ? { ...prev, messages: [...prev.messages, ...fresh].sort((a, b) => a.seq - b.seq) } : prev
    })
  }, [])

  /** Watch a run until it finishes, then add its answer to the history. */
  const watch = useCallback(
    async (docId: string, runId: string, gen: number) => {
      let failures = 0
      for (;;) {
        let detail: RunDetail
        try {
          detail = await api.getRun(runId)
          failures = 0
        } catch (e) {
          if (gen !== generation.current) return
          failures += 1
          if (failures >= MAX_POLL_FAILURES) {
            setError(`Lost contact with the assistant: ${describe(e)}`)
            setActive(null)
            return
          }
          await new Promise((r) => window.setTimeout(r, pollMs))
          if (gen !== generation.current) return
          continue
        }
        if (gen !== generation.current) return
        if (FINISHED.has(detail.run.status)) {
          if (detail.assistant_message) append(docId, [detail.assistant_message])
          setActive(null)
          setLastFinished(detail)
          onRunFinished.current?.()
          return
        }
        setActive(detail)
        await new Promise((r) => window.setTimeout(r, pollMs))
        if (gen !== generation.current) return
      }
    },
    [append, pollMs],
  )

  // Load (or switch to) a document's conversation.
  useEffect(() => {
    generation.current += 1
    const gen = generation.current
    if (!documentId) return
    const t = window.setTimeout(() => {
      void (async () => {
        try {
          const conversations = await api.listConversations(documentId)
          if (gen !== generation.current) return
          const conversation = conversations[0] ?? null
          const page = conversation ? await api.listMessages(conversation.id, { limit: PAGE_SIZE }) : { messages: [], has_more: false }
          if (gen !== generation.current) return
          setLoaded({ documentId, conversationId: conversation?.id ?? null, messages: page.messages, hasMore: page.has_more })
          setActive(null)
          setLastFinished(null)
          setError(null)
          // A request may still be running (the page was reloaded mid-run): keep watching it.
          const last = page.messages[page.messages.length - 1]
          if (last?.role === 'user' && last.run_id) {
            const detail = await api.getRun(last.run_id)
            if (gen !== generation.current) return
            if (!FINISHED.has(detail.run.status)) {
              setActive(detail)
              void watch(documentId, last.run_id, gen)
            }
          }
        } catch (e) {
          if (gen === generation.current) {
            setLoaded({ documentId, conversationId: null, messages: [], hasMore: false })
            setError(describe(e))
          }
        }
      })()
    }, 0)
    return () => {
      window.clearTimeout(t)
      generation.current += 1
    }
  }, [documentId, watch])

  const current = loaded && loaded.documentId === documentId ? loaded : null

  const send = useCallback(
    async (text: string): Promise<boolean> => {
      if (!documentId) return false
      if (!current) {
        setError('The conversation is still loading. Try again in a moment.') // never drop a message silently
        return false
      }
      const gen = generation.current
      setError(null)
      try {
        let conversationId = current.conversationId
        if (conversationId === null) {
          conversationId = (await api.createConversation(documentId)).id
          setLoaded((prev) => (prev && prev.documentId === documentId ? { ...prev, conversationId } : prev))
        }
        const result = await api.sendMessage(conversationId, text, newClientId())
        if (gen !== generation.current) return false
        append(documentId, [result.message])
        setLastFinished(null)
        setActive({ run: result.run, tool_events: [], assistant_message: null })
        void watch(documentId, result.run.id, gen)
        return true
      } catch (e) {
        if (gen === generation.current) setError(e instanceof ApiError ? e.message : describe(e))
        return false
      }
    },
    [append, current, documentId, watch],
  )

  const cancel = useCallback(async () => {
    if (!active) return
    try {
      await api.cancelRun(active.run.id)
    } catch (e) {
      setError(describe(e))
    }
  }, [active])

  const loadEarlier = useCallback(async () => {
    if (!current?.conversationId || current.messages.length === 0) return
    const gen = generation.current
    try {
      const page = await api.listMessages(current.conversationId, { beforeSeq: current.messages[0].seq, limit: PAGE_SIZE })
      if (gen !== generation.current) return
      setLoaded((prev) => {
        if (!prev || prev.documentId !== documentId) return prev
        const known = new Set(prev.messages.map((m) => m.id))
        return { ...prev, messages: [...page.messages.filter((m) => !known.has(m.id)), ...prev.messages], hasMore: page.has_more }
      })
    } catch (e) {
      setError(describe(e))
    }
  }, [current, documentId])

  const state: ChatState = {
    loading: documentId !== null && current === null,
    messages: current?.messages ?? [],
    hasMore: current?.hasMore ?? false,
    active,
    lastFinished,
    error,
  }
  return { ...state, send, cancel, loadEarlier, dismissError: () => setError(null) }
}
