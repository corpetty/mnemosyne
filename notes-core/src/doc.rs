//! The notes document in a hashseq store: reading it as a [`Model`], editing it with the
//! same marks a client makes, `set_text`, frames, snapshots.

use std::collections::BTreeMap;

use hashseq::encoding::{
    apply_delta, decode_varint, encode_delta, encode_varint, ARTIFACT_TAG, DELTA_TAG,
};
use hashseq::value::{is_identity, KIND_SEQ, TOMBSTONE};
use hashseq::{
    decode_hashweb, encode_hashweb, Anchor, HashSeq, HashWeb, HashWebClock, Id, Op, Payload, Value,
};

use crate::diff;
use crate::model::{
    dont_care, normalize, same, same_link, Block, Fmt, LineAttr, Link, Model, BOLD, CODE, ITALIC,
    STRIKE,
};
use crate::parse::parse;
use crate::render::{plain, render, Rendered};
use crate::{
    Error, COMMENT_PREFIX, FLAG_VALUE, KIND_ALIGN, KIND_BLOCK, KIND_BOLD, KIND_CODE, KIND_ITALIC,
    KIND_LANG, KIND_LINK, KIND_STRIKE,
};

/// One meeting's notes: the store and the seq opened at the notes origin.
#[derive(Debug, Clone)]
pub struct Notes {
    pub web: HashWeb,
    pub origin: Id,
    /// The seq's object id, `object_id(KIND_SEQ ‖ origin)` (wasm `seqId(origin)`).
    pub obj: Id,
}

/// Comment ranges in the plain text: `[start, end)` in characters, `None` when every
/// character the comment covered is gone (or it was unmarked).
pub type Comments = BTreeMap<String, Option<(usize, usize)>>;

impl Notes {
    /// An empty store with the notes seq opened (idempotent, like `createSeq`).
    pub fn new(origin: Id) -> Notes {
        let mut web = HashWeb::new();
        let obj = web.create_seq(origin);
        Notes { web, origin, obj }
    }

    /// From a canonical snapshot (`encode`), opening the notes seq if it is not there.
    pub fn from_snapshot(origin: Id, bytes: &[u8]) -> Result<Notes, Error> {
        let mut web = decode_hashweb(bytes).map_err(|e| Error(format!("{e}")))?;
        let obj = web.create_seq(origin);
        Ok(Notes { web, origin, obj })
    }

    /// The canonical snapshot: equal op sets (and artifacts) give equal bytes.
    pub fn snapshot(&self) -> Vec<u8> {
        encode_hashweb(&self.web)
    }

    pub fn seq(&self) -> &HashSeq {
        self.web
            .seq(&self.obj)
            .expect("the notes seq is always open")
    }

    fn seq_mut(&mut self) -> &mut HashSeq {
        self.web
            .seq_mut(&self.obj)
            .expect("the notes seq is always open")
    }

    /// Apply a frame from a client: a 0xDE delta (groups for other objects are ignored) or
    /// a 0xAF artifact. Duplicates change nothing. Returns the nodes newly delivered.
    pub fn apply_frame(&mut self, frame: &[u8]) -> Result<usize, Error> {
        match frame.first() {
            Some(&DELTA_TAG) => {
                let mine = only_object(frame, self.origin)?;
                apply_delta(&mut self.web, &mine).map_err(|e| Error(format!("{e}")))
            }
            Some(&ARTIFACT_TAG) => {
                if frame.len() < 2 {
                    return Err(Error("empty artifact".into()));
                }
                self.web.provide_artifact_bytes(frame[1..].to_vec());
                Ok(0)
            }
            Some(tag) => Err(Error(format!("unknown frame tag 0x{tag:02X}"))),
            None => Err(Error("empty frame".into())),
        }
    }

    /// Union with another replica of the same notes.
    pub fn merge(&mut self, other: Notes) {
        self.web.merge(other.web);
        self.obj = self.web.create_seq(self.origin);
    }

