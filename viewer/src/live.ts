// Keeping an open publication current (docs/plans/2026-11-05-live-publications.md, "The viewer").
// No DOM: Viewer.svelte gives it the hosted handle and the version shown, and gets back newer
// versions to swap in, the end of the publication, and whether the page is followed live.
//
// - Poll: every POLL_MS, `GET /pub/<id>` with `If-None-Match` (the shown ETag). A 304 changes
//   nothing; a manifest with a higher version is swapped in; 404 and 410 end the page (`onGone`:
//   not found, no longer published, or expired when the shown manifest's expiry has passed). A
//   network failure or another status is skipped until the next poll.
// - Room: with a manifest `live` field and an `X-Pub-Room` header, the room is joined with the
//   token derived from the link key (`Hosted.liveKeys`). Every frame (the snapshot too) is opened
//   in the pub-live space and verified against `live.sign`; anything that does not verify, or
//   whose body is malformed, is dropped silently (`dropped` counts them). VERSION refetches with
//   `cache: 'no-cache'` and is taken only when the response's ETag is the frame's sha256 and its
//   version is higher than the shown one. META and THREADS: the newest `at` wins (THREADS is kept
//   for the comments of M2 and not shown).
// - Room closes (RoomClient handles reconnects and backoff): 4410 (the room is gone) re-reads
//   `GET /pub/<id>` for a new room id, and only a 410 from that read ends the page; 4401 (token
//   refused) leaves that room and keeps polling; 4429 (room full or write budget) and 4403 (ended)
//   are the client's to retry or stop: polling goes on. A room id that was gone or refused is
//   never joined again; a later answer naming another room is.
// - Every answer's `X-Pub-Room` (a 304's too) is where the room is: a new id moves there, none
//   leaves the room.

import { RoomClient, ROOM_CLOSE, roomUrl, type RoomSocket, type RoomSocketCtor, type RoomState } from '$lib/rooms/client';
import type { SpaceKeys } from '$lib/sealed/envelope';
import { openSigned, parseLiveKeys, PubKind } from '$lib/sealed/signed';
import type { Manifest } from './format';
import { expiredOn, type Fetched, type Hosted } from './load';

export const POLL_MS = 60_000;

/** What the page shows: the manifest and where it came from. */
export interface Shown {
	manifest: Manifest;
	etag: string;
	room: string | null;
}

/** The end of a publication while it was open. */
export type Gone = { kind: 'not_found' } | { kind: 'unpublished' } | { kind: 'expired'; on: string };

export interface Meta {
	comments: boolean;
	follow: boolean;
	at: string;
}

/** The owner's THREADS as it came (M2 renders it). */
export interface Threads {
	threads: unknown[];
	at: string;
}

export interface FollowHandlers {
	/** A newer version to show in place of the current one. */
	onManifest(next: Shown): void;
	/** The publication ended; following has stopped. */
	onGone(state: Gone): void;
	/** `following` (or the room state behind it) may have changed. */
	onChange?(): void;
}

export interface FollowOptions {
	webSocket?: RoomSocketCtor;
	/** The page's origin, for the room's WebSocket URL. */
	location?: { protocol: string; host: string };
	now?: () => Date;
	pollMs?: number;
}

/** The status line under the meeting. */
export const STATUS = {
	live: 'Kept up to date as the meeting changes',
	polling: 'Checks for updates every minute',
	file: 'This file does not update'
} as const;

export function statusLine(following: boolean): string {
	return following ? STATUS.live : STATUS.polling;
}

const SHA = /^[0-9a-f]{64}$/;

function atOf(v: unknown): number | null {
	if (typeof v !== 'string') return null;
	const t = Date.parse(v);
	return Number.isNaN(t) ? null : t;
}

/** A socket constructor that tells `seen` every close code before the client hears it. */
function watchCloses(Base: RoomSocketCtor, seen: (code: number) => void): RoomSocketCtor {
	return class implements RoomSocket {
		onopen: RoomSocket['onopen'] = null;
		onmessage: RoomSocket['onmessage'] = null;
		onclose: RoomSocket['onclose'] = null;
		onerror: RoomSocket['onerror'] = null;
		private readonly ws: RoomSocket;
		constructor(url: string) {
			const ws = new Base(url);
			this.ws = ws;
			ws.onopen = (ev) => this.onopen?.(ev);
			ws.onmessage = (ev) => this.onmessage?.(ev);
			ws.onerror = (ev) => this.onerror?.(ev);
			ws.onclose = (ev) => {
				seen(ev.code);
				this.onclose?.(ev);
			};
		}
		get readyState(): number {
			return this.ws.readyState;
		}
		get binaryType(): string {
			return this.ws.binaryType;
		}
		set binaryType(v: string) {
			this.ws.binaryType = v;
		}
		send(data: string | Uint8Array): void {
			this.ws.send(data);
		}
		close(code?: number, reason?: string): void {
			this.ws.close(code, reason);
		}
	};
}

