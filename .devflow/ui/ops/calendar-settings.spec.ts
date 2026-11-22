import { test } from '@playwright/test';
import { calendarSettings, login } from '../operations';
import { runOperation } from './run';

test('calendar-settings', async ({page}) => {
  await runOperation(page, 'calendar-settings', async () => {
    await login(page);
    return calendarSettings(page, JSON.parse(process.env.DEVFLOW_OP_ARGS || '{}'));
  });
});
