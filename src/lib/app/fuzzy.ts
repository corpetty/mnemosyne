/**
 * Fuzzy matching for the command palette: every word of the query must appear in the text as a
 * subsequence. Matches at word starts and consecutive characters score higher, so "st ai" finds
 * "Settings · AI" and "rel" ranks "Release planning" above "Parallel".
 */

function wordScore(word: string, text: string): number | null {
  const direct = text.indexOf(word);
  if (direct >= 0) {
    const atStart = direct === 0 || /[\s·\-_/(]/.test(text[direct - 1]);
    return 20 * word.length + (atStart ? 30 : 0) - Math.min(direct, 20) * 0.1;
  }
  let score = 0;
  let from = 0;
  let previous = -2;
  for (const ch of word) {
    const i = text.indexOf(ch, from);
    if (i < 0) return null;
    const atStart = i === 0 || /[\s·\-_/(]/.test(text[i - 1]);
    score += atStart ? 8 : i === previous + 1 ? 5 : 1;
    previous = i;
    from = i + 1;
  }
  return score;
}

/** Higher is better; null when the text does not match. An empty query matches everything. */
export function fuzzyScore(query: string, text: string): number | null {
  const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const haystack = text.toLowerCase();
  let total = 0;
  for (const word of words) {
    const s = wordScore(word, haystack);
    if (s === null) return null;
    total += s;
  }
  return total;
}
