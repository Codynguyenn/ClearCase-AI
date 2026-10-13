import { useCallback, useMemo, useState } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import type { BBox, PageGeometry, Word } from '../api/types'
import { bboxToCss, cssToBbox, renderedPageSize } from '../lib/geometry'

// PDF.js does its heavy lifting in a web worker. Per react-pdf's docs the worker
// URL must be set in the same module that renders <Document>/<Page>.
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3]

/** Something to outline on the page. Colours come from `kind`. */
export interface Highlight {
  id: string
  boxes: BBox[]
  kind: 'proposed' | 'approved' | 'rejected' | 'search'
  selected?: boolean
  label?: string
}

const HIGHLIGHT_STYLES: Record<Highlight['kind'], string> = {
  proposed: 'border-amber-500 bg-amber-300/30',
  approved: 'border-red-700 bg-black/60',
  rejected: 'border-emerald-600 border-dashed bg-emerald-300/15',
  search: 'border-sky-500 bg-sky-300/25',
}

interface Props {
  fileUrl: string
  /** Changes whenever the stored document changes; forces a reload. */
  fileKey: string
  pageNumber: number
  pageCount: number
  onPageChange: (n: number) => void
  geometry: PageGeometry | null
  words: Word[] | null
  highlights?: Highlight[]
  onHighlightClick?: (id: string) => void
  /** When set, the page captures a drag and reports the rectangle in page space. */
  drawPrompt?: string | null
  onDraw?: (box: BBox) => void
  onCancelDraw?: () => void
}

