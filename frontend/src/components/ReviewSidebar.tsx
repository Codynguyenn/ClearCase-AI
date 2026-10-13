import { useState } from 'react'

export type TabId = 'review' | 'search' | 'text' | 'export'

const TABS: { id: TabId; label: string }[] = [
  { id: 'review', label: 'Review' },
  { id: 'search', label: 'Search' },
  { id: 'text', label: 'Text' },
  { id: 'export', label: 'Export' },
]

interface Props {
  /** Render function per tab so inactive tabs are not mounted. */
  render: (tab: TabId) => React.ReactNode
  error: string | null
  badge?: Partial<Record<TabId, number>>
  tab: TabId
  onTab: (t: TabId) => void
}

export function ReviewSidebar({ render, error, badge, tab, onTab }: Props) {
  const [dismissed, setDismissed] = useState<string | null>(null)
  const showError = error && error !== dismissed
  return (
    <aside className="flex h-full w-[26rem] shrink-0 flex-col border-l border-slate-200 bg-white" aria-label="Review sidebar">
      <div role="tablist" className="flex border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            type="button"
            aria-selected={tab === t.id}
            onClick={() => onTab(t.id)}
            className={`flex-1 px-2 py-2 text-xs font-medium ${
              tab === t.id ? 'border-b-2 border-indigo-600 text-indigo-700' : 'text-slate-600 hover:bg-slate-50'
            }`}
          >
            {t.label}
            {badge?.[t.id] ? (
              <span className="ml-1 rounded-full bg-amber-200 px-1.5 text-[10px] text-amber-900">{badge[t.id]}</span>
            ) : null}
          </button>
        ))}
      </div>
      {showError && (
        <div role="alert" className="flex items-start gap-2 border-b border-red-200 bg-red-50 p-2 text-xs text-red-800">
          <span className="flex-1">{error}</span>
          <button type="button" aria-label="Dismiss error" onClick={() => setDismissed(error)} className="text-red-600">
            ✕
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1" role="tabpanel">
        {render(tab)}
      </div>
    </aside>
  )
}
