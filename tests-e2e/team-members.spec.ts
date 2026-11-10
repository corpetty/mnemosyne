import { expect, test, type Page } from '@playwright/test';
import { importMeeting } from './fixtures';

// A team member's app (docs/plans/2026-11-10-team-members-in-the-app.md). Two demo backends:
//
// - HOST (8078) hosts a team: the owner Corey, one member Sam with an app device, a meeting each.
//   `POST /api/demo/team-host` also opens its tunnel listener, the door the link sidecar's streams
//   come in by, which is the only one a member's token opens (api/auth.py).
// - MEMBER (8088) is Sam's own computer. `POST /api/demo/join-team` writes its membership pointed
//   straight at that listener, so no link sidecar, no iroh and no pairing take part.
//
// The window of a desktop app talks to its own backend at 127.0.0.1:8008 (api/local.ts, and the
// team's library under /api/joined); here every request, and the event socket, is sent on to MEMBER.
// Demo mode records with a synthetic capture (services/satellite.py FakeCapture), never a device.
//
// What is not covered: joining itself (redeeming the app invite through the sidecar: backend tests
// with a stand-in sidecar), a team recording whose work runs on the member's computer (it needs
// PipeWire capture on the member's backend: the move it ends in is covered by "Put in the team"),
// the host's own window, and the Settings invite box (it needs the sidecar's ticket).
const HOST = 'http://127.0.0.1:8078';
const MEMBER = 'http://127.0.0.1:8088';
const LOCAL = 'http://127.0.0.1:8008';
const UI = 'http://localhost:5183';
const TOKEN = 'e2e-team-host-token';
const auth = { Authorization: `Bearer ${TOKEN}` };
const TEAM = 'Team on Desk';

// The window starts on this computer's own backend, as the desktop app does.
test.use({
  storageState: {
    cookies: [],
    origins: [{ origin: UI, localStorage: [{ name: 'mnemosyne.connection', value: JSON.stringify({ url: LOCAL, token: '' }) }] }]
  }
});
test.describe.configure({ mode: 'serial' });

let teamMeeting = '';
let ownerMeeting = '';

test.beforeAll(async ({ playwright }) => {
  const api = await playwright.request.newContext();
  try {
    // A team host asks for a token: set before the first hosting call (and kept for retries).
    expect((await api.put(`${HOST}/api/settings`, { data: { api_token: TOKEN }, headers: auth })).ok()).toBeTruthy();
    const hosted = await api.post(`${HOST}/api/demo/team-host`, { data: { host_name: 'Desk' }, headers: auth });
    expect(hosted.ok(), await hosted.text()).toBeTruthy();
    const team = await hosted.json();
    teamMeeting = team.member_meeting;
    ownerMeeting = team.owner_meeting;
    const joined = await api.post(`${MEMBER}/api/demo/join-team`, {
      data: { url: team.url, token: team.token, name: team.host_name, user: team.user }
    });
    expect(joined.ok(), await joined.text()).toBeTruthy();
  } finally {
    await api.dispose();
  }
});

test.afterAll(async ({ playwright }) => {
  const api = await playwright.request.newContext();
  try {
    await api.delete(`${MEMBER}/api/joined`).catch(() => {});
    await api.delete(`${HOST}/api/demo/team-host`, { headers: auth }).catch(() => {});
    await api.put(`${HOST}/api/settings`, { data: { api_token: '' }, headers: auth }).catch(() => {});
  } finally {
    await api.dispose();
  }
});

/** The window of Sam's computer: what it asks of 127.0.0.1:8008 (HTTP and the event socket) is
 * answered by MEMBER. */
async function memberWindow(page: Page) {
  await page.route(`${LOCAL}/**`, (route) => route.continue({ url: route.request().url().replace(LOCAL, MEMBER) }));
  await page.routeWebSocket(/127\.0\.0\.1:8008/, (ws) => {
    const upstream = new WebSocket(ws.url().replace('127.0.0.1:8008', '127.0.0.1:8088'));
    upstream.binaryType = 'arraybuffer';
    const early: (string | Buffer)[] = [];
    ws.onMessage((m) => (upstream.readyState === WebSocket.OPEN ? upstream.send(m) : early.push(m)));
    ws.onClose(() => upstream.close());
    upstream.onopen = () => early.splice(0).forEach((m) => upstream.send(m));
    upstream.onmessage = (e) => ws.send(typeof e.data === 'string' ? e.data : Buffer.from(e.data as ArrayBuffer));
    upstream.onclose = () => ws.close();
  });
}

