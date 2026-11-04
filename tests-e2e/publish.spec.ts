import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { addImage, importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAME = 'publish-demo';

// The demo summary is the same for every meeting: a summarized meeting left behind would add a
// second copy of its decision and task to the specs that run after this one.
test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) {
    await request.delete(`${BACKEND}/api/sessions/${s.id}/publication`).catch(() => {});
    await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  }
});

async function summarize(page: Page) {
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Update the docs before the release').first()).toBeVisible();
}

async function openPublish(page: Page) {
  await page.getByRole('button', { name: 'More for this meeting' }).click();
  await page.getByRole('menuitem', { name: /^Publish…/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Publish this meeting' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('heading', { name: NAME, level: 1 })).toBeVisible(); // the preview
  return dialog;
}

test('publish, view with the key, update, unpublish', async ({ page, context, request }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  await summarize(page);

  // The demo meeting has no named people: invite two, and reopen it so the app sees them.
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  const id = sessions.find((x) => x.name === NAME)!.id;
  const put = await request.put(`${BACKEND}/api/sessions/${id}/attendees`, { data: { attendees: ['Ana Lopez', 'Bo Chen'] } });
  expect(put.ok()).toBeTruthy();
  await openApp(page);
  await page.getByText(NAME, { exact: true }).first().click();
  await expect(page.getByText('Invited: Ana Lopez, Bo Chen')).toBeVisible();

  const dialog = await openPublish(page);
  const decisions = dialog.getByRole('heading', { name: 'Decisions', level: 2 });
  await expect(decisions).toBeVisible();
  await dialog.getByRole('checkbox', { name: /^Decisions/ }).uncheck();
  await expect(decisions).toHaveCount(0);

  await dialog.getByRole('group', { name: 'Names' }).getByRole('button', { name: 'Person 1' }).click();
  await expect(dialog.locator('article header p').first()).toContainText('Person 1, Person 2');
  await expect(dialog.locator('article header p').first()).not.toContainText('Ana');

  await dialog.getByRole('group', { name: 'Where' }).getByRole('button', { name: 'This computer' }).click();
  await dialog.getByRole('button', { name: 'Publish', exact: true }).click();
  const link = dialog.getByLabel('Link');
  await expect(link).toBeVisible();
  const url = await link.inputValue();
  expect(url).toContain('#');
  const key = url.split('#')[1];
  expect(key.length).toBeGreaterThan(10);
  await dialog.getByRole('button', { name: 'Close' }).click();

  // A reader's page: only the viewer's own paths, never the key.
  const reader = await context.newPage();
  const requests: string[] = [];
  reader.on('request', (r) => requests.push(r.url()));
  await reader.goto(url);
  await expect(reader.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
  await expect(reader.getByText('Waku').first()).toBeVisible();
  await expect(reader.getByRole('heading', { name: 'Decisions' })).toHaveCount(0);
  expect(requests.length).toBeGreaterThan(0);
  for (const r of requests) {
    const u = new URL(r);
    expect(u.origin, r).toBe(BACKEND);
    expect(u.pathname, r).toMatch(/^\/(p|pub)\//);
    expect(r, r).not.toContain(key);
  }

  // The header chip, then an update with Decisions on.
  const chip = page.getByRole('button', { name: /^Published · v1/ });
  await expect(chip).toBeVisible();
  await chip.click();
  const dialog2 = page.getByRole('dialog', { name: 'Publish this meeting' });
  await dialog2.getByRole('checkbox', { name: /^Decisions/ }).check();
  await dialog2.getByRole('button', { name: 'Update', exact: true }).click();
  await expect(page.getByText('Updated: version 2')).toBeVisible();
  await dialog2.getByRole('button', { name: /^(Close|Cancel)$/ }).click();
  await expect(page.getByRole('button', { name: /^Published · v2/ })).toBeVisible();

  await reader.reload();
  await expect(reader.getByRole('heading', { name: 'Decisions' })).toBeVisible();

  // Unpublish.
  await page.getByRole('button', { name: /^Published · v2/ }).click();
  const dialog3 = page.getByRole('dialog', { name: 'Publish this meeting' });
  await dialog3.getByRole('button', { name: 'Unpublish', exact: true }).click();
  await dialog3.getByRole('button', { name: 'Yes', exact: true }).click();
  await expect(page.getByText('Unpublished. The link no longer works.')).toBeVisible();
  await reader.reload();
  await expect(reader.getByRole('heading', { name: 'No longer published' })).toBeVisible();
});

test('save as a password-protected file and open it from disk', async ({ page, context }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  await summarize(page);

  const dialog = await openPublish(page);
  await dialog.getByRole('group', { name: 'Where' }).getByRole('button', { name: 'A file' }).click();
  await dialog.getByLabel(/^Password/).fill('correct horse battery');
  const download = page.waitForEvent('download');
  await dialog.getByRole('button', { name: 'Save file' }).click();
  const saved = join(info.outputDir, 'meeting.html');
  await (await download).saveAs(saved);
  expect(readFileSync(saved, 'utf8')).not.toContain('Waku migration');

  const reader = await context.newPage();
  await reader.goto(`file://${saved}`);
  const field = reader.getByLabel('Password');
  await expect(field).toBeVisible();
  await field.fill('not the password');
  await reader.getByRole('button', { name: 'Open', exact: true }).click();
  await expect(reader.getByRole('alert')).toBeVisible();
  await field.fill('correct horse battery');
  await reader.getByRole('button', { name: 'Open', exact: true }).click();
  await expect(reader.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
});

// Images in the notes (images-in-notes task 14; PublishDialog.svelte, viewer/src/notes.ts). The
// notes need the link sidecar (the Notes tab keeps a textarea without it). The dialog lists each
// image of the notes with a checkbox (all on); the viewer draws the ticked ones from their decrypted
// blob URL and a left-out one as `[image: caption]`.
test('images in the notes: listed with a checkbox, shown to readers, and left out as their caption', async ({ page, context, request }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, NAME);
  await summarize(page);
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  const id = sessions.find((x) => x.name === NAME)!.id;

  // One captioned picture in the meeting's notes.
  await page.getByRole('button', { name: /^Notes/ }).click();
  const editor = page.getByTestId('notes-editor');
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await editor.click();
  await addImage(page, editor);
  await expect(editor.locator('figure.ne-image')).toHaveAttribute('data-state', 'ready', { timeout: 20_000 });
  await page.keyboard.type('Whiteboard photo');
  // The dialog reads the meeting's notes text, which the backend folds a couple of seconds after the last frame.
  await expect(async () => {
    const notes: string = (await (await request.get(`${BACKEND}/api/sessions/${id}`)).json()).notes;
    expect(notes).toContain('![Whiteboard photo](mnemosyne:image/');
  }).toPass({ timeout: 10_000 });

  const dialog = await openPublish(page);
  await dialog.getByRole('checkbox', { name: /^Live notes/ }).check();
  const images = dialog.getByRole('region', { name: 'Images in the notes' });
  const row = images.getByRole('checkbox', { name: /Whiteboard photo/ });
  await expect(images.getByRole('checkbox')).toHaveCount(1);
  await expect(row).toBeChecked();
  // The preview shows the picture too (a blob URL fetched with the connection's headers).
  const previewImg = dialog.locator('article .mn-image img');
  await expect(previewImg).toHaveCount(1);
  await expect(previewImg).toHaveAttribute('src', /^blob:/);

  await dialog.getByRole('group', { name: 'Where' }).getByRole('button', { name: 'This computer' }).click();
  await dialog.getByRole('button', { name: 'Publish', exact: true }).click();
  const url = await dialog.getByLabel('Link').inputValue();
  const key = url.split('#')[1];
  await dialog.getByRole('button', { name: 'Close' }).click();

  // A reader sees the picture, decrypted in the page; no request carries the key.
  const reader = await context.newPage();
  const requests: string[] = [];
  reader.on('request', (r) => requests.push(r.url()));
  await reader.goto(url);
  await expect(reader.getByRole('heading', { name: NAME, level: 1 })).toBeVisible();
  const shown = reader.locator('article .mn-image img');
  await expect(shown).toHaveCount(1);
  await expect(shown).toHaveAttribute('src', /^blob:/);
  await expect(shown).toHaveAttribute('alt', 'Whiteboard photo');
  await expect.poll(() => shown.evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBe(240);
  await expect(reader.getByText('[image: Whiteboard photo]')).toHaveCount(0);
  for (const r of requests) {
    const u = new URL(r);
    expect(u.origin, r).toBe(BACKEND);
    expect(r, r).not.toContain(key);
  }

  // Leave the image out and update: readers get its caption in brackets, no picture.
  await page.getByRole('button', { name: /^Published · v1/ }).click();
  const dialog2 = page.getByRole('dialog', { name: 'Publish this meeting' });
  const row2 = dialog2.getByRole('region', { name: 'Images in the notes' }).getByRole('checkbox', { name: /Whiteboard photo/ });
  await expect(row2).toBeChecked();
  await row2.uncheck();
  await expect(dialog2.locator('article .mn-image img')).toHaveCount(0);
  await expect(dialog2.locator('article')).toContainText('[image: Whiteboard photo]');
  await dialog2.getByRole('button', { name: 'Update', exact: true }).click();
  await expect(page.getByText('Updated: version 2')).toBeVisible();
  await dialog2.getByRole('button', { name: /^(Close|Cancel)$/ }).click();

  await reader.reload();
  await expect(reader.getByText('[image: Whiteboard photo]')).toBeVisible();
  await expect(reader.locator('article .mn-image img')).toHaveCount(0);
});
