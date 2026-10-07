import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// Everything in a summary can be changed after it is made (backend services/summary_edit.py).
// The demo LLM gives a fixed summary (backend/mnemosyne/demo.py) to edit.

test.describe.configure({ mode: 'serial' });

let page: Page;

test.beforeAll(async ({ browser }, info) => {
  page = await browser.newPage();
  await openApp(page);
  await importMeeting(page, info.outputDir, 'summary-edit');
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Ship the Waku migration in October')).toBeVisible();
});

test.afterAll(async () => {
  await page.close();
});

test('edit the text, tags, decisions, items, chapters and questions', async () => {
  await page.getByTitle(/^Edit the summary/).click();
  await page.getByLabel('Summary text').fill('We agreed to ship in October and to fix the docs first.');
  await page.getByLabel('Remove topic waku').click();
  await page.getByLabel('Add a topic').fill('docs');
  await page.getByLabel('Add a topic').press('Enter');
  await page.getByLabel('Decision 1', { exact: true }).fill('Ship the Waku migration in late October');
  await page.getByLabel('Remove question 1').click();
  await page.getByRole('button', { name: '+ Add action item' }).click();
  await page.getByLabel('Action item 2', { exact: true }).fill('Book the release call');
  await page.getByLabel('Owner of action item 2').fill('Ann');
  await page.getByLabel('Due date of action item 2').fill('2026-10-30');
  await page.getByLabel('Chapter 2', { exact: true }).fill('Who owns mobile');
  await page.getByRole('button', { name: 'Save summary' }).click();

  await expect(page.getByText('Summary saved')).toBeVisible();
  await expect(page.getByText('We agreed to ship in October and to fix the docs first.')).toBeVisible();
  await expect(page.getByText('Ship the Waku migration in late October')).toBeVisible();
  await expect(page.getByText('Who owns the mobile regression?')).toHaveCount(0);
  await expect(page.getByText('Book the release call')).toBeVisible();
  await expect(page.getByRole('button', { name: /Who owns mobile/ })).toBeVisible();
  await expect(page.getByText('docs', { exact: true })).toBeVisible();
  await expect(page.getByText('waku', { exact: true })).toHaveCount(0);
  // The edited decision still points at where it came up.
  await expect(page.getByTitle('Show where this came up in the transcript').first()).toHaveText('0:07');
  await expect(page.getByText(/· edited /)).toBeVisible();
});

test('edits are kept, and summarizing again asks first', async () => {
  await page.reload();
  await page.getByRole('button', { name: /summary-edit/ }).first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByText('Book the release call')).toBeVisible();
  page.once('dialog', (d) => d.dismiss());
  await page.getByRole('button', { name: 'Re-summarize' }).click();
  await expect(page.getByText('Book the release call')).toBeVisible(); // nothing replaced
});

test('cancel leaves the summary as it was', async () => {
  await page.getByTitle(/^Edit the summary/).click();
  await page.getByLabel('Summary text').fill('Throwaway');
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByText('We agreed to ship in October and to fix the docs first.')).toBeVisible();
});

test('the follow-up draft is kept as edited', async () => {
  await page.getByRole('button', { name: 'Draft follow-up' }).click();
  const draft = page.getByLabel('Follow-up draft');
  await expect(draft).toHaveValue(/^Subject: Release planning follow-up/);
  await draft.fill('Hi all, the release call is on the 30th.');
  await page.getByRole('button', { name: 'Save draft' }).click();
  await expect(page.getByText('Follow-up draft saved')).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: /summary-edit/ }).first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByLabel('Follow-up draft')).toHaveValue('Hi all, the release call is on the 30th.');
});

test('edit a task from the Tasks view', async () => {
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByPlaceholder('Filter…').fill('release call');
  await page.getByRole('button', { name: 'Edit task: Book the release call' }).click();
  await page.getByLabel('Task text').fill('Book the release call with QA');
  await page.getByLabel('Task owner').fill('Bob');
  await page.getByLabel('Task text').press('Enter');
  await page.getByPlaceholder('Filter…').fill('');
  await expect(page.getByText('Book the release call with QA')).toBeVisible();
  // The meeting's summary has it too.
  await page.getByRole('button', { name: /summary-edit/ }).first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByText('Book the release call with QA')).toBeVisible();
  await expect(page.getByText('· Bob')).toBeVisible();
});
