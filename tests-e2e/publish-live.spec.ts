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

// Readers' comments on the published NOTES (M4, items 1-2; docs/plans/2026-11-09-live-publications-m4.md).
// The reader selects a passage of the notes and comments; the owner's bridge finds the passage in
// the live notes document and puts a comment mark on it, so the owner sees the thread on the
// notes as any notes comment ("Ana (reader)", "From the published link"), a second reader sees it
// with its quote, and it follows the text: edits around the passage leave it alone, deleting the
// passage detaches it. Needs the link sidecar (the notes editor, and the bridge's marking).
const NOTES_LINE = 'Budget sign-off waits for the vendor quote.';
const NOTES_PASSAGE = 'vendor quote';
const NOTES_OTHER = 'Next review is on Friday.';
const NOTES_PREFIX = 'Update: ';
const ON_NOTES = 'Can we get that quote this week?';

test('readers comment on the published notes: the owner sees the mark, another reader the quote, and the thread follows an edit and detaches with the text', async ({
  page,
  context,
  browser,
  request
}, info) => {
  test.setTimeout(300_000);
  const set = await request.put(`${BACKEND}/api/settings`, { data: { publish_host: PUBLISH_HOST } });
  expect(set.ok()).toBeTruthy();

  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  const id: string = (await (await request.get(`${BACKEND}/api/sessions`)).json()).find((s: { name: string }) => s.name === NAME).id;
  const notesThreads = async () => {
    const body: {
      threads: { root: { id: string; author: string; text: string; target: string; quote: string; changed: boolean; detached: boolean } }[];
    } = await (await request.get(`${BACKEND}/api/sessions/${id}/comments`)).json();
    return body.threads.filter((t) => t.root.target === 'notes' && t.root.text === ON_NOTES);
  };

  // 1. Two lines in the meeting's notes, typed before the summary (which the app adds below them).
  await page.getByRole('button', { name: /^Notes/ }).click();
  const editor = page.getByTestId('notes-editor');
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await editor.click();
  await page.keyboard.type(NOTES_LINE);
  await page.keyboard.press('Enter');
  await page.keyboard.type(NOTES_OTHER);
  // The backend folds the live document into the meeting's notes a couple of seconds after the last frame.
  await expect(async () => {
    const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
    expect(notes).toContain(NOTES_LINE);
    expect(notes).toContain(NOTES_OTHER);
  }).toPass({ timeout: 15_000 });

  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Update the docs before the release').first()).toBeVisible();

  // 2. Publish to the host with the notes ticked and readers allowed to comment.
  await page.getByRole('button', { name: 'More for this meeting' }).click();
  await page.getByRole('menuitem', { name: /^Publish…/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Publish this meeting' });
  await dialog.getByRole('group', { name: 'Where' }).getByRole('button', { name: 'Publish host' }).click();
  await dialog.getByRole('checkbox', { name: /^Live notes/ }).check();
  await expect(dialog.locator('article')).toContainText(NOTES_LINE);
  await expect(dialog.getByRole('checkbox', { name: 'Readers may comment' })).toBeChecked();
  await dialog.getByRole('button', { name: 'Publish', exact: true }).click();
  const link = dialog.getByLabel('Link', { exact: true });
  await expect(link).toBeVisible();
  const url = await link.inputValue();
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toHaveCount(0);

  // 3. Reader A sees the notes, names themself, selects "vendor quote" and comments. The floating
  // "Comment" is offered once META says the notes take comments, so the selection is retried.
  const a = await context.newPage();
  await a.goto(url);
  await expect(a.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
  const notesA = a.locator('[data-anchor="notes"]');
  await expect(notesA).toContainText(NOTES_LINE);
  const composeA = a.getByTestId('reader-compose');
  await expect(composeA).toBeVisible({ timeout: 15_000 }); // META (comments on) has arrived
  await composeA.getByLabel('Your name, shown with your comments').fill('Ana');
  await composeA.getByRole('button', { name: 'Continue' }).click();
  await expect(composeA.getByLabel('Comment on this meeting')).toBeVisible();

  const select = (needle: string) =>
    a.evaluate((text) => {
      const section = document.querySelector('[data-anchor="notes"]')!;
      const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const i = (n.textContent ?? '').indexOf(text);
        if (i < 0) continue;
        const range = document.createRange();
        range.setStart(n, i);
        range.setEnd(n, i + text.length);
        const sel = document.getSelection()!;
        sel.removeAllRanges();
        sel.addRange(range);
        document.dispatchEvent(new Event('selectionchange'));
        return;
      }
      throw new Error('notes text not found');
    }, needle);
  await expect(async () => {
    await select(NOTES_PASSAGE);
    await expect(a.getByTestId('comment-passage')).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 20_000, intervals: [500, 1_000, 2_000] });
  await a.getByTestId('comment-passage').click();
  await expect(composeA.getByTestId('compose-anchor')).toContainText(NOTES_PASSAGE);
  await composeA.getByLabel('Your comment').fill(ON_NOTES);
  await composeA.getByRole('button', { name: 'Send', exact: true }).click();
  const threadsA = a.locator('[data-thread]');
  await expect(threadsA.filter({ hasText: ON_NOTES })).toBeVisible();
  // Once the owner's THREADS carries it back it is Ana's own to edit.
  await expect(a.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(1, ROUND_TRIP);

  // 4. The owner's meeting has a notes comment by "Ana (reader)" on the passage...
  await expect(async () => {
    const found = await notesThreads();
    expect(found).toHaveLength(1);
    expect(found[0].root.author).toBe('Ana (reader)');
    expect(found[0].root.quote).toBe(NOTES_PASSAGE);
    expect(found[0].root.detached).toBe(false);
  }).toPass({ timeout: 30_000 });
  // ... listed in the Comments card of the Summary tab, marked as from the published link ...
  const card = page.getByRole('region', { name: 'Comments' });
  const ownerThread = card.locator('[data-comment-thread]').filter({ hasText: ON_NOTES });
  await expect(ownerThread).toBeVisible(ROUND_TRIP);
  await expect(ownerThread).toContainText(/Notes: .vendor quote./);
  await expect(ownerThread.getByText('Ana (reader)', { exact: true })).toBeVisible();
  await expect(ownerThread.getByText('From the published link · replies are shown to its readers')).toBeVisible();
  // ... and the passage is marked in the notes editor; a click on it opens the thread under the editor.
  await page.getByRole('button', { name: /^Notes/ }).click();
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  const marks = editor.locator('mark.ne-comment');
  const markedText = () => marks.evaluateAll((els) => els.map((e) => e.textContent ?? '').join(''));
  await expect.poll(markedText, ROUND_TRIP).toBe(NOTES_PASSAGE);
  await marks.first().click();
  const underEditor = page.getByTestId('notes-comment-threads');
  await expect(underEditor).toContainText('Ana (reader)');
  await expect(underEditor).toContainText('From the published link');
  await expect(underEditor).toContainText(ON_NOTES);

  // 5. Reader B (another context, another reader key) sees the thread with its quote, may not touch it.
  const ctxB = await browser.newContext();
  try {
    const b = await ctxB.newPage();
    await b.goto(url);
    await expect(b.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
    const threadsB = b.locator('[data-thread]');
    const threadB = threadsB.filter({ hasText: ON_NOTES });
    await expect(threadB).toBeVisible(ROUND_TRIP);
    await expect(threadB).toContainText('Ana');
    await expect(threadB.getByTestId('thread-anchor')).toContainText(NOTES_PASSAGE);
    await expect(b.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0);
    const notesB = b.locator('[data-anchor="notes"]');
    await expect(notesB).toContainText(NOTES_LINE);

    // 6. The owner types in front of the line: the mark stays on "vendor quote", the thread is not
    // "changed", and readers get the new text (the link follows the meeting) with the thread kept.
    await editor.click();
    await page.keyboard.press('Control+Home');
    await page.keyboard.type(NOTES_PREFIX);
    await expect(async () => {
      const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
      expect(notes).toContain(NOTES_PREFIX + NOTES_LINE);
    }).toPass({ timeout: 15_000 });
    await expect(async () => {
      const [t] = await notesThreads();
      expect(t.root.changed).toBe(false);
      expect(t.root.detached).toBe(false);
      expect(t.root.quote).toBe(NOTES_PASSAGE);
    }).toPass({ timeout: 20_000 });
    await expect.poll(markedText).toBe(NOTES_PASSAGE);
    await marks.first().click();
    await expect(underEditor).toContainText(ON_NOTES);
    await expect(underEditor).not.toContainText('The text changed since');
    await expect(underEditor).not.toContainText('no longer there');
    await expect(notesB).toContainText(NOTES_PREFIX + NOTES_LINE, { timeout: 60_000 });
    await expect(threadsB.filter({ hasText: ON_NOTES })).toHaveCount(1);
    await expect(threadsB.filter({ hasText: ON_NOTES }).getByTestId('thread-anchor')).toContainText(NOTES_PASSAGE);

    // 7. The owner deletes the whole line: the thread is detached (kept, with its quote), the mark
    // is gone, and the reader's page shows the thread still but the notes without the line.
    await editor.click();
    await page.keyboard.press('Control+Home');
    await page.keyboard.press('Shift+End');
    await page.keyboard.press('Backspace');
    await expect(async () => {
      const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
      expect(notes).not.toContain(NOTES_PASSAGE);
      expect(notes).toContain(NOTES_OTHER);
    }).toPass({ timeout: 15_000 });
    await expect(async () => {
      const [t] = await notesThreads();
      expect(t.root.detached).toBe(true);
      expect(t.root.quote).toBe(NOTES_PASSAGE);
    }).toPass({ timeout: 20_000 });
    await expect(marks).toHaveCount(0);
    await page.getByRole('button', { name: 'Summary', exact: true }).click();
    const detached = card.locator('[data-comment-thread]').filter({ hasText: ON_NOTES });
    await expect(detached).toContainText('Notes · detached', ROUND_TRIP);
    await expect(detached).toContainText('On text that is no longer there');
    await expect(detached).toContainText(NOTES_PASSAGE);
    await expect(notesB).not.toContainText(NOTES_PASSAGE, { timeout: 60_000 });
    await expect(notesB).toContainText(NOTES_OTHER);
    await expect(threadsB.filter({ hasText: ON_NOTES })).toHaveCount(1);
  } finally {
    await ctxB.close();
  }
});
