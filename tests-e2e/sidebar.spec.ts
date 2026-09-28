import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAMES = ['delete-me', 'rec-live', 'other-meeting', 'finished-one'];

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => NAMES.includes(x.name))) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

const row = (page: import('@playwright/test').Page, name: string) =>
  page.locator('[role=button]', { hasText: name }).first();

// A meeting is deleted for good, so one click only arms the button.
test('deleting a meeting takes a second click', async ({ page, request }) => {
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'delete-me' } });
  await openApp(page);
  await page.getByRole('button', { name: 'Delete delete-me', exact: true }).click();
  await expect(row(page, 'delete-me')).toBeVisible();
  await page.getByRole('button', { name: 'Confirm deleting delete-me', exact: true }).click();
  await expect(page.getByText('Deleted “delete-me”')).toBeVisible();
  await expect(row(page, 'delete-me')).toHaveCount(0);
});

// Looking at another meeting while recording neither shows the recording's live lines there
// nor loses them.
test('the live transcript stays with the meeting being recorded', async ({ page, request }) => {
  const { id } = await (await request.post(`${BACKEND}/api/sessions`, { data: { name: 'rec-live' } })).json();
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'other-meeting' } });
  await page.route(/\/api\/audio\/active$/, (route) =>
    route.fulfill({
      json: [
        {
          session_id: id,
          started_at: Date.now() / 1000 - 30,
          device_ids: [1],
          part: 0,
          live: true,
          live_segments: [{ source: 'mic', segment: { text: 'Heard while recording.', speaker: 'Me', start: 3, end: 5 } }]
        }
      ]
    })
  );
  await page.goto('/');
  await expect(page.getByText('Heard while recording.')).toBeVisible();

  await row(page, 'other-meeting').click();
  await expect(page.getByRole('heading', { name: 'other-meeting' })).toBeVisible();
  await expect(page.getByText('Heard while recording.')).toHaveCount(0);

  await row(page, 'rec-live').click();
  await expect(page.getByRole('heading', { name: 'rec-live' })).toBeVisible();
  await page.getByTitle('Go to the recording').click();
  await expect(page.getByText('Heard while recording.')).toBeVisible();
});

// A finished meeting opens on its transcript, not on the Recording tab left from before.
test('a finished meeting does not open on the recording tab', async ({ page, request }, info) => {
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'other-meeting' } });
  await openApp(page);
  await importMeeting(page, info.outputDir, 'finished-one');
  await row(page, 'other-meeting').click();
  const tab = (name: RegExp) => page.getByRole('navigation', { name: 'Meeting' }).getByRole('button', { name });
  await tab(/^Recording/).click();
  await expect(tab(/^Recording/)).toHaveAttribute('aria-current', 'page');
  await row(page, 'finished-one').click();
  await expect(tab(/^Transcript/)).toHaveAttribute('aria-current', 'page');
});
