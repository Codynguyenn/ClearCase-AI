import type {
  ApiErrorBody,
  BBox,
  Category,
  Conversation,
  DecisionAction,
  DocumentListResponse,
  DocumentRecord,
  ExportAuthorization,
  ExportRecord,
  Finding,
  FindingsResponse,
  HealthResponse,
  MessagePage,
  PageSummary,
  PageText,
  PolicyPassage,
  PolicyRecord,
  ProposalResult,
  RetrievalResult,
  ReviewSummary,
  RunDetail,
  SearchResult,
  SendResult,
} from './types'

/** Error thrown for non-2xx responses, carrying the backend's stable error code. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch {
    throw new ApiError(0, 'network_error', 'Could not reach the ClearCase backend. Is it running?')
  }
  if (!response.ok) {
    let code = 'http_error'
    let message = `Request failed with status ${response.status}`
    try {
      const body = (await response.json()) as ApiErrorBody
      if (body?.error) {
        code = body.error.code
        message = body.error.message
      }
    } catch {
      // body was not JSON; keep the generic message
    }
    throw new ApiError(response.status, code, message)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  health: () => request<HealthResponse>('/api/health'),

  listDocuments: () => request<DocumentListResponse>('/api/documents'),

  getDocument: (id: string) => request<DocumentRecord>(`/api/documents/${id}`),

  uploadDocument: (file: File) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return request<DocumentRecord>('/api/documents', { method: 'POST', body: form })
  },

  reprocessDocument: (id: string) =>
    request<DocumentRecord>(`/api/documents/${id}/reprocess`, { method: 'POST' }),

  deleteDocument: (id: string) => request<unknown>(`/api/documents/${id}`, { method: 'DELETE' }),

  listPages: (id: string) => request<PageSummary[]>(`/api/documents/${id}/pages`),

  getPage: (id: string, pageNumber: number) =>
    request<PageText>(`/api/documents/${id}/pages/${pageNumber}`),

  /** URL the PDF viewer loads directly (streamed from GridFS by the backend). */
  documentFileUrl: (id: string) => `/api/documents/${id}/file`,

  // ---- search
  search: (id: string, q: string, opts: { caseSensitive: boolean; wholeWord: boolean }) => {
    const params = new URLSearchParams({
      q,
      case_sensitive: String(opts.caseSensitive),
      whole_word: String(opts.wholeWord),
    })
    return request<SearchResult>(`/api/documents/${id}/search?${params}`)
  },

  // ---- review
  listFindings: (id: string) => request<FindingsResponse>(`/api/documents/${id}/findings`),

  reviewSummary: (id: string) => request<ReviewSummary>(`/api/documents/${id}/review-summary`),

  proposeDetected: (id: string, categories: Category[]) =>
    request<ProposalResult>(
      `/api/documents/${id}/findings/propose-detected`,
      jsonInit('POST', { categories }),
    ),

  proposeFromSearch: (
    id: string,
    body: {
      q: string
      case_sensitive: boolean
      whole_word: boolean
      category: Category
      occurrence_ids?: string[]
      all_matches?: boolean
    },
  ) => request<ProposalResult>(`/api/documents/${id}/findings/propose-search`, jsonInit('POST', body)),

  /** Propose every other copy of text the reviewer approved that no finding covers. Proposals only: nothing is approved. */
  proposeRemainingCopies: (id: string) =>
    request<ProposalResult>(`/api/documents/${id}/findings/propose-remaining-copies`, { method: 'POST' }),

  decide: (id: string, findingId: string, action: DecisionAction, applyToAllMatching = false) =>
    request<unknown>(
      `/api/documents/${id}/findings/${findingId}/decision`,
      jsonInit('POST', { action, apply_to_all_matching: applyToAllMatching }),
    ),

  bulkDecide: (id: string, findingIds: string[], action: DecisionAction) =>
    request<unknown>(
      `/api/documents/${id}/findings/bulk-decision`,
      jsonInit('POST', { action, finding_ids: findingIds }),
    ),

  adjust: (id: string, findingId: string, boxes: BBox[]) =>
    request<Finding>(`/api/documents/${id}/findings/${findingId}/boxes`, jsonInit('PUT', { boxes })),

  addManualRegion: (id: string, pageNumber: number, box: BBox) =>
    request<Finding>(
      `/api/documents/${id}/findings/manual`,
      jsonInit('POST', { page_number: pageNumber, box }),
    ),

  // ---- export
  /** `reviewer` is the reviewer's own export; `assistant` lets the assistant export once (only it can use that one). */
  authorizeExport: (id: string, audience: 'reviewer' | 'assistant' = 'reviewer') =>
    request<ExportAuthorization>(
      `/api/documents/${id}/export-authorizations`,
      audience === 'assistant' ? jsonInit('POST', { audience }) : { method: 'POST' },
    ),

  createExport: (id: string, authorization: string, acknowledgeUnsupportedPages: boolean) =>
    request<ExportRecord>(
      `/api/documents/${id}/exports`,
      jsonInit('POST', {
        authorization,
        acknowledge_unsupported_pages: acknowledgeUnsupportedPages,
      }),
    ),

  listExports: (id: string) => request<ExportRecord[]>(`/api/documents/${id}/exports`),

  /** Reviewer-only: "I understand other copies of approved text remain in this file". Unlocks the download. */
  acknowledgeExportWarnings: (id: string, exportId: string) =>
    request<ExportRecord>(`/api/documents/${id}/exports/${exportId}/acknowledge-warnings`, { method: 'POST' }),

  exportFileUrl: (id: string, exportId: string) => `/api/documents/${id}/exports/${exportId}/file`,

  // ---- policies
  listPolicies: () => request<PolicyRecord[]>('/api/policies'),

  uploadPolicy: (file: File) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return request<PolicyRecord>('/api/policies', { method: 'POST', body: form })
  },

  seedPolicies: () => request<PolicyRecord[]>('/api/policies/seed-samples', jsonInit('POST', { reset: false })),

  activatePolicy: (policyId: string, version: string) =>
    request<PolicyRecord>(
      `/api/policies/${encodeURIComponent(policyId)}/versions/${encodeURIComponent(version)}/activate`,
      { method: 'POST' },
    ),

  reindexPolicy: (policyId: string, version: string) =>
    request<PolicyRecord>(
      `/api/policies/${encodeURIComponent(policyId)}/versions/${encodeURIComponent(version)}/reindex`,
      { method: 'POST' },
    ),

  deletePolicy: (policyId: string) =>
    request<unknown>(`/api/policies/${encodeURIComponent(policyId)}`, { method: 'DELETE' }),

  searchPolicies: (query: string, topK = 3) =>
    request<RetrievalResult>('/api/policies/search', jsonInit('POST', { query, top_k: topK })),

  getPassage: (citationId: string) =>
    request<PolicyPassage>(`/api/policies/passages/${encodeURIComponent(citationId)}`),

  // ---- assistant chat
  listConversations: (documentId: string) => request<Conversation[]>(`/api/documents/${documentId}/conversations`),

  createConversation: (documentId: string) =>
    request<Conversation>(`/api/documents/${documentId}/conversations`, jsonInit('POST', {})),

  listMessages: (conversationId: string, opts: { beforeSeq?: number; limit?: number } = {}) => {
    const params = new URLSearchParams({ limit: String(opts.limit ?? 50) })
    if (opts.beforeSeq !== undefined) params.set('before_seq', String(opts.beforeSeq))
    return request<MessagePage>(`/api/conversations/${conversationId}/messages?${params}`)
  },

  /** Stores the message and starts the assistant. Repeating `clientMessageId` returns the original (safe retry). */
  sendMessage: (conversationId: string, content: string, clientMessageId: string) =>
    request<SendResult>(
      `/api/conversations/${conversationId}/messages`,
      jsonInit('POST', { content, client_message_id: clientMessageId }),
    ),

  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),

  cancelRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}/cancel`, { method: 'POST' }),
}
