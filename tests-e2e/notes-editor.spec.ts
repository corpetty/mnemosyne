import { expect, test, type Page } from '@playwright/test';

// The notes editor's input layer in a real engine, on the dev-only harness route
// (src/routes/dev/notes-editor: a local document, no backend). Typing goes through
// page.keyboard so the browser's own beforeinput/input path is exercised; the document's
// markdown and the DOM are checked after each construct.

declare global {
  interface Window {
    __notes: {
      markdown(): string;
      text(): string;
      remoteInsert(at: number, text: string): void;
      log: string[];
      linkRequests: { from: number; to: number; url: string | null }[];
      view: { setSelection(anchor: number, focus?: number, preferAfter?: boolean): void; lines: { start: number; end: number }[] };
      getSelectionOffsets(root: HTMLElement): { anchor: number; focus: number } | null;
      input: { setLink(from: number, to: number, url: string | null): void; setReadOnly(v: boolean): void };
    };
  }
}

const md = (page: Page) => page.evaluate(() => window.__notes.markdown());
const caret = (page: Page) =>
  page.evaluate(() => window.__notes.getSelectionOffsets(document.querySelector('[data-testid=editor]') as HTMLElement));
const select = (page: Page, anchor: number, focus = anchor) =>
  page.evaluate(([a, f]) => window.__notes.view.setSelection(a, f), [anchor, focus]);
const lineEnd = (page: Page, i: number) => page.evaluate((k) => window.__notes.view.lines[k].end, i);

async function open(page: Page, markdown = '') {
  await page.goto(`/dev/notes-editor?md=${encodeURIComponent(markdown)}`);
  await expect(page.getByTestId('status')).toHaveText('ready');
  const editor = page.getByTestId('editor');
  await editor.click();
  return editor;
}

/** A paste as the browser would deliver it: a ClipboardEvent with text/plain. */
async function paste(page: Page, text: string) {
  await page.evaluate((t) => {
    const dt = new DataTransfer();
    dt.setData('text/plain', t);
    const editor = document.querySelector('[data-testid=editor]') as HTMLElement;
    editor.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  }, text);
}

/** What copy puts on the clipboard (the event's data, read back from the DataTransfer). */
async function copyText(page: Page): Promise<string> {
  return page.evaluate(() => {
    const dt = new DataTransfer();
    const editor = document.querySelector('[data-testid=editor]') as HTMLElement;
    editor.dispatchEvent(new ClipboardEvent('copy', { clipboardData: dt, bubbles: true, cancelable: true }));
    return dt.getData('text/plain');
  });
}

test('typing, lists, headings, marks, joins and undo', async ({ page }) => {
  const editor = await open(page);
  await page.keyboard.type('Hello world');
  await expect(editor.locator('.ne-line')).toHaveText('Hello world');
  expect(await md(page)).toBe('Hello world\n');

  // An input rule starts a list, Enter continues it, Enter on an empty item leaves it.
  await page.keyboard.press('Enter');
  await page.keyboard.type('- first');
  await expect(editor.locator('.ne-bullet')).toHaveText('first');
  await page.keyboard.press('Enter');
  await page.keyboard.type('second');
  await page.keyboard.press('Enter');
  await expect(editor.locator('.ne-bullet')).toHaveCount(3);
  await page.keyboard.press('Enter');
  await expect(editor.locator('.ne-bullet')).toHaveCount(2);
  expect(await md(page)).toBe('Hello world\n- first\n- second\n\n');

  // A heading rule, then bold over a native selection.
  await page.keyboard.type('# Title');
  await expect(editor.locator('.ne-h1')).toHaveText('Title');
  await page.keyboard.press('Shift+Home');
  await page.keyboard.press('Control+b');
  await expect(editor.locator('.ne-h1 strong')).toHaveText('Title');
  expect(await md(page)).toBe('Hello world\n- first\n- second\n**# Title**\n'.replace('**# Title**', '# **Title**'));

  // End lands in the filler after the span: typing there is plain (the browser's placement
  // is the intent). Arrowing back over a bold character attaches the caret to it, inside.
  await page.keyboard.press('End');
  await page.keyboard.type('s');
  await expect(editor.locator('.ne-h1')).toHaveText('Titles');
  await expect(editor.locator('.ne-h1 strong')).toHaveText('Title');
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.type('X');
  await expect(editor.locator('.ne-h1 strong')).toHaveText('TitleX');
  expect(await md(page)).toContain('# **TitleX**s');
  await page.keyboard.press('End');

  // Enter after a heading gives a plain line; a backtick rule makes code and the next
  // character is plain.
  await page.keyboard.press('Enter');
  await page.keyboard.type('use `x` y');
  await expect(editor.locator('.ne-line').last().locator('code')).toHaveText('x');
  expect(await md(page)).toContain('use `x` y\n');

  // Backspace at the line start clears nothing on a plain line and joins it to the heading.
  await page.keyboard.press('Home');
  await page.keyboard.press('Backspace');
  await expect(editor.locator('.ne-h1')).toHaveText('TitleXsuse x y');
  await page.keyboard.press('Control+z');
  await expect(editor.locator('.ne-h1')).toHaveText('TitleXs');
  expect(await md(page)).toBe('Hello world\n- first\n- second\n# **TitleX**s\nuse `x` y\n');

  // Delete at a line end joins the next line.
  await select(page, await lineEnd(page, 0));
  await page.keyboard.press('Delete');
  expect(await md(page)).toBe('Hello worldfirst\n- second\n# **TitleX**s\nuse `x` y\n');
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('Hello world\n- first\n- second\n# **TitleX**s\nuse `x` y\n');

  // Undo groups typed words; redo brings them back.
  await select(page, await lineEnd(page, 4));
  await page.keyboard.type(' more');
  await page.keyboard.press('Control+z');
  expect(await md(page)).toContain('use `x` y\n');
  await page.keyboard.press('Control+Shift+z');
  expect(await md(page)).toContain('use `x` y more\n');
});