    /// What a replica holding `had` is missing of this one, as frames for its clients:
    /// artifacts (0xAF) for the strings the new ops reference and `had` lacks, then one delta
    /// (0xDE) of the ops outside `had`'s causal closure (`before` is `had.web.clock()`).
    /// Empty when `had` already has everything.
    pub fn frames_since(&self, before: &HashWebClock, had: &Notes) -> Vec<Vec<u8>> {
        let groups = self.web.deltas_for(before);
        if groups.is_empty() {
            return Vec::new();
        }
        let mut wanted: Vec<Id> = Vec::new();
        let mut want = |id: &Id| {
            if !is_identity(id)
                && had.web.artifact_bytes(id).is_none()
                && self.web.artifact_bytes(id).is_some()
                && !wanted.contains(id)
            {
                wanted.push(*id);
            }
        };
        for (_, _, nodes) in &groups {
            for node in nodes {
                match &node.op {
                    Op::Mark { kind_v, value, .. } => {
                        want(kind_v);
                        want(value);
                    }
                    Op::Put { key, value, .. } => {
                        want(key);
                        want(value);
                    }
                    Op::Insert {
                        payload: Payload::Id(id),
                        ..
                    } => want(id),
                    _ => {}
                }
            }
        }
        let mut frames = Vec::new();
        for id in wanted {
            if let Some(bytes) = self.web.artifact_bytes(&id) {
                let mut f = vec![ARTIFACT_TAG];
                f.extend_from_slice(bytes);
                frames.push(f);
            }
        }
        frames.push(encode_delta(&groups));
        frames
    }

    pub fn text(&self) -> String {
        self.seq().iter().collect()
    }

    /// Insert text at a character position (as wasm `textInsert`).
    pub fn insert(&mut self, pos: usize, text: &str) {
        self.seq_mut().insert_batch(pos, text.chars());
    }

    /// Remove `n` characters at a position (as wasm `textRemove`).
    pub fn remove(&mut self, pos: usize, n: usize) {
        self.seq_mut().remove_batch(pos, n);
    }

    /// The document as stored: marks resolved, not normalized.
    pub fn model(&self) -> Model {
        self.read().0
    }

    pub fn render(&self) -> Rendered {
        render(&self.model())
    }

    /// Every `comment:<id>` mark's current range in the plain text.
    pub fn comments(&self) -> Comments {
        let (_, mut comments) = self.read();
        for (_, node) in self.seq().all_nodes() {
            if let Op::Mark { kind_v, .. } = node.op {
                if let Some(Value::String(kind)) = self.web.resolve(&kind_v) {
                    if let Some(id) = kind.strip_prefix(COMMENT_PREFIX) {
                        comments.entry(id.to_string()).or_insert(None);
                    }
                }
            }
        }
        comments
    }

    /// Read the seq: per character text and format, per line attributes, comment ranges.
    fn read(&self) -> (Model, Comments) {
        let seq = self.seq();
        let mut model = Model::default();
        let mut comments: Comments = BTreeMap::new();
        let mut names: BTreeMap<Id, Option<String>> = BTreeMap::new();
        let mut name = |id: &Id| -> Option<String> {
            names
                .entry(*id)
                .or_insert_with(|| match self.web.resolve(id) {
                    Some(Value::String(s)) => Some(s),
                    _ => None,
                })
                .clone()
        };
        for (text, set) in seq.marked_spans() {
            let mut fmt = Fmt::default();
            let mut line = LineAttr::default();
            let mut here: Vec<String> = Vec::new();
            for (kind, lives) in &set {
                let Some(kind) = name(kind) else { continue };
                let values: Vec<Option<String>> = lives.iter().map(|(_, v)| name(v)).collect();
                let first = values.iter().flatten().next().cloned();
                match kind.as_str() {
                    KIND_BLOCK => line.kind = resolve_block(&values),
                    KIND_LANG => line.lang = first,
                    KIND_ALIGN => line.align = first,
                    KIND_BOLD => fmt.set(BOLD, true),
                    KIND_ITALIC => fmt.set(ITALIC, true),
                    KIND_STRIKE => fmt.set(STRIKE, true),
                    KIND_CODE => fmt.set(CODE, true),
                    KIND_LINK => {
                        let mut urls: Vec<String> = values.into_iter().flatten().collect();
                        urls.sort();
                        urls.dedup();
                        fmt.link = match urls.as_slice() {
                            [] => Link::None,
                            [one] => model.intern_url(one),
                            _ => Link::Conflict,
                        };
                    }
                    k => {
                        if let Some(id) = k.strip_prefix(COMMENT_PREFIX) {
                            here.push(id.to_string());
                        }
                    }
                }
            }
            for c in text.chars() {
                let at = model.text.len();
                for id in &here {
                    let range = comments.entry(id.clone()).or_insert(Some((at, at + 1)));
                    if let Some((s, e)) = range {
                        *s = (*s).min(at);
                        *e = (*e).max(at + 1);
                    }
                }
                model.text.push(c);
                model.fmt.push(fmt);
                if c == '\n' {
                    model.lines.push(line.clone());
                }
            }
        }
        if model.text.last().is_some_and(|&c| c != '\n') {
            model.lines.push(LineAttr::default());
        }
        (model, comments)
    }

