import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

// Devices and people (docs/plans/2026-11-09-connections-design.md, task 9). Two backends take part:
//
// - MAIN (8058, a second demo backend, `E2E_BACKEND_PORT` in start-backend.sh) stands in for a main
//   computer. It asks for an API token, as one does once a device has been paired.
// - LAPTOP is the ordinary e2e backend (8018) acting as a laptop's own backend. Demo mode records
//   with a synthetic capture (services/satellite.py FakeCapture), never a device. A laptop window looks
//   for its own backend at 127.0.0.1:8008 (api/local.ts); there the window's requests are sent on to 8018.
//
// What is not covered: the desktop shell (pairing through the tunnel, `PUT /api/system/main` made by the
// shell, remote.json), a window whose main connection and local backend are two different machines, and
// the record button of a laptop window (device selection needs real devices).
const MAIN = 'http://127.0.0.1:8058';
const LAPTOP = 'http://127.0.0.1:8018';
const LOCAL = 'http://127.0.0.1:8008';
const TOKEN = 'e2e-main-token';
const auth = { Authorization: `Bearer ${TOKEN}` };

test.describe.configure({ mode: 'serial' });

// The token is set here, not made by pairing, so a retried run finds the backend as it left it.
test.beforeAll(async ({ playwright }) => {
  const api = await playwright.request.newContext();
  const r = await api.put(`${MAIN}/api/settings`, { data: { api_token: TOKEN }, headers: auth });
  expect(r.ok()).toBeTruthy();
  await api.dispose();
});

/** The window's connection, set before the app starts (the config's storage state points at 8018). */
async function connectWindow(page: Page, url: string, token: string, remote = false) {
  await page.addInitScript(
    ([key, value]) => localStorage.setItem(key, value),
    ['mnemosyne.connection', JSON.stringify({ url, token, remote })]
  );
}

/** A window of a computer that uses a main computer: connected through "remote access" (the flag the
 * desktop app sets), its own backend answering at 127.0.0.1:8008, which here is the 8018 backend. */
async function laptopWindow(page: Page) {
  await page.route(`${LOCAL}/**`, (route) => route.continue({ url: route.request().url().replace(LOCAL, LAPTOP) }));
  await connectWindow(page, LAPTOP, '', true);
}

async function tellMain(request: APIRequestContext, url: string, token: string, name = 'Studio desktop') {
  const r = await request.put(`${LAPTOP}/api/system/main`, { data: { url, token, name } });
  expect(r.ok()).toBeTruthy();
}

/** Leave the laptop backend as the other specs expect it: no main computer, nothing waiting. */
async function resetLaptop(request: APIRequestContext) {
  await request.post(`${LAPTOP}/api/satellite/stop`, { data: {} }).catch(() => {});
  const status = await (await request.get(`${LAPTOP}/api/satellite`)).json();
  for (const item of status.outbox) await request.delete(`${LAPTOP}/api/satellite/outbox/${item.id}`);
  await request.delete(`${LAPTOP}/api/system/main`);
}

async function openDevicesTab(page: Page) {
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'Devices and people', exact: true }).click();
}

