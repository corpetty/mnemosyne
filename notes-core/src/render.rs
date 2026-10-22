//! The document as canonical markdown. Deterministic: equal documents (after
//! [`normalize`]) give byte-equal markdown, and [`crate::parse`] reads it back to the same
//! document.
//!
//! One markdown line per document line, except that consecutive code lines become one fence
//! and a table gets its separator row after the head. A blank line is added between two
//! blocks a markdown reader would otherwise join ([`needs_gap`]): the parser drops exactly
//! that one again.

use crate::model::{normalize, Block, Fmt, Link, Model, BOLD, CELL, CODE, ITALIC, STRIKE};

/// What a fold or set_text reports: the markdown (what `Session.notes` holds) and the plain
/// text (search). See [`plain`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rendered {
    pub markdown: String,
    pub plain: String,
}

/// Render a document.
pub fn render(model: &Model) -> Rendered {
    Rendered {
        markdown: markdown(model),
        plain: plain(&model.text),
    }
}

/// The plain text: the document's text with each cell separator (U+001F) turned into a tab.
/// One character for one, so a position in `plain` is the same position in the document
/// (comment ranges, client offsets), and search sees the cells as separate words.
pub fn plain(text: &[char]) -> String {
    text.iter()
        .map(|&c| if c == CELL { '\t' } else { c })
        .collect()
}

/// What the gap rule looks at: a non-empty line's kind, whether its text is empty, and the
/// markdown line where it starts (a code run's fence, a table's head row).
#[derive(Debug, Clone)]
pub(crate) struct Sig {
    pub kind: Option<Block>,
    pub empty: bool,
    pub md: String,
}

/// Would markdown join `b` into `a` (or misread either) without a blank line between them?
/// List, quote and table lines take a following paragraph line as a continuation; a table
/// runs into the next table; a paragraph absorbs a table head, turns an empty list item
/// into a setext underline, and becomes a table head over a line that looks like a
/// separator row.
pub(crate) fn needs_gap(a: &Sig, b: &Sig) -> bool {
    let continues =
        |k: Option<Block>| k.is_some_and(|k| k.is_list() || k.is_table() || k == Block::Quote);
    match (a.kind, b.kind) {
        (ak, None) if continues(ak) => true,
        (Some(ak), Some(Block::TableHead)) if ak.is_table() => true,
        (None, Some(Block::TableHead)) => true,
        (None, Some(Block::Bullet | Block::Number)) if b.empty => true,
        (None, _) => has_unescaped_pipe(&a.md) && is_delimiter_row(&b.md),
        _ => false,
    }
}

/// A `|` not escaped by a backslash.
pub(crate) fn has_unescaped_pipe(line: &str) -> bool {
    let chars: Vec<char> = line.chars().collect();
    chars
        .iter()
        .enumerate()
        .any(|(i, &c)| c == '|' && (i == 0 || chars[i - 1] != '\\'))
}

/// A GFM table separator row: cells of `:?-+:?` between pipes (at least one pipe).
pub(crate) fn is_delimiter_row(line: &str) -> bool {
    if !line.contains('|') {
        return false;
    }
    let cells = crate::parse::split_row(line);
    !cells.is_empty()
        && cells.iter().all(|c| {
            let c = c.strip_prefix(':').unwrap_or(c);
            let c = c.strip_suffix(':').unwrap_or(c);
            !c.is_empty() && c.chars().all(|x| x == '-')
        })
}

