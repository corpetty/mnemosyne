import { describe, expect, it } from 'vitest';
import { commentRange, commentSelection, commentsAt } from './comments.js';
import { addComment, removeComment } from './commands.js';
import { CELL_SEP } from './model.js';
import { single } from './test-harness.js';

function docWith(text: string) {
  const A = single();
  A.web.textInsert(A.doc.obj, 0, text);
  return A;
}

describe('commentSelection', () => {
  it('gives the text and the ids of its first and last character', () => {
    const A = docWith('Our launch plan is set\nSecond line\n');
    const s = commentSelection(A.doc, 4, 15);
    expect(s).toMatchObject({ from: 4, to: 15, text: 'launch plan' });
    expect(s?.first).toBe(A.web.seqIdAt(A.doc.obj, 4));
    expect(s?.last).toBe(A.web.seqIdAt(A.doc.obj, 14));
    expect(s?.first).toMatch(/^[0-9a-f]{64}$/);
  });
  it('takes either order and leaves line breaks and blanks at the edges out', () => {
    const A = docWith('Our launch plan is set\nSecond line\n');
    // A triple click: the whole first line and its break.
    expect(commentSelection(A.doc, 23, 0)).toMatchObject({ from: 0, to: 22, text: 'Our launch plan is set' });
    expect(commentSelection(A.doc, 3, 16)).toMatchObject({ text: 'launch plan' });
  });
  it('is null without text', () => {
    const A = docWith('a  \n\nb\n');
    expect(commentSelection(A.doc, 1, 3)).toBeNull();
    expect(commentSelection(A.doc, 5, 5)).toBeNull();
    expect(commentSelection(A.doc, 0, 0)).toBeNull();
  });
  it('counts code points and shows table cells as tabs', () => {
    const A = docWith(`😀 a${CELL_SEP}b\n`);
    const s = commentSelection(A.doc, 0, 5);
    expect(s).toMatchObject({ from: 0, to: 5, text: '😀 a\tb' });
  });
});

describe('commentRange and commentsAt', () => {
  it('find the mark and follow the text', () => {
    const A = docWith('Our launch plan is set\n');
    expect(commentRange(A.doc, 'c1')).toBeNull();
    addComment(A.doc, 4, 15, 'c1');
    addComment(A.doc, 9, 20, 'c2');
    expect(commentRange(A.doc, 'c1')).toEqual({ start: 4, end: 15 });
    expect(commentRange(A.doc, 'c2')).toEqual({ start: 9, end: 20 });
    expect(commentsAt(A.doc, 10)).toEqual(['c1', 'c2']);
    expect(commentsAt(A.doc, 5)).toEqual(['c1']);
    expect(commentsAt(A.doc, 0)).toEqual([]);
    A.web.textInsert(A.doc.obj, 0, 'Hey! ');
    expect(commentRange(A.doc, 'c1')).toEqual({ start: 9, end: 20 });
    removeComment(A.doc, 'c1');
    expect(commentRange(A.doc, 'c1')).toBeNull();
  });
  it('is null once every character is deleted', () => {
    const A = docWith('Our launch plan is set\n');
    addComment(A.doc, 4, 15, 'c1');
    A.web.textRemove(A.doc.obj, 3, 13);
    expect(commentRange(A.doc, 'c1')).toBeNull();
  });
});
