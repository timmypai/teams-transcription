import type { Page } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { OperationError } from '../operations';

export async function runOperation(page: Page, name: string, action: () => Promise<unknown>) {
  const target = process.env.DEVFLOW_OP_RESULT;
  if (!target) throw new Error('missing_DEVFLOW_OP_RESULT');
  const evidence = dirname(target);
  const origin = new URL(process.env.BASE_URL!).origin;
  await page.context().route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  const deadline = setTimeout(() => page.close(), 20000);
  try {
    const result = await action();
    // Only visible safe metadata, never full DOM, inputs, titles or binding.
    const section = page.locator('section[data-timezone]');
    if (await section.count()) {
      await section.screenshot({ path: join(evidence, `${name}.png`), timeout: 5000,
        mask: [section.locator('td:first-child'), section.locator('td:nth-child(4)')] });
    } else {
      await page.locator('h1').screenshot({ path: join(evidence, `${name}.png`), timeout: 5000 });
    }
    writeFileSync(target, JSON.stringify({ ok: true, ...result as object }), { mode: 0o600 });
  } catch (error) {
    const failure = error instanceof OperationError ? { code: error.code, reason: error.reason } : { code: 'operation_failed' };
    writeFileSync(target, JSON.stringify({ ok: false, ...failure }), { mode: 0o600 });
    // Raw Playwright exceptions can contain DOM, locator values or credentials.
    throw new Error(failure.code);
  } finally {
    clearTimeout(deadline);
  }
}
