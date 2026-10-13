import { useRef, useState, type DragEvent } from 'react'
import type { DocumentRecord } from '../api/types'
import { StatusBadge } from './StatusBadge'

interface Props {
  documents: DocumentRecord[]
  loading: boolean
  error: string | null
  selectedId: string | null
  onSelect: (id: string) => void
  onUpload: (file: File) => Promise<unknown>
  uploading: boolean
  uploadError: string | null
  onDelete: (id: string) => Promise<void>
  onRefresh: () => void
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export function Sidebar({
  documents,
  loading,
  error,
  selectedId,
  onSelect,
  onUpload,
  uploading,
  uploadError,
  onDelete,
  onRefresh,
}: Props) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const pick = () => fileInput.current?.click()

  const handleFiles = async (files: FileList | null) => {
    const file = files?.[0]
    if (file) await onUpload(file)
    if (fileInput.current) fileInput.current.value = ''
  }

  const onDrop = async (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    await handleFiles(e.dataTransfer.files)
  }

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-200 p-4">
        <h1 className="text-lg font-semibold tracking-tight">ClearCase</h1>
        <p className="text-xs text-slate-500">Document redaction review</p>
      </div>

      <div
        className={`m-3 rounded-lg border-2 border-dashed p-3 text-center text-sm transition-colors ${
          dragging ? 'border-blue-500 bg-blue-50' : 'border-slate-300'
        }`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => void onDrop(e)}
      >
        <input
          ref={fileInput}
          type="file"
          accept="application/pdf,.pdf"
          className="sr-only"
          onChange={(e) => void handleFiles(e.target.files)}
          data-testid="file-input"
        />
        <button
          type="button"
          onClick={pick}
          disabled={uploading}
          className="w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
        >
          {uploading ? 'Uploading…' : 'Upload PDF'}
        </button>
        <p className="mt-2 text-xs text-slate-500">or drop a text-based PDF here</p>
        {uploadError && (
          <p role="alert" className="mt-2 rounded bg-red-50 p-2 text-left text-xs text-red-700">
            {uploadError}
          </p>
        )}
      </div>

      <div className="flex items-center justify-between px-4 py-1 text-xs text-slate-500">
        <span>{documents.length} document{documents.length === 1 ? '' : 's'}</span>
        <button type="button" onClick={onRefresh} className="hover:text-slate-800">
          Refresh
        </button>
      </div>

      <nav aria-label="Documents" className="flex-1 overflow-y-auto">
        {loading && <p className="p-4 text-sm text-slate-500">Loading…</p>}
        {error && (
          <p role="alert" className="m-3 rounded bg-red-50 p-2 text-xs text-red-700">
            {error}
          </p>
        )}
        {!loading && !error && documents.length === 0 && (
          <div className="p-4 text-sm text-slate-500">
            <p className="font-medium text-slate-700">No documents yet</p>
            <p className="mt-1">
              Upload a PDF to begin. A synthetic sample lives at{' '}
              <code className="rounded bg-slate-100 px-1">sample_data/sample_contract.pdf</code>.
            </p>
          </div>
        )}
        <ul>
          {documents.map((doc) => {
            const selected = doc.id === selectedId
            return (
              <li key={doc.id} className="border-b border-slate-100">
                <div
                  className={`flex items-start gap-2 px-3 py-2 ${selected ? 'bg-blue-50' : 'hover:bg-slate-50'}`}
                >
                  <button
                    type="button"
                    onClick={() => onSelect(doc.id)}
                    aria-current={selected ? 'true' : undefined}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div className="truncate text-sm font-medium text-slate-800" title={doc.filename}>
                      {doc.filename}
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-xs text-slate-500">
                      <StatusBadge status={doc.status} />
                      <span>
                        {doc.page_count} p · {formatBytes(doc.size_bytes)}
                      </span>
                    </div>
                  </button>
                  {confirmDelete === doc.id ? (
                    <div className="flex flex-col gap-1">
                      <button
                        type="button"
                        className="rounded bg-red-600 px-2 py-0.5 text-xs text-white"
                        onClick={() => {
                          setConfirmDelete(null)
                          void onDelete(doc.id)
                        }}
                      >
                        Delete
                      </button>
                      <button
                        type="button"
                        className="rounded border px-2 py-0.5 text-xs"
                        onClick={() => setConfirmDelete(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      aria-label={`Delete ${doc.filename}`}
                      title="Delete document"
                      onClick={() => setConfirmDelete(doc.id)}
                      className="rounded px-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                    >
                      ×
                    </button>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      </nav>

      <div className="border-t border-slate-200 p-3 text-[11px] leading-snug text-slate-500">
        Local demo. Files stay on this machine. Sample content is synthetic; this tool does not
        guarantee legal compliance or detect every sensitive item.
      </div>
    </aside>
  )
}
