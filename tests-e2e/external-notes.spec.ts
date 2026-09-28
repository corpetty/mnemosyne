import { expect, test } from '@playwright/test';
import { openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'notes-only')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// A meeting that wasn't recorded, only Gemini took notes: summarize it from those.
test("a meeting is summarized from Gemini's notes", async ({ page, request }) => {
  await request.post(`${BACKEND}/api/sessions`, { data: { name: 'notes-only' } });
  await openApp(page);
  await page.getByText('notes-only').first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  const card = page.getByRole('region', { name: 'Notes from other assistants' });
  await card.getByRole('button', { name: 'Add notes…' }).click();
  await card.getByPlaceholder('Paste the notes here').fill('Notes by Gemini\n\nSummary\nWe will ship the migration in October.');
  await card.getByRole('button', { name: 'Add', exact: true }).click();
  await expect(card).toContainText('Gemini');
  await expect(card).toContainText('Summarize to make a summary from them.');
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('The team reviewed the release.').first()).toBeVisible();
});
