import { expect, test, type Page } from '@playwright/test';

// Each test saves one screenshot to site/shots/out/; site/shots/make.sh turns them into WebP
// in site/assets/.
const OUT = 'site/shots/out';
const BACKEND = 'http://127.0.0.1:8048';

async function openMeeting(page: Page, tab: 'Summary' | 'Transcript') {
  await page.goto('/');
  await page.getByText('Atlas 2.0 launch plan').first().click();
  await page.getByRole('button', { name: new RegExp(`^${tab}`) }).first().click();
  await page.waitForTimeout(600);
}

test('summary', async ({ page }) => {
  await openMeeting(page, 'Summary');
  await expect(page.getByText('Sync status and first-sync speed')).toBeVisible();
  await page.screenshot({ path: `${OUT}/summary.png` });
});

test('transcript', async ({ page }) => {
  await openMeeting(page, 'Transcript');
  await expect(page.getByText('forty thousand edits')).toBeVisible();
  await page.screenshot({ path: `${OUT}/transcript.png` });
});

test('tasks', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/tasks.png` });
});

// A meeting being recorded: the live transcript beside the copilot. Capturing for real needs
// PipeWire (and would record this machine), so the backend's answers are stood in for.
test('recording', async ({ page, request }) => {
  const sessions = await (await request.get(`${BACKEND}/api/sessions`)).json();
  const id = sessions.find((s: { name: string }) => s.name === 'Atlas 2.0 launch plan').id;
  const session = await (await request.get(`${BACKEND}/api/sessions/${id}`)).json();
  const live = session.transcript.slice(0, 11).map((segment: object) => ({ source: 'system', segment }));
  await page.route(/\/api\/devices$/, (route) =>
    route.fulfill({
      json: [
        { id: 1, name: 'mic', description: 'Studio microphone', media_class: 'Audio/Source', is_input: true, is_output: false, is_monitor: false, is_echo_cancelled: false },
        { id: 2, name: 'out', description: 'Speakers', media_class: 'Audio/Sink', is_input: false, is_output: true, is_monitor: false, is_echo_cancelled: false }
      ]
    })
  );
  await page.route(/\/api\/audio\/active$/, (route) =>
    route.fulfill({
      json: [{ session_id: id, started_at: Date.now() / 1000 - 4 * 60 - 12, device_ids: [1, 2], part: 0, live: true, live_segments: live, problems: {} }]
    })
  );
  await page.route(new RegExp(`/api/sessions/${id}/copilot$`), (route) =>
    route.fulfill({
      json: {
        session_id: id,
        summary: ['Sync is feature complete and passed the weekend soak test', 'First sync on large workspaces (~90 s) is the remaining risk'],
        decisions: ['Batching change goes in before the beta'],
        action_items: [{ text: 'Surface sync progress counts in the UI', owner: 'Daniel Okafor' }],
        open_questions: ['Does the progress indicator need design review?'],
        lines: 11,
        updated_at: new Date().toISOString()
      }
    })
  );
  await page.goto('/');
  await expect(page.getByText('Studio microphone')).toBeVisible();
  await expect(page.getByText('picked up the recording')).toBeHidden({ timeout: 10_000 }); // the toast
  await page.screenshot({ path: `${OUT}/recording.png` });
});
