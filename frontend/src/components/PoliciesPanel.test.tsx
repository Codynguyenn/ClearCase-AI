import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { RetrievedPassage } from '../api/types'
import { apiError, deferred, mockApi } from '../test/mockApi'
import { makePolicy, makeRetrieval, makeRetrieved } from '../test/factories'
import { PoliciesPanel } from './PoliciesPanel'

const ask = async (question: string) => {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Question for the active policies'), question)
  await user.click(screen.getByRole('button', { name: 'Ask' }))
  return user
}

describe('PoliciesPanel: library', () => {
  it('offers to load the fictional samples when the library is empty, then lists them', async () => {
    let policies: unknown[] = []
    const api = mockApi({
      'GET /api/policies': () => ({ json: policies }),
      'POST /api/policies/seed-samples': () => {
        policies = [makePolicy(), makePolicy({ id: 'pol-2', policy_id: 'northwind-sharing-guidelines', title: 'Sharing Guidelines' })]
        return { json: policies }
      },
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)

    expect(await screen.findByText(/No policies yet/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Load the fictional sample policies' }))

    expect(await screen.findByText('Northwind Analytics Document Redaction Policy')).toBeInTheDocument()
    expect(screen.getByText('Sharing Guidelines')).toBeInTheDocument()
    expect(screen.getAllByText('FICTIONAL EXAMPLE')).toHaveLength(2)
    expect(screen.getAllByText('searchable')).toHaveLength(2)
    expect(screen.queryByText(/No policies yet/)).not.toBeInTheDocument()
    expect(api.callsTo('POST', '/api/policies/seed-samples')[0].body).toEqual({ reset: false })
  })

  it('shows a policy whose embeddings failed as unavailable, with the reason and a Re-index action', async () => {
    const broken = makePolicy({
      embedding: {
        state: 'unavailable',
        model: 'm',
        revision: 'r',
        chunks_current: 0,
        chunks_stale: 0,
        chunks_missing: 10,
        detail: 'The embedding model has not been downloaded.',
      },
    })
    let current = broken
    const api = mockApi({
      'GET /api/policies': () => ({ json: [current] }),
      'POST /api/policies/northwind-redaction-policy/versions/1.0/reindex': () => {
        current = makePolicy()
        return { json: current }
      },
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)

    expect(await screen.findByText('search unavailable')).toBeInTheDocument()
    expect(screen.getByText('The embedding model has not been downloaded.')).toBeInTheDocument()
    expect(screen.getByText('0/10 indexed')).toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: 'Re-index' }))
    expect(await screen.findByText('searchable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Re-index' })).not.toBeInTheDocument()
    expect(api.callsTo('POST', '/api/policies/northwind-redaction-policy/versions/1.0/reindex')).toHaveLength(1)
  })

  it('explains stale vectors and truncated passages', async () => {
    mockApi({
      'GET /api/policies': {
        json: [
          makePolicy({
            truncated_chunks: 2,
            embedding: { state: 'partial', model: 'm', revision: 'r', chunks_current: 7, chunks_stale: 3, chunks_missing: 0, detail: null },
          }),
        ],
      },
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    expect(await screen.findByText('partly indexed')).toBeInTheDocument()
    expect(screen.getByText(/7\/10 indexed, 3 from an older model/)).toBeInTheDocument()
    expect(screen.getByText(/2 passage\(s\) are longer than the model reads/)).toBeInTheDocument()
  })

  it('lets you make an archived version active again', async () => {
    let list = [makePolicy({ version: '1.0', status: 'archived' }), makePolicy({ id: 'pol-2', version: '2.0', status: 'active' })]
    const api = mockApi({
      'GET /api/policies': () => ({ json: list }),
      'POST /api/policies/northwind-redaction-policy/versions/1.0/activate': () => {
        list = [makePolicy({ version: '1.0', status: 'active' }), makePolicy({ id: 'pol-2', version: '2.0', status: 'archived' })]
        return { json: list[0] }
      },
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    const makeActive = await screen.findAllByRole('button', { name: 'Make active' })
    expect(makeActive).toHaveLength(1) // only the archived version offers it

    await userEvent.setup().click(makeActive[0])
    await waitFor(() => expect(api.callsTo('POST', '/api/policies/northwind-redaction-policy/versions/1.0/activate')).toHaveLength(1))
    const rows = await screen.findAllByRole('listitem')
    expect(within(rows.find((r) => r.getAttribute('data-version') === '2.0')!).getByRole('button', { name: 'Make active' })).toBeInTheDocument()
  })

  it('shows the server’s message when an upload is refused', async () => {
    mockApi({
      'GET /api/policies': { json: [] },
      'POST /api/policies': apiError(409, 'policy_version_exists', 'Version 1.0 of "x" already exists with different content. Upload the changes as a new version.'),
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText(/No policies yet/)
    const file = new File(['# Policy'], 'policy.md', { type: 'text/markdown' })
    await userEvent.setup().upload(screen.getByLabelText(/Upload a policy/), file)
    expect(await screen.findByRole('alert')).toHaveTextContent(/already exists with different content/)
  })

  it('asks for confirmation before deleting, and deletes nothing if you cancel', async () => {
    let list = [makePolicy()]
    const api = mockApi({
      'GET /api/policies': () => ({ json: list }),
      'DELETE /api/policies/northwind-redaction-policy': () => {
        list = []
        return { json: { policies: 1, chunks: 10 } }
      },
    })
    const confirm = vi.fn((_message?: string) => false) // the user clicks "Cancel"
    vi.stubGlobal('confirm', confirm)
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Delete' }))

    expect(confirm).toHaveBeenCalledTimes(1)
    expect(String(confirm.mock.calls[0]?.[0])).toMatch(/Delete "Northwind Analytics Document Redaction Policy"/)
    expect(api.callsTo('DELETE', '/api/policies/northwind-redaction-policy')).toHaveLength(0)
    expect(screen.getByText('Northwind Analytics Document Redaction Policy')).toBeInTheDocument()

    confirm.mockReturnValue(true) // now the user clicks "OK"
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByText(/No policies yet/)).toBeInTheDocument()
    expect(api.callsTo('DELETE', '/api/policies/northwind-redaction-policy')).toHaveLength(1)
  })
})

describe('PoliciesPanel: chunking and citations', () => {
  it('flags a version split by older chunking rules and says how to fix it', async () => {
    mockApi({ 'GET /api/policies': { json: [makePolicy({ chunker_version: 1, chunker_outdated: true })] } })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    const note = await screen.findByTestId('chunker-outdated')
    expect(note).toHaveTextContent('older chunking rules')
    expect(note).toHaveTextContent('existing citations stay valid')
    expect(note).toHaveTextContent('as a new version')
  })

  it('shows no chunking note for an up-to-date version', async () => {
    mockApi({ 'GET /api/policies': { json: [makePolicy()] } })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText('Northwind Analytics Document Redaction Policy')
    expect(screen.queryByTestId('chunker-outdated')).not.toBeInTheDocument()
  })

  it('tells you how many findings cite the policy (all versions) before you confirm a delete', async () => {
    mockApi({
      'GET /api/policies': {
        json: [
          makePolicy({ id: 'a', version: '1.0', status: 'archived', cited_by_findings: 2 }),
          makePolicy({ id: 'b', version: '2.0', cited_by_findings: 1 }),
          makePolicy({ id: 'c', policy_id: 'other-policy', title: 'Other', cited_by_findings: 9 }),
        ],
      },
    })
    const confirm = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirm)
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await userEvent.setup().click((await screen.findAllByRole('button', { name: 'Delete' }))[0]!)
    expect(String(confirm.mock.calls[0]?.[0])).toMatch(/3 findings cite this policy and will no longer be able to show the passage/)
  })

  it('says plainly when nothing cites the policy', async () => {
    mockApi({ 'GET /api/policies': { json: [makePolicy()] } })
    const confirm = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirm)
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Delete' }))
    expect(String(confirm.mock.calls[0]?.[0])).toMatch(/No findings cite it/)
  })

  it('uses the singular for one finding', async () => {
    mockApi({ 'GET /api/policies': { json: [makePolicy({ cited_by_findings: 1 })] } })
    const confirm = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirm)
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Delete' }))
    expect(String(confirm.mock.calls[0]?.[0])).toMatch(/1 finding cites this policy/)
  })
})

describe('PoliciesPanel: asking the policy', () => {
  const library = { 'GET /api/policies': { json: [makePolicy()] } }

  it('shows matching passages with similarity and a citation that opens the passage', async () => {
    const api = mockApi({
      ...library,
      'POST /api/policies/search': { json: makeRetrieval({ passages: [makeRetrieved({ score: 0.7 }), makeRetrieved({ citation_id: 'northwind-redaction-policy:1.0:3', section: '2. Personal identifiers of individuals', text: 'Full names must be redacted.', score: 0.55, rank: 2 })] }) },
    })
    const onOpenCitation = vi.fn()
    render(<PoliciesPanel onOpenCitation={onOpenCitation} />)
    await screen.findByText('searchable')
    const user = await ask('Can company names stay visible?')

    const results = await screen.findByTestId('policy-results')
    expect(within(results).getByText(/Names of companies are not personal information/)).toBeInTheDocument()
    expect(within(results).getByText('similarity 0.70')).toBeInTheDocument()
    expect(within(results).getByText('similarity 0.55')).toBeInTheDocument()
    expect(within(results).getAllByText('FICTIONAL EXAMPLE').length).toBeGreaterThan(0)
    expect(within(results).getByText(/similarity cutoff 0\.38/)).toBeInTheDocument()
    expect(api.callsTo('POST', '/api/policies/search')[0].body).toEqual({ query: 'Can company names stay visible?', top_k: 3 })

    await user.click(within(results).getByRole('button', { name: 'Open policy passage northwind-redaction-policy:1.0:3' }))
    expect(onOpenCitation).toHaveBeenCalledWith('northwind-redaction-policy:1.0:3')
  })

  it('says the policy does not address the question, with the best score, and shows no passage', async () => {
    mockApi({
      ...library,
      'POST /api/policies/search': {
        json: makeRetrieval({
          status: 'no_relevant_policy',
          message: 'The active policies do not appear to address this question. Do not assume a rule.',
          passages: [],
          top_score_below_cutoff: 0.0106,
        }),
      },
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText('searchable')
    await ask('What are the cafeteria opening hours?')

    const results = await screen.findByTestId('policy-results')
    expect(results).toHaveAttribute('data-status', 'no_relevant_policy')
    expect(results).toHaveTextContent('do not appear to address this question')
    expect(results).toHaveTextContent('Best similarity was 0.01, below the cutoff of 0.38.')
    expect(within(results).queryByText(/^similarity/)).not.toBeInTheDocument()
    expect(within(results).queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows an unavailable search as unavailable, never as an empty "no match"', async () => {
    mockApi({
      ...library,
      'POST /api/policies/search': {
        json: makeRetrieval({
          status: 'unavailable',
          message: 'Semantic policy search is unavailable: the model is missing. keyword search is not used as a substitute.',
          passages: [],
        }),
      },
    })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText('searchable')
    await ask('anything')
    const results = await screen.findByTestId('policy-results')
    expect(results).toHaveAttribute('data-status', 'unavailable')
    expect(results).toHaveTextContent('Semantic policy search is unavailable')
    expect(results).not.toHaveTextContent('Best similarity')
  })

  it('reports a failed search instead of showing stale results', async () => {
    mockApi({ ...library, 'POST /api/policies/search': apiError(500, 'boom', 'Policy search failed on the server.') })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText('searchable')
    await ask('hello')
    expect(await screen.findByText('Policy search failed on the server.')).toBeInTheDocument()
    expect(screen.queryByTestId('policy-results')).not.toBeInTheDocument()
  })

  it('does not search for an empty question', async () => {
    const api = mockApi(library)
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText('searchable')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
    await userEvent.setup().type(screen.getByLabelText('Question for the active policies'), '   ')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
    expect(api.callsTo('POST', '/api/policies/search')).toHaveLength(0)
  })

  it('warns that the first search can be slow while the model loads', async () => {
    const pending = deferred<{ json: unknown }>()
    mockApi({ ...library, 'POST /api/policies/search': () => pending.promise })
    render(<PoliciesPanel onOpenCitation={vi.fn()} />)
    await screen.findByText('searchable')
    await ask('slow one')

    expect(await screen.findByRole('button', { name: 'Searching…' })).toBeDisabled()
    expect(screen.getByText(/loads the embedding model, which can take 15-20 seconds/)).toBeInTheDocument()

    pending.resolve({ json: makeRetrieval() })
    await screen.findByTestId('policy-results')
    expect(screen.queryByText(/can take 15-20 seconds/)).not.toBeInTheDocument()
  })

  describe('reviewer-only weak matches', () => {
    const weak = [
      makeRetrieved({ citation_id: 'northwind-sharing-guidelines:1.0:3', section: '2. Who may approve external sharing', text: 'A second person must check it.', score: 0.33, rank: 1 }),
      makeRetrieved({ citation_id: 'northwind-sharing-guidelines:1.0:4', section: '3. Labelling redacted documents', text: 'Include REDACTED in the file name.', score: 0.31, rank: 2 }),
    ]
    const noPolicy = (weak_passages: RetrievedPassage[]) =>
      makeRetrieval({ status: 'no_relevant_policy', message: 'The active policies do not appear to address this question.', passages: [], top_score_below_cutoff: 0.33, weak_passages })

    it('offers weak matches in a collapsed section with a clear warning, and they open like any citation', async () => {
      mockApi({ ...library, 'POST /api/policies/search': { json: noPolicy(weak) } })
      const onOpenCitation = vi.fn()
      render(<PoliciesPanel onOpenCitation={onOpenCitation} />)
      await screen.findByText('searchable')
      const user = await ask('What is the vacation policy?')

      const results = await screen.findByTestId('policy-results')
      expect(results).toHaveAttribute('data-status', 'no_relevant_policy') // still reported as "does not address"
      const details = within(results).getByTestId('weak-matches')
      expect(details).toHaveTextContent('Possibly related (weak matches): 2')
      expect(details).not.toHaveAttribute('open') // collapsed until the reviewer asks
      expect(details).toHaveTextContent(/often are not an answer, but sometimes they are/)
      expect(details).toHaveTextContent(/The assistant is not given them and they should not be cited as policy/)
      expect(within(details).getByText('similarity 0.33 (weak)')).toBeInTheDocument()

      await user.click(within(details).getByRole('button', { name: 'Open weak-match passage northwind-sharing-guidelines:1.0:4' }))
      expect(onOpenCitation).toHaveBeenCalledWith('northwind-sharing-guidelines:1.0:4')
    })

    it('shows no weak section when there are none', async () => {
      mockApi({ ...library, 'POST /api/policies/search': { json: noPolicy([]) } })
      render(<PoliciesPanel onOpenCitation={vi.fn()} />)
      await screen.findByText('searchable')
      await ask('anything')
      await screen.findByTestId('policy-results')
      expect(screen.queryByTestId('weak-matches')).not.toBeInTheDocument()
    })

    it.each(['unavailable', 'no_policies'] as const)(
      'never shows weak matches when the search was %s (they only make sense for "does not address this")',
      async (status) => {
        mockApi({
          ...library,
          'POST /api/policies/search': { json: makeRetrieval({ status, message: 'Nothing to search.', passages: [], weak_passages: weak }) },
        })
        render(<PoliciesPanel onOpenCitation={vi.fn()} />)
        await screen.findByText('searchable')
        await ask('company names')
        expect(await screen.findByTestId('policy-results')).toHaveAttribute('data-status', status)
        expect(screen.queryByTestId('weak-matches')).not.toBeInTheDocument()
      },
    )

    it('shows real answers without any weak section (that branch renders passages only)', async () => {
      mockApi({
        ...library,
        'POST /api/policies/search': { json: makeRetrieval({ status: 'ok', passages: [makeRetrieved()], weak_passages: weak }) },
      })
      render(<PoliciesPanel onOpenCitation={vi.fn()} />)
      await screen.findByText('searchable')
      await ask('company names')
      await screen.findByText(/Names of companies are not personal information/)
      expect(screen.queryByTestId('weak-matches')).not.toBeInTheDocument()
    })
  })
})
