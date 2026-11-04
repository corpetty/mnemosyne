//! Line refs: the `ref` mark set through `set_text`'s `refs`, kept across rewording, gone
//! with its line, cleared by `""`, reported with the markdown line it renders at, merged,
//! and never in the markdown. Every note here is synthetic.

mod common;

use std::collections::BTreeMap;

use base64::{engine::general_purpose::STANDARD, Engine};
use common::{origin, random_model, Rng};
use hashseq::encoding::ARTIFACT_TAG;
use notes_core::api::handle;
use notes_core::parse::parse_lines;
use notes_core::render::md_lines;
use notes_core::{normalize, parse, render, Notes, KIND_REF};
use serde_json::{json, Value};

const IMAGE: &str =
    "3f0c5aa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5ca9.webp@1600x900";

fn refs(pairs: &[(usize, &str)]) -> BTreeMap<usize, String> {
    pairs.iter().map(|&(n, r)| (n, r.to_string())).collect()
}

/// (document line, ref) of every ref'd line.
fn by_index(doc: &Notes) -> Vec<(usize, String)> {
    doc.refs().into_iter().map(|r| (r.index, r.value)).collect()
}

fn hex(o: u8) -> String {
    notes_core::id_to_hex(&origin(o))
}

fn call(request: Value) -> Value {
    handle(request).expect("answer")
}

fn b64(bytes: &[u8]) -> String {
    STANDARD.encode(bytes)
}

fn snapshot_of(answer: &Value) -> Vec<u8> {
    STANDARD
        .decode(answer["snapshot"].as_str().expect("snapshot"))
        .expect("base64")
}

#[test]
fn a_ref_set_by_set_text_is_in_refs_and_on_its_task() {
    let md = "# Summary\n- [ ] write the report (Ada)\n- [ ] a person's own task\n";
    let answer = call(json!({
        "op": "set_text", "notes": hex(1), "snapshot": null, "frames": [], "markdown": md,
        "refs": {"0": "sec:summary", "1": "sum:0a1b2c3d"},
    }));
    assert_eq!(answer["markdown"], md, "refs are not in the markdown");
    assert_eq!(answer["plain"], md.replace("# ", "").replace("- [ ] ", ""));
    let got = answer["refs"].as_array().expect("refs");
    assert_eq!(got.len(), 2);
    assert_eq!(got[0]["ref"], "sec:summary");
    assert_eq!(
        (got[0]["index"].clone(), got[0]["md_line"].clone()),
        (json!(0), json!(0))
    );
    assert_eq!(got[1]["ref"], "sum:0a1b2c3d");
    assert_eq!(
        (got[1]["index"].clone(), got[1]["md_line"].clone()),
        (json!(1), json!(1))
    );
    let tasks = answer["tasks"].as_array().expect("tasks");
    assert_eq!(tasks.len(), 2);
    assert_eq!(tasks[0]["ref"], "sum:0a1b2c3d");
    assert_eq!(tasks[0]["line"], got[1]["line"], "the same terminator");
    assert_eq!(tasks[1]["ref"], Value::Null);

    // Every op answers with them, a fold of the snapshot too.
    let folded = call(json!({
        "op": "fold", "notes": hex(1), "snapshot": answer["snapshot"], "frames": [],
    }));
    assert_eq!(folded["refs"], answer["refs"]);
    assert_eq!(folded["tasks"], answer["tasks"]);
    // A document without refs answers with an empty list.
    let none = call(json!({"op": "fold", "notes": hex(1), "snapshot": null, "frames": []}));
    assert_eq!(none["refs"], json!([]));
}

