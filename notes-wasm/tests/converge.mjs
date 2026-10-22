// Convergence spike for live shared notes (phase 6, T1).
//
//   scripts/build-notes-wasm.sh && node notes-wasm/tests/converge.mjs
//
// Replicas talk through a fake relay that forwards 0xDE frames from takeDeltas() to every
// other replica and never echoes, the way /ws/notes/{id} will. Clients use the QUEUE 39 fix:
// drain takeDeltas() before applying a remote frame, then applyFrontier(encodeFrontier()).
// Lines starting PASS/FAIL are invariants (a FAIL exits 1); OBS lines are observed behaviour.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { initSync, WasmHashWeb } from '../../src/lib/notes/pkg/notes_wasm.js';

initSync({
	module: readFileSync(new URL('../../src/lib/notes/pkg/notes_wasm_bg.wasm', import.meta.url))
});

const DELTA_TAG = 0xde;
const ARTIFACT_TAG = 0xaf;
let failures = 0;
const check = (ok, what) => {
	console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`);
	if (!ok) failures++;
};
const obs = (what) => console.log(`OBS   ${what}`);
const section = (name) => console.log(`\n== ${name}`);
const show = (s) => JSON.stringify(s);

/** The design's origin: sha256("mnemosyne/notes/" + session_id). */
const originFor = (sessionId) =>
	createHash('sha256').update(`mnemosyne/notes/${sessionId}`).digest('hex');

class Relay {
	constructor() {
		this.clients = [];
		this.queues = new Map();
		this.log = []; // every frame, in arrival order (what shared_notes_ops would hold)
		this.bytes = 0;
		this.frames = 0;
		this.artifactFrames = 0;
	}
	join(client, { replayFrom = null } = {}) {
		this.clients.push(client);
		this.queues.set(client, replayFrom === null ? [] : this.log.slice(replayFrom));
	}
	send(from, frame) {
		if (frame[0] !== DELTA_TAG && frame[0] !== ARTIFACT_TAG) {
			throw new Error(`unexpected frame tag 0x${frame[0].toString(16)}`);
		}
		this.log.push(frame);
		this.bytes += frame.length;
		if (frame[0] === ARTIFACT_TAG) this.artifactFrames++;
		else this.frames++;
		for (const c of this.clients) if (c !== from) this.queues.get(c).push(frame);
	}
	/** Deliver until quiet: every queue empty and nobody has anything left to send. */
	settle() {
		for (let round = 0; round < 100; round++) {
			let moved = false;
			for (const c of this.clients) {
				const q = this.queues.get(c);
				while (q.length) {
					c.receive(q.shift());
					moved = true;
				}
				if (c.flush()) moved = true;
			}
			if (!moved) return;
		}
		throw new Error('relay did not settle');
	}
}

class Client {
	constructor(name, relay, { fix = true, web = null, pushArtifacts = true } = {}) {
		this.name = name;
		this.relay = relay;
		this.fix = fix;
		this.pushArtifacts = pushArtifacts;
		this.web = web ?? new WasmHashWeb();
		this.uselessFrames = 0; // remote 0xDE frames that delivered nothing new
		this.received = 0;
	}
	open(originHex) {
		this.obj = this.web.createSeq(originHex);
		return this.obj;
	}
	/** Send what we have that the relay lacks. Returns true when something went out. */
	flush() {
		let sent = false;
		if (this.pushArtifacts) {
			for (const id of this.web.takeNewArtifacts()) {
				this.relay.send(this, this.web.artifactFrame(id));
				sent = true;
			}
		}
		const d = this.web.takeDeltas();
		if (d.length) {
			this.relay.send(this, d);
			sent = true;
		}
		return sent;
	}
	receive(frame) {
		if (frame[0] === ARTIFACT_TAG) {
			this.web.provideArtifactBytes(frame.subarray(1));
			return;
		}
		if (this.fix) this.flush(); // QUEUE 39: our pending ops go out first...
		const n = this.web.applyDelta(frame);
		this.received++;
		if (n === 0) this.uselessFrames++;
		// ...then the relay holds everything we hold.
		if (this.fix) this.web.applyFrontier(this.web.encodeFrontier());
	}
	text() {
		return this.web.text(this.obj);
	}
	ins(i, s) {
		this.web.textInsert(this.obj, i, s);
	}
	del(i, n) {
		this.web.textRemove(this.obj, i, n);
	}
	spans() {
		return JSON.parse(this.web.markedSpans(this.obj));
	}
	/** Compact span rendering: "text"{kind=v1|v2,...} */
	spanStr() {
		return this.spans()
			.map(
				(s) =>
					JSON.stringify(s.text) +
					(s.marks.length
						? '{' + s.marks.map((m) => `${m.kind}=${m.values.join('|')}`).join(',') + '}'
						: '')
			)
			.join(' ');
	}
	/** [{line, block: [values]}] from the block mark on each '\n'. */
	lines() {
		const out = [];
		let cur = '';
		for (const s of this.spans()) {
			const block = s.marks.find((m) => m.kind === 'block')?.values ?? [];
			for (const ch of s.text) {
				if (ch === '\n') {
					out.push({ line: cur, block });
					cur = '';
				} else cur += ch;
			}
		}
		if (cur) out.push({ line: cur, block: ['<no terminator>'] });
		return out;
	}
	lineStr() {
		return this.lines()
			.map((l) => `${JSON.stringify(l.line)}:${l.block.join('|') || '-'}`)
			.join(' ');
	}
	/** Character indices (code points) of text that carries `kind`. */
	marked(kind) {
		let out = '';
		for (const s of this.spans()) if (s.marks.some((m) => m.kind === kind)) out += s.text;
		return out;
	}
}

const sameBytes = (a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)) === 0;
const allEqual = (clients) => {
	const t = clients[0].text();
	const e = clients[0].web.encode();
	return clients.every((c) => c.text() === t && sameBytes(c.web.encode(), e));
};

// ---------------------------------------------------------------------------
// 1. Sync scenario, with and without the QUEUE 39 client fix.
function scenario(fix, log) {
	const relay = new Relay();
	const origin = originFor('a1b2c3d4e5f6');
	const A = new Client('A', relay, { fix });
	const B = new Client('B', relay, { fix });
	const objA = A.open(origin);
	const objB = B.open(origin);
	const createDelta = A.web.takeDeltas().length + B.web.takeDeltas().length;
	relay.join(A);
	relay.join(B);
	if (log) {
		check(objA === objB && objA === WasmHashWeb.seqId(origin), 'both replicas open the same seq id from the origin (no create race)');
		obs(`createSeq authors no op: takeDeltas() right after it is ${createDelta} bytes`);
	}

	A.ins(0, 'Hello world\n');
	relay.settle();
	if (log) check(B.text() === 'Hello world\n', 'B receives A\'s first line');

	// Concurrent inserts at the same position (both before either is delivered).
	A.ins(5, '-AAA');
	B.ins(5, '+BBB');
	A.flush();
	B.flush();
	relay.settle();
	if (log) {
		check(A.text() === B.text(), `concurrent same-position inserts converge: ${show(A.text())}`);
		check(/-AAA\+BBB|\+BBB-AAA/.test(A.text()), 'concurrent burst inserts do not interleave');
	}

	// Concurrent typing, one keystroke (one frame) at a time, interleaved deliveries.
	const a = 'xyz';
	const b = '123';
	for (let i = 0; i < 3; i++) {
		A.ins(i, a[i]);
		A.flush();
		B.ins(i, b[i]);
		B.flush();
	}
	relay.settle();
	if (log) {
		check(A.text() === B.text(), `keystroke-by-keystroke concurrent typing converges: ${show(A.text())}`);
		check(/^(xyz123|123xyz)/.test(A.text()), 'keystroke runs do not interleave');
	}

	// Delete races insert: A deletes "world", B types inside it.
	const w = A.text().indexOf('world');
	A.del(w, 5);
	B.ins(w + 3, '[big]');
	A.flush();
	B.flush();
	relay.settle();
	if (log) {
		check(A.text() === B.text(), `delete vs insert converges: ${show(A.text())}`);
		check(A.text().includes('[big]') && !A.text().includes('world'), 'the insert survives, the deleted text stays deleted');
	}

	// A delete racing a delete of an overlapping range.
	const h = A.text().indexOf('Hello');
	A.del(h, 3); // "Hel"
	B.del(h + 1, 3); // "ell"
	relay.settle();
	if (log) check(A.text() === B.text() && !A.text().includes('Hel') && A.text().includes('o'), `overlapping deletes converge: ${show(A.text())}`);

	// Fold point: the relay's snapshot (A's encode), then more edits arrive as frames.
	const snapshot = A.web.encode();
	const foldAt = relay.log.length;
	A.ins(A.text().length - 1, ' after-fold-A');
	A.flush();
	B.ins(0, 'B-after-fold ');
	B.flush();
	relay.settle();

	// C joins: decode(snapshot), then the unfolded frames, then edits.
	const C = new Client('C', relay, { fix, web: WasmHashWeb.decode(snapshot) });
	C.obj = objA;
	if (fix) {
		// The relay holds the snapshot: say so, or the first takeDeltas ships it all back.
		C.web.applyFrontier(C.web.encodeFrontier());
	}
	relay.join(C, { replayFrom: 0 }); // replay everything: folded frames too, to prove idempotence
	relay.settle();
	if (log) {
		check(C.text() === A.text(), 'C (snapshot + replayed frames) matches A');
		obs(`C replayed ${C.received} frames, ${C.uselessFrames} already in the snapshot (applyDelta returned 0)`);
	}
	C.ins(0, 'C says hi. ');
	C.flush();
	B.ins(B.text().length, 'tail-B\n');
	B.flush();
	relay.settle();

	const all = [A, B, C];
	if (log) {
		check(allEqual(all), `final text equal and encode() byte-equal across A, B, C (${A.web.encode().length} bytes)`);
		obs(`final text ${show(A.text())}`);
	}
	void foldAt;
	return {
		bytes: relay.bytes,
		frames: relay.frames,
		useless: A.uselessFrames + B.uselessFrames,
		converged: allEqual(all)
	};
}

section('Sync through a relay (QUEUE 39 fix on)');
const withFix = scenario(true, true);
const noFix = scenario(false, false);
section('Re-shipping: same scenario with and without the fix');
console.log(`      with fix:    ${withFix.frames} frames, ${withFix.bytes} bytes, ${withFix.useless} useless deliveries to A/B`);
console.log(`      without fix: ${noFix.frames} frames, ${noFix.bytes} bytes, ${noFix.useless} useless deliveries to A/B`);
check(withFix.useless === 0, 'with the fix no frame reaches A or B that delivers nothing new');
check(noFix.converged, 'without the fix it still converges (only wasteful)');
obs(`the fix saves ${noFix.bytes - withFix.bytes} of ${noFix.bytes} bytes (${Math.round((100 * (noFix.bytes - withFix.bytes)) / noFix.bytes)}%)`);

// Frame size in steady state: one keystroke per frame, as the editor would send.
{
	const relay = new Relay();
	const A = new Client('A', relay);
	const B = new Client('B', relay);
	A.open(originFor('keys'));
	B.open(originFor('keys'));
	relay.join(A);
	relay.join(B);
	A.ins(0, 'The quick brown fox jumps over the lazy dog.\n'.repeat(50));
	relay.settle();
	const sizes = [];
	for (const ch of 'typing one key at a time') {
		const before = relay.bytes;
		A.ins(A.text().length - 1, ch);
		A.flush();
		relay.settle();
		sizes.push(relay.bytes - before);
	}
	const before = relay.bytes;
	A.del(10, 1);
	A.flush();
	relay.settle();
	const delBytes = relay.bytes - before;
	const before2 = relay.bytes;
	A.web.markRange(A.obj, 4, 9, 'bold', 'true');
	A.flush();
	relay.settle();
	sizes.sort((a, b) => a - b);
	const paste = 'Pasted prose, one textInsert call. '.repeat(29).slice(0, 1000);
	A.ins(0, paste);
	const pasteFrame = A.web.takeDeltas().length;
	const encAfter = A.web.encode().length;
	obs(`a 1000-char paste is one ${pasteFrame} B frame (~${Math.round(pasteFrame / 1000)} B/char; the snapshot grew to ${encAfter} B): the 256 KB frame cap is reached near ${Math.floor((256 * 1024) / (pasteFrame / 1000))} chars`);
	obs(`steady-state 0xDE frame: keystroke median ${sizes[sizes.length >> 1]} B (min ${sizes[0]}, max ${sizes[sizes.length - 1]}), delete ${delBytes} B, bold mark ${relay.bytes - before2} B; doc snapshot ${A.web.encode().length} B for ${A.text().length} chars`);
}

// Joining by snapshot without the frontier step.
{
	const relay = new Relay();
	const A = new Client('A', relay);
	A.open(originFor('join'));
	relay.join(A);
	A.ins(0, 'x'.repeat(2000) + '\n');
	A.flush();
	const snap = A.web.encode();
	const dec = WasmHashWeb.decode(snap);
	const resend = dec.takeDeltas().length;
	const dec2 = WasmHashWeb.decode(snap);
	dec2.applyFrontier(dec2.encodeFrontier());
	const resend2 = dec2.takeDeltas().length;
	const merged = new WasmHashWeb();
	merged.mergeEncoded(snap);
	const resend3 = merged.takeDeltas().length;
	obs(`join by decode(snapshot ${snap.length} B): first takeDeltas ${resend} B without applyFrontier(encodeFrontier()), ${resend2} B with it; new WasmHashWeb().mergeEncoded(snapshot) gives ${resend3} B`);
	check(resend2 === 0 && resend3 === 0, 'a snapshot joiner ships nothing back once its upstream clock is set');
}

// ---------------------------------------------------------------------------
// 2. Marks: the design's conventions.
function pair(id) {
	const relay = new Relay();
	const A = new Client('A', relay);
	const B = new Client('B', relay);
	A.open(originFor(id));
	B.open(originFor(id));
	relay.join(A);
	relay.join(B);
	return { relay, A, B };
}

section('Identical concurrent ops (content-addressed ids)');
{
	const { relay, A, B } = pair('same');
	A.ins(0, 'ab\n');
	relay.settle();
	A.ins(2, 'x');
	B.ins(2, 'x');
	A.flush();
	B.flush();
	relay.settle();
	obs(`A and B both type "x" at the same place from the same state: ${show(A.text())}`);
	check(A.text() === B.text(), 'converges');
	A.ins(3, ' same words');
	B.ins(3, ' same words');
	A.flush();
	B.flush();
	relay.settle();
	obs(`both type " same words" there: ${show(A.text())}`);
	A.ins(A.text().length, '\n');
	B.ins(B.text().length, '\n');
	A.flush();
	B.flush();
	relay.settle();
	obs(`both press Enter at the end of the doc: ${show(A.text())}`);
	check(A.text() === 'abx same words\n\n' && A.text() === B.text(), 'identical concurrent inserts (same text, same place, same state) collapse into one');
	A.ins(0, 'p');
	relay.settle();
	B.ins(1, 'p'); // B has seen A's "p" and types another after it
	relay.settle();
	check(A.text().startsWith('ppab') && A.text() === B.text(), `the same keystroke made after seeing the other one is a new element: ${show(A.text())}`);
}

section('Block kind: closed mark on the line terminator');
{
	const { relay, A, B } = pair('blocks');
	A.ins(0, 'Title\nfirst item\nsecond item\nname\u001fage\n');
	const nl = (c, n) => [...c.text()].reduce((acc, ch, i) => (ch === '\n' ? [...acc, i] : acc), [])[n];
	A.web.markRangeClosed(A.obj, nl(A, 0), nl(A, 0) + 1, 'block', 'h2');
	A.web.markRangeClosed(A.obj, nl(A, 1), nl(A, 1) + 1, 'block', 'bullet');
	A.web.markRangeClosed(A.obj, nl(A, 2), nl(A, 2) + 1, 'block', 'bullet');
	A.web.markRangeClosed(A.obj, nl(A, 3), nl(A, 3) + 1, 'block', 'table_head');
	relay.settle();
	obs(`lines ${B.lineStr()}`);
	check(B.lineStr() === A.lineStr() && B.lines()[0].block[0] === 'h2' && B.lines()[3].block[0] === 'table_head', 'block marks reach B');
	check(B.marked('block') === '\n\n\n\n', 'only the terminators carry the block mark');

	// Edits before and after the terminator.
	A.ins(0, 'Big ');
	B.ins(B.text().indexOf('\n'), ' (draft)'); // typing at the end of the h2 line
	B.ins(B.text().indexOf('\n') + 1, '- '); // typing at the start of the next line
	relay.settle();
	obs(`after typing at line end/start: ${A.lineStr()}`);
	check(A.lines()[0].line === 'Big Title (draft)' && A.lines()[0].block[0] === 'h2', 'h2 survives text inserted before it and at the line end');
	check(A.marked('block') === '\n\n\n\n', 'text typed at a line end or the next line start never takes the block mark');

	// Enter in the middle of an h2 line: insert '\n' mid-line.
	const mid = A.text().indexOf('Title');
	A.ins(mid, '\n');
	relay.settle();
	obs(`Enter mid-line (naive insert '\\n'): ${A.lineStr()}  -> the OLD terminator stays with the tail`);

	// Enter at the end of a line: '\n' before vs after the existing terminator.
	{
		const { relay: r2, A: X } = pair('enter');
		X.ins(0, 'Head\nbody\n');
		X.web.markRangeClosed(X.obj, 4, 5, 'block', 'h2');
		X.ins(4, '\n'); // before the terminator
		X.ins(5, 'typed');
		obs(`Enter at line end, '\\n' inserted BEFORE the terminator, then typing: ${X.lineStr()}`);
		const { A: Y } = pair('enter2');
		Y.ins(0, 'Head\nbody\n');
		Y.web.markRangeClosed(Y.obj, 4, 5, 'block', 'h2');
		Y.ins(5, '\n'); // after the terminator
		Y.ins(5, 'typed');
		obs(`Enter at line end, '\\n' inserted AFTER the terminator, then typing: ${Y.lineStr()}`);
		void r2;
	}

	// Deleting a terminator (Backspace at the start of the next line).
	{
		const { relay: r, A: X, B: Y } = pair('ghost');
		X.ins(0, 'Head\nbody\nmore\n');
		X.web.markRangeClosed(X.obj, 4, 5, 'block', 'h2');
		X.web.markRangeClosed(X.obj, 9, 10, 'block', 'bullet');
		r.settle();
		X.del(4, 1); // join "Head" and "body"
		r.settle();
		obs(`h2 terminator deleted: ${Y.lineStr()}  spans ${Y.spanStr()}`);
		check(!Y.spans().some((s) => s.marks.some((m) => m.values.includes('h2'))), 'no live element carries h2 once its terminator is deleted');
		X.ins(4, '\n'); // Enter again at the same place
		r.settle();
		obs(`Enter again where the deleted terminator was: ${Y.lineStr()}  (ghost anchor ${Y.lines()[0].block.includes('h2') ? 'REVIVES h2' : 'does not revive h2'})`);
		X.ins(4, 'X');
		r.settle();
		obs(`then typing at that line end: ${Y.lineStr()}  marked(block)=${show(Y.marked('block'))}`);
	}

	// Concurrent: A deletes the terminator while B types at that line's end.
	{
		const { relay: r, A: X, B: Y } = pair('ghost2');
		X.ins(0, 'Head\nbody\n');
		X.web.markRangeClosed(X.obj, 4, 5, 'block', 'h2');
		r.settle();
		X.del(4, 1);
		Y.ins(4, ' more');
		Y.ins(9, '\n'); // B also presses Enter at the end of "Head more" (before the old terminator)
		X.flush();
		Y.flush();
		r.settle();
		obs(`concurrent: A deletes the h2 terminator, B types " more" + Enter at that line end: ${X.lineStr()}  marked(block)=${show(X.marked('block'))}`);
		check(X.text() === Y.text() && X.spanStr() === Y.spanStr(), 'converges (text and spans)');
	}
}

section('Inline marks');
{
	const { relay, A, B } = pair('inline');
	A.ins(0, 'hello world\nsecond\n');
	A.web.markRange(A.obj, 6, 11, 'bold', 'true'); // "world", expanding end = Before('\n')
	relay.settle();
	B.ins(11, '!!'); // typing at the end of the line, right after the bold
	relay.settle();
	obs(`expanding bold at line end, typing after it: ${A.spanStr()}`);
	check(A.marked('bold') === 'world!!', 'typing at the end of a bold run at line end continues bold');
	A.ins(6, '>'); // typing right before the bold start
	relay.settle();
	obs(`typing just before the bold start: bold=${show(A.marked('bold'))}`);
	// Enter at the end of the bold line.
	const end = A.text().indexOf('\n');
	A.ins(end, '\n');
	A.ins(end + 1, 'next');
	relay.settle();
	obs(`Enter at the end of a bold line ('\\n' before the old terminator) then typing: bold=${show(A.marked('bold'))}  ${A.spanStr()}`);

	// Partial unmark.
	const { relay: r2, A: X, B: Y } = pair('unmark');
	X.ins(0, 'abcdefghij\n');
	X.web.markRange(X.obj, 0, 10, 'bold', 'true');
	r2.settle();
	Y.web.unmarkRange(Y.obj, 3, 6, 'bold');
	r2.settle();
	obs(`bold 0..10 then unmark 3..6: ${X.spanStr()}`);
	check(X.marked('bold') === 'abcghij' && X.spanStr() === Y.spanStr(), 'partial unmark leaves bold outside the range');

	// Link (closed) with a URL longer than the identity form: an artifact.
	const { relay: r3, A: L, B: M } = pair('link');
	L.ins(0, 'see docs here\n');
	L.web.markRangeClosed(L.obj, 4, 8, 'link', 'https://example.com/a/long/path');
	L.web.markRangeClosed(L.obj, 9, 13, 'comment:0123456789ab', 'on');
	const arts = L.web.takeNewArtifacts();
	obs(`artifacts minted by those marks: ${arts.length} (block/bold/h2-sized strings are identity-form ids: <= 14 UTF-8 bytes)`);
	// Deliver the delta only, no 0xAF push.
	M.receive(L.web.takeDeltas());
	obs(`B with the 0xDE delta only: ${M.spanStr()}`);
	const ok1 = M.spans().some((s) => s.marks.some((m) => m.values.includes('https://example.com/a/long/path')));
	check(!ok1, 'without the 0xAF push the URL is NOT readable on B (an unresolved id)');
	const encEqualBefore = sameBytes(L.web.encode(), M.web.encode());
	for (const id of arts) M.receive(L.web.artifactFrame(id));
	obs(`B after the 0xAF artifact frames: ${M.spanStr()}`);
	check(M.spanStr() === L.spanStr(), 'the 0xAF push makes link URLs and comment:<id> kinds readable');
	obs(`encode() equal before the artifact push: ${encEqualBefore}; after: ${sameBytes(L.web.encode(), M.web.encode())}`);
	void r3;
}

section('Conflicting block values');
{
	const { relay, A, B } = pair('conflict');
	A.ins(0, 'line\n');
	relay.settle();
	A.web.markRangeClosed(A.obj, 4, 5, 'block', 'h2');
	B.web.markRangeClosed(B.obj, 4, 5, 'block', 'bullet');
	A.flush();
	B.flush();
	relay.settle();
	obs(`concurrent h2 vs bullet on one terminator: ${A.spanStr()}`);
	check(A.spanStr() === B.spanStr() && A.lines()[0].block.length === 2, 'both values are live (MVR), in the same order on both');
	A.web.markRangeClosed(A.obj, 4, 5, 'block', 'h3');
	relay.settle();
	obs(`a later mark that saw both: ${B.spanStr()}`);
	check(B.lines()[0].block.join() === 'h3', 'a later mark overwrites both conflicting values');

	A.web.markRangeClosed(A.obj, 4, 5, 'block', 'task');
	relay.settle();
	A.web.markRangeClosed(A.obj, 4, 5, 'block', 'task_done');
	B.web.markRangeClosed(B.obj, 4, 5, 'block', 'task_done');
	A.flush();
	B.flush();
	relay.settle();
	obs(`both tick the same task concurrently: ${B.spanStr()}`);
	A.web.markRangeClosed(A.obj, 4, 5, 'block', 'task_done');
	B.web.markRangeClosed(B.obj, 4, 5, 'block', 'task');
	A.flush();
	B.flush();
	relay.settle();
	obs(`concurrent tick vs untick: ${B.spanStr()}`);
}

section('Edge documents');
{
	const { relay, A } = pair('empty');
	obs(`markedSpans on an empty doc: ${A.web.markedSpans(A.obj)}`);
	A.ins(0, '\n');
	obs(`markedSpans on "\\n": ${A.web.markedSpans(A.obj)}`);
	A.web.markRangeClosed(A.obj, 0, 1, 'block', 'h1');
	obs(`markedSpans on "\\n" with block=h1: ${A.web.markedSpans(A.obj)}`);
	A.del(0, 1);
	obs(`after deleting that only "\\n": ${A.web.markedSpans(A.obj)} text=${show(A.text())}`);
	let err = '';
	try {
		A.web.markRangeClosed(A.obj, 0, 0, 'block', 'h1');
	} catch (e) {
		err = String(e);
	}
	obs(`markRangeClosed over an empty range throws: ${show(err)}`);
	void relay;
}

section('Index units (U+001F and astral characters)');
{
	const { relay, A, B } = pair('units');
	A.ins(0, 'a\u001fb\n');
	relay.settle();
	check(B.text() === 'a\u001fb\n' && B.web.textLen(B.obj) === 4, 'U+001F round-trips through textInsert/text/textLen');
	const sepId = B.web.seqIdAt(B.obj, 1);
	check(B.web.seqPositionOf(B.obj, sepId) === 1, 'seqIdAt/seqPositionOf on the U+001F element');
	A.ins(2, 'X'); // after the separator
	relay.settle();
	check(B.text() === 'a\u001fXb\n', `textInsert at index 2 lands after U+001F: ${show(B.text())}`);

	const { A: U } = pair('units2');
	U.ins(0, 'x😀y\n');
	const len = U.web.textLen(U.obj);
	U.ins(3, 'Z');
	const t = U.text();
	obs(`"x😀y\\n": JS length ${'x😀y\n'.length} (UTF-16), textLen ${len}; textInsert(3,"Z") -> ${show(t)}`);
	check(len === 4 && t === 'x😀yZ\n', 'textInsert/textLen index Unicode scalar values (code points), not UTF-16 units');
	const ids = [0, 1, 2, 3].map((i) => U.web.seqIdAt(U.obj, i));
	check(new Set(ids).size === 4 && U.web.seqPositionOf(U.obj, ids[2]) === 2, 'seqIdAt(1) is the whole emoji; seqIdAt(2) is "y"');
	const { A: F } = pair('units3');
	F.ins(0, '👨‍👩‍👧\n');
	obs(`family emoji (ZWJ sequence): JS length ${'👨‍👩‍👧'.length}, textLen ${F.web.textLen(F.obj) - 1} (+1 for "\\n"): one element per code point, graphemes span several`);
	U.web.markRange(U.obj, 1, 2, 'bold', 'true');
	obs(`markRange(1,2) on the emoji: ${U.spanStr()}`);
	let err = '';
	try {
		U.web.textRemove(U.obj, 1, 1);
	} catch (e) {
		err = String(e);
	}
	obs(`textRemove(1,1) removes the emoji whole: ${show(U.text())} ${err}`);
}

console.log(failures ? `\n${failures} FAILED` : '\nall invariants passed');
process.exit(failures ? 1 : 0);
