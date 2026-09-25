import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

test('settings are saved and survive a reload', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Transcription', exact: true }).click();
  const glossary = page.locator('textarea[placeholder*="->"]');
  await glossary.fill('Waku\nwalk you -> Waku');
  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByText('Settings saved')).toBeVisible();

  await page.reload();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Transcription', exact: true }).click();
  await expect(page.locator('textarea[placeholder*="->"]')).toHaveValue('Waku\nwalk you -> Waku');
});

test('the demo provider is the only one listed', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await expect(page.getByText('Provider status')).toBeVisible();
  const status = page.locator('section', { has: page.getByText('Provider status') });
  await expect(status.getByText('demo', { exact: true })).toBeVisible();
  await expect(status.getByText('1 model', { exact: true })).toBeVisible();
  await expect(status.getByText('ollama', { exact: true })).toHaveCount(0);
});

test('settings are grouped in tabs', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const tabs = page.getByRole('navigation', { name: 'Settings sections' });
  for (const [tab, heading] of [
    ['General', 'Server mode'],
    ['Recording', 'Auto-record'],
    ['Transcription', 'Names and terms'],
    ['AI', 'Search and Ask'],
    ['Notes & sharing', 'GitHub issues']
  ]) {
    await tabs.getByRole('button', { name: tab, exact: true }).click();
    await expect(page.getByRole('heading', { name: heading })).toBeVisible();
  }
});