#[test]
fn a_reworded_line_keeps_its_ref_and_a_new_line_has_none() {
    let mut doc = Notes::new(origin(2));
    doc.set_text(
        "Notes\n- [ ] write the report (Ada)\nlast\n",
        &refs(&[(1, "sum:00000001")]),
    );
    assert_eq!(by_index(&doc), vec![(1, "sum:00000001".to_string())]);
    // Reworded, without a `refs` key: the terminator stays, and so the ref.
    doc.set_text(
        "Notes\n- [ ] write the final report (Ada Lovelace)\nlast\n",
        &BTreeMap::new(),
    );
    assert_eq!(by_index(&doc), vec![(1, "sum:00000001".to_string())]);
    // Another kind too: the ref is on any kind.
    doc.set_text("Notes\n- write the final report\nlast\n", &BTreeMap::new());
    assert_eq!(by_index(&doc), vec![(1, "sum:00000001".to_string())]);
    // A new line above moves it down; the new line (even with the same text) has none.
    doc.set_text(
        "Notes\n- write the final report\n- write the final report\nlast\n",
        &BTreeMap::new(),
    );
    let got = by_index(&doc);
    assert_eq!(got.len(), 1, "{got:?}");
    assert_eq!(got[0].1, "sum:00000001");
    // A brand-new line has none.
    doc.set_text(
        "Notes\nsomething else entirely\n- write the final report\n- write the final report\nlast\n",
        &BTreeMap::new(),
    );
    assert_eq!(doc.refs().len(), 1);
    assert!(!render(&doc.model()).markdown.contains("sum:"));
}

#[test]
fn a_deleted_lines_ref_is_gone_and_an_empty_value_clears() {
    let mut doc = Notes::new(origin(3));
    doc.set_text(
        "alpha one\nbeta two\ngamma three\n",
        &refs(&[(0, "cp:1"), (1, "cp:2"), (2, "cp:3")]),
    );
    assert_eq!(doc.refs().len(), 3);
    doc.set_text("alpha one\ngamma three\n", &BTreeMap::new());
    assert_eq!(
        by_index(&doc),
        vec![(0, "cp:1".to_string()), (1, "cp:3".to_string())]
    );
    // `""` clears, a value replaces, an absent key keeps.
    let frames = doc.set_text("alpha one\ngamma three\n", &refs(&[(0, ""), (1, "cp:9")]));
    assert!(!frames.is_empty());
    assert_eq!(by_index(&doc), vec![(1, "cp:9".to_string())]);
    // Asking for what is there changes nothing; clearing a line without one neither.
    assert!(doc
        .set_text("alpha one\ngamma three\n", &refs(&[(0, ""), (1, "cp:9")]))
        .is_empty());
    assert!(doc
        .set_text("alpha one\ngamma three\n", &BTreeMap::new())
        .is_empty());
}

#[test]
fn md_line_points_at_the_rendering_with_gaps_fences_tables_and_images() {
    let md = format!(
        "# Title\n- item one\n\n\nparagraph\n```py\nx = 1\ny = 2\n```\n| a | b |\n| --- | --- |\n\
         | 1 | 2 |\n| 3 | 4 |\n\n![cap](mnemosyne:image/{IMAGE})\n\n- [ ] task (Owner)\n"
    );
    // Canonical already: the document renders back to it.
    assert_eq!(render(&parse(&md)).markdown, md);
    let lines: Vec<&str> = md.lines().collect();
    assert_eq!(lines[3], "", "the gap between the list and the paragraph");
    // Keys at every markdown line that starts a document line, plus some that start none:
    // the gap (3), the first code line's own line (6, its line starts at the fence), the
    // closing fence (8), the separator row (10), the gaps before and after the image (13,
    // 15) and past the end (99).
    let starts = [0, 1, 2, 4, 5, 7, 9, 11, 12, 14, 16];
    let mut want: BTreeMap<usize, String> = starts.iter().map(|&n| (n, format!("r{n}"))).collect();
    for n in [3, 6, 8, 10, 13, 15, 99] {
        want.insert(n, format!("ignored{n}"));
    }
    let answer = call(json!({
        "op": "set_text", "notes": hex(4), "snapshot": null, "frames": [], "markdown": md,
        "refs": want,
    }));
    assert_eq!(answer["markdown"], md);
    let got: Vec<(u64, String, u64)> = answer["refs"]
        .as_array()
        .expect("refs")
        .iter()
        .map(|r| {
            (
                r["index"].as_u64().expect("index"),
                r["ref"].as_str().expect("ref").to_string(),
                r["md_line"].as_u64().expect("md_line"),
            )
        })
        .collect();
    let expected: Vec<(u64, String, u64)> = starts
        .iter()
        .enumerate()
        .map(|(i, &n)| (i as u64, format!("r{n}"), n as u64))
        .collect();
    assert_eq!(got, expected);
    // What those markdown lines are.
    let at = |i: usize| lines[got[i].2 as usize];
    assert_eq!(at(0), "# Title");
    assert_eq!(
        at(2),
        "",
        "the empty line is the first blank, the gap the second"
    );
    assert_eq!(at(3), "paragraph");
    assert_eq!(
        at(4),
        "```py",
        "a code run's first line starts at its fence"
    );
    assert_eq!(at(5), "y = 2");
    assert_eq!(at(6), "| a | b |");
    assert_eq!(at(7), "| 1 | 2 |", "rows follow the separator row");
    assert_eq!(at(9), format!("![cap](mnemosyne:image/{IMAGE})"));
    assert_eq!(at(10), "- [ ] task (Owner)");
    assert_eq!(answer["tasks"][0]["ref"], "r16");
    // An empty fence: its only line starts at the fence.
    let mut doc = Notes::new(origin(4));
    doc.set_text("text\n```\n```\n", &refs(&[(1, "fence")]));
    let r = doc.refs();
    assert_eq!((r[0].index, r[0].md_line), (1, 1));
}

