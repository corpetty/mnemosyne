//! The document as the browser's `NotesModel` (src/lib/notes/commands.ts): what pasted
//! markdown becomes (`parseMarkdown`) and what a selection's markdown is made from
//! (`renderMarkdown`). JSON, because that is what crosses the wasm boundary:
//!
//! ```json
//! {"text": "...", "lines": [{"kind": "h1"|...|null, "lang": "py", "align": ["left", null]}],
//!  "inline": [{"from": 0, "to": 3, "kind": "bold", "value": "true"}]}
//! ```
//!
//! - `text` has U+001F between table cells and every line ends in `\n`; `lines` has one entry
//!   per `\n`; offsets are code points. `lang` and `align` are left out when absent; `align`
//!   is one of `left`, `center`, `right` or `null` per column (the model's `l c r -`).
//! - `inline` kinds are `bold`, `italic`, `strike`, `code` (value `"true"`) and `link` (value
//!   the URL). A model going the other way may leave a flag's value out and may carry
//!   `comment:<id>` kinds (ignored: comments are not markdown). Where two links overlap,
//!   the later one wins.

use serde::{Deserialize, Serialize};

use crate::model::{normalize, Block, Fmt, LineAttr, Model, BOLD, CODE, ITALIC, STRIKE};
use crate::{render, Error, COMMENT_PREFIX, FLAG_VALUE};

#[derive(Serialize, Deserialize)]
struct Wire {
    text: String,
    #[serde(default)]
    lines: Vec<WireLine>,
    #[serde(default)]
    inline: Vec<WireMark>,
}

#[derive(Serialize, Deserialize, Default)]
struct WireLine {
    #[serde(default)]
    kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    lang: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    align: Option<Vec<Option<String>>>,
}

#[derive(Serialize, Deserialize)]
struct WireMark {
    from: usize,
    to: usize,
    kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    value: Option<String>,
}

fn align_name(c: char) -> Option<String> {
    match c {
        'l' => Some("left".into()),
        'c' => Some("center".into()),
        'r' => Some("right".into()),
        _ => None,
    }
}

fn align_char(name: Option<&str>) -> char {
    match name {
        Some("left") => 'l',
        Some("center") => 'c',
        Some("right") => 'r',
        _ => '-',
    }
}

/// A document as the client's model, JSON.
pub fn to_json(model: &Model) -> String {
    let parsed = model.to_parsed();
    let wire = Wire {
        text: parsed.text,
        lines: parsed
            .lines
            .iter()
            .map(|l| WireLine {
                kind: l.kind.map(|k| k.as_str().to_string()),
                lang: l.lang.clone(),
                align: l
                    .align
                    .as_ref()
                    .map(|a| a.chars().map(align_name).collect()),
            })
            .collect(),
        inline: parsed
            .inline
            .into_iter()
            .map(|r| WireMark {
                from: r.start,
                to: r.end,
                kind: r.kind.to_string(),
                value: Some(r.value.unwrap_or_else(|| FLAG_VALUE.to_string())),
            })
            .collect(),
    };
    serde_json::to_string(&wire).expect("a model is always serializable")
}

/// A document from the client's model (JSON). Not normalized: [`render_json`] does that.
pub fn from_json(json: &str) -> Result<Model, Error> {
    let wire: Wire =
        serde_json::from_str(json).map_err(|e| Error(format!("invalid model: {e}")))?;
    let text: Vec<char> = wire.text.chars().collect();
    let mut model = Model {
        fmt: vec![Fmt::default(); text.len()],
        text,
        ..Model::default()
    };
    for (i, l) in wire.lines.iter().enumerate() {
        let kind = match l.kind.as_deref() {
            None => None,
            Some(k) => Some(
                Block::parse(k).ok_or_else(|| Error(format!("lines[{i}]: unknown kind {k:?}")))?,
            ),
        };
        let align = l.align.as_ref().map(|a| {
            a.iter()
                .map(|n| align_char(n.as_deref()))
                .collect::<String>()
        });
        model.lines.push(LineAttr {
            kind,
            lang: l.lang.clone(),
            align,
        });
    }
    for (i, m) in wire.inline.iter().enumerate() {
        if m.from > m.to || m.to > model.text.len() {
            return Err(Error(format!(
                "inline[{i}]: range {}..{} outside the text ({} characters)",
                m.from,
                m.to,
                model.text.len()
            )));
        }
        let flag = match m.kind.as_str() {
            "bold" => BOLD,
            "italic" => ITALIC,
            "strike" => STRIKE,
            "code" => CODE,
            "link" => {
                let Some(url) = m.value.as_deref() else {
                    return Err(Error(format!("inline[{i}]: a link needs a url")));
                };
                let link = model.intern_url(url);
                for f in &mut model.fmt[m.from..m.to] {
                    f.link = link;
                }
                continue;
            }
            k if k.starts_with(COMMENT_PREFIX) => continue,
            k => return Err(Error(format!("inline[{i}]: unknown kind {k:?}"))),
        };
        for f in &mut model.fmt[m.from..m.to] {
            f.set(flag, true);
        }
    }
    Ok(model)
}

/// Markdown to the client's model: parse, normalize, JSON.
pub fn parse_markdown(markdown: &str) -> String {
    to_json(&crate::parse(markdown))
}

/// The client's model to canonical markdown.
pub fn render_json(json: &str) -> Result<String, Error> {
    let model = from_json(json)?;
    Ok(render(&normalize(&model)).markdown)
}
