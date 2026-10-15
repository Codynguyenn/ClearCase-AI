import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AssistantInfo, ChatMessage, RunDetail } from '../api/types'
import {
  makeConversation,
  makeHealth,
  makeMessage,
  makeRun,
  makeRunDetail,
  makeToolEvent,
} from '../test/factories'
import { apiError, mockApi } from '../test/mockApi'
import { ChatPanel } from './ChatPanel'

const DOC = 'doc1'
const POLL = 5

const live = (patch: Partial<AssistantInfo> = {}): AssistantInfo => ({
  mode: 'live',
  provider: 'anthropic',
  model: 'claude-opus-5-5',
  configured: true,
  sends_excerpts_to_provider: true,
  ...patch,
})
const health = (assistant?: AssistantInfo) => ({ json: makeHealth(assistant ? { assistant, assistant_mode: assistant.mode } : {}) })

/** A run the test controls: the server keeps answering with the current state until the test changes it. */
function controllable(initial: RunDetail) {
  let current = initial
  return { route: () => ({ json: current }), set: (next: RunDetail) => (current = next) }
}

/** A run route that walks through the given frames, then repeats the last one. */
function frames(...details: RunDetail[]) {
  let i = 0
  return () => ({ json: details[Math.min(i++, details.length - 1)] })
}

const running = (events = [makeToolEvent()]) => makeRunDetail({ run: makeRun({ status: 'running' }), tool_events: events, assistant_message: null })
const answer = (content: string, extra: Partial<ChatMessage> = {}) =>
  makeMessage({ id: 'm2', seq: 2, role: 'assistant', content, run_id: 'run1', ...extra })

const noConversation = (extra: Record<string, unknown> = {}) => ({
  'GET /api/health': health(),
  'GET /api/documents/doc1/conversations': { json: [] },
  ...extra,
})

const withHistory = (messages: ChatMessage[], extra: Record<string, unknown> = {}) => ({
  'GET /api/health': health(),
  'GET /api/documents/doc1/conversations': { json: [makeConversation({ message_count: messages.length })] },
  'GET /api/conversations/conv1/messages': { json: { messages, has_more: false } },
  ...extra,
})

/** The message box, once it is usable: it stays disabled until the assistant info and the conversation have loaded. */
async function messageBox() {
  const box = await screen.findByLabelText('Message to the assistant')
  await waitFor(() => expect(box).toBeEnabled())
  return box
}

const ui = (props: Partial<React.ComponentProps<typeof ChatPanel>> = {}) =>
  render(<ChatPanel documentId={DOC} pollMs={POLL} onJumpToPage={vi.fn()} onOpenCitation={vi.fn()} {...props} />)

beforeEach(() => {
  try {
    window.localStorage.clear()
  } catch {
    // storage unavailable in this environment: the tests that need it will say so
  }
})