#[test]
fn md_lines_are_what_the_parser_reads_back() {
    // For random documents: the line each document line renders at is the line the parser
    // says it starts at, and a ref on every line changes neither the markdown nor that.
    for seed in 0..300 {
        let mut rng = Rng::new(seed);
        let m = normalize(&random_model(&mut rng));
        let rendered = render(&m).markdown;
        let starts = md_lines(&m);
        assert_eq!(starts.len(), m.line_spans().len(), "seed {seed}");
        let (back, src) = parse_lines(&rendered);
        assert_eq!(src, starts, "seed {seed}: {rendered:?}");
        assert!(back.lines.iter().all(|l| l.r#ref.is_none()), "never parsed");
        let mut with = m.clone();
        for (i, line) in with.lines.iter_mut().enumerate() {
            line.r#ref = Some(format!("sum:{i:08x}"));
        }
        assert_eq!(
            render(&with).markdown,
            rendered,
            "seed {seed}: never rendered"
        );
        assert_eq!(md_lines(&with), starts);
    }
}

#[test]
fn refs_set_by_md_line_round_trip_through_set_text() {
    // Set a ref on every line through the keys an answer gives, then set_text of the
    // rendering with those same refs, or with none, changes nothing.
    for seed in 0..120 {
        let mut rng = Rng::new(seed);
        let m = normalize(&random_model(&mut rng));
        let md = render(&m).markdown;
        let keys: BTreeMap<usize, String> = md_lines(&m)
            .into_iter()
            .enumerate()
            .map(|(i, n)| (n, format!("cp:{i}")))
            .collect();
        let mut doc = Notes::new(origin(5));
        doc.set_text(&md, &keys);
        let got = doc.refs();
        assert_eq!(got.len(), m.line_spans().len(), "seed {seed}");
        for r in &got {
            assert_eq!(r.value, format!("cp:{}", r.index), "seed {seed}");
        }
        assert_eq!(render(&doc.model()).markdown, md, "seed {seed}");
        assert!(doc.set_text(&md, &keys).is_empty(), "seed {seed}");
        assert!(
            doc.set_text(&md, &BTreeMap::new()).is_empty(),
            "seed {seed}"
        );
    }
}

#[test]
fn merge_keeps_refs_from_both_sides() {
    let mut base = Notes::new(origin(6));
    base.set_text("one\ntwo\nthree\n", &BTreeMap::new());
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("one\ntwo\nthree\n", &refs(&[(0, "sec:summary")]));
    b.set_text(
        "one\ntwo\nthree, edited\n",
        &refs(&[(2, "sec:action_items")]),
    );
    let answer = call(json!({
        "op": "merge", "notes": hex(6), "snapshot": b64(&a.snapshot()),
        "other": b64(&b.snapshot()),
    }));
    assert_eq!(answer["markdown"], "one\ntwo\nthree, edited\n");
    let got: Vec<(Value, Value)> = answer["refs"]
        .as_array()
        .expect("refs")
        .iter()
        .map(|r| (r["index"].clone(), r["ref"].clone()))
        .collect();
    assert_eq!(
        got,
        vec![
            (json!(0), json!("sec:summary")),
            (json!(2), json!("sec:action_items"))
        ]
    );
    // The frames bring a's replica the other side's ref (an artifact: 16 bytes).
    let frames = answer["frames"].as_array().expect("frames");
    let mut a2 = a.clone();
    for f in frames {
        let bytes = STANDARD.decode(f.as_str().expect("b64")).expect("base64");
        a2.apply_frame(&bytes).expect("frame");
    }
    assert_eq!(
        a2.refs(),
        Notes::from_snapshot(origin(6), &snapshot_of(&answer))
            .unwrap()
            .refs()
    );
}

#[test]
fn two_live_values_resolve_to_the_first_on_every_replica() {
    let mut base = Notes::new(origin(7));
    base.set_text("one\ntwo\n", &BTreeMap::new());
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("one\ntwo\n", &refs(&[(1, "cp:1")]));
    b.set_text("one\ntwo\n", &refs(&[(1, "cp:2")]));
    let (mut ab, mut ba) = (a.clone(), b.clone());
    ab.merge(b);
    ba.merge(a);
    assert_eq!(ab.refs(), ba.refs());
    let got = by_index(&ab);
    assert_eq!(got.len(), 1);
    assert!(got[0].1 == "cp:1" || got[0].1 == "cp:2", "{got:?}");
    // Both stay live: setting the shown value again changes nothing, a set_text naming
    // the other one wins over both.
    let shown = got[0].1.clone();
    assert!(ab.set_text("one\ntwo\n", &BTreeMap::new()).is_empty());
    let other = if shown == "cp:1" { "cp:2" } else { "cp:1" };
    ab.set_text("one\ntwo\n", &refs(&[(1, other)]));
    assert_eq!(by_index(&ab), vec![(1, other.to_string())]);
}

#[test]
fn long_values_travel_as_artifacts_and_reach_another_replica() {
    let mut base = Notes::new(origin(8));
    base.set_text("## Action items\n- [ ] a (Ada)\n", &BTreeMap::new());
    let mut other = base.clone();
    let frames = base.set_text(
        "## Action items\n- [ ] a (Ada)\n",
        &refs(&[(0, "sec:open_questions"), (1, "sum:12345678")]),
    );
    // `sec:open_questions` is 18 bytes: an artifact; `sum:12345678` (12) and the kind
    // `ref` ride inside the delta.
    assert_eq!(frames.iter().filter(|f| f[0] == ARTIFACT_TAG).count(), 1);
    for f in &frames {
        other.apply_frame(f).expect("frame");
    }
    assert_eq!(other.refs(), base.refs());
    assert_eq!(
        by_index(&other),
        vec![
            (0, "sec:open_questions".to_string()),
            (1, "sum:12345678".to_string())
        ]
    );
}

#[test]
fn a_ref_marked_by_hand_reads_like_one_set_by_set_text() {
    // As a client (or an older sidecar's frames) would carry it: the closed mark on `\n`.
    let mut doc = Notes::new(origin(9));
    doc.set_text("first\nsecond\n", &BTreeMap::new());
    let mut minted = Vec::new();
    doc.mark_line(12, KIND_REF, Some("cp:7"), &mut minted);
    assert_eq!(by_index(&doc), vec![(1, "cp:7".to_string())]);
    // An empty value is no ref.
    doc.mark_line(5, KIND_REF, Some(""), &mut minted);
    assert_eq!(doc.refs().len(), 1);
    // set_text of the rendering keeps it.
    assert!(doc
        .set_text(&render(&doc.model()).markdown, &BTreeMap::new())
        .is_empty());
}

#[test]
fn bad_refs_are_an_invalid_request() {
    let err = handle(json!({
        "op": "set_text", "notes": hex(1), "snapshot": null, "frames": [], "markdown": "a\n",
        "refs": {"one": "x"},
    }))
    .expect_err("not a line number");
    assert_eq!(err.0, "refs: \"one\" is not a line number");
    for bad in ["-1", "+1", "01", " 1", "1.0"] {
        let err = handle(json!({
            "op": "set_text", "notes": hex(1), "snapshot": null, "frames": [],
            "markdown": "a\n", "refs": {bad: "x"},
        }))
        .expect_err(bad);
        assert!(err.0.starts_with("refs: "), "{}", err.0);
    }
    // `null` is no refs.
    let ok = call(json!({
        "op": "set_text", "notes": hex(1), "snapshot": null, "frames": [], "markdown": "a\n",
        "refs": null,
    }));
    assert_eq!(ok["refs"], json!([]));
}
