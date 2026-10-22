//! Markdown to a document.
//!
//! Line based: every markdown line is one document line, except fence lines and a table's
//! separator row; one blank line that [`crate::render`] adds between blocks
//! ([`needs_gap`]) is dropped again. Covered:
//!
//! - what [`crate::render`] writes, so `render(parse(md)) == md` for its output;
//! - ATX headings (`####`–`######` become h3), `-`/`*`/`+` bullets, `1.`/`1)` numbers,
//!   `[ ]`/`[x]`/`[X]` tasks, `>` quotes, fenced code (backticks or tildes, info string as the
//!   language), GFM tables with alignment;
//! - inline: `**`/`__` bold, `*`/`_` italic, `~~` strike, code spans, `[text](url "title")`
//!   links (the title is dropped), backslash escapes; emphasis is resolved with CommonMark's
//!   delimiter algorithm, with simpler flanking (a delimiter run opens when not followed by
//!   whitespace and closes when not preceded by it; `_` never inside a word) and, instead of
//!   its "rule of 3", openers consumed innermost part first (`**` closes only with `**`).
//!
//! Everything else stays literal text: HTML, images, entities, autolinks, setext headings,
//! thematic breaks, reference links, indented code, single-tilde strike. Nested lists and
//! quotes flatten: the indentation (or the inner `>`) goes, the line keeps its list kind.
//! Leading whitespace a reader would drop is kept only where the line starts with text.

use crate::model::{Block, Fmt, LineAttr, Link, Model, BOLD, CELL, CODE, ITALIC, STRIKE};
use crate::render::{is_delimiter_row, needs_gap, Sig};

/// Parse markdown into a document (not yet normalized; see [`crate::model::normalize`]).
pub fn parse(md: &str) -> Model {
    let md = md.replace("\r\n", "\n").replace('\r', "\n");
    let mut model = Model::default();
    if md.is_empty() {
        return model;
    }
    let body = md.strip_suffix('\n').unwrap_or(&md);
    let lines: Vec<&str> = body.split('\n').collect();
    let mut items: Vec<Item> = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        if line.is_empty() {
            items.push(Item::Blank);
            i += 1;
            continue;
        }
        match classify(line) {
            Class::Fence { ch, len, lang } => {
                i += 1;
                let mut first = true;
                while i < lines.len() && !is_fence_close(lines[i], ch, len) {
                    items.push(code_line(
                        &mut model,
                        lines[i],
                        first.then(|| lang.clone()),
                        line,
                    ));
                    first = false;
                    i += 1;
                }
                if i < lines.len() {
                    i += 1; // the closing fence
                }
                if first {
                    items.push(code_line(&mut model, "", Some(lang), line));
                }
            }
            Class::Heading(level, content) => {
                let kind = match level {
                    1 => Block::H1,
                    2 => Block::H2,
                    _ => Block::H3,
                };
                items.push(text_line(&mut model, Some(kind), content, line));
                i += 1;
            }
            Class::Quote(content) => {
                items.push(text_line(&mut model, Some(Block::Quote), content, line));
                i += 1;
            }
            Class::Task(done, content) => {
                let kind = if done { Block::TaskDone } else { Block::Task };
                items.push(text_line(&mut model, Some(kind), content, line));
                i += 1;
            }
            Class::Bullet(content) => {
                items.push(text_line(&mut model, Some(Block::Bullet), content, line));
                i += 1;
            }
            Class::Number(content) => {
                items.push(text_line(&mut model, Some(Block::Number), content, line));
                i += 1;
            }
            Class::Thematic => {
                items.push(text_line(&mut model, None, line, line));
                i += 1;
            }
            Class::Para => {
                let head = i + 1 < lines.len()
                    && crate::render::has_unescaped_pipe(line)
                    && is_delimiter_row(lines[i + 1])
                    && split_row(line).len() == split_row(lines[i + 1]).len();
                if head {
                    let align: String = split_row(lines[i + 1])
                        .iter()
                        .map(|c| match (c.starts_with(':'), c.ends_with(':')) {
                            (true, true) => 'c',
                            (true, false) => 'l',
                            (false, true) => 'r',
                            _ => '-',
                        })
                        .collect();
                    let mut item = table_row(&mut model, line, Block::TableHead);
                    if let Item::Line(d) = &mut item {
                        d.attr.align = Some(align);
                    }
                    items.push(item);
                    i += 2;
                    while i < lines.len() && !lines[i].is_empty() && !starts_block(lines[i]) {
                        items.push(table_row(&mut model, lines[i], Block::Table));
                        i += 1;
                    }
                } else {
                    items.push(text_line(&mut model, None, line, line));
                    i += 1;
                }
            }
        }
    }

    // Blank lines become empty lines, less the one the renderer adds between blocks.
    let mut last: Option<Sig> = None;
    let mut pending = 0usize;
    let mut text: Vec<char> = Vec::new();
    let mut fmt: Vec<Fmt> = Vec::new();
    let mut attrs: Vec<LineAttr> = Vec::new();
    let empty_line =
        |n: usize, text: &mut Vec<char>, fmt: &mut Vec<Fmt>, attrs: &mut Vec<LineAttr>| {
            for _ in 0..n {
                text.push('\n');
                fmt.push(Fmt::default());
                attrs.push(LineAttr::default());
            }
        };
    for item in items {
        match item {
            Item::Blank => pending += 1,
            Item::Line(d) if d.attr.kind.is_none() && d.chars.is_empty() => pending += 1,
            Item::Line(d) => {
                let sig = Sig {
                    kind: d.attr.kind,
                    empty: d.chars.is_empty(),
                    md: d.md.clone(),
                };
                let gap = last.as_ref().is_some_and(|a| needs_gap(a, &sig));
                empty_line(
                    if gap {
                        pending.saturating_sub(1)
                    } else {
                        pending
                    },
                    &mut text,
                    &mut fmt,
                    &mut attrs,
                );
                pending = 0;
                text.extend(&d.chars);
                fmt.extend(&d.fmt);
                text.push('\n');
                fmt.push(Fmt::default());
                attrs.push(d.attr);
                last = Some(Sig {
                    md: d.md_line,
                    ..sig
                });
            }
        }
    }
    empty_line(pending, &mut text, &mut fmt, &mut attrs);
    model.text = text;
    model.fmt = fmt;
    model.lines = attrs;
    model
}

