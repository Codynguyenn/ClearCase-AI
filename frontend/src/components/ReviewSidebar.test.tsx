import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ReviewSidebar, type TabId } from './ReviewSidebar'

describe('ReviewSidebar tabs', () => {
  const renderTab = (tab: TabId) => <p>content of {tab}</p>

  it('has Chat and Policy tabs between Search and Text, and switches to each', async () => {
    const onTab = vi.fn()
    render(<ReviewSidebar tab="review" onTab={onTab} render={renderTab} error={null} />)
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Review', 'Search', 'Chat', 'Policy', 'Text', 'Export'])
    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: 'Policy' }))
    expect(onTab).toHaveBeenLastCalledWith('policy')
    await user.click(screen.getByRole('tab', { name: 'Chat' }))
    expect(onTab).toHaveBeenLastCalledWith('chat')
  })

  it('renders only the active tab’s content and marks it selected', () => {
    render(<ReviewSidebar tab="policy" onTab={vi.fn()} render={renderTab} error={null} />)
    expect(screen.getByText('content of policy')).toBeInTheDocument()
    expect(screen.queryByText('content of review')).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Policy' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute('aria-selected', 'false')
  })

  it('shows an error banner that can be dismissed', async () => {
    render(<ReviewSidebar tab="review" onTab={vi.fn()} render={renderTab} error="Something broke" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Something broke')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Dismiss error' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
