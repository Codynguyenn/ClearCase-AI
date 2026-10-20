import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AssistantInfo, ChatMessage, RunDetail, ToolEvent } from '../api/types'
import { segmentsWithCitations } from '../lib/citations'
import { useChat } from '../hooks/useChat'

interface Props {
  documentId: string
  /** How often to ask the server how the running request is going. Tests pass a small number. */
  pollMs?: number
  onJumpToPage: (pageNumber: number) => void
  onOpenCitation: (citationId: string) => void
  /** Called when a request finishes, so the Findings panel can reload what the assistant proposed. */
  onRunFinished?: () => void
}

const DEMO_COMMANDS = ['help', 'detect email phone', 'propose', 'summary', 'policy can company names stay visible?', 'export']
const ACK_PREFIX = 'clearcase.live-ack.'

function readAck(provider: string | null): boolean {
  try {
    return window.localStorage.getItem(ACK_PREFIX + (provider ?? 'unknown')) === 'yes'
  } catch {
    return false // storage can be blocked; the notice then simply shows again next time
  }
}

function writeAck(provider: string | null) {
  try {
    window.localStorage.setItem(ACK_PREFIX + (provider ?? 'unknown'), 'yes')
  } catch {
    // ignore: this is only a convenience
  }
}

export function ChatPanel({ documentId, pollMs, onJumpToPage, onOpenCitation, onRunFinished }: Props) {
  const chat = useChat(documentId, { pollMs, onRunFinished })
  const [assistant, setAssistant] = useState<AssistantInfo | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [authState, setAuthState] = useState<'idle' | 'working'>('idle')
  const [authError, setAuthError] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    let cancelled = false
    const t = window.setTimeout(() => {
      api
        .health()
        .then((h) => {
          if (cancelled) return
          setAssistant(h.assistant)
          setAcknowledged(h.assistant.sends_excerpts_to_provider ? readAck(h.assistant.provider) : true)
        })
        .catch(() => undefined) // the banner is simply absent; sending will report the real problem
    }, 0)
    return () => {
      cancelled = true
      window.clearTimeout(t)
    }
  }, [])

  const messageCount = chat.messages.length
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messageCount, chat.active?.tool_events.length])

  const live = assistant?.mode === 'live'
  const notConfigured = assistant !== null && !assistant.configured
  const needsAck = assistant !== null && assistant.sends_excerpts_to_provider && !acknowledged
  const working = chat.active !== null
  const canType = assistant !== null && !notConfigured && !needsAck && !chat.loading
  const canSend = canType && !working && !sending && text.trim().length > 0

  const submit = async () => {
    if (!canSend) return
    const message = text.trim()
    setSending(true)
    const ok = await chat.send(message)
    setSending(false)
    if (ok) {
      setText('')
      setAuthState('idle')
      setAuthError(null)
    }
  }

  const needsAuthorization =
    chat.lastFinished?.tool_events.some((e) => e.tool === 'export_approved_redactions' && e.status === 'error') ?? false

  /** One click: the reviewer lets the assistant export once, and the assistant is asked to do it now. */
  const letAssistantExport = async () => {
    setAuthState('working')
    setAuthError(null)
    try {
      await api.authorizeExport(documentId, 'assistant') // the assistant never sees or receives this
    } catch (e) {
      setAuthState('idle')
      setAuthError(e instanceof Error ? e.message : 'Could not allow the export.')
      return
    }
    await chat.send('export')
    setAuthState('idle')
  }

  return (
    <div className="flex h-full min-h-0 flex-col" aria-label="Assistant chat">
      <ModeBanner assistant={assistant} />

      {notConfigured && (
        <div role="alert" className="border-b border-red-200 bg-red-50 p-2 text-xs text-red-800">
          Live mode is on, but the AI provider is not set up. Set CLEARCASE_LLM_PROVIDER, CLEARCASE_LLM_API_KEY and
          CLEARCASE_LLM_MODEL in .env and restart. Nothing has been sent anywhere.
        </div>
      )}

      {needsAck && assistant && (
        <div role="region" aria-label="Privacy notice" className="space-y-2 border-b border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          <p>
            <strong>Before you start:</strong> in live mode, excerpts of this document (text the assistant reads through its
            tools) are sent to <strong>{assistant.provider}</strong> ({assistant.model}) to produce answers. Only continue if
            you are allowed to share this document with that provider.
          </p>
          <button
            type="button"
            onClick={() => {
              writeAck(assistant.provider)
              setAcknowledged(true)
            }}
            className="rounded bg-amber-700 px-2 py-1 font-medium text-white hover:bg-amber-800"
          >
            I understand, continue
          </button>
        </div>
      )}

      <div ref={listRef} role="log" aria-live="polite" aria-label="Conversation" className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
        {chat.loading && <p className="text-xs text-slate-500">Loading conversation…</p>}

        {chat.hasMore && (
          <button
            type="button"
            onClick={() => void chat.loadEarlier()}
            className="mx-auto block rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-50"
          >
            Load earlier messages
          </button>
        )}

        {!chat.loading && chat.messages.length === 0 && !working && (
          <p className="rounded border border-dashed border-slate-300 p-3 text-center text-xs text-slate-600">
            {live
              ? 'Ask the assistant to find sensitive information and propose redactions, for example: “Find the client’s personal information, suggest redactions, and keep company names visible.”'
              : 'No messages yet. Pick a command below, or type help.'}
          </p>
        )}

        {chat.messages.map((m) => (
          <MessageView
            key={m.id}
            message={m}
            prefetched={chat.lastFinished?.run.id === m.run_id ? chat.lastFinished : null}
            onJumpToPage={onJumpToPage}
            onOpenCitation={onOpenCitation}
          />
        ))}

        {chat.active && <WorkingView detail={chat.active} onCancel={() => void chat.cancel()} />}

        {needsAuthorization && !working && (
          <div role="region" aria-label="Export hand-off" className="space-y-2 rounded border border-indigo-200 bg-indigo-50 p-3 text-xs text-indigo-900">
            <p>
              The assistant cannot create the redacted file until <strong>you</strong> let it. Clicking below allows{' '}
              <strong>one export</strong> within 5 minutes and asks the assistant to run it now. It exports only what you
              approved, and the assistant never sees the permission. (To export yourself, use the Export tab.)
            </p>
            <button
              type="button"
              disabled={authState === 'working' || !canType}
              onClick={() => void letAssistantExport()}
              className="rounded bg-indigo-700 px-2 py-1 font-medium text-white hover:bg-indigo-800 disabled:opacity-50"
            >
              {authState === 'working' ? 'Working…' : 'Let the assistant export'}
            </button>
            {authError && (
              <p role="alert" className="text-red-700">
                {authError}
              </p>
            )}
          </div>
        )}
      </div>

      {chat.error && (
        <div role="alert" className="flex items-start gap-2 border-t border-red-200 bg-red-50 p-2 text-xs text-red-800">
          <span className="flex-1">{chat.error}</span>
          <button type="button" aria-label="Dismiss error" onClick={chat.dismissError} className="text-red-600">
            ✕
          </button>
        </div>
      )}

      <div className="space-y-2 border-t border-slate-200 p-3">
        {assistant?.mode === 'demo' && (
          <div className="flex flex-wrap gap-1" aria-label="Demo commands">
            {DEMO_COMMANDS.map((c) => (
              <button
                key={c}
                type="button"
                disabled={!canType || working}
                onClick={() => {
                  setText(c)
                  inputRef.current?.focus()
                }}
                className="rounded border border-slate-300 bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {c}
              </button>
            ))}
          </div>
        )}
        <label htmlFor="chat-input" className="sr-only">
          Message to the assistant
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          value={text}
          rows={2}
          maxLength={4000}
          disabled={!canType}
          placeholder={live ? 'Ask about this document…' : 'Type a command, e.g. detect email'}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              void submit()
            }
          }}
          className="block w-full resize-none rounded border border-slate-300 p-2 text-sm disabled:bg-slate-100"
        />
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={!canSend}
            onClick={() => void submit()}
            className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            Send
          </button>
          <span className="text-[11px] text-slate-500">Enter to send, Shift+Enter for a new line</span>
        </div>
      </div>
    </div>
  )
}

