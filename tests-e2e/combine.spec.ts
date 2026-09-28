import { expect, test } from '@playwright/test';
import { importMeeting, makeWav, openApp } from './fixtures';

// A call that dropped and was recorded again: put the two meetings back together.
const BACKEND = 'http://127.0.0.1:8018';

test('combine two meetings, then add an audio file as a part', async ({ page, request }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'first-half');
  await importMeeting(page, info.outputDir, 'second-half');

  await page.getByRole('button', { name: 'More for this meeting' }).click();
  await page.getByRole('menuitem', { name: /Combine with another meeting/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Combine with another meeting' });
  await dialog.getByPlaceholder('Find a meeting').fill('first');
  page.once('dialog', (d) => d.accept());
  await dialog.getByRole('button', { name: /first-half/ }).click();
  await expect(page.getByText('Combined: 2 parts')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'second-half' })).toBeVisible();
  await expect(page.getByText('first-half', { exact: true })).toHaveCount(0); // gone from the list

  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'More for this meeting' }).click();
  await page.getByRole('menuitem', { name: /Add an audio file/ }).click();
  await (await chooser).setFiles(makeWav(info.outputDir, 'phone.wav', 5));
  await expect(page.getByText('Added phone.wav as the next part')).toBeVisible();

  // Leave the shared demo backend as the other specs expect it (their search and Ask results).
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'second-half')) {
    await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  }
});
