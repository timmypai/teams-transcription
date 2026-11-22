import { test, expect, type Locator } from '@playwright/test';
import { calendarOccurrences, calendarSettings, issueBinding, login, OperationError } from './operations';
import { readFileSync, mkdirSync } from 'node:fs';
import { runOperation } from './ops/run';
process.env.ADMIN_PASSWORD = readFileSync(process.env.DEVFLOW_FIXTURE_PASSWORD_FILE || '/tmp/devflow-fixture-password', 'utf8').trim();

// Disposable service only. These are NOT the manual AC-4 Teams/audio acceptance.
test.use({ baseURL: 'http://127.0.0.1:18081' });

async function bindingArgs(row: Locator) {
  return {
    occurrence_id: await row.getAttribute('data-occurrence-id'),
    revision: Number(await row.locator('form[action="/actions/target-consent"] input[name="revision"]').inputValue()),
    start_at: await row.locator('time[data-edge="start"]').getAttribute('datetime'),
    end_at: await row.locator('time[data-edge="end"]').getAttribute('datetime'),
    present: true, media: true,
  };
}
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
    expect(Object.keys(result.occurrences[0]).sort()).toEqual(['end_at', 'joinable', 'occurrence_id', 'revision', 'start_at']);
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
    const args = await bindingArgs(page.locator('tr[data-occurrence-id]').filter({
      has: page.locator('td:first-child').filter({ hasText: /^PRIVATE-current/ }),
    }));
    const writes: string[] = [];
    page.on('request', request => { if (request.method() === 'POST') writes.push(new URL(request.url()).pathname); });
    for (const invalid of [{...args, revision: String(args.revision)}, {...args, media: 'true'}]) {
      await expect(issueBinding(page, invalid)).rejects.toMatchObject({code:'invalid_binding_args'});
    }
    for (const changed of [{...args, revision: args.revision+1}, {...args, end_at: args.start_at},
      {...args, end_at: '2099-01-01T01:00:00Z'}]) {
      await expect(issueBinding(page, changed)).rejects.toBeInstanceOf(OperationError);
    }
    expect(writes).toEqual([]);
    const binding = await issueBinding(page, args);
    expect(writes).toEqual(['/actions/target-consent']);
    expect(binding.occurrence_id).toBe(args.occurrence_id);
    expect(binding.revision).toBe(2);
    expect(binding.request_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(binding.expires_at-binding.issued_at).toBeCloseTo(300, 2);
    expect(binding.expires_in_seconds).toBe(300);
    expect(JSON.stringify(binding)).not.toMatch(/PRIVATE|microsoft|password|csrf/i);
    await expect(issueBinding(page, args)).rejects.toMatchObject({code:'binding_selection_changed'});
  });
  await test.step('Then 過期與尚未進入入會時段的場次被拒絕並保留畫面原因', async () => {
    for (const name of ['PRIVATE-expired', 'PRIVATE-later']) {
      await page.goto('/schedule');
      const args = await bindingArgs(page.locator('tr[data-occurrence-id]').filter({
        has: page.locator('td:first-child').filter({ hasText: new RegExp(`^${name}`) }),
      }));
      await expect(issueBinding(page, args)).rejects.toMatchObject({
        code: 'occurrence_outside_join_window', reason: expect.stringContaining('場次不在可入會時段'),
      });
    }
  });
});

test('Calendar settings reads before exact form write, preserves grants and does not poll or OAuth', async ({page}) => {
  await login(page);
  await page.goto('/settings');
  const requests: string[] = [];
  page.on('request', request => requests.push(request.method()+' '+new URL(request.url()).pathname));
  const before = await calendarSettings(page, {});
  expect(before.saved).toBe(false);
  expect(before.scheduler_paused).toBe(true);
  const original = before.calendars[0];
  expect(original.calendar_ref).toMatch(/^[a-f0-9]{32}$/);
  expect(JSON.stringify(before)).not.toMatch(/PRIVATE|synthetic|password|csrf|@|https?:/);
  const after = await calendarSettings(page, {calendar_ref:original.calendar_ref,revision:original.revision,enabled:!original.enabled});
  expect(after.saved).toBe(true);
  expect(after.calendars[0]).toEqual({...original,revision:original.revision+1,enabled:!original.enabled});
  await expect(calendarSettings(page, {calendar_ref:original.calendar_ref,revision:original.revision,enabled:true})).rejects.toMatchObject({code:'calendar_revision_changed'});
  await expect(calendarSettings(page, {calendar_ref:'PRIVATE@example.com',revision:0,enabled:true})).rejects.toMatchObject({code:'invalid_calendar_settings_args'});
  await expect(calendarSettings(page, {calendar_ref:'primary',revision:0,enabled:true,recording_authorized:true})).rejects.toMatchObject({code:'invalid_calendar_settings_args'});
  // Restore only the originally observed read bit via the same reviewed form.
  await calendarSettings(page, {calendar_ref:original.calendar_ref,revision:original.revision+1,enabled:original.enabled});
  expect(requests.every(row => ['GET /settings','GET /assets/calendar.css','POST /actions/calendar-readonly'].includes(row))).toBe(true);
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

test('production settings runner creates primary read-only and exports only safe JSON/heading PNG', async ({page}) => {
  await login(page);
  const evidence = process.env.DEVFLOW_UI_EVIDENCE || '/tmp/devflow-evidence';
  mkdirSync(evidence,{recursive:true});
  const resultPath = evidence+'/calendar-settings-result.json';
  const previousOrigin = process.env.BASE_URL, previousResult = process.env.DEVFLOW_OP_RESULT;
  process.env.BASE_URL = 'http://127.0.0.1:18081';
  process.env.DEVFLOW_OP_RESULT = resultPath;
  try {
    await expect(runOperation(page,'calendar-settings',() => calendarSettings(page,{calendar_ref:'primary'})))
      .rejects.toThrow('invalid_calendar_settings_args');
    expect(JSON.parse(readFileSync(resultPath,'utf8'))).toMatchObject({ok:false,code:'invalid_calendar_settings_args'});
    await runOperation(page,'calendar-settings',() => calendarSettings(page,{calendar_ref:'primary',revision:0,enabled:true}));
    const result = JSON.parse(readFileSync(resultPath,'utf8'));
    const primary = result.calendars.find(row => row.primary);
    expect(result).toMatchObject({ok:true,saved:true,scheduler_paused:true});
    expect(primary).toMatchObject({revision:1,enabled:true,recording_authorized:false,speaking_authorized:false});
    expect(JSON.stringify(result)).not.toMatch(/PRIVATE|synthetic|password|csrf|@|https?:/);
    expect(readFileSync(evidence+'/calendar-settings.png').subarray(0,8).toString('hex')).toBe('89504e470d0a1a0a');
  } finally {
    if (previousOrigin === undefined) delete process.env.BASE_URL; else process.env.BASE_URL = previousOrigin;
    if (previousResult === undefined) delete process.env.DEVFLOW_OP_RESULT; else process.env.DEVFLOW_OP_RESULT = previousResult;
  }
});
