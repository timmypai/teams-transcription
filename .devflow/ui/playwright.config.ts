import { defineConfig } from '@playwright/test';
const operating = Boolean(process.env.DEVFLOW_OP);
const engineering = process.env.DEVFLOW_UI_SUITE === 'operations';
const evidence = process.env.DEVFLOW_UI_EVIDENCE || (operating ? '/evidence' : '/tmp/devflow-evidence');
export default defineConfig({
  testDir: '.', testMatch: operating ? `ops/${process.env.DEVFLOW_OP}.spec.ts` :
    (engineering ? ['operations.spec.ts'] : ['login.spec.ts']),
  testIgnore: operating ? [] : ['ops/**'], workers: 1, retries: 0, timeout: 30000,
  reporter: [['json', { outputFile: `${evidence}/${engineering ? 'operations-report' : 'playwright-report'}.json` }]],
  outputDir: process.env.DEVFLOW_UI_OUTPUT || '/tmp/playwright-output',
  webServer: operating || !engineering ? undefined : {
    command: `${process.env.DEVFLOW_PYTHON || 'python'} fixture.py`,
    url: 'http://127.0.0.1:18081/login', reuseExistingServer: false,
    stdout: 'ignore', stderr: 'pipe', timeout: 20000,
  },
  use: {
    baseURL: operating ? process.env.BASE_URL : 'http://127.0.0.1:18080',
    headless: true,
    launchOptions: { executablePath: process.env.DEVFLOW_CHROMIUM || (operating ? undefined : '/usr/bin/chromium'), chromiumSandbox: true,
      args: ['--disable-dev-shm-usage'] },
    trace: 'off', video: 'off', screenshot: 'off',
  },
});