describe('ChatPanel: demo mode', () => {
  it('says plainly that demo mode is scripted and offers the commands', async () => {
    mockApi(noConversation())
    ui()
    expect(await screen.findByRole('status', { name: '' })).toHaveTextContent('Demo mode')
    expect(screen.getByText(/No AI model is involved and nothing leaves this machine/)).toBeInTheDocument()
    expect(await screen.findByText(/Pick a command below, or type help/)).toBeInTheDocument()
    const chips = within(screen.getByLabelText('Demo commands')).getAllByRole('button').map((b) => b.textContent)
    expect(chips).toEqual(['help', 'detect email phone', 'propose', 'summary', 'policy can company names stay visible?', 'export'])
  })

  it('a command chip fills the input without sending anything', async () => {
    const api = mockApi(noConversation())
    ui()
    await userEvent.setup().click(await screen.findByRole('button', { name: 'summary' }))
    expect(screen.getByLabelText('Message to the assistant')).toHaveValue('summary')
    expect(api.calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })
})

describe('ChatPanel: sending and watching a request', () => {
  it('creates the conversation on first send, shows progress, then the answer, and tells the Findings panel to reload', async () => {
    const onRunFinished = vi.fn()
    const run = controllable(running([]))
    const api = mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': (call: { body: unknown }) => ({
          status: 202,
          json: {
            message: makeMessage({ id: 'm1', seq: 1, content: (call.body as { content: string }).content, run_id: 'run1' }),
            run: makeRun({ status: 'queued' }),
            duplicate: false,
          },
        }),
        'GET /api/runs/run1': run.route,
      }),
    )
    ui({ onRunFinished })
    const user = userEvent.setup()
    await user.type(await messageBox(), '  find the emails  ')
    await user.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByRole('status', { name: 'Assistant is working' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled() // one request at a time
    expect(onRunFinished).not.toHaveBeenCalled()

    run.set(makeRunDetail({ assistant_message: answer('I found 2 emails.') }))
    expect(await screen.findByText('I found 2 emails.')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: 'Assistant is working' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Message to the assistant')).toHaveValue('')
    expect(onRunFinished).toHaveBeenCalledTimes(1)

    const post = api.callsTo('POST', '/api/conversations/conv1/messages')[0].body as { content: string; client_message_id: string }
    expect(post.content).toBe('find the emails') // trimmed
    expect(post.client_message_id).toMatch(/^[A-Za-z0-9_-]{8,64}$/) // what makes a retry safe
    expect(api.callsTo('POST', '/api/documents/doc1/conversations')).toHaveLength(1)
    expect(screen.getAllByRole('article').map((a) => a.getAttribute('data-role'))).toEqual(['user', 'assistant'])
  })

  it('shows the tool activity while it works', async () => {
    const run = controllable(running())
    mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': { status: 202, json: { message: makeMessage({ run_id: 'run1' }), run: makeRun({ status: 'running' }), duplicate: false } },
        'GET /api/runs/run1': run.route,
      }),
    )
    ui()
    const user = userEvent.setup()
    await user.type(await messageBox(), 'find')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    const working = await screen.findByRole('status', { name: 'Assistant is working' })
    expect(await within(working).findByText('search_document')).toBeInTheDocument()
    expect(within(working).getByText(/2 match\(es\) on page\(s\) 1, 2/)).toBeInTheDocument()
    run.set(makeRunDetail({ assistant_message: answer('ok') }))
    await screen.findByText('ok')
  })

  it('Enter sends, Shift+Enter does not, and an empty message cannot be sent', async () => {
    const api = mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': { status: 202, json: { message: makeMessage({ run_id: 'run1' }), run: makeRun({ status: 'running' }), duplicate: false } },
        'GET /api/runs/run1': frames(makeRunDetail({ assistant_message: answer('ok') })),
      }),
    )
    ui()
    const user = userEvent.setup()
    const input = await messageBox()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.type(input, '   ')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.clear(input)
    await user.type(input, 'line one{Shift>}{Enter}{/Shift}line two')
    expect(api.callsTo('POST', '/api/conversations/conv1/messages')).toHaveLength(0)
    expect(input).toHaveValue('line one\nline two')
    await user.type(input, '{Enter}')
    await waitFor(() => expect(api.callsTo('POST', '/api/conversations/conv1/messages')).toHaveLength(1))
  })

  it('keeps what you typed and explains when the assistant is still busy with the previous request', async () => {
    mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': apiError(409, 'run_active', 'The assistant is still working on the previous request in this conversation.'),
      }),
    )
    ui()
    const user = userEvent.setup()
    await user.type(await messageBox(), 'second question')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('still working on the previous request')
    expect(screen.getByLabelText('Message to the assistant')).toHaveValue('second question')
    await user.click(screen.getByRole('button', { name: 'Dismiss error' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('can be cancelled while it works, and the stopped answer is marked', async () => {
    const run = controllable(running())
    const cancelled = makeRunDetail({ run: makeRun({ status: 'cancelled' }), assistant_message: answer('Stopped at your request.', { status: 'cancelled' }) })
    const api = mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': { status: 202, json: { message: makeMessage({ run_id: 'run1' }), run: makeRun({ status: 'running' }), duplicate: false } },
        'GET /api/runs/run1': run.route,
        'POST /api/runs/run1/cancel': () => {
          run.set(cancelled) // the server stops the run and records it
          return { status: 202, json: cancelled }
        },
      }),
    )
    ui()
    const user = userEvent.setup()
    await user.type(await messageBox(), 'find')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(api.callsTo('POST', '/api/runs/run1/cancel')).toHaveLength(1)
    const stopped = await screen.findByText('Stopped at your request.')
    expect(stopped.closest('article')).toHaveAttribute('data-status', 'cancelled')
    expect(within(stopped.closest('article') as HTMLElement).getByText('stopped')).toBeInTheDocument()
  })
})