test('tables: the row rule, Tab between cells, Tab in the last cell adds a row', async ({ page }) => {
  const editor = await open(page);
  await page.keyboard.type('|a|b|');
  await page.keyboard.press('Enter');
  await expect(editor.locator('table th')).toHaveCount(2);
  await expect(editor.locator('table tr')).toHaveCount(2);
  await page.keyboard.type('1');
  await page.keyboard.press('Tab');
  await page.keyboard.type('2');
  await page.keyboard.press('Tab');
  await expect(editor.locator('table tr')).toHaveCount(3);
  await page.keyboard.type('3');
  // Shift+Tab selects the previous row's last cell; typing replaces it.
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.type('X');
  expect(await md(page)).toBe('| a | b |\n| --- | --- |\n| 1 | X |\n| 3 |  |\n');
  // Enter in a table adds a plain line after the row.
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Enter');
  await page.keyboard.type('after');
  expect(await md(page)).toBe('| a | b |\n| --- | --- |\n| 1 | X |\n| 3 |  |\n\nafter\n');
  // Backspace at a cell start steps back to the previous cell; at a filled row's start it does nothing.
  await select(page, 2);
  await page.keyboard.press('Backspace');
  expect(await caret(page)).toEqual({ anchor: 1, focus: 1 });
  await select(page, 0);
  await page.keyboard.press('Backspace');
  expect(await md(page)).toBe('| a | b |\n| --- | --- |\n| 1 | X |\n| 3 |  |\n\nafter\n');
});

test('paste: inline markdown, line structure, and a table cell', async ({ page }) => {
  const editor = await open(page, 'ab\n');
  await select(page, 1);
  await paste(page, 'x **y**');
  await expect(editor.locator('.ne-line').first().locator('strong')).toHaveText('y');
  expect(await md(page)).toBe('ax **y**b\n');
  await select(page, 1);
  await paste(page, '# Head\n- one\n- two\n');
  expect(await md(page)).toBe('a\n# Head\n- one\n- two\n\nx **y**b\n');
  await expect(editor.locator('.ne-h1')).toHaveText('Head');
  await expect(editor.locator('.ne-bullet')).toHaveCount(2);
  // The caret ends on the last pasted line; typing continues there.
  await page.keyboard.type('!');
  expect(await md(page)).toContain('- two!\n');
  // Pasted text after bold text stays plain.
  await open(page, '**ab**\n');
  await select(page, 2);
  await paste(page, 'cd');
  expect(await md(page)).toBe('**ab**cd\n');
  // Into a cell: one line, no separators.
  await open(page, '| a | b |\n|---|---|\n| 1 | 2 |\n');
  await select(page, 0);
  await paste(page, 'p\nq | r');
  expect(await md(page)).toBe('| p q \\| ra | b |\n| --- | --- |\n| 1 | 2 |\n');
});

