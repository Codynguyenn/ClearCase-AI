import { render, within } from '@testing-library/react'
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
