import { expect, test } from '@playwright/test';

const BACKEND = 'http://127.0.0.1:8018';

// The desktop app shows its window while the backend is still starting (or installing).
test('meetings show up when the backend comes up after the window', async ({ page, request }) => {
  const created = await request.post(`${BACKEND}/api/sessions`, { data: { name: 'startup-check' } });
  expect(created.ok()).toBeTruthy();

  let backendUp = false;
  await page.route(/127\.0\.0\.1:8018\//, (route) => (backendUp ? route.continue() : route.abort()));
  await page.goto('/');
  await page.waitForTimeout(2500); // the sidebar has mounted and its first requests failed
  backendUp = true;
  await expect(page.getByText('startup-check')).toBeVisible();
});
