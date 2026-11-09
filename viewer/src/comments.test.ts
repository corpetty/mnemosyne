// @vitest-environment node
// Readers' comments, the pure parts: the COMMENT ops (byte for byte where the vectors pin them),
// boxing them for the owner, ownership by rid, the pending states, and the reader's identity with
// and without storage. Node for WebCrypto; the vectors are publication_live_v1.json.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import { canonicalJson, hex } from '$lib/sealed/envelope';
import { boxOp, deriveLiveKeys, openBox, signPublic } from '$lib/sealed/signed';
import {
  COUNT_FROM,
  MAX_TEXT,
  NOT_SENT_MS,
  HeldComments,
  MAX_HELD,
  ReaderComments,
  ReaderIdentity,
  anchorKey,
  anchorLine,
  commentsOpen,
  createOp,
  deleteOp,
  editOp,
  heldKey,
  itemAnchor,
  meetingAnchor,
  parseThreads,
  passageAnchor,
  quoteAnchor,
  replyOp,
  sendable,
  storageKeys,
  type KeyValue,
  type ReaderOp
} from './comments';
import { b64urlDecode, fromHex, type Manifest } from './format';

const vec = JSON.parse(
  readFileSync(fileURLToPath(new URL('../../backend/tests/vectors/publication_live_v1.json', import.meta.url)), 'utf-8')
);
const ROOM: string = vec.room;
const KEY = b64urlDecode(vec.key_b64url);
const OWNER_BOX_PUB = fromHex(vec.owner.box_public_hex);
const OWNER_BOX = fromHex(vec.owner.box_private_hex);
const READER = fromHex(vec.reader.private_hex);
const MANIFEST: Manifest = JSON.parse(vec.manifest.json);
const PUB = MANIFEST.id;
const AT = '2026-11-05T10:20:00.000Z';

/** A Map behind the localStorage calls, with what was written. */
class Store implements KeyValue {
  map = new Map<string, string>();
  getItem(k: string): string | null {
    return this.map.get(k) ?? null;
  }
  setItem(k: string, v: string): void {
    this.map.set(k, v);
  }
}

function identity(name = 'Ana', key: Uint8Array | null = READER): { id: ReaderIdentity; store: Store } {
  const store = new Store();
  const keys = storageKeys(PUB);
  if (name) store.setItem(keys.name, name);
  if (key) store.setItem(keys.key, hex(key));
  return { id: new ReaderIdentity(PUB, () => store), store };
}

/** A comment as THREADS carries it. */
const comment = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  parent_id: null,
  author: 'Ana (reader)',
  rid: vec.reader.rid,
  text: `text ${id}`,
  anchor: { target: '', target_id: null, quote_id: null, quote: null },
  created_at: '2026-11-05T10:00:00Z',
  updated_at: '2026-11-05T10:00:00Z',
  resolved: false,
  deleted: false,
  detached: false,
  changed: false,
  ...over
});
const thread = (root: object, replies: object[] = []) => ({ root, replies });

function model(opts: { name?: string; key?: Uint8Array | null } = {}) {
  const { id, store } = identity(opts.name ?? 'Ana', opts.key === undefined ? READER : opts.key);
  let t = Date.parse(AT);
  const sent: { op: ReaderOp; key: Uint8Array }[] = [];
  const changes = vi.fn();
  const m = new ReaderComments(id, (op, key) => sent.push({ op, key }), () => t, changes);
  return { m, store, sent, changes, advance: (ms: number) => (t += ms), at: () => new Date(t).toISOString() };
}

