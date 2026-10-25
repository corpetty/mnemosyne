import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const PUBLISH_HOST = 'http://127.0.0.1:8028';
const RELAY = '127.0.0.1:8038';
const NAME = 'guest-notes-iroh-demo';

// Guest notes over iroh (phase 10; docs/plans/2026-10-25-phase-10-tasks.md, C7, C9, C13). The
// demo backend runs the rooms sidecar (`mnemosyne-link rooms`, MNEMOSYNE_LINK_BIN) which serves
// a room on a loopback host and an iroh endpoint through the plain-HTTP dev relay on 8038
// (`remote_relays`, tests-e2e/start-relay.sh). One backend serves every spec, so each test sets
// `guest_transport` (and `guest_host`) through the settings API and afterEach puts back what
// start-backend.sh configured for the phase 9 spec.
//
// The guest page's CSP (a meta tag in the built page, intersected with the host's header) lists
// https/wss relays only; the dev relay is http/ws on loopback, so the guest context bypasses
// CSP. That leaves the relay entries of the policy untested here (the backend's `guest_csp` has
// a pytest). The guest is a second context on another origin with no stored state.

test.setTimeout(120_000);

test.afterEach(async ({ request }) => {
  // Deleting the meeting also deletes its guest room.
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
  await putSettings(request, { guest_transport: 'host', guest_host: PUBLISH_HOST });
});

async function putSettings(request: APIRequestContext, data: Record<string, string>) {
  const res = await request.put(`${BACKEND}/api/settings`, { data });
  expect(res.ok()).toBeTruthy();
}

async function openNotesTab(page: Page) {
  await page.getByRole('button', { name: /^Notes/ }).click();
  const editor = page.getByTestId('notes-editor');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await expect(page.getByTestId('notes-status')).toContainText(/Saved as you type|here/);
  return editor;
}

interface Room {
  url: string | null;
  transport: string;
  relay_kind: string;
  mirror_host_url: string;
}

/** The owner imports the meeting and opens its Notes tab; returns the meeting id and the editor. */
async function setUp(page: Page, request: APIRequestContext, dir: string) {
  await openApp(page);
  await importMeeting(page, dir, NAME);
  const id: string = (await (await request.get(`${BACKEND}/api/sessions`)).json()).find(
    (s: { name: string }) => s.name === NAME
  ).id;
  const owner = await openNotesTab(page);
  return { id, owner };
}