    /// Make the document say `markdown`. Returns the frames for the clients, artifacts
    /// (0xAF) first and then one delta (0xDE) of only the new ops, or none when nothing
    /// changed. The text is diffed by lines, then words; kept lines keep their terminators
    /// (and so their kinds). Then marks are added or removed where the formats differ.
    pub fn set_text(&mut self, markdown: &str) -> Vec<Vec<u8>> {
        let desired = normalize(&parse(markdown));
        let before = self.web.clock();
        let mut minted = Vec::new();
        let old: String = self.text();
        let new: String = desired.text.iter().collect();
        if old != new {
            let script = diff::edits_by_line(&old, &new);
            diff::apply(self.seq_mut(), &script);
        }
        debug_assert!(self.seq().iter().eq(new.chars()));
        self.reconcile(&desired, &mut minted);
        self.new_frames(&before, &minted)
    }

    /// The frames for the clients of what happened since `before` (`web.clock()`): the
    /// `minted` artifacts (0xAF), then one delta (0xDE) of only the new ops; none when there
    /// are no new ops.
    fn new_frames(&self, before: &HashWebClock, minted: &[Id]) -> Vec<Vec<u8>> {
        let groups = self.web.deltas_for(before);
        if groups.is_empty() {
            return Vec::new();
        }
        let mut frames: Vec<Vec<u8>> = Vec::new();
        for id in minted {
            if let Some(bytes) = self.web.artifact_bytes(id) {
                let mut f = vec![ARTIFACT_TAG];
                f.extend_from_slice(bytes);
                frames.push(f);
            }
        }
        frames.push(encode_delta(&groups));
        frames
    }

    /// Mark the characters from element `first` to element `last` (their `seqIdAt` ids, both
    /// included) as comment `comment`: `Before(first)..After(last)`, what the client's
    /// `markRangeClosed` over their positions makes. For a reader, whose editor cannot author
    /// the mark. Returns the frames for the clients, as [`Notes::set_text`] does. An id that
    /// is not in the document or was deleted, or `last` before `first`, is an error (the text
    /// the comment was on is gone).
    pub fn mark_comment(
        &mut self,
        first: &Id,
        last: &Id,
        comment: &str,
    ) -> Result<Vec<Vec<u8>>, Error> {
        let at = |id: &Id, what: &str| {
            self.seq()
                .position_of(id)
                .ok_or_else(|| Error(format!("{what}: no such character (deleted or unknown)")))
        };
        let (start, end) = (at(first, "first")?, at(last, "last")?);
        if end < start {
            return Err(Error("last is before first".into()));
        }
        let before = self.web.clock();
        let mut minted = Vec::new();
        let kind = format!("{COMMENT_PREFIX}{comment}");
        self.mark_inline(start, end + 1, &kind, Some(FLAG_VALUE), false, &mut minted);
        Ok(self.new_frames(&before, &minted))
    }

