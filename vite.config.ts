import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The built UI lands in ./dist, which make_proxies.py serves (users never need Node).
// `npm run dev` proxies /api to a running `python make_proxies.py` (PROXY_MAKER_PORT, default 8765).
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: { '/api': `http://127.0.0.1:${process.env.PROXY_MAKER_PORT ?? 8765}` },
  },
})