/** Invite guests -> Create a guest link; returns the link from the menu (the menu is closed). */
async function createLink(page: Page) {
  await page.getByRole('button', { name: 'Invite guests', exact: true }).click();
  const menu = page.getByRole('dialog', { name: 'Guest notes' });
  // Starting a room starts the rooms sidecar and an iroh endpoint: allow for it.
  await menu.getByRole('button', { name: 'Create a guest link' }).click();
  const linkInput = menu.getByLabel('Guest link');
  await expect(linkInput).toHaveValue(/#2\./, { timeout: 60_000 });
  const link = await linkInput.inputValue();
  await expect(page.getByRole('button', { name: 'Guests · live' })).toBeVisible();
  return { link, menu };
}

async function guestJoins(page: Page, link: string, name: string, opts: { checkCsp?: boolean } = {}) {
  const response = await page.goto(link);
  if (opts.checkCsp !== false) expect(response?.headers()['content-security-policy'] ?? '').toContain(`ws://${RELAY}`);
  await expect(page.getByRole('heading', { name: /^Join .+'s notes$/ })).toBeVisible({ timeout: 60_000 });
  await page.getByRole('textbox', { name: 'Your name' }).fill(name);
  await page.getByRole('button', { name: 'Join', exact: true }).click();
  const editor = page.getByTestId('guest-notes-editor');
  await expect(editor).toHaveAttribute('contenteditable', 'true', { timeout: 60_000 });
  await expect(page.getByTestId('guest-status')).toContainText('Live', { timeout: 60_000 });
  return editor;
}

test('guests join over iroh through the relay, with no host in the link, and are ended', async ({ page, browser, request }, info) => {
  await putSettings(request, { guest_transport: 'iroh', guest_host: '' });
  const guestCtx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const { id, owner } = await setUp(page, request, info.outputDir);
    const { link, menu } = await createLink(page);

    // A format 2 link: the room, the key, the endpoint id and the relay in the fragment; the
    // page is the local publish host's (guest_page_url), which has no such room.
    expect(link).toMatch(new RegExp(`^${PUBLISH_HOST}/n/#2\\.[0-9a-f]{16}\\.[^.]+\\.[0-9a-f]+\\.${RELAY.replaceAll('.', '\\.')}$`));
    const room: Room = await (await request.get(`${BACKEND}/api/sessions/${id}/guest-notes`)).json();
    expect(room.url).toBe(link);
    expect(room.url).toMatch(/#2\.[0-9a-f]{16}\./);
    expect(room.transport).toBe('iroh');
    expect(room.relay_kind).toBe('custom');
    expect(room.mirror_host_url).toBe('');
    await expect(menu).toContainText('Works while this computer is on');
    await expect(menu).toContainText(`Through your relay ${RELAY}`);
    await expect(menu.getByText('Nobody has opened it yet')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);

    // The guest opens it: the page has to reach this computer over iroh.
    const guest = await guestCtx.newPage();
    const guestEditor = await guestJoins(guest, link, 'Ina');

    // Both type; each sees the other.
    await guestEditor.click();
    await guest.keyboard.type('Hello from Ina');
    await expect(owner).toContainText('Hello from Ina', { timeout: 30_000 });
    await expect(page.getByTestId('notes-status')).toContainText('Guests here: Ina', { timeout: 30_000 });

    await owner.click();
    await page.keyboard.press('Control+End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('Owner over iroh');
    await expect(guestEditor).toContainText('Owner over iroh', { timeout: 30_000 });
    await expect(guestEditor).toContainText('Hello from Ina');

    // End guest notes: the guest page turns read-only, with what was written still readable.
    await page.getByRole('button', { name: 'Guests · live' }).click();
    const menu2 = page.getByRole('dialog', { name: 'Guest notes' });
    await expect(menu2).toContainText('Here now: Ina', { timeout: 30_000 });
    await menu2.getByRole('button', { name: 'End guest notes' }).click();
    await menu2.getByRole('button', { name: 'Yes', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Guests · ended' })).toBeVisible();
    await expect(guest.getByText(/These notes ended\. You can still read and copy them/)).toBeVisible({ timeout: 60_000 });
    await expect(guestEditor).toHaveAttribute('contenteditable', 'false');
    await expect(guestEditor).toContainText('Hello from Ina');
    await expect(guestEditor).toContainText('Owner over iroh');
  } finally {
    await guestCtx.close();
  }
});

test('with a publish host the link points at its page and a mirror keeps the room', async ({ page, browser, request }, info) => {
  await putSettings(request, { guest_transport: 'iroh', guest_host: PUBLISH_HOST });
  const guestCtx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const { id, owner } = await setUp(page, request, info.outputDir);
    const { link, menu } = await createLink(page);

    // The mirror host serves the page at /n/<room>; the same room id sits in the fragment.
    const m = new RegExp(`^${PUBLISH_HOST}/n/([0-9a-f]{16})#2\\.\\1\\.[^.]+\\.[0-9a-f]+\\.${RELAY.replaceAll('.', '\\.')}$`).exec(link);
    expect(m, link).not.toBeNull();
    const room: Room = await (await request.get(`${BACKEND}/api/sessions/${id}/guest-notes`)).json();
    expect(room.url).toBe(link);
    expect(room.transport).toBe('iroh');
    expect(room.mirror_host_url).toBe(PUBLISH_HOST);
    await expect(menu).toContainText('Also kept on 127.0.0.1');
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);

    // A guest joins and the two converge (over iroh, or the host's socket if iroh is slow).
    const guest = await guestCtx.newPage();
    const guestEditor = await guestJoins(guest, link, 'Ina');
    await guestEditor.click();
    await guest.keyboard.type('Mirrored hello');
    await expect(owner).toContainText('Mirrored hello', { timeout: 30_000 });
    await owner.click();
    await page.keyboard.press('Control+End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('Owner mirrored');
    await expect(guestEditor).toContainText('Owner mirrored', { timeout: 30_000 });
    await expect(guestEditor).toContainText('Mirrored hello');

    // Stop sharing the notes: both rooms are deleted; the guest page, loaded again, has none.
    await page.getByRole('button', { name: 'Guests · live' }).click();
    const menu2 = page.getByRole('dialog', { name: 'Guest notes' });
    await menu2.getByRole('button', { name: 'Stop sharing the notes' }).click();
    await menu2.getByRole('button', { name: 'Yes', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Invite guests', exact: true })).toBeVisible();
    expect(await (await request.get(`${BACKEND}/api/sessions/${id}/guest-notes`)).json()).toBeNull();
    await guest.reload();
    await expect(guest.getByRole('alert')).toContainText(/These notes are no longer shared\.|doesn't open these notes/, {
      timeout: 60_000
    });
    await expect(guest.getByTestId('guest-notes-editor')).toHaveCount(0);
  } finally {
    await guestCtx.close();
  }
});

// Live carets across transports (phase 11, task 15). Ina opens the format 2 link (iroh first);
// Bob opens the same room as a format 1 link (`<host>/n/<room>#<key>`, no endpoint), which can
// only use the mirror host's WebSocket. They share no room, so each sees the other (typing line
// and caret) only because the owner's bridge forwards presence between the iroh room and the
// mirror. The owner page sees both guests' carets, and both guests the owner's.
test('carets and typing cross between an iroh guest, a mirror-host guest and the owner', async ({ page, browser, request }, info) => {
  await putSettings(request, { guest_transport: 'iroh', guest_host: PUBLISH_HOST });
  const inaCtx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const bobCtx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const { owner } = await setUp(page, request, info.outputDir);
    const { link, menu } = await createLink(page);
    const m = new RegExp(`^${PUBLISH_HOST}/n/([0-9a-f]{16})#2\\.\\1\\.([^.]+)\\.`).exec(link);
    expect(m, link).not.toBeNull();
    const hostLink = `${PUBLISH_HOST}/n/${m![1]}#${m![2]}`;
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);

    const ina = await inaCtx.newPage();
    const bob = await bobCtx.newPage();
    const inaEditor = await guestJoins(ina, link, 'Ina');
    const bobEditor = await guestJoins(bob, hostLink, 'Bob', { checkCsp: false });
    // The owner's side names guests "<name> (guest)" (guest_notes._guest_name); guest pages do not.
    await expect(page.getByTestId('notes-status')).toContainText(/Guests here: (Ina \(guest\), Bob|Bob \(guest\), Ina) \(guest\)/, {
      timeout: 30_000
    });

    const carets = (p: Page, who?: string) =>
      p.locator(who ? `[data-testid="remote-caret"][data-name="${who}"]` : '[data-testid="remote-caret"]');
    const typeSlowly = (p: Page, text: string) => p.keyboard.type(text, { delay: 120 });

    // Ina types a line (Bob gets it across the bridge) while Bob's page shows her typing.
    await inaEditor.click();
    const typing = typeSlowly(ina, 'Ina is writing a line of notes');
    await expect(bob.getByTestId('guest-status')).toContainText('Ina is typing…', { timeout: 30_000 });
    await expect(page.getByTestId('notes-status')).toContainText('Ina (guest) is typing…', { timeout: 30_000 });
    await typing;
    await expect(bobEditor).toContainText('Ina is writing a line of notes', { timeout: 30_000 });

    // Bob opens a line of his own, slowly, and Ina sees him typing.
    await bobEditor.click();
    await bob.keyboard.press('Control+End');
    await bob.keyboard.press('Enter');
    const typing2 = typeSlowly(bob, 'Bob answers on the host');
    await expect(ina.getByTestId('guest-status')).toContainText('Bob is typing…', { timeout: 30_000 });
    await typing2;
    await expect(inaEditor).toContainText('Bob answers on the host', { timeout: 30_000 });

    // Carets: the owner sees both guests, each guest the other one, and nobody sees themselves.
    await expect(carets(page, 'Ina (guest)').locator('.bar')).toBeVisible({ timeout: 30_000 });
    await expect(carets(page, 'Bob (guest)').locator('.bar')).toBeVisible({ timeout: 30_000 });
    await expect(carets(ina, 'Bob').locator('.bar')).toBeVisible({ timeout: 30_000 });
    await expect(carets(bob, 'Ina').locator('.bar')).toBeVisible({ timeout: 30_000 });
    await expect(carets(ina, 'Ina')).toHaveCount(0);
    await expect(carets(bob, 'Bob')).toHaveCount(0);

    // Ina selects a word: Bob (on the other transport) and the owner see the range.
    await inaEditor.getByText('Ina is writing a line of notes').click();
    await ina.keyboard.press('Home');
    for (let i = 0; i < 'Ina'.length; i++) await ina.keyboard.press('Shift+ArrowRight');
    await expect(carets(bob, 'Ina').locator('.selection span').first()).toBeVisible({ timeout: 30_000 });
    await expect(carets(page, 'Ina (guest)').locator('.selection span').first()).toBeVisible({ timeout: 30_000 });

    // The owner focuses the notes: both guests get a third caret (the owner's, under their name).
    await owner.click();
    await page.keyboard.press('Control+Home');
    await expect(ina.getByTestId('remote-caret')).toHaveCount(2, { timeout: 30_000 });
    await expect(bob.getByTestId('remote-caret')).toHaveCount(2, { timeout: 30_000 });

    // Bob leaves the editor: his caret goes from the owner's page and from Ina's.
    await bobEditor.evaluate((el) => (el as HTMLElement).blur());
    await expect(carets(page, 'Bob (guest)')).toHaveCount(0, { timeout: 30_000 });
    await expect(carets(ina, 'Bob')).toHaveCount(0, { timeout: 30_000 });
    await expect(carets(page, 'Ina (guest)').locator('.bar')).toBeVisible();
  } finally {
    await inaCtx.close();
    await bobCtx.close();
  }
});
