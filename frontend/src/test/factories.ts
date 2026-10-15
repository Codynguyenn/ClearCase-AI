import type {
  AgentRun,
  ChatMessage,
  Conversation,
  DocumentRecord,
  ExportRecord,
  Finding,
  HealthResponse,
  PolicyPassage,
  PolicyRecord,
  RetrievalResult,
  RetrievedPassage,
  RunDetail,
  ToolEvent,
} from '../api/types'

export function makePolicy(overrides: Partial<PolicyRecord> = {}): PolicyRecord {
  return {
    id: 'pol-1',
    policy_id: 'northwind-redaction-policy',
    version: '1.0',
    title: 'Northwind Analytics Document Redaction Policy',
    source: 'fictional_redaction_policy.md',
    is_fictional_example: true,
    status: 'active',
    chunk_count: 10,
    word_count: 400,
    truncated_chunks: 0,
    chunker_version: 1,
    chunker_outdated: false,
    cited_by_findings: 0,
    created_at: '2026-10-14T00:00:00Z',
    updated_at: '2026-10-14T00:00:00Z',
    embedding: {
      state: 'ready',
      model: 'sentence-transformers/all-MiniLM-L6-v2',
      revision: '1110a243',
      chunks_current: 10,
      chunks_stale: 0,
      chunks_missing: 0,
      detail: null,
    },
    ...overrides,
  }
}

export function makePassage(overrides: Partial<PolicyPassage> = {}): PolicyPassage {
  return {
    citation_id: 'northwind-redaction-policy:1.0:7',
    policy_id: 'northwind-redaction-policy',
    policy_title: 'Northwind Analytics Document Redaction Policy',
    policy_version: '1.0',
    policy_status: 'active',
    section: '6. Organisation names',
    source: 'fictional_redaction_policy.md',
    ordinal: 7,
    text: 'Names of companies are not personal information and should remain visible by default.',
    is_fictional_example: true,
    ...overrides,
  }
}

export function makeRetrieved(overrides: Partial<RetrievedPassage> = {}): RetrievedPassage {
  return { ...makePassage(), score: 0.7, rank: 1, ...overrides }
}

export function makeRetrieval(overrides: Partial<RetrievalResult> = {}): RetrievalResult {
  return {
    status: 'ok',
    message: '1 passage(s) from the active policies.',
    query: 'Can company names stay visible?',
    passages: [makeRetrieved()],
    method: {
      kind: 'semantic_cosine',
      model: 'sentence-transformers/all-MiniLM-L6-v2',
      revision: '1110a243',
      min_score: 0.38,
      weak_min_score: 0.3,
      note: 'heuristic',
    },
    weak_passages: [],
    chunks_considered: 19,
    chunks_stale_ignored: 0,
    top_score_below_cutoff: null,
    ...overrides,
  }
}

export function makeFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: 'fnd_1',
    document_id: 'doc_1',
    page_number: 1,
    char_start: 10,
    char_end: 30,
    text: 'Maria Delgado-Reyes',
    context: '...Maria Delgado-Reyes...',
    boxes: [{ x0: 0, y0: 0, x1: 10, y1: 10 }],
    original_boxes: [{ x0: 0, y0: 0, x1: 10, y1: 10 }],
    category: 'PERSON',
    source: 'ner:spacy:en_core_web_sm@3.8.0',
    sources: ['ner:spacy:en_core_web_sm@3.8.0'],
    origin: 'detector',
    label: 'review_needed',
    explanation: 'Detected as a person name.',
    policy_refs: [],
    status: 'proposed',
    mappable: true,
    partial_word: false,
    adjusted: false,
    group_key: 'g1',
    overlaps: [],
    document_version: 1,
    ...overrides,
  }
}

export function makeHealth(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    status: 'ok',
    mongodb: true,
    assistant_mode: 'demo',
    assistant: { mode: 'demo', provider: null, model: null, configured: true, sends_excerpts_to_provider: false },
    pdf_engine: 'pymupdf 1.28.2',
    ner_available: true,
    ner_model: 'ner:spacy:en_core_web_sm@3.8.0',
    embeddings: {
      available: true,
      enabled: true,
      model: 'sentence-transformers/all-MiniLM-L6-v2',
      revision: '1110a243',
      dimension: 384,
      reason: null,
    },
    ...overrides,
  }
}

