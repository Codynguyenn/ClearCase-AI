import fs from 'node:fs'
import path from 'node:path'
import { expect, type Locator, type Page, test } from '@playwright/test'

// One reviewer's session, told in order, in a real browser against the real backend, database and PDF engine.
// The tests share one database (scripts/e2e_backend.sh drops it when the servers start), so run the file as a whole.
// Only the assistant is scripted ("demo mode"): no model is called. The sample contract is synthetic.
const SAMPLE = path.resolve(import.meta.dirname, '../../sample_data/sample_contract.pdf')
const DEMO_NOTE = 'Demo mode: a scripted reply built from the tool results. No AI model was used.'
const COMPANY_SENTENCE = 'Names of companies and other organisations are not personal information'

test.describe.configure({ mode: 'serial' })

// ------------------------------------------------------------------------------------------------ helpers
const openSample = async (page: Page) => {
  await page.goto('/')
  await page.getByRole('navigation', { name: 'Documents' }).getByText('sample_contract.pdf', { exact: true }).click()
  await expect(page.getByRole('heading', { name: 'sample_contract.pdf' })).toBeVisible()
}

const searchFor = async (page: Page, text: string) => {
  await page.getByRole('tab', { name: 'Search' }).click()
  await page.getByPlaceholder('Search all pages (exact text)…').fill(text)
  await page.getByRole('button', { name: 'Search', exact: true }).click()
}

const proposedCount = async (page: Page) => Number((await page.getByText(/^\d+ proposed$/).textContent())!.match(/\d+/)![0])

const conversation = (page: Page) => page.getByRole('log', { name: 'Conversation' })
const assistantMessages = (page: Page) => conversation(page).locator('article[data-role="assistant"]')

/** Send a message and wait for the finished reply (the page polls the run until it completes). */
const sendChat = async (page: Page, text: string) => {
  await expect(page.getByLabel('Message to the assistant')).toBeEnabled() // the box stays disabled until the saved conversation has loaded
  const before = await assistantMessages(page).count()
  await page.getByLabel('Message to the assistant').fill(text)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(assistantMessages(page)).toHaveCount(before + 1, { timeout: 60_000 })
  return assistantMessages(page).last()
}

/** A native modal dialog keeps keyboard focus off the page behind it. After its last control the browser may move focus
 *  out of the document (to its own address bar), so `body` is fine; a control of the page behind the dialog is not.
 *  happy-dom cannot show this; a real browser can. */
const expectFocusStaysOffThePage = async (page: Page) => {
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press('Tab')
    const where = await page.evaluate(() => {
      const el = document.activeElement
      if (!el || el === document.body) return 'outside the document'
      return el.closest('dialog') ? 'inside the dialog' : `PAGE BEHIND: ${el.tagName} ${el.getAttribute('aria-label') ?? el.textContent?.slice(0, 30)}`
    })
    expect(where).not.toContain('PAGE BEHIND')
  }
}

/** Opens a cited passage, checks the dialog behaves, closes it with Escape and expects focus back on the opener. */
const readPassageAndClose = async (page: Page, opener: Locator) => {
  await opener.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText(COMPANY_SENTENCE)
  await expectFocusStaysOffThePage(page)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(opener).toBeFocused()
}

// ------------------------------------------------------------------------------------------------ the session
test('upload, search, detect, and review: the detectors only propose, the reviewer decides', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'ClearCase' })).toBeVisible()
  await expect(page.getByText('0 documents')).toBeVisible() // a fresh database

  // Upload, and wait until the server has extracted the text and the first page is drawn.
  await page.getByTestId('file-input').setInputFiles(SAMPLE)
  await expect(page.getByRole('heading', { name: 'sample_contract.pdf' })).toBeVisible()
  await expect(page.getByText('Ready', { exact: true }).first()).toBeVisible()
  await expect(page.locator('canvas').first()).toBeVisible()

  // Search is literal text over the stored extraction.
  await searchFor(page, 'Northwind')
  await expect(page.getByText(/^[1-9]\d* matches?$/)).toBeVisible()
  await expect(page.getByLabel('Search results').getByRole('listitem').first()).toBeVisible()
  await searchFor(page, 'Delgado-Reyes')
  await expect(page.getByText('5 matches')).toBeVisible() // remembered: the redacted file is searched for this name later

  // Detect & propose. Nothing is approved yet.
  await page.getByRole('tab', { name: 'Review' }).click()
  await expect(page.getByText('0 proposed')).toBeVisible()
  await page.getByRole('button', { name: 'Detect & propose' }).click()
  await expect(page.getByText('0 proposed')).toHaveCount(0)
  const proposed = await proposedCount(page)
  expect(proposed).toBeGreaterThan(10)
  await expect(page.getByText('0 approved')).toBeVisible()

  // Reject the first finding (a "Maria Delgado-Reyes" the reviewer decides to keep), approve the rest.
  const findings = page.getByLabel('Findings', { exact: true }).getByRole('listitem')
  await findings.first().getByRole('button', { name: 'Reject', exact: true }).click()
  await expect(page.getByText('1 rejected')).toBeVisible()
  // With "all statuses" shown, "Approve shown" acts only on findings nobody has decided yet (and says how many), so it
  // cannot turn the rejection above into an approval.
  await page.getByRole('button', { name: `Approve shown (${proposed - 1} undecided)` }).click()
  await expect(page.getByText(`${proposed - 1} approved`)).toBeVisible()
  await expect(page.getByText('1 rejected')).toBeVisible() // the rejected finding was left alone
})

