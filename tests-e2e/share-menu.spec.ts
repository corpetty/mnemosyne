import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// The one Share button of a meeting's header (components/ShareButton.svelte). "People with Mnemosyne" is
// listed whenever contacts are available and shared-with-me.spec.ts covers its dialog; "Team members" exists
// on a team server only, so here just its absence is asserted.
const BACKEND = 'http://127.0.0.1:8018';
const NAME = 'share-menu-demo';

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

test('one Share menu offers the live-notes link and a page, and each opens its own panel', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);

  // The old separate buttons are gone.
  await expect(page.getByRole('button', { name: 'Invite guests', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Publish…', exact: true })).toHaveCount(0);

  const share = page.getByRole('button', { name: 'Share', exact: true });
  await share.click();
  const menu = page.getByRole('menu');
  await expect(menu.getByRole('menuitem', { name: /^A link to the live notes/ })).toBeVisible();
  await expect(menu.getByRole('menuitem', { name: /^A page…/ })).toBeVisible();
  await expect(menu.getByRole('menuitem', { name: /^Team members/ })).toHaveCount(0);

  // The live-notes link: its panel replaces the list, "‹ Share" goes back.
  await menu.getByRole('menuitem', { name: /^A link to the live notes/ }).click();
  await expect(page.getByRole('dialog', { name: 'Guest notes' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create a guest link' })).toBeVisible();
  await page.getByRole('button', { name: '‹ Share' }).click();
  await expect(page.getByRole('dialog', { name: 'Guest notes' })).toHaveCount(0);

  // A page: the publish dialog for this meeting.
  await page.getByRole('menuitem', { name: /^A page…/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Publish this meeting' });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('menu')).toHaveCount(0); // the menu closed behind it
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toHaveCount(0);
});

test('Escape closes the Share menu', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  await page.getByRole('button', { name: 'Share', exact: true }).click();
  await expect(page.getByRole('menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);
});
