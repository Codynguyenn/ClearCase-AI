import { useMemo, useState } from 'react'
import { api } from './api/client'
import type { BBox, Category, DecisionAction, Finding, Occurrence, SearchResult } from './api/types'
import { DocumentHeader } from './components/DocumentHeader'
import { ExportPanel } from './components/ExportPanel'
import { FindingsPanel } from './components/FindingsPanel'
import { HealthBanner } from './components/HealthBanner'
import { PageTextPanel } from './components/PageTextPanel'
import { PdfViewer, type Highlight } from './components/PdfViewer'
import { ReviewSidebar, type TabId } from './components/ReviewSidebar'
import { SearchPanel } from './components/SearchPanel'
import { Sidebar } from './components/Sidebar'
import { useDocuments } from './hooks/useDocuments'
import { usePage } from './hooks/usePage'
import { useReview } from './hooks/useReview'

type DrawMode = { kind: 'manual' } | { kind: 'adjust'; finding: Finding } | null

export default function App() {
  const docs = useDocuments()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [tab, setTab] = useState<TabId>('review')
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null)
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null)
  const [draw, setDraw] = useState<DrawMode>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Derived, not stored: if the selected document is deleted it simply stops
  // matching and the workspace falls back to the empty state.
  const selected = useMemo(
    () => docs.documents.find((d) => d.id === selectedId) ?? null,
    [docs.documents, selectedId],
  )

  // Review-related UI state belongs to one document version. When it changes, reset it
  // during render (React's recommended alternative to an effect that sets state).
  const scopeKey = selected ? `${selected.id}:${selected.version}` : ''
  const [prevScope, setPrevScope] = useState(scopeKey)
  if (prevScope !== scopeKey) {
    setPrevScope(scopeKey)
    setSelectedFindingId(null)
    setSearchResult(null)
    setDraw(null)
    setNotice(null)
  }

  const select = (id: string) => {
    setSelectedId(id)
    setPageNumber(1)
  }

  const hasPages = selected !== null && (selected.status === 'ready' || selected.status === 'unsupported')
  const { page, loading: pageLoading, error: pageError } = usePage(
    hasPages ? selected.id : null,
    pageNumber,
    selected?.version ?? 0,
  )
  const review = useReview(selected?.id ?? null, hasPages)

  const handleUpload = async (file: File) => {
    const created = await docs.upload(file)
    if (created) select(created.id)
  }

  // ---------------------------------------------------------------- highlights
  const highlights = useMemo<Highlight[]>(() => {
    const out: Highlight[] = []
    for (const f of review.findings) {
      if (f.page_number !== pageNumber || f.status === 'stale') continue
      out.push({
        id: f.id,
        boxes: f.boxes,
        kind: f.status,
        selected: f.id === selectedFindingId,
        label: `${f.category.toLowerCase()} (${f.status})${f.text ? `: ${f.text}` : ''}`,
      })
    }
    for (const o of searchResult?.occurrences ?? []) {
      if (o.page_number === pageNumber) {
        out.push({ id: `occ:${o.occurrence_id}`, boxes: o.boxes, kind: 'search', label: `match: ${o.text}` })
      }
    }
    return out
  }, [review.findings, searchResult, pageNumber, selectedFindingId])

  // ------------------------------------------------------------------- actions
  const docId = selected?.id ?? null

  const describeProposal = (r: { created: Finding[]; existing: Finding[]; rejected_inputs: string[]; notes: string[] }) => {
    const parts = [`${r.created.length} new proposal(s)`]
    if (r.existing.length) parts.push(`${r.existing.length} already proposed`)
    if (r.rejected_inputs.length) parts.push(`${r.rejected_inputs.length} refused`)
    return [parts.join(', ') + '.', ...r.notes].join(' ')
  }

  const detect = (categories: Category[]) =>
    docId &&
    review.run(async () => {
      setNotice(describeProposal(await api.proposeDetected(docId, categories)))
    })

  const decide = (id: string, action: DecisionAction, all: boolean) =>
    docId && review.run(() => api.decide(docId, id, action, all))

  const bulk = (ids: string[], action: DecisionAction) =>
    docId && review.run(() => api.bulkDecide(docId, ids, action))

  const proposeFromSearch = (
    q: string,
    opts: { caseSensitive: boolean; wholeWord: boolean },
    category: Category,
    selection: { occurrenceIds?: string[]; allMatches?: boolean },
  ) =>
    docId &&
    review.run(async () => {
      const r = await api.proposeFromSearch(docId, {
        q,
        case_sensitive: opts.caseSensitive,
        whole_word: opts.wholeWord,
        category,
        occurrence_ids: selection.occurrenceIds,
        all_matches: selection.allMatches ?? false,
      })
      setNotice(describeProposal(r))
      setTab('review')
    })

  const onFindingSelect = (f: Finding) => {
    setSelectedFindingId(f.id)
    setPageNumber(f.page_number)
  }

  const onHighlightClick = (id: string) => {
    if (id.startsWith('occ:')) {
      setTab('search')
      return
    }
    setTab('review')
    setSelectedFindingId(id)
  }

  const onDraw = (box: BBox) => {
    if (!docId || !draw) return
    if (draw.kind === 'manual') {
      setDraw(null)
      void review.run(async () => {
        await api.addManualRegion(docId, pageNumber, box)
        setNotice('Manual region added and approved (you drew it).')
      })
    } else {
      if (draw.finding.page_number !== pageNumber) {
        setNotice(`Draw on page ${draw.finding.page_number}, where this finding is.`)
        return
      }
      const f = draw.finding
      setDraw(null)
      void review.run(async () => {
        await api.adjust(docId, f.id, [box])
        setNotice('Rectangle replaced. The finding is back to “proposed” so you can re-approve it.')
      })
    }
  }

  const startAdjust = (f: Finding) => {
    setPageNumber(f.page_number)
    setSelectedFindingId(f.id)
    setDraw({ kind: 'adjust', finding: f })
  }

  const drawPrompt =
    draw === null
      ? null
      : draw.kind === 'manual'
        ? `Drag on page ${pageNumber} to draw a region to redact. It will be approved immediately.`
        : `Drag to replace the rectangle for this finding on page ${draw.finding.page_number}.`

  const jumpToOccurrence = (o: Occurrence) => setPageNumber(o.page_number)

  const renderTab = (t: TabId) => {
    if (!selected) return null
    switch (t) {
      case 'review':
        return (
          <FindingsPanel
            findings={review.findings}
            summary={review.summary}
            loading={review.loading}
            busy={review.busy}
            currentPage={pageNumber}
            selectedId={selectedFindingId}
            notice={notice}
            onSelect={onFindingSelect}
            onDetect={(c) => void detect(c)}
            onDecide={(id, a, all) => void decide(id, a, all)}
            onBulk={(ids, a) => void bulk(ids, a)}
            onStartManual={() => setDraw({ kind: 'manual' })}
            onStartAdjust={startAdjust}
          />
        )
      case 'search':
        return (
          <SearchPanel
            documentId={selected.id}
            busy={review.busy}
            result={searchResult}
            onResult={setSearchResult}
            onJump={jumpToOccurrence}
            onPropose={(q, o, c, s) => void proposeFromSearch(q, o, c, s)}
          />
        )
      case 'text':
        return <PageTextPanel page={page} loading={pageLoading} error={pageError} />
      case 'export':
        return (
          <ExportPanel
            documentId={selected.id}
            summary={review.summary}
            exports={review.exports}
            busy={review.busy}
            onExport={(ack) =>
              void review.run(async () => {
                const auth = await api.authorizeExport(selected.id)
                const record = await api.createExport(selected.id, auth.token, ack)
                setNotice(`Export ${record.status.replace('_', ' ')}.`)
              })
            }
          />
        )
    }
  }

  return (
    <div className="flex h-full flex-col">
      <HealthBanner />
      <div className="flex min-h-0 flex-1">
        <Sidebar
          documents={docs.documents}
          loading={docs.loading}
          error={docs.error}
          selectedId={selectedId}
          onSelect={select}
          onUpload={handleUpload}
          uploading={docs.uploading}
          uploadError={docs.uploadError}
          onDelete={docs.remove}
          onRefresh={() => void docs.refresh()}
        />

        <main className="flex min-w-0 flex-1 flex-col">
          {!selected ? (
            <EmptyWorkspace />
          ) : (
            <>
              <DocumentHeader doc={selected} onRetry={() => docs.reprocess(selected.id)} />
              <div className="flex min-h-0 flex-1">
                <PdfViewer
                  fileUrl={api.documentFileUrl(selected.id)}
                  fileKey={`${selected.id}:${selected.content_hash}`}
                  pageNumber={pageNumber}
                  pageCount={selected.page_count}
                  onPageChange={setPageNumber}
                  geometry={page?.geometry ?? null}
                  words={page?.words ?? null}
                  highlights={highlights}
                  onHighlightClick={onHighlightClick}
                  drawPrompt={drawPrompt}
                  onDraw={onDraw}
                  onCancelDraw={() => setDraw(null)}
                />
                {hasPages ? (
                  <ReviewSidebar
                    tab={tab}
                    onTab={setTab}
                    render={renderTab}
                    error={review.error}
                    badge={{ review: review.summary?.unresolved ?? 0 }}
                  />
                ) : (
                  <aside className="w-[26rem] shrink-0 border-l border-slate-200 bg-white p-4 text-sm text-slate-600">
                    Review tools appear once the document has been processed.
                  </aside>
                )}
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

function EmptyWorkspace() {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="max-w-md text-center">
        <h2 className="text-lg font-semibold text-slate-800">Select or upload a document</h2>
        <p className="mt-2 text-sm text-slate-600">
          ClearCase extracts the text and word coordinates from a text-based PDF so that sensitive
          information can be found, reviewed and removed. Scanned pages are detected and flagged rather
          than silently skipped.
        </p>
      </div>
    </div>
  )
}