describe('ops', () => {
  it('a create on a decision is the vectors\' op, byte for byte', () => {
    const v = JSON.parse(vec.samples.comment.json);
    const op = createOp(v.id, v.text, v.name, v.at, itemAnchor('decision', 'd1'));
    expect(new TextDecoder().decode(canonicalJson(op))).toBe(vec.samples.comment.json);
  });

  it('builds every kind with exactly the seven keys', () => {
    const keys = ['anchor', 'at', 'id', 'name', 'op', 'parent_id', 'text'];
    expect(createOp('0123456789ab', 'On the quote', 'Ana', AT, quoteAnchor('q1'))).toEqual({
      op: 'create',
      id: '0123456789ab',
      parent_id: null,
      text: 'On the quote',
      name: 'Ana',
      at: AT,
      anchor: { target: 'quote', target_id: null, quote_id: 'q1', exact: null, prefix: null, suffix: null }
    });
    const body = passageAnchor('We agreed on the launch date — naïve estimates were dropped.', 13, 28)!;
    expect(createOp('0123456789ab', 'Which date?', 'Ana', AT, body).anchor).toEqual({
      target: 'body',
      target_id: null,
      quote_id: null,
      exact: 'the launch date',
      prefix: 'We agreed on ',
      suffix: ' — naïve estimates were dropped.'
    });
    expect(createOp('0123456789ab', 'Overall', 'Ana', AT, meetingAnchor()).anchor).toEqual({
      target: '',
      target_id: null,
      quote_id: null,
      exact: null,
      prefix: null,
      suffix: null
    });
    expect(replyOp('ba9876543210', '0123456789ab', 'Agreed', 'Ana', AT)).toEqual({
      op: 'reply',
      id: 'ba9876543210',
      parent_id: '0123456789ab',
      text: 'Agreed',
      name: 'Ana',
      at: AT,
      anchor: null
    });
    expect(editOp('0123456789ab', 'Changed', 'Ana', AT)).toEqual({
      op: 'edit',
      id: '0123456789ab',
      parent_id: null,
      text: 'Changed',
      name: 'Ana',
      at: AT,
      anchor: null
    });
    expect(deleteOp('0123456789ab', 'Ana', AT)).toEqual({
      op: 'delete',
      id: '0123456789ab',
      parent_id: null,
      text: null,
      name: 'Ana',
      at: AT,
      anchor: null
    });
    for (const op of [
      createOp('a', 'x', 'n', AT, meetingAnchor()),
      replyOp('a', 'b', 'x', 'n', AT),
      editOp('a', 'x', 'n', AT),
      deleteOp('a', 'n', AT)
    ]) {
      expect(Object.keys(op).sort()).toEqual(keys);
    }
  });

  it('takes a passage with 32 characters of context and without its outer whitespace', () => {
    const full = `${'a'.repeat(40)}  the passage  ${'b'.repeat(40)}`;
    const a = passageAnchor(full, 40, 55)!;
    expect(a.exact).toBe('the passage');
    expect(a.prefix).toBe(`${'a'.repeat(30)}  `);
    expect(a.prefix!.length).toBe(32);
    expect(a.suffix).toBe(`  ${'b'.repeat(30)}`);
    expect(passageAnchor('at the start', 0, 2)).toMatchObject({ exact: 'at', prefix: '', suffix: ' the start' });
    expect(passageAnchor('a   b', 1, 4)).toBeNull();
  });

  it('caps the text at 4,000 characters, counted as code points', () => {
    expect(COUNT_FROM).toBe(3500);
    expect(sendable('x'.repeat(MAX_TEXT))).toBe(true);
    expect(sendable('x'.repeat(MAX_TEXT + 1))).toBe(false);
    expect(sendable('😀'.repeat(MAX_TEXT))).toBe(true); // 8,000 UTF-16 units, 4,000 characters
    expect(sendable('   ')).toBe(false);
  });
});

