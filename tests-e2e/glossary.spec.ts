import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// Fixing a misheard name in a transcript offers to teach the glossary, which then also fixes
// the rest of that meeting.
test('a corrected name is offered for the glossary', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'glossary-check');
  await page.getByText('The Waku migration is code complete.').click();
  const box = page.locator('textarea').first();
  await box.fill('The Waku migration is Nimbus complete.');
  await box.press('Enter');
  await expect(page.getByText('Always write “Nimbus” for “code”?')).toBeVisible();
  await page.getByRole('button', { name: 'Add to glossary' }).click();
  await expect(page.getByText(/^Added to the glossary/)).toBeVisible();
});