enum Item {
    Blank,
    Line(DocLine),
}

struct DocLine {
    attr: LineAttr,
    chars: Vec<char>,
    fmt: Vec<Fmt>,
    /// The markdown line where this line's block starts (a fence, a table head row).
    md: String,
    /// The markdown line itself.
    md_line: String,
}

fn text_line(model: &mut Model, kind: Option<Block>, content: &str, line: &str) -> Item {
    let src: Vec<char> = content.chars().collect();
    let (chars, fmt) = parse_inline(&src, model);
    Item::Line(DocLine {
        attr: LineAttr {
            kind,
            ..LineAttr::default()
        },
        chars,
        fmt,
        md: line.to_string(),
        md_line: line.to_string(),
    })
}

fn code_line(_model: &mut Model, line: &str, lang: Option<String>, fence: &str) -> Item {
    let chars: Vec<char> = line.chars().collect();
    let fmt = vec![Fmt::default(); chars.len()];
    Item::Line(DocLine {
        attr: LineAttr {
            kind: Some(Block::Code),
            lang: lang.filter(|l| !l.is_empty()),
            align: None,
        },
        chars,
        fmt,
        md: fence.to_string(),
        md_line: line.to_string(),
    })
}

fn table_row(model: &mut Model, line: &str, kind: Block) -> Item {
    let mut chars = Vec::new();
    let mut fmt = Vec::new();
    for (n, cell) in split_row(line).iter().enumerate() {
        if n > 0 {
            chars.push(CELL);
            fmt.push(Fmt::default());
        }
        let src: Vec<char> = cell
            .replace("\\|", "|")
            .chars()
            .filter(|&c| c != CELL)
            .collect();
        let (c, f) = parse_inline(&src, model);
        chars.extend(c);
        fmt.extend(f);
    }
    Item::Line(DocLine {
        attr: LineAttr {
            kind: Some(kind),
            ..LineAttr::default()
        },
        chars,
        fmt,
        md: line.to_string(),
        md_line: line.to_string(),
    })
}

