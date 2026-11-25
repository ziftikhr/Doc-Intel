/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// The browser talks to this dev server only; /api and /health are forwarded to the
// FastAPI backend, so no CORS setup is needed and the backend URL lives in one place.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = env.BACKEND_URL || 'http://127.0.0.1:8000'
  const proxy = {
    '/api': { target: backend, changeOrigin: true },
    '/health': { target: backend, changeOrigin: true },
  }
  return {
    plugins: [react(), tailwindcss()],
    server: { port: 5173, proxy },
    preview: { port: 4173, proxy },
    test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], restoreMocks: true },
  }
})
