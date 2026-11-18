import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.', testMatch: 'login.spec.ts', workers: 1, retries: 0, timeout: 30000,
  reporter: [['json', { outputFile: '/tmp/devflow-evidence/playwright-report.json' }]],
  outputDir: '/tmp/playwright-output',
  use: {
    baseURL: 'http://127.0.0.1:18080',
    headless: true,
    launchOptions: { executablePath: '/usr/bin/chromium', chromiumSandbox: true,
      args: ['--disable-dev-shm-usage'] },
    trace: 'off', video: 'off', screenshot: 'off',
  },
});
