import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Finding } from '../api/types'
import { makeFinding } from '../test/factories'
import { FindingsPanel } from './FindingsPanel'

function setup(findings: Finding[]) {
  const handlers = {
    onSelect: vi.fn(),
    onDetect: vi.fn(),
    onDecide: vi.fn(),
    onBulk: vi.fn(),
    onStartManual: vi.fn(),
    onStartAdjust: vi.fn(),
    onOpenCitation: vi.fn(),
  }
  render(
    <FindingsPanel
      findings={findings}
      summary={null}
      loading={false}
      busy={false}
      currentPage={1}
      selectedId={null}
      notice={null}
      {...handlers}
    />,
  )
  return handlers
}

const rowFor = (id: string) => document.querySelector(`[data-finding-id="${id}"]`) as HTMLElement

describe('FindingsPanel: policy citations and assistant proposals', () => {
  const assistant = makeFinding({
    id: 'fnd_agent',
    origin: 'agent',
    source: 'agent:mcp',
    sources: ['agent:mcp'],
    explanation: 'Client name; the policy requires redacting individuals’ names.',
    policy_refs: ['northwind-redaction-policy:1.0:3', 'northwind-sharing-guidelines:1.0:5'],
  })

  it('shows one chip per cited passage, and clicking a chip asks the app to open that passage', async () => {
    const h = setup([assistant])
    const row = within(rowFor('fnd_agent'))
    const chips = row.getAllByRole('button', { name: /^Read policy passage / })
    expect(chips.map((c) => c.textContent)).toEqual(['northwind-redaction-policy:1.0:3', 'northwind-sharing-guidelines:1.0:5'])

    await userEvent.setup().click(row.getByRole('button', { name: 'Read policy passage northwind-sharing-guidelines:1.0:5' }))
    expect(h.onOpenCitation).toHaveBeenCalledTimes(1)
    expect(h.onOpenCitation).toHaveBeenCalledWith('northwind-sharing-guidelines:1.0:5')
    expect(h.onSelect).not.toHaveBeenCalled() // opening a citation must not also select/move the finding
  })

  it('marks assistant proposals and shows the reason the assistant gave', () => {
    setup([assistant])
    const row = within(rowFor('fnd_agent'))
    expect(row.getByText('assistant')).toBeInTheDocument()
    expect(row.getByText(/Reason given: Client name; the policy requires redacting/)).toBeInTheDocument()
    expect(row.getByText('proposed')).toBeInTheDocument() // never auto-approved
  })

  it('shows no policy section, assistant badge or reason for an ordinary detector finding', () => {
    setup([makeFinding({ id: 'fnd_det' })])
    const row = within(rowFor('fnd_det'))
    expect(row.queryByText('assistant')).not.toBeInTheDocument()
    expect(row.queryByText(/Reason given/)).not.toBeInTheDocument()
    expect(row.queryByText(/^Policy:/)).not.toBeInTheDocument()
    expect(row.queryAllByRole('button', { name: /Read policy passage/ })).toHaveLength(0)
    expect(row.getByText('Maria Delgado-Reyes')).toBeInTheDocument()
  })

  it('keeps the normal review actions on an assistant proposal (the reviewer decides)', async () => {
    const h = setup([assistant])
    const row = within(rowFor('fnd_agent'))
    await userEvent.setup().click(row.getByRole('button', { name: 'Approve' }))
    expect(h.onDecide).toHaveBeenCalledWith('fnd_agent', 'approve', false)
    expect(row.getByRole('button', { name: 'Reject' })).toBeEnabled()
  })
})

describe('FindingsPanel: bulk buttons never overwrite a decision the reviewer already made', () => {
  const proposedA = makeFinding({ id: 'fnd_a', status: 'proposed' })
  const proposedB = makeFinding({ id: 'fnd_b', status: 'proposed', category: 'EMAIL' })
  const rejected = makeFinding({ id: 'fnd_rejected', status: 'rejected' })
  const approved = makeFinding({ id: 'fnd_approved', status: 'approved' })
  const stale = makeFinding({ id: 'fnd_stale', status: 'stale' })

  const pickStatus = (status: string) =>
    userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Filter by status' }), status)

  it('with all statuses shown, "Approve shown" skips a finding the reviewer already rejected', async () => {
    const h = setup([proposedA, rejected, proposedB])
    await userEvent.setup().click(screen.getByRole('button', { name: 'Approve shown (2 undecided)' }))
    expect(h.onBulk).toHaveBeenCalledTimes(1)
    expect(h.onBulk).toHaveBeenCalledWith(['fnd_a', 'fnd_b'], 'approve')
  })

  it('with all statuses shown, "Reject shown" skips an approved finding, so approved text is never left unredacted by mistake', async () => {
    const h = setup([proposedA, approved, proposedB])
    await userEvent.setup().click(screen.getByRole('button', { name: 'Reject shown (2 undecided)' }))
    expect(h.onBulk).toHaveBeenCalledWith(['fnd_a', 'fnd_b'], 'reject')
  })

  it('counts only undecided findings (not stale ones) and follows the other filters', async () => {
    setup([proposedA, proposedB, rejected, approved, stale])
    expect(screen.getByRole('button', { name: 'Approve shown (2 undecided)' })).toBeInTheDocument()
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Filter by category' }), 'EMAIL')
    expect(screen.getByRole('button', { name: 'Approve shown (1 undecided)' })).toBeInTheDocument()
  })

  it('offers no bulk buttons when every finding is already decided', () => {
    setup([rejected, approved, stale])
    expect(screen.queryByRole('button', { name: /shown/ })).not.toBeInTheDocument()
  })

  it('when the reviewer filters to one status, the bulk action applies to exactly that filtered set', async () => {
    const h = setup([proposedA, rejected, approved, stale])
    await pickStatus('approved')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Reject shown' }))
    expect(h.onBulk).toHaveBeenCalledWith(['fnd_approved'], 'reject')

    await pickStatus('rejected')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Approve shown' }))
    expect(h.onBulk).toHaveBeenLastCalledWith(['fnd_rejected'], 'approve')
  })

  it('never sends stale findings, even when filtered to "stale"', async () => {
    setup([proposedA, stale])
    await pickStatus('stale')
    expect(screen.queryByRole('button', { name: /shown/ })).not.toBeInTheDocument()
  })
})
