import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

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

    // B reloads: the notes persist.
    await b.reload();
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
