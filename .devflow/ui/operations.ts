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
function datedEdge(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)$/.test(value) &&
    Number.isFinite(Date.parse(value));
}
const googleCodes = new Set([
  'google_oauth_client_required','google_authorization_required','google_reauthorization_required',
  'private_calendar_configuration_invalid','private_calendar_directory_required','google_transport_failed',
  'google_token_exchange_failed','invalid_google_token_response','calendar_response_too_large',
  'invalid_google_response','calendar_request_failed','invalid_calendar_window','invalid_calendar_pagination',
  'calendar_window_too_large','invalid_calendar_event','invalid_recurring_identity','calendar_event_too_large','calendar_poll_failed',
]);
async function readback(page: Page) {
  requireValue(await page.locator('#calendar-readback').count() === 1, 'calendar_readback_missing');
  const text = await page.locator('#calendar-readback').getAttribute('data-readback', {timeout:2000});
  let value;
  try { value = JSON.parse(text || ''); } catch { throw new OperationError('calendar_readback_invalid'); }
  const google = value?.google, read = value?.read;
  requireValue(/^[a-f0-9]{32}$/.test(value?.poll_id) && google && read &&
    ['configured','authorized','reauthorization_required'].every(key => typeof google[key] === 'boolean') &&
    (google.error_code === null || googleCodes.has(google.error_code)) &&
    Number.isSafeInteger(value.enabled_calendar_count) && value.enabled_calendar_count >= 0 &&
    ['attempted','succeeded','failed','event_count'].every(key => Number.isSafeInteger(read[key]) && read[key] >= 0) &&
    typeof read.completed === 'boolean' && read.attempted <= value.enabled_calendar_count &&
    read.attempted === read.succeeded + read.failed &&
    (!read.completed || read.attempted === value.enabled_calendar_count) &&
    Array.isArray(value.error_codes) && value.error_codes.every(code => googleCodes.has(code)) &&
    typeof value.ready === 'boolean', 'calendar_readback_invalid');
  const ready = google.configured && google.authorized && !google.reauthorization_required &&
    google.error_code === null && value.enabled_calendar_count > 0 && read.completed &&
    read.succeeded === value.enabled_calendar_count && value.error_codes.length === 0;
  requireValue(value.ready === ready, 'calendar_readback_invalid');
  return {poll_id:value.poll_id,google:{configured:google.configured,authorized:google.authorized,
    reauthorization_required:google.reauthorization_required,error_code:google.error_code},
    enabled_calendar_count:value.enabled_calendar_count,
    read:{attempted:read.attempted,succeeded:read.succeeded,failed:read.failed,event_count:read.event_count,completed:read.completed},
    error_codes:value.error_codes,ready};
}
export async function calendarOccurrences(page: Page) {
  await schedule(page);
  const previous = await page.locator('#calendar-readback').count() ? (await readback(page)).poll_id : null;
  await page.goto('/settings');
  requireValue(await page.getByRole('heading', { name: '設定', exact: true }).isVisible(), 'settings_page_required');
  await page.getByRole('button', { name: '更新行事曆', exact: true }).click();
  requireValue(await page.getByRole('heading', { name: '排程與人工入會', exact: true }).isVisible(), 'calendar_sync_action_failed');
  // Read the initiating session's completed snapshot through an explicit same-origin GET.
  await schedule(page);
  const evidence = await readback(page);
  requireValue(evidence.poll_id !== previous, 'calendar_readback_stale');
  const timezone = await page.locator('section[data-timezone]').getAttribute('data-timezone');
  requireValue(timezone && /^[A-Za-z_+-]+(?:\/[A-Za-z0-9_+-]+)*$/.test(timezone), 'schedule_timezone_missing');
  try { new Intl.DateTimeFormat('en', {timeZone:timezone}); } catch { throw new OperationError('schedule_timezone_missing'); }
  const rows = await page.locator('tr[data-state="pending"]').evaluateAll(rows => rows.map(row => ({
    occurrence_id: row.getAttribute('data-occurrence-id'),
    revision: row.querySelector('form[action="/actions/target-consent"] input[name="revision"]')?.getAttribute('value'),
    start_at: row.querySelector('time[data-edge="start"]')?.getAttribute('datetime'),
    end_at: row.querySelector('time[data-edge="end"]')?.getAttribute('datetime'),
    discoverable: row.getAttribute('data-discoverable'), joinable: row.getAttribute('data-joinable'),
  })));
  const occurrences: {occurrence_id:string,revision:number,start_at:string,end_at:string,joinable:boolean}[] = [];
  for (const row of rows) {
    const revision = Number(row.revision);
    requireValue(typeof row.occurrence_id === 'string' && /^[a-f0-9]{32}$/.test(row.occurrence_id) &&
      Number.isSafeInteger(revision) && revision > 0 &&
      datedEdge(row.start_at) && datedEdge(row.end_at) && Date.parse(row.end_at) > Date.parse(row.start_at) &&
      ['true','false'].includes(row.discoverable || '') && ['true','false'].includes(row.joinable || ''),
      'calendar_occurrence_invalid');
    if (row.discoverable === 'true' && Date.parse(row.end_at) > Date.now()) {
      occurrences.push({occurrence_id:row.occurrence_id,revision,start_at:row.start_at,end_at:row.end_at,joinable:row.joinable === 'true'});
    }
  }
  // Failed prerequisites never turn stale locally cached rows into candidates.
  return {timezone,...evidence,occurrences:evidence.ready ? occurrences : []};
}
type CalendarSetting = {calendar_ref:string,revision:number,primary:boolean,enabled:boolean,
  recording_authorized:boolean,speaking_authorized:boolean,join_early_seconds:number,max_lateness_seconds:number};