export function makeExport(overrides: Partial<ExportRecord> = {}): ExportRecord {
  return {
    id: 'exp-1',
    document_id: 'doc_1',
    status: 'verified',
    review_revision: 3,
    approved_count: 2,
    by_category: { EMAIL: 2 },
    output_hash: 'ab12cd34ef56',
    output_size_bytes: 3500,
    filename: 'redacted_ab12cd34ef.pdf',
    checks: [
      { name: 'output_opens', passed: true, required: true, detail: 'Output reopens with 1 page(s).' },
      { name: 'text_removed_in_approved_boxes', passed: true, required: true, detail: 'No extractable text inside approved boxes.' },
    ],
    retained: { intentionally_retained: 0, unresolved: 0, unresolved_locations: [] },
    unsupported_pages_acknowledged: [],
    scope_statement: 'This export removes text under the rectangles you approved.',
    stale: false,
    has_warnings: false,
    requires_acknowledgement: false,
    warnings_acknowledged: false,
    downloadable: true,
    created_at: '2026-10-14T00:00:00',
    ...overrides,
  }
}

/** An export that is verified, but two readable copies of approved text remain on page 2 (a "with warnings" export). */
export function makeExportWithRemainingCopies(overrides: Partial<ExportRecord> = {}): ExportRecord {
  return makeExport({
    has_warnings: true,
    requires_acknowledgement: true,
    downloadable: false,
    checks: [
      { name: 'output_opens', passed: true, required: true, detail: 'ok' },
      { name: 'no_unresolved_copies', passed: false, required: false, detail: '2 remaining copy(ies) of approved text on page(s) 2 are not covered by any finding.' },
    ],
    retained: { intentionally_retained: 0, unresolved: 2, unresolved_locations: [{ page: 2, group: 0 }, { page: 2, group: 0 }] },
    ...overrides,
  })
}

export function makeDocument(overrides: Partial<DocumentRecord> = {}): DocumentRecord {
  return {
    id: 'doc1',
    owner_id: 'demo-user',
    filename: 'contract.pdf',
    content_hash: 'a'.repeat(64),
    size_bytes: 1234,
    page_count: 3,
    status: 'ready',
    version: 1,
    review_revision: 0,
    extraction: {
      engine: 'pymupdf',
      engine_version: '1.28.2',
      extracted_at: null,
      duration_ms: 12,
      supported_pages: 3,
      unsupported_pages: [],
      pages_with_images: [],
      limitations: [],
    },
    processing: { attempts: 1, started_at: null, finished_at: null, error: null },
    created_at: '2026-10-14T00:00:00Z',
    updated_at: '2026-10-14T00:00:00Z',
    ...overrides,
  }
}

export function makeConversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: 'conv1',
    document_id: 'doc1',
    title: 'Conversation',
    mode: 'demo',
    message_count: 0,
    created_at: '2026-10-14T00:00:00Z',
    updated_at: '2026-10-14T00:00:00Z',
    ...overrides,
  }
}

export function makeMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    conversation_id: 'conv1',
    document_id: 'doc1',
    seq: 1,
    role: 'user',
    content: 'find the emails',
    status: 'complete',
    citations: [],
    run_id: null,
    client_message_id: null,
    created_at: '2026-10-14T00:00:00Z',
    ...overrides,
  }
}

export function makeRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'run1',
    conversation_id: 'conv1',
    document_id: 'doc1',
    mode: 'demo',
    provider: null,
    model: null,
    status: 'completed',
    rounds: 1,
    tool_calls: 1,
    limits_hit: [],
    error_code: null,
    error: null,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    unverified_citations: 0,
    assistant_message_id: 'm2',
    created_at: '2026-10-14T00:00:00Z',
    started_at: '2026-10-14T00:00:01Z',
    finished_at: '2026-10-14T00:00:02Z',
    ...overrides,
  }
}

export function makeToolEvent(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    id: 'ev1',
    run_id: 'run1',
    seq: 1,
    tool: 'search_document',
    status: 'ok',
    duration_ms: 42,
    summary: '2 match(es) on page(s) 1, 2; 0 page(s) not searched',
    error_code: null,
    ...overrides,
  }
}

export function makeRunDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return { run: makeRun(), tool_events: [makeToolEvent()], assistant_message: makeMessage({ id: 'm2', seq: 2, role: 'assistant', content: 'Done.', run_id: 'run1' }), ...overrides }
}