/** `setup` adds routes after the forwarding ones (the last one registered is asked first). */
async function openMember(page: Page, setup?: () => Promise<void>) {
  await memberWindow(page);
  await setup?.();
  await page.goto('/');
  await expect(page.getByRole('group', { name: 'Library' })).toBeVisible();
}

const library = (page: Page) => page.getByRole('group', { name: 'Library' });

test('the library switch names the team; the team library holds the member\'s meeting and not the owner\'s', async ({ page, request }) => {
  await openMember(page);
  await expect(library(page).getByRole('button', { name: 'Your library' })).toHaveAttribute('aria-pressed', 'true');
  await expect(library(page).getByRole('button', { name: TEAM })).toHaveAttribute('aria-pressed', 'false');

  await library(page).getByRole('button', { name: TEAM }).click();
  await expect(library(page).getByRole('button', { name: TEAM })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('header').getByTitle(TEAM, { exact: true })).toBeVisible(); // the header's label
  await expect(page.getByRole('button', { name: /^Team standup/ })).toBeVisible();
  await expect(page.getByText('Private planning')).toHaveCount(0);

  // The member's own backend proxies the host: as the member, through the proxy, the owner's meeting is not there.
  const mine: { id: string }[] = await (await request.get(`${MEMBER}/api/joined/api/sessions`)).json();
  expect(mine.map((s) => s.id)).toContain(teamMeeting);
  expect(mine.map((s) => s.id)).not.toContain(ownerMeeting);

  await page.getByRole('button', { name: /^Team standup/ }).click();
  await expect(page.getByRole('heading', { name: 'Team standup' })).toBeVisible();

  await library(page).getByRole('button', { name: 'Your library' }).click();
  await expect(library(page).getByRole('button', { name: 'Your library' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: /^Team standup/ })).toHaveCount(0);
});

test('Settings, Your team: the card names the team, who you are and where team recordings are done', async ({ page }) => {
  await openMember(page);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'Devices and people', exact: true }).click();
  const card = page.getByRole('region', { name: 'Your team' });
  await expect(card.getByText(TEAM, { exact: true })).toBeVisible();
  await expect(card.getByText('reachable', { exact: true })).toBeVisible();
  await expect(card.getByText('You are Sam, member')).toBeVisible();
  const work = card.getByRole('radiogroup', { name: 'Where team recordings are done' });
  await expect(work.getByRole('radio', { name: 'On this computer' })).toBeChecked();
  await expect(work.getByRole('radio', { name: 'On Desk' })).not.toBeChecked();
  await expect(card.getByRole('button', { name: 'Leave the team' })).toBeVisible();
});

