import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The frontend never talks to MongoDB or holds secrets. Every `/api` call is
// proxied to the FastAPI backend so the browser sees a single origin in dev.
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    // A simulated browser (happy-dom) so React components can be rendered in tests. It implements the
    // <dialog> element (showModal/close/close event), which the citation reader relies on.
    environment: 'happy-dom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
