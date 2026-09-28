import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

test.afterEach(async ({ request }) => {
  await request.put(`${BACKEND}/api/settings`, { data: { meeting_types: [] } });
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'Daily standup (types)')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// A type is defined once; meetings with its words in the title get it.
test('a meeting type is given by title and can be changed', async ({ page, request }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await page.getByRole('button', { name: 'Add a meeting type' }).click();
  await page.getByPlaceholder('e.g. Standup').fill('Standup');
  await page.getByPlaceholder('standup, daily sync').fill('standup');
  await page.getByRole('button', { name: 'Save meeting types' }).click();
  await expect(page.getByText('Meeting types saved')).toBeVisible();

  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'Daily standup (types)' } });
  await page.reload();
  await page.getByText('Daily standup (types)').first().click();
  const picker = page.getByLabel('Meeting type', { exact: true });
  await expect(picker).toHaveValue('Standup');
  await picker.selectOption('none');
  await expect(picker).toHaveValue('none');
});
