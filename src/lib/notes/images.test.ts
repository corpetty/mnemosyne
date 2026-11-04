import { describe, expect, it } from 'vitest';
import { IMAGE_URL, imageUrl, imageValue, isImageValue, parseImageUrl, parseImageValue } from './images.js';

const SHA = '3f0c5aa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5ca9';
const A = `${SHA}.webp@1600x900`;

describe('image values', () => {
  it('builds and parses a value', () => {
    expect(imageValue({ sha: SHA, ext: 'webp', w: 1600, h: 900 })).toBe(A);
    expect(parseImageValue(A)).toEqual({ value: A, sha: SHA, ext: 'webp', w: 1600, h: 900 });
    expect(parseImageValue(`${SHA}.jpeg@1x99999`)).toMatchObject({ ext: 'jpeg', w: 1, h: 99999 });
  });

  it('refuses what notes-core refuses (tests/images.rs)', () => {
    for (const ok of [`${SHA}.webp@1x1`, `${SHA}.jpeg@99999x99999`]) expect(isImageValue(ok), ok).toBe(true);
    for (const bad of [
      '',
      `${SHA}.png@1x1`,
      `${SHA}.webp@0x1`,
      `${SHA}.webp@01x1`,
      `${SHA}.webp@100000x1`,
      `${SHA}.webp@1x`,
      `${SHA}.webp@1x1x1`,
      `${SHA}.webp@1X1`,
      `${SHA}.webp`,
      `${SHA}webp@1x1`,
      `${SHA.toUpperCase()}.webp@1x1`,
      `${SHA.slice(1)}.webp@1x1`,
      `${SHA}0.webp@1x1`,
      `${SHA}.webp@1x1 `,
      `${SHA}.webp@1x1\n`,
      `${SHA}.webp.webp@1x1`
    ]) {
      expect(isImageValue(bad), bad).toBe(false);
      expect(parseImageValue(bad), bad).toBeNull();
    }
    expect(isImageValue(null)).toBe(false);
    expect(isImageValue(42)).toBe(false);
  });

  it('throws on parts that make no valid value', () => {
    expect(() => imageValue({ sha: SHA, ext: 'webp', w: 0, h: 9 })).toThrow(RangeError);
    expect(() => imageValue({ sha: SHA, ext: 'webp', w: 100_000, h: 9 })).toThrow(RangeError);
    expect(() => imageValue({ sha: 'abc', ext: 'jpeg', w: 1, h: 1 })).toThrow(RangeError);
    expect(() => imageValue({ sha: SHA, ext: 'png' as never, w: 1, h: 1 })).toThrow(RangeError);
  });

  it('maps to and from the markdown destination', () => {
    expect(imageUrl(A)).toBe(`mnemosyne:image/${A}`);
    expect(IMAGE_URL).toBe('mnemosyne:image/');
    expect(parseImageUrl(imageUrl(A))?.sha).toBe(SHA);
    expect(parseImageUrl(A)).toBeNull();
    expect(parseImageUrl(`mnemosyne:image/${SHA}.png@1x1`)).toBeNull();
    expect(parseImageUrl(`https://example.com/${A}`)).toBeNull();
  });
});