describe('ChatPanel: history', () => {
  it('shows the saved conversation in order after a reload', async () => {
    mockApi(
      withHistory([
        makeMessage({ id: 'm1', seq: 1, content: 'first question' }),
        answer('first answer'),
      ]),
    )
    ui()
    expect(await screen.findByText('first answer')).toBeInTheDocument()
    expect(screen.getAllByRole('article').map((a) => a.getAttribute('data-role'))).toEqual(['user', 'assistant'])
  })

  it('pages back through older messages on request', async () => {
    const api = mockApi({
      'GET /api/health': health(),
      'GET /api/documents/doc1/conversations': { json: [makeConversation()] },
      'GET /api/conversations/conv1/messages': (call: { url: string }) =>
        call.url.includes('before_seq=')
          ? { json: { messages: [makeMessage({ id: 'old', seq: 1, content: 'the very first question' })], has_more: false } }
          : { json: { messages: [makeMessage({ id: 'new', seq: 2, content: 'a later question' })], has_more: true } },
    })
    ui()
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Load earlier messages' }))
    await screen.findByText('the very first question')
    expect(screen.getAllByRole('article').map((a) => a.textContent)).toEqual([expect.stringContaining('the very first question'), expect.stringContaining('a later question')])
    expect(api.calls.some((c) => c.url.includes('before_seq=2'))).toBe(true)
    expect(screen.queryByRole('button', { name: 'Load earlier messages' })).not.toBeInTheDocument()
  })

  it('resumes watching a request that was still running when the page was reloaded', async () => {
    const run = controllable(running())
    mockApi(withHistory([makeMessage({ id: 'm1', seq: 1, run_id: 'run1' })], { 'GET /api/runs/run1': run.route }))
    ui()
    expect(await screen.findByRole('status', { name: 'Assistant is working' })).toBeInTheDocument()
    run.set(makeRunDetail({ assistant_message: answer('Finished while you were away.') }))
    expect(await screen.findByText('Finished while you were away.')).toBeInTheDocument()
  })

  it('"What it did" loads the activity only when opened and labels demo runs as scripted', async () => {
    const api = mockApi(
      withHistory([makeMessage({ id: 'm1', seq: 1 }), answer('Found 2.')], {
        'GET /api/runs/run1': { json: makeRunDetail() },
      }),
    )
    ui()
    await screen.findByText('Found 2.')
    expect(api.callsTo('GET', '/api/runs/run1')).toHaveLength(0)
    await userEvent.setup().click(screen.getByText('What it did'))
    expect(await screen.findByText('search_document')).toBeInTheDocument()
    expect(screen.getByText('Scripted demo command: no model call.')).toBeInTheDocument()
    expect(api.callsTo('GET', '/api/runs/run1')).toHaveLength(1)
  })

  it('"What it did" reports tokens and unverified citations for a live run', async () => {
    mockApi(
      withHistory([makeMessage({ id: 'm1', seq: 1 }), answer('Done.')], {
        'GET /api/health': health(live()),
        'GET /api/runs/run1': { json: makeRunDetail({ run: makeRun({ mode: 'live', rounds: 3, input_tokens: 100, cache_read_tokens: 900, output_tokens: 50, unverified_citations: 2, limits_hit: ['max_rounds'] }) }) },
      }),
    )
    window.localStorage.setItem('clearcase.live-ack.anthropic', 'yes')
    ui()
    await userEvent.setup().click(await screen.findByText('What it did'))
    expect(await screen.findByText(/3 model call\(s\), 1000 input \/ 50 output tokens/)).toBeInTheDocument()
    expect(screen.getByText(/2 citation\(s\) could not be verified/)).toBeInTheDocument()
    expect(screen.getByText(/stopped early: max_rounds/)).toBeInTheDocument()
  })
})

