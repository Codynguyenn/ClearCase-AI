import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// Vitest does not run Testing Library's automatic cleanup unless test globals are on, so do it here:
// unmount whatever a test rendered and restore anything it replaced (fetch, confirm, ...).
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})
