import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

test('settings are saved and survive a reload', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const glossary = page.locator('textarea[placeholder*="->"]');
  await glossary.fill('Waku\nwalk you -> Waku');
  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByText('Settings saved')).toBeVisible();

  await page.reload();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.locator('textarea[placeholder*="->"]')).toHaveValue('Waku\nwalk you -> Waku');
});

test('the demo provider is the only one listed', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByText('Provider status')).toBeVisible();
  const status = page.locator('section', { has: page.getByText('Provider status') });
  await expect(status.getByText('demo', { exact: true })).toBeVisible();
  await expect(status.getByText('1 model', { exact: true })).toBeVisible();
  await expect(status.getByText('ollama', { exact: true })).toHaveCount(0);
});
