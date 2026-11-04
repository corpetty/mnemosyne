import { expect, test, type Page } from '@playwright/test';
import { addImage, importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAME = 'shared-notes-demo';

// Live shared notes (backend services/shared_notes.py, api/notes_ws.py; the editor in
// src/lib/notes). Needs the mnemosyne-link sidecar: without it the Notes tab keeps the old
// textarea. Two browser contexts stand in for two people on the same meeting. Both connect
// with the same stored connection, so the server names both of them the same way.

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

async function openNotes(page: Page) {
  await openApp(page);
  await page.getByText(NAME).first().click();
  await page.getByRole('button', { name: /^Notes/ }).click();
  const editor = page.getByTestId('notes-editor');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await expect(page.getByTestId('notes-status')).toContainText(/Saved as you type|here/);
  return editor;
}

const lines = (editor: ReturnType<Page['getByTestId']>) => editor.locator('.ne-line, .ne-h1, .ne-bullet').allInnerTexts();

test('two people edit the same notes live', async ({ page, browser, request }, info) => {
  const ctxA = await browser.newContext({ storageState: (await page.context().storageState()) });
  const ctxB = await browser.newContext({ storageState: (await page.context().storageState()) });
  try {
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await openApp(a);
    await importMeeting(a, info.outputDir, NAME);
    const edA = await openNotes(a);
    const edB = await openNotes(b);

    // A types a heading, a bullet and a line; B sees them.
    await edA.click();
    await a.keyboard.type('# Agenda');
    await a.keyboard.press('Enter');
    await a.keyboard.type('- first point');
    await a.keyboard.press('Enter');
    await a.keyboard.press('Enter'); // an empty item leaves the list
    await a.keyboard.type('Alice writes here');
    await expect(edB.locator('.ne-h1')).toHaveText('Agenda');
    await expect(edB.locator('.ne-bullet')).toHaveText('first point');
    await expect(edB).toContainText('Alice writes here');

    // B opens a line of its own at the end; then both type at the same time, on different lines.
    await edB.click();
    await b.keyboard.press('Control+End');
    await b.keyboard.press('Enter');
    await Promise.all([
      a.keyboard.type(' and then some', { delay: 15 }),
      b.keyboard.type('Bob types meanwhile', { delay: 15 })
    ]);
    // Both end with the same text, and neither line was interleaved with the other's.
    await expect(edA).toContainText('Alice writes here and then some');
    await expect(edA).toContainText('Bob types meanwhile');
    await expect(edB).toContainText('Alice writes here and then some');
    await expect(edB).toContainText('Bob types meanwhile');
    await expect(async () => {
      expect(await lines(edA)).toEqual(await lines(edB));
    }).toPass();
    expect(await lines(edA)).toEqual(['Agenda', 'first point', 'Alice writes here and then some', 'Bob types meanwhile']);

    // Bold on a word in A shows up as <strong> in B.
    await edA.getByText('Alice writes here').click();
    await a.keyboard.press('Home');
    for (let i = 0; i < 'Alice'.length; i++) await a.keyboard.press('Shift+ArrowRight');
    await a.keyboard.press('Control+b');
    await expect(edA.locator('strong')).toHaveText('Alice');
    await expect(edB.locator('strong')).toHaveText('Alice');

    // Presence: both browsers connect as the same (desktop) user. The server lists distinct
    // names (shared_notes._presence) and the status line leaves out your own, so with one name
    // present twice neither side sees "X here": each shows "Saved as you type" and no "last edit
    // by" (the other's edits carry the same name). Different users are covered by the backend tests.
    await expect(a.getByTestId('notes-status')).toHaveText('Saved as you type');
    await expect(b.getByTestId('notes-status')).toHaveText('Saved as you type');

    // B loads the app again (openNotes navigates to "/"): the notes persist. No reload() before
    // it: WebKit, its module loads cut off by the second navigation, fails to load the dev
    // server's entry modules ("WebKit encountered an internal error") and the app never starts.
    const edB2 = await openNotes(b);
    await expect(edB2.locator('.ne-h1')).toHaveText('Agenda');
    await expect(edB2.locator('strong')).toHaveText('Alice');
    await expect(edB2).toContainText('Bob types meanwhile');

    // The document is folded into the meeting's notes text a couple of seconds after the last
    // frame: markdown, with the heading and the bold.
    const id = (await (await request.get(`${BACKEND}/api/sessions`)).json()).find((s: { name: string }) => s.name === NAME).id;
    await expect(async () => {
      const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
      expect(notes).toContain('# Agenda');
      expect(notes).toContain('**Alice**');
      expect(notes).toContain('Bob types meanwhile');
    }).toPass({ timeout: 8000 });
  } finally {
    await ctxA.close();
    await ctxB.close();
  }
});

// Live carets (phase 11, task 15): the overlay beside the editor (src/lib/notes/ui/CaretOverlay.svelte)
// draws one `remote-caret` per OTHER person, with a `.bar`, a `.flag` (class `shown` for 3 s after a
// move) and, for a range, `.selection span` rectangles. Presence travels over the notes WebSocket
// (`sel`, coalesced to 100 ms) and only while the editor has focus. Both contexts are the same
// desktop user, so the two carets have the same name; each page still sees only the other's.
test('a caret and a selection show up in the other page and go away on blur', async ({ page, browser }, info) => {
  const ctxA = await browser.newContext({ storageState: await page.context().storageState() });
  const ctxB = await browser.newContext({ storageState: await page.context().storageState() });
  try {
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await openApp(a);
    await importMeeting(a, info.outputDir, NAME);
    const edA = await openNotes(a);
    const edB = await openNotes(b);
    const carets = (p: Page) => p.getByTestId('remote-caret');

    // Two lines of text, typed by A; B is not focused, so nobody has a caret yet.
    await edA.click();
    await a.keyboard.type('Carets are shown');
    await a.keyboard.press('Enter');
    await a.keyboard.type('on the second line');
    await expect(edB).toContainText('on the second line');
    await expect(carets(a)).toHaveCount(0);
    await expect(carets(b)).toHaveCount(1, { timeout: 15_000 }); // A's caret, at the end of the text

    // A moves the caret to the start of the first line: B's bar for A moves up and left.
    const bar = carets(b).locator('.bar');
    await expect(bar).toBeVisible();
    const before = await bar.boundingBox();
    expect(before).not.toBeNull();
    await a.keyboard.press('Control+Home');
    await expect(async () => {
      const now = await bar.boundingBox();
      expect(now!.y).toBeLessThan(before!.y - 4);
    }).toPass({ timeout: 15_000 });
    // The flag with A's name shows right after the move, then fades back to the bar after ~3 s.
    await expect(carets(b).locator('.flag')).toHaveText(/\S/);
    await expect(carets(b).locator('.flag.shown')).toHaveCount(0, { timeout: 15_000 });

    // A selects "Carets": B shows the range as rectangles; a collapsed caret has none.
    await expect(carets(b).locator('.selection span')).toHaveCount(0);
    for (let i = 0; i < 'Carets'.length; i++) await a.keyboard.press('Shift+ArrowRight');
    await expect(carets(b).locator('.selection span').first()).toBeVisible({ timeout: 15_000 });
    await expect(carets(b)).toHaveCount(1);
    await expect(carets(a)).toHaveCount(0);

    // A collapses the selection again: the rectangles go, the caret stays.
    await a.keyboard.press('ArrowRight');
    await expect(carets(b).locator('.selection span')).toHaveCount(0, { timeout: 15_000 });
    await expect(bar).toBeVisible();

    // B focuses its editor too: A now shows B's caret (no flicker of its own).
    await edB.click();
    await b.keyboard.press('Control+End');
    await expect(carets(a)).toHaveCount(1, { timeout: 15_000 });
    await expect(carets(b)).toHaveCount(1);

    // A leaves the editor: focus is gone, so is its caret in B (and B's stays in A).
    await edA.evaluate((el) => (el as HTMLElement).blur());
    await expect(carets(b)).toHaveCount(0, { timeout: 15_000 });
    await expect(carets(a)).toHaveCount(1);
  } finally {
    await ctxA.close();
    await ctxB.close();
  }
});

// Images in the notes (images-in-notes task 14). A picture pasted in one window goes up to the
// backend (PUT /api/sessions/{id}/notes/media/{sha}, src/lib/app/notesMedia.ts); the image line
// travels in the document, and the other window fetches the bytes when the `notes_media` event (or
// its retry timer) says they are stored. Both pages run the same stored connection.
test('an image pasted in one window shows in another, and after a reload', async ({ page, browser, request }, info) => {
  const ctxA = await browser.newContext({ storageState: await page.context().storageState() });
  const ctxB = await browser.newContext({ storageState: await page.context().storageState() });
  try {
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await openApp(a);
    await importMeeting(a, info.outputDir, NAME);
    const edA = await openNotes(a);
    const edB = await openNotes(b);
    const id: string = (await (await request.get(`${BACKEND}/api/sessions`)).json()).find((s: { name: string }) => s.name === NAME).id;

    // A types a line, pastes a picture and captions it.
    await edA.click();
    await a.keyboard.type('Whiteboard from the call');
    await a.keyboard.press('Enter');
    await addImage(a, edA);
    const figA = edA.locator('figure.ne-image');
    await expect(figA).toHaveCount(1);
    await expect(figA).toHaveAttribute('data-state', 'ready', { timeout: 20_000 });
    await a.keyboard.type('Roadmap sketch');
    const sha = (await figA.getAttribute('data-sha'))!;
    expect(sha).toMatch(/^[0-9a-f]{64}$/);

    // B gets the line, then the bytes (loaded from the backend into an object URL), and the caption.
    const figB = edB.locator('figure.ne-image');
    await expect(figB).toHaveCount(1, { timeout: 20_000 });
    await expect(figB).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(figB).toHaveAttribute('data-sha', sha);
    await expect(figB.locator('img')).toHaveAttribute('src', /^blob:/);
    await expect(edB.locator('.ne-image-line')).toContainText('Roadmap sketch');
    await expect(edB).toContainText('Whiteboard from the call');

    // The backend holds the bytes (a WebP or JPEG, whichever the browser could encode) ...
    const media = await request.get(`${BACKEND}/api/sessions/${id}/notes/media/${sha}`);
    expect(media.ok()).toBeTruthy();
    expect(media.headers()['content-type']).toMatch(/^image\/(webp|jpeg)$/);
    // ... and, once the document is folded, the meeting's notes text names them.
    await expect(async () => {
      const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
      expect(notes).toMatch(new RegExp(`!\\[Roadmap sketch\\]\\(mnemosyne:image/${sha}\\.(webp|jpeg)@240x160\\)`));
    }).toPass({ timeout: 8000 });

    // B loads the app again (no reload(): see the note in the first test): the picture is still there.
    const edB2 = await openNotes(b);
    const figB2 = edB2.locator('figure.ne-image');
    await expect(figB2).toHaveCount(1);
    await expect(figB2).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(edB2.locator('.ne-image-line')).toContainText('Roadmap sketch');
  } finally {
    await ctxA.close();
    await ctxB.close();
  }
});

// Summaries in the notes (docs/plans/2026-11-04-summaries-in-notes.md, task 10). While recording,
// the live copilot adds its points under a "## Copilot" heading, each line led by a Copilot chip
// (backend services/copilot_notes.py); a summarize replaces those lines with a "## Summary"
// section (services/notes_summary.py) whose edits and ticks flow back into the summary. The demo
// route `POST /api/demo/copilot/{id}` stands in for "the copilot ran" (no audio): canned notes of
// one point, a decision, an action item with an owner and a question, added once.
// The demo summary (backend/mnemosyne/demo.py) is what the Summary section mirrors.
const COPILOT_POINT = 'The team went over the Waku migration';
const COPILOT_DECISION = 'Hold the beta until the docs are ready';
const COPILOT_ACTION = 'Send the beta invites';
const COPILOT_QUESTION = 'Who tests the Android build?';
const DECISION = 'Ship the Waku migration in October';
const EDITED = `${DECISION} for real`;
const ACTION = 'Update the docs before the release';

test('the copilot writes into the notes, the summary replaces it, and edits and ticks flow back', async ({ page, request }, info) => {
  test.setTimeout(150_000);
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  const id: string = (await (await request.get(`${BACKEND}/api/sessions`)).json()).find((s: { name: string }) => s.name === NAME).id;
  const detail = async () => (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()) as {
    notes: string;
    summary_data: { decisions: string[]; action_items: { text: string; done: boolean }[] } | null;
  };

  // The copilot "runs" twice: four lines the first time, nothing the second.
  const editor = await openNotes(page);
  const first = await request.post(`${BACKEND}/api/demo/copilot/${id}`);
  expect(first.ok()).toBeTruthy();
  expect(await first.json()).toEqual({ added: 4 });
  await expect(editor.locator('.ne-h2')).toHaveText('Copilot');
  const chips = editor.locator('a.ne-chip-by');
  await expect(chips).toHaveCount(4);
  await expect(chips.first()).toHaveText('Copilot');
  await expect(chips.first()).toHaveAttribute('title', 'Added by the copilot');
  const bullets = editor.locator('.ne-bullet');
  await expect(bullets).toHaveCount(3);
  await expect(bullets.nth(0)).toContainText(COPILOT_POINT);
  await expect(bullets.nth(1)).toContainText(`Decided: ${COPILOT_DECISION}`);
  await expect(bullets.nth(2)).toContainText(`Question: ${COPILOT_QUESTION}`);
  await expect(editor.locator('.ne-task')).toHaveCount(1);
  await expect(editor.locator('.ne-task')).toContainText(`${COPILOT_ACTION} (Mira)`);

  const second = await request.post(`${BACKEND}/api/demo/copilot/${id}`);
  expect(await second.json()).toEqual({ added: 0 });
  await expect(editor.locator('.ne-h2')).toHaveCount(1);
  await expect(chips).toHaveCount(4);
  await expect(editor.getByText(COPILOT_DECISION)).toHaveCount(1);
  // The folded notes text has the heading once and four chips.
  await expect(async () => {
    const notes = (await detail()).notes;
    expect(notes.match(/^## Copilot$/gm)).toHaveLength(1);
    expect(notes.match(/mnemosyne:by\/copilot/g)).toHaveLength(4);
  }).toPass({ timeout: 10_000 });

  // Summarize: the Notes tab then has a Summary section, the Copilot heading and its lines are gone.
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText(DECISION)).toBeVisible();
  const notes1 = await openNotes(page);
  await expect(notes1.locator('.ne-h2')).toHaveText('Summary');
  await expect(notes1.locator('.ne-h3')).toHaveText(['Decisions', 'Action items', 'Open questions']);
  await expect(notes1.locator('.ne-bullet').filter({ hasText: DECISION })).toHaveCount(1);
  await expect(notes1.locator('.ne-task')).toContainText(ACTION);
  await expect(notes1.locator('.ne-task')).toContainText('(SPEAKER_01)');
  await expect(notes1.locator('a.ne-chip-by')).toHaveCount(0);
  await expect(notes1).not.toContainText('Copilot');
  await expect(notes1).not.toContainText(COPILOT_DECISION);

  // Ticking the action item's box in the notes ticks it in the summary and in Tasks.
  await notes1.locator('.ne-task .ne-check').click();
  await expect(notes1.locator('.ne-task_done .ne-check')).toHaveAttribute('aria-checked', 'true');
  await expect(async () => {
    const items = (await detail()).summary_data?.action_items ?? [];
    expect(items.map((a) => [a.text, a.done])).toEqual([[ACTION, true]]);
  }).toPass({ timeout: 20_000 });
  await openApp(page);
  await page.getByText(NAME).first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByRole('button', { name: `Reopen: ${ACTION}` })).toBeVisible();
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByRole('button', { name: 'Everyone', exact: true }).click(); // Mine is the default
  await page.getByPlaceholder('Filter…').fill(ACTION);
  await page.getByRole('button', { name: 'done', exact: true }).click(); // the Open list hides it
  await expect(page.getByRole('checkbox', { name: `Done: ${ACTION}` })).toBeChecked();

  // Editing the decision's text in the notes changes it in the summary after the fold.
  const notes2 = await openNotes(page);
  await notes2.locator('.ne-bullet').filter({ hasText: DECISION }).click();
  await page.keyboard.press('End');
  await page.keyboard.type(' for real');
  await expect(notes2.locator('.ne-bullet').filter({ hasText: DECISION })).toHaveText(EDITED);
  await expect(async () => {
    expect((await detail()).summary_data?.decisions).toEqual([EDITED]);
  }).toPass({ timeout: 20_000 });
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByText(EDITED)).toBeVisible();
  await expect(page.getByText(DECISION, { exact: true })).toHaveCount(0);

  // Summarizing again keeps the edit (merged, so no "replaces your edits" question) and the notes.
  let asked = false;
  const onDialog = (d: import('@playwright/test').Dialog) => {
    asked = true;
    void d.dismiss();
  };
  page.on('dialog', onDialog);
  await page.getByRole('button', { name: 'Re-summarize' }).click();
  // Disabled while it runs ("Summarizing..."), enabled again when the job is done.
  await expect(page.getByRole('button', { name: 'Re-summarize' })).toBeEnabled();
  page.off('dialog', onDialog);
  expect(asked).toBe(false);
  await expect(page.getByText(EDITED)).toBeVisible();
  expect((await detail()).summary_data?.decisions).toEqual([EDITED]);
  const notes3 = await openNotes(page);
  await expect(notes3.locator('.ne-h2')).toHaveText('Summary');
  await expect(notes3.locator('.ne-bullet').filter({ hasText: 'Ship the Waku migration' })).toHaveText(EDITED);
  await expect(notes3.locator('.ne-bullet').filter({ hasText: 'Ship the Waku migration' })).toHaveCount(1);
});
