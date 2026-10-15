import type { DocumentRecord } from '../api/types'
import { StatusBadge } from './StatusBadge'

interface Props {
  doc: DocumentRecord
  onRetry: () => Promise<void>
}

/** Filename, status and any honest warnings about what could not be analysed. */
export function DocumentHeader({ doc, onRetry }: Props) {
  const ex = doc.extraction
  const imagePages = ex?.pages_with_images ?? []
  return (
    <header className="border-b border-slate-200 bg-white px-4 py-2">
      <div className="flex items-center gap-3">
        <h2 className="truncate text-base font-semibold text-slate-900" title={doc.filename}>
          {doc.filename}
        </h2>
        <StatusBadge status={doc.status} />
        <span className="text-xs text-slate-500">
          {doc.page_count} pages · v{doc.version} · sha256 {doc.content_hash.slice(0, 12)}…
        </span>
        {doc.status === 'error' && (
          <button
            type="button"
            onClick={() => void onRetry()}
            className="ml-auto rounded bg-slate-800 px-2 py-1 text-xs text-white hover:bg-slate-700"
          >
            Retry processing
          </button>
        )}
      </div>

      {doc.status === 'error' && doc.processing.error && (
        <p role="alert" className="mt-2 rounded bg-red-50 p-2 text-xs text-red-700">
          {doc.processing.error}
        </p>
      )}

      {doc.status === 'unsupported' && ex && (
        <p role="status" className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-800">
          {ex.supported_pages} of {doc.page_count} pages contain extractable text. Page
          {ex.unsupported_pages.length === 1 ? '' : 's'} {ex.unsupported_pages.join(', ')}{' '}
          {ex.unsupported_pages.length === 1 ? 'is' : 'are'} scanned or image-heavy and{' '}
          <strong>cannot be analysed</strong>. Review of this document is incomplete.
        </p>
      )}

      {doc.status === 'ready' && imagePages.length > 0 && (
        <p role="status" className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-800">
          Page{imagePages.length === 1 ? '' : 's'} {imagePages.join(', ')}{' '}
          {imagePages.length === 1 ? 'contains an image' : 'contain images'}. Text inside images is{' '}
          <strong>not searched</strong>, and an image is only blanked where an approved box covers it.
        </p>
      )}

      {(doc.status === 'uploaded' || doc.status === 'processing') && (
        <p role="status" className="mt-2 text-xs text-slate-500">
          Extracting text and coordinates…
        </p>
      )}
    </header>
  )
}
