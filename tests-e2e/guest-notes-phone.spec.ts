import { expect, test, type Page } from '@playwright/test';
import { importMeeting, openApp } from './fixtures';

const BACKEND = 'http://127.0.0.1:8018';
const NAME = 'guest-phone-demo';

// The guest notes page on a phone (guest/src/App.svelte, Tabs.svelte, Comments.svelte and the
// shared editor's touch parts in src/lib/notes/ui/: TouchBar, Sheet, CommentSheet). Same setup as
// guest-notes.spec.ts (demo backend, local publish host on 8028, the mnemosyne-link sidecar); the
// guest is a second browser context with a phone's viewport and a coarse pointer.
// Chromium only: WebKit's emulation does not report `pointer: coarse` the way the page needs
// (see the touch-screen test in notes-editor.spec.ts), so this spec is not in the WebKit project.

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

type Thread = { root: { id: string; author: string; text: string; target: string }; replies: { author: string; text: string }[] };

test('a guest on a phone: tabs, touch bar, comment sheet, thread sheet', async ({ page, browser, request }, info) => {
  test.skip(browser.browserType().name() !== 'chromium', 'pointer: coarse is emulated by Chromium only');
  test.setTimeout(120_000);
  const guestCtx = await browser.newContext({
    storageState: { cookies: [], origins: [] },
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true
  });
  try {
    // 1. The owner: a meeting, an agenda of two points (one covered), the Notes tab, a guest link, a line of notes.
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
    await page.getByRole('button', { name: 'Invite guests', exact: true }).click();
    const menu = page.getByRole('dialog', { name: 'Guest notes' });
    await menu.getByRole('button', { name: 'Create a guest link' }).click();
    const linkInput = menu.getByLabel('Guest link');
    await expect(linkInput).toHaveValue(/\/n\/[0-9a-f]+#.+/);
    const link = await linkInput.inputValue();
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);

    await owner.click();
    await page.keyboard.type('Hello from owner');

    // 2. The guest opens the link on a phone: the name card, then the page.
    const guest = await guestCtx.newPage();
    await guest.goto(link);
    expect(await guest.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
    await expect(guest.getByRole('heading', { name: /^Join .+'s notes$/ })).toBeVisible({ timeout: 30_000 });
    await guest.getByRole('textbox', { name: 'Your name' }).fill('Ana');
    await guest.getByRole('button', { name: 'Join', exact: true }).click();
    const guestEditor = guest.getByTestId('guest-notes-editor');
    await expect(guestEditor).toHaveAttribute('contenteditable', 'true', { timeout: 30_000 });
    await expect(guest.getByTestId('guest-status')).toContainText('Live', { timeout: 30_000 });
    await expect(guestEditor).toContainText('Hello from owner', { timeout: 30_000 });

    // 3. Tabs: Notes, Agenda, Comments (no count yet); Notes is selected. No slim toolbar.
    const tabs = guest.getByRole('tablist');
    await expect(tabs).toBeVisible();
    const notesTab = tabs.getByRole('tab', { name: 'Notes', exact: true });
    const agendaTab = tabs.getByRole('tab', { name: 'Agenda', exact: true });
    const commentsTab = tabs.getByRole('tab', { name: /^Comments/ });
    await expect(tabs.getByRole('tab')).toHaveCount(3);
    await expect(notesTab).toHaveAttribute('aria-selected', 'true');
    await expect(commentsTab).toHaveText(/^\s*Comments\s*$/);
    await expect(guest.getByRole('toolbar', { name: 'Format notes', exact: true })).toBeHidden();
    await expect(guest.getByRole('tabpanel', { name: 'Notes' })).toBeVisible();

    // 4. The Agenda tab lists the points, the covered one marked.
    await agendaTab.tap();
    await expect(agendaTab).toHaveAttribute('aria-selected', 'true');
    const agendaCard = guest.getByTestId('guest-agenda');
    await expect(agendaCard).toBeVisible();
    await expect(agendaCard.getByRole('listitem').filter({ hasText: 'Beta date' })).toContainText('Covered');
    await expect(agendaCard.getByRole('listitem').filter({ hasText: 'Pricing' })).toBeVisible();
    await expect(guestEditor).toBeHidden();

    // 5. Back to Notes; select "owner" in the editor and tap the touch bar's Comment.
    await notesTab.tap();
    await expect(notesTab).toHaveAttribute('aria-selected', 'true');
    await expect(guestEditor).toBeVisible();
    await guestEditor.evaluate((root, word) => {
      const el = root as HTMLElement;
      el.focus({ preventScroll: true });
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const i = (n.textContent ?? '').indexOf(word);
        if (i < 0) continue;
        const range = document.createRange();
        range.setStart(n, i);
        range.setEnd(n, i + word.length);
        const sel = window.getSelection()!;
        sel.removeAllRanges();
        sel.addRange(range);
        return;
      }
      throw new Error(`"${word}" not found in the editor`);
    }, 'owner');
    const bar = guest.getByRole('toolbar', { name: 'Format notes (touch)' });
    await expect(bar).toBeVisible();
    await bar.getByRole('button', { name: 'Comment on the selection' }).tap();

    // The sheet quotes the word; write the comment and send it.
    const sheet = guest.getByRole('dialog', { name: 'Comment on the selection' });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByTestId('comment-sheet-quote')).toHaveText('owner');
    await sheet.getByRole('textbox', { name: 'Your comment' }).fill('Is this right?');
    await sheet.getByRole('button', { name: 'Comment', exact: true }).click();
    await expect(sheet).toHaveCount(0);

    // 6. The Comments tab counts the thread and lists it.
    await expect(commentsTab).toContainText('1', { timeout: 20_000 });
    await commentsTab.tap();
    await expect(commentsTab).toHaveAttribute('aria-selected', 'true');
    await expect(guest.getByTestId('guest-comments')).toContainText('Is this right?', { timeout: 20_000 });

    // It reaches the owner: through the API and as a marked comment in the owner's notes.
    await expect(async () => {
      const body: { threads: Thread[] } = await (await request.get(`${BACKEND}/api/sessions/${id}/comments`)).json();
      const t = body.threads.find((x) => x.root.text === 'Is this right?');
      expect(t?.root.author).toBe('Ana (guest)');
      expect(t?.root.target).toBe('notes');
    }).toPass({ timeout: 20_000 });
    const mark = owner.locator('mark.ne-comment').first();
    await expect(mark).toBeVisible({ timeout: 20_000 });
    await mark.click();
    await expect(page.getByTestId('notes-comment-threads')).toContainText('Is this right?');

    // 7. Back in the notes, tapping the highlight opens the thread in a sheet; reply from it.
    await notesTab.tap();
    await expect(guestEditor).toBeVisible();
    const guestMark = guestEditor.locator('mark.ne-comment-open').first();
    await expect(guestMark).toBeVisible({ timeout: 20_000 });
    await guestMark.tap();
    const thread = guest.getByRole('dialog', { name: 'Comment thread' });
    await expect(thread).toBeVisible();
    await expect(thread).toContainText('Is this right?');
    await expect(thread.getByTestId('thread-sheet-quote')).toContainText('owner');
    await thread.getByRole('textbox', { name: 'Reply' }).fill('From the phone');
    await thread.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(thread).toContainText('From the phone', { timeout: 20_000 });
    await expect(async () => {
      const body: { threads: Thread[] } = await (await request.get(`${BACKEND}/api/sessions/${id}/comments`)).json();
      const t = body.threads.find((x) => x.root.text === 'Is this right?');
      expect(t?.replies.map((r) => r.text)).toContain('From the phone');
    }).toPass({ timeout: 20_000 });
    await thread.getByRole('button', { name: 'Close', exact: true }).tap();
    await expect(thread).toHaveCount(0);

    // 8. "Show in notes" in the Comments tab goes back to the Notes tab.
    await commentsTab.tap();
    await expect(commentsTab).toHaveAttribute('aria-selected', 'true');
    await expect(guestEditor).toBeHidden();
    await guest.getByRole('button', { name: 'Show in notes', exact: true }).first().tap();
    await expect(notesTab).toHaveAttribute('aria-selected', 'true');
    await expect(guestEditor).toBeVisible();
  } finally {
    await guestCtx.close();
  }
  // 9. The meeting is deleted by afterEach (and with it the guest room).
});
