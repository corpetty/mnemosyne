import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const TASK = 'Update the docs before the release';
const BACKEND = 'http://127.0.0.1:8018';

// The demo summary is the same for every meeting: a summarized meeting left behind would add a
// second copy of its decision and task to the specs that run after this one.
test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === 'comments-demo')) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

// Comments on transcript lines and summary blocks (backend services/comments.py): a thread with a
// reply, resolved, and one on an action item; the Comments card under the summary lists both.

test('comment on a line, reply, resolve, then on an action item', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'comments-demo');

  // A comment on line 1.
  await page.locator('[data-idx="0"]').hover();
  await page.getByRole('button', { name: 'Comment on line 1' }).click();
  const lineBox = page.locator('[data-session]').filter({ has: page.getByPlaceholder('Add a comment') });
  await page.getByPlaceholder('Add a comment').fill('Check this claim with the team');
  await lineBox.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(page.getByRole('button', { name: '1 comment on line 1' })).toHaveText('1');
  await expect(page.getByText('Check this claim with the team')).toBeVisible();

  // A reply stays in the same thread.
  await page.getByRole('button', { name: 'Reply', exact: true }).click();
  await page.getByPlaceholder('Reply').fill('Agreed, asking on Monday');
  await page
    .locator('[data-session]')
    .filter({ has: page.getByPlaceholder('Reply') })
    .getByRole('button', { name: 'Comment', exact: true })
    .click();
  await expect(page.getByText('Agreed, asking on Monday')).toBeVisible();
  await expect(page.getByRole('button', { name: '1 comment on line 1' })).toHaveText('1');

  // Resolve it: the thread folds to one line; expanding it offers Reopen.
  await page.getByRole('button', { name: 'Resolve', exact: true }).click();
  const folded = page.getByRole('button', { name: /^✓ Resolved by .*Check this claim with the team/ });
  await expect(folded).toBeVisible();
  await folded.click();
  await expect(page.getByRole('button', { name: 'Reopen', exact: true })).toBeVisible();

  // Summarize, then comment on the action item.
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText(TASK).first()).toBeVisible();
  await page.getByText(TASK).first().hover();
  await page.getByRole('button', { name: 'Comment on this action item' }).first().click();
  await page.getByPlaceholder('Add a comment').fill('Who owns the docs?');
  await page
    .locator('[data-session]')
    .filter({ has: page.getByPlaceholder('Add a comment') })
    .getByRole('button', { name: 'Comment', exact: true })
    .click();
  await expect(page.getByRole('button', { name: '1 comment on this action item' })).toBeVisible();

  // The Comments card lists both threads (the resolved one under All).
  const card = page.getByRole('region', { name: 'Comments' });
  await card.getByRole('button', { name: 'All', exact: true }).click();
  await expect(card.getByText('Check this claim with the team')).toBeVisible();
  await expect(card.getByText('Who owns the docs?')).toBeVisible();
  await expect(card.getByText('Transcript line 1')).toBeVisible();
  await expect(card.getByText(`Action item: ${TASK}`)).toBeVisible();
});