fn markdown(model: &Model) -> String {
    let m = normalize(model);
    let spans = m.line_spans();
    let mut out: Vec<String> = Vec::new();
    let mut last: Option<Sig> = None;
    let mut pending = 0usize;
    let mut number = 0usize;
    let mut li = 0;
    while li < spans.len() {
        let (s, e, _) = spans[li];
        let kind = m.lines[li].kind;
        if kind.is_none() && s == e {
            pending += 1;
            number = 0;
            li += 1;
            continue;
        }
        // Render this line (or run) first: the gap depends on its first markdown line.
        let mut block: Vec<String> = Vec::new();
        let first_kind;
        let last_sig;
        match kind {
            Some(Block::Code) => {
                let start = li;
                while li < spans.len() && m.lines[li].kind == Some(Block::Code) {
                    li += 1;
                }
                let texts: Vec<String> = (start..li)
                    .map(|l| m.text[spans[l].0..spans[l].1].iter().collect())
                    .collect();
                let lang = m.lines[start].lang.clone().unwrap_or_default();
                let fence_char = if lang.contains('`') { '~' } else { '`' };
                let longest = texts
                    .iter()
                    .map(|t| {
                        t.trim_start_matches([' ', '\t'])
                            .chars()
                            .take_while(|&c| c == fence_char)
                            .count()
                    })
                    .max()
                    .unwrap_or(0);
                let fence: String = std::iter::repeat_n(fence_char, (longest + 1).max(3)).collect();
                block.push(format!("{fence}{lang}"));
                block.extend(texts);
                block.push(fence);
                first_kind = Some(Block::Code);
                last_sig = Sig {
                    kind: Some(Block::Code),
                    empty: false,
                    md: String::new(),
                };
                number = 0;
            }
            Some(Block::Table | Block::TableHead) => {
                let start = li;
                li += 1;
                while li < spans.len() && m.lines[li].kind == Some(Block::Table) {
                    li += 1;
                }
                let rows: Vec<Vec<(usize, usize)>> = (start..li)
                    .map(|l| cells(&m.text, spans[l].0, spans[l].1))
                    .collect();
                let cols = rows.iter().map(Vec::len).max().unwrap_or(1);
                let row_md = |cells: &[(usize, usize)]| {
                    let mut parts: Vec<String> =
                        cells.iter().map(|&(a, b)| inline(&m, a, b, true)).collect();
                    parts.resize(cols, String::new());
                    format!("| {} |", parts.join(" | "))
                };
                block.push(row_md(&rows[0]));
                let align: Vec<char> = m.lines[start]
                    .align
                    .as_deref()
                    .unwrap_or_default()
                    .chars()
                    .collect();
                let sep: Vec<&str> = (0..cols)
                    .map(|c| match align.get(c) {
                        Some('l') => ":---",
                        Some('c') => ":---:",
                        Some('r') => "---:",
                        _ => "---",
                    })
                    .collect();
                block.push(format!("| {} |", sep.join(" | ")));
                for row in &rows[1..] {
                    block.push(row_md(row));
                }
                first_kind = Some(Block::TableHead);
                last_sig = Sig {
                    kind: Some(Block::Table),
                    empty: false,
                    md: String::new(),
                };
                number = 0;
            }
            _ => {
                let content = inline(&m, s, e, false);
                let line = match kind {
                    None => escape_line_start(&content, false),
                    Some(Block::H1) => prefixed("#", &escape_heading_end(&content)),
                    Some(Block::H2) => prefixed("##", &escape_heading_end(&content)),
                    Some(Block::H3) => prefixed("###", &escape_heading_end(&content)),
                    Some(Block::Bullet) => prefixed("-", &escape_line_start(&content, true)),
                    Some(Block::Number) => {
                        number += 1;
                        prefixed(&format!("{number}."), &escape_line_start(&content, true))
                    }
                    Some(Block::Task) => prefixed("- [ ]", &escape_line_start(&content, false)),
                    Some(Block::TaskDone) => prefixed("- [x]", &escape_line_start(&content, false)),
                    Some(Block::Quote) => prefixed(">", &escape_line_start(&content, false)),
                    Some(Block::Code | Block::Table | Block::TableHead) => unreachable!(),
                };
                if kind != Some(Block::Number) {
                    number = 0;
                }
                first_kind = kind;
                last_sig = Sig {
                    kind,
                    empty: s == e,
                    md: line.clone(),
                };
                block.push(line);
                li += 1;
            }
        }
        let first = Sig {
            kind: first_kind,
            empty: last_sig.empty,
            md: block[0].clone(),
        };
        let gap = last.as_ref().is_some_and(|a| needs_gap(a, &first));
        for _ in 0..pending + usize::from(gap) {
            out.push(String::new());
        }
        pending = 0;
        out.extend(block);
        last = Some(last_sig);
    }
    for _ in 0..pending {
        out.push(String::new());
    }
    let mut md = String::new();
    for line in out {
        md.push_str(&line);
        md.push('\n');
    }
    md
}

fn prefixed(prefix: &str, content: &str) -> String {
    if content.is_empty() {
        prefix.to_string()
    } else {
        format!("{prefix} {content}")
    }
}

/// The cells of a table line `[s, e)`: ranges between separators.
fn cells(text: &[char], s: usize, e: usize) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut a = s;
    for (i, &c) in text.iter().enumerate().take(e).skip(s) {
        if c == CELL {
            out.push((a, i));
            a = i + 1;
        }
    }
    out.push((a, e));
    out
}

/// One delimiter level of the fixed nesting: link outermost, then bold, italic, strike.
/// Code is innermost and rendered per run as a whole span.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Tok {
    Link(Link),
    Bold,
    Italic,
    Strike,
}

fn toks(f: &Fmt) -> Vec<Tok> {
    let mut out = Vec::new();
    if f.link != Link::None {
        out.push(Tok::Link(f.link));
    }
    for (flag, tok) in [
        (BOLD, Tok::Bold),
        (ITALIC, Tok::Italic),
        (STRIKE, Tok::Strike),
    ] {
        if f.has(flag) {
            out.push(tok);
        }
    }
    out
}

