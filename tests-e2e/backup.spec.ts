import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

// The demo backend's backups go to tests-e2e/.data/backups (start-backend.sh), never ~/Documents.
test('back up now, then the backup is listed with its meetings', async ({ page, request }) => {
  await request.post('http://127.0.0.1:8018/api/sessions', { data: { name: 'backed-up meeting' } });
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'General', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Backups' })).toBeVisible();
  await page.getByRole('button', { name: 'Back up now' }).click();
  await expect(page.getByText('Backup made')).toBeVisible();
  const list = page.getByRole('list', { name: 'Backups' });
  await expect(list.getByRole('listitem')).toHaveCount(1);
  await expect(list).toContainText(/\d+ meetings? · .* · not encrypted/);

  // Restoring asks first, then waits for the backend to restart (in a browser: by hand).
  page.once('dialog', (d) => d.accept());
  await list.getByRole('button', { name: 'Restore' }).click();
  await expect(page.getByText('Restart the backend to finish restoring')).toBeVisible();
  await expect(page.getByText(/is restored when the backend restarts/)).toBeVisible();
});
