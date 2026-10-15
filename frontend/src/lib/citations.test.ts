import { describe, expect, it } from 'vitest'
import type { Citation } from '../api/types'
import { segmentsWithCitations } from './citations'

const ID = 'northwind-redaction-policy:1.0:3'
const cite = (c: Partial<Citation> & Pick<Citation, 'kind' | 'ref'>): Citation => ({ verified: true, quote: null, ...c })
const kinds = (text: string, citations: Citation[]) => segmentsWithCitations(text, citations).map((s) => (s.type === 'text' ? 'text' : s.verified ? 'ok' : 'unverified'))

describe('segmentsWithCitations', () => {
  it('returns plain text untouched when there are no markers', () => {
    expect(segmentsWithCitations('I found two emails.', [])).toEqual([{ type: 'text', text: 'I found two emails.' }])
  })

  it('turns page and policy markers into citation segments and keeps the text around them', () => {
    const segs = segmentsWithCitations(`On [p2] and per [${ID}] done.`, [cite({ kind: 'page', ref: '2' }), cite({ kind: 'policy', ref: ID })])
    expect(segs.map((s) => (s.type === 'text' ? s.text : s.label))).toEqual(['On ', 'p2', ' and per ', ID, ' done.'])
    expect(kinds(`On [p2] and per [${ID}] done.`, [cite({ kind: 'page', ref: '2' }), cite({ kind: 'policy', ref: ID })])).toEqual(['text', 'ok', 'text', 'ok', 'text'])
  })

  it('marks a citation unverified when the server says so, and when the server did not list it at all', () => {
    expect(kinds('See [p9].', [cite({ kind: 'page', ref: '9', verified: false })])).toEqual(['text', 'unverified', 'text'])
    expect(kinds('See [p9].', [])).toEqual(['text', 'unverified', 'text'])
  })

  it('keeps the quote visible and matches the quoted marker to the quote the server checked', () => {
    const text = `It says "Names must be redacted." [${ID}] and "Names may be shared." [${ID}]`
    const segs = segmentsWithCitations(text, [
      cite({ kind: 'policy', ref: ID, quote: 'Names must be redacted.', verified: true }),
      cite({ kind: 'policy', ref: ID, quote: 'Names may be shared.', verified: false }),
    ])
    expect(segs.filter((s) => s.type === 'text').map((s) => (s as { text: string }).text).join('')).toContain('"Names must be redacted."')
    const marks = segs.filter((s) => s.type === 'citation').map((s) => (s.type === 'citation' ? [s.quote, s.verified] : []))
    expect(marks).toEqual([['Names must be redacted.', true], ['Names may be shared.', false]])
  })

  it('does not treat other bracketed text or malformed markers as citations', () => {
    expect(segmentsWithCitations('[p0] [P3] [page 3] [a:1.0:0] [see above]', [])).toEqual([{ type: 'text', text: '[p0] [P3] [page 3] [a:1.0:0] [see above]' }])
  })

  it('never upgrades a bare marker because a quoted one of the same passage was verified', () => {
    const segs = segmentsWithCitations(`Rule [${ID}].`, [cite({ kind: 'policy', ref: ID, quote: 'Some quote here.', verified: true })])
    expect(segs.find((s) => s.type === 'citation')).toMatchObject({ verified: false })
  })
})
