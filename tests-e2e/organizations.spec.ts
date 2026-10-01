import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { makeWav, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAMES = ['Annual review Maria Lopez', 'Review with Maria Lopez'];

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => NAMES.includes(x.name))) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  const organizations: { id: string }[] = await (await request.get(`${BACKEND}/api/organizations`)).json();
  for (const h of organizations) await request.delete(`${BACKEND}/api/organizations/${h.id}`);
});

// What an organization said at its last meeting shows on the page of its next meeting.
test('an organization collects facts and briefs the next meeting', async ({ page, request }, info) => {
  const wav = makeWav(info.outputDir, 'review.wav');
  const imported = await request.post(`${BACKEND}/api/audio/import`, {
    multipart: { file: { name: 'review.wav', mimeType: 'audio/wav', buffer: readFileSync(wav) }, name: NAMES[0] }
  });
  const sid = (await imported.json()).session.id as string;
  await expect.poll(async () => (await (await request.get(`${BACKEND}/api/sessions/${sid}`)).json()).transcript.length).toBeGreaterThan(0);
  await request.post(`${BACKEND}/api/sessions/${sid}/summarize`, { data: { style: 'client' } });
  await expect
    .poll(async () => (await (await request.get(`${BACKEND}/api/sessions/${sid}`)).json()).summary_data?.client_facts?.length ?? 0)
    .toBeGreaterThan(0);
  await request.post(`${BACKEND}/api/sessions`, { data: { name: NAMES[1] } });

  await openApp(page);
  await page.getByRole('button', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Organizations', exact: true }).click();
  await page.getByRole('button', { name: 'New organization' }).click();
  await page.getByPlaceholder('Acme Corp').fill('Acme');
  await page.getByPlaceholder('Dana Reyes <dana@acme.example>').fill('Maria Lopez\nDavid Lopez');
  await page.getByRole('button', { name: 'Save organization' }).click();
  const organization = page.getByRole('article', { name: 'Organization Acme' });
  await expect(organization.getByText('Client said they want it shipped in October.')).toBeVisible();
  await expect(organization.getByText('Maria Lopez, David Lopez · 2 meetings')).toBeVisible();

  await page.getByText(NAMES[1]).first().click();
  const brief = page.getByLabel('Organization brief');
  await expect(brief).toContainText('Acme, last time:');
  await expect(brief).toContainText('Client said nobody owns the mobile regression.');
});
