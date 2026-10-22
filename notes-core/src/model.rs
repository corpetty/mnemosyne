//! The notes document as data: text, one set of attributes per line, one format per character.
//! What [`crate::parse`] gives, what [`crate::render`] reads, and what `set_text` compares.

use serde::Serialize;

/// Separates table cells in the text.
pub const CELL: char = '\u{1f}';

/// A line's kind: the value of the closed `block` mark on its terminating `\n`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Block {
    H1,
    H2,
    H3,
    Bullet,
    Number,
    Task,
    TaskDone,
    Quote,
    Code,
    Table,
    TableHead,
}

impl Block {
    pub const ALL: [Block; 11] = [
        Block::H1,
        Block::H2,
        Block::H3,
        Block::Bullet,
        Block::Number,
        Block::Task,
        Block::TaskDone,
        Block::Quote,
        Block::Code,
        Block::Table,
        Block::TableHead,
    ];

    /// The mark value.
    pub fn as_str(self) -> &'static str {
        match self {
            Block::H1 => "h1",
            Block::H2 => "h2",
            Block::H3 => "h3",
            Block::Bullet => "bullet",
            Block::Number => "number",
            Block::Task => "task",
            Block::TaskDone => "task_done",
            Block::Quote => "quote",
            Block::Code => "code",
            Block::Table => "table",
            Block::TableHead => "table_head",
        }
    }

    pub fn parse(value: &str) -> Option<Block> {
        Block::ALL.into_iter().find(|b| b.as_str() == value)
    }

    pub fn is_list(self) -> bool {
        matches!(
            self,
            Block::Bullet | Block::Number | Block::Task | Block::TaskDone
        )
    }

    pub fn is_table(self) -> bool {
        matches!(self, Block::Table | Block::TableHead)
    }
}

/// Attributes of one line (all held by marks on its terminator).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct LineAttr {
    pub kind: Option<Block>,
    /// A code fence's language (meaningful on the first line of a code run).
    pub lang: Option<String>,
    /// A table head row's column alignment: one of `l`, `c`, `r`, `-` per column (`lc-r`).
    pub align: Option<String>,
}

/// A link state of one character.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum Link {
    #[default]
    None,
    /// An index into [`Model::urls`].
    Url(u32),
    /// More than one live URL (concurrent edits): rendered as text with " (link conflict)".
    Conflict,
}

pub const BOLD: u8 = 1;
pub const ITALIC: u8 = 2;
pub const STRIKE: u8 = 4;
pub const CODE: u8 = 8;

/// The inline format of one character.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub struct Fmt {
    pub flags: u8,
    pub link: Link,
}

impl Fmt {
    pub fn has(&self, flag: u8) -> bool {
        self.flags & flag != 0
    }
    pub fn set(&mut self, flag: u8, on: bool) {
        if on {
            self.flags |= flag;
        } else {
            self.flags &= !flag;
        }
    }
}

/// The document. `text` is lines ending in `\n` (a document from elsewhere may lack the last
/// one); `lines` has one entry per line, `fmt` one per character.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Model {
    pub text: Vec<char>,
    pub lines: Vec<LineAttr>,
    pub fmt: Vec<Fmt>,
    pub urls: Vec<String>,
}

/// One inline range of a [`Parsed`] document, in characters of its text.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InlineRange {
    pub start: usize,
    pub end: usize,
    /// `bold`, `italic`, `strike`, `code` or `link`.
    pub kind: &'static str,
    /// The URL of a link.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<String>,
}

/// A [`Model`] as plain data: what a client applies after parsing pasted markdown.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Parsed {
    /// The text, cells separated by U+001F, every line ending in `\n`.
    pub text: String,
    pub lines: Vec<LineAttr>,
    pub inline: Vec<InlineRange>,
}

impl Model {
    /// Line `i`'s character range, without its terminator, and the terminator's index.
    pub fn line_spans(&self) -> Vec<(usize, usize, Option<usize>)> {
        let mut out = Vec::new();
        let mut start = 0;
        for (i, &c) in self.text.iter().enumerate() {
            if c == '\n' {
                out.push((start, i, Some(i)));
                start = i + 1;
            }
        }
        if start < self.text.len() {
            out.push((start, self.text.len(), None));
        }
        out
    }