/// Inline markdown for the characters `[s, e)` of a line or a cell.
fn inline(m: &Model, s: usize, e: usize, in_cell: bool) -> String {
    let mut runs: Vec<(usize, usize)> = Vec::new();
    let mut a = s;
    for i in s + 1..=e {
        if i == e || m.fmt[i] != m.fmt[a] {
            if a < i {
                runs.push((a, i));
            }
            a = i;
        }
    }
    let mut out = String::new();
    let mut stack: Vec<Tok> = Vec::new();
    for (r, &(a, b)) in runs.iter().enumerate() {
        let f = m.fmt[a];
        let want = toks(&f);
        let keep = stack.iter().zip(&want).take_while(|(x, y)| x == y).count();
        while stack.len() > keep {
            let tok = stack.pop().expect("len > keep");
            close(m, tok, in_cell, &mut out);
        }
        for &tok in &want[keep..] {
            open(tok, &mut out);
            stack.push(tok);
        }
        let chars = &m.text[a..b];
        if f.has(CODE) {
            code_span(chars, in_cell, &mut out);
            continue;
        }
        let in_link = matches!(f.link, Link::Url(_));
        // Does the next run open a link right after this one? Then a final `!` would make it
        // an image.
        let link_next = runs.get(r + 1).is_some_and(|&(na, _)| {
            let nf = m.fmt[na];
            matches!(nf.link, Link::Url(_)) && nf.link != f.link
        });
        // A `~` at a run edge would join a strike delimiter there.
        let strike = |i: Option<usize>| i.is_some_and(|i| m.fmt[runs[i].0].has(STRIKE));
        let edges = Edges {
            in_link,
            in_cell,
            link_next,
            strike_before: f.has(STRIKE) || strike(r.checked_sub(1)),
            strike_after: f.has(STRIKE) || strike(Some(r + 1).filter(|&n| n < runs.len())),
        };
        for k in 0..chars.len() {
            escape_char(chars, k, &edges, &mut out);
        }
    }
    while let Some(tok) = stack.pop() {
        close(m, tok, in_cell, &mut out);
    }
    out
}

fn open(tok: Tok, out: &mut String) {
    out.push_str(match tok {
        Tok::Link(Link::Url(_)) => "[",
        Tok::Link(_) => "",
        Tok::Bold => "**",
        Tok::Italic => "*",
        Tok::Strike => "~~",
    });
}

fn close(m: &Model, tok: Tok, in_cell: bool, out: &mut String) {
    match tok {
        Tok::Link(Link::Url(i)) => {
            out.push_str("](");
            out.push_str(&url(
                m.urls.get(i as usize).map_or("", String::as_str),
                in_cell,
            ));
            out.push(')');
        }
        Tok::Link(_) => out.push_str(" (link conflict)"),
        Tok::Bold => out.push_str("**"),
        Tok::Italic => out.push('*'),
        Tok::Strike => out.push_str("~~"),
    }
}

/// A link destination: `<...>` when it holds whitespace, else bare; backslash escapes where
/// either form needs them.
fn url(u: &str, in_cell: bool) -> String {
    let angle = u.chars().any(|c| c.is_whitespace() || c.is_control());
    let mut out = String::new();
    if angle {
        out.push('<');
    }
    for c in u.chars() {
        let c = if c.is_control() { ' ' } else { c };
        let esc = match c {
            '\\' => true,
            '<' | '>' => true,
            '(' | ')' => !angle,
            '|' => in_cell,
            _ => false,
        };
        if esc {
            out.push('\\');
        }
        out.push(c);
    }
    if angle {
        out.push('>');
    }
    out
}

fn code_span(chars: &[char], in_cell: bool, out: &mut String) {
    let mut longest = 0;
    let mut run = 0;
    for &c in chars {
        if c == '`' {
            run += 1;
            longest = longest.max(run);
        } else {
            run = 0;
        }
    }
    let fence: String = std::iter::repeat_n('`', longest + 1).collect();
    let first = chars[0];
    let last = chars[chars.len() - 1];
    let pad = first == '`'
        || last == '`'
        || (first == ' ' && last == ' ' && !chars.iter().all(|&c| c == ' '));
    out.push_str(&fence);
    if pad {
        out.push(' ');
    }
    for &c in chars {
        if c == '|' && in_cell {
            out.push('\\');
        }
        out.push(c);
    }
    if pad {
        out.push(' ');
    }
    out.push_str(&fence);
}

/// Text character `k` of a run: escaped only where the parser (and a CommonMark reader)
/// would otherwise read it as markup. A run's edges are next to delimiters, so the checks
/// treat them as unknown neighbours.
struct Edges {
    in_link: bool,
    in_cell: bool,
    /// The next run opens a link.
    link_next: bool,
    /// A strike delimiter may come right before / after this run.
    strike_before: bool,
    strike_after: bool,
}

