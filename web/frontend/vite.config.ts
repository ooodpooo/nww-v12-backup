import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
      },
      '/auth': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
      },
      '/publisher-api': {
        target: 'http://127.0.0.1:8012',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/publisher-api/, ''),
      },
    },
  },
})
