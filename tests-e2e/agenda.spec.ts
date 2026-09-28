import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'agenda-check')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// Before a meeting: write the points to get through (the copilot ticks them off live).
test('write an agenda and tick a point off', async ({ page, request }) => {
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'agenda-check' } });
  await openApp(page);
  await page.getByText('agenda-check').first().click();
  const agenda = page.getByRole('region', { name: 'Agenda' });
  const input = agenda.getByPlaceholder('Add a point, then Enter');
  await input.fill('Beta date');
  await input.press('Enter');
  await expect(agenda).toContainText('0 of 1 covered');
  await input.fill('Pricing');
  await input.press('Enter');
  await expect(agenda).toContainText('0 of 2 covered');
  await agenda.getByLabel('Covered: Beta date').check();
  await expect(agenda).toContainText('1 of 2 covered');
  await page.reload();
  await page.getByText('agenda-check').first().click();
  await expect(page.getByRole('region', { name: 'Agenda' })).toContainText('1 of 2 covered');
});
