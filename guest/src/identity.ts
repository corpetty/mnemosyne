// Who this guest is, kept in the page's localStorage: a display name and a random guest id
// (`gid`, how the bridge knows a guest's own comments; as weak as the name). Private browsing or
// blocked storage throws on access: everything falls back to memory for this visit.

export const NAME_KEY = 'mnemosyne.guest.name';
export const GID_KEY = 'mnemosyne.guest.gid';
export const NAME_MAX = 80;
export const GID_LEN = 16;

const GID_ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-';
const GID_RE = /^[0-9A-Za-z_-]{1,64}$/;

/** The storage calls we use (localStorage, or a stand-in in tests). */
export interface KeyValue {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
}

/** 16 characters from [0-9A-Za-z_-]; the alphabet has 64 letters, so `byte & 63` is uniform. */
export function newGid(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
	return Array.from(random(GID_LEN), (b) => GID_ALPHABET[b & 63]).join('');
}

/** A name as we keep and send it: trimmed, inner whitespace collapsed, at most 80 characters. */
export function cleanName(text: string): string {
	return Array.from(text.replace(/\s+/g, ' ').trim()).slice(0, NAME_MAX).join('').trim();
}

export class Identity {
	private memory = new Map<string, string>();
	private readonly store: () => KeyValue | null;

	constructor(store: () => KeyValue | null = () => globalThis.localStorage ?? null) {
		this.store = store;
	}

	private get(key: string): string | null {
		try {
			const v = this.store()?.getItem(key);
			if (typeof v === 'string') return v;
		} catch {
			/* private mode: memory below */
		}
		return this.memory.get(key) ?? null;
	}

	private set(key: string, value: string): void {
		this.memory.set(key, value);
		try {
			this.store()?.setItem(key, value);
		} catch {
			/* private mode: kept in memory for this visit */
		}
	}

	/** The remembered name, or "" on a first visit. */
	get name(): string {
		return cleanName(this.get(NAME_KEY) ?? '');
	}

	set name(value: string) {
		this.set(NAME_KEY, cleanName(value));
	}

	/** This page's guest id, created on first use and kept from then on. */
	get gid(): string {
		const v = this.get(GID_KEY);
		if (v && GID_RE.test(v)) return v;
		const gid = newGid();
		this.set(GID_KEY, gid);
		return gid;
	}
}
