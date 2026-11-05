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

// Readers' comments on a live publication (live publications, tasks 11-13; M2). A reader's page boxes
// and signs each comment into the room; the owner's backend bridge applies it (a 2 s THREADS
// debounce) and seals the threads back, redacted, to every reader. Round trips are allowed ~30 s.
const ROUND_TRIP = { timeout: 30_000 };
const ON_DECISION = 'Does October still hold if QA slips?';
const ON_PASSAGE = 'Which migration is this about?';
const PASSAGE = 'Waku migration';
const REPLY = 'Yes, QA has a week of slack.';

test('readers comment on a published link: threads reach other readers and the owner, replies and removals come back, comments off', async ({
  page,
  context,
  browser,
  request
}, info) => {
  test.setTimeout(240_000);
  const set = await request.put(`${BACKEND}/api/settings`, { data: { publish_host: PUBLISH_HOST } });
  expect(set.ok()).toBeTruthy();

  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Update the docs before the release').first()).toBeVisible();

  // 1. Publish to the host with "Readers may comment" on (it starts on).
  await page.getByRole('button', { name: 'More for this meeting' }).click();
  await page.getByRole('menuitem', { name: /^Publish…/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Publish this meeting' });
  await dialog.getByRole('group', { name: 'Where' }).getByRole('button', { name: 'Publish host' }).click();
  await expect(dialog.getByRole('checkbox', { name: 'Readers may comment' })).toBeChecked();
  await dialog.getByRole('button', { name: 'Publish', exact: true }).click();
  const link = dialog.getByLabel('Link', { exact: true });
  await expect(link).toBeVisible();
  const url = await link.inputValue();
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toHaveCount(0);

  // 2. Reader A: asked for a name at the first comment, then a comment on the decision and one on a
  // passage of the summary text (the floating "Comment" after a selection).
  const a = await context.newPage();
  await a.goto(url);
  await expect(a.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
  const composeA = a.getByTestId('reader-compose');
  await expect(composeA).toBeVisible({ timeout: 15_000 }); // META (comments on) has arrived
  await composeA.getByLabel('Your name, shown with your comments').fill('Ana');
  await composeA.getByRole('button', { name: 'Continue' }).click();
  await expect(composeA.getByLabel('Comment on this meeting')).toBeVisible();

  await a.locator('[data-anchor^="decision:"]').first().getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(composeA.getByTestId('compose-anchor')).toContainText('Ship the Waku migration in October');
  await composeA.getByLabel('Your comment').fill(ON_DECISION);
  await composeA.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(a.locator('[data-thread]').filter({ hasText: ON_DECISION })).toBeVisible();

  // Select "Waku migration" in the summary text; the viewer follows `selectionchange`.
  await a.evaluate((needle) => {
    const body = document.querySelector('[data-anchor="body"]')!;
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const i = (n.textContent ?? '').indexOf(needle);
      if (i < 0) continue;
      const range = document.createRange();
      range.setStart(n, i);
      range.setEnd(n, i + needle.length);
      const sel = document.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
      return;
    }
    throw new Error('summary text not found');
  }, PASSAGE);
  await a.getByTestId('comment-passage').click();
  await expect(composeA.getByTestId('compose-anchor')).toContainText(PASSAGE);
  await composeA.getByLabel('Your comment').fill(ON_PASSAGE);
  await composeA.getByRole('button', { name: 'Send', exact: true }).click();

  // Once the owner's THREADS carries them back, Ana's own comments are hers to edit and delete.
  const threadsA = a.locator('[data-thread]');
  await expect(threadsA).toHaveCount(2);
  await expect(a.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(2, ROUND_TRIP);
  await expect(a.getByRole('button', { name: 'Delete', exact: true })).toHaveCount(2);

  // 3. Reader B (another context: another reader key) sees both threads and may not touch them.
  const ctxB = await browser.newContext();
  const b = await ctxB.newPage();
  await b.goto(url);
  await expect(b.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
  const threadsB = b.locator('[data-thread]');
  await expect(threadsB.filter({ hasText: ON_DECISION })).toBeVisible(ROUND_TRIP);
  await expect(threadsB.filter({ hasText: ON_PASSAGE })).toBeVisible();
  await expect(threadsB.filter({ hasText: 'Ana' })).toHaveCount(2);
  await expect(b.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0);
  await expect(b.getByRole('button', { name: 'Delete', exact: true })).toHaveCount(0);

  // 4. The owner's Comments card shows both, authored "Ana (reader)", with where they came from.
  const card = page.getByRole('region', { name: 'Comments' });
  const ownerDecision = card.locator('[data-comment-thread]').filter({ hasText: ON_DECISION });
  const ownerPassage = card.locator('[data-comment-thread]').filter({ hasText: ON_PASSAGE });
  await expect(ownerDecision).toBeVisible(ROUND_TRIP);
  await expect(ownerPassage).toBeVisible();
  await expect(card.getByText('Ana (reader)', { exact: true })).toHaveCount(2);
  await expect(card.getByText('From the published link · replies are shown to its readers')).toHaveCount(2);
  await expect(ownerDecision).toContainText('Ship the Waku migration in October');
  await expect(ownerPassage).toContainText(PASSAGE);

  // 5. The owner replies on the decision; both readers' pages show the reply.
  await ownerDecision.getByRole('button', { name: 'Reply', exact: true }).click();
  await ownerDecision.getByPlaceholder('Reply').fill(REPLY);
  await ownerDecision.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(ownerDecision).toContainText(REPLY);
  await expect(threadsA.filter({ hasText: ON_DECISION })).toContainText(REPLY, ROUND_TRIP);
  await expect(threadsB.filter({ hasText: ON_DECISION })).toContainText(REPLY, ROUND_TRIP);

  // 6. The owner deletes Ana's comment on the passage (nothing replies to it, so the store drops it
  // for good): both readers keep the thread as "removed".
  await ownerPassage.getByRole('button', { name: 'Delete', exact: true }).click();
  await ownerPassage.getByRole('button', { name: 'Yes', exact: true }).click();
  await expect(card.getByText(ON_PASSAGE)).toHaveCount(0);
  await expect(a.getByText('removed', { exact: true })).toBeVisible(ROUND_TRIP);
  await expect(b.getByText('removed', { exact: true })).toBeVisible(ROUND_TRIP);
  await expect(a.getByText(ON_PASSAGE)).toHaveCount(0);
  await expect(b.getByText(ON_PASSAGE)).toHaveCount(0);

  // 7. The owner turns comments off and updates: the composer and every Comment button go from
  // reader A's page (the threads stay, read-only).
  await expect(composeA).toBeVisible();
  await page.getByRole('button', { name: /^Published · v\d+/ }).click();
  const published = page.getByRole('dialog', { name: 'Publish this meeting' });
  const comments = published.getByRole('checkbox', { name: 'Readers may comment' });
  await expect(comments).toBeChecked();
  await comments.uncheck();
  await published.getByRole('button', { name: 'Update', exact: true }).click();
  await expect(page.getByText(/^Updated: version \d+/)).toBeVisible();
  await expect(a.getByTestId('reader-compose')).toHaveCount(0, ROUND_TRIP);
  await expect(a.getByTestId('comment-on')).toHaveCount(0);
  await expect(a.getByTestId('comment-count')).toHaveCount(0);
  await expect(a.getByRole('button', { name: 'Reply', exact: true })).toHaveCount(0);
  await expect(threadsA.filter({ hasText: ON_DECISION })).toContainText(REPLY);
  await ctxB.close();
});