describe('boxing', () => {
  it('a create boxed with the reader key opens for the owner, with the rid of the reader key', async () => {
    const keys = await deriveLiveKeys(KEY);
    const op = createOp('0123456789ab', 'Why Friday?', 'Ana', AT, itemAnchor('decision', 'd1'));
    const env = await boxOp(keys, ROOM, op, READER, OWNER_BOX_PUB);
    const opened = await openBox(keys, ROOM, env, OWNER_BOX);
    expect(opened.op).toEqual(op);
    expect(opened.rid).toBe(vec.reader.rid);
    expect(hex(opened.readerPub)).toBe(vec.reader.public_hex);
    // Neither the envelope nor what the owner opens carries the private key.
    expect(hex(env)).not.toContain(vec.reader.private_hex);
    expect(JSON.stringify(opened.op)).not.toContain(vec.reader.private_hex);
  });

  it('every op kind round-trips', async () => {
    const keys = await deriveLiveKeys(KEY);
    for (const op of [
      createOp('0123456789ab', 'On a quote', 'Ana', AT, quoteAnchor('q1')),
      createOp('0123456789ab', 'On a passage', 'Ana', AT, passageAnchor('We agreed on the date.', 3, 9)!),
      replyOp('ba9876543210', '0123456789ab', 'A reply', 'Ana', AT),
      editOp('0123456789ab', 'Edited', 'Ana', AT),
      deleteOp('0123456789ab', 'Ana', AT)
    ]) {
      const opened = await openBox(keys, ROOM, await boxOp(keys, ROOM, op, READER, OWNER_BOX_PUB), OWNER_BOX);
      expect(opened.op).toEqual(op);
    }
  });
});

describe('threads and anchors', () => {
  it('parses THREADS, leaving out what is malformed', () => {
    const raw = JSON.parse(vec.samples.threads.json).threads;
    const ts = parseThreads([...raw, null, { root: { text: 'no id' } }, 'x']);
    expect(ts).toHaveLength(1);
    expect(ts[0].root).toMatchObject({ id: '5f3a9c1e7b20', rid: vec.reader.rid, author: 'Ana (reader)' });
    expect(ts[0].root.anchor).toEqual({ target: 'decision', target_id: 'd1', quote_id: null, quote: null });
    expect(ts[0].replies[0]).toMatchObject({ author: 'Corey', rid: null, parent_id: '5f3a9c1e7b20' });
  });

  it('keys and quotes anchors from the manifest', () => {
    const a = (o: Partial<{ target: string; target_id: string | null; quote_id: string | null; quote: string | null }>) => ({
      target: '',
      target_id: null,
      quote_id: null,
      quote: null,
      ...o
    });
    expect(anchorKey(a({ target: 'decision', target_id: 'd1' }))).toBe('decision:d1');
    expect(anchorKey(a({ target: 'segment', quote_id: 'q1' }))).toBe('quote:q1');
    expect(anchorKey(a({ target: 'quote', quote_id: 'q1' }))).toBe('quote:q1');
    expect(anchorKey(a({ target: 'body', quote: 'x' }))).toBe('body');
    expect(anchorKey(a({ target: 'notes', quote: 'x' }))).toBe('notes');
    expect(anchorLine(a({ target: 'notes', quote: ' the notes line ' }), MANIFEST)).toBe('the notes line');
    expect(passageAnchor('Some notes here', 5, 10, 'notes')).toMatchObject({ target: 'notes', exact: 'notes', prefix: 'Some ', suffix: ' here' });
    expect(anchorKey(a({}))).toBeNull();
    expect(anchorKey(itemAnchor('item', 'a1'))).toBe('item:a1');
    expect(anchorLine(a({ target: 'decision', target_id: 'd1' }), MANIFEST)).toBe('Ship on Friday 📦');
    expect(anchorLine(a({ target: 'item', target_id: 'a1' }), MANIFEST)).toBe('Write the release notes');
    expect(anchorLine(a({ target: 'body', quote: 'the launch date' }), MANIFEST)).toBe('the launch date');
    expect(anchorLine(a({ target: 'decision', target_id: 'gone', quote: 'Old text' }), MANIFEST)).toBe('Old text');
    expect(anchorLine(a({}), MANIFEST)).toBeNull();
  });

  it('opens comments only with live keys in the manifest and comments on in META', () => {
    const unlive = { ...MANIFEST, live: undefined };
    expect(commentsOpen(MANIFEST, { comments: true })).toBe(true);
    expect(commentsOpen(MANIFEST, { comments: false })).toBe(false);
    expect(commentsOpen(MANIFEST, null)).toBe(false);
    expect(commentsOpen(unlive, { comments: true })).toBe(false);
    expect(commentsOpen({ live: { sign: 'zz', box: 'zz' } }, { comments: true })).toBe(false);
  });
});