test('My devices lists a paired phone and computer by kind, and Remove cuts one off', async ({ page, playwright }) => {
  test.setTimeout(90_000);
  const api = await playwright.request.newContext({ extraHTTPHeaders: auth });
  try {
    // A phone, paired the way the phone page does it: a code, then redeemed with no token.
    const phoneCode = await (await api.post(`${MAIN}/api/pairing/codes`, { data: { kind: 'phone' } })).json();
    const phone = await api.post(`${MAIN}/api/pairing/redeem`, { data: { code: phoneCode.code, name: 'Corey phone' } });
    expect(phone.ok()).toBeTruthy();

    // A computer needs remote access (the mnemosyne-link program): without it the code is refused with
    // the reason, and only the phone is checked.
    let computer = false;
    const desktopCode = await api.post(`${MAIN}/api/pairing/codes`, { data: { kind: 'desktop' } });
    if (desktopCode.ok()) {
      const { code } = await desktopCode.json();
      const redeemed = await api.post(`${MAIN}/api/pairing/redeem`, {
        data: { code, name: 'Corey laptop', endpoint_id: 'e2e'.padEnd(64, '0') }
      });
      expect(redeemed.ok()).toBeTruthy();
      computer = true;
    } else {
      expect(desktopCode.status()).toBe(409);
    }

    await connectWindow(page, MAIN, TOKEN);
    await page.goto('/');
    await expect(page.getByTitle('Connected', { exact: true })).toBeVisible();
    await openDevicesTab(page);

    await expect(page.getByRole('radiogroup', { name: 'What this computer is' }).getByRole('radio', { name: /^Main computer/ })).toBeChecked();
    await expect(page.getByRole('heading', { name: 'My devices' })).toBeVisible();

    // Add a device offers a computer or a phone.
    await page.getByRole('button', { name: 'Add a device' }).click();
    await expect(page.getByRole('button', { name: 'A laptop or another computer' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'A phone', exact: true })).toBeVisible();

    const list = page.getByRole('list', { name: 'My devices' });
    const phoneRow = list.getByRole('listitem').filter({ hasText: 'Corey phone' });
    await expect(phoneRow).toContainText('Sends recordings');
    await expect(phoneRow).toContainText('not seen yet');
    await expect(phoneRow.getByTitle('Phone')).toBeVisible();
    if (computer) {
      const row = list.getByRole('listitem').filter({ hasText: 'Corey laptop' });
      await expect(row).toContainText('Uses this computer');
      await expect(row.getByTitle('Computer')).toBeVisible();
    }

    // Remove asks, then the device is gone from the list and from the API.
    page.once('dialog', (d) => {
      expect(d.message()).toContain('Corey phone');
      void d.accept();
    });
    await phoneRow.getByRole('button', { name: 'Remove' }).click();
    await expect(phoneRow).toHaveCount(0);
    const left: { name: string }[] = await (await api.get(`${MAIN}/api/connections/devices`)).json();
    expect(left.map((d) => d.name)).not.toContain('Corey phone');
  } finally {
    const rest: { remove: string }[] = await (await api.get(`${MAIN}/api/connections/devices`)).json();
    for (const d of rest) await api.delete(`${MAIN}${d.remove}`);
    await api.dispose();
  }
});

test('a laptop streams a recording to its main computer: the meeting is the main computer\'s, none appears on the laptop', async ({ request }) => {
  test.setTimeout(60_000);
  const title = 'Laptop streamed e2e';
  try {
    expect(await (await request.get(`${LAPTOP}/api/satellite`)).json()).toMatchObject({ main: null, recording: null, outbox: [] });
    expect(await (await request.get(`${LAPTOP}/api/system/role`)).json()).toEqual({ role: 'main' });
    // Only the tunnel of the desktop app is a main computer's address.
    const bad = await request.put(`${LAPTOP}/api/system/main`, { data: { url: 'http://example.com:80', token: 'x', name: 'x' } });
    expect(bad.status()).toBe(400);

    await tellMain(request, MAIN, TOKEN);
    await expect.poll(async () => (await (await request.get(`${LAPTOP}/api/satellite`)).json()).reachable).toBe(true);

    const start = await request.post(`${LAPTOP}/api/satellite/start`, { data: { mic_device: 1, system_device: 2, title } });
    expect(start.ok()).toBeTruthy();
    const { session_id, streaming } = await start.json();
    expect(streaming).toBe(true);
    expect(session_id).toBeTruthy();
    // Audio is being sent while it is recorded.
    await expect
      .poll(async () => {
        const s = await (await request.get(`${LAPTOP}/api/satellite`)).json();
        return s.recording?.sources.some((x: { sent: number }) => x.sent > 0);
      })
      .toBe(true);

    const stop = await request.post(`${LAPTOP}/api/satellite/stop`, { data: {} });
    expect((await stop.json()).outcome).toBe('streamed');

    // The main computer has the meeting and transcribes it; the laptop's own library has none.
    await expect
      .poll(async () => {
        const all: { id: string; has_transcript: boolean }[] = await (await request.get(`${MAIN}/api/sessions`, { headers: auth })).json();
        return all.find((s) => s.id === session_id)?.has_transcript;
      }, { timeout: 30_000 })
      .toBe(true);
    const own: { name: string }[] = await (await request.get(`${LAPTOP}/api/sessions`)).json();
    expect(own.map((s) => s.name)).not.toContain(title);
    expect((await (await request.get(`${LAPTOP}/api/satellite`)).json()).outbox).toEqual([]);
    await request.delete(`${MAIN}/api/sessions/${session_id}`, { headers: auth });
  } finally {
    await resetLaptop(request);
  }
});

