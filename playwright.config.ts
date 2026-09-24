import { defineConfig, devices } from '@playwright/test';

// Browser tests against the real UI and a demo-mode backend (canned transcriber,
// diarizer and LLM; see backend/mnemosyne/demo.py). Ports differ from the dev ones
// so a running app is left alone.
const BACKEND = 'http://127.0.0.1:8018';
const UI = 'http://localhost:5183';

export default defineConfig({
  testDir: 'tests-e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: UI,
    trace: 'retain-on-failure',
    storageState: {
      cookies: [],
      origins: [
        {
          origin: UI,
          localStorage: [{ name: 'mnemosyne.connection', value: JSON.stringify({ url: BACKEND, token: '' }) }]
        }
      ]
    }
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: 'bash tests-e2e/start-backend.sh',
      url: `${BACKEND}/health`,
      reuseExistingServer: false,
      timeout: 120_000
    },
    {
      command: 'pnpm exec vite dev --port 5183 --strictPort',
      url: UI,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000
    }
  ]
});
