import { defineConfig } from 'vite';

export default defineConfig({
  base: '/modern/',
  server: { proxy: { '/api': { target: 'http://127.0.0.1:18771', changeOrigin: false } } },
  build: { target: 'es2022', sourcemap: false },
});
