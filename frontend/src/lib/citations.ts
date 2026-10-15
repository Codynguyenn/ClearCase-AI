import type { Citation } from '../api/types'

/** A piece of an assistant answer: plain text, or a citation marker found in it. */
export type Segment =
  | { type: 'text'; text: string }
  | { type: 'citation'; kind: 'page' | 'policy'; ref: string; label: string; verified: boolean; quote: string | null }

// The same shapes the backend parses (backend/app/agent/citations.py): `[p3]`, `[policy-id:1.0:3]`, and a quoted
// sentence directly before a policy marker. Anything else in square brackets is ordinary text.
const POLICY_ID = '[a-z0-9][a-z0-9-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,19}:[1-9][0-9]{0,5}'
const MARKER = new RegExp(
  `(?<open>["“])(?<quote>[^"“”\\n]{3,2000})(?<close>["”])\\s*\\[(?<qref>${POLICY_ID})\\]` +
    `|\\[p(?<page>[1-9][0-9]{0,4})\\]` +
    `|\\[(?<ref>${POLICY_ID})\\]`,
  'g',
)

/**
 * Split an answer into text and citation segments. A marker shows as verified only if the server's own check
 * (the `citations` list stored with the message) says so. A marker the server did not list, for example in an
 * older message, is shown as unverified: the interface never upgrades a claim on its own.
 */
export function segmentsWithCitations(text: string, citations: Citation[]): Segment[] {
  const segments: Segment[] = []
  // Exact match on kind, ref and quote: a quoted use and a bare use of the same passage are checked separately.
  const lookup = (kind: 'page' | 'policy', ref: string, quote: string | null) =>
    citations.find((c) => c.kind === kind && c.ref === ref && c.quote === quote)
  let last = 0
  for (const match of text.matchAll(MARKER)) {
    const index = match.index ?? 0
    const g = match.groups ?? {}
    if (index > last) segments.push({ type: 'text', text: text.slice(last, index) })
    if (g.qref !== undefined) {
      segments.push({ type: 'text', text: `${g.open}${g.quote}${g.close} ` })
      const found = lookup('policy', g.qref, g.quote ?? null)
      segments.push({ type: 'citation', kind: 'policy', ref: g.qref, label: g.qref, verified: found?.verified ?? false, quote: g.quote ?? null })
    } else if (g.page !== undefined) {
      const found = lookup('page', g.page, null)
      segments.push({ type: 'citation', kind: 'page', ref: g.page, label: `p${g.page}`, verified: found?.verified ?? false, quote: null })
    } else if (g.ref !== undefined) {
      const found = lookup('policy', g.ref, null)
      segments.push({ type: 'citation', kind: 'policy', ref: g.ref, label: g.ref, verified: found?.verified ?? false, quote: null })
    }
    last = index + match[0].length
  }
  if (last < text.length) segments.push({ type: 'text', text: text.slice(last) })
  return segments
}
