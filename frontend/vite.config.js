/**
 * VoiceBridge 前端开发服务器（前后端分离）
 * - 前端端口与主服务一致（默认 8002），内网穿透只暴露此端口即可
 * - /api、/ws 代理到后端 8011
 */
import { defineConfig } from 'vite'
import { join } from 'path'
import { fileURLToPath } from 'url'
import fs from 'fs'

const __dirname = fileURLToPath(new URL('.', import.meta.url))

const FRONTEND_PORT = 8002
const BACKEND_URL = 'http://127.0.0.1:8011'

// 路径与 HTML 的映射（与原先 FastAPI 路由一致）
const ROUTE_TO_HTML = {
  '/': 'login.html',
  '/login': 'login.html',
  '/interview': 'interview.html',
  '/desktop-interview': 'desktop-interview.html',
  '/mobile-interview': 'mobile-interview.html',
  '/mobile-debug': 'mobile-debug.html',
  '/mobile-layout-debug': 'mobile-layout-debug.html',
}

export default defineConfig({
  root: __dirname,
  server: {
    port: FRONTEND_PORT,
    host: '0.0.0.0',
    strictPort: true,
    cors: true,
    proxy: {
      '/api': {
        target: BACKEND_URL,
        changeOrigin: true,
        secure: false,
        ws: false,
        configure(proxy) {
          proxy.on('error', (err) => console.error('[Vite代理] API 错误:', err))
          proxy.on('proxyReq', (req) => {
            console.log('[Vite代理] API 请求:', req.method, req.path)
          })
        },
      },
      '/ws': {
        target: BACKEND_URL.replace('http://', 'ws://').replace('https://', 'wss://'),
        ws: true,
        changeOrigin: true,
        secure: false,
        configure(proxy) {
          proxy.on('error', (err) => console.error('[Vite代理] WebSocket 错误:', err))
          proxy.on('upgrade', (req) => {
            console.log('[Vite代理] WebSocket 升级:', req.url)
          })
        },
      },
      '/health': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      '/docs': { target: BACKEND_URL, changeOrigin: true },
      '/redoc': { target: BACKEND_URL, changeOrigin: true },
      '/openapi.json': { target: BACKEND_URL, changeOrigin: true },
    },
  },
  plugins: [
    {
      name: 'voicebridge-html-routes',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          const pathname = req.url?.split('?')[0] || ''
          // 将 /static/xxx 映射为根路径下的 xxx，以便 /static/js/api.js -> js/api.js
          if (pathname.startsWith('/static/')) {
            req.url = pathname.slice(7) + (req.url?.includes('?') ? '?' + req.url.split('?')[1] : '')
            return next()
          }
          const htmlFile = ROUTE_TO_HTML[pathname]
          if (htmlFile) {
            const filePath = join(__dirname, htmlFile)
            if (fs.existsSync(filePath)) {
              req.url = '/' + htmlFile + (req.url?.includes('?') ? '?' + req.url.split('?')[1] : '')
            }
          }
          next()
        })
      },
    },
  ],
})