test('a laptop window says when its main computer is away, lists what waits, and offers a way out for a refused recording', async ({ page, request }) => {
  test.setTimeout(90_000);
  await laptopWindow(page);
  try {
    // Nothing listens on port 9: the main computer is away.
    await tellMain(request, 'http://127.0.0.1:9', 'unused');
    await page.goto('/');
    await expect(page.getByTitle(/^Connected to /)).toBeVisible();

    const banner = page.getByRole('status').filter({ hasText: 'Main computer not reachable. Recordings are kept here and go to it when it is.' }).first();
    await expect(banner).toBeVisible();
    await expect(page.getByRole('status').getByRole('button', { name: 'Record', exact: true })).toBeVisible();

    // A recording made meanwhile is kept here (not streamed) and waits.
    const start = await request.post(`${LAPTOP}/api/satellite/start`, { data: { mic_device: 1, system_device: 2, title: 'Weekly sync' } });
    expect((await start.json()).streaming).toBe(false);
    expect((await (await request.post(`${LAPTOP}/api/satellite/stop`, { data: {} })).json()).outcome).toBe('waiting');

    await page.getByRole('button', { name: '1 recording waiting' }).click();
    const region = page.getByRole('region', { name: 'Recordings waiting' });
    await expect(region).toContainText('Weekly sync');
    await expect(region).toContainText('Waiting for Studio desktop');
    await expect(region.getByRole('button', { name: 'Send as a new meeting' })).toHaveCount(0);

    // The main computer is back, but refuses this laptop's key: the recording is kept, with the reason.
    await tellMain(request, MAIN, 'a-key-main-does-not-know');
    await expect(banner).toBeHidden();
    await expect(region).toContainText('Studio desktop did not take it');
    await expect(region).toContainText('Missing or invalid API token');

    // Send as a new meeting: asked of this computer's backend, which tries again.
    const asked = page.waitForResponse((r) => /\/api\/satellite\/outbox\/[^/]+\/new-meeting$/.test(r.url()) && r.request().method() === 'POST');
    await region.getByRole('button', { name: 'Send as a new meeting' }).click();
    expect((await asked).ok()).toBeTruthy();
    await expect(region.getByRole('button', { name: 'Discard' })).toBeVisible(); // refused again: still kept

    // Discard asks, then deletes the audio from this computer.
    page.once('dialog', (d) => {
      expect(d.message()).toContain('Weekly sync');
      void d.accept();
    });
    await region.getByRole('button', { name: 'Discard' }).click();
    await expect(page.getByRole('button', { name: /recordings? waiting/ })).toHaveCount(0);
    expect((await (await request.get(`${LAPTOP}/api/satellite`)).json()).outbox).toEqual([]);
  } finally {
    await resetLaptop(request);
  }
});

test('Settings of a laptop window: it uses a main computer, so these are the main computer\'s settings', async ({ page, request }) => {
  await laptopWindow(page);
  try {
    await tellMain(request, MAIN, TOKEN);
    await page.goto('/');
    await expect(page.getByTitle(/^Connected to /)).toBeVisible();
    await openDevicesTab(page);
    const roles = page.getByRole('radiogroup', { name: 'What this computer is' });
    await expect(roles.getByRole('radio', { name: /^Uses my main computer/ })).toBeChecked();
    await expect(page.getByText("These are your main computer's settings.")).toBeVisible();
    await expect(page.getByText('Keep a copy of the library on this laptop')).toBeVisible();
    // The list of devices is the main computer's to show.
    await expect(page.getByRole('heading', { name: 'My devices' })).toHaveCount(0);
  } finally {
    await resetLaptop(request);
  }
});
