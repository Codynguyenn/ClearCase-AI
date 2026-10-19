import { useState } from 'react'
import { api } from '../api/client'
import type { ExportRecord, ReviewSummary } from '../api/types'

interface Props {
  documentId: string
  summary: ReviewSummary | null
  exports: ExportRecord[]
  busy: boolean
  onExport: (acknowledgeUnsupported: boolean) => void
  onAcknowledge: (exportId: string) => void
  /** Propose the other copies of approved text that no finding covers (proposals only; the reviewer still decides). */
  onProposeCopies: () => void
}

const CHECK_TITLES: Record<string, string> = {
  output_opens: 'Output file opens',
  text_removed_in_approved_boxes: 'No text left inside approved boxes',
  boxes_render_black: 'Approved boxes render solid black',
  metadata_and_attachments_scrubbed: 'Metadata, attachments and annotations removed',
  full_rewrite: 'File fully rewritten (no old revisions)',
  original_unchanged: 'Original upload unchanged',
  no_collateral_text_loss: 'Nothing outside the boxes was lost',
  no_unresolved_copies: 'No other copies of approved text remain',
  export_allowed: 'File can be sanitised',
  export_completed: 'Export completed',
}

export function ExportPanel({ documentId, summary, exports, busy, onExport, onAcknowledge, onProposeCopies }: Props) {
  const [ack, setAck] = useState(false)
  const unsupported = summary?.unsupported_pages ?? []
  const needsAck = unsupported.length > 0
  const canClick = !!summary?.can_export && (!needsAck || ack) && !busy

  return (
    <div className="h-full overflow-auto p-3 text-sm">
      <h2 className="text-sm font-semibold text-slate-800">Export redacted PDF</h2>
      <p className="mt-1 text-xs leading-snug text-slate-600">
        Exporting permanently removes the approved content from a <em>copy</em> of the document, then checks the
        result. Your original upload is never modified.
      </p>

      {summary && !summary.can_export && (
        <ul className="mt-2 space-y-1 rounded bg-slate-100 p-2 text-xs text-slate-700" aria-label="Export blockers">
          {summary.export_blockers.map((b) => (
            <li key={b}>• {b}</li>
          ))}
        </ul>
      )}

      {summary && summary.warnings.length > 0 && (
        <ul className="mt-2 space-y-1 rounded bg-amber-50 p-2 text-xs text-amber-900" aria-label="Export warnings">
          {summary.warnings.map((w) => (
            <li key={w}>• {w}</li>
          ))}
        </ul>
      )}

      {summary && summary.unproposed_copies > 0 && (
        <div className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-950" data-testid="propose-copies-before">
          <p>Proposing only suggests them: you still approve or reject each one, then export.</p>
          <button
            type="button"
            disabled={busy}
            onClick={onProposeCopies}
            className="mt-1.5 rounded bg-amber-700 px-2 py-1 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50"
          >
            Propose these {summary.unproposed_copies} copy(ies) for review
          </button>
        </div>
      )}

      {needsAck && (
        <label className="mt-2 flex items-start gap-1.5 text-xs text-slate-700">
          <input type="checkbox" className="mt-0.5" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span>
            I understand pages {unsupported.join(', ')} were not analysed and may still contain sensitive content
            (only regions I drew by hand are redacted there).
          </span>
        </label>
      )}

      <button
        type="button"
        disabled={!canClick}
        onClick={() => onExport(ack)}
        className="mt-3 rounded bg-red-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-800 disabled:opacity-40"
      >
        {busy ? 'Working…' : `Export ${summary?.approved ?? 0} approved redaction(s)`}
      </button>

      <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">Exports</h3>
      {exports.length === 0 && <p className="mt-1 text-xs text-slate-500">No exports yet.</p>}
      <ul className="mt-1 space-y-3">
        {exports.map((e) => (
          <ExportCard
            key={e.id}
            documentId={documentId}
            record={e}
            busy={busy}
            onAcknowledge={onAcknowledge}
            onProposeCopies={onProposeCopies}
          />
        ))}
      </ul>
    </div>
  )
}

