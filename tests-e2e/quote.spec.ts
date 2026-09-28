import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// Runs before setup.spec (alphabetical), which moves the transcriber off the demo one.
test('quote a range of lines as text and as an audio clip', async ({ page, context }, info) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await openApp(page);
  await importMeeting(page, info.outputDir, 'quote-check');

  await page.getByRole('button', { name: 'Quote from line 2' }).click();
  await page.getByRole('button', { name: 'Quote from line 3' }).click({ modifiers: ['Shift'] });
  const bar = page.getByRole('toolbar', { name: 'Quote' });
  await expect(bar).toContainText('2 lines · 0:03–0:10');

  await bar.getByRole('button', { name: 'Copy text' }).click();
  await expect(page.getByText('Quote copied')).toBeVisible();
  const text = await page.evaluate(() => navigator.clipboard.readText());
  expect(text).toContain('> **SPEAKER_01** [00:03]: The Waku migration is code complete.');
  expect(text).toContain('> **SPEAKER_00** [00:07]: Great, then we ship the migration in October.');
  expect(text).toContain('— *quote-check*,');

  const download = page.waitForEvent('download');
  await bar.getByRole('button', { name: 'Save audio clip' }).click();
  expect((await download).suggestedFilename()).toBe('quote-check 0m03s.ogg'); // where it starts

  await bar.getByRole('button', { name: 'Cancel the quote' }).click();
  await expect(bar).toBeHidden();
});
