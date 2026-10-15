import { defineConfig, devices } from '@playwright/test'

// Browser end-to-end tests: a real Chromium drives the real frontend against the real backend and a real MongoDB.
// Only the assistant is scripted (demo mode), so nothing here can call a paid model. See scripts/e2e_backend.sh.
//
//   npm run e2e                      (needs MongoDB on 127.0.0.1:27017 and the backend virtualenv)
//   E2E_CHANNEL=chrome npm run e2e   (use the installed Google Chrome instead of Playwright's own Chromium)
//   E2E_REUSE=1 npm run e2e          (reuse servers already running on these ports; for quick iteration only)
const API_PORT = Number(process.env.E2E_API_PORT ?? 8011)
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5183)
const reuse = Boolean(process.env.E2E_REUSE)

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  // One flow, one browser, in order: the steps share a document and a database.
  workers: 1,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never', outputFolder: 'e2e-report' }]] : [['list']],
  outputDir: 'e2e-results',
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    acceptDownloads: true,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], channel: process.env.E2E_CHANNEL || undefined } }],
  webServer: [
    {
      command: '../scripts/e2e_backend.sh',
      env: { E2E_API_PORT: String(API_PORT) },
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: reuse,
      timeout: 120_000,
    },
    {
      command: `npx vite --port ${WEB_PORT}`,
      env: { VITE_API_PROXY_TARGET: `http://127.0.0.1:${API_PORT}` },
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: reuse,
      timeout: 60_000,
    },
  ],
})