describe('ReaderComments', () => {
  it('knows its own comments by rid, and edits and deletes only those', async () => {
    const { m, sent } = model();
    await m.init();
    expect(m.rid).toBe(vec.reader.rid);
    m.setThreads([thread(comment('aaaaaaaaaaaa')), thread(comment('bbbbbbbbbbbb', { rid: '0000000000000000', author: 'Bo (reader)' }))]);
    const [mine, theirs] = m.threads.map((t) => t.root);
    expect(m.isMine(mine)).toBe(true);
    expect(m.isMine(theirs)).toBe(false);
    expect(m.isMine({ rid: null })).toBe(false);
    expect(m.edit(theirs, 'hijack')).toBeNull();
    expect(m.remove(theirs)).toBeNull();
    expect(sent).toEqual([]);
    expect(m.edit(mine, 'better')?.op).toBe('edit');
    expect(m.remove(mine)?.op).toBe('delete');
    expect(sent.map((s) => [s.op.op, s.op.id])).toEqual([
      ['edit', 'aaaaaaaaaaaa'],
      ['delete', 'aaaaaaaaaaaa']
    ]);
  });

  it('has no own comments before a key exists, and makes one on the first op', async () => {
    const { m, store, sent } = model({ key: null });
    await m.init();
    expect(m.rid).toBeNull();
    const op = m.create({ anchor: itemAnchor('decision', 'd1'), line: 'Ship on Friday 📦' }, '  Why?  ')!;
    expect(op).toMatchObject({ op: 'create', text: 'Why?', name: 'Ana', parent_id: null });
    expect(op.id).toMatch(/^[0-9a-f]{12}$/);
    const stored = store.getItem(storageKeys(PUB).key)!;
    expect(stored).toMatch(/^[0-9a-f]{64}$/);
    expect(hex(sent[0].key)).toBe(stored);
    await vi.waitFor(() => expect(m.rid).not.toBeNull());
    // The pending thread is the reader's own and shows where it is.
    const v = m.view();
    expect(v.open[0].root).toMatchObject({ pending: true, author: 'Ana (reader)', rid: m.rid });
    expect(anchorKey(v.open[0].root.anchor)).toBe('decision:d1');
    expect(m.counts()).toEqual({ 'decision:d1': 1 });
    // The op itself never carries the key.
    expect(JSON.stringify(sent[0].op)).not.toContain(stored);
  });

  it('pending → confirmed when a THREADS shows it', () => {
    const { m } = model();
    const op = m.create({ anchor: meetingAnchor(), line: null }, 'Hello')!;
    expect(m.stateOf(op.id)).toBe('sending');
    m.setThreads([]);
    expect(m.stateOf(op.id)).toBe('sending');
    m.setThreads([thread(comment(op.id, { text: 'Hello' }))]);
    expect(m.stateOf(op.id)).toBeNull();
    expect(m.view().open[0].root.pending).toBe(false);
  });

  it('pending → not sent after 15 s; Retry sends the same op again', () => {
    const { m, sent, advance } = model();
    const op = m.create({ anchor: meetingAnchor(), line: null }, 'Hello')!;
    expect(m.nextDeadline()).toBe(Date.parse(AT) + NOT_SENT_MS);
    advance(NOT_SENT_MS - 1);
    expect(m.stateOf(op.id)).toBe('sending');
    advance(1);
    expect(m.stateOf(op.id)).toBe('not_sent');
    expect(m.nextDeadline()).toBeNull();
    expect(m.retry(op.id)).toEqual([op]);
    expect(sent.map((s) => s.op)).toEqual([op, op]);
    expect(sent[1].op).toBe(op); // same id, same `at`: the bridge takes it once
    expect(m.stateOf(op.id)).toBe('sending');
    m.setThreads([thread(comment(op.id, { text: 'Hello' }))]);
    expect(m.stateOf(op.id)).toBeNull();
  });

  it('edits and deletes are pending the same way; a redacted edit counts by its updated_at', async () => {
    const { m, advance } = model();
    await m.init();
    m.setThreads([thread(comment('aaaaaaaaaaaa', { text: 'mail me' }))]);
    const c = m.threads[0].root;
    m.edit(c, 'mail me at ana@example.com');
    expect(m.view().open[0].root).toMatchObject({ text: 'mail me at ana@example.com', pending: true });
    expect(m.stateOf(c.id)).toBe('sending');
    advance(NOT_SENT_MS);
    expect(m.stateOf(c.id)).toBe('not_sent');
    // The owner redacts the address on the way back: the text differs, the time moved.
    m.setThreads([thread(comment('aaaaaaaaaaaa', { text: 'mail me at [email]', updated_at: '2026-11-05T10:21:00Z' }))]);
    expect(m.stateOf(c.id)).toBeNull();
    m.remove(m.threads[0].root);
    expect(m.view().open[0].root).toMatchObject({ deleted: true, text: '', pending: true });
    m.setThreads([thread(comment('aaaaaaaaaaaa', { deleted: true, text: '' }))]);
    expect(m.stateOf(c.id)).toBeNull();
  });

  it('a reply shows under its thread at once', () => {
    const { m } = model();
    m.setThreads([thread(comment('aaaaaaaaaaaa', { rid: null, author: 'Corey' }))]);
    const op = m.reply('aaaaaaaaaaaa', 'Thanks')!;
    expect(op).toMatchObject({ op: 'reply', parent_id: 'aaaaaaaaaaaa', anchor: null });
    const r = m.view().open[0].replies;
    expect(r.map((x) => [x.id, x.pending, x.author])).toEqual([[op.id, true, 'Ana (reader)']]);
  });

  it('refuses to send without a name or over the cap', () => {
    const { m, sent } = model({ name: '' });
    expect(m.create({ anchor: meetingAnchor(), line: null }, 'Hello')).toBeNull();
    expect(m.setName('   ')).toBe(false);
    expect(m.setName('  Ana   Lima ')).toBe(true);
    expect(m.name).toBe('Ana Lima');
    expect(m.create({ anchor: meetingAnchor(), line: null }, 'x'.repeat(MAX_TEXT + 1))).toBeNull();
    expect(sent).toEqual([]);
    expect(m.create({ anchor: meetingAnchor(), line: null }, 'Hello')?.name).toBe('Ana Lima');
  });
});

