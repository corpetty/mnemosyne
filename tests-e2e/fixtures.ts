import { expect, type Page } from '@playwright/test';
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
  await expect(page.getByText('API', { exact: true })).toBeVisible();
}

/** Import a file through the sidebar and wait until its transcript is shown. */
export async function importMeeting(page: Page, dir: string, name: string) {
  const file = makeWav(dir, `${name}.wav`);
  await page.locator('input[type=file]').setInputFiles(file);
  await expect(page.getByRole('heading', { name })).toBeVisible();
  await page.getByRole('button', { name: /^Transcript/ }).click();
  await expect(page.getByText('The Waku migration is code complete.')).toBeVisible();
}
