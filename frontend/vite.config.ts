import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // three, That Open and web-ifc make one large bundle; fine for a local demo.
  build: { chunkSizeWarningLimit: 8000 },
  server: {
    proxy: {
      // The backend streams chat events as SSE; the proxy passes chunks through unbuffered.
      '/api': {
        target: process.env.COBBIE_BACKEND_URL ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
