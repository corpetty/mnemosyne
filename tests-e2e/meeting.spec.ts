import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// One meeting, taken through the whole flow in order. The backend runs in demo mode:
// every import yields the same six-line, two-speaker transcript and the LLM gives
// fixed replies (backend/mnemosyne/demo.py).

test.describe.configure({ mode: 'serial' });

let page: Page;

test.beforeAll(async ({ browser }, info) => {
  page = await browser.newPage();
  await openApp(page);
  await importMeeting(page, info.outputDir, 'release-sync');
});

test.afterAll(async () => {
  await page.close();
});

test('import shows the diarized transcript', async () => {
  await expect(page.getByRole('button', { name: /^Transcript\s*\(6\)/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'SPEAKER_00', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'SPEAKER_01', exact: true })).toBeVisible();
  // Who spoke when: six alternating turns; the talk-time table opens on click.
  await expect(page.getByLabel('Who spoke when').getByRole('button')).toHaveCount(6);
  await page.getByRole('button', { name: /Talk time/ }).click();
  await expect(page.getByText('3 turns').first()).toBeVisible();
});

test('rename a speaker', async () => {
  await page.getByRole('button', { name: 'SPEAKER_01', exact: true }).click();
  await page.getByPlaceholder('Name').fill('Alice');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Alice', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'SPEAKER_01', exact: true })).toHaveCount(0);
  // Every line of that speaker follows.
  const aliceLines = page.locator('select').filter({ has: page.locator('option:checked', { hasText: 'Alice' }) });
  await expect(aliceLines).toHaveCount(3);
});

test('edit a transcript line', async () => {
  await page.getByText("Morning everyone, let's go through the release.").click();
  const box = page.locator('textarea').first();
  await box.fill("Morning all, let's go through the release.");
  await box.press('Enter');
  await expect(page.getByText("Morning all, let's go through the release.")).toBeVisible();
  // Persisted: survives a reload.
  await page.reload();
  await page.getByRole('button', { name: /release-sync/ }).first().click();
  await page.getByRole('button', { name: /^Transcript/ }).click();
  await expect(page.getByText("Morning all, let's go through the release.")).toBeVisible();
});

test('search finds the line and opens the meeting', async () => {
  await page.getByPlaceholder('Search transcripts…').fill('regression');
  const hit = page.getByRole('button', { name: /mobile regression/ });
  await expect(hit).toBeVisible();
  await hit.click();
  await expect(page.getByRole('heading', { name: 'release-sync' })).toBeVisible();
  await page.getByPlaceholder('Search transcripts…').fill('');
});

test('summarize produces decisions and action items', async () => {
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Ship the Waku migration in October')).toBeVisible();
  await expect(page.getByText('Update the docs before the release')).toBeVisible();
  await expect(page.getByText('Who owns the mobile regression?')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Re-summarize' })).toBeVisible();
});

test('ask answers with a citation that opens the meeting', async () => {
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  const box = page.getByPlaceholder(/What did we decide/);
  await box.fill('When does the Waku migration ship?');
  await box.press('Enter');
  await expect(page.getByText('The migration ships in October')).toBeVisible();
  const cite = page.getByRole('button', { name: /\[1\]\s*release-sync/ });
  await expect(cite).toBeVisible();
  await cite.click();
  await expect(page.getByRole('heading', { name: 'release-sync' })).toBeVisible();
});

test('digest of this week', async () => {
  await page.getByRole('button', { name: 'Digest', exact: true }).click();
  await page.getByRole('button', { name: 'Generate' }).click();
  await expect(page.getByText('A demo week.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'release-sync' }).first()).toBeVisible();
  await expect(page.getByText(/saved to .*digests/)).toBeVisible();
});

test('export to the Obsidian vault', async () => {
  await page.getByRole('link', { name: 'release-sync' }).first().click();
  await page.getByRole('button', { name: 'Export', exact: true }).click();
  await page.getByRole('button', { name: 'Export to Obsidian' }).click();
  await expect(page.getByText(/release-sync\.md/).first()).toBeVisible();
});
