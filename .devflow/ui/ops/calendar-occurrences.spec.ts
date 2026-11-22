import { test } from '@playwright/test';
import { calendarOccurrences, login } from '../operations';
import { runOperation } from './run';

test('calendar-occurrences', async ({ page }) => {
  await runOperation(page, 'calendar-occurrences', async () => {
    await login(page);
    return calendarOccurrences(page);
  });
});
