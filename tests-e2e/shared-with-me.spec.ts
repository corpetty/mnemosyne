import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const SHARED_ID = 'ada000000001';
const SHARED_NAME = 'Shared: Waku roadmap';
const OWN = 'share-menu-demo';

// Phase 8: POST /api/demo/share makes a contact, Ada Lovelace, and one meeting she shares with the
// demo user (backend services/share_demo.py); it is seeded here and removed afterwards so no other
// spec sees it. Ada's computer answers nobody, so what we send stays pending.
test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/api/demo/share`);
  expect(r.ok()).toBeTruthy();
});

test.afterAll(async ({ request }) => {
  await request.delete(`${BACKEND}/api/demo/share`);
});

test.afterEach(async ({ request }) => {
  const r = await request.get(`${BACKEND}/api/sessions/${SHARED_ID}/comments`);
  if (r.ok()) {
    const body: { threads: { root: { id: string }; replies?: { id: string }[] }[] } = await r.json();
    for (const t of body.threads) {
      for (const c of t.replies ?? []) await request.delete(`${BACKEND}/api/sessions/${SHARED_ID}/comments/${c.id}`).catch(() => {});
      await request.delete(`${BACKEND}/api/sessions/${SHARED_ID}/comments/${t.root.id}`).catch(() => {});
    }
  }
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === OWN)) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

const row = (page: Page, name: string) => page.locator('[role=button]', { hasText: name }).first();

async function openShared(page: Page) {
  await openApp(page);
  await row(page, SHARED_NAME).click();
  await expect(page.getByRole('heading', { name: SHARED_NAME })).toBeVisible();
}

test('a meeting shared with me sits in its own group and opens read-mostly', async ({ page }) => {
  await openApp(page);
  const group = page.getByRole('heading', { name: 'Shared with me', level: 3 });
  await expect(group).toBeVisible();
  await expect(row(page, SHARED_NAME)).toContainText('from Ada Lovelace');

  await row(page, SHARED_NAME).click();
  await expect(page.getByRole('heading', { name: SHARED_NAME })).toBeVisible();
  await expect(page.getByText('Shared by Ada Lovelace', { exact: true })).toBeVisible();
  await expect(page.getByText('Ada Lovelace shared this with you: you can comment and tick action items.')).toBeVisible();
  // Not mine: no rename, no Share menu.
  await expect(page.getByTitle('Rename this meeting')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Share', exact: true })).toHaveCount(0);

  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  await expect(page.getByText('Ship the store protocol before light clients')).toBeVisible();
  await expect(page.getByText('Review the roadmap every month')).toBeVisible();
  await expect(page.getByText('Who owns the light client benchmarks?')).toBeVisible();
  await expect(page.getByText('Write up the store protocol milestones')).toBeVisible();
});

test('a comment on a shared meeting waits for the owner\'s computer', async ({ page }) => {
  await openShared(page);
  await page.getByRole('button', { name: 'Summary', exact: true }).click();
  const decision = 'Ship the store protocol before light clients';
  await expect(page.getByText(decision)).toBeVisible();
  await page.getByText(decision).hover();
  await page.getByRole('button', { name: 'Comment on this decision' }).first().click();
  await page.getByPlaceholder('Add a comment').fill('Does this include the filter protocol?');
  await page
    .locator('[data-comment-block]')
    .filter({ has: page.getByPlaceholder('Add a comment') })
    .getByRole('button', { name: 'Comment', exact: true })
    .click();

  // The thread shows under the decision and again in the Comments card: check the inline one.
  await expect(
    page.locator('[data-comment-block]').getByText('Does this include the filter protocol?'),
  ).toBeVisible();
  await expect(page.getByText('You', { exact: true }).first()).toBeVisible();
  // Ada's computer is unreachable: the comment stays queued.
  await expect(page.getByText(/^Waiting for Ada Lovelace's computer: \d+ comments?/)).toBeVisible();
});

test('Settings lists the people I share with', async ({ page }) => {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'General', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'People you share with' })).toBeVisible();
  const ada = page.locator('div[aria-label="People you share with"] li').filter({ hasText: 'Ada Lovelace' });
  await expect(ada).toBeVisible();
  // The fingerprint: 8 groups of 4 base32 characters.
  await expect(ada).toContainText(/(?:[A-Z2-7]{4} ){7}[A-Z2-7]{4}/);
});

test('sharing my own meeting: the menu lists Ada and the dialog warns about the transcript', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, OWN);

  await page.getByRole('button', { name: 'Share', exact: true }).click();
  const menu = page.getByRole('dialog', { name: 'Share this meeting with people' });
  await expect(menu.getByText('People you share with')).toBeVisible();
  await expect(menu.getByText('Ada Lovelace')).toBeVisible();
  await menu.getByRole('button', { name: 'Share…' }).click();

  const dialog = page.getByRole('dialog', { name: 'Share this meeting with Ada Lovelace' });
  await expect(dialog.getByRole('heading', { name: `Share “${OWN}” with Ada Lovelace` })).toBeVisible();
  const warning = dialog.getByText("The whole transcript goes to Ada Lovelace's computer and can't be taken back.");
  await expect(warning).toHaveCount(0);
  await dialog.getByRole('checkbox', { name: 'Transcript' }).check();
  await expect(warning).toBeVisible();

  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  // Nothing was shared: the header button is still plain "Share".
  await expect(page.getByRole('button', { name: 'Share', exact: true })).toBeVisible();
});
