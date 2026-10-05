import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// Сборка без нативных бинарников (.node/.exe): esbuild и rollup подменены на WASM-версии
// через "overrides" в package.json — так проект запускается даже при блокировке
// неподписанных файлов политикой Windows (Smart App Control / App Control).
//
// В разработке /api проксируется на FastAPI (python -m backend.api, порт 8000):
// браузер видит один origin, поэтому httpOnly-cookie сессии работает без CORS.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          // Библиотеки отдельно от кода страниц: лучше кэшируются между релизами
          manualChunks: {
            react: ['react', 'react-dom', 'react-dom/client', 'react-router-dom', '@tanstack/react-query'],
            charts: ['recharts'],
          },
        },
      },
    },
  }
})