test('copy and cut put markdown on the clipboard', async ({ page }) => {
  await open(page, '# Head\nsome **bold** text\n');
  await select(page, 2, 14);
  expect(await copyText(page)).toBe('# ad\nsome **bold**');
  await page.evaluate(() => {
    const dt = new DataTransfer();
    const editor = document.querySelector('[data-testid=editor]') as HTMLElement;
    editor.dispatchEvent(new ClipboardEvent('cut', { clipboardData: dt, bubbles: true, cancelable: true }));
  });
  expect(await md(page)).toBe('# He text\n');
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('# Head\nsome **bold** text\n');
});

test('arrows step by code point, Ctrl+K asks for a link, a remote edit keeps the caret', async ({ page }) => {
  const editor = await open(page, 'a **b** c\n');
  await select(page, 0);
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  expect(await caret(page)).toEqual({ anchor: 2, focus: 2 });
  await page.keyboard.press('ArrowRight');
  expect(await caret(page)).toEqual({ anchor: 3, focus: 3 });
  await page.keyboard.press('Shift+ArrowLeft');
  expect(await caret(page)).toEqual({ anchor: 3, focus: 2 });
  await page.keyboard.press('Control+k');
  expect(await page.evaluate(() => window.__notes.linkRequests)).toEqual([{ from: 2, to: 3, url: null }]);
  await page.evaluate(() => window.__notes.input.setLink(2, 3, 'https://example.com/'));
  await expect(editor.locator('a')).toHaveAttribute('href', 'https://example.com/');
  expect(await md(page)).toBe('a [**b**](https://example.com/) c\n');

  // Someone else inserts before the caret: it stays on its characters.
  await select(page, 5);
  await page.evaluate(() => window.__notes.remoteInsert(0, 'REMOTE '));
  expect(await caret(page)).toEqual({ anchor: 12, focus: 12 });
  await page.keyboard.type('!');
  expect(await md(page)).toBe('REMOTE a [**b**](https://example.com/) c!\n');
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('REMOTE a [**b**](https://example.com/) c\n');
});

test('read-only: no edits, selection and copy still work', async ({ page }) => {
  const editor = await open(page, 'fixed\n');
  await page.evaluate(() => window.__notes.input.setReadOnly(true));
  await expect(editor).toHaveAttribute('contenteditable', 'false');
  await select(page, 0, 5);
  await page.keyboard.type('x');
  await page.keyboard.press('Control+b');
  await paste(page, 'nope');
  expect(await md(page)).toBe('fixed\n');
  expect(await copyText(page)).toBe('fixed');
});

test('cross-line edits: select-all typing, Backspace over lines, Control+Backspace at a line start', async ({ page }) => {
  const editor = await open(page, 'one\n- two\n# three\n');
  await select(page, 2, 7); // on|e\ntwo|: the text has no markdown prefixes
  await page.keyboard.press('Backspace');
  expect(await md(page)).toBe('on\n# three\n');
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('one\n- two\n# three\n');
  await select(page, 4); // the bullet's start
  await page.keyboard.press('Control+Backspace');
  expect(await md(page)).toBe('one\n- two\n# three\n'.replace('- two', 'two'));
  await page.keyboard.press('Control+Backspace');
  expect(await md(page)).toBe('onetwo\n# three\n');
  await page.keyboard.press('Control+a');
  await page.keyboard.type('fresh');
  expect(await md(page)).toBe('fresh\n');
  await expect(editor.locator('.ne-line')).toHaveCount(1);
  // The replacement is one entry, the rest of the word a typing burst.
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('f\n');
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('onetwo\n# three\n');
});

test('IME: a composition is committed once, after remote frames that arrived meanwhile', async ({ page, browserName }) => {
  test.skip(browserName !== 'chromium', 'composition is driven over CDP');
  await open(page, 'ab\n');
  await select(page, 1);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: 'か', selectionStart: 1, selectionEnd: 1 });
  expect(await page.evaluate(() => window.__notes.text())).toBe('ab\n');
  await page.evaluate(() => window.__notes.remoteInsert(0, 'R'));
  expect(await page.evaluate(() => window.__notes.text())).toBe('Rab\n');
  await cdp.send('Input.insertText', { text: '漢' });
  expect(await md(page)).toBe('Ra漢b\n');
  expect(await page.evaluate(() => window.__notes.extractText(document.querySelector('[data-testid=editor]') as HTMLElement))).toBe('Ra漢b\n');
  expect(await caret(page)).toEqual({ anchor: 3, focus: 3 });
  await page.keyboard.press('Control+z');
  expect(await md(page)).toBe('Rab\n');
});