describe('comments waiting for the owner\'s approval', () => {
  const RID: string = vec.reader.rid;
  const OTHER = '0123456789abcdef';
  const held = (store: Store): Record<string, { text: string }> => JSON.parse(store.getItem(heldKey(PUB)) ?? '{}');
  const meeting = { anchor: meetingAnchor(), line: null };

  it('keeps the words of what it sends, under one key per publication, by comment id', async () => {
    const { m, store } = model();
    await m.init();
    const a = m.create(meeting, 'Overall good')!;
    const b = m.create({ anchor: itemAnchor('decision', 'd1'), line: null }, 'Why Friday?')!;
    expect(heldKey(PUB)).toBe(`mnemosyne.reader.${PUB}.held`);
    expect(Object.keys(held(store))).toEqual([a.id, b.id]);
    expect(held(store)[b.id]).toMatchObject({ text: 'Why Friday?', name: 'Ana', parent_id: null, at: AT });
    expect(held(store)[b.id]).toHaveProperty('anchor.target_id', 'd1');
  });

  it('shows its own listed comment as waiting, from the kept text, and not as "Sending…"', async () => {
    const { m, sent } = model();
    await m.init();
    const op = m.create({ anchor: itemAnchor('decision', 'd1'), line: null }, 'Secret-ish remark')!;
    expect(m.stateOf(op.id)).toBe('sending');
    // The owner's THREADS: nothing to read, the id under this reader's rid, no text anywhere.
    const pending = { [RID]: [op.id] };
    expect(JSON.stringify(pending)).not.toContain('remark');
    m.setThreads([], pending);
    expect(m.stateOf(op.id)).toBeNull();
    expect(m.pending.list).toEqual([]);
    const root = m.view().open[0].root;
    expect(root).toMatchObject({ id: op.id, waiting: true, pending: false, text: 'Secret-ish remark', author: 'Ana (reader)', rid: RID });
    expect(anchorKey(root.anchor)).toBe('decision:d1');
    expect(m.counts()).toEqual({ 'decision:d1': 1 });
    expect(sent).toHaveLength(1); // nothing was sent again
  });

  it('shows a waiting reply under its thread', async () => {
    const { m } = model();
    await m.init();
    m.setThreads([thread(comment('aaaaaaaaaaaa', { rid: OTHER, author: 'Bo (reader)' }))], {});
    const op = m.reply('aaaaaaaaaaaa', 'I agree')!;
    m.setThreads([thread(comment('aaaaaaaaaaaa', { rid: OTHER, author: 'Bo (reader)' }))], { [RID]: [op.id] });
    const [t] = m.view().open;
    expect(t.replies.map((r) => [r.id, r.waiting, r.text])).toEqual([[op.id, true, 'I agree']]);
  });

  it('drops the kept text when THREADS shows the comment itself (approved)', async () => {
    const { m, store } = model();
    await m.init();
    const op = m.create(meeting, 'Overall good')!;
    m.setThreads([], { [RID]: [op.id] });
    expect(held(store)[op.id]).toBeDefined();
    m.setThreads([thread(comment(op.id, { text: 'Overall good' }))], {});
    expect(held(store)).toEqual({});
    const root = m.view().open[0].root;
    expect(root).toMatchObject({ id: op.id, pending: false });
    expect(root.waiting).toBeUndefined();
  });

  it('drops the kept text when a THREADS no longer lists it as pending (hidden, deleted)', async () => {
    const { m, store } = model();
    await m.init();
    const a = m.create(meeting, 'One')!;
    const b = m.create(meeting, 'Two')!;
    m.setThreads([], { [RID]: [a.id, b.id] });
    expect(m.view().open.map((t) => t.root.id)).toEqual([a.id, b.id]);
    m.setThreads([], { [RID]: [b.id] }); // the owner hid the first
    expect(Object.keys(held(store))).toEqual([b.id]);
    expect(m.view().open.map((t) => t.root.id)).toEqual([b.id]);
    m.setThreads([], {});
    expect(held(store)).toEqual({});
    expect(m.view().open).toEqual([]);
  });

  it('keeps the text while the op is still on its way, whatever an older THREADS says', async () => {
    const { m, store } = model();
    await m.init();
    const op = m.create(meeting, 'Just sent')!;
    m.setThreads([], {}); // sent after this THREADS was made: the owner has not seen it yet
    expect(held(store)[op.id]).toBeDefined();
    expect(m.stateOf(op.id)).toBe('sending');
    m.setThreads([], { [RID]: [op.id] });
    expect(m.stateOf(op.id)).toBeNull();
  });

  it('only believes the list under its own rid', async () => {
    const { m } = model();
    await m.init();
    const op = m.create(meeting, 'Mine')!;
    m.setThreads([], { [OTHER]: [op.id] }); // somebody else claiming the id: not ours
    expect(m.stateOf(op.id)).toBe('sending');
    expect(m.view().open[0].root).toMatchObject({ pending: true });
    expect(m.view().open[0].root.waiting).toBeUndefined();
  });

  it('shows nothing for a listed comment whose text is gone (a cleared browser, another page)', async () => {
    const { m } = model();
    await m.init();
    m.setThreads([], { [RID]: ['0000000000aa'] });
    expect(m.view()).toEqual({ open: [], resolved: [] });
    expect(m.counts()).toEqual({});
  });

  it('keeps the words of an edit that has to wait again', async () => {
    const { m } = model();
    await m.init();
    m.setThreads([thread(comment('aaaaaaaaaaaa', { text: 'Before' }))], {});
    const c = m.threads[0].root;
    m.edit(c, 'After the edit');
    // Approved text edited: the owner takes it back to waiting, so THREADS lists it and not it.
    m.setThreads([], { [RID]: [c.id] });
    expect(m.stateOf(c.id)).toBeNull();
    expect(m.view().open[0].root).toMatchObject({ id: c.id, waiting: true, text: 'After the edit' });
  });

  it('is tied to the identity that learns its rid late: nothing is dropped before it is known', async () => {
    const { m, store } = model();
    // A later visit: the kept text is there, THREADS comes before the rid is known.
    store.setItem(heldKey(PUB), JSON.stringify({ aaaaaaaaaaaa: { parent_id: null, text: 'From before', name: 'Ana', at: AT, anchor: { target: '' } } }));
    m.setThreads([], { [RID]: ['aaaaaaaaaaaa'] });
    expect(m.rid).toBeNull();
    expect(held(store).aaaaaaaaaaaa).toBeDefined();
    await m.init();
    expect(m.view().open[0].root).toMatchObject({ id: 'aaaaaaaaaaaa', waiting: true, text: 'From before' });
  });

  it('is kept in memory when storage throws, and bounded', () => {
    const blocked = new HeldComments(PUB, () => ({
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      }
    }));
    const entry = { parent_id: null, text: 'x', name: 'Ana', at: AT, anchor: { target: '', target_id: null, quote_id: null, quote: null } };
    blocked.set('aaaaaaaaaaaa', entry);
    expect(blocked.get('aaaaaaaaaaaa')?.text).toBe('x');
    blocked.delete('aaaaaaaaaaaa');
    expect(blocked.all()).toEqual({});
    for (let i = 0; i < MAX_HELD + 5; i++) blocked.set(i.toString(16).padStart(12, '0'), entry);
    expect(Object.keys(blocked.all())).toHaveLength(MAX_HELD);
    expect(blocked.get('000000000000')).toBeUndefined(); // the oldest went
    // Garbage in storage is ignored.
    const junk = new Store();
    junk.setItem(heldKey(PUB), '{"a":{"text":3},"b":[],"c":null');
    expect(new HeldComments(PUB, () => junk).all()).toEqual({});
    junk.setItem(heldKey(PUB), '[1,2]');
    expect(new HeldComments(PUB, () => junk).all()).toEqual({});
  });
});

