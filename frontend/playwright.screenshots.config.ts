import { defineConfig, devices } from '@playwright/test'
import base from './playwright.config.ts'

// Makes the README screenshots from the real app (synthetic sample contract, scripted demo assistant, fresh database).
//   npm run screenshots              (E2E_CHANNEL=chrome npm run screenshots to use the installed Chrome)
// Same servers and safety settings as the end-to-end test; only the folder of specs and the window size differ.
export default defineConfig({
  ...base,
  testDir: './e2e-screenshots',
  outputDir: 'e2e-results/screenshots',
  retries: 0,
  projects: [
    {
      name: 'screenshots',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, channel: process.env.E2E_CHANNEL || undefined },
    },
  ],
})
