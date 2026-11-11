import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: 'http://127.0.0.1:18772/modern/',
    viewport: { width: 1440, height: 1000 },
    launchOptions: {
      executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
      args: ['--no-sandbox'],
    },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: `${process.env.ORI_PYTHON || 'python3'} tests/core_fixture.py`,
      url: 'http://127.0.0.1:18771/api/v1/health',
      reuseExistingServer: false,
      timeout: 30000,
    },
    {
      command: 'npm run dev -- --port 18772 --strictPort',
      url: 'http://127.0.0.1:18772/modern/',
      reuseExistingServer: false,
    },
  ],
});
