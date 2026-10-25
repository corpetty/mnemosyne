//! The JSON requests of `mnemosyne-link notes doc` (one request in, one answer out). Bytes
//! are standard base64. Every request names the notes object by its origin (`notes`, 64
//! hex characters).
//!
//! - `{"op":"fold","notes":hex,"snapshot":b64|null,"frames":[b64]}` →
//!   `{"snapshot":b64,"markdown":s,"plain":s,"comments":{"<id>":[s,e]|null},"tasks":[...]}`
//!   (`tasks` is in every answer, see below)
//! - `{"op":"set_text","notes":hex,"snapshot":b64|null,"frames":[b64],"markdown":s}` →
//!   `{"frames":[b64],"snapshot":b64,"markdown":s,"plain":s,"comments":{...}}`: `frames` are
//!   what to broadcast to clients (0xAF artifacts first, then one 0xDE delta of only the new
//!   ops), `[]` when nothing changed; `snapshot` is after the given frames and the new ones.
//! - `{"op":"merge","notes":hex,"snapshot":b64,"other":b64,"frames":[b64]?}` →
//!   `{"snapshot":b64,"markdown":s,"plain":s,"comments":{...},"frames":[b64]}` (the union).
//!   The answer's `frames` are what the replica holding `snapshot` lacks of the union, for
//!   its connected clients: 0xAF artifacts the new ops reference, then one 0xDE delta; `[]`
//!   when the union adds nothing to it. (The request's `frames` are applied after the union
//!   and count as new.)
//! - `{"op":"mark_comment","notes":hex,"snapshot":b64|null,"frames":[b64],"first":hex,"last":hex,
//!   "comment":id}` → `{"frames":[b64],"snapshot":b64,"markdown":s,"plain":s,"comments":{...}}`:
//!   marks `comment:<id>` closed from element `first` to element `last` (the ids of wasm's
//!   `seqIdAt`, hex, both included) for a client that cannot author the mark. `frames` as in
//!   `set_text`; `snapshot` includes the mark. An id that is unknown or deleted, or `last`
//!   before `first`, is an error (the comment is detached).
//! - `{"op":"set_kind","notes":hex,"snapshot":b64|null,"frames":[b64],"line":hex,"kind":"task"|
//!   "task_done"}` → as `mark_comment`: makes the task line whose terminating `\n` is element
//!   `line` open or done. `frames` are what to broadcast, `[]` when the line already shows
//!   `kind`. An id that is unknown, deleted, not a line end or not a task line is an error.
//!
//! Every answer has `tasks`, the lines whose kind is `task` or `task_done`, in document
//! order: `[{"line":hex,"done":bool,"text":s,"links":[[start,end,url]],"frame":i|null}]`.
//! `line` is the element id of the line's `\n` (wasm's `seqIdAt`, what `set_kind` takes),
//! `text` the line without it, `links` the live link marks as `[start, end)` code points of
//! `text`. `frame` is the index in the request's `frames` of the latest frame that added the
//! line's `\n` or the mark giving its kind; `null` when neither came from a request frame
//! (the snapshot, the union in `merge`, or what the request itself did).
//!
//! `frames` are a client log in order: 0xDE deltas and 0xAF artifacts. Duplicates are
//! harmless; delta groups for other objects are ignored; a frame that does not decode is an
//! error naming its index (`frame 3: ...`). `comments` give each `comment:<id>` mark's range
//! in `plain` as `[start, end)` code points, `null` once all its characters are gone.

use base64::{engine::general_purpose::STANDARD, Engine};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::doc::{fold_tracked, Arrivals, Notes};
use crate::model::Block;
use crate::{id_from_hex, id_to_hex, Error};

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    Fold {
        notes: String,
        snapshot: Option<String>,
        #[serde(default)]
        frames: Vec<String>,
    },
    SetText {
        notes: String,
        snapshot: Option<String>,
        #[serde(default)]
        frames: Vec<String>,
        markdown: String,
    },
    Merge {
        notes: String,
        snapshot: String,
        other: String,
        #[serde(default)]
        frames: Vec<String>,
    },
    MarkComment {
        notes: String,
        snapshot: Option<String>,
        #[serde(default)]
        frames: Vec<String>,
        first: String,
        last: String,
        comment: String,
    },
    SetKind {
        notes: String,
        snapshot: Option<String>,
        #[serde(default)]
        frames: Vec<String>,
        line: String,
        kind: String,
    },
}

/// Handle one request (JSON text) and give the answer (JSON text).
pub fn handle_str(input: &str) -> Result<String, Error> {
    let request: Value =
        serde_json::from_str(input).map_err(|e| Error(format!("invalid request: {e}")))?;
    Ok(handle(request)?.to_string())
}