/// A table row's cells, trimmed, still escaped (`\|` is not a separator).
pub(crate) fn split_row(line: &str) -> Vec<String> {
    let t = line.trim_matches([' ', '\t']);
    let t = t.strip_prefix('|').unwrap_or(t);
    let t = match t.strip_suffix('|') {
        Some(s) if !s.ends_with('\\') => s,
        _ => t,
    };
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut prev = '\0';
    for c in t.chars() {
        if c == '|' && prev != '\\' {
            out.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
        prev = c;
    }
    out.push(cur);
    out.into_iter()
        .map(|c| c.trim_matches([' ', '\t']).to_string())
        .collect()
}

enum Class<'a> {
    Fence { ch: char, len: usize, lang: String },
    Heading(usize, &'a str),
    Thematic,
    Quote(&'a str),
    Task(bool, &'a str),
    Bullet(&'a str),
    Number(&'a str),
    Para,
}

/// Leading spaces and tabs: (byte length, width with a tab as 4).
fn indent(line: &str) -> (usize, usize) {
    let n = line.len() - line.trim_start_matches([' ', '\t']).len();
    let w = line[..n]
        .chars()
        .map(|c| if c == '\t' { 4 } else { 1 })
        .sum();
    (n, w)
}

/// Strip one space or tab.
fn one_space(s: &str) -> &str {
    s.strip_prefix([' ', '\t']).unwrap_or(s)
}

fn classify(line: &str) -> Class<'_> {
    let (n, w) = indent(line);
    let rest = &line[n..];
    let small = w <= 3;
    if small {
        if let Some((ch, len, info)) = fence_open(rest) {
            return Class::Fence {
                ch,
                len,
                lang: info.trim().to_string(),
            };
        }
        let hashes = rest.chars().take_while(|&c| c == '#').count();
        if (1..=6).contains(&hashes) {
            let after = &rest[hashes..];
            if after.is_empty() || after.starts_with([' ', '\t']) {
                return Class::Heading(hashes, heading_content(one_space(after)));
            }
        }
        if is_thematic(rest) {
            return Class::Thematic;
        }
        if let Some(q) = rest.strip_prefix('>') {
            let mut content = one_space(q);
            // Nested quotes flatten.
            loop {
                let (n, w) = indent(content);
                match content[n..].strip_prefix('>') {
                    Some(inner) if w <= 3 => content = one_space(inner),
                    _ => break,
                }
            }
            return Class::Quote(content);
        }
    }
    if let Some(m) = list_marker(rest) {
        let marker_end = if m == 0 { 1 } else { m + 1 };
        let content = one_space(&rest[marker_end..]);
        if m == 0 {
            if let Some((done, after)) = task_box(content) {
                return Class::Task(done, after);
            }
            return Class::Bullet(content);
        }
        return Class::Number(content);
    }
    Class::Para
}

/// A list marker at the start of `rest`: `Some(0)` for a bullet (`-`, `*`, `+`), or
/// `Some(digits)` for an ordered marker (the `.` or `)` follows the digits). The marker is
/// followed by a space, a tab or the end of the line.
pub(crate) fn list_marker(rest: &str) -> Option<usize> {
    let mut chars = rest.chars();
    let first = chars.next()?;
    let ends = |s: &str| s.is_empty() || s.starts_with([' ', '\t']);
    if matches!(first, '-' | '*' | '+') && ends(&rest[1..]) {
        return Some(0);
    }
    let digits = rest.chars().take_while(char::is_ascii_digit).count();
    if (1..=9).contains(&digits)
        && rest[digits..].starts_with(['.', ')'])
        && ends(&rest[digits + 1..])
    {
        return Some(digits);
    }
    None
}

/// `[ ]`, `[x]` or `[X]` followed by a space, a tab or the end: (done, the content after it).
pub(crate) fn task_box(content: &str) -> Option<(bool, &str)> {
    let done = if content.starts_with("[ ]") {
        false
    } else if content.starts_with("[x]") || content.starts_with("[X]") {
        true
    } else {
        return None;
    };
    let after = &content[3..];
    (after.is_empty() || after.starts_with([' ', '\t'])).then(|| (done, one_space(after)))
}

/// `---`, `***`, `___` (three or more, spaces allowed between).
pub(crate) fn is_thematic(rest: &str) -> bool {
    let Some(c) = rest.chars().next() else {
        return false;
    };
    matches!(c, '-' | '*' | '_')
        && rest.chars().all(|x| x == c || x == ' ' || x == '\t')
        && rest.chars().filter(|&x| x == c).count() >= 3
}

/// A heading's content without its closing sequence (` ##` at the end).
fn heading_content(s: &str) -> &str {
    let t = s.trim_end_matches([' ', '\t']);
    let j = t.trim_end_matches('#').len();
    if j == t.len() {
        return s;
    }
    if j == 0 {
        return "";
    }
    if t[..j].ends_with([' ', '\t']) {
        t[..j].trim_end_matches([' ', '\t'])
    } else {
        s
    }
}

fn fence_open(rest: &str) -> Option<(char, usize, &str)> {
    let ch = rest.chars().next()?;
    if ch != '`' && ch != '~' {
        return None;
    }
    let len = rest.chars().take_while(|&c| c == ch).count();
    if len < 3 {
        return None;
    }
    let info = &rest[len..];
    if ch == '`' && info.contains('`') {
        return None;
    }
    Some((ch, len, info))
}

fn is_fence_close(line: &str, ch: char, len: usize) -> bool {
    let (n, w) = indent(line);
    let rest = &line[n..];
    let run = rest.chars().take_while(|&c| c == ch).count();
    w <= 3 && run >= len && rest[run..].chars().all(|c| c == ' ' || c == '\t')
}

/// Does this line start a block that ends a table?
fn starts_block(line: &str) -> bool {
    !matches!(classify(line), Class::Para)
}

// ---- inline ----

enum Kind {
    Text,
    Code,
    Delim {
        ch: char,
        can_open: bool,
        can_close: bool,
    },
    Open {
        image: bool,
        active: bool,
        used: bool,
        bottom: usize,
    },
    Close,
}

struct Piece {
    kind: Kind,
    chars: Vec<char>,
    /// Delimiter characters left (a delimiter run).
    count: usize,
}

enum Mark {
    Flag(u8),
    Link(Link),
}

fn text(pieces: &mut Vec<Piece>, c: char) {
    if let Some(Piece {
        kind: Kind::Text,
        chars,
        ..
    }) = pieces.last_mut()
    {
        chars.push(c);
    } else {
        pieces.push(Piece {
            kind: Kind::Text,
            chars: vec![c],
            count: 0,
        });
    }
}

fn is_punct(c: char) -> bool {
    !c.is_alphanumeric() && !c.is_whitespace()
}

/// Inline markdown (one line or cell, unescaped `\|` already) to text and formats.
pub(crate) fn parse_inline(src: &[char], model: &mut Model) -> (Vec<char>, Vec<Fmt>) {
    let n = src.len();
    let mut pieces: Vec<Piece> = Vec::new();
    let mut delims: Vec<usize> = Vec::new();
    let mut brackets: Vec<usize> = Vec::new();
    let mut matches: Vec<(usize, usize, Mark)> = Vec::new();
    let mut i = 0;
    while i < n {
        let c = src[i];
        match c {
            '\\' if i + 1 < n && src[i + 1].is_ascii_punctuation() => {
                text(&mut pieces, src[i + 1]);
                i += 2;
            }
            '`' => {
                let run = src[i..].iter().take_while(|&&x| x == '`').count();
                match backtick_closer(src, i + run, run) {
                    Some(close) => {
                        let mut content = src[i + run..close].to_vec();
                        if content.len() >= 2
                            && content[0] == ' '
                            && content[content.len() - 1] == ' '
                            && !content.iter().all(|&x| x == ' ')
                        {
                            content.remove(0);
                            content.pop();
                        }
                        pieces.push(Piece {
                            kind: Kind::Code,
                            chars: content,
                            count: 0,
                        });
                        i = close + run;
                    }
                    None => {
                        for _ in 0..run {
                            text(&mut pieces, '`');
                        }
                        i += run;
                    }
                }
            }
            '*' | '_' | '~' => {
                let run = src[i..].iter().take_while(|&&x| x == c).count();
                let prev = i.checked_sub(1).map(|j| src[j]);
                let next = src.get(i + run).copied();
                let left = next.is_some_and(|x| !x.is_whitespace());
                let right = prev.is_some_and(|x| !x.is_whitespace());
                let (can_open, can_close) = match c {
                    '*' => (left, right),
                    '_' => (
                        left && (!right || prev.is_some_and(is_punct)),
                        right && (!left || next.is_some_and(is_punct)),
                    ),
                    _ if run == 2 => (left, right),
                    _ => (false, false),
                };
                if can_open || can_close {
                    delims.push(pieces.len());
                    pieces.push(Piece {
                        kind: Kind::Delim {
                            ch: c,
                            can_open,
                            can_close,
                        },
                        chars: Vec::new(),
                        count: run,
                    });
                } else {
                    for _ in 0..run {
                        text(&mut pieces, c);
                    }
                }
                i += run;
            }
            '[' | '!' if c == '[' || src.get(i + 1) == Some(&'[') => {
                brackets.push(pieces.len());
                pieces.push(Piece {
                    kind: Kind::Open {
                        image: c == '!',
                        active: true,
                        used: false,
                        bottom: delims.len(),
                    },
                    chars: Vec::new(),
                    count: 0,
                });
                i += if c == '!' { 2 } else { 1 };
            }
            ']' => {
                i += 1;
                let Some(&o) = brackets.last() else {
                    text(&mut pieces, ']');
                    continue;
                };
                let (image, active, bottom) = match pieces[o].kind {
                    Kind::Open {
                        image,
                        active,
                        bottom,
                        ..
                    } => (image, active, bottom),
                    _ => unreachable!("brackets hold openers"),
                };
                brackets.pop();
                let tail = if active && !image {
                    link_tail(src, i)
                } else {
                    None
                };
                let Some((url, next)) = tail else {
                    text(&mut pieces, ']');
                    continue;
                };
                let close = pieces.len();
                pieces.push(Piece {
                    kind: Kind::Close,
                    chars: Vec::new(),
                    count: 0,
                });
                if let Kind::Open { used, .. } = &mut pieces[o].kind {
                    *used = true;
                }
                let link = model.intern_url(&url);
                matches.push((o, close, Mark::Link(link)));
                process_emphasis(&mut pieces, &mut delims, bottom, &mut matches);
                // No links in links: earlier openers become text.
                for &b in &brackets {
                    if let Kind::Open {
                        image: false,
                        active,
                        ..
                    } = &mut pieces[b].kind
                    {
                        *active = false;
                    }
                }
                i = next;
            }
            _ => {
                text(&mut pieces, c);
                i += 1;
            }
        }
    }
    process_emphasis(&mut pieces, &mut delims, 0, &mut matches);

    let mut chars = Vec::new();
    let mut fmt = Vec::new();
    let mut starts = vec![0; pieces.len()];
    let mut ends = vec![0; pieces.len()];
    for (k, p) in pieces.iter().enumerate() {
        starts[k] = chars.len();
        let (lit, flags): (Vec<char>, u8) = match &p.kind {
            Kind::Text => (p.chars.clone(), 0),
            Kind::Code => (p.chars.clone(), CODE),
            Kind::Delim { ch, .. } => (vec![*ch; p.count], 0),
            Kind::Open { used: true, .. } | Kind::Close => (Vec::new(), 0),
            Kind::Open { image: true, .. } => (vec!['!', '['], 0),
            Kind::Open { .. } => (vec!['['], 0),
        };
        for c in lit {
            chars.push(c);
            fmt.push(Fmt {
                flags,
                link: Link::None,
            });
        }
        ends[k] = chars.len();
    }
    for (o, c, mark) in matches {
        for f in &mut fmt[ends[o]..starts[c]] {
            match mark {
                Mark::Flag(flag) => f.set(flag, true),
                Mark::Link(link) => f.link = link,
            }
        }
    }
    (chars, fmt)
}

/// The start of the next backtick run of exactly `run` from `from`.
fn backtick_closer(src: &[char], from: usize, run: usize) -> Option<usize> {
    let mut i = from;
    while i < src.len() {
        if src[i] == '`' {
            let len = src[i..].iter().take_while(|&&x| x == '`').count();
            if len == run {
                return Some(i);
            }
            i += len;
        } else {
            i += 1;
        }
    }
    None
}

/// CommonMark's `process_emphasis` over the delimiters above `bottom`.
fn process_emphasis(
    pieces: &mut [Piece],
    delims: &mut Vec<usize>,
    bottom: usize,
    matches: &mut Vec<(usize, usize, Mark)>,
) {
    let delim = |p: &Piece| match p.kind {
        Kind::Delim {
            ch,
            can_open,
            can_close,
        } => (ch, can_open, can_close),
        _ => unreachable!("the delimiter stack holds delimiters"),
    };
    let mut ci = bottom;
    while ci < delims.len() {
        let c = delims[ci];
        let (ch, c_open, c_close) = delim(&pieces[c]);
        if !c_close {
            ci += 1;
            continue;
        }
        let mut found = None;
        let mut oi = ci;
        while oi > bottom {
            oi -= 1;
            let o = delims[oi];
            let (och, o_open, _) = delim(&pieces[o]);
            if och != ch || !o_open || pieces[o].count == 0 {
                continue;
            }
            // Instead of CommonMark's "rule of 3" (which refuses some of the renderer's runs,
            // `**a*b****c*`): an opener's innermost part is taken whole. A run of 3 opens bold
            // then italic, so italic (1) is innermost; a run of 2 is bold and needs 2.
            let want = match (ch, pieces[o].count) {
                ('~', _) | (_, 2) => 2,
                _ => 1,
            };
            if pieces[c].count < want {
                continue;
            }
            found = Some((oi, want));
            break;
        }
        match found {
            Some((oi, used)) => {
                let o = delims[oi];
                let flag = match (ch, used) {
                    ('~', _) => STRIKE,
                    (_, 2) => BOLD,
                    _ => ITALIC,
                };
                matches.push((o, c, Mark::Flag(flag)));
                pieces[o].count -= used;
                pieces[c].count -= used;
                delims.drain(oi + 1..ci);
                ci = oi + 1;
                if pieces[o].count == 0 {
                    delims.remove(oi);
                    ci -= 1;
                }
                if pieces[c].count == 0 {
                    delims.remove(ci);
                }
            }
            None => {
                if c_open {
                    ci += 1;
                } else {
                    delims.remove(ci);
                }
            }
        }
    }
    delims.truncate(bottom);
}

/// `(dest "title")` after a `]`: the destination and the index after `)`.
fn link_tail(src: &[char], i: usize) -> Option<(String, usize)> {
    if src.get(i) != Some(&'(') {
        return None;
    }
    let mut j = i + 1;
    let skip_ws = |j: &mut usize| {
        while src.get(*j).is_some_and(|c| c.is_whitespace()) {
            *j += 1;
        }
    };
    skip_ws(&mut j);
    let mut url = String::new();
    if src.get(j) == Some(&'<') {
        j += 1;
        loop {
            match src.get(j)? {
                '>' => {
                    j += 1;
                    break;
                }
                '<' => return None,
                '\\' if src.get(j + 1).is_some_and(char::is_ascii_punctuation) => {
                    url.push(src[j + 1]);
                    j += 2;
                }
                &c => {
                    url.push(c);
                    j += 1;
                }
            }
        }
    } else {
        let mut depth = 0usize;
        while let Some(&c) = src.get(j) {
            match c {
                '\\' if src.get(j + 1).is_some_and(char::is_ascii_punctuation) => {
                    url.push(src[j + 1]);
                    j += 2;
                    continue;
                }
                c if c.is_whitespace() || c.is_control() => break,
                '(' => depth += 1,
                ')' if depth == 0 => break,
                ')' => depth -= 1,
                _ => {}
            }
            url.push(c);
            j += 1;
        }
        if depth != 0 {
            return None;
        }
    }
    let before_title = j;
    skip_ws(&mut j);
    if let Some(&q) = src.get(j).filter(|&&q| matches!(q, '"' | '\'' | '(')) {
        if j == before_title {
            return None; // a title needs whitespace before it
        }
        let close = if q == '(' { ')' } else { q };
        j += 1;
        loop {
            match src.get(j)? {
                '\\' => j += 2,
                &c if c == close => {
                    j += 1;
                    break;
                }
                _ => j += 1,
            }
        }
        skip_ws(&mut j);
    }
    (src.get(j) == Some(&')')).then_some((url, j + 1))
}
