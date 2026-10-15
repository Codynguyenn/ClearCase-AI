import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { makeDocument } from '../test/factories'
import { DocumentHeader } from './DocumentHeader'

const withExtraction = (patch: Record<string, unknown>) => {
  const base = makeDocument()
  return makeDocument({ extraction: { ...base.extraction!, ...patch } })
}

describe('DocumentHeader', () => {
  it('shows no warning for an ordinary text document', () => {
    render(<DocumentHeader doc={makeDocument()} onRetry={vi.fn()} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('warns, without blocking, that text inside images on a ready document is not searched', () => {
    render(<DocumentHeader doc={withExtraction({ pages_with_images: [2, 3] })} onRetry={vi.fn()} />)
    const note = screen.getByRole('status')
    expect(note).toHaveTextContent('Pages 2, 3 contain images')
    expect(note).toHaveTextContent('Text inside images is not searched')
    expect(note).toHaveTextContent('only blanked where an approved box covers it')
  })

  it('uses the singular for one page', () => {
    render(<DocumentHeader doc={withExtraction({ pages_with_images: [1] })} onRetry={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent('Page 1 contains an image')
  })

  it('still renders for records saved before this field existed', () => {
    const base = makeDocument()
    const { pages_with_images: _unused, ...legacy } = base.extraction!
    render(<DocumentHeader doc={makeDocument({ extraction: legacy })} onRetry={vi.fn()} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows the time-limit error with a Retry button', () => {
    const onRetry = vi.fn()
    const doc = makeDocument({
      status: 'error',
      processing: { attempts: 1, started_at: null, finished_at: null, error: 'ExtractionTimeout: Extraction stopped after 60 s' },
    })
    render(<DocumentHeader doc={doc} onRetry={onRetry} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Extraction stopped after 60 s')
    screen.getByRole('button', { name: 'Retry processing' }).click()
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})
