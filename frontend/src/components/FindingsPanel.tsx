import { useMemo, useState } from 'react'
import {
  DETECTOR_CATEGORIES,
  type Category,
  type DecisionAction,
  type Finding,
  type ReviewStatus,
  type ReviewSummary,
} from '../api/types'

interface Props {
  findings: Finding[]
  summary: ReviewSummary | null
  loading: boolean
  busy: boolean
  currentPage: number
  selectedId: string | null
  notice: string | null
  onSelect: (finding: Finding) => void
  onDetect: (categories: Category[]) => void
  onDecide: (id: string, action: DecisionAction, applyToAll: boolean) => void
  onBulk: (ids: string[], action: DecisionAction) => void
  onStartManual: () => void
  onStartAdjust: (finding: Finding) => void
  onOpenCitation: (citationId: string) => void
}

const STATUS_STYLES: Record<ReviewStatus, string> = {
  proposed: 'bg-amber-100 text-amber-800',
  approved: 'bg-slate-900 text-white',
  rejected: 'bg-emerald-100 text-emerald-800',
  stale: 'bg-slate-200 text-slate-600',
}

const LABELS: Record<Finding['label'], string> = {
  format_valid: 'format valid',
  review_needed: 'review needed',
  user_specified: 'you specified',
  manual_region: 'manual region',
}