describe('ReaderIdentity', () => {
  it('keeps the name and key under keys scoped to the publication id', () => {
    const store = new Store();
    const id = new ReaderIdentity(PUB, () => store);
    expect(id.name).toBe('');
    expect(id.key).toBeNull();
    id.name = 'Ana';
    const key = id.ensureKey();
    expect([...store.map.keys()].sort()).toEqual([`mnemosyne.reader.${PUB}.key`, `mnemosyne.reader.${PUB}.name`]);
    expect(id.ensureKey()).toEqual(key);
    // Another publication is another reader.
    const other = new ReaderIdentity('fedcba9876543210', () => store);
    expect(other.name).toBe('');
    expect(other.key).toBeNull();
    // The same publication on a later visit is the same reader.
    expect(new ReaderIdentity(PUB, () => store).key).toEqual(key);
  });

  it('lives in memory when storage throws or is missing', async () => {
    for (const store of [
      () => {
        throw new Error('SecurityError');
      },
      () => null,
      () => ({
        getItem: () => {
          throw new Error('blocked');
        },
        setItem: () => {
          throw new Error('blocked');
        }
      })
    ]) {
      const id = new ReaderIdentity(PUB, store as () => KeyValue | null);
      expect(id.name).toBe('');
      id.name = 'Ana';
      expect(id.name).toBe('Ana');
      const key = id.ensureKey();
      expect(id.key).toEqual(key);
      expect(await id.rid()).toBe((await new ReaderIdentity(PUB, () => {
        const s = new Store();
        s.setItem(storageKeys(PUB).key, hex(key));
        return s;
      }).rid()));
    }
  });

  it('rid is ridOf the stored key\'s public half', async () => {
    const { id } = identity('Ana', READER);
    expect(hex(signPublic(READER))).toBe(vec.reader.public_hex);
    expect(await id.rid()).toBe(vec.reader.rid);
  });
});