    /// Mark or unmark until the document's formats (normalized) equal `desired`'s, whose
    /// text it already has. The first round only touches what renders differently; later
    /// rounds (rare: normalization depends on neighbours) set every raw value that differs.
    fn reconcile(&mut self, desired: &Model, minted: &mut Vec<Id>) {
        let skip = dont_care(desired);
        for round in 0..4 {
            let raw = self.model();
            let cur = normalize(&raw);
            if same(&cur, desired) {
                return;
            }
            let all = round > 0;
            let mut changed = false;
            let spans = desired.line_spans();
            for (li, &(_, _, nl)) in spans.iter().enumerate() {
                let Some(nl) = nl else { continue };
                let (want, have, was) = (&desired.lines[li], &cur.lines[li], &raw.lines[li]);
                if (all || have.kind != want.kind) && was.kind != want.kind {
                    self.mark_line(nl, KIND_BLOCK, want.kind.map(Block::as_str), minted);
                    changed = true;
                }
                if want.kind == Some(Block::Code)
                    && (all || have.lang != want.lang)
                    && was.lang != want.lang
                {
                    self.mark_line(nl, KIND_LANG, want.lang.as_deref(), minted);
                    changed = true;
                }
                if want.kind == Some(Block::TableHead)
                    && (all || have.align != want.align)
                    && was.align != want.align
                {
                    self.mark_line(nl, KIND_ALIGN, want.align.as_deref(), minted);
                    changed = true;
                }
            }
            for (flag, kind) in [
                (BOLD, KIND_BOLD),
                (ITALIC, KIND_ITALIC),
                (STRIKE, KIND_STRIKE),
                (CODE, KIND_CODE),
            ] {
                let differs = |i: usize| {
                    let want = desired.fmt[i].has(flag);
                    !skip[i]
                        && (all || cur.fmt[i].has(flag) != want)
                        && raw.fmt[i].has(flag) != want
                };
                for (s, e) in runs(desired.text.len(), differs, |i| desired.fmt[i].has(flag)) {
                    let on = desired.fmt[s].has(flag);
                    self.mark_inline(s, e, kind, on.then_some(FLAG_VALUE), flag != CODE, minted);
                    changed = true;
                }
            }
            let differs = |i: usize| {
                let want = desired.fmt[i].link;
                !skip[i]
                    && (all || !same_link(&cur, cur.fmt[i].link, desired, want))
                    && !same_link(&raw, raw.fmt[i].link, desired, want)
            };
            let url_at = |i: usize| desired.url(desired.fmt[i].link).map(str::to_string);
            for (s, e) in runs(desired.text.len(), differs, url_at) {
                let url = url_at(s);
                self.mark_inline(s, e, KIND_LINK, url.as_deref(), false, minted);
                changed = true;
            }
            if !changed {
                return;
            }
        }
    }

    /// Set (or with `None` remove) a line attribute: a closed mark of `kind` over the
    /// terminator at `nl`, `Before(nl)..After(nl)` (wasm `markRangeClosed` over
    /// `[nl, nl + 1)`).
    pub fn mark_line(&mut self, nl: usize, kind: &str, value: Option<&str>, minted: &mut Vec<Id>) {
        let kind_id = self.intern(kind, minted);
        let value_id = match value {
            Some(v) => self.intern(v, minted),
            None => *TOMBSTONE,
        };
        let seq = self.seq_mut();
        let Some(a) = seq.anchor_id_at(nl) else {
            return;
        };
        let _ = seq.mark_range(Anchor::Before(a), Anchor::After(a), kind_id, value_id);
    }

    /// Set (or with `None` remove) an inline format over `[start, end)`, with the anchors of
    /// hashseq's wasm API (the client marks through it): expanding marks (bold, italic,
    /// strike) as `markRange`, `Before(start)..Before(end)` (`After(end - 1)` at the end of
    /// the document), so they grow at their end only; closed ones (code, link, comments) as
    /// `markRangeClosed`, `Before(start)..After(end - 1)`. Positions go through
    /// `anchor_id_at`, as wasm does.
    pub fn mark_inline(
        &mut self,
        start: usize,
        end: usize,
        kind: &str,
        value: Option<&str>,
        expanding: bool,
        minted: &mut Vec<Id>,
    ) {
        if start >= end {
            return;
        }
        let kind_id = self.intern(kind, minted);
        let value_id = match value {
            Some(v) => self.intern(v, minted),
            None => *TOMBSTONE,
        };
        let seq = self.seq_mut();
        if end > seq.len() {
            return;
        }
        let at = |i: usize| seq.anchor_id_at(i).expect("in range");
        let s = Anchor::Before(at(start));
        let e = if expanding && end < seq.len() {
            Anchor::Before(at(end))
        } else {
            Anchor::After(at(end - 1))
        };
        let _ = seq.mark_range(s, e, kind_id, value_id);
    }

