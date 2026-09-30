import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

test.afterEach(async ({ request }) => {
  await request.put(`${BACKEND}/api/settings`, { data: { supervision: false } });
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'Supervised meeting')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// A line with a compliance phrase puts the meeting in the Review view until a reviewer signs off.
test('flagged lines are reviewed with a note', async ({ page, request }, info) => {
  await request.put(`${BACKEND}/api/settings`, { data: { supervision: true, compliance_phrases: 'nobody yet' } });
  await openApp(page);
  await importMeeting(page, info.outputDir, 'Supervised meeting');

  const card = page.locator('details', { hasText: 'Supervision: 1 flagged line' });
  await expect(card).toBeVisible();
  await expect(card.locator('mark')).toHaveText('Nobody yet');
  await card.getByLabel('Note (what you checked, what follows)').fill('Scheduling, not advice');
  await card.getByRole('button', { name: 'Mark reviewed' }).click();
  await expect(card.getByText('reviewed', { exact: true })).toBeVisible();

  // Other tests' meetings (the same demo transcript) are flagged too: look at this one only.
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  const row = page.getByRole('listitem').filter({ hasText: 'Supervised meeting' });
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible();
  await expect(page.getByText('Loading…')).toHaveCount(0);
  await expect(row).toHaveCount(0);
  await page.getByRole('button', { name: 'Reviewed', exact: true }).click();
  await expect(row).toContainText('Scheduling, not advice');
});
