//! Three-way merge of a markdown note through a hashseq CRDT.
//!
//! The app re-renders an exported note while the user may have edited the file in the vault.
//! The history holds two sequences with one origin: `combined` (the app's and the user's
//! operations; its text is what we last wrote) and `app` (the app's operations alone; its text
//! is what the app last rendered). A new rendering is diffed against the app branch, the file
//! against the combined branch, and the two are merged. The app's operations on the combined
//! branch are already there, so only the new ones are added and a user edit that no rendering
//! contains survives every later export, not just the first.
//!
//! The diff is by lines (patience), then by words (and punctuation) inside every replaced
//! block of lines. A diff by characters across a note, or one that replaces a whole middle
//! section, garbles merges.
//!
//! Identical concurrent edits collapse only when both forks made them from the same state;
//! after other edits they are kept twice (both ticking one checkbox gives `[xx]`), so callers
//! normalize such text before calling.

use anyhow::{anyhow, bail, ensure, Context, Result};
use hashseq::{
    encoding::{decode_hashseq, encode_hashseq},
    HashSeq, Id,
};
use similar::{Algorithm, ChangeTag, DiffOp, TextDiff};

/// Start of a stored history.
pub const MAGIC: &[u8; 4] = b"MNHS";
/// Layout of a stored history: [`MAGIC`], this byte, a little-endian `u32` with the length of
/// the combined branch's encoding, that encoding, then the app branch's encoding (the rest).
pub const VERSION: u8 = 1;

/// The result of a merge: the new history to store and the note to write.
pub struct Merged {
    pub history: Vec<u8>,
    pub text: String,
}

/// One step of an edit script over characters.
#[derive(Debug, PartialEq, Eq)]
enum Edit {
    Equal(usize),
    Delete(usize),
    Insert(String),
}

/// The stored state: two sequences with one origin.
struct History {
    /// Everything: the app's operations and the user's. Its text is what we last wrote.
    combined: HashSeq,
    /// Only the app's operations. Its text is what the app last rendered.
    app: HashSeq,
}

/// Merge `app` (the app's new rendering) and `vault` (the file as the user left it, if it
/// differs from what we wrote) into the stored `history`.
pub fn merge(history: Option<&[u8]>, app: &str, vault: Option<&str>) -> Result<Merged> {
    merge_with(history, app, vault, false)
}

/// A one-off three-way merge: a history is started from `base` (as [`merge`] with no history
/// does from the app's text), then `app` and `vault` are merged into it as usual.
pub fn merge_from_base(base: &str, app: &str, vault: Option<&str>) -> Result<Merged> {
    let start = merge(None, base, None)?;
    merge(Some(&start.history), app, vault)
}

/// [`merge`], optionally rebased: with `rebase` the app branch is first set to the combined
/// branch, so `app` is diffed against the combined text (what we last wrote, user edits
/// included) instead of the app's last rendering. A new rendering that already echoes the
/// user's edits then does not add them a second time. `rebase` means nothing without a
/// history and is ignored.
pub fn merge_with(
    history: Option<&[u8]>,
    app: &str,
    vault: Option<&str>,
    rebase: bool,
) -> Result<Merged> {
    let Some(history) = history else {
        ensure!(
            vault.is_none(),
            "a vault version needs a history to merge against"
        );
        let mut seq = HashSeq::new(random_id()?);
        seq.insert_batch(0, app.chars());
        return Ok(Merged {
            history: encode(&History {
                combined: seq.clone(),
                app: seq,
            }),
            text: app.to_string(),
        });
    };
    let History {
        combined,
        app: mut old,
    } = decode(history)?;
    if rebase {
        old = combined.clone();
    }

    // The app's side: its last rendering, diffed into its new one.
    let old_text: String = old.iter().collect();
    let mut app_branch = old;
    replay(&mut app_branch, &old_text, app).context("applying the app's version")?;

    // The user's side: what we last wrote, diffed into the file now. The combined branch
    // already holds the app's earlier operations, so merging in the app branch adds only the
    // new ones, and a user edit is not undone by an app rendering that never had it.
    let combined_text: String = combined.iter().collect();
    let mut result = combined;
    if let Some(vault) = vault.filter(|v| *v != combined_text) {
        replay(&mut result, &combined_text, vault).context("applying the vault's version")?;
    }
    result.merge(app_branch.clone());
    Ok(Merged {
        text: result.iter().collect(),
        history: encode(&History {
            combined: result,
            app: app_branch,
        }),
    })
}

/// The text held by an encoded history: what was last written.
pub fn text_of(history: &[u8]) -> Result<String> {
    Ok(decode(history)?.combined.iter().collect())
}

fn random_id() -> Result<Id> {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).map_err(|e| anyhow!("no random bytes: {e}"))?;
    Ok(Id(bytes))
}

fn encode(history: &History) -> Vec<u8> {
    let combined = encode_hashseq(&history.combined);
    let app = encode_hashseq(&history.app);
    let mut out = MAGIC.to_vec();
    out.push(VERSION);
    out.extend_from_slice(&(combined.len() as u32).to_le_bytes());
    out.extend_from_slice(&combined);
    out.extend_from_slice(&app);
    out
}

fn decode(bytes: &[u8]) -> Result<History> {
    let Some(rest) = bytes.strip_prefix(MAGIC.as_slice()) else {
        bail!("not a note history (bad magic)");
    };
    let body = match rest.split_first() {
        Some((&VERSION, body)) => body,
        Some((v, _)) => bail!("unsupported note history version {v}"),
        None => bail!("note history is truncated"),
    };
    let Some((len, body)) = body.split_first_chunk::<4>() else {
        bail!("note history is truncated");
    };
    let len = u32::from_le_bytes(*len) as usize;
    ensure!(
        len <= body.len(),
        "note history is truncated (branch length out of range)"
    );
    let (combined, app) = body.split_at(len);
    let corrupt = |e| anyhow!("note history is corrupt: {e:?}");
    let combined = decode_hashseq(combined).map_err(corrupt)?;
    let app = decode_hashseq(app).map_err(corrupt)?;
    ensure!(
        combined.origin() == app.origin(),
        "note history is corrupt: the branches have different origins"
    );
    Ok(History { combined, app })
}

/// Turn `old` into `new` on `seq` (whose text is `old`), and check it got there.
fn replay(seq: &mut HashSeq, old: &str, new: &str) -> Result<()> {
    let mut pos = 0;
    for edit in edits(old, new) {
        match edit {
            Edit::Equal(n) => pos += n,
            Edit::Delete(n) => {
                seq.remove_batch(pos, n);
            }
            Edit::Insert(text) => {
                seq.insert_batch(pos, text.chars());
                pos += text.chars().count();
            }
        }
    }
    ensure!(
        seq.iter().eq(new.chars()),
        "replaying the diff did not produce the target text"
    );
    Ok(())
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
fn edits(old: &str, new: &str) -> Vec<Edit> {
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
                let (a, b) = (tokens(&a), tokens(&b));
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
/// (`similar`'s own word split would keep "April," as one token).
fn tokens(text: &str) -> Vec<&str> {
    #[derive(PartialEq, Clone, Copy)]
    enum Kind {
        Word,
        Space,
        Other,
    }
    let kind = |c: char| {
        if c.is_alphanumeric() || c == '_' {
            Kind::Word
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
        if current.is_some_and(|cur| cur != k || k == Kind::Other) {
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
