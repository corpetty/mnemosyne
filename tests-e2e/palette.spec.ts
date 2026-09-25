import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

test('Ctrl+K finds settings, meetings and asks questions', async ({ page }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'palette-standup');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  const search = palette.getByRole('combobox');

  // A settings tab, by two partial words.
  await page.keyboard.press('Control+k');
  await search.fill('settings ai');
  await expect(palette.getByRole('option').first()).toHaveText('Settings · AI');
  await search.press('Enter');
  await expect(palette).toBeHidden();
  await expect(page.getByRole('heading', { name: 'Search and Ask' })).toBeVisible();

  // A meeting, by part of its name; it is also remembered as recent.
  await page.keyboard.press('Control+k');
  await search.fill('palette-st');
  await search.press('Enter');
  await expect(page.getByRole('heading', { name: 'palette-standup' })).toBeVisible();
  await page.keyboard.press('Control+k');
  await expect(palette.getByText('Recent')).toBeVisible();
  await expect(palette.getByRole('option').first()).toContainText('palette-standup');

  // Escape closes; arrow keys move the selection.
  await search.press('ArrowDown');
  await expect(palette.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true');
  await search.press('Escape');
  await expect(palette).toBeHidden();

  // "ask …" puts the question first and sends it to Ask.
  await page.keyboard.press('Control+k');
  await search.fill('ask when does the migration ship?');
  await expect(palette.getByRole('option').first()).toHaveText(
    'Ask your meetings: “when does the migration ship?”'
  );
  await search.press('Enter');
  await expect(page.getByText('when does the migration ship?', { exact: true })).toBeVisible();
  // Other specs may have asked Ask already, so there can be more than one answer.
  await expect(page.getByText('The migration ships in October').last()).toBeVisible();
});
