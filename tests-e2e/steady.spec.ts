import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'steady-check')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// An open meeting sits still: no request loops while nothing changes.
test('an open transcript does not keep asking the backend', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'steady-check');
  const calls: string[] = [];
  page.on('request', (r) => {
    if (r.url().includes('/api/')) calls.push(new URL(r.url()).pathname);
  });
  await page.waitForTimeout(3000);
  const speakers = calls.filter((c) => c.endsWith('/speakers')).length;
  expect(speakers, calls.join('\n')).toBeLessThanOrEqual(1);
  expect(calls.length, calls.join('\n')).toBeLessThan(10);
});
