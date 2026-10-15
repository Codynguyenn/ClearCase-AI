import path from 'node:path'
import { expect, type Page, test } from '@playwright/test'

// Writes docs/images/*.png for the README. Everything shown is the synthetic sample contract and the two fictional policies.
const SAMPLE = path.resolve(import.meta.dirname, '../../sample_data/sample_contract.pdf')
const IMAGES = path.resolve(import.meta.dirname, '../../docs/images')
const shot = (page: Page, name: string) => page.screenshot({ path: path.join(IMAGES, name) })

const sendChat = async (page: Page, text: string) => {
  const replies = page.getByRole('log', { name: 'Conversation' }).locator('article[data-role="assistant"]')
  await expect(page.getByLabel('Message to the assistant')).toBeEnabled()
  const before = await replies.count()
  await page.getByLabel('Message to the assistant').fill(text)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(replies).toHaveCount(before + 1, { timeout: 60_000 })
  return replies.last()
}

test('README screenshots', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('file-input').setInputFiles(SAMPLE)
  await expect(page.getByText('Ready', { exact: true }).first()).toBeVisible()
  await expect(page.locator('canvas').first()).toBeVisible()

  // 1. The detectors only propose.
  await page.getByRole('button', { name: 'Detect & propose' }).click()
  await expect(page.getByText('0 proposed')).toHaveCount(0)
  await expect(page.getByLabel('Findings', { exact: true }).getByRole('button', { name: 'Approve', exact: true }).first()).toBeVisible()
  await shot(page, '01-detect-and-propose.png')

  // 2. The reviewer decides: reject one, approve the rest.
  const findings = page.getByLabel('Findings', { exact: true }).getByRole('listitem')
  await findings.first().getByRole('button', { name: 'Reject', exact: true }).click()
  await page.getByRole('button', { name: /^Approve shown/ }).click() // acts only on the undecided ones, so the rejection stays
  await expect(page.getByText('0 proposed')).toBeVisible()
  await shot(page, '02-review-and-approve.png')

  // 3. The chat assistant (scripted demo mode) quotes a policy sentence with a checked citation.
  await page.getByRole('tab', { name: 'Policy' }).click()
  await page.getByRole('button', { name: 'Load the fictional sample policies' }).click()
  await expect(page.getByText('searchable', { exact: true })).toHaveCount(2, { timeout: 90_000 })
  await page.getByRole('tab', { name: 'Chat' }).click()
  const answer = await sendChat(page, 'policy can company names stay visible?')
  const chip = answer.getByRole('button', { name: /^Read policy passage / })
  await expect(chip).toBeVisible()
  await shot(page, '03-chat-with-citation.png')

  // 4. The citation opens the real passage.
  await chip.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await shot(page, '04-citation-passage.png')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toBeHidden()

  // 5. Export, then the verification checks.
  await page.getByRole('tab', { name: 'Export' }).click()
  await page.getByRole('button', { name: /^Export \d+ approved redaction/ }).click()
  await expect(page.getByTestId('export-card').first()).toHaveAttribute('data-status', 'verified')
  await shot(page, '05-export-verified.png')
})
