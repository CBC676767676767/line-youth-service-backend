import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.spec.mjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 15_000 },
  reporter: [['list', { printSteps: true }]],
  // Fixture secrets, OTPs and authenticated browser state must never enter artifacts.
  outputDir: join(tmpdir(), 'youth-playwright-results'),
  preserveOutput: 'never',
  use: {
    headless: true,
    viewport: { width: 1440, height: 1000 },
    actionTimeout: 20_000,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    launchOptions: process.env.YOUTH_E2E_BROWSER
      ? { executablePath: process.env.YOUTH_E2E_BROWSER }
      : existsSync(edge) ? { executablePath: edge } : {},
  },
});
