//! Mnemosyne's live shared notes: one meeting's notes as a hashseq document, and markdown
//! both ways. Used by the sidecar (`mnemosyne-link notes doc`, see [`api`]) and compiled into
//! the browser's wasm module, so the server's fold and the client's markdown paste agree.
//!
//! # The document (docs/plans/2026-10-12-hashseq-collaboration.md, "Phase 6 design")
//!
//! One flat seq per meeting, structure in marks:
//!
//! - **Object**: the seq opened at a 32-byte origin, `sha256("mnemosyne/notes/" + session_id)`
//!   (computed by the backend, given as hex). Its object id is `object_id(KIND_SEQ ‖ origin)`,
//!   what wasm's `createSeq(origin)` / `seqId(origin)` give. Opening is idempotent.
//! - **Text**: lines ending in `\n`; the document ends with one (an empty document has no
//!   lines). A document missing its last `\n` (a client deleted it) renders as if it were
//!   there, and `set_text` puts it back.
//! - **Indexes** are Unicode scalar values (code points), as hashseq counts elements: one
//!   `char` per element. JavaScript strings count UTF-16 units, so a client converts.
//! - **Line kind**: a closed mark of kind [`KIND_BLOCK`] over the line's `\n`
//!   (`Before(nl)..After(nl)`), value one of `h1 h2 h3 bullet number task task_done quote
//!   code table table_head` ([`model::Block`]). A paragraph has none.
//! - [`KIND_LANG`] on a code run's first terminator (the fence's info string);
//!   [`KIND_ALIGN`] on a table head row's terminator, one of `l c r -` per column (`lc-r`:
//!   14 columns stay within 14 bytes, inside the delta).
//! - **Tables**: rows are `table` lines, the first row of a run `table_head`; cells are
//!   separated by U+001F ([`model::CELL`]). A `table` line after a non-table line renders as
//!   a head. Ragged rows are padded with empty cells.
//! - **Inline**: [`KIND_BOLD`], [`KIND_ITALIC`], [`KIND_STRIKE`] expanding at the end only
//!   (`Before(first)..Before(next)`, `After(last)` at the document's end: wasm `markRange`),
//!   value [`FLAG_VALUE`]; [`KIND_CODE`] closed (`Before(first)..After(last)`: wasm
//!   `markRangeClosed`), value [`FLAG_VALUE`]; [`KIND_LINK`] closed, value the
//!   URL; `comment:<id>` ([`COMMENT_PREFIX`]) closed, value the thread. Any value other than
//!   the unmark tombstone turns a flag on.
//! - **Concurrent values** stay live together (MVR); render resolves them: `task` with
//!   `task_done` is done; other `block` conflicts show the first value in markedSpans order
//!   (the lowest mark id, as the client's editor does); two live link URLs render as the
//!   plain text followed by " (link conflict)"; `lang`/`align` take the first value too.
//! - Mark strings longer than 14 UTF-8 bytes (most URLs, every `comment:<id>`) are
//!   artifacts: they travel as 0xAF frames next to the 0xDE delta, never inside it.
//!
//! # Markdown
//!
//! [`render()`] is canonical (see [`render`]); [`parse()`] reads it back and a practical
//! superset (see [`parse`]). `render(parse(md))` is a fixpoint for rendered markdown, and
//! `set_text(doc, render(doc))` changes nothing.

pub mod api;
pub mod client;
pub mod diff;
pub mod doc;
pub mod model;
pub mod parse;
pub mod render;

pub use doc::{fold, Comments, Notes};
pub use model::{normalize, Block, Model, Parsed};
pub use render::{render, Rendered};

/// Parse markdown into a normalized document.
pub fn parse(markdown: &str) -> Model {
    normalize(&parse::parse(markdown))
}

pub const KIND_BLOCK: &str = "block";
pub const KIND_LANG: &str = "lang";
pub const KIND_ALIGN: &str = "align";
pub const KIND_BOLD: &str = "bold";
pub const KIND_ITALIC: &str = "italic";
pub const KIND_STRIKE: &str = "strike";
pub const KIND_CODE: &str = "code";
pub const KIND_LINK: &str = "link";
/// A comment's mark kind is this prefix and the comment id.
pub const COMMENT_PREFIX: &str = "comment:";
/// The value of a flag mark (bold, italic, strike, code).
pub const FLAG_VALUE: &str = "true";

/// An error with a message for the caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error(pub String);

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

/// A 32-byte id from 64 hex characters.
pub fn id_from_hex(hex: &str) -> Result<hashseq::Id, Error> {
    let hex = hex.trim();
    if hex.len() != 64 || !hex.is_ascii() {
        return Err(Error("expected 64 hex characters".into()));
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16)
            .map_err(|_| Error("expected 64 hex characters".into()))?;
    }
    Ok(hashseq::Id(out))
}

/// 64 hex characters from a 32-byte id (the form of wasm's `seqIdAt`).
pub fn id_to_hex(id: &hashseq::Id) -> String {
    id.0.iter().map(|b| format!("{b:02x}")).collect()
}
