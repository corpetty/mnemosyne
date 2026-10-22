//! The client wire form (`notes_core::client`): shape, and markdown through it and back.

mod common;

use common::Rng;
use notes_core::client::{from_json, parse_markdown, render_json, to_json};
use notes_core::model::{same, CELL};
use notes_core::{normalize, parse, render};
use serde_json::{json, Value};

const EVERYTHING: &str = "# Title\n\
## Sub\n\
### Third\n\
\n\
Plain with **bold**, *italic*, ~~gone~~, `code` and [a link](https://example.com/a/very/long/url?q=1).\n\
**bold *and italic*** and ***both***\n\
\n\
- bullet\n\
1. numbered\n\
- [ ] open task\n\
- [x] done task\n\
\n\
> quoted\n\
\n\
```py\n\
print(\"hi\")\n\
\n\
x = 1\n\
```\n\
\n\
| Left | Center | Right | None |\n\
|:-----|:------:|------:|------|\n\
| a | **b** | `c` | [d](https://e.example) |\n\
| ünï | 日本語 | 😀 | |\n\
\n\
Last line, with émoji 😀 and **bold 😀**.\n";

fn value(json: &str) -> Value {
    serde_json::from_str(json).unwrap()
}

#[test]
fn shape_is_the_clients_notes_model() {
    let v = value(&parse_markdown(
        "# t\n```py\nx\n```\n| a | b |\n|:-:|--:|\n| **c** | [d](https://x.y) |\n",
    ));
    let lines = v["lines"].as_array().unwrap();
    assert_eq!(
        v["text"].as_str().unwrap().matches('\n').count(),
        lines.len()
    );
    assert_eq!(lines[0], json!({"kind": "h1"}));
    // Plain lines name no lang or align; code carries its language on the first line only.
    assert_eq!(lines[1], json!({"kind": "code", "lang": "py"}));
    // Table head: align as names (null = default), rows after it none.
    assert_eq!(
        lines[2],
        json!({"kind": "table_head", "align": ["center", "right"]})
    );
    assert_eq!(lines[3], json!({"kind": "table"}));
    let inline = v["inline"].as_array().unwrap();
    assert!(inline.contains(&json!({"from": 8, "to": 9, "kind": "bold", "value": "true"})));
    let link = inline.iter().find(|m| m["kind"] == "link").unwrap();
    assert_eq!(link["value"], "https://x.y");
    // A paragraph has a null kind, as `LineKind | null`.
    let v = value(&parse_markdown("plain\n"));
    assert_eq!(v["lines"][0], json!({"kind": null}));
    assert_eq!(v["inline"], json!([]));
}

#[test]
fn table_text_uses_the_cell_separator_and_code_point_offsets() {
    let v = value(&parse_markdown("| a | b |\n|---|---|\n| 😀 | **c** |\n"));
    let text = v["text"].as_str().unwrap();
    assert_eq!(text, format!("a{CELL}b\n😀{CELL}c\n"));
    // "c" is the 6th..7th code point after the emoji (a, SEP, b, \n, 😀, SEP, c).
    assert_eq!(
        v["inline"],
        json!([{"from": 6, "to": 7, "kind": "bold", "value": "true"}])
    );
}

#[test]
fn everything_round_trips_through_the_wire() {
    let model = parse(EVERYTHING);
    let again = from_json(&to_json(&model)).unwrap();
    assert!(same(&normalize(&again), &model), "wire changed the model");
    let md = render(&model).markdown;
    assert_eq!(render_json(&parse_markdown(EVERYTHING)).unwrap(), md);
    // And the markdown it gave parses back to the same thing.
    assert_eq!(render_json(&parse_markdown(&md)).unwrap(), md);
}

#[test]
fn tolerates_what_a_client_may_send() {
    // Flags without a value, comment kinds, a lang/align left out, no trailing newline.
    let md = render_json(
        r#"{"text":"hello world\n","lines":[{"kind":"bullet"}],
            "inline":[{"from":0,"to":5,"kind":"bold"},
                      {"from":6,"to":11,"kind":"comment:abc","value":"t1"}]}"#,
    )
    .unwrap();
    assert_eq!(md, "- **hello** world\n");
    // Fewer line entries than lines: the rest are paragraphs. A selection ending mid-line.
    let md = render_json(r#"{"text":"a\nb\nc","lines":[{"kind":"h2"}],"inline":[]}"#).unwrap();
    // The unterminated last line loses its (meaningless) kind; markdown always ends in a newline.
    assert_eq!(md, "## a\nb\nc\n");
    assert_eq!(
        render_json(r#"{"text":"","lines":[],"inline":[]}"#).unwrap(),
        ""
    );
}

#[test]
fn rejects_bad_models() {
    for (json, needle) in [
        ("not json", "invalid model"),
        (r#"{"text":"a\n","lines":[{"kind":"h9"}]}"#, "unknown kind"),
        (
            r#"{"text":"ab","inline":[{"from":1,"to":3,"kind":"bold"}]}"#,
            "outside",
        ),
        (
            r#"{"text":"ab","inline":[{"from":2,"to":1,"kind":"bold"}]}"#,
            "outside",
        ),
        (
            r#"{"text":"ab","inline":[{"from":0,"to":1,"kind":"blink"}]}"#,
            "unknown kind",
        ),
        (
            r#"{"text":"ab","inline":[{"from":0,"to":1,"kind":"link"}]}"#,
            "needs a url",
        ),
    ] {
        let e = render_json(json).unwrap_err();
        assert!(e.0.contains(needle), "{json}: {e}");
    }
}

#[test]
fn random_documents_survive_the_wire() {
    for seed in 0..200 {
        let model = normalize(&common::random_model(&mut Rng::new(seed)));
        let back = normalize(&from_json(&to_json(&model)).unwrap());
        assert!(same(&back, &model), "seed {seed}: {model:?} vs {back:?}");
    }
}