    /// A string's value id, recording artifacts this replica did not hold before.
    fn intern(&mut self, s: &str, minted: &mut Vec<Id>) -> Id {
        let v = Value::String(s.to_string());
        let id = v.value_id();
        if !is_identity(&id) && self.web.artifact_bytes(&id).is_none() && !minted.contains(&id) {
            minted.push(id);
        }
        self.web.provide_value(&v)
    }
}

/// Maximal runs of positions where `differs`, split where `value` changes.
fn runs<V: PartialEq>(
    len: usize,
    differs: impl Fn(usize) -> bool,
    value: impl Fn(usize) -> V,
) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < len {
        if !differs(i) {
            i += 1;
            continue;
        }
        let s = i;
        let v = value(i);
        i += 1;
        while i < len && differs(i) && value(i) == v {
            i += 1;
        }
        out.push((s, i));
    }
    out
}

/// The kind a line shows for its live `block` values (in markedSpans order, by mark id):
/// `task` and `task_done` both live is done (whatever else is live); any other conflict
/// shows the first value, as the client's editor does. Unknown values are ignored.
fn resolve_block(values: &[Option<String>]) -> Option<Block> {
    let known: Vec<Block> = values
        .iter()
        .flatten()
        .filter_map(|v| Block::parse(v))
        .collect();
    if known.is_empty() {
        return None;
    }
    if known.contains(&Block::Task) && known.contains(&Block::TaskDone) {
        return Some(Block::TaskDone);
    }
    known.first().copied()
}

/// A 0xDE frame with only the notes seq's groups (others are ignored), structure checked.
fn only_object(frame: &[u8], origin: Id) -> Result<Vec<u8>, Error> {
    let bad = |what: &str| Error(format!("malformed delta: {what}"));
    let mut out = vec![DELTA_TAG];
    let mut pos = 1;
    while pos < frame.len() {
        let kind = frame[pos];
        pos += 1;
        if kind > 1 {
            return Err(bad("unknown object kind"));
        }
        let org: [u8; 32] = frame
            .get(pos..pos + 32)
            .ok_or_else(|| bad("truncated origin"))?
            .try_into()
            .expect("32 bytes");
        pos += 32;
        let (n, used) = decode_varint(&frame[pos..]).map_err(|e| bad(&format!("{e}")))?;
        pos += used;
        let start = pos;
        for _ in 0..n {
            let (len, used) = decode_varint(&frame[pos..]).map_err(|e| bad(&format!("{e}")))?;
            pos += used;
            if frame.len() - pos < len {
                return Err(bad("truncated node"));
            }
            pos += len;
        }
        if kind == KIND_SEQ && Id(org) == origin {
            out.push(kind);
            out.extend_from_slice(&org);
            encode_varint(n, &mut out);
            out.extend_from_slice(&frame[start..pos]);
        }
    }
    Ok(out)
}

/// Fold: a snapshot (or nothing) plus frames in log order, to a new snapshot. A frame that
/// does not decode is an error naming its index.
pub fn fold(origin: Id, snapshot: Option<&[u8]>, frames: &[Vec<u8>]) -> Result<Notes, Error> {
    let mut notes = match snapshot {
        Some(bytes) => {
            Notes::from_snapshot(origin, bytes).map_err(|e| Error(format!("snapshot: {}", e.0)))?
        }
        None => Notes::new(origin),
    };
    for (i, frame) in frames.iter().enumerate() {
        notes
            .apply_frame(frame)
            .map_err(|e| Error(format!("frame {i}: {}", e.0)))?;
    }
    Ok(notes)
}

/// The plain text of a document (see [`plain`]).
pub fn plain_of(notes: &Notes) -> String {
    plain(&notes.model().text)
}
