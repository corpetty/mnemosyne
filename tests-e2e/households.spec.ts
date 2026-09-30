import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { makeWav, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAMES = ['Annual review Maria Lopez', 'Review with Maria Lopez'];

test.afterEach(async ({ request }) => {
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => NAMES.includes(x.name))) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  const households: { id: string }[] = await (await request.get(`${BACKEND}/api/households`)).json();
  for (const h of households) await request.delete(`${BACKEND}/api/households/${h.id}`);
});

// What a household said at its last review shows on the page of its next meeting.
test('a household collects facts and briefs the next meeting', async ({ page, request }, info) => {
  const wav = makeWav(info.outputDir, 'review.wav');
  const imported = await request.post(`${BACKEND}/api/audio/import`, {
    multipart: { file: { name: 'review.wav', mimeType: 'audio/wav', buffer: readFileSync(wav) }, name: NAMES[0] }
  });
  const sid = (await imported.json()).session.id as string;
  await expect.poll(async () => (await (await request.get(`${BACKEND}/api/sessions/${sid}`)).json()).transcript.length).toBeGreaterThan(0);
  await request.post(`${BACKEND}/api/sessions/${sid}/summarize`, { data: { style: 'advisory' } });
  await expect
    .poll(async () => (await (await request.get(`${BACKEND}/api/sessions/${sid}`)).json()).summary_data?.client_facts?.length ?? 0)
    .toBeGreaterThan(0);
  await request.post(`${BACKEND}/api/sessions`, { data: { name: NAMES[1] } });

  await openApp(page);
  await page.getByRole('button', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Households', exact: true }).click();
  await page.getByRole('button', { name: 'New household' }).click();
  await page.getByPlaceholder('Lopez household').fill('Lopez household');
  await page.getByPlaceholder('Maria Lopez <maria@example.com>').fill('Maria Lopez\nDavid Lopez');
  await page.getByRole('button', { name: 'Save household' }).click();
  const household = page.getByRole('article', { name: 'Household Lopez household' });
  await expect(household.getByText('Client said they want to retire at 62.')).toBeVisible();
  await expect(household.getByText('Maria Lopez, David Lopez · 2 meetings')).toBeVisible();

  await page.getByText(NAMES[1]).first().click();
  const brief = page.getByLabel('Household brief');
  await expect(brief).toContainText('Lopez household, last time:');
  await expect(brief).toContainText('Client said their daughter starts college in fall 2027.');
});
