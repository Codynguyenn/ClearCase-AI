import type { DocumentStatus } from '../api/types'

const STYLES: Record<DocumentStatus, { label: string; className: string }> = {
  uploaded: { label: 'Queued', className: 'bg-slate-200 text-slate-700' },
  processing: { label: 'Processing', className: 'bg-blue-100 text-blue-800' },
  ready: { label: 'Ready', className: 'bg-emerald-100 text-emerald-800' },
  unsupported: { label: 'Partially supported', className: 'bg-amber-100 text-amber-800' },
  error: { label: 'Error', className: 'bg-red-100 text-red-800' },
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const s = STYLES[status]
  const busy = status === 'uploaded' || status === 'processing'
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${s.className}`}
      aria-live={busy ? 'polite' : undefined}
    >
      {busy && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" aria-hidden />}
      {s.label}
    </span>
  )
}
