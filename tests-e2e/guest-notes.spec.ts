import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAME = 'guest-notes-demo';

// Guest notes (phase 9; backend services/guest_notes.py, the owner's menu in
// GuestNotesMenu.svelte, the guests' page in guest/). The demo backend relays through a local
// publish host on 8028 (`guest_host`, tests-e2e/start-backend.sh); the guest page is served by
// that host at /n/<room>#<key>. Needs the mnemosyne-link sidecar like the shared notes do.
// The guest is a second browser context on another origin, with no stored state.
// The owner's display name comes from the system account of the machine, so it is not asserted.

test.afterEach(async ({ request }) => {
  // Deleting the meeting also deletes its guest room.
  const sessions: { id: string; name: string }[] = await (await request.get(`${BACKEND}/api/sessions`)).json();
  for (const s of sessions.filter((x) => x.name === NAME)) await request.delete(`${BACKEND}/api/sessions/${s.id}`);
});

async function openNotesTab(page: Page) {
  await page.getByRole('button', { name: /^Notes/ }).click();
  const editor = page.getByTestId('notes-editor');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await expect(page.getByTestId('notes-status')).toContainText(/Saved as you type|here/);
  return editor;
}

test('guests take notes with the owner through a link, comment, and are ended', async ({ page, browser, request }, info) => {
  test.setTimeout(90_000);
  const guestCtx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    // 1. The owner: a meeting, an agenda of two points (one already covered), the Notes tab.
    await openApp(page);
    await importMeeting(page, info.outputDir, NAME);
    const id: string = (await (await request.get(`${BACKEND}/api/sessions`)).json()).find(
      (s: { name: string }) => s.name === NAME
    ).id;
    const agenda = await request.put(`${BACKEND}/api/sessions/${id}/agenda`, {
      data: {
        items: [
          { text: 'Beta date', covered: true },
          { text: 'Pricing', covered: false }
        ]
      }
    });
    expect(agenda.ok()).toBeTruthy();

    const owner = await openNotesTab(page);

    // Invite guests -> Create a guest link; the link sits in a read-only input.
    await page.getByRole('button', { name: 'Invite guests', exact: true }).click();
    const menu = page.getByRole('dialog', { name: 'Guest notes' });
    await menu.getByRole('button', { name: 'Create a guest link' }).click();
    const linkInput = menu.getByLabel('Guest link');
    await expect(linkInput).toHaveValue(/\/n\/[0-9a-f]+#.+/);
    const link = await linkInput.inputValue();
    // The API says the same.
    const room: { url: string | null } = await (await request.get(`${BACKEND}/api/sessions/${id}/guest-notes`)).json();
    expect(room.url).toBe(link);
    await expect(page.getByRole('button', { name: 'Guests · live' })).toBeVisible();
    await expect(menu.getByText('Nobody has opened it yet')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);

    // 2. The guest opens the link: a name card ("Join <owner>'s notes", "Your name", Join).
    const guest = await guestCtx.newPage();
    await guest.goto(link);
    await expect(guest.getByRole('heading', { name: /^Join .+'s notes$/ })).toBeVisible({ timeout: 30_000 });
    await guest.getByRole('textbox', { name: 'Your name' }).fill('Ana');
    await guest.getByRole('button', { name: 'Join', exact: true }).click();
    const guestEditor = guest.getByTestId('guest-notes-editor');
    await expect(guestEditor).toHaveAttribute('contenteditable', 'true', { timeout: 30_000 });
    await expect(guest.getByTestId('guest-status')).toContainText('Live', { timeout: 30_000 });

    // 3. Both type. The guest first; the owner sees it, and "Guests here: Ana".
    await guestEditor.click();
    await guest.keyboard.type('Hello from Ana');
    await expect(owner).toContainText('Hello from Ana', { timeout: 20_000 });
    await expect(page.getByTestId('notes-status')).toContainText('Guests here: Ana', { timeout: 20_000 });

    await owner.click();
    await page.keyboard.press('Control+End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('Owner here');
    await expect(guestEditor).toContainText('Owner here', { timeout: 20_000 });
    await expect(guestEditor).toContainText('Hello from Ana');

    // The owner's menu names the guest too.
    await page.getByRole('button', { name: 'Guests · live' }).click();
    await expect(page.getByRole('dialog', { name: 'Guest notes' })).toContainText('Here now: Ana', { timeout: 20_000 });
    await page.keyboard.press('Escape');

    // 4. The agenda on the guest page, with the covered point marked.
    const agendaCard = guest.getByTestId('guest-agenda');
    await expect(agendaCard.getByRole('listitem').filter({ hasText: 'Beta date' })).toContainText('Covered');
    await expect(agendaCard.getByRole('listitem').filter({ hasText: 'Pricing' })).toBeVisible();
    await expect(agendaCard.getByRole('listitem').filter({ hasText: 'Pricing' })).not.toContainText('Covered');

    // 5. The guest selects "Ana" and comments on it.
    await guestEditor.getByText('Hello from Ana').click();
    await guest.keyboard.press('End');
    for (let i = 0; i < 'Ana'.length; i++) await guest.keyboard.press('Shift+ArrowLeft');
    await guest.getByRole('button', { name: 'Comment on the selection' }).click();
    const compose = guest.getByTestId('guest-comment-compose');
    await expect(compose).toContainText('Ana');
    await compose.getByRole('textbox', { name: 'Your comment' }).fill('Is this right?');
    await compose.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(guest.getByTestId('guest-comments')).toContainText('Is this right?');

    // The comment reaches the owner's meeting as a comment on the notes by "Ana (guest)".
    await expect(async () => {
      const body: { threads: { root: { author: string; text: string; target: string } }[] } = await (
        await request.get(`${BACKEND}/api/sessions/${id}/comments`)
      ).json();
      const t = body.threads.find((x) => x.root.text === 'Is this right?');
      expect(t?.root.author).toBe('Ana (guest)');
      expect(t?.root.target).toBe('notes');
    }).toPass({ timeout: 20_000 });
    // In the app: the commented text is marked in the notes; its thread opens on a click.
    const mark = owner.locator('mark.ne-comment').first();
    await expect(mark).toBeVisible({ timeout: 20_000 });
    await mark.click();
    const threads = page.getByTestId('notes-comment-threads');
    await expect(threads).toContainText('Ana (guest)');
    await expect(threads).toContainText('Is this right?');

    // 6. End guest notes (the menu asks inline: Yes / No). The guest page turns read-only.
    await page.getByRole('button', { name: 'Guests · live' }).click();
    const menu2 = page.getByRole('dialog', { name: 'Guest notes' });
    await menu2.getByRole('button', { name: 'End guest notes' }).click();
    await menu2.getByRole('button', { name: 'Yes', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Guests · ended' })).toBeVisible();
    await expect(guest.getByText(/These notes ended\. You can still read and copy them/)).toBeVisible({ timeout: 30_000 });
    await expect(guestEditor).toHaveAttribute('contenteditable', 'false');
    // What was written is still there to read.
    await expect(guestEditor).toContainText('Hello from Ana');
    await expect(guestEditor).toContainText('Owner here');

    // 7. Stop sharing the notes: the room is deleted; the guest page, loaded again, has none.
    await menu2.getByRole('button', { name: 'Stop sharing the notes' }).click();
    await menu2.getByRole('button', { name: 'Yes', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Invite guests', exact: true })).toBeVisible();
    expect(await (await request.get(`${BACKEND}/api/sessions/${id}/guest-notes`)).json()).toBeNull();
    await guest.reload();
    await expect(guest.getByRole('alert')).toContainText(/These notes are no longer shared\.|doesn't open these notes/, {
      timeout: 30_000
    });
    await expect(guest.getByTestId('guest-notes-editor')).toHaveCount(0);
  } finally {
    await guestCtx.close();
  }
  // 8. The meeting is deleted by afterEach.
});
