import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { makeHealth } from '../test/factories'
import { mockApi } from '../test/mockApi'
import { HealthBanner } from './HealthBanner'

const unavailable = (reason: string) =>
  makeHealth({ embeddings: { ...makeHealth().embeddings, available: false, dimension: null, reason } })

describe('HealthBanner', () => {
  it('shows nothing when everything is available', async () => {
    const api = mockApi({ 'GET /api/health': { json: makeHealth() } })
    const { container } = render(<HealthBanner />)
    await waitFor(() => expect(api.callsTo('GET', '/api/health')).toHaveLength(1))
    await new Promise((r) => setTimeout(r, 20)) // let the state update settle
    expect(container).toBeEmptyDOMElement()
  })

  it('warns, without blocking, when semantic policy search is unavailable, and can be dismissed', async () => {
    mockApi({ 'GET /api/health': { json: unavailable('The embedding model has not been downloaded.') } })
    render(<HealthBanner />)
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Semantic policy search is unavailable: The embedding model has not been downloaded.')
    expect(banner).toHaveTextContent('The rest of ClearCase works normally.')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument() // not an error banner

    await userEvent.setup().click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows the blocking error for a missing database instead of the embeddings notice, and offers Retry', async () => {
    const api = mockApi({
      'GET /api/health': { json: makeHealth({ mongodb: false, status: 'degraded', embeddings: unavailable('x').embeddings }) },
    })
    render(<HealthBanner />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/MongoDB is unreachable/)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.callsTo('GET', '/api/health')).toHaveLength(2))
  })

  it('tells you when the backend itself cannot be reached', async () => {
    const failing = () => Promise.reject(new TypeError('network down'))
    vi.stubGlobal('fetch', failing)
    render(<HealthBanner />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/backend is not reachable/)
  })
})
