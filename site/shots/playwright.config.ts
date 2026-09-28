import { defineConfig } from '@playwright/test';

// Screenshots of the app for the landing page (site/), taken against a demo backend with
// seeded meetings (seed.py). Run: pnpm exec playwright test --config site/shots/playwright.config.ts
const BACKEND = 'http://127.0.0.1:8048';
const UI = 'http://localhost:5193';

export default defineConfig({
  testDir: '.',
  workers: 1,
  timeout: 60_000,
  reporter: 'list',
  use: {
    baseURL: UI,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
    storageState: {
      cookies: [],
      origins: [
        { origin: UI, localStorage: [{ name: 'mnemosyne.connection', value: JSON.stringify({ url: BACKEND, token: '' }) }] }
      ]
    }
  },
  webServer: [
    { command: 'bash site/shots/start.sh', cwd: '../..', url: `${BACKEND}/health`, reuseExistingServer: true, timeout: 180_000 },
    { command: 'pnpm exec vite dev --port 5193 --strictPort', cwd: '../..', url: UI, reuseExistingServer: true, timeout: 120_000 }
  ]
});
