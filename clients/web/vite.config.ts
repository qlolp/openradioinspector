import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  base: '/modern/',
  server: {
    fs: { allow: [fileURLToPath(new URL('../..', import.meta.url))] },
    proxy: { '/api': { target: 'http://127.0.0.1:18771', changeOrigin: false, ws: true } },
  },
  build: { target: 'es2022', sourcemap: false },
});