export function FindingsPanel(p: Props) {
  const [categories, setCategories] = useState<Category[]>(['EMAIL', 'PHONE', 'SSN', 'PERSON'])
  const [statusFilter, setStatusFilter] = useState<ReviewStatus | 'all'>('all')
  const [categoryFilter, setCategoryFilter] = useState<Category | 'all'>('all')
  const [thisPageOnly, setThisPageOnly] = useState(false)

  const visible = useMemo(
    () =>
      p.findings.filter(
        (f) =>
          (statusFilter === 'all' || f.status === statusFilter) &&
          (categoryFilter === 'all' || f.category === categoryFilter) &&
          (!thisPageOnly || f.page_number === p.currentPage),
      ),
    [p.findings, statusFilter, categoryFilter, thisPageOnly, p.currentPage],
  )

  const groupSizes = useMemo(() => {
    const m = new Map<string, number>()
    for (const f of p.findings) if (f.status !== 'stale') m.set(f.group_key, (m.get(f.group_key) ?? 0) + 1)
    return m
  }, [p.findings])

  // With "all statuses" the bulk buttons only touch findings nobody has decided yet, so they cannot overwrite a
  // decision the reviewer already made. Filtering to a specific status is the explicit way to act on those.
  const decidable = visible.filter((f) => f.status !== 'stale')
  const bulkTargets = statusFilter === 'all' ? decidable.filter((f) => f.status === 'proposed') : decidable
  const bulkSuffix = statusFilter === 'all' ? ` (${bulkTargets.length} undecided)` : ''
  const toggle = (c: Category) =>
    setCategories((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c]))

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b border-slate-200 p-3">
        <SummaryStrip summary={p.summary} />

        <fieldset className="rounded border border-slate-200 p-2">
          <legend className="px-1 text-xs font-medium text-slate-600">Find sensitive information</legend>
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {DETECTOR_CATEGORIES.map((c) => (
              <label key={c} className="flex items-center gap-1 text-xs text-slate-700">
                <input type="checkbox" checked={categories.includes(c)} onChange={() => toggle(c)} />
                {c.toLowerCase()}
              </label>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              disabled={p.busy || categories.length === 0}
              onClick={() => p.onDetect(categories)}
              className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              Detect &amp; propose
            </button>
            <button
              type="button"
              onClick={p.onStartManual}
              disabled={p.busy}
              className="rounded border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              Draw region…
            </button>
          </div>
          <p className="mt-1.5 text-[11px] leading-snug text-slate-500">
            Detectors only <em>propose</em>. Nothing is redacted until you approve it. They have no confidence
            scores; “review needed” means exactly that.
          </p>
        </fieldset>

        {p.notice && (
          <p role="status" className="rounded bg-sky-50 p-2 text-xs text-sky-900">
            {p.notice}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <select
            aria-label="Filter by status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as ReviewStatus | 'all')}
            className="rounded border border-slate-300 px-1 py-0.5"
          >
            <option value="all">all statuses</option>
            <option value="proposed">proposed</option>
            <option value="approved">approved</option>
            <option value="rejected">rejected</option>
            <option value="stale">stale</option>
          </select>
          <select
            aria-label="Filter by category"
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value as Category | 'all')}
            className="rounded border border-slate-300 px-1 py-0.5"
          >
            <option value="all">all categories</option>
            {[...DETECTOR_CATEGORIES, 'CUSTOM', 'MANUAL'].map((c) => (
              <option key={c} value={c}>
                {c.toLowerCase()}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1 text-slate-600">
            <input type="checkbox" checked={thisPageOnly} onChange={(e) => setThisPageOnly(e.target.checked)} />
            this page
          </label>
        </div>

        {bulkTargets.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-slate-500">
              {statusFilter === 'all' ? 'Shown:' : `Shown (${bulkTargets.length}):`}
            </span>
            <button
              type="button"
              disabled={p.busy}
              onClick={() => p.onBulk(bulkTargets.map((f) => f.id), 'approve')}
              className="rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-50 disabled:opacity-50"
            >
              Approve shown{bulkSuffix}
            </button>
            <button
              type="button"
              disabled={p.busy}
              onClick={() => p.onBulk(bulkTargets.map((f) => f.id), 'reject')}
              className="rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-50 disabled:opacity-50"
            >
              Reject shown{bulkSuffix}
            </button>
          </div>
        )}
      </div>

      <ul className="min-h-0 flex-1 divide-y divide-slate-100 overflow-auto" aria-label="Findings">
        {p.loading && <li className="p-3 text-sm text-slate-500">Loading findings…</li>}
        {!p.loading && visible.length === 0 && (
          <li className="p-3 text-sm text-slate-500">
            {p.findings.length === 0
              ? 'No findings yet. Run the detectors, or search for specific text.'
              : 'No findings match these filters.'}
          </li>
        )}
        {visible.map((f) => (
          <FindingRow
            key={f.id}
            finding={f}
            selected={f.id === p.selectedId}
            busy={p.busy}
            groupSize={groupSizes.get(f.group_key) ?? 1}
            onSelect={() => p.onSelect(f)}
            onDecide={(a, all) => p.onDecide(f.id, a, all)}
            onAdjust={() => p.onStartAdjust(f)}
            onOpenCitation={p.onOpenCitation}
          />
        ))}
      </ul>
    </div>
  )
}

function SummaryStrip({ summary }: { summary: ReviewSummary | null }) {
  if (!summary) return null
  const n = (k: string) => summary.by_status[k] ?? 0
  return (
    <div className="space-y-1" data-testid="review-summary">
      <div className="flex flex-wrap gap-1.5 text-[11px]">
        <Chip className={STATUS_STYLES.proposed}>{n('proposed')} proposed</Chip>
        <Chip className={STATUS_STYLES.approved}>{n('approved')} approved</Chip>
        <Chip className={STATUS_STYLES.rejected}>{n('rejected')} rejected</Chip>
        {n('stale') > 0 && <Chip className={STATUS_STYLES.stale}>{n('stale')} stale</Chip>}
      </div>
      {summary.warnings.map((w) => (
        <p key={w} className="rounded bg-amber-50 p-1.5 text-[11px] leading-snug text-amber-900">
          {w}
        </p>
      ))}
    </div>
  )
}

function Chip({ children, className }: { children: React.ReactNode; className: string }) {
  return <span className={`rounded-full px-2 py-0.5 font-medium ${className}`}>{children}</span>
}

function FindingRow({
  finding: f,
  selected,
  busy,
  groupSize,
  onSelect,
  onDecide,
  onAdjust,
  onOpenCitation,
}: {
  finding: Finding
  selected: boolean
  busy: boolean
  groupSize: number
  onSelect: () => void
  onDecide: (action: DecisionAction, applyToAll: boolean) => void
  onAdjust: () => void
  onOpenCitation: (citationId: string) => void
}) {
  const [applyAll, setApplyAll] = useState(false)
  const isStale = f.status === 'stale'
  const display = f.category === 'MANUAL' ? 'Manual region' : f.text
  return (
    <li
      className={`p-2.5 text-sm ${selected ? 'bg-indigo-50' : ''}`}
      data-finding-id={f.id}
      data-status={f.status}
    >
      <button type="button" onClick={onSelect} className="block w-full text-left">
        <div className="flex items-center gap-1.5">
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${STATUS_STYLES[f.status]}`}>
            {f.status}
          </span>
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
            {f.category.toLowerCase()}
          </span>
          {f.origin === 'agent' && (
            <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-900">assistant</span>
          )}
          <span className="ml-auto text-[11px] text-slate-500">p.{f.page_number}</span>
        </div>
        <p className="mt-1 break-words font-medium text-slate-900">{display || '(empty)'}</p>
        <p className="text-[11px] text-slate-500">
          {f.sources.join(' + ') || f.source} · {LABELS[f.label]}
        </p>
      </button>

      {f.explanation && f.origin === 'agent' && (
        <p className="mt-1 text-[11px] text-slate-600">Reason given: {f.explanation}</p>
      )}
      {f.policy_refs.length > 0 && (
        <p className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-slate-600">
          Policy:
          {f.policy_refs.map((ref) => (
            <button
              key={ref}
              type="button"
              onClick={() => onOpenCitation(ref)}
              aria-label={`Read policy passage ${ref}`}
              className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 font-mono text-indigo-800 hover:bg-indigo-100"
            >
              {ref}
            </button>
          ))}
        </p>
      )}

      {(f.partial_word || !f.mappable || f.overlaps.length > 0 || f.adjusted) && (
        <ul className="mt-1 space-y-0.5 text-[11px] text-amber-800">
          {!f.mappable && <li>Could not be located on the page. Draw a region before approving.</li>}
          {f.partial_word && <li>Starts or ends inside a word; the box covers the whole word.</li>}
          {f.overlaps.length > 0 && <li>Overlaps {f.overlaps.length} other finding(s) (kept separately).</li>}
          {f.adjusted && <li>Rectangle adjusted by you.</li>}
        </ul>
      )}

      {!isStale && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {f.status !== 'approved' && (
            <ActionButton disabled={busy} onClick={() => onDecide('approve', applyAll)}>
              Approve
            </ActionButton>
          )}
          {f.status !== 'rejected' && (
            <ActionButton disabled={busy} onClick={() => onDecide('reject', applyAll)}>
              Reject
            </ActionButton>
          )}
          {f.status !== 'proposed' && (
            <ActionButton disabled={busy} onClick={() => onDecide('reopen', applyAll)}>
              Reopen
            </ActionButton>
          )}
          <ActionButton disabled={busy} onClick={onAdjust}>
            Adjust box
          </ActionButton>
          {groupSize > 1 && f.category !== 'MANUAL' && (
            <label className="flex items-center gap-1 text-[11px] text-slate-600">
              <input type="checkbox" checked={applyAll} onChange={(e) => setApplyAll(e.target.checked)} />
              all {groupSize} matching
            </label>
          )}
        </div>
      )}
      {isStale && (
        <p className="mt-1 text-[11px] text-slate-500">Stale: the document was re-processed. Propose it again.</p>
      )}
    </li>
  )
}

function ActionButton({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
    >
      {children}
    </button>
  )
}
