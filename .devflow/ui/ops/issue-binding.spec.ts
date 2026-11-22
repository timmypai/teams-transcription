import { test } from '@playwright/test';
import { issueBinding, login } from '../operations';
import { runOperation } from './run';

test('issue-binding', async ({ page }) => {
  await runOperation(page, 'issue-binding', async () => {
    const args = JSON.parse(process.env.DEVFLOW_OP_ARGS || '{}');
    await login(page);
    return issueBinding(page, args);
  });
});
