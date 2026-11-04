import { expect, type Locator, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

/** A short mono 16 kHz WAV (a quiet tone); the demo transcriber ignores its content. */
export function makeWav(dir: string, name: string, seconds = 20): string {
  const rate = 16000;
  const n = rate * seconds;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0);
  buf.writeUInt32LE(36 + n * 2, 4);
  buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); // PCM
  buf.writeUInt16LE(1, 22); // mono
  buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28);
  buf.writeUInt16LE(2, 32);
  buf.writeUInt16LE(16, 34);
  buf.write('data', 36);
  buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(3000 * Math.sin((2 * Math.PI * 440 * i) / rate)), 44 + i * 2);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, name);
  writeFileSync(path, buf);
  return path;
}

/** Open the app and wait for the backend connection. */
export async function openApp(page: Page) {
  await page.goto('/');
  await expect(page.getByTitle('Connected', { exact: true })).toBeVisible();
}

/** Import a file through the sidebar and wait until its transcript is shown. */
export async function importMeeting(page: Page, dir: string, name: string) {
  const file = makeWav(dir, `${name}.wav`);
  await page.locator('input[type=file]').setInputFiles(file);
  await expect(page.getByRole('heading', { name })).toBeVisible();
  await page.getByRole('button', { name: /^Transcript/ }).click();
  await expect(page.getByText('The Waku migration is code complete.')).toBeVisible();
}

/**
 * A 240x160 PNG (wide enough to show the failed-upload buttons under it) drawn on a canvas in the
 * page, as base64. `n` makes it another picture (n pixels wider, another colour), so two images
 * in one document are two different blobs.
 */
export async function drawPng(page: Page, n = 0): Promise<string> {
  return page.evaluate((k) => {
    const c = document.createElement('canvas');
    c.width = 240 + k;
    c.height = 160;
    const g = c.getContext('2d')!;
    g.fillStyle = `hsl(${(k * 67) % 360} 70% 45%)`;
    g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = '#fff';
    g.fillRect(20, 20, 60, 40);
    return c.toDataURL('image/png').split(',')[1];
  }, n);
}

/**
 * Paste `drawPng(n)` into an editor as the browser would deliver it: a ClipboardEvent whose
 * DataTransfer holds a File. False when the engine did not keep the file in the DataTransfer
 * (WebKit may not): nothing was dispatched, use `chooseImage`.
 */
export async function pasteImage(page: Page, editor: Locator, n = 0): Promise<boolean> {
  const b64 = await drawPng(page, n);
  return editor.evaluate(
    (el, [data, k]) => {
      const bytes = Uint8Array.from(atob(data), (ch) => ch.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], `picture-${k}.png`, { type: 'image/png' }));
      if (!dt.files.length) return false;
      el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
      return true;
    },
    [b64, n] as const
  );
}

/**
 * Add `drawPng(n)` through the `/Image` item of the insert menu and the browser's file chooser,
 * at the caret (on an empty line, where the menu opens). The editor must have focus.
 */
export async function chooseImage(page: Page, n = 0) {
  const b64 = await drawPng(page, n);
  const chooser = page.waitForEvent('filechooser');
  await page.keyboard.type('/ima');
  await expect(page.getByRole('listbox', { name: 'Insert' }).getByRole('option', { name: /^Image/ })).toBeVisible();
  await page.keyboard.press('Enter');
  await (await chooser).setFiles({ name: `picture-${n}.png`, mimeType: 'image/png', buffer: Buffer.from(b64, 'base64') });
}

/** Put an image in the editor: a paste, else (an engine that drops pasted files) the chooser. */
export async function addImage(page: Page, editor: Locator, n = 0) {
  if (!(await pasteImage(page, editor, n))) await chooseImage(page, n);
}
