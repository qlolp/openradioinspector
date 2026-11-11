import { defineConfig } from '@playwright/test';
import config from './playwright.config';
export default defineConfig({ ...config, testMatch: '**/*.probe.ts', timeout: 45000 });
