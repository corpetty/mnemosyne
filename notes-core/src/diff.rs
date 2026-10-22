//! Text diffs that merge well: patience over lines, then over words (and punctuation) inside
//! every replaced block of lines. A diff by characters across a note, or one that replaces a
//! whole middle section, garbles merges.
//!
//! Moved here from link/src/notes.rs (the Obsidian note merge), which still uses [`edits`]
//! unchanged. The notes document uses [`edits_by_line`], which also makes every `\n` its own
//! word: a line the new text keeps keeps its terminator, so its line kind (a mark on that
//! terminator) stays on it.

use hashseq::HashSeq;
use similar::{Algorithm, ChangeTag, DiffOp, TextDiff};

/// One step of an edit script over characters (Unicode scalar values).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Edit {
    Equal(usize),
    Delete(usize),
    Insert(String),
}

fn push(out: &mut Vec<Edit>, edit: Edit) {
    match (out.last_mut(), edit) {
        (Some(Edit::Equal(a)), Edit::Equal(b)) => *a += b,
        (Some(Edit::Delete(a)), Edit::Delete(b)) => *a += b,
        (Some(Edit::Insert(a)), Edit::Insert(b)) => a.push_str(&b),
        (_, edit) => out.push(edit),
    }
}

/// The edit script from `old` to `new`: patience over lines, then over words inside each
/// replaced block of lines.
pub fn edits(old: &str, new: &str) -> Vec<Edit> {
    edits_with(old, new, false)
}

/// [`edits`] with every `\n` a word of its own in the word-level pass.
pub fn edits_by_line(old: &str, new: &str) -> Vec<Edit> {
    edits_with(old, new, true)
}

fn edits_with(old: &str, new: &str, newline_words: bool) -> Vec<Edit> {
    let diff = TextDiff::configure()
        .algorithm(Algorithm::Patience)
        .diff_lines(old, new);
    let (old_lines, new_lines) = (diff.old_slices(), diff.new_slices());
    let mut out = Vec::new();
    for op in diff.ops() {
        match *op {
            DiffOp::Equal { old_index, len, .. } => {
                let n: usize = old_lines[old_index..old_index + len]
                    .iter()
                    .map(|l| l.chars().count())
                    .sum();
                push(&mut out, Edit::Equal(n));
            }
            DiffOp::Delete {
                old_index, old_len, ..
            } => {
                let n: usize = old_lines[old_index..old_index + old_len]
                    .iter()
                    .map(|l| l.chars().count())
                    .sum();
                push(&mut out, Edit::Delete(n));
            }
            DiffOp::Insert {
                new_index, new_len, ..
            } => push(
                &mut out,
                Edit::Insert(new_lines[new_index..new_index + new_len].concat()),
            ),
            DiffOp::Replace {
                old_index,
                old_len,
                new_index,
                new_len,
            } => {
                let a = old_lines[old_index..old_index + old_len].concat();
                let b = new_lines[new_index..new_index + new_len].concat();
                let (a, b) = (tokens(&a, newline_words), tokens(&b, newline_words));
                let words = TextDiff::configure()
                    .algorithm(Algorithm::Patience)
                    .diff_slices(&a, &b);
                for change in words.iter_all_changes() {
                    let value: &str = change.value();
                    push(
                        &mut out,
                        match change.tag() {
                            ChangeTag::Equal => Edit::Equal(value.chars().count()),
                            ChangeTag::Delete => Edit::Delete(value.chars().count()),
                            ChangeTag::Insert => Edit::Insert(value.to_string()),
                        },
                    );
                }
            }
        }
    }
    out
}

/// Split text for the word-level diff: runs of letters and digits, runs of whitespace, and
/// every other character on its own. Punctuation apart from its word matters: a user who
/// appends ", pending" to a line the app reworded must not be seen as rewriting the last word
/// (`similar`'s own word split would keep "April," as one token). With `newline_words`, a
/// `\n` is always a token of its own too.
fn tokens(text: &str, newline_words: bool) -> Vec<&str> {
    #[derive(PartialEq, Clone, Copy)]
    enum Kind {
        Word,
        Space,
        Newline,
        Other,
    }
    let kind = |c: char| {
        if c.is_alphanumeric() || c == '_' {
            Kind::Word
        } else if newline_words && c == '\n' {
            Kind::Newline
        } else if c.is_whitespace() {
            Kind::Space
        } else {
            Kind::Other
        }
    };
    let mut out = Vec::new();
    let mut start = 0;
    let mut current: Option<Kind> = None;
    for (i, c) in text.char_indices() {
        let k = kind(c);
        if current.is_some_and(|cur| cur != k || k == Kind::Other || k == Kind::Newline) {
            out.push(&text[start..i]);
            start = i;
        }
        current = Some(k);
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// Apply an edit script to `seq` (whose text is the script's old side). Positions are in
/// characters, as hashseq indexes.
pub fn apply(seq: &mut HashSeq, script: &[Edit]) {
    let mut pos = 0;
    for edit in script {
        match edit {
            Edit::Equal(n) => pos += n,
            Edit::Delete(n) => {
                seq.remove_batch(pos, *n);
            }
            Edit::Insert(text) => {
                seq.insert_batch(pos, text.chars());
                pos += text.chars().count();
            }
        }
    }
}

/// Turn `old` into `new` on `seq` (whose text is `old`) with [`edits`]; true when the seq's
/// text is then `new`.
pub fn replay(seq: &mut HashSeq, old: &str, new: &str) -> bool {
    apply(seq, &edits(old, new));
    seq.iter().eq(new.chars())
}