async function settingsReadback(page: Page): Promise<{scheduler_paused:boolean,calendars:CalendarSetting[]}> {
  await page.goto('/settings');
  requireValue(await page.getByRole('heading', {name:'設定',exact:true}).isVisible(), 'settings_page_required');
  requireValue(await page.locator('#calendar-settings').count() === 1, 'calendar_settings_missing');
  let data;
  try { data = JSON.parse(await page.locator('#calendar-settings').getAttribute('data-settings') || ''); }
  catch { throw new OperationError('calendar_settings_invalid'); }
  requireValue(data && Object.keys(data).sort().join(',') === 'calendars,scheduler_paused' &&
    typeof data.scheduler_paused === 'boolean' && Array.isArray(data.calendars), 'calendar_settings_invalid');
  const seen = new Set<string>();
  for (const row of data.calendars) {
    requireValue(row && Object.keys(row).sort().join(',') ===
      'calendar_ref,enabled,join_early_seconds,max_lateness_seconds,primary,recording_authorized,revision,speaking_authorized' &&
      typeof row.calendar_ref === 'string' && /^[a-f0-9]{32}$/.test(row.calendar_ref) && !seen.has(row.calendar_ref) &&
      Number.isSafeInteger(row.revision) && row.revision > 0 &&
      ['primary','enabled','recording_authorized','speaking_authorized'].every(key => typeof row[key] === 'boolean') &&
      Number.isSafeInteger(row.join_early_seconds) && row.join_early_seconds >= 0 && row.join_early_seconds <= 600 &&
      Number.isSafeInteger(row.max_lateness_seconds) && row.max_lateness_seconds >= 0 && row.max_lateness_seconds <= 1800,
      'calendar_settings_invalid');
    seen.add(row.calendar_ref);
  }
  requireValue(data.calendars.filter(row => row.primary).length <= 1, 'calendar_settings_invalid');
  return data;
}
export async function calendarSettings(page: Page, args: Record<string,unknown>) {
  requireValue(args && typeof args === 'object' && !Array.isArray(args), 'invalid_calendar_settings_args');
  const writing = Object.keys(args).length > 0;
  if (writing) requireValue(Object.keys(args).sort().join(',') === 'calendar_ref,enabled,revision' &&
    typeof args.calendar_ref === 'string' && (args.calendar_ref === 'primary' || /^[a-f0-9]{32}$/.test(args.calendar_ref)) &&
    Number.isSafeInteger(args.revision) && Number(args.revision) >= 0 && typeof args.enabled === 'boolean', 'invalid_calendar_settings_args');
  const before = await settingsReadback(page);
  if (!writing) return {...before,saved:false};
  requireValue(before.scheduler_paused, 'calendar_settings_not_quiescent');
  const old = before.calendars.find(row => row.calendar_ref === args.calendar_ref);
  requireValue(args.calendar_ref === 'primary' ? args.revision === 0 && !before.calendars.some(row => row.primary) :
    old && old.revision === args.revision, 'calendar_revision_changed');
  if (old && old.enabled === args.enabled) return {...before,saved:false};
  const form = page.locator('form[action="/actions/calendar-readonly"]').filter({
    has:page.locator(`input[name="calendar_ref"][value="${args.calendar_ref}"]`),
  });
  requireValue(await form.count() === 1 && await form.locator('input[name="revision"]').inputValue() === String(args.revision),
    'calendar_revision_changed');
  await form.getByRole('checkbox', {name:'啟用唯讀 Calendar',exact:true}).setChecked(args.enabled as boolean);
  await form.getByRole('button', {name:'保存唯讀設定',exact:true}).click();
  requireValue(await page.getByRole('heading', {name:'設定',exact:true}).isVisible(), 'calendar_settings_save_failed');
  const after = await settingsReadback(page);
  requireValue(after.scheduler_paused, 'calendar_settings_save_failed');
  const changed = old ? after.calendars.find(row => row.calendar_ref === old.calendar_ref) : after.calendars.find(row => row.primary);
  requireValue(changed && changed.enabled === args.enabled && changed.revision === Number(args.revision)+1 &&
    (old ? changed.recording_authorized === old.recording_authorized && changed.speaking_authorized === old.speaking_authorized &&
      changed.primary === old.primary && changed.join_early_seconds === old.join_early_seconds && changed.max_lateness_seconds === old.max_lateness_seconds :
      !changed.recording_authorized && !changed.speaking_authorized && changed.join_early_seconds === 300 && changed.max_lateness_seconds === 300) &&
    after.calendars.length === before.calendars.length+(old ? 0 : 1) &&
    before.calendars.filter(row => row !== old).every(row => JSON.stringify(row) === JSON.stringify(after.calendars.find(v => v.calendar_ref === row.calendar_ref))),
    'calendar_settings_save_failed');
  return {...after,saved:true};
}

