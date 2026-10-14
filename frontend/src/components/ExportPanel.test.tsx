import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ExportRecord } from '../api/types'
import { makeExport, makeExportWithRemainingCopies } from '../test/factories'
import { ExportPanel } from './ExportPanel'

function setup(record: ExportRecord, { busy = false }: { busy?: boolean } = {}) {
  const onAcknowledge = vi.fn()
  const onExport = vi.fn()
  render(
    <ExportPanel documentId="doc_1" summary={null} exports={[record]} busy={busy} onExport={onExport} onAcknowledge={onAcknowledge} />,
  )
  return { onAcknowledge, onExport, card: within(screen.getByTestId('export-card')) }
}

describe('ExportPanel: export cards', () => {
  it('shows a plain "verified" export with its download link and no warnings', () => {
    const { card } = setup(makeExport())
    expect(card.getByText('verified', { exact: true })).toBeInTheDocument()
    expect(card.queryByText(/with warnings/)).not.toBeInTheDocument()
    expect(card.queryByTestId('ack-callout')).not.toBeInTheDocument()
    const link = card.getByRole('link', { name: 'Download redacted_ab12cd34ef.pdf' })
    expect(link).toHaveAttribute('href', '/api/documents/doc_1/exports/exp-1/file')
  })

  it('labels a verified export that has warnings as "verified · with warnings", not plain "verified"', () => {
    const { card } = setup(makeExportWithRemainingCopies())
    expect(card.getByText('verified · with warnings')).toBeInTheDocument()
    expect(card.queryByText('verified', { exact: true })).not.toBeInTheDocument()
  })

  it('explains the remaining copies, says where they are, and withholds the download until acknowledged', async () => {
    const { card, onAcknowledge } = setup(makeExportWithRemainingCopies())
    const callout = card.getByTestId('ack-callout')
    expect(callout).toHaveAttribute('role', 'alert')
    expect(callout).toHaveTextContent('2 other copy(ies) of text you approved for redaction are still readable in this file (page 2).')
    expect(callout).toHaveTextContent('No finding covers them')
    expect(card.queryByRole('link')).not.toBeInTheDocument() // no way to download yet

    await userEvent.setup().click(within(callout).getByRole('button', { name: 'I understand: allow download' }))
    expect(onAcknowledge).toHaveBeenCalledTimes(1)
    expect(onAcknowledge).toHaveBeenCalledWith('exp-1')
  })

  it('names every affected page, once each', () => {
    const record = makeExportWithRemainingCopies({
      retained: { intentionally_retained: 0, unresolved: 3, unresolved_locations: [{ page: 5, group: 0 }, { page: 2, group: 0 }, { page: 5, group: 1 }] },
    })
    const { card } = setup(record)
    expect(card.getByTestId('ack-callout')).toHaveTextContent('(pages 2, 5)')
  })

  it('after acknowledgement shows the download link and a record of it, but stays honest that there were warnings', () => {
    const { card } = setup(makeExportWithRemainingCopies({ warnings_acknowledged: true, downloadable: true }))
    expect(card.queryByTestId('ack-callout')).not.toBeInTheDocument()
    expect(card.getByText(/You acknowledged that other copies of approved text remain/)).toBeInTheDocument()
    expect(card.getByText('verified · with warnings')).toBeInTheDocument()
    expect(card.getByRole('link', { name: /Download redacted_/ })).toBeInTheDocument()
  })

  it('shows warnings that are not about remaining copies as information, without any acknowledgement step', () => {
    const record = makeExport({
      has_warnings: true,
      checks: [{ name: 'no_collateral_text_loss', passed: false, required: false, detail: '3 word(s) outside approved boxes are missing on page(s) 1.' }],
    })
    const { card } = setup(record)
    expect(card.getByText('verified · with warnings')).toBeInTheDocument()
    expect(card.getByText('These warnings are informational. Please read them above.')).toBeInTheDocument()
    expect(card.queryByTestId('ack-callout')).not.toBeInTheDocument()
    expect(card.getByRole('link', { name: /Download/ })).toBeInTheDocument()
  })

  it('shows a failed verification as failed, with no download and no acknowledgement', () => {
    const record = makeExport({
      status: 'verification_failed',
      downloadable: false,
      filename: null,
      output_hash: null,
      checks: [{ name: 'text_removed_in_approved_boxes', passed: false, required: true, detail: 'Text still present inside approved boxes on page(s) 1.' }],
    })
    const { card } = setup(record)
    expect(card.getByText('verification failed')).toBeInTheDocument()
    expect(card.getByText('Verification failed, so no file was kept. Nothing can be downloaded.')).toBeInTheDocument()
    expect(card.queryByRole('link')).not.toBeInTheDocument()
    expect(card.queryByTestId('ack-callout')).not.toBeInTheDocument()
  })

  it('does not ask to acknowledge an out-of-date export (it cannot be downloaded at all)', () => {
    const { card } = setup(makeExportWithRemainingCopies({ stale: true }))
    expect(card.getByText('OUT OF DATE')).toBeInTheDocument()
    expect(card.queryByTestId('ack-callout')).not.toBeInTheDocument()
    expect(card.queryByRole('link')).not.toBeInTheDocument()
  })

  it('disables the acknowledge button while another action is running', () => {
    const { card } = setup(makeExportWithRemainingCopies(), { busy: true })
    expect(card.getByRole('button', { name: 'I understand: allow download' })).toBeDisabled()
  })
})
