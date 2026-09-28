import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// Runs before setup.spec (alphabetical), which moves the transcriber off the demo one.
test('after transcription, name the speakers in one go', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'naming-check');
  const card = page.getByRole('region', { name: 'Who is who' });
  await expect(card).toBeVisible();
  await expect(card.getByRole('listitem')).toHaveCount(2); // SPEAKER_00 and SPEAKER_01
  await expect(card.getByRole('button', { name: 'Play a sample of SPEAKER_00' })).toBeEnabled();

  await card.getByLabel('Name for SPEAKER_00').fill('Dana');
  await card.getByRole('button', { name: 'Done' }).click();
  await expect(card).toBeHidden();
  await expect(page.getByRole('button', { name: 'Dana', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'SPEAKER_01', exact: true })).toBeVisible();

  // Done for this meeting: it does not come back.
  await page.reload();
  await page.getByRole('button', { name: 'naming-check' }).first().click();
  await page.getByRole('button', { name: /^Transcript/ }).click();
  await expect(page.getByText('The Waku migration is code complete.')).toBeVisible();
  await expect(card).toBeHidden();
});