fn escape_char(chars: &[char], k: usize, edges: &Edges, out: &mut String) {
    let Edges {
        in_link,
        in_cell,
        link_next,
        strike_before,
        strike_after,
    } = *edges;
    let c = chars[k];
    let prev = k.checked_sub(1).map(|j| chars[j]);
    let next = chars.get(k + 1).copied();
    let ws = |x: Option<char>| x.is_some_and(char::is_whitespace);
    let alnum = |x: Option<char>| x.is_some_and(char::is_alphanumeric);
    let esc = match c {
        '\\' => next.is_none_or(|n| n.is_ascii_punctuation()),
        '`' => true,
        '*' => !(ws(prev) && ws(next)),
        '_' => !((alnum(prev) && alnum(next)) || (ws(prev) && ws(next))),
        '~' => {
            (prev.is_none() && strike_before)
                || (next.is_none() && strike_after)
                || prev == Some('~')
                || next == Some('~')
        }
        '[' => in_link,
        ']' => in_link || next == Some('('),
        '!' => next.is_none() && link_next,
        '<' => next.is_some_and(|n| n.is_ascii_alphabetic() || matches!(n, '/' | '!' | '?')),
        '&' => entity_follows(&chars[k + 1..]),
        '|' => in_cell,
        _ => false,
    };
    if esc {
        out.push('\\');
    }
    out.push(c);
}

/// `name;`, `#123;` or `#x1F;` (an HTML entity a markdown reader would decode).
fn entity_follows(rest: &[char]) -> bool {
    let body: Vec<char> = rest.iter().copied().take_while(|&c| c != ';').collect();
    if body.len() == rest.len() || body.is_empty() {
        return false;
    }
    if let Some(num) = body.strip_prefix(&['#']) {
        if let Some(hex) = num
            .strip_prefix(&['x'])
            .or_else(|| num.strip_prefix(&['X']))
        {
            return !hex.is_empty() && hex.iter().all(char::is_ascii_hexdigit);
        }
        return !num.is_empty() && num.iter().all(char::is_ascii_digit);
    }
    body.iter().all(char::is_ascii_alphanumeric)
}

/// Escape the start of a line's content where it would open a block: list markers (at any
/// indentation, since the parser flattens nested lists), and with at most three spaces of
/// indentation headings, quotes, thematic breaks, setext underlines, table separator rows,
/// link reference definitions and fences; with `task`, a `[ ]` task box too (list content).
pub(crate) fn escape_line_start(s: &str, task: bool) -> String {
    let ws_len = s.len() - s.trim_start_matches([' ', '\t']).len();
    let width: usize = s[..ws_len]
        .chars()
        .map(|c| if c == '\t' { 4 } else { 1 })
        .sum();
    let rest = &s[ws_len..];
    let small = width <= 3;
    let at = if rest.is_empty() {
        None
    } else if let Some(n) = crate::parse::list_marker(rest) {
        Some(ws_len + n)
    } else if small && block_start(rest, task) {
        Some(ws_len)
    } else {
        None
    };
    match at {
        Some(i) => format!("{}\\{}", &s[..i], &s[i..]),
        None => s.to_string(),
    }
}

/// Would `rest` (no indentation) open a block other than a list?
fn block_start(rest: &str, task: bool) -> bool {
    let first = rest.chars().next().unwrap_or(' ');
    let hashes = rest.chars().take_while(|&c| c == '#').count();
    let after_hashes = rest[hashes..].chars().next();
    (1..=6).contains(&hashes) && after_hashes.is_none_or(|c| c == ' ' || c == '\t')
        || first == '>'
        || crate::parse::is_thematic(rest)
        || (first == '=' && rest.trim_end_matches([' ', '\t']).chars().all(|c| c == '='))
        || is_delimiter_row(rest)
        || (first == '[' && is_reference_definition(rest))
        || rest.starts_with("```")
        || rest.starts_with("~~~")
        || (task && crate::parse::task_box(rest).is_some())
}

fn is_reference_definition(rest: &str) -> bool {
    let mut esc = false;
    for (i, c) in rest.char_indices().skip(1) {
        match c {
            _ if esc => esc = false,
            '\\' => esc = true,
            ']' => return rest[i + 1..].starts_with(':'),
            _ => {}
        }
    }
    false
}

/// A heading's content would lose a trailing ` #` run (read as a closing sequence).
fn escape_heading_end(s: &str) -> String {
    let t = s.trim_end_matches([' ', '\t']);
    if !t.ends_with('#') {
        return s.to_string();
    }
    let j = t.trim_end_matches('#').len();
    if j == 0 || t[..j].ends_with([' ', '\t']) {
        format!("{}\\{}", &s[..j], &s[j..])
    } else {
        s.to_string()
    }
}
