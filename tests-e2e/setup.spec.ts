import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

test('the setup wizard walks through every step and saves choices', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Run setup again' }).click();
  const setup = page.getByLabel('Setup', { exact: true });
  await expect(setup.getByRole('heading', { name: 'Welcome to Mnemosyne' })).toBeVisible();
  await setup.getByRole('button', { name: 'Get started' }).click();

  await expect(setup.getByRole('heading', { name: 'What should be recorded?' })).toBeVisible();
  await setup.getByRole('button', { name: 'Next' }).click();

  await expect(setup.getByRole('heading', { name: 'How should meetings be transcribed?' })).toBeVisible();
  await setup.getByRole('radio', { name: /^Parakeet/ }).check();
  await setup.getByRole('button', { name: 'Next' }).click();

  await expect(setup.getByRole('heading', { name: 'Which model writes summaries?' })).toBeVisible();
  await setup.getByRole('radio', { name: /^Not now/ }).check();
  await setup.getByRole('button', { name: 'Next' }).click();

  await expect(setup.getByRole('heading', { name: /Notes and calendar/ })).toBeVisible();
  await setup.getByLabel('Obsidian vault path').fill('/tmp/some-vault');
  await setup.getByRole('button', { name: 'Next' }).click();

  await expect(setup.getByRole('heading', { name: "You're set" })).toBeVisible();
  await expect(setup.getByText('Transcription: Parakeet')).toBeVisible();
  await setup.getByRole('button', { name: 'Go to the app' }).click();
  await expect(setup).toHaveCount(0);

  const settings = await (await page.request.get('http://127.0.0.1:8018/api/settings')).json();
  expect(settings.values.transcriber).toBe('parakeet');
  expect(settings.values.obsidian_vault_path).toBe('/tmp/some-vault');
  expect(settings.values.setup_complete).toBe(true);
});
