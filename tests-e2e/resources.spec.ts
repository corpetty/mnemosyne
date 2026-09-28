import { expect, test } from '@playwright/test';
import { makeWav, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name.startsWith('resources-'))) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  const lib: { asset: { id: string } }[] = await (await request.get(`${BACKEND}/api/assets`)).json();
  for (const x of lib) await request.delete(`${BACKEND}/api/assets/${x.asset.id}`);
});

// A link shared in one meeting is at hand in the next; files can be added too.
test('add a link, reuse it in another meeting, add a file', async ({ page, request }, info) => {
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'resources-first' } });
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'resources-second' } });
  await openApp(page);

  await page.getByText('resources-first').first().click();
  const card = page.getByRole('region', { name: 'Resources' });
  await card.getByPlaceholder('Paste a link, then Enter').fill('https://docs.example.com/d/launch-plan');
  await card.getByPlaceholder('Paste a link, then Enter').press('Enter');
  await expect(card.getByRole('button', { name: 'docs.example.com/launch-plan', exact: true })).toBeVisible();

  await page.getByText('resources-second').first().click();
  await card.getByRole('button', { name: 'From earlier meetings…' }).click();
  const picker = page.getByRole('dialog', { name: 'Resources from earlier meetings' });
  await expect(picker).toContainText('1 meeting');
  await picker.getByRole('button', { name: 'Attach' }).click();
  await expect(picker).toContainText('attached');
  await page.keyboard.press('Escape');
  await expect(card.getByRole('button', { name: 'docs.example.com/launch-plan', exact: true })).toBeVisible();

  const chooser = page.waitForEvent('filechooser');
  await card.getByRole('button', { name: 'Add a file' }).click();
  await (await chooser).setFiles(makeWav(info.outputDir, 'recording-notes.wav', 1));
  await expect(card.getByRole('button', { name: 'recording-notes.wav', exact: true })).toBeVisible();
});