export async function issueBinding(page: Page, args: Record<string, unknown>) {
  requireValue(args && typeof args === 'object' && !Array.isArray(args) &&
    Object.keys(args).sort().join(',') === 'end_at,media,occurrence_id,present,revision,start_at', 'invalid_binding_args');
  const {occurrence_id: id, revision, start_at: start, end_at: end} = args;
  requireValue(typeof id === 'string' && /^[a-f0-9]{32}$/.test(id) &&
    typeof revision === 'number' && Number.isSafeInteger(revision) && revision > 0 &&
    datedEdge(start) && datedEdge(end) && Date.parse(end) > Date.parse(start) &&
    typeof args.present === 'boolean' && typeof args.media === 'boolean', 'invalid_binding_args');
  // Supply these only after fresh owner attendance/media consent; never infer it from login.
  requireValue(args.present === true && args.media === true, 'fresh_owner_confirmation_required');
  await schedule(page);
  const row = page.locator(`tr[data-occurrence-id="${id}"]`);
  requireValue(await row.count() === 1 && await row.getAttribute('data-state') === 'pending', 'pending_occurrence_required');
  const form = row.locator('form[action="/actions/target-consent"]');
  requireValue(await form.count() === 1, 'binding_form_required');
  requireValue(await form.locator('input[name="id"]').inputValue() === id &&
    await form.locator('input[name="revision"]').inputValue() === String(revision) &&
    await row.locator('time[data-edge="start"]').getAttribute('datetime') === start &&
    await row.locator('time[data-edge="end"]').getAttribute('datetime') === end, 'binding_selection_changed');
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
  requireValue(value.occurrence_id === id && value.revision === revision + 1 &&
    /^[a-f0-9]{64}$/.test(value.request_sha256) && value.expires_in_seconds === 300 &&
    Number.isFinite(value.issued_at) && Number.isFinite(value.expires_at) &&
    value.expires_at > Date.now()/1000, 'binding_result_invalid');
  return { occurrence_id: id, revision: value.revision, request_sha256: value.request_sha256,
    issued_at: value.issued_at, expires_at: value.expires_at, expires_in_seconds: value.expires_in_seconds };
}
