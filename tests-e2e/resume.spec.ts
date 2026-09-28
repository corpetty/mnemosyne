import { expect, test } from '@playwright/test';

const BACKEND = 'http://127.0.0.1:8018';

// The app crashed or was restarted while the backend kept recording: the relaunched window
// shows that recording (Stop, time, live transcript so far) instead of a Record button.
test('a recording already in progress is picked up', async ({ page, request }) => {
  const created = await request.post(`${BACKEND}/api/sessions`, { data: { name: 'still-recording' } });
  const { id } = await created.json();
  // Capturing for real needs PipeWire (and would record this machine), so the backend's
  // answer is stood in for.
  await page.route(/\/api\/audio\/active$/, (route) =>
    route.fulfill({
      json: [
        {
          session_id: id,
          started_at: Date.now() / 1000 - 125,
          device_ids: [1],
          part: 0,
          live: true,
          live_segments: [
            { source: 'mic', segment: { text: 'Said before the crash.', speaker: 'Me', start: 3, end: 5 } }
          ]
        }
      ]
    })
  );
  await page.goto('/');
  await expect(page.getByText('picked up the recording in progress')).toBeVisible();
  await expect(page.getByTitle('Stop recording (Ctrl+S)')).toBeVisible();
  await expect(page.getByTitle('Go to the recording')).toContainText(/2:0\d/);
  await expect(page.getByText('Said before the crash.')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'still-recording' })).toBeVisible();
});
