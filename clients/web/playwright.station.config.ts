import { defineConfig } from '@playwright/test';
import shared, { coreFixtureServer } from './playwright.config';

export default defineConfig({
  ...shared,
  testMatch: 'station.spec.ts',
  webServer: [
    coreFixtureServer,
    {
      command: 'npm run preview -- --port 18772 --strictPort',
      url: 'http://127.0.0.1:18772/modern/',
      reuseExistingServer: false,
    },
  ],
});