test('Settings in the team library: Devices and people is only the Your team card', async ({ page }) => {
  await openMember(page);
  await library(page).getByRole('button', { name: TEAM }).click();
  await expect(library(page).getByRole('button', { name: TEAM })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'Devices and people', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Your team' })).toBeVisible();
  await expect(page.getByText("This computer's devices, sharing and links are in Your library.")).toBeVisible();
  // The host's own sections are not shown as this computer's.
  await expect(page.getByRole('region', { name: 'This computer' })).toHaveCount(0);
  await expect(page.getByText('Host a team on this computer')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Leave the team' })).toBeVisible();
});

test('the Record menu offers the team, and a recording done on the host ends up in the team library', async ({ page, request }) => {
  test.setTimeout(90_000);
  const before: { id: string }[] = await (await request.get(`${MEMBER}/api/joined/api/sessions`)).json();

  await openMember(page, async () => {
    // The member's computer has no audio devices here: two stand-ins, remembered as the selection.
    await page.route(/\/api\/devices$/, (route) =>
      route.fulfill({
        json: [
          { id: 1, name: 'e2e-mic', description: 'E2E microphone', media_class: 'Audio/Source', is_input: true, is_output: false, is_monitor: false, is_echo_cancelled: false },
          { id: 2, name: 'e2e-call', description: 'E2E call audio', media_class: 'Audio/Sink', is_input: false, is_output: true, is_monitor: false, is_echo_cancelled: false }
        ]
      })
    );
    await page.addInitScript(() => localStorage.setItem('mnemosyne.selectedDevices', JSON.stringify(['e2e-mic', 'e2e-call'])));
  });
  await library(page).getByRole('button', { name: TEAM }).click();
  await expect(page.getByRole('button', { name: /^Team standup/ })).toBeVisible();
  await page.getByRole('button', { name: 'New meeting' }).first().click();

  await page.getByRole('button', { name: 'Where this recording goes' }).click();
  const menu = page.getByRole('menu', { name: 'Where this recording goes' });
  await expect(menu.getByRole('menuitemradio', { name: /^Your library/ })).toBeVisible();
  const here = menu.getByRole('menuitemradio', { name: /^Team on Desk, done on this computer/ });
  await expect(here).toContainText('Moves to the team when transcribed');
  await expect(here).toHaveAttribute('aria-checked', 'true'); // the default: done here
  const onHost = menu.getByRole('menuitemradio', { name: /^Team on Desk, done on Desk/ });
  await expect(onHost).toContainText('Streams as you record; needs a connection');
  await onHost.click();

  await page.getByRole('button', { name: 'Record', exact: true }).click();
  await expect(page.getByText('Recording started, going to Team on Desk')).toBeVisible();
  // Audio is being sent to the host as it is recorded.
  await expect
    .poll(async () => {
      const s = await (await request.get(`${MEMBER}/api/satellite`)).json();
      return s.recording?.sources.some((x: { sent: number }) => x.sent > 0);
    })
    .toBe(true);
  await page.getByRole('button', { name: 'Stop', exact: true }).first().click();

  // The host made the meeting as Sam (it is in the team library) and transcribed it; none on this computer.
  let made = '';
  await expect
    .poll(async () => {
      const all: { id: string; has_transcript: boolean }[] = await (await request.get(`${MEMBER}/api/joined/api/sessions`)).json();
      const fresh = all.find((s) => s.has_transcript && !before.some((b) => b.id === s.id));
      made = fresh?.id ?? '';
      return fresh?.has_transcript;
    }, { timeout: 45_000 })
    .toBe(true);
  const own: { id: string }[] = await (await request.get(`${MEMBER}/api/sessions`)).json();
  expect(own.map((s) => s.id)).not.toContain(made);
  const seen = await (await request.get(`${HOST}/api/sessions/${made}`, { headers: auth })).json();
  expect(seen.owner_id).not.toBe('');
  // The draft "New meeting" made is the meeting that was recorded: no empty one is left behind.
  const after: { id: string }[] = await (await request.get(`${MEMBER}/api/joined/api/sessions`)).json();
  const added = after.filter((x) => !before.some((b) => b.id === x.id));
  expect(added.map((s) => s.id)).toEqual([made]);
  for (const s of added) await request.delete(`${MEMBER}/api/joined/api/sessions/${s.id}`);
});

test('"Put in the team" moves a local meeting: it leaves Your library and appears in the team\'s', async ({ page, request }, info) => {
  test.setTimeout(90_000);
  const name = 'Local planning';
  // A run that failed half way may have left its meetings behind: start without them.
  for (const base of [`${MEMBER}/api`, `${MEMBER}/api/joined/api`]) {
    const left: { id: string; name: string }[] = await (await request.get(`${base}/sessions`)).json();
    for (const s of left.filter((x) => x.name === name)) await request.delete(`${base}/sessions/${s.id}`);
  }
  await openMember(page);
  await importMeeting(page, info.outputDir, name);
  const local: { id: string; name: string }[] = await (await request.get(`${MEMBER}/api/sessions`)).json();
  const id = local.find((s) => s.name === name)!.id;

  await page.getByRole('button', { name: 'More for this meeting' }).click();
  const put = page.getByRole('menuitem', { name: /^Put in the team/ });
  await expect(put).toContainText('Moves to Team on Desk once its work is done');
  page.once('dialog', (d) => {
    expect(d.message()).toContain('Team on Desk');
    void d.accept();
  });
  await put.click();

  // The host has it (as Sam's), and it is gone from this computer: one copy, the team's.
  await expect
    .poll(async () => {
      const all: { name: string }[] = await (await request.get(`${MEMBER}/api/joined/api/sessions`)).json();
      return all.some((s) => s.name === name);
    }, { timeout: 45_000 })
    .toBe(true);
  await expect
    .poll(async () => {
      const mine: { id: string }[] = await (await request.get(`${MEMBER}/api/sessions`)).json();
      return mine.some((s) => s.id === id);
    })
    .toBe(false);
  // The open window hears of it without a reload: the list and the open meeting follow, and the
  // toast opens the team's copy.
  await expect(page.getByText('Moved to Team on Desk')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: new RegExp(`^${name}`) })).toHaveCount(0);
  await expect(page.getByRole('heading', { name })).toHaveCount(0);
  await page.getByRole('button', { name: 'Open', exact: true }).click();
  await expect(library(page).getByRole('button', { name: TEAM })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('heading', { name })).toBeVisible();
  await expect(page.getByRole('button', { name: new RegExp(`^${name}`) }).first()).toBeVisible(); // in the list
  const team: { id: string; name: string }[] = await (await request.get(`${MEMBER}/api/joined/api/sessions`)).json();
  for (const s of team.filter((x) => x.name === name)) await request.delete(`${MEMBER}/api/joined/api/sessions/${s.id}`);
});