function ModeBanner({ assistant }: { assistant: AssistantInfo | null }) {
  if (!assistant) return null
  if (assistant.mode === 'demo') {
    return (
      <p role="status" className="border-b border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] text-slate-700">
        <strong>Demo mode:</strong> scripted commands run the real tools. No AI model is involved and nothing leaves this machine.
      </p>
    )
  }
  return (
    <p role="status" className="border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-900">
      <strong>Live mode</strong> ({assistant.provider}, {assistant.model}): excerpts of this document are sent to {assistant.provider}.
      The assistant only proposes; you decide.
    </p>
  )
}

function MessageView({
  message,
  prefetched,
  onJumpToPage,
  onOpenCitation,
}: {
  message: ChatMessage
  prefetched: RunDetail | null
  onJumpToPage: (page: number) => void
  onOpenCitation: (id: string) => void
}) {
  const mine = message.role === 'user'
  const tone =
    message.status === 'failed'
      ? 'border-red-200 bg-red-50'
      : message.status === 'cancelled'
        ? 'border-slate-200 bg-slate-50'
        : mine
          ? 'border-indigo-200 bg-indigo-50'
          : 'border-slate-200 bg-white'
  return (
    <article className={`rounded border p-2 text-sm ${tone}`} data-role={message.role} data-status={message.status}>
      <header className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        {mine ? 'You' : 'Assistant'}
        {message.status === 'failed' && <span className="rounded bg-red-100 px-1 text-red-800">error</span>}
        {message.status === 'cancelled' && <span className="rounded bg-slate-200 px-1 text-slate-700">stopped</span>}
      </header>
      <div className="whitespace-pre-wrap break-words text-slate-900">
        {mine
          ? message.content
          : segmentsWithCitations(message.content, message.citations).map((seg, i) =>
              seg.type === 'text' ? (
                <span key={i}>{seg.text}</span>
              ) : seg.verified ? (
                <button
                  key={i}
                  type="button"
                  onClick={() => (seg.kind === 'page' ? onJumpToPage(Number(seg.ref)) : onOpenCitation(seg.ref))}
                  aria-label={seg.kind === 'page' ? `Go to page ${seg.ref}` : `Read policy passage ${seg.ref}`}
                  className="mx-0.5 rounded border border-indigo-200 bg-indigo-50 px-1 font-mono text-[11px] text-indigo-800 hover:bg-indigo-100"
                >
                  {seg.label}
                </button>
              ) : (
                <span
                  key={i}
                  title="This citation could not be checked against what the tools returned. Do not rely on it."
                  className="mx-0.5 rounded border border-amber-300 bg-amber-50 px-1 font-mono text-[11px] text-amber-900"
                >
                  {seg.label} · unverified
                </span>
              ),
            )}
      </div>
      {!mine && message.run_id && <RunActivity runId={message.run_id} prefetched={prefetched} />}
    </article>
  )
}