describe('ChatPanel: citations', () => {
  const POLICY = 'northwind-redaction-policy:1.0:3'

  it('verified citations are buttons that jump or open; unverified ones are flagged and are not buttons', async () => {
    const onJumpToPage = vi.fn()
    const onOpenCitation = vi.fn()
    mockApi(
      withHistory([
        makeMessage({ id: 'm1', seq: 1 }),
        answer(`Seen on [p2] and [p9]. Rule: "Names must be redacted." [${POLICY}]`, {
          citations: [
            { kind: 'page', ref: '2', verified: true, quote: null },
            { kind: 'page', ref: '9', verified: false, quote: null },
            { kind: 'policy', ref: POLICY, verified: true, quote: 'Names must be redacted.' },
          ],
        }),
      ]),
    )
    ui({ onJumpToPage, onOpenCitation })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Go to page 2' }))
    expect(onJumpToPage).toHaveBeenCalledWith(2)
    await user.click(screen.getByRole('button', { name: `Read policy passage ${POLICY}` }))
    expect(onOpenCitation).toHaveBeenCalledWith(POLICY)
    expect(screen.queryByRole('button', { name: 'Go to page 9' })).not.toBeInTheDocument()
    expect(screen.getByText('p9 · unverified')).toBeInTheDocument()
    expect(screen.getByText(/"Names must be redacted\."/)).toBeInTheDocument() // the quote stays visible
  })
})

