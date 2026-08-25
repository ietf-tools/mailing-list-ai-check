import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The build output goes to frontend/dist, which Flask serves in production
// (see src/mailing_list_ai_check/webapp/__init__.py). In dev the Vite server runs
// on 5173 and proxies /api to the Flask dev server so no CORS is needed.
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // Bind every interface, not Vite's default `localhost`. In a container
    // `localhost` resolves to ::1 alone, and a devcontainer port forward dials
    // 127.0.0.1, so the forwarded port refuses every connection.
    host: '0.0.0.0',
    // Honor an assigned PORT (e.g. from the dev-tool launcher) so multiple
    // dev servers can coexist; fall back to the usual 5173.
    port: Number(process.env.PORT) || 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8050',
        changeOrigin: true,
      },
    },
  },
})
