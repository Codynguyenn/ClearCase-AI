import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { apiError, deferred, mockApi } from '../test/mockApi'
import { makePassage } from '../test/factories'
import { PassageDialog } from './PassageDialog'

const ID = 'northwind-redaction-policy:1.0:7'
const PATH = `/api/policies/passages/${ID}`

const dialogEl = () => document.querySelector('dialog') as HTMLDialogElement

describe('PassageDialog', () => {
  it('stays closed and makes no request when there is no citation', () => {
    const api = mockApi({})
    render(<PassageDialog citationId={null} onClose={vi.fn()} />)
    expect(dialogEl().open).toBe(false)
    expect(api.calls).toHaveLength(0)
  })

  it('opens, shows loading, then the real passage with its provenance', async () => {
    const pending = deferred<{ json: unknown }>()
    const api = mockApi({ [`GET ${PATH}`]: () => pending.promise })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)

    expect(dialogEl().open).toBe(true)
    expect(screen.getByText('Loading…')).toBeInTheDocument()

    pending.resolve({ json: makePassage() })
    expect(await screen.findByText(/Names of companies are not personal information/)).toBeInTheDocument()
    expect(screen.getByText('Northwind Analytics Document Redaction Policy')).toBeInTheDocument()
    expect(screen.getByText('v1.0')).toBeInTheDocument()
    expect(screen.getByText('FICTIONAL EXAMPLE')).toBeInTheDocument()
    expect(screen.getByText(/Section: 6\. Organisation names/)).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
    // The colon-separated id is percent-encoded in the URL (it is a path segment).
    expect(api.calls[0].url).toBe('/api/policies/passages/northwind-redaction-policy%3A1.0%3A7')
  })

  it('says the passage is unavailable when the server returns 404, and shows no text', async () => {
    mockApi({ [`GET ${PATH}`]: apiError(404, 'citation_not_found', 'Policy passage not found.') })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/not available/i)
    expect(screen.queryByRole('blockquote')).not.toBeInTheDocument()
    expect(document.querySelector('blockquote')).toBeNull()
  })

  it('shows the server message for other failures instead of a blank dialog', async () => {
    mockApi({ [`GET ${PATH}`]: apiError(500, 'boom', 'The policy service is down.') })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The policy service is down.')
  })

  it('labels an older policy version as not searched', async () => {
    mockApi({ [`GET ${PATH}`]: { json: makePassage({ policy_status: 'archived' }) } })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    expect(await screen.findByText('older version (not searched)')).toBeInTheDocument()
  })

  it('does not call a user-uploaded policy fictional or add the example disclaimer', async () => {
    mockApi({ [`GET ${PATH}`]: { json: makePassage({ is_fictional_example: false }) } })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    await screen.findByText(/Names of companies/)
    expect(screen.queryByText('FICTIONAL EXAMPLE')).not.toBeInTheDocument()
    expect(screen.queryByText(/invented example policy/)).not.toBeInTheDocument()
  })

  it('renders passage text as plain text, never as HTML', async () => {
    const evil = '<img src=x onerror="window.__pwned = true"> and <b>bold</b>'
    mockApi({ [`GET ${PATH}`]: { json: makePassage({ text: evil }) } })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    await screen.findByText(/and <b>bold<\/b>/) // the markup is visible as literal characters
    expect(document.querySelector('dialog img')).toBeNull()
    expect(document.querySelector('dialog b')).toBeNull()
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined()
  })

  it('never shows the previous citation’s text while the next one is loading', async () => {
    const OTHER = 'northwind-sharing-guidelines:1.0:5'
    const pending = deferred<{ json: unknown }>()
    mockApi({
      [`GET ${PATH}`]: { json: makePassage({ text: 'FIRST PASSAGE TEXT' }) },
      [`GET /api/policies/passages/${OTHER}`]: () => pending.promise,
    })
    const { rerender } = render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    await screen.findByText('FIRST PASSAGE TEXT')

    rerender(<PassageDialog citationId={OTHER} onClose={vi.fn()} />)
    expect(screen.queryByText('FIRST PASSAGE TEXT')).not.toBeInTheDocument()
    expect(screen.getByText('Loading…')).toBeInTheDocument()

    pending.resolve({ json: makePassage({ citation_id: OTHER, text: 'SECOND PASSAGE TEXT' }) })
    expect(await screen.findByText('SECOND PASSAGE TEXT')).toBeInTheDocument()
  })

  it('closes with its Close button and tells the parent', async () => {
    mockApi({ [`GET ${PATH}`]: { json: makePassage() } })
    const onClose = vi.fn()
    render(<PassageDialog citationId={ID} onClose={onClose} />)
    await screen.findByText(/Names of companies/)

    await userEvent.setup().click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(dialogEl().open).toBe(false)
  })

  it('is labelled for assistive technology', async () => {
    mockApi({ [`GET ${PATH}`]: { json: makePassage() } })
    render(<PassageDialog citationId={ID} onClose={vi.fn()} />)
    await screen.findByText(/Names of companies/)
    expect(dialogEl()).toHaveAccessibleName('Policy passage')
  })
})