describe('ChatPanel: live mode', () => {
  it('shows what leaves this machine and blocks sending until you confirm, once', async () => {
    mockApi(noConversation({ 'GET /api/health': health(live()) }))
    const first = ui()
    expect(await screen.findByRole('status')).toHaveTextContent('excerpts of this document are sent to anthropic')
    expect(screen.getByRole('region', { name: 'Privacy notice' })).toHaveTextContent('claude-opus-5-5')
    expect(screen.getByLabelText('Message to the assistant')).toBeDisabled()

    await userEvent.setup().click(screen.getByRole('button', { name: 'I understand, continue' }))
    expect(screen.queryByRole('region', { name: 'Privacy notice' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Message to the assistant')).toBeEnabled()
    expect(window.localStorage.getItem('clearcase.live-ack.anthropic')).toBe('yes')
    first.unmount()

    mockApi(noConversation({ 'GET /api/health': health(live()) }))
    ui() // a later visit does not ask again
    expect(await screen.findByRole('status')).toHaveTextContent('Live mode')
    expect(screen.queryByRole('region', { name: 'Privacy notice' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Message to the assistant')).toBeEnabled()
  })

  it('without a usable provider it says so and cannot be used, instead of pretending', async () => {
    window.localStorage.setItem('clearcase.live-ack.anthropic', 'yes') // so the privacy notice is not what disables the box
    const api = mockApi(noConversation({ 'GET /api/health': health(live({ configured: false, model: null })) }))
    ui()
    expect(await screen.findByRole('alert')).toHaveTextContent('Live mode is on, but the AI provider is not set up')
    await waitFor(() => expect(screen.queryByText('Loading conversation…')).not.toBeInTheDocument()) // loading is over
    expect(screen.getByLabelText('Message to the assistant')).toBeDisabled() // so this is the setup problem, not loading
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(api.calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('shows the server’s fixed setup message if a send is refused as not configured', async () => {
    mockApi(
      noConversation({
        'GET /api/health': health(live()),
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': apiError(409, 'assistant_not_configured', 'The AI provider rejected the API key. Check CLEARCASE_LLM_API_KEY.'),
      }),
    )
    window.localStorage.setItem('clearcase.live-ack.anthropic', 'yes')
    ui()
    const user = userEvent.setup()
    await user.type(await messageBox(), 'find names')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Check CLEARCASE_LLM_API_KEY')
  })
})

describe('ChatPanel: export hand-off', () => {
  const exportAttempt = makeRunDetail({
    tool_events: [makeToolEvent({ tool: 'export_approved_redactions', status: 'error', error_code: 'tool_error', summary: 'failed: tool_error' })],
    assistant_message: answer('I cannot create the redacted file yet. A reviewer has to click “Authorize export”.'),
  })

  it('offers the reviewer the Authorize export action, and only then offers to let the assistant export', async () => {
    const api = mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': (call: { body: unknown }) => ({
          status: 202,
          json: { message: makeMessage({ id: `u${Math.random()}`, seq: Math.floor(Math.random() * 1e6), content: (call.body as { content: string }).content, run_id: 'run1' }), run: makeRun({ status: 'running' }), duplicate: false },
        }),
        'GET /api/runs/run1': frames(exportAttempt),
        'POST /api/documents/doc1/export-authorizations': { status: 201, json: { token: 'secret-token', expires_at: '2026-10-14T00:05:00Z' } },
      }),
    )
    ui()
    const user = userEvent.setup()
    await user.type(await messageBox(), 'export')
    await user.click(screen.getByRole('button', { name: 'Send' }))

    const card = await screen.findByRole('region', { name: 'Export hand-off' })
    expect(card).toHaveTextContent('the assistant never sees it')
    expect(within(card).queryByRole('button', { name: /Ask the assistant to export/ })).not.toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Authorize export' }))
    expect(api.callsTo('POST', '/api/documents/doc1/export-authorizations')).toHaveLength(1)
    expect(await within(card).findByText('Authorized for 5 minutes.')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('secret-token') // the token is the reviewer's, never shown or passed on

    await user.click(within(card).getByRole('button', { name: 'Ask the assistant to export now' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/conversations/conv1/messages')).toHaveLength(2))
    expect((api.callsTo('POST', '/api/conversations/conv1/messages')[1].body as { content: string }).content).toBe('export')
  })

  it('shows no hand-off card when the export did not fail', async () => {
    mockApi(
      noConversation({
        'POST /api/documents/doc1/conversations': { status: 201, json: makeConversation() },
        'POST /api/conversations/conv1/messages': { status: 202, json: { message: makeMessage({ run_id: 'run1' }), run: makeRun({ status: 'running' }), duplicate: false } },
        'GET /api/runs/run1': frames(makeRunDetail({ assistant_message: answer('Export verified.') })),
      }),
    )
    ui()
    const user = userEvent.setup()
    await user.type(await messageBox(), 'summary')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByText('Export verified.')
    expect(screen.queryByRole('region', { name: 'Export hand-off' })).not.toBeInTheDocument()
  })
})

describe('ChatPanel: loading', () => {
  it('keeps the message box disabled until the conversation has loaded, so a message cannot be dropped', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    mockApi({
      'GET /api/health': health(),
      'GET /api/documents/doc1/conversations': async () => {
        await gate
        return { json: [] }
      },
    })
    ui()
    await screen.findByRole('status') // the assistant info has loaded, so only the conversation is still loading
    const box = screen.getByLabelText('Message to the assistant')
    expect(screen.getByText('Loading conversation…')).toBeInTheDocument()
    expect(box).toBeDisabled()
    release()
    await waitFor(() => expect(box).toBeEnabled())
  })

  it('shows a clear error, not silence, if the conversation cannot be loaded', async () => {
    mockApi({
      'GET /api/health': health(),
      'GET /api/documents/doc1/conversations': apiError(404, 'document_not_found', 'Document not found.'),
    })
    ui()
    expect(await screen.findByRole('alert')).toHaveTextContent('Document not found.')
  })
})
