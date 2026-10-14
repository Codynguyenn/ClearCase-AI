// TypeScript mirrors of the backend Pydantic models (backend/app/models/documents.py).
// Keeping them hand-written and small is deliberate for the MVP; an OpenAPI
// generator can replace this file later without changing callers.

export type DocumentStatus = 'uploaded' | 'processing' | 'ready' | 'unsupported' | 'error'

export type PageKind = 'text' | 'scanned' | 'mixed' | 'empty'

export interface BBox {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface Word {
  text: string
  bbox: BBox
  block: number
  line: number
  word_index: number
  char_start: number
  char_end: number
}

export interface PageGeometry {
  width: number
  height: number
  rotation: number
}

export interface PageSummary {
  page_number: number
  geometry: PageGeometry
  kind: PageKind
  word_count: number
  image_count: number
  image_coverage: number
  supported: boolean
}

export interface PageText extends PageSummary {
  text: string
  words: Word[]
}

export interface ExtractionInfo {
  engine: string
  engine_version: string
  extracted_at: string | null
  duration_ms: number | null
  supported_pages: number
  unsupported_pages: number[]
  limitations: string[]
}

export interface ProcessingInfo {
  attempts: number
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface DocumentRecord {
  id: string
  owner_id: string
  filename: string
  content_hash: string
  size_bytes: number
  page_count: number
  status: DocumentStatus
  version: number
  review_revision: number
  extraction: ExtractionInfo | null
  processing: ProcessingInfo
  created_at: string
  updated_at: string
}

export interface DocumentListResponse {
  items: DocumentRecord[]
  total: number
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  mongodb: boolean
  assistant_mode: string
  pdf_engine: string
  ner_available: boolean
  ner_model: string | null
  embeddings: EmbeddingStatus
}

export interface EmbeddingStatus {
  available: boolean
  enabled: boolean
  model: string
  revision: string
  dimension: number | null
  reason: string | null
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown }
}

// ---------------------------------------------------------------- Phase 2: review
// Mirrors backend/app/models/{search,findings}.py.

export type Category =
  | 'PERSON'
  | 'ORGANIZATION'
  | 'LOCATION'
  | 'EMAIL'
  | 'PHONE'
  | 'SSN'
  | 'ADDRESS'
  | 'CUSTOM'
  | 'MANUAL'

/** Categories the detectors can produce (CUSTOM/MANUAL come from the user). */
export const DETECTOR_CATEGORIES: Category[] = [
  'PERSON',
  'ORGANIZATION',
  'LOCATION',
  'EMAIL',
  'PHONE',
  'SSN',
  'ADDRESS',
]

export type ReviewStatus = 'proposed' | 'approved' | 'rejected' | 'stale'
export type DecisionAction = 'approve' | 'reject' | 'reopen'

export interface Occurrence {
  occurrence_id: string
  page_number: number
  char_start: number
  char_end: number
  text: string
  context: string
  boxes: BBox[]
  mappable: boolean
  partial_word: boolean
}

export interface SearchResult {
  query: string
  total: number
  truncated: boolean
  occurrences: Occurrence[]
  searched_pages: number[]
  unsupported_pages: number[]
}

export interface Finding {
  id: string
  document_id: string
  page_number: number
  char_start: number | null
  char_end: number | null
  text: string
  context: string
  boxes: BBox[]
  original_boxes: BBox[]
  category: Category
  source: string
  sources: string[]
  origin: 'detector' | 'search' | 'manual' | 'agent'
  label: 'format_valid' | 'review_needed' | 'user_specified' | 'manual_region'
  explanation: string
  policy_refs: string[]
  status: ReviewStatus
  mappable: boolean
  partial_word: boolean
  adjusted: boolean
  group_key: string
  overlaps: string[]
  document_version: number
}

export interface FindingsResponse {
  items: Finding[]
  total: number
  review_revision: number
}

export interface ProposalResult {
  created: Finding[]
  existing: Finding[]
  rejected_inputs: string[]
  notes: string[]
  review_revision: number
}

export interface ReviewSummary {
  document_id: string
  review_revision: number
  by_status: Record<string, number>
  by_category: Record<string, Record<string, number>>
  unresolved: number
  unmappable_proposed: number
  unsupported_pages: number[]
  approved: number
  can_export: boolean
  export_blockers: string[]
  warnings: string[]
  unproposed_copies: number
}

export interface ExportAuthorization {
  token: string
  document_id: string
  review_revision: number
  expires_at: string
}

export interface VerificationCheck {
  name: string
  passed: boolean
  detail: string
  required: boolean
}

export interface ExportRecord {
  id: string
  document_id: string
  status: 'verified' | 'verification_failed' | 'blocked'
  review_revision: number
  approved_count: number
  by_category: Record<string, number>
  output_hash: string | null
  output_size_bytes: number | null
  filename: string | null
  checks: VerificationCheck[]
  retained: { intentionally_retained: number; unresolved: number }
  unsupported_pages_acknowledged: number[]
  scope_statement: string
  stale: boolean
  downloadable: boolean
  created_at: string
}

// ---------------------------------------------------------------- policies (Phase 4)
export type PolicyStatus = 'active' | 'archived'
export type EmbeddingState = 'ready' | 'partial' | 'stale' | 'unavailable' | 'pending'

export interface EmbeddingSummary {
  state: EmbeddingState
  model: string
  revision: string
  chunks_current: number
  chunks_stale: number
  chunks_missing: number
  detail: string | null
}

export interface PolicyRecord {
  id: string
  policy_id: string
  version: string
  title: string
  source: string
  is_fictional_example: boolean
  status: PolicyStatus
  chunk_count: number
  word_count: number
  truncated_chunks: number
  embedding: EmbeddingSummary
  created_at: string
  updated_at: string
}

export interface PolicyPassage {
  citation_id: string
  policy_id: string
  policy_title: string
  policy_version: string
  policy_status: PolicyStatus
  section: string
  source: string
  ordinal: number
  text: string
  is_fictional_example: boolean
}

export interface RetrievedPassage extends PolicyPassage {
  score: number
  rank: number
}

export type RetrievalStatus = 'ok' | 'no_relevant_policy' | 'no_policies' | 'unavailable'

export interface RetrievalResult {
  status: RetrievalStatus
  message: string
  query: string
  passages: RetrievedPassage[]
  method: { kind: string; model: string; revision: string; min_score: number; note: string }
  chunks_considered: number
  chunks_stale_ignored: number
  top_score_below_cutoff: number | null
}

export interface ResolveResult {
  found: PolicyPassage[]
  missing: string[]
}