/// Handle one request.
pub fn handle(request: Value) -> Result<Value, Error> {
    let request: Request =
        serde_json::from_value(request).map_err(|e| Error(format!("invalid request: {e}")))?;
    match request {
        Request::Fold {
            notes,
            snapshot,
            frames,
        } => {
            let (notes, arrivals) = load(&notes, snapshot.as_deref(), &frames)?;
            Ok(answer(&notes, None, &arrivals))
        }
        Request::SetText {
            notes,
            snapshot,
            frames,
            markdown,
        } => {
            let (mut notes, arrivals) = load(&notes, snapshot.as_deref(), &frames)?;
            let out = notes.set_text(&markdown);
            Ok(answer(&notes, Some(out), &arrivals))
        }
        Request::MarkComment {
            notes,
            snapshot,
            frames,
            first,
            last,
            comment,
        } => {
            let (mut notes, arrivals) = load(&notes, snapshot.as_deref(), &frames)?;
            let first = id_from_hex(&first).map_err(|e| Error(format!("first: {e}")))?;
            let last = id_from_hex(&last).map_err(|e| Error(format!("last: {e}")))?;
            if comment.is_empty() {
                return Err(Error("comment: empty".into()));
            }
            let out = notes.mark_comment(&first, &last, &comment)?;
            Ok(answer(&notes, Some(out), &arrivals))
        }
        Request::SetKind {
            notes,
            snapshot,
            frames,
            line,
            kind,
        } => {
            let (mut notes, arrivals) = load(&notes, snapshot.as_deref(), &frames)?;
            let line = id_from_hex(&line).map_err(|e| Error(format!("line: {e}")))?;
            let kind = match kind.as_str() {
                "task" => Block::Task,
                "task_done" => Block::TaskDone,
                _ => return Err(Error("kind: expected task or task_done".into())),
            };
            let out = notes.set_kind(&line, kind)?;
            Ok(answer(&notes, Some(out), &arrivals))
        }
        Request::Merge {
            notes,
            snapshot,
            other,
            frames,
        } => {
            let origin = id_from_hex(&notes).map_err(|e| Error(format!("notes: {e}")))?;
            let mut arrivals = Arrivals::new();
            let mut a = Notes::from_snapshot(origin, &b64("snapshot", &snapshot)?)
                .map_err(|e| Error(format!("snapshot: {e}")))?;
            let b = Notes::from_snapshot(origin, &b64("other", &other)?)
                .map_err(|e| Error(format!("other: {e}")))?;
            let had = a.clone();
            let before = had.web.clock();
            a.merge(b);
            for (i, frame) in frames.iter().enumerate() {
                a.apply_frame_at(&b64(&format!("frame {i}"), frame)?, i, &mut arrivals)
                    .map_err(|e| Error(format!("frame {i}: {e}")))?;
            }
            let delta = a.frames_since(&before, &had);
            Ok(answer(&a, Some(delta), &arrivals))
        }
    }
}

fn b64(what: &str, s: &str) -> Result<Vec<u8>, Error> {
    STANDARD
        .decode(s.trim())
        .map_err(|_| Error(format!("{what}: not base64")))
}

fn load(
    notes: &str,
    snapshot: Option<&str>,
    frames: &[String],
) -> Result<(Notes, Arrivals), Error> {
    let origin = id_from_hex(notes).map_err(|e| Error(format!("notes: {e}")))?;
    let snapshot = snapshot.map(|s| b64("snapshot", s)).transpose()?;
    let frames = frames
        .iter()
        .enumerate()
        .map(|(i, f)| b64(&format!("frame {i}"), f))
        .collect::<Result<Vec<_>, _>>()?;
    fold_tracked(origin, snapshot.as_deref(), &frames)
}

fn answer(notes: &Notes, frames: Option<Vec<Vec<u8>>>, arrivals: &Arrivals) -> Value {
    let rendered = notes.render();
    let comments: serde_json::Map<String, Value> = notes
        .comments()
        .into_iter()
        .map(|(id, range)| (id, range.map_or(Value::Null, |(s, e)| json!([s, e]))))
        .collect();
    let tasks: Vec<Value> = notes
        .tasks()
        .into_iter()
        .map(|t| {
            json!({
                "line": id_to_hex(&t.line),
                "done": t.done,
                "frame": t.frame(arrivals),
                "text": t.text,
                "links": t.links.iter().map(|(s, e, u)| json!([s, e, u])).collect::<Vec<_>>(),
            })
        })
        .collect();
    let mut out = json!({
        "snapshot": STANDARD.encode(notes.snapshot()),
        "markdown": rendered.markdown,
        "plain": rendered.plain,
        "comments": comments,
        "tasks": tasks,
    });
    if let Some(frames) = frames {
        out["frames"] = frames.iter().map(|f| STANDARD.encode(f)).collect();
    }
    out
}
