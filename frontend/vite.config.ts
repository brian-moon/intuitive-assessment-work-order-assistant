import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the UI runs on :5180 and proxies /api to the FastAPI server; in production FastAPI serves dist/.
export default defineConfig({
  plugins: [react()],
  server: { port: 5180, proxy: { '/api': 'http://127.0.0.1:8790', '/health': 'http://127.0.0.1:8790' } },
})
