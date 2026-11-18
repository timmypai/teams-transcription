import { expect, test } from '@playwright/test';
import { scenario } from './ac';
import * as fs from 'node:fs';
const specs = JSON.parse(fs.readFileSync('/tmp/devflow-ui/scenarios.json', 'utf8'));
const password = fs.readFileSync('/tmp/devflow-fixture-password', 'utf8').trim();
// Each test receives a new anonymous context. No account, real data, trace or HAR.
test.beforeEach(async ({ context }) => {
  await context.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:18080/') ? route.continue() : route.abort());
});
for (const n of [1, 2, 3]) {
  const id = `t_a3f4f673/AC-3.S${n}`;
  const s = specs[id];
  scenario(id, s.description, {
    kind: 'ui', smoke: s.smoke,
    given: { title: s.given, check: async ({ page, request }) => {
      expect((await request.get('/api/status')).status()).toBe(401);
      await page.goto('/login');
      await expect(page.getByRole('heading', { name: '管理登入' })).toBeVisible();
    } },
    when: { title: s.when, act: async ({ page }) => {
      if (n === 3) { await page.goto('/login'); return; }
      await page.locator('input[name=password]').fill(n === 1 ? password : 'SYNTHETIC-WRONG-NOT-A-GRANT');
      await page.getByRole('button', { name: '登入', exact: true }).click();
      // Screenshots never retain even synthetic passwords.
      if (await page.locator('input[name=password]').count()) await page.locator('input[name=password]').fill('');
    } },
    then: { title: s.then, check: async ({ page, request }) => {
      if (n === 1) {
        await expect(page.getByRole('heading', { name: '行事曆', exact: true })).toBeVisible();
        await expect(page.getByRole('region', { name: '週行事曆', exact: true })).toBeVisible();
        await page.goto('/settings');
        await expect(page.getByRole('heading', { name: '設定', exact: true })).toBeVisible();
      } else {
        if (n === 2) {
          await expect(page.getByText('密碼不正確，請重新登入。', { exact: true })).toBeVisible();
          expect((await request.get('/api/status')).status()).toBe(401);
          await page.goto('/');
        }
        await expect(page.getByRole('heading', { name: '管理登入' })).toBeVisible();
        await expect(page.locator('input[name=password]')).toBeVisible();
        await expect(page.getByRole('button', { name: '登入', exact: true })).toBeVisible();
        expect((await request.get('/api/status')).status()).toBe(401);
        await expect(page.locator('#workspace')).toHaveCount(0);
      }
    } },
  });
}
