import { test } from '@playwright/test';
import { OperationError } from '../operations';
import { runOperation } from './run';

test('look', async ({ page }) => {
  await runOperation(page, 'look', async () => {
    const { path = '/login' } = JSON.parse(process.env.DEVFLOW_OP_ARGS || '{}');
    // Unattended look cannot export OAuth pages or private meeting data.
    if (path !== '/login') throw new OperationError('look_login_only');
    const response = await page.goto(path);
    if (response?.status() !== 200 || !await page.getByRole('heading', { name: '管理登入', exact: true }).isVisible()) {
      throw new OperationError('login_page_required');
    }
    return { path, status: 200, title: '管理登入' };
  });
});