export class Follower {
	meta: Meta | null = null;
	threads: Threads | null = null;
	/** Frames that did not open, did not verify or were malformed (a debug count, never shown). */
	dropped = 0;
	/** The room client's state, 'none' without a room. */
	roomState: RoomState | 'none' = 'none';

	private shown: Shown;
	private client: RoomClient | null = null;
	private clientRoom: string | null = null;
	/** Where each room's log was read up to, for a rejoin. */
	private sinces = new Map<string, number>();
	/** Rooms that closed 4410 or 4401: never joined again. */
	private deadRooms = new Set<string>();
	private lastClose: number | null = null;
	private keys: SpaceKeys | null = null;
	private stopped = true;
	private timer: ReturnType<typeof setTimeout> | null = null;
	private chain: Promise<unknown> = Promise.resolve();
	/** Bumped by every syncLive: a key derivation that finishes late is ignored. */
	private liveGen = 0;
	private metaAt: number | null = null;
	private threadsAt: number | null = null;

	private readonly Socket: RoomSocketCtor | undefined;
	private readonly loc: { protocol: string; host: string } | undefined;
	private readonly now: () => Date;
	private readonly pollMs: number;

	constructor(
		private readonly hosted: Hosted,
		shown: Shown,
		private readonly h: FollowHandlers,
		opts: FollowOptions = {}
	) {
		this.shown = shown;
		this.Socket = opts.webSocket;
		this.loc = opts.location;
		this.now = opts.now ?? (() => new Date());
		this.pollMs = opts.pollMs ?? POLL_MS;
	}

	/** Joined to the room and the owner says it keeps the link up to date. */
	get following(): boolean {
		return this.roomState === 'live' && this.meta?.follow === true;
	}

	/** The room joined or being joined (null: poll only). */
	get room(): string | null {
		return this.clientRoom;
	}

	start(): void {
		if (!this.stopped) return;
		this.stopped = false;
		this.schedule();
		void this.syncLive();
	}

	stop(): void {
		this.stopped = true;
		if (this.timer) clearTimeout(this.timer);
		this.timer = null;
		this.leave();
	}

	/** Ask for a newer version now (the poll, also run after a rejoin). */
	poll(): Promise<void> {
		return this.serial(async () => {
			const r = await this.hosted.fetch({ etag: this.shown.etag || null, cache: 'no-store' });
			this.take(r);
		});
	}

	// ---- internals -------------------------------------------------------------------------

	/** A poll out of turn, its failure ignored like a poll's. */
	private check(): void {
		this.poll().catch(() => {});
	}

	private serial(fn: () => Promise<void>): Promise<void> {
		const run = this.chain.then(() => (this.stopped ? undefined : fn()));
		this.chain = run.catch(() => {});
		return run;
	}

	private schedule(): void {
		if (this.stopped) return;
		this.timer = setTimeout(() => {
			this.timer = null;
			void this.poll()
				.catch(() => {})
				.finally(() => this.schedule());
		}, this.pollMs);
	}

	/** An answer of `GET /pub/<id>`. With `expect`, a manifest is taken only when its ETag is that sha. */
	private take(r: Fetched, expect?: string): void {
		if (this.stopped) return;
		switch (r.kind) {
			case 'not_found':
				this.end({ kind: 'not_found' });
				return;
			case 'unpublished': {
				// The host tombstones an expired publication too: say which it was.
				const on = expiredOn(this.shown.manifest, this.now());
				this.end(on ? { kind: 'expired', on } : { kind: 'unpublished' });
				return;
			}
			case 'error':
				break;
			case 'not_modified':
				this.moveRoom(r.room);
				break;
			case 'manifest':
				if (r.manifest.version > this.shown.manifest.version && (expect === undefined || r.etag === expect)) {
					this.shown = { manifest: r.manifest, etag: r.etag, room: r.room };
					this.h.onManifest(this.shown);
					if (this.stopped) return;
					void this.syncLive();
				} else this.moveRoom(r.room);
				break;
		}
		const on = expiredOn(this.shown.manifest, this.now());
		if (on) this.end({ kind: 'expired', on });
	}

	private end(state: Gone): void {
		this.stop();
		this.h.onGone(state);
	}

