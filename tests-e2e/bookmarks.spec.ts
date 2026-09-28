import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

// Marking a moment afterwards, on a transcript line (while recording it is the Mark button,
// Ctrl+M, the tray or `mnemosyne --mark`; capture needs PipeWire, so that is tested in pytest).
// The demo backend is shared by all specs: leave no meeting behind, even on failure.
test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'bookmark-check')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

test('bookmark a line, then find, annotate and remove it', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'bookmark-check');
  await page.getByRole('button', { name: 'Quote from line 3' }).click();
  await page.getByRole('toolbar', { name: 'Quote' }).getByRole('button', { name: /Bookmark/ }).click();
  await expect(page.getByText('Bookmarked')).toBeVisible();
  const marks = page.getByLabel('Bookmarks');
  await expect(marks.getByTitle('Go to this moment')).toContainText('0:07');

  page.once('dialog', (d) => d.accept('ship date'));
  await marks.getByRole('button', { name: 'Edit the note' }).click();
  await expect(marks).toContainText('ship date');
  await marks.getByRole('button', { name: 'Remove the bookmark' }).click();
  await expect(marks).toBeHidden();
});
