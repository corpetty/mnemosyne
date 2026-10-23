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

test('rename a meeting from its header', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'rename-me');
  const header = page.getByRole('heading', { name: 'rename-me' });

  await header.getByRole('button', { name: 'rename-me' }).click();
  const input = page.getByLabel('Meeting name');
  await expect(input).toBeFocused();
  await input.fill('  Quarterly review  ');
  await input.press('Enter');
  await expect(page.getByRole('heading', { name: 'Quarterly review' })).toBeVisible();
  await expect(page.getByLabel('Meeting name')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Quarterly review' }).first()).toBeVisible();

  // Escape leaves the name alone.
  await page.getByTitle('Rename this meeting').click();
  await page.getByLabel('Meeting name').fill('Something else');
  await page.getByLabel('Meeting name').press('Escape');
  await expect(page.getByLabel('Meeting name')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Quarterly review' })).toBeVisible();
  await expect(page.getByText('Something else')).toHaveCount(0);
});
