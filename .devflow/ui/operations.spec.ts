import { test, expect } from '@playwright/test';
import { calendarOccurrences, issueBinding, login, OperationError } from './operations';
import { readFileSync } from 'node:fs';
process.env.ADMIN_PASSWORD = readFileSync(process.env.DEVFLOW_FIXTURE_PASSWORD_FILE || '/tmp/devflow-fixture-password', 'utf8').trim();

// Disposable service only. These are NOT the manual AC-4 Teams/audio acceptance.
test.use({ baseURL: 'http://127.0.0.1:18081' });
test('calendar-occurrences: Given anonymous and seeded calendar, When login and sync, Then future and current pending metadata with read evidence', async ({ page }) => {
  await test.step('Given 未登入且一次性日曆尚未同步', async () => {
    await page.goto('/login');
    await expect(page.getByRole('heading', { name: '管理登入' })).toBeVisible();
  });
  let result;
  await test.step('When 執行相同登入與日曆同步操作', async () => {
    await login(page);
    result = await calendarOccurrences(page);
  });
  await test.step('Then 回傳未來 pending 場次、時區及起訖，沒有私有內容', async () => {
    expect(result.timezone).toBe('Asia/Taipei');
    expect(result.ready).toBe(true);
    expect(result.read).toEqual({attempted:1,succeeded:1,failed:0,event_count:5,completed:true});
    expect(result.occurrences.map(row => row.joinable).sort()).toEqual([false,true,true]);
    expect(result.occurrences).toHaveLength(3);
    expect(Object.keys(result.occurrences[0]).sort()).toEqual(['end_at', 'joinable', 'occurrence_id', 'start_at']);
    expect(result.occurrences[0].occurrence_id).toMatch(/^[a-f0-9]{32}$/);
    expect(Date.parse(result.occurrences[0].end_at)-Date.parse(result.occurrences[0].start_at)).toBe(3600000);
    expect(JSON.stringify(result)).not.toMatch(/PRIVATE|microsoft|password|csrf/i);
  });
});

test('issue-binding: Given synced calendar, When issue in window, Then binding expires in 300s and expired/future are refused', async ({ page }) => {
  await test.step('Given 已登入且日曆同步成功', async () => {
    await login(page);
    await calendarOccurrences(page);
    await expect(page.getByRole('heading', { name: '排程與人工入會' })).toBeVisible();
  });
  await test.step('When 同一操作簽發有效場次 binding', async () => {
    const id = await page.locator('tr[data-occurrence-id]').filter({
      has: page.locator('td:first-child').filter({ hasText: /^PRIVATE-current/ }),
    }).getAttribute('data-occurrence-id');
    const binding = await issueBinding(page, { occurrence_id: id, present: true, media: true });
    expect(binding.occurrence_id).toBe(id);
    expect(binding.revision).toBe(2);
    expect(binding.request_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(binding.expires_at-binding.issued_at).toBeCloseTo(300, 2);
    expect(binding.expires_in_seconds).toBe(300);
    expect(JSON.stringify(binding)).not.toMatch(/PRIVATE|microsoft|password|csrf/i);
  });
  await test.step('Then 過期與尚未進入入會時段的場次被拒絕並保留畫面原因', async () => {
    for (const name of ['PRIVATE-expired', 'PRIVATE-later']) {
      await page.goto('/schedule');
      const id = await page.locator('tr[data-occurrence-id]').filter({
        has: page.locator('td:first-child').filter({ hasText: new RegExp(`^${name}`) }),
      }).getAttribute('data-occurrence-id');
      await expect(issueBinding(page, { occurrence_id: id, present: true, media: true })).rejects.toMatchObject({
        code: 'occurrence_outside_join_window', reason: expect.stringContaining('場次不在可入會時段'),
      });
    }
  });
});

test('operations refuse missing credentials and unconfirmed consent without a write', async ({ page }) => {
  const password = process.env.ADMIN_PASSWORD;
  delete process.env.ADMIN_PASSWORD;
  try { await expect(login(page)).rejects.toMatchObject({ code: 'missing_ADMIN_PASSWORD' }); }
  finally { process.env.ADMIN_PASSWORD = password; }
  await expect(issueBinding(page, { occurrence_id: 'a'.repeat(32) })).rejects.toBeInstanceOf(OperationError);
});

for (const [mode, ready, attempted, succeeded] of [
  ['zero',false,0,0], ['unconfigured',false,1,0], ['unauthorized',false,1,0],
  ['failed',false,1,0], ['empty',true,1,1],
] as const) {
  test(`calendar readback ${mode}: UI success is separate from readiness`, async ({page}) => {
    await page.request.post(`/fixture/${mode}`);
    try {
      await login(page);
      const result = await calendarOccurrences(page);
      expect(result.ready).toBe(ready);
      expect(result.enabled_calendar_count).toBe(mode === 'zero' ? 0 : 1);
      expect(result.read).toEqual({attempted,succeeded,failed:attempted-succeeded,event_count:0,completed:true});
      expect(result.occurrences).toHaveLength(mode === 'empty' ? 3 : 0);
      // An empty external response does not delete previously cached local rows.
      expect(result.read.event_count).toBe(0);
      if (mode === 'unconfigured') expect(result.google.configured).toBe(false);
      if (mode === 'unauthorized') expect(result.google.authorized).toBe(false);
    } finally { await page.request.post('/fixture/normal'); }
  });
}

test('calendar refuses absent, malformed, stale or inconsistent evidence and unsafe occurrence fields', async ({page}) => {
  await login(page);
  for (const mutation of ['missing','unknown-code','missing-field','false-ready','stale','bad-id','bad-time']) {
    await page.route('**/schedule', async route => {
      const response = await route.fetch();
      let body = await response.text();
      if (mutation === 'missing') body = body.replace('id="calendar-readback"', 'id="removed"');
      else if (mutation === 'bad-id') body = body.replace(/data-occurrence-id="[^"]+"/, 'data-occurrence-id="PRIVATE"');
      else if (mutation === 'bad-time') body = body.replace(/datetime="[^"]+"/, 'datetime="PRIVATE"');
      else {
        body = body.replace(/data-readback="([^"]+)"/, (_, encoded) => {
          const data = JSON.parse(encoded.replace(/&quot;/g, '"'));
          if (mutation === 'unknown-code') data.google.error_code = 'PRIVATE';
          if (mutation === 'missing-field') delete data.google.authorized;
          if (mutation === 'false-ready') data.read.succeeded = 0;
          if (mutation === 'stale') data.poll_id = 'a'.repeat(32);
          return 'data-readback="'+JSON.stringify(data).replace(/"/g, '&quot;')+'"';
        });
      }
      await route.fulfill({response,body});
    });
    await expect(calendarOccurrences(page)).rejects.toBeInstanceOf(OperationError);
    await page.unroute('**/schedule');
  }
});