function ExportCard({
  documentId,
  record: e,
  busy,
  onAcknowledge,
  onProposeCopies,
}: {
  documentId: string
  record: ExportRecord
  busy: boolean
  onAcknowledge: (exportId: string) => void
  onProposeCopies: () => void
}) {
  const failedRequired = e.checks.filter((c) => c.required && !c.passed)
  const warnings = e.checks.filter((c) => !c.required && !c.passed)
  const awaitingAck = e.requires_acknowledgement && !e.warnings_acknowledged && !e.stale
  const remainingPages = [...new Set(e.retained.unresolved_locations.map((l) => l.page))].sort((a, b) => a - b)
  const badge =
    e.status !== 'verified'
      ? { text: e.status.replace('_', ' '), style: 'bg-red-100 text-red-800' }
      : e.has_warnings
        ? { text: 'verified · with warnings', style: 'bg-amber-100 text-amber-900' }
        : { text: 'verified', style: 'bg-emerald-100 text-emerald-800' }
  return (
    <li className="rounded border border-slate-200 p-2.5" data-testid="export-card" data-status={e.status}>
      <div className="flex items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${badge.style}`}>{badge.text}</span>
        <span className="text-[11px] text-slate-500">{new Date(e.created_at + 'Z').toLocaleString()}</span>
        {e.stale && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">OUT OF DATE</span>}
      </div>
      <p className="mt-1 text-xs text-slate-600">{e.approved_count} redaction(s) applied</p>

      {e.stale && (
        <p className="mt-1 text-xs text-amber-800">
          The review changed after this export, so it can no longer be downloaded. Export again.
        </p>
      )}
      {failedRequired.length > 0 && (
        <p className="mt-1 text-xs font-medium text-red-700">
          Verification failed, so no file was kept. Nothing can be downloaded.
        </p>
      )}

      <ul className="mt-2 space-y-0.5 text-xs" aria-label="Verification checks">
        {e.checks.map((c) => (
          <li key={c.name} className="flex gap-1.5" title={c.detail}>
            <span aria-hidden className={c.passed ? 'text-emerald-600' : c.required ? 'text-red-600' : 'text-amber-600'}>
              {c.passed ? '✓' : c.required ? '✗' : '!'}
            </span>
            <span className={c.passed ? 'text-slate-700' : 'text-slate-900'}>
              {CHECK_TITLES[c.name] ?? c.name}
              {!c.passed && <span className="block text-[11px] text-slate-600">{c.detail}</span>}
            </span>
          </li>
        ))}
      </ul>

      {warnings.length > 0 && !e.requires_acknowledgement && (
        <p className="mt-1 text-[11px] text-amber-800">These warnings are informational. Please read them above.</p>
      )}
      {awaitingAck && (
        <div role="alert" className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-950" data-testid="ack-callout">
          <p className="font-medium">
            {e.retained.unresolved} other copy(ies) of text you approved for redaction are still readable in this file
            (page{remainingPages.length === 1 ? '' : 's'} {remainingPages.join(', ')}).
          </p>
          <p className="mt-1 text-[11px] leading-snug">
            No finding covers them, so they were not removed. Hidden or invisible text is a common cause. Propose them
            for review, approve the ones you want removed, and export again. Or acknowledge this and download the file
            anyway.
          </p>
          <div className="mt-1.5 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onProposeCopies}
              className="rounded bg-amber-700 px-2 py-1 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50"
            >
              Propose these {e.retained.unresolved} copy(ies) for review
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAcknowledge(e.id)}
              className="rounded border border-amber-700 bg-white px-2 py-1 text-xs font-medium text-amber-950 hover:bg-amber-100 disabled:opacity-50"
            >
              I understand: allow download
            </button>
          </div>
        </div>
      )}
      {e.requires_acknowledgement && e.warnings_acknowledged && (
        <p className="mt-1 text-[11px] text-amber-800">You acknowledged that other copies of approved text remain in this file.</p>
      )}
      {e.retained.intentionally_retained > 0 && (
        <p className="mt-1 text-[11px] text-slate-600">
          {e.retained.intentionally_retained} copy(ies) of approved text were kept on purpose (rejected findings).
        </p>
      )}

      {e.downloadable && (
        <a
          href={api.exportFileUrl(documentId, e.id)}
          className="mt-2 inline-block rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700"
        >
          Download {e.filename}
        </a>
      )}

      <details className="mt-2 text-[11px] text-slate-500">
        <summary className="cursor-pointer">What this export does and does not cover</summary>
        <p className="mt-1 leading-snug">{e.scope_statement}</p>
      </details>
    </li>
  )
}
