import { expect, test } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

// The desktop app shows its window while the backend is still starting (or installing).
test('meetings show up when the backend comes up after the window', async ({ page, context }, info) => {
  await openApp(page);
  await importMeeting(page, info.outputDir, 'startup-check');

  const late = await context.newPage();
  let backendUp = false;
  await late.route(/127\.0\.0\.1:8018\//, (route) => (backendUp ? route.continue() : route.abort()));
  await late.goto('/');
  await late.waitForTimeout(2500); // the sidebar has mounted and its first requests failed
  backendUp = true;
  await expect(late.getByText('startup-check')).toBeVisible();
});
