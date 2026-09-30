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

test('copy diagnostics puts a report without secrets on the clipboard', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'General', exact: true }).click();
  await page.getByRole('button', { name: 'Copy diagnostics' }).click();
  await expect(page.getByText('Diagnostics copied')).toBeVisible();
  await expect(page.getByText('Backend log:')).toBeVisible();
  const text = await page.evaluate(() => navigator.clipboard.readText());
  expect(text).toContain('## Mnemosyne diagnostics');
  expect(text).toContain("transcriber = 'demo'");
  expect(text).toMatch(/obsidian_vault_path = <set>/); // a path with the user's name in it
});

test('report a problem copies the report and opens a GitHub issue with only the title', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await context.route('https://github.com/**', (route) => route.fulfill({ body: 'stub' })); // never GitHub
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'General', exact: true }).click();
  await page.getByRole('button', { name: 'Report a problem…' }).click();
  const dialog = page.getByRole('dialog', { name: 'Report a problem' });
  await expect(dialog.getByLabel('Diagnostics (editable)')).toHaveValue(/## Mnemosyne diagnostics/);
  await dialog.getByLabel('Title').fill('Echo canceller picks the wrong mic');
  await dialog.getByLabel('What happened?').fill('It used the webcam mic.');

  const popup = context.waitForEvent('page');
  await dialog.getByRole('button', { name: 'Copy and open GitHub' }).click();
  const url = new URL((await popup).url());
  expect(url.origin + url.pathname).toBe('https://github.com/corpetty/mnemosyne/issues/new');
  expect(url.searchParams.get('title')).toBe('Echo canceller picks the wrong mic');
  expect(url.searchParams.get('body')).not.toContain('Mnemosyne diagnostics'); // not in the link
  const report = await page.evaluate(() => navigator.clipboard.readText());
  expect(report).toContain('It used the webcam mic.');
  expect(report).toContain('## Mnemosyne diagnostics');
  await expect(dialog).toBeHidden();
});

test('the calendar can be turned off, or read from the desktop', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Recording', exact: true }).click();
  const source = page.getByRole('radiogroup', { name: 'Calendar source' });
  await source.getByText("This computer's calendars").click();
  await expect(page.getByText('GNOME Settings → Online Accounts')).toBeVisible();
  await source.getByText('No calendar').click();
  await expect(page.getByRole('button', { name: 'Test', exact: true })).toBeHidden();
  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByText('Settings saved')).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Recording', exact: true }).click();
  await expect(page.getByRole('radio', { name: 'No calendar' })).toBeChecked();
  // Put it back for the specs that follow.
  await page.getByRole('radiogroup', { name: 'Calendar source' }).getByText('Private ICS address').click();
  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByText('Settings saved')).toBeVisible();
});

// The app scrolls inside its panes, never as a page: an element poking out below the window
// (the calendar radios once did, 400 px) let the wheel move the whole app up over the window.
test('settings never make the page taller than the window', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const tabs = page.getByRole('navigation', { name: 'Settings sections' });
  for (const tab of ['General', 'Recording', 'Transcription', 'AI', 'Notes & sharing']) {
    await tabs.getByRole('button', { name: tab, exact: true }).click();
    const [scrollHeight, height] = await page.evaluate(() => [
      document.documentElement.scrollHeight,
      window.innerHeight
    ]);
    expect(scrollHeight, tab).toBeLessThanOrEqual(height);
  }
  await page.mouse.move(600, 400);
  for (let i = 0; i < 10; i++) await page.mouse.wheel(0, 2000);
  expect(await page.evaluate(() => document.scrollingElement!.scrollTop)).toBe(0);
});
