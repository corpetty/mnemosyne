import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const PUBLISH_HOST = 'http://127.0.0.1:8028';
const NAME = 'publish-live-demo';
const EDITED = 'Live update: the summary changed while the reader had the page open.';

// A hosted publication that follows the meeting (live publications, task 10; docs/plans/
// 2026-11-05-live-publications.md): the owner's backend keeps a room on the publish host and its
// watcher pushes a new version a few seconds after the meeting stops changing; an open viewer takes
// it without navigating. The e2e backend has no `publish_host` (start-backend.sh only knows the
// guest host), so the spec sets it through the settings API and puts it back afterwards.
test.setTimeout(120_000);

// The demo summary is the same for every meeting: a summarized meeting left behind would add a
// second copy of its decision and task to the specs that run after this one.
test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) {
    await request.delete(`${BACKEND}/api/sessions/${s.id}/publication`).catch(() => {});
    await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  }
  await request.put(`${BACKEND}/api/settings`, { data: { publish_host: '' } });
});

test('a hosted link kept up to date: a new version reaches the open viewer, readers are counted, unpublish ends it', async ({
  page,
  context,
  request
}, info) => {
  const set = await request.put(`${BACKEND}/api/settings`, { data: { publish_host: PUBLISH_HOST } });
  expect(set.ok()).toBeTruthy();

  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Update the docs before the release').first()).toBeVisible();

  // 1. Publish to the host: both live switches start on.
  await page.getByRole('button', { name: 'More for this meeting' }).click();
  await page.getByRole('menuitem', { name: /^Publish…/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Publish this meeting' });
  await expect(dialog.getByRole('heading', { name: NAME, level: 1 })).toBeVisible(); // the preview
  await dialog.getByRole('group', { name: 'Where' }).getByRole('button', { name: 'Publish host' }).click();
  await expect(dialog.getByRole('checkbox', { name: 'Keep the link up to date' })).toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: 'Readers may comment' })).toBeChecked();
  await dialog.getByRole('button', { name: 'Publish', exact: true }).click();
  // Exact: "Keep the link up to date" is a label too.
  const link = dialog.getByLabel('Link', { exact: true });
  await expect(link).toBeVisible();
  const url = await link.inputValue();
  expect(url).toContain(`${PUBLISH_HOST}/p/`);
  expect(url).toContain('#');
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toHaveCount(0);

  // 2. A reader opens the link and is told the page follows the meeting.
  const reader = await context.newPage();
  await reader.goto(url);
  await expect(reader.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
  await expect(reader.getByText('Kept up to date as the meeting changes')).toBeVisible();
  const readerUrl = reader.url();
  await reader.evaluate(() => {
    (window as unknown as { __liveMarker: string }).__liveMarker = 'same page';
  });
  await expect(reader.getByText(EDITED)).toHaveCount(0);
  await expect(reader.getByText(/Updated \d\d:\d\d/)).toHaveCount(0);

  // 3. Edit the summary in the app. The watcher waits for the meeting to be quiet (10 s) and
  // publishes by itself; the viewer takes the version from the room, without a reload.
  await page.getByTitle(/^Edit the summary/).click();
  await page.getByLabel('Summary text').fill(EDITED);
  await page.getByRole('button', { name: 'Save summary' }).click();
  await expect(page.getByText('Summary saved')).toBeVisible();
  await expect(reader.getByText(EDITED)).toBeVisible({ timeout: 45_000 });
  expect(reader.url()).toBe(readerUrl);
  expect(await reader.evaluate(() => (window as unknown as { __liveMarker?: string }).__liveMarker)).toBe('same page');
  await expect(reader.getByText(/Updated \d\d:\d\d/)).toBeVisible();
  await expect(reader.getByText('Kept up to date as the meeting changes')).toBeVisible();

  // 4. The owner's dialog shows the link as live, with the reader in the room.
  const chip = page.getByRole('button', { name: /^Published · v\d+/ });
  const published = page.getByRole('dialog', { name: 'Publish this meeting' });
  await expect(async () => {
    // Each try reads the publication afresh, so it closes the dialog of the last one first.
    if (await published.isVisible()) {
      await published.getByRole('button', { name: /^(Close|Cancel)$/ }).click();
      await expect(published).toHaveCount(0);
    }
    await chip.click();
    await expect(published).toContainText('· live', { timeout: 3_000 });
    await expect(published).toContainText('1 reading', { timeout: 3_000 });
  }).toPass({
    timeout: 30_000,
    intervals: [1_000, 2_000, 3_000]
  });

  // 5. Unpublish: the open viewer says so at once, without a reload. The publication is deleted
  // before its room, so the re-read on the room's close (4410) already finds it gone (410).
  await published.getByRole('button', { name: 'Unpublish', exact: true }).click();
  await published.getByRole('button', { name: 'Yes', exact: true }).click();
  await expect(page.getByText('Unpublished. The link no longer works.')).toBeVisible();
  await expect(reader.getByRole('heading', { name: 'No longer published' })).toBeVisible({ timeout: 15_000 });
});
