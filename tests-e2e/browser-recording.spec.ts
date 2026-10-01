import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

// Recording in the browser (a team server; src/lib/app/browser-capture.ts), here with the UI
// from Vite and `mnemosyne.recordInBrowser` set. Chromium's fake microphone plays a beep; the
// call-audio share dialog cannot be driven headless, so only the microphone is recorded.
const BACKEND = 'http://127.0.0.1:8018';

test.use({
  launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] },
  permissions: ['microphone']
});

test('the browser records the microphone and the meeting keeps it', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('mnemosyne.recordInBrowser', '1'));
  await openApp(page);
  await page.getByRole('button', { name: 'New meeting' }).first().click();
  const call = page.getByRole('checkbox', { name: /The call's audio/ });
  await expect(call).toBeVisible();
  if (await call.isChecked()) await call.uncheck();

  await page.getByRole('button', { name: 'Record', exact: true }).click();
  await expect(page.getByLabel('Recording from')).toContainText('Fake');
  await page.waitForTimeout(3000); // three seconds of beeps, sent as they are recorded
  await page.getByRole('button', { name: 'Stop', exact: true }).first().click();

  // Saved as the meeting's part, from the browser's microphone, about three seconds long.
  await expect(async () => {
    const sessions = await (await request.get(`${BACKEND}/api/sessions`)).json();
    const newest = sessions[0];
    const session = await (await request.get(`${BACKEND}/api/sessions/${newest.id}`)).json();
    expect(session.recordings).toHaveLength(1);
    expect(session.recordings[0].source).toBe('mic');
    expect(session.recordings[0].device_name).toContain('Fake');
    const history = await (await request.get(`${BACKEND}/api/sessions/${newest.id}/history`)).json();
    const part = history.parts[0];
    expect(part.seconds).toBeGreaterThan(2);
    expect(part.seconds).toBeLessThan(6);
  }).toPass({ timeout: 15_000 });
});