function RunActivity({ runId, prefetched }: { runId: string; prefetched: RunDetail | null }) {
  const [detail, setDetail] = useState<RunDetail | null>(prefetched)
  const [error, setError] = useState<string | null>(null)
  const [opened, setOpened] = useState(false)

  useEffect(() => {
    if (!opened || detail) return
    let cancelled = false
    api
      .getRun(runId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load the activity.'))
    return () => {
      cancelled = true
    }
  }, [opened, detail, runId])

  return (
    <details className="mt-2 text-xs text-slate-600" onToggle={(e) => setOpened((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer select-none">What it did</summary>
      {error && <p role="alert" className="mt-1 text-red-700">{error}</p>}
      {!error && !detail && <p className="mt-1">Loading…</p>}
      {detail && (
        <div className="mt-1 space-y-1">
          <ToolEventList events={detail.tool_events} emptyText="No tools were used." />
          <p className="text-[11px] text-slate-500">
            {detail.run.mode === 'live'
              ? `${detail.run.rounds} model call(s), ${detail.run.input_tokens + detail.run.cache_read_tokens + detail.run.cache_write_tokens} input / ${detail.run.output_tokens} output tokens`
              : 'Scripted demo command: no model call.'}
            {detail.run.unverified_citations > 0 && ` · ${detail.run.unverified_citations} citation(s) could not be verified`}
            {detail.run.limits_hit.length > 0 && ` · stopped early: ${detail.run.limits_hit.join(', ')}`}
          </p>
        </div>
      )}
    </details>
  )
}

function ToolEventList({ events, emptyText }: { events: ToolEvent[]; emptyText: string }) {
  if (events.length === 0) return <p className="text-slate-500">{emptyText}</p>
  return (
    <ol className="space-y-0.5">
      {events.map((e) => (
        <li key={e.seq} className="flex flex-wrap gap-x-2">
          <span className="font-mono text-slate-800">{e.tool}</span>
          <span className={e.status === 'ok' ? 'text-emerald-700' : 'text-red-700'}>{e.status === 'ok' ? 'ok' : `failed${e.error_code ? `: ${e.error_code}` : ''}`}</span>
          <span className="text-slate-500">{e.duration_ms} ms</span>
          {e.summary && <span className="text-slate-600">{e.summary}</span>}
        </li>
      ))}
    </ol>
  )
}

function WorkingView({ detail, onCancel }: { detail: RunDetail; onCancel: () => void }) {
  return (
    <div role="status" aria-label="Assistant is working" className="space-y-1 rounded border border-slate-200 bg-slate-50 p-2 text-xs text-slate-700">
      <div className="flex items-center gap-2">
        <span className="font-medium">{detail.run.status === 'queued' ? 'Waiting for a free slot…' : 'Working…'}</span>
        <button
          type="button"
          onClick={onCancel}
          className="ml-auto rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-100"
        >
          Cancel
        </button>
      </div>
      <ToolEventList events={detail.tool_events} emptyText="Starting…" />
    </div>
  )
}
