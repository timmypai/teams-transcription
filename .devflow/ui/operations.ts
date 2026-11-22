import type { Page } from '@playwright/test';

export class OperationError extends Error {
  constructor(public code: string, public reason = code) { super(reason); }
}
function requireValue(ok: unknown, code: string): asserts ok {
  if (!ok) throw new OperationError(code);
}
export async function login(page: Page) {
  const password = process.env.ADMIN_PASSWORD;
  requireValue(password, 'missing_ADMIN_PASSWORD');
  await page.goto('/login');
  requireValue(await page.getByRole('heading', { name: '管理登入', exact: true }).isVisible(), 'login_page_required');
  await page.getByLabel('管理密碼').fill(password);
  await page.getByRole('button', { name: '登入', exact: true }).click();
  if (await page.locator('input[name=password]').count()) {
    await page.locator('input[name=password]').fill('');
    throw new OperationError('admin_login_failed');
  }
  requireValue(await page.getByRole('heading', { name: '行事曆', exact: true }).isVisible(), 'admin_login_failed');
}
async function schedule(page: Page) {
  await page.goto('/schedule');
  requireValue(await page.getByRole('heading', { name: '排程與人工入會', exact: true }).isVisible(), 'schedule_page_required');
}
export async function calendarOccurrences(page: Page) {
  await page.goto('/settings');
  requireValue(await page.getByRole('heading', { name: '設定', exact: true }).isVisible(), 'settings_page_required');
  await page.getByRole('button', { name: '更新行事曆', exact: true }).click();
  await schedule(page);
  requireValue(await page.locator('#calendar-sync').getAttribute('data-failed') === 'false', 'calendar_sync_failed');
  const timezone = await page.locator('section[data-timezone]').getAttribute('data-timezone');
  requireValue(timezone, 'schedule_timezone_missing');
  const occurrences = await page.locator('tr[data-state="pending"]').evaluateAll(rows => rows.map(row => ({
    occurrence_id: row.getAttribute('data-occurrence-id'),
    start_at: row.querySelector('time[data-edge="start"]')?.getAttribute('datetime'),
    end_at: row.querySelector('time[data-edge="end"]')?.getAttribute('datetime'),
  })).filter(row => Date.parse(row.start_at || '') >= Date.now() && Date.parse(row.end_at || '') > Date.parse(row.start_at || '')));
  return { timezone, occurrences };
}
export async function issueBinding(page: Page, args: Record<string, unknown>) {
  const id = args.occurrence_id;
  requireValue(typeof id === 'string' && /^[a-f0-9]{32}$/.test(id), 'invalid_occurrence_id');
  // Supply these only after fresh owner attendance/media consent; never infer it from login.
  requireValue(args.present === true && args.media === true, 'fresh_owner_confirmation_required');
  await schedule(page);
  const row = page.locator(`tr[data-occurrence-id="${id}"]`);
  requireValue(await row.count() === 1 && await row.getAttribute('data-state') === 'pending', 'pending_occurrence_required');
  const form = row.locator('form[action="/actions/target-consent"]');
  requireValue(await form.count() === 1, 'binding_form_required');
  await form.getByRole('checkbox', { name: '我現在已在此會議；已通知準備單次加入' }).check();
  await form.getByRole('checkbox', { name: '我確認本次加入、錄音、逐字保存及助理發聲同意' }).check();
  await form.getByRole('button', { name: '建立五分鐘 Hermes 單次控制 binding（不加入）', exact: true }).click();
  if (await page.getByRole('heading', { name: '操作未完成', exact: true }).count()) {
    await page.getByText('技術詳情', { exact: true }).click();
    requireValue(await page.locator('details code').isVisible(), 'binding_rejected');
    const code = (await page.locator('details code').innerText()).trim();
    // Server renders fixed, escaped reasons, never exception bodies.
    requireValue(/^[a-z_]{1,80}$/.test(code), 'binding_rejected');
    throw new OperationError(code, await page.locator('p.error').innerText());
  }
  requireValue(await page.getByRole('heading', { name: 'Hermes 單次控制 binding', exact: true }).isVisible(), 'binding_page_required');
  const value = JSON.parse(await page.locator('pre').innerText());
  requireValue(value.occurrence_id === id && Number.isInteger(value.revision) && value.revision > 0 &&
    /^[a-f0-9]{64}$/.test(value.request_sha256) && value.expires_in_seconds === 300 &&
    Number.isFinite(value.issued_at) && Number.isFinite(value.expires_at) &&
    value.expires_at > Date.now()/1000, 'binding_result_invalid');
  return { occurrence_id: id, revision: value.revision, request_sha256: value.request_sha256,
    issued_at: value.issued_at, expires_at: value.expires_at, expires_in_seconds: value.expires_in_seconds };
}