    pub fn url(&self, link: Link) -> Option<&str> {
        match link {
            Link::Url(i) => self.urls.get(i as usize).map(String::as_str),
            _ => None,
        }
    }

    pub fn intern_url(&mut self, url: &str) -> Link {
        match self.urls.iter().position(|u| u == url) {
            Some(i) => Link::Url(i as u32),
            None => {
                self.urls.push(url.to_string());
                Link::Url(self.urls.len() as u32 - 1)
            }
        }
    }

    /// Plain data for clients: inline ranges are maximal runs per kind (and per URL).
    pub fn to_parsed(&self) -> Parsed {
        let mut inline = Vec::new();
        for (flag, kind) in [
            (BOLD, "bold"),
            (ITALIC, "italic"),
            (STRIKE, "strike"),
            (CODE, "code"),
        ] {
            let mut start = None;
            for i in 0..=self.fmt.len() {
                let on = self.fmt.get(i).is_some_and(|f| f.has(flag));
                match (on, start) {
                    (true, None) => start = Some(i),
                    (false, Some(s)) => {
                        inline.push(InlineRange {
                            start: s,
                            end: i,
                            kind,
                            value: None,
                        });
                        start = None;
                    }
                    _ => {}
                }
            }
        }
        let mut start: Option<(usize, Link)> = None;
        for i in 0..=self.fmt.len() {
            let link = self.fmt.get(i).map_or(Link::None, |f| f.link);
            if start.is_some_and(|(_, l)| l == link) {
                continue;
            }
            if let Some((s, l)) = start.take() {
                if let Some(url) = self.url(l) {
                    inline.push(InlineRange {
                        start: s,
                        end: i,
                        kind: "link",
                        value: Some(url.to_string()),
                    });
                }
            }
            if link != Link::None {
                start = Some((i, link));
            }
        }
        inline.sort_by_key(|r| (r.start, r.end, r.kind));
        Parsed {
            text: self.text.iter().collect(),
            lines: self.lines.clone(),
            inline,
        }
    }
}

/// Characters whose inline format means nothing: terminators, cell separators in table rows,
/// and every character of a code line (its text is raw).
pub fn dont_care(model: &Model) -> Vec<bool> {
    let mut out = vec![false; model.text.len()];
    for (li, (start, end, nl)) in model.line_spans().into_iter().enumerate() {
        let kind = model.lines.get(li).and_then(|l| l.kind);
        for (i, slot) in out.iter_mut().enumerate().take(end).skip(start) {
            *slot = match kind {
                Some(Block::Code) => true,
                Some(Block::Table | Block::TableHead) => model.text[i] == CELL,
                _ => false,
            };
        }
        if let Some(nl) = nl {
            out[nl] = true;
        }
    }
    out
}

