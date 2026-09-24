import { expect, test } from '@playwright/test';
import { makeWav } from './fixtures';

// The phone page is served by the backend itself (not the UI).
const BACKEND = 'http://127.0.0.1:8018';

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

test('phone page uploads a recording that becomes a meeting', async ({ page, request }, info) => {
  await page.goto(`${BACKEND}/m`);
  await expect(page.getByRole('heading', { name: 'Record a meeting' })).toBeVisible();
  // localhost is a secure context, so recording in the page is offered too.
  await expect(page.getByRole('button', { name: 'Start recording' })).toBeVisible();
  await page.getByLabel('Name').fill('Hallway chat');
  await page.locator('#file').setInputFiles(makeWav(info.outputDir, 'hallway.wav', 5));
  await expect(page.getByText('Uploaded. Transcribing on your computer.')).toBeVisible();
  await expect(async () => {
    const sessions = await (await request.get(`${BACKEND}/api/sessions`)).json();
    const s = sessions.find((x: { name: string }) => x.name === 'Hallway chat');
    expect(s?.has_transcript).toBe(true);
  }).toPass({ timeout: 10_000 });
});
