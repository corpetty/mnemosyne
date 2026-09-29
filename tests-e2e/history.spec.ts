import { expect, test } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { importMeeting, makeWav, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
// The demo backend's data folder (tests-e2e/start-backend.sh).
const RECORDINGS = join(import.meta.dirname, '.data', 'db', 'recordings');

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'history-check')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// A meeting shows what it is made of, and audio an interrupted recording left behind can be
// saved into it from there.
test('parts and history, and saving audio an interruption left behind', async ({ page, request }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'history-check');
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  const id = sessions.find((s) => s.name === 'history-check')!.id;

  // What a backend killed mid-recording leaves in the meeting's folder: a WAV and its manifest.
  const folder = join(RECORDINGS, id);
  makeWav(folder, 'lost0001_device_1.wav', 3);
  const track = { device_id: 1, device_name: 'Desk mic', source: 'mic', wav: 'lost0001_device_1.wav' };
  writeFileSync(join(folder, 'recording-lost0001.json'), JSON.stringify({ recording_id: 'lost0001', part: 1, tracks: [track] }));

  await page.getByRole('navigation', { name: 'Meeting' }).getByRole('button', { name: /^Recording/ }).click();
  const card = page.getByLabel('Parts and history');
  await expect(card).toContainText('1 recording not saved yet');
  await expect(card).toContainText('0:03 of audio recorded as part 2 is not in the meeting yet');
  await expect(card.getByLabel('Parts')).toContainText('Part 1');

  await card.getByRole('button', { name: 'Save it into the meeting' }).click();
  await expect(card).toContainText('2 parts');
  await expect(card).toContainText('all audio in place');
  await expect(card.getByLabel('Parts')).toContainText('recovered after an interruption');
  await expect(card.getByLabel('What happened')).toContainText('Recovered part 2 (0:03)');
});
