import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAME = 'notes-items-demo';
const TASK = 'send the deck';

// Chips and action items in the live notes (phase 11, tasks 19-29; docs/plans/2026-10-25-phase-11-tasks.md).
// A task line with a person chip is an action item of the meeting from the next fold of the notes
// (~2 s after typing stops, services/shared_notes.py FOLD_DELAY): it shows in the Tasks view
// ("from the notes") and ticking it there ticks the line in the notes (the sidecar's set_kind).
// Needs the mnemosyne-link sidecar like shared-notes.spec.ts; the e2e CI job builds it.
//
// The demo meeting has no named people, so the spec invites two (the same API publish.spec.ts
// uses); the `@` picker lists a meeting's invited people first.

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

async function sessionId(request: import('@playwright/test').APIRequestContext): Promise<string> {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  return sessions.find((x) => x.name === NAME)!.id;
}

async function openNotes(page: Page, select = true) {
  if (select) await page.getByText(NAME).first().click();
  await page.getByRole('button', { name: /^Notes/ }).click();
  const editor = page.getByTestId('notes-editor');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await expect(page.getByTestId('notes-status')).toContainText(/Saved as you type|here/);
  return editor;
}

test('an action item typed in the notes shows in Tasks, and ticking it there ticks the line', async ({ page, request }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  const id = await sessionId(request);
  const put = await request.put(`${BACKEND}/api/sessions/${id}/attendees`, { data: { attendees: ['Ada Lovelace', 'Bob Smith'] } });
  expect(put.ok()).toBeTruthy();
  await openApp(page); // reopen the meeting so the app sees its people
  const editor = await openNotes(page);

  // "/Action item" makes the line a task and opens the people picker for its owner.
  await editor.click();
  await page.keyboard.type('/Action');
  const menu = page.getByRole('listbox', { name: 'Insert' });
  await expect(menu.getByRole('option', { name: 'Action item' })).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(editor.locator('.ne-task')).toHaveCount(1);
  const people = page.getByRole('listbox', { name: 'People' });
  await expect(people).toBeVisible();
  await page.keyboard.type('Ada');
  await expect(people.getByRole('option', { name: /Ada Lovelace/ })).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(people).toHaveCount(0);
  await expect(editor.locator('.ne-task a.ne-chip-person')).toHaveText('@Ada Lovelace');
  await page.keyboard.type(TASK);
  await expect(editor.locator('.ne-task')).toContainText(TASK);
  await expect(editor.locator('.ne-task_done')).toHaveCount(0);

  // The fold turns the line into the meeting's action item: owner from the chip, text without it.
  await expect(async () => {
    const detail = await (await request.get(`${BACKEND}/api/sessions/${id}`)).json();
    const items: { text: string; owner: string | null; done: boolean }[] = detail.notes_items ?? [];
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ text: TASK, owner: 'Ada Lovelace', done: false });
  }).toPass({ timeout: 20_000 });

  // The Tasks view lists it, marked as coming from the notes, with its owner.
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByRole('button', { name: 'Everyone', exact: true }).click(); // Mine is the default
  await page.getByPlaceholder('Filter…').fill(TASK);
  const box = page.getByRole('checkbox', { name: `Done: ${TASK}` });
  await expect(box).toHaveCount(1, { timeout: 15_000 });
  const row = page.getByRole('listitem').filter({ has: box });
  await expect(row).toContainText('from the notes');
  await expect(row).toContainText('Ada Lovelace');
  await expect(box).not.toBeChecked();

  // Ticking it here ticks the line in the notes. click, not check(): the row leaves the Open list.
  await box.click();
  await page.getByRole('button', { name: 'done', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: `Done: ${TASK}` })).toBeChecked();

  const notes2 = await openNotes(page);
  await expect(notes2.locator('.ne-task_done')).toHaveCount(1, { timeout: 15_000 });
  await expect(notes2.locator('.ne-task_done .ne-check')).toHaveAttribute('aria-checked', 'true');
  await expect(notes2.locator('.ne-task_done')).toContainText(TASK);
  await expect(notes2.locator('.ne-task')).toHaveCount(0);
  const folded: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
  expect(folded).toMatch(/\[[xX]\]/);
  expect(folded).toContain('mnemosyne:person/Ada%20Lovelace');
});

test('a time chip comes from the player position', async ({ page, request, browserName }, info) => {
  // CI's WebKit never plays the demo audio (currentTime stays 0), as for meeting.spec's player
  // test, which runs in Chromium only.
  test.skip(browserName === 'webkit', 'no audio playback in CI WebKit');
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  const id = await sessionId(request);

  // Without a position in the recording "Time now" is not offered (nothing is playing yet).
  const editor0 = await openNotes(page, false);
  await editor0.click();
  await page.keyboard.type('/');
  const menu = page.getByRole('listbox', { name: 'Insert' });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole('option', { name: 'Bulleted list' })).toBeVisible();
  await expect(menu.getByRole('option', { name: /^Time now/ })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  await page.keyboard.press('Backspace'); // the "/" that opened it

  // The player is created on first use: play from the 0:07 line, then pause.
  await page.getByRole('button', { name: /^Transcript/ }).click();
  await page.getByTitle('Play from here').nth(2).click();
  await expect(page.locator('audio')).toHaveCount(1);
  await expect
    .poll(async () => page.locator('audio').evaluate((a: HTMLAudioElement) => a.currentTime))
    .toBeGreaterThan(6.5);
  await page.locator('audio').evaluate((a: HTMLAudioElement) => a.pause());

  // Now the menu offers the moment, and the chip carries the player's position.
  const editor = await openNotes(page, false);
  await editor.click();
  await page.keyboard.press('Control+End');
  await page.keyboard.type('at /time');
  await expect(menu.getByRole('option', { name: /^Time now/ })).toBeVisible();
  await page.keyboard.press('Enter');
  const chip = editor.locator('a.ne-chip-time');
  await expect(chip).toHaveCount(1);
  await expect(chip).toHaveText(/^\d+:\d{2}$/);
  const url = (await chip.getAttribute('data-chip')) ?? '';
  expect(url).toMatch(/^mnemosyne:t\/\d+\/\d+$/);
  expect(Number(url.split('/').pop())).toBeGreaterThanOrEqual(6);
  expect(Number(url.split('/').pop())).toBeLessThanOrEqual(20);

  // Clicking the chip behaves like a summary item's time link: the transcript opens on the line
  // at that moment (0:07 in the demo transcript) and the line flashes.
  await chip.click();
  const line = page.locator('[data-idx]').filter({ hasText: 'then we ship the migration in October' });
  await expect(line).toBeVisible();
  await expect(line).toHaveClass(/bg-yellow-900/); // flashed

  // The chip is part of the notes text after the fold.
  await expect(async () => {
    const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
    expect(notes).toMatch(/\]\(mnemosyne:t\/\d+\/\d+\)/);
  }).toPass({ timeout: 20_000 });
});
