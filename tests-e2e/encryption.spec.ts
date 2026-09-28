import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

// The demo backend keeps its key in a file in its data dir, never in the real keyring.
test('encrypt meetings, keep the recovery code, and turn it off again', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'General', exact: true }).click();
  await expect(page.getByText('Off', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Encrypt meetings…' }).click();
  await page.getByRole('button', { name: 'Encrypt', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Recovery code' });
  const code = (await dialog.getByLabel('The recovery code').textContent())!.trim();
  expect(code).toMatch(/^([A-Z2-7]{4}-){12}[A-Z2-7]{4}$/);
  const done = dialog.getByRole('button', { name: 'Done' });
  await expect(done).toBeDisabled(); // not before the code is saved
  await dialog.getByRole('button', { name: 'Copy' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(code);
  await dialog.getByLabel('I have saved it').check();
  await done.click();
  await expect(page.getByText('On: meetings are encrypted')).toBeVisible();

  // Everything still works while encrypted.
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Tasks' })).toBeVisible();

  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'General', exact: true }).click();
  await page.getByRole('button', { name: 'Turn off…' }).click();
  await page.getByRole('button', { name: 'Decrypt', exact: true }).click();
  await expect(page.getByText('Meetings are no longer encrypted')).toBeVisible();
  await expect(page.getByText('Off', { exact: true })).toBeVisible();
});
