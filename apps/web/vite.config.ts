import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({mode}) => ({
  define: { 'import.meta.env.VITE_MOCK_API': JSON.stringify(mode === 'mock' ? 'true' : 'false') },
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: { '/api': { target: process.env.API_PROXY_TARGET || 'http://127.0.0.1:8767', changeOrigin: true, configure(proxy) { proxy.on('proxyReq', request => request.removeHeader('origin')); } } } },
  build: { sourcemap: false, assetsDir: 'th-assets', rollupOptions: { output: { manualChunks: { ui: ['antd'], react: ['react', 'react-dom', 'react-router-dom'] } } } },
}));