/// The canonical form of a document's attributes; equal canonical forms render the same.
///
/// - Lines without a terminator have no kind.
/// - A table run starts at a `table_head` line or at a `table` line after a non-table line;
///   its first line is the head. `align` only on heads, padded or cut to the run's widest
///   row, none when every column is `-`. `lang` only on a code run's first line.
/// - Inline formats are cleared where they mean nothing ([`dont_care`]).
/// - Bold, italic and strike never begin or end on whitespace (outside code): markdown
///   delimiters next to whitespace do not parse. Runs are cut where the delimiter has to be
///   closed and reopened (an outer format changes: link > bold > italic > strike), and each
///   piece is trimmed.
/// - A link conflict stays a conflict.
pub fn normalize(model: &Model) -> Model {
    let mut m = model.clone();
    let spans = m.line_spans();
    m.lines.resize(spans.len(), LineAttr::default());
    m.lines.truncate(spans.len());
    for (li, (_, _, nl)) in spans.iter().enumerate() {
        if nl.is_none() {
            m.lines[li] = LineAttr::default();
        }
    }
    // Table runs and heads; lang on code-run starts.
    let mut li = 0;
    while li < m.lines.len() {
        match m.lines[li].kind {
            Some(k) if k.is_table() => {
                let start = li;
                m.lines[li].kind = Some(Block::TableHead);
                li += 1;
                while li < m.lines.len() && m.lines[li].kind == Some(Block::Table) {
                    li += 1;
                }
                let cols = (start..li)
                    .map(|l| {
                        let (s, e, _) = spans[l];
                        m.text[s..e].iter().filter(|&&c| c == CELL).count() + 1
                    })
                    .max()
                    .unwrap_or(1);
                for l in start..li {
                    m.lines[l].lang = None;
                    if l > start {
                        m.lines[l].align = None;
                    }
                }
                m.lines[start].align = normalize_align(m.lines[start].align.as_deref(), cols);
            }
            Some(Block::Code) => {
                m.lines[li].align = None;
                m.lines[li].lang = m.lines[li]
                    .lang
                    .as_deref()
                    .map(|l| l.trim().replace('\n', " "))
                    .filter(|l| !l.is_empty());
                li += 1;
                while li < m.lines.len() && m.lines[li].kind == Some(Block::Code) {
                    m.lines[li].lang = None;
                    m.lines[li].align = None;
                    li += 1;
                }
            }
            _ => {
                m.lines[li].lang = None;
                m.lines[li].align = None;
                li += 1;
            }
        }
    }
    if m.fmt.len() != m.text.len() {
        m.fmt.resize(m.text.len(), Fmt::default());
    }
    let skip = dont_care(&m);
    for (i, f) in m.fmt.iter_mut().enumerate() {
        if skip[i] {
            *f = Fmt::default();
        }
    }
    // Trim the delimiter kinds, outermost first: each one's cut points depend on the
    // (already normalized) formats outside it.
    for (flag, outer) in [(BOLD, 0u8), (ITALIC, BOLD), (STRIKE, BOLD | ITALIC)] {
        let mut i = 0;
        while i < m.text.len() {
            if !m.fmt[i].has(flag) {
                i += 1;
                continue;
            }
            // A piece: a run of `flag` with the same outer formats and link, within one
            // segment (never across a don't-care character).
            let start = i;
            let key = |f: &Fmt| (f.flags & outer, f.link);
            let k0 = key(&m.fmt[i]);
            i += 1;
            while i < m.text.len() && !skip[i] && m.fmt[i].has(flag) && key(&m.fmt[i]) == k0 {
                i += 1;
            }
            let end = i;
            let trimmable = |j: usize| m.text[j].is_whitespace() && !m.fmt[j].has(CODE);
            let mut s = start;
            while s < end && trimmable(s) {
                s += 1;
            }
            let mut e = end;
            while e > s && trimmable(e - 1) {
                e -= 1;
            }
            for j in (start..s).chain(e..end) {
                m.fmt[j].set(flag, false);
            }
        }
    }
    m
}

/// `align` for a head row of `cols` columns: one of `l`, `c`, `r`, `-` per column, padded
/// with `-` or cut to `cols`; none when every column is `-`.
pub fn normalize_align(align: Option<&str>, cols: usize) -> Option<String> {
    let given: Vec<char> = align.map(|a| a.chars().collect()).unwrap_or_default();
    let out: String = (0..cols)
        .map(|i| match given.get(i) {
            Some(&a @ ('l' | 'c' | 'r')) => a,
            _ => '-',
        })
        .collect();
    if out.chars().all(|c| c == '-') {
        None
    } else {
        Some(out)
    }
}

/// Do two documents (normalized) render the same? Compares URLs by value.
pub fn same(a: &Model, b: &Model) -> bool {
    a.text == b.text
        && a.lines == b.lines
        && a.fmt.len() == b.fmt.len()
        && a.fmt.iter().zip(&b.fmt).all(|(x, y)| same_fmt(a, x, b, y))
}

pub fn same_fmt(a: &Model, x: &Fmt, b: &Model, y: &Fmt) -> bool {
    x.flags == y.flags && same_link(a, x.link, b, y.link)
}

pub fn same_link(a: &Model, x: Link, b: &Model, y: Link) -> bool {
    match (x, y) {
        (Link::None, Link::None) | (Link::Conflict, Link::Conflict) => true,
        (Link::Url(_), Link::Url(_)) => a.url(x) == b.url(y),
        _ => false,
    }
}
