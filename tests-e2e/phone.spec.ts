import { expect, test, type APIRequestContext } from '@playwright/test';
import { makeWav } from './fixtures';

// The phone page is served by the backend itself (not the UI).
const BACKEND = 'http://127.0.0.1:8018';

test.use({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
  // Chromium's fake microphone (a beep), so the page can record without a real one.
  launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] },
  permissions: ['microphone']
});

async function transcribed(request: APIRequestContext, name: string) {
  await expect(async () => {
    const sessions = await (await request.get(`${BACKEND}/api/sessions`)).json();
    const s = sessions.find((x: { name: string }) => x.name === name);
    expect(s?.has_transcript).toBe(true);
  }).toPass({ timeout: 10_000 });
}

test('phone page uploads a recording that becomes a meeting', async ({ page, request }, info) => {
  await page.goto(`${BACKEND}/m`);
  await expect(page.getByRole('heading', { name: 'Record a meeting' })).toBeVisible();
  // localhost is a secure context, so recording in the page is offered too.
  await expect(page.getByRole('button', { name: 'Start recording' })).toBeVisible();
  await page.getByLabel('Name').fill('Hallway chat');
  await page.locator('#file').setInputFiles(makeWav(info.outputDir, 'hallway.wav', 5));
  await expect(page.getByText('Uploaded. Transcribing on your computer.')).toBeVisible();
  await transcribed(request, 'Hallway chat');
});

// Issue #4: a recording made in the page was never sent.
test('phone page sends what it records itself', async ({ page, request }) => {
  await page.goto(`${BACKEND}/m`);
  await page.getByLabel('Name').fill('Recorded on the phone');
  await page.getByRole('button', { name: 'Start recording' }).click();
  await expect(page.getByText('Recording…')).toBeVisible();
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: 'Stop and send' }).click();
  await expect(page.getByText('Uploaded. Transcribing on your computer.')).toBeVisible();
  await transcribed(request, 'Recorded on the phone');
});