export function PdfViewer({
  fileUrl,
  fileKey,
  pageNumber,
  pageCount,
  onPageChange,
  geometry,
  words,
  highlights = [],
  onHighlightClick,
  drawPrompt = null,
  onDraw,
  onCancelDraw,
}: Props) {
  const [scale, setScale] = useState(1.25)
  const [userRotation, setUserRotation] = useState(0)
  const [showWordBoxes, setShowWordBoxes] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pageInput, setPageInput] = useState(String(pageNumber))
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)

  // "Adjust state during render" (React docs pattern): when a prop changes,
  // reset the dependent local state immediately instead of one render later.
  const [prevPageNumber, setPrevPageNumber] = useState(pageNumber)
  if (prevPageNumber !== pageNumber) {
    setPrevPageNumber(pageNumber)
    setPageInput(String(pageNumber))
  }
  const [prevFileKey, setPrevFileKey] = useState(fileKey)
  if (prevFileKey !== fileKey) {
    setPrevFileKey(fileKey)
    setLoadError(null)
  }

  const totalRotation = ((geometry?.rotation ?? 0) + userRotation) % 360

  const go = useCallback(
    (n: number) => {
      const clamped = Math.min(Math.max(1, n), Math.max(1, pageCount))
      if (clamped !== pageNumber) onPageChange(clamped)
    },
    [onPageChange, pageCount, pageNumber],
  )

  const zoomIn = () => setScale((s) => ZOOM_STEPS.find((z) => z > s + 1e-6) ?? s)
  const zoomOut = () => setScale((s) => [...ZOOM_STEPS].reverse().find((z) => z < s - 1e-6) ?? s)

  // Keyboard navigation when the viewer region has focus.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.target instanceof HTMLInputElement) return
    if (e.key === 'Escape' && drawPrompt) onCancelDraw?.()
    if (e.key === 'ArrowRight' || e.key === 'PageDown') go(pageNumber + 1)
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') go(pageNumber - 1)
    else if (e.key === '+' || e.key === '=') zoomIn()
    else if (e.key === '-') zoomOut()
  }

  const overlaySize = useMemo(
    () => (geometry ? renderedPageSize(geometry, totalRotation, scale) : null),
    [geometry, totalRotation, scale],
  )

  const file = useMemo(() => ({ url: fileUrl }), [fileUrl])

  return (
    <section
      className="flex h-full min-w-0 flex-1 flex-col bg-slate-200"
      aria-label="PDF viewer"
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-300 bg-white px-3 py-2 text-sm">
        <div className="flex items-center gap-1">
          <ToolbarButton onClick={() => go(pageNumber - 1)} disabled={pageNumber <= 1} label="Previous page">
            ‹
          </ToolbarButton>
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              go(Number.parseInt(pageInput, 10) || pageNumber)
            }}
          >
            <label htmlFor="page-input" className="sr-only">
              Page number
            </label>
            <input
              id="page-input"
              className="w-12 rounded border border-slate-300 px-1 py-0.5 text-center"
              value={pageInput}
              onChange={(e) => setPageInput(e.target.value)}
              onBlur={() => go(Number.parseInt(pageInput, 10) || pageNumber)}
              inputMode="numeric"
            />
            <span className="text-slate-500">/ {pageCount}</span>
          </form>
          <ToolbarButton onClick={() => go(pageNumber + 1)} disabled={pageNumber >= pageCount} label="Next page">
            ›
          </ToolbarButton>
        </div>

        <div className="mx-2 h-5 w-px bg-slate-300" aria-hidden />

        <div className="flex items-center gap-1">
          <ToolbarButton onClick={zoomOut} disabled={scale <= ZOOM_STEPS[0]} label="Zoom out">
            −
          </ToolbarButton>
          <span className="w-12 text-center tabular-nums text-slate-700">{Math.round(scale * 100)}%</span>
          <ToolbarButton onClick={zoomIn} disabled={scale >= ZOOM_STEPS[ZOOM_STEPS.length - 1]} label="Zoom in">
            +
          </ToolbarButton>
          <ToolbarButton onClick={() => setScale(1)} label="Reset zoom">
            100%
          </ToolbarButton>
        </div>

        <div className="mx-2 h-5 w-px bg-slate-300" aria-hidden />

        <ToolbarButton onClick={() => setUserRotation((r) => (r + 90) % 360)} label="Rotate clockwise">
          ⟳ Rotate
        </ToolbarButton>

        <label className="ml-auto flex items-center gap-1.5 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={showWordBoxes}
            onChange={(e) => setShowWordBoxes(e.target.checked)}
            disabled={!words}
          />
          Show word boxes
        </label>
      </div>

      {drawPrompt && (
        <div
          role="status"
          className="flex items-center gap-3 border-b border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs text-indigo-900"
        >
          <span>{drawPrompt}</span>
          <button
            type="button"
            onClick={() => onCancelDraw?.()}
            className="ml-auto rounded border border-indigo-300 bg-white px-2 py-0.5 hover:bg-indigo-100"
          >
            Cancel (Esc)
          </button>
        </div>
      )}

      <div className="flex-1 overflow-auto p-6">
        <div className="mx-auto w-fit">
          {loadError ? (
            <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
              Could not render this PDF: {loadError}
            </div>
          ) : (
            <Document
              key={fileKey}
              file={file}
              suspense={false}
              onLoadError={(e) => setLoadError(e.message)}
              onSourceError={(e) => setLoadError(e.message)}
              loading={<div className="p-8 text-sm text-slate-500">Loading PDF…</div>}
              error={<div className="p-8 text-sm text-red-600">Failed to load PDF.</div>}
              className="relative"
            >
              <div className="relative shadow-lg" style={overlaySize ?? undefined}>
                <Page
                  pageNumber={pageNumber}
                  scale={scale}
                  rotate={totalRotation}
                  renderTextLayer
                  renderAnnotationLayer={false}
                  loading={<div className="p-8 text-sm text-slate-500">Rendering page…</div>}
                />
                {/* Overlay layers. All rectangles are stored in page space and converted
                    with bboxToCss, so they follow zoom and rotation. */}
                {showWordBoxes && geometry && words && (
                  <div className="pointer-events-none absolute inset-0" aria-hidden data-testid="word-overlay">
                    {words.map((w) => {
                      const css = bboxToCss(w.bbox, geometry, totalRotation, scale)
                      return (
                        <div
                          key={`${w.block}-${w.line}-${w.word_index}-${w.char_start}`}
                          className="absolute border border-fuchsia-500/70 bg-fuchsia-300/15"
                          style={{ left: css.left, top: css.top, width: css.width, height: css.height }}
                        />
                      )
                    })}
                  </div>
                )}

                {geometry && highlights.length > 0 && (
                  <div className="pointer-events-none absolute inset-0" data-testid="highlight-layer">
                    {highlights.flatMap((h) =>
                      h.boxes.map((b, i) => {
                        const css = bboxToCss(b, geometry, totalRotation, scale)
                        return (
                          <button
                            type="button"
                            key={`${h.id}-${i}`}
                            data-highlight-id={h.id}
                            data-kind={h.kind}
                            title={h.label}
                            aria-label={h.label ?? `${h.kind} highlight`}
                            onClick={() => onHighlightClick?.(h.id)}
                            className={`pointer-events-auto absolute cursor-pointer border ${HIGHLIGHT_STYLES[h.kind]} ${
                              h.selected ? 'ring-2 ring-indigo-600 ring-offset-1' : ''
                            }`}
                            style={{ left: css.left, top: css.top, width: css.width, height: css.height }}
                          />
                        )
                      }),
                    )}
                  </div>
                )}

                {geometry && drawPrompt && (
                  <div
                    className="absolute inset-0 cursor-crosshair bg-indigo-500/5 ring-2 ring-indigo-500"
                    data-testid="draw-layer"
                    onPointerDown={(e) => {
                      const r = e.currentTarget.getBoundingClientRect()
                      const x = e.clientX - r.left
                      const y = e.clientY - r.top
                      e.currentTarget.setPointerCapture(e.pointerId)
                      setDrag({ x0: x, y0: y, x1: x, y1: y })
                    }}
                    onPointerMove={(e) => {
                      if (!drag) return
                      const r = e.currentTarget.getBoundingClientRect()
                      setDrag({ ...drag, x1: e.clientX - r.left, y1: e.clientY - r.top })
                    }}
                    onPointerUp={() => {
                      if (!drag) return
                      const rect = {
                        left: Math.min(drag.x0, drag.x1),
                        top: Math.min(drag.y0, drag.y1),
                        width: Math.abs(drag.x1 - drag.x0),
                        height: Math.abs(drag.y1 - drag.y0),
                      }
                      setDrag(null)
                      // Ignore accidental clicks; a real region is at least a few pixels each way.
                      if (rect.width < 4 || rect.height < 4) return
                      onDraw?.(cssToBbox(rect, geometry, totalRotation, scale))
                    }}
                  >
                    {drag && (
                      <div
                        className="absolute border-2 border-indigo-600 bg-indigo-400/30"
                        style={{
                          left: Math.min(drag.x0, drag.x1),
                          top: Math.min(drag.y0, drag.y1),
                          width: Math.abs(drag.x1 - drag.x0),
                          height: Math.abs(drag.y1 - drag.y0),
                        }}
                      />
                    )}
                  </div>
                )}
              </div>
            </Document>
          )}
        </div>
      </div>
    </section>
  )
}

function ToolbarButton({
  children,
  onClick,
  disabled,
  label,
}: {
  children: React.ReactNode
  onClick: () => void
  disabled?: boolean
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="rounded border border-slate-300 bg-white px-2 py-0.5 text-slate-700 hover:bg-slate-50 disabled:opacity-40"
    >
      {children}
    </button>
  )
}
