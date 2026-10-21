import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const TASK = 'Update the docs before the release';

// The same action item in two meetings is one task (backend services/task_links.py): the demo LLM
// gives every meeting the same item, so summarizing two meetings links them.

test.describe.configure({ mode: 'serial' });

let page: Page;

async function summarize(name: string) {
  await page.getByRole('button', { name: new RegExp(name) }).first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText(TASK)).toBeVisible();
}

test.beforeAll(async ({ browser, request }, info) => {
  // Earlier specs leave their own open copies of the demo item; a done task is never linked to
  // again, so close them to start from a clean slate.
  const open: { key: string; text: string }[] = await (await request.get(`${BACKEND}/api/tasks?status=open`)).json();
  for (const t of open.filter((x) => x.text === TASK)) {
    await request.patch(`${BACKEND}/api/tasks/${encodeURIComponent(t.key)}`, { data: { done: true } });
  }
  page = await browser.newPage();
  await openApp(page);
  await importMeeting(page, info.outputDir, 'task-link-a');
  await summarize('task-link-a');
  await importMeeting(page, info.outputDir, 'task-link-b');
  await summarize('task-link-b');
});

test.afterAll(async () => {
  await page.close();
});

test('the same task in two meetings is one row, ticked in both', async () => {
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByRole('button', { name: 'Everyone', exact: true }).click(); // Mine is the default
  await page.getByPlaceholder('Filter…').fill(TASK);
  await expect(page.getByRole('checkbox', { name: `Done: ${TASK}` })).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'in 2 meetings' })).toHaveCount(1);
  // The history lists both meetings, the newest marked as the latest.
  await page.getByRole('button', { name: 'in 2 meetings' }).click();
  const history = page.getByRole('list', { name: `Meetings of: ${TASK}` });
  await expect(history.getByRole('listitem')).toHaveCount(2);
  await expect(history.getByText('(latest)')).toHaveCount(1);
  await expect(history.getByRole('button', { name: 'task-link-a', exact: true })).toBeVisible();
  await expect(history.getByRole('button', { name: 'task-link-b', exact: true })).toBeVisible();
  // click, not check(): the row leaves the Open list as soon as it is done.
  await page.getByRole('checkbox', { name: `Done: ${TASK}` }).click();
  await page.getByRole('button', { name: 'done', exact: true }).click();
  await expect(page.getByRole('button', { name: 'in 2 meetings' })).toHaveCount(1);
  await page.getByRole('button', { name: 'in 2 meetings' }).click(); // fold the history away again
  // Both meetings' summaries show it as done.
  for (const name of ['task-link-a', 'task-link-b']) {
    await page.getByRole('button', { name: new RegExp(name) }).first().click();
    await page.getByRole('button', { name: 'Summary', exact: true }).click();
    await expect(page.getByRole('button', { name: `Reopen: ${TASK}` })).toBeVisible();
  }
});

test('unlinking splits the task in two', async () => {
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByRole('button', { name: 'Everyone', exact: true }).click();
  await page.getByRole('button', { name: 'done', exact: true }).click();
  await page.getByPlaceholder('Filter…').fill(TASK);
  const rows = page.getByRole('checkbox', { name: `Done: ${TASK}` });
  const before = await rows.count();
  await page.getByRole('button', { name: 'in 2 meetings' }).click();
  await page.getByRole('button', { name: /^Unlink task-link-a/ }).click();
  await expect(rows).toHaveCount(before + 1);
  await expect(page.getByRole('button', { name: 'in 2 meetings' })).toHaveCount(0);
});