test('policy library: load the samples, ask, open a cited passage, and the dialog behaves like a dialog', async ({ page }) => {
  await openSample(page)
  await page.getByRole('tab', { name: 'Policy' }).click()
  await page.getByRole('button', { name: 'Load the fictional sample policies' }).click()
  // Loading embeds every passage (and the first time loads the embedding model), so wait for both policies to be searchable.
  await expect(page.getByText('searchable', { exact: true })).toHaveCount(2, { timeout: 90_000 })

  await page.getByLabel('Question for the active policies').fill('Can company names stay visible?')
  await page.getByRole('button', { name: 'Ask', exact: true }).click()
  const results = page.getByTestId('policy-results')
  await expect(results).toBeVisible({ timeout: 90_000 })
  await expect(results.getByRole('listitem').first()).toBeVisible() // passages were found (a non-ok answer has no list)

  await readPassageAndClose(page, results.getByRole('button', { name: /^Open policy passage / }).first())
})

test('chat in demo mode: scripted commands, a checked citation, and the assistant cannot export until the reviewer authorizes', async ({ page }) => {
  await openSample(page)
  await page.getByRole('tab', { name: 'Chat' }).click()
  await expect(page.getByLabel('Message to the assistant')).toBeEnabled()

  // The assistant reads the same review state that the first test wrote through the interface.
  const summary = await sendChat(page, 'summary')
  await expect(summary).toContainText('Review status: 17 approved, 1 rejected.')
  await expect(summary).toContainText(DEMO_NOTE)
  await summary.getByText('What it did').click() // tool activity is visible and says which tool ran
  await expect(summary).toContainText('get_review_summary')

  // A policy answer quotes a sentence and cites it. The citation is a checked chip that opens the passage.
  const policy = await sendChat(page, 'policy can company names stay visible?')
  await expect(policy).toContainText(COMPANY_SENTENCE)
  await readPassageAndClose(page, policy.getByRole('button', { name: 'Read policy passage northwind-redaction-policy:1.0:7' }))

  // Nobody has authorized an export, so the assistant refuses and the interface offers the reviewer's hand-off.
  const refused = await sendChat(page, 'export')
  await expect(refused).toContainText('I cannot create the redacted file yet')
  const handoff = page.getByRole('region', { name: 'Export hand-off' })
  await expect(handoff).toBeVisible()
  await handoff.getByRole('button', { name: 'Authorize export' }).click()
  await expect(handoff.getByText('Authorized for 5 minutes.')).toBeVisible()
  const before = await assistantMessages(page).count()
  await handoff.getByRole('button', { name: 'Ask the assistant to export now' }).click()
  await expect(assistantMessages(page)).toHaveCount(before + 1, { timeout: 60_000 })
  await expect(assistantMessages(page).last()).toContainText('Export verified: 17 approved redaction(s)')
  await expect(assistantMessages(page).last()).toContainText('The assistant cannot download or share it.')
})

test('export: the assistant\'s export is verified, the reviewer exports and downloads, and the downloaded file really is redacted', async ({ page }, testInfo) => {
  await openSample(page)
  await page.getByRole('tab', { name: 'Export' }).click()
  const cards = page.getByTestId('export-card')
  await expect(cards).toHaveCount(1) // the one the assistant made in the previous test
  await expect(cards.first()).toHaveAttribute('data-status', 'verified')

  // The reviewer's own export, with its verification checks.
  await page.getByRole('button', { name: 'Export 17 approved redaction(s)' }).click()
  await expect(cards).toHaveCount(2)
  const newest = cards.first()
  await expect(newest).toHaveAttribute('data-status', 'verified')
  await expect(newest.getByLabel('Verification checks').getByRole('listitem').first()).toBeVisible()

  const [download] = await Promise.all([page.waitForEvent('download'), newest.getByRole('link', { name: /^Download / }).click()])
  const redactedPath = testInfo.outputPath('redacted.pdf')
  await download.saveAs(redactedPath)
  expect(fs.readFileSync(redactedPath).subarray(0, 5).toString()).toBe('%PDF-')

  // Asking the assistant to export again returns the existing export: it never makes a second file.
  await page.getByRole('tab', { name: 'Chat' }).click()
  await expect(await sendChat(page, 'export')).toContainText('Export verified: 17 approved redaction(s)')
  await page.getByRole('tab', { name: 'Export' }).click()
  await expect(cards).toHaveCount(2)

  // The strongest check available to a user: open the downloaded file in the product itself and search its text.
  await page.getByTestId('file-input').setInputFiles(redactedPath)
  await expect(page.getByText('2 documents')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'redacted.pdf' })).toBeVisible()
  await searchFor(page, 'Delgado-Reyes')
  await expect(page.getByText('1 match', { exact: true })).toBeVisible() // 5 before; only the finding the reviewer rejected is still readable
  await searchFor(page, 'blueharbor-example.com')
  await expect(page.getByText('0 matches')).toBeVisible() // approved e-mail addresses are gone
  await searchFor(page, '512-44-9087')
  await expect(page.getByText('0 matches')).toBeVisible() // so is the approved SSN
  await searchFor(page, 'Northwind')
  await expect(page.getByText(/^[1-9]\d* matches?$/)).toBeVisible() // company names were not approved, so they stay
})

test('the conversation is still there after reloading the page', async ({ page }) => {
  await openSample(page)
  await page.getByRole('tab', { name: 'Chat' }).click()
  await expect(conversation(page).locator('article[data-role="user"]')).toHaveCount(5) // summary, policy, export, export, export
  await expect(assistantMessages(page)).toHaveCount(5)
  await expect(conversation(page).getByText('summary', { exact: true })).toBeVisible()
  // ...and the citation is still a working chip: the verification result was stored with the message.
  await expect(conversation(page).getByRole('button', { name: /^Read policy passage / })).toBeVisible()
})
