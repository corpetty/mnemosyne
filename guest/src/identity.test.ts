import { describe, expect, it } from 'vitest';
import { GID_KEY, Identity, NAME_KEY, cleanName, newGid, type KeyValue } from './identity';

class MapStore implements KeyValue {
	m = new Map<string, string>();
	getItem(k: string) {
		return this.m.get(k) ?? null;
	}
	setItem(k: string, v: string) {
		this.m.set(k, v);
	}
}

const throwing: KeyValue = {
	getItem() {
		throw new DOMException('denied', 'SecurityError');
	},
	setItem() {
		throw new DOMException('denied', 'SecurityError');
	}
};

describe('newGid', () => {
	it('is 16 characters from [0-9A-Za-z_-]', () => {
		for (let i = 0; i < 50; i++) expect(newGid()).toMatch(/^[0-9A-Za-z_-]{16}$/);
	});
	it('maps bytes onto the 64-letter alphabet', () => {
		expect(newGid((n) => new Uint8Array(n).fill(255))).toBe('-'.repeat(16));
		expect(newGid((n) => new Uint8Array(n).fill(64))).toBe('0'.repeat(16));
	});
});

describe('cleanName', () => {
	it('trims, collapses whitespace and caps at 80 code points', () => {
		expect(cleanName('  Ada   Lovelace \n')).toBe('Ada Lovelace');
		expect(cleanName('x'.repeat(100))).toHaveLength(80);
		expect(Array.from(cleanName('😀'.repeat(90)))).toHaveLength(80);
	});
});

describe('Identity', () => {
	it('creates the gid once and keeps it', () => {
		const s = new MapStore();
		const a = new Identity(() => s);
		const gid = a.gid;
		expect(a.gid).toBe(gid);
		expect(s.m.get(GID_KEY)).toBe(gid);
		expect(new Identity(() => s).gid).toBe(gid);
	});
	it('replaces a stored gid that is not valid', () => {
		const s = new MapStore();
		s.setItem(GID_KEY, 'not valid!');
		expect(new Identity(() => s).gid).toMatch(/^[0-9A-Za-z_-]{16}$/);
	});
	it('remembers the name', () => {
		const s = new MapStore();
		const a = new Identity(() => s);
		expect(a.name).toBe('');
		a.name = '  Bob ';
		expect(s.m.get(NAME_KEY)).toBe('Bob');
		expect(new Identity(() => s).name).toBe('Bob');
	});
	it('falls back to memory when storage throws', () => {
		const a = new Identity(() => throwing);
		const gid = a.gid;
		expect(a.gid).toBe(gid);
		a.name = 'Eve';
		expect(a.name).toBe('Eve');
	});
	it('works without storage at all', () => {
		const a = new Identity(() => null);
		a.name = 'Kim';
		expect(a.name).toBe('Kim');
		expect(a.gid).toBe(a.gid);
	});
});