	private moveRoom(room: string | null): void {
		if (room === this.shown.room) return;
		this.shown = { ...this.shown, room };
		void this.syncLive();
	}

	/** Join the room the shown version names, if it has live keys and the room may be joined. */
	private async syncLive(): Promise<void> {
		const gen = ++this.liveGen;
		const live = parseLiveKeys(this.shown.manifest.live);
		const room = this.shown.room;
		if (this.stopped || !live || !room || this.deadRooms.has(room)) {
			this.leave();
			return;
		}
		if (this.clientRoom === room && this.client) return;
		let keys: SpaceKeys;
		try {
			keys = this.keys ?? (await this.hosted.liveKeys());
		} catch {
			return; // no WebCrypto: polling still works
		}
		if (gen !== this.liveGen || this.stopped) return;
		this.keys = keys;
		this.leave();
		this.join(room, keys);
	}

	private join(room: string, keys: SpaceKeys): void {
		let caughtUp = 0;
		const Base = this.Socket ?? (WebSocket as unknown as RoomSocketCtor);
		const client: RoomClient = new RoomClient(
			{
				onFrame: (_seq, env) => void this.onEnvelope(room, env),
				onSnapshot: (_seq, env) => void this.onEnvelope(room, env),
				onState: (s) => this.onRoomState(client, room, s),
				// After a rejoin a VERSION may have been pruned from the log by a snapshot: ask.
				onCaughtUp: () => {
					if (caughtUp++ > 0) this.check();
				}
			},
			{ webSocket: watchCloses(Base, (code) => (this.lastClose = code)) }
		);
		this.client = client;
		this.clientRoom = room;
		this.roomState = client.state;
		this.h.onChange?.();
		client.connect(roomUrl(room, this.loc), keys.roomToken, this.sinces.get(room) ?? 0);
	}

	private leave(): void {
		const client = this.client;
		if (!client) return;
		if (this.clientRoom) this.sinces.set(this.clientRoom, client.since);
		this.client = null;
		this.clientRoom = null;
		client.close();
		this.roomState = 'none';
		this.h.onChange?.();
	}

	private onRoomState(client: RoomClient, room: string, s: RoomState): void {
		if (client !== this.client) return;
		this.roomState = s;
		if (s === 'gone') {
			this.sinces.set(room, client.since);
			this.deadRooms.add(room);
			this.client = null;
			this.clientRoom = null;
			this.roomState = 'none';
			// The room going away is not the publication going away: ask where the room is now.
			// 4401 (not let in) keeps polling only.
			if (this.lastClose === ROOM_CLOSE.gone) this.check();
		}
		this.h.onChange?.();
	}

	private async onEnvelope(room: string, env: Uint8Array): Promise<void> {
		const keys = this.keys;
		const live = parseLiveKeys(this.shown.manifest.live);
		if (!keys || !live || room !== this.clientRoom) return;
		let frame: { kind: number; obj: Record<string, unknown> };
		try {
			frame = await openSigned(keys, room, env, live.sign);
		} catch {
			this.dropped++;
			return;
		}
		if (this.stopped || room !== this.clientRoom) return;
		const ok =
			frame.kind === PubKind.VERSION
				? this.onVersion(frame.obj)
				: frame.kind === PubKind.META
					? this.onMeta(frame.obj)
					: this.onThreads(frame.obj);
		if (!ok) this.dropped++;
	}

	private onVersion(o: Record<string, unknown>): boolean {
		const version = o.version;
		const sha = o.sha256;
		if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) return false;
		if (typeof sha !== 'string' || !SHA.test(sha)) return false;
		if (version <= this.shown.manifest.version) return true;
		this.serial(async () => {
			if (version <= this.shown.manifest.version) return;
			const r = await this.hosted.fetch({ cache: 'no-cache' });
			this.take(r, sha);
		}).catch(() => {});
		return true;
	}

	private onMeta(o: Record<string, unknown>): boolean {
		const at = atOf(o.at);
		if (at === null || typeof o.follow !== 'boolean' || typeof o.comments !== 'boolean') return false;
		if (this.metaAt !== null && at <= this.metaAt) return true;
		this.metaAt = at;
		this.meta = { follow: o.follow, comments: o.comments, at: o.at as string };
		this.h.onChange?.();
		return true;
	}

	private onThreads(o: Record<string, unknown>): boolean {
		const at = atOf(o.at);
		if (at === null || !Array.isArray(o.threads)) return false;
		if (this.threadsAt !== null && at <= this.threadsAt) return true;
		this.threadsAt = at;
		this.threads = { threads: o.threads, at: o.at as string };
		this.h.onChange?.();
		return true;
	}
}
