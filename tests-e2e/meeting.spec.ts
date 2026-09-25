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

test('the audio player is created on first use, then seeks', async () => {
  // Opening a meeting must not load media (it crashed WebKit in the AppImage).
  await expect(page.locator('audio')).toHaveCount(0);
  await page.getByTitle('Play from here').nth(2).click(); // the 0:07 line
  await expect(page.locator('audio')).toHaveCount(1);
  await expect
    .poll(async () => page.locator('audio').evaluate((a: HTMLAudioElement) => a.currentTime))
    .toBeGreaterThan(6.5);
  await page.locator('audio').evaluate((a: HTMLAudioElement) => a.pause());
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

test('search by meaning: "launch" finds "ship"', async () => {
  const box = page.getByPlaceholder('Search transcripts…');
  // The meeting is indexed a few seconds after import; retype until it shows up.
  await expect(async () => {
    await box.fill('');
    await box.fill('launch');
    await expect(page.getByText('related').first()).toBeVisible({ timeout: 1000 });
  }).toPass({ timeout: 20_000 });
  await expect(page.getByRole('button', { name: /≈.*ship the migration/ })).toBeVisible();
  await box.fill('');
});

test('summarize produces decisions and action items', async () => {
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Summarize', exact: true }).click();
  await expect(page.getByText('Ship the Waku migration in October')).toBeVisible();
  await expect(page.getByText('Update the docs before the release')).toBeVisible();
  await expect(page.getByText('Who owns the mobile regression?')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Re-summarize' })).toBeVisible();
  // Chapters jump into the transcript, which shows them as headings.
  await page.getByRole('button', { name: /Mobile regression owner/ }).click();
  await expect(page.getByText('Release status', { exact: true })).toBeVisible();
  await expect(page.getByText('Mobile regression owner', { exact: true })).toBeVisible();
});

test('summary items link to where they came up', async () => {
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  const links = page.getByTitle('Show where this came up in the transcript');
  await expect(links).toHaveCount(3); // the decision, the action item, the open question
  await expect(links.first()).toHaveText('0:07');
  await links.first().click();
  const line = page.locator('[data-idx]').filter({ hasText: 'then we ship the migration in October' });
  await expect(line).toBeVisible();
  await expect(line).toHaveClass(/bg-yellow-900/); // flashed
});

test('draft a follow-up email', async () => {
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await page.getByRole('button', { name: 'Draft follow-up' }).click();
  await expect(page.getByLabel('Follow-up draft')).toHaveValue(/^Subject: Release planning follow-up/);
  await expect(page.getByRole('button', { name: 'Redraft' })).toBeVisible();
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

test('tasks: tick off an action item across meetings', async () => {
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await expect(page.getByText(/· 1 open\./)).toBeVisible();
  // click, not check(): the item leaves the Open list as soon as it is done.
  await page.getByRole('checkbox', { name: 'Done: Update the docs before the release' }).click();
  await expect(page.getByText(/· 0 open\./)).toBeVisible();
  await expect(page.getByText('Nothing matches.')).toBeVisible();
  await page.getByRole('button', { name: 'done', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: 'Done: Update the docs before the release' })).toBeChecked();
  // The meeting's summary shows it as done too.
  await page.getByRole('button', { name: /release-sync/ }).first().click();
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Reopen: Update the docs before the release' })).toBeVisible();
});

test('brief: a later meeting with the same title shows what is open', async ({}, info) => {
  await importMeeting(page, info.outputDir, 'release-sync-2');
  await page.getByRole('button', { name: 'Recording', exact: true }).click();
  const brief = page.getByLabel('Meeting brief');
  await expect(brief.getByText('From earlier meetings:')).toBeVisible();
  await expect(brief.getByRole('button', { name: 'release-sync', exact: true })).toBeVisible();
  // The only action item was ticked off in the tasks test; the question is still open.
  await expect(brief.getByText(/0 open items, 1 open question/)).toBeVisible();
  await expect(brief.getByText('? Who owns the mobile regression?')).toBeVisible();
});

test('people: a renamed speaker has a page with their meetings', async () => {
  await page.getByRole('button', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: /^Alice/ }).click();
  const person = page.getByLabel('Person Alice');
  await expect(person.getByRole('heading', { name: 'Alice' })).toBeVisible();
  await expect(person.getByRole('button', { name: 'release-sync', exact: true })).toBeVisible();
  await expect(person.getByText(/spoke 0:\d\d \(\d+%\)/).first()).toBeVisible();
});

test('topics: follow a topic and ask where it stands', async () => {
  await page.getByRole('button', { name: 'Topics', exact: true }).click();
  await page.getByRole('button', { name: /^waku/ }).click();
  await expect(page.getByText('✔ Ship the Waku migration in October')).toBeVisible();
  await page.getByRole('button', { name: 'Where does this stand?' }).click();
  await expect(page.getByLabel('Where it stands').getByText('The migration ships in October.')).toBeVisible();
});

test('local only: the lock marks a meeting in the header and sidebar', async () => {
  await page.getByRole('button', { name: /^release-sync/ }).first().click();
  const lock = page.getByRole('button', { name: /Cloud allowed/ });
  await lock.click();
  await expect(page.getByRole('button', { name: /Local only/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTitle('Local only').first()).toBeVisible();
  await page.getByRole('button', { name: /Local only/ }).click();
  await expect(page.getByRole('button', { name: /Cloud allowed/ })).toBeVisible();
});
