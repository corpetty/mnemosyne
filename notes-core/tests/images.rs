//! Image lines: the `image` mark, markdown both ways, set_text, conflicts, `media` in the
//! answers and the client wire form. Every note here is synthetic.

mod common;

use base64::{engine::general_purpose::STANDARD, Engine};
use common::origin;
use hashseq::encoding::ARTIFACT_TAG;
use notes_core::api::handle;
use notes_core::client::{from_json, parse_markdown, render_json, to_json};
use notes_core::model::{Block, Link};
use notes_core::{
    fold, id_to_hex, is_image_value, normalize, parse, render, Notes, KIND_BLOCK, KIND_IMAGE,
    KIND_LINK,
};
use serde_json::{json, Value};

const A: &str = "3f0c5aa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5ca9.webp@1600x900";
const B: &str = "00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff.jpeg@640x480";

fn line(caption: &str, value: &str) -> String {
    format!("![{caption}](mnemosyne:image/{value})")
}

/// Markdown in, canonical markdown out (and the output is a fixpoint).
fn canonical(md: &str) -> String {
    let out = render(&parse(md)).markdown;
    assert_eq!(
        render(&parse(&out)).markdown,
        out,
        "not a fixpoint: {out:?}"
    );
    out
}

#[test]
fn image_values_are_checked_by_the_pattern() {
    assert!(is_image_value(A));
    assert!(is_image_value(B));
    let sha = &A[..64];
    for ok in [format!("{sha}.webp@1x1"), format!("{sha}.jpeg@99999x99999")] {
        assert!(is_image_value(&ok), "{ok}");
    }
    for bad in [
        String::new(),
        format!("{sha}.png@1x1"),
        format!("{sha}.webp@0x1"),
        format!("{sha}.webp@01x1"),
        format!("{sha}.webp@100000x1"),
        format!("{sha}.webp@1x"),
        format!("{sha}.webp@1x1x1"),
        format!("{sha}.webp@1X1"),
        format!("{sha}.webp"),
        format!("{sha}webp@1x1"),
        format!("{}.webp@1x1", sha.to_uppercase()),
        format!("{}.webp@1x1", &sha[1..]),
        format!("{sha}0.webp@1x1"),
        format!("{sha}.webp@1x1 "),
        format!("{sha}.webp.webp@1x1"),
    ] {
        assert!(!is_image_value(&bad), "{bad}");
    }
}

#[test]
fn an_image_line_round_trips() {
    let md = format!("# Board\n{}\nafter\n", line("The whiteboard", A));
    assert_eq!(canonical(&md), md);
    let m = parse(&md);
    assert_eq!(m.lines[1].kind, Some(Block::Image));
    assert_eq!(m.lines[1].image.as_deref(), Some(A));
    let text: String = m.text.iter().collect();
    assert_eq!(text, "Board\nThe whiteboard\nafter\n");
    // The plain text is the caption: search finds it.
    assert_eq!(render(&m).plain, "Board\nThe whiteboard\nafter\n");
}

#[test]
fn captions_are_escaped_as_link_text() {
    for (caption, md) in [
        ("", "![]"),
        ("a ] b", r"![a \] b]"),
        ("[x]", r"![\[x\]]"),
        ("**not bold**", r"![\*\*not bold\*\*]"),
        ("a * b", "![a * b]"),
        ("snake_case and _x_", r"![snake_case and \_x\_]"),
        ("`tick`", r"![\`tick\`]"),
        ("ends with \\", r"![ends with \\]"),
        ("x](mnemosyne:image/y)", r"![x\](mnemosyne:image/y)]"),
        ("日本 😀 !", "![日本 😀 !]"),
    ] {
        let mut m = parse("cap\n");
        m.text = caption.chars().chain(['\n']).collect();
        m.fmt = vec![Default::default(); m.text.len()];
        m.lines[0].kind = Some(Block::Image);
        m.lines[0].image = Some(A.to_string());
        let out = render(&m).markdown;
        assert_eq!(out, format!("{md}(mnemosyne:image/{A})\n"), "{caption:?}");
        let back = parse(&out);
        assert_eq!(back.lines[0].kind, Some(Block::Image), "{caption:?}");
        let text: String = back.text.iter().collect();
        assert_eq!(text, format!("{caption}\n"), "{caption:?}");
        assert_eq!(render(&back).markdown, out);
    }
}

#[test]
fn a_caption_is_plain_text() {
    // Formats and links on a caption mean nothing: a chip-like link stays plain.
    let mut m = parse(&format!("{}\n", line("Bob at the board", A)));
    let link = m.intern_url("mnemosyne:person/Bob");
    for f in &mut m.fmt[0..3] {
        f.link = link;
        f.set(notes_core::model::BOLD, true);
    }
    let n = normalize(&m);
    assert!(n.fmt.iter().all(|f| f.flags == 0 && f.link == Link::None));
    assert_eq!(
        render(&m).markdown,
        format!("{}\n", line("Bob at the board", A))
    );
    // Markup typed into a caption by hand is dropped, its text kept.
    let typed = format!(
        "{}\n",
        line("**Bob** [at](mnemosyne:person/Bob) `the` board", A)
    );
    assert_eq!(
        canonical(&typed),
        format!("{}\n", line("Bob at the board", A))
    );
    let back = parse(&typed);
    assert!(back.urls.is_empty(), "caption links intern no URLs");
    // A chip's markdown as caption text is kept as text, escaped.
    let mut m = parse("x\n");
    m.text = "[Bob](mnemosyne:person/Bob)\n".chars().collect();
    m.fmt = vec![Default::default(); m.text.len()];
    m.lines[0].kind = Some(Block::Image);
    m.lines[0].image = Some(A.to_string());
    let out = render(&m).markdown;
    assert_eq!(
        out,
        format!("{}\n", line(r"\[Bob\](mnemosyne:person/Bob)", A))
    );
    let text: String = parse(&out).text.iter().collect();
    assert_eq!(text, "[Bob](mnemosyne:person/Bob)\n");
}

#[test]
fn other_images_stay_literal() {
    for md in [
        "![x](https://example.com/a.png)\n".to_string(),
        "![x](mnemosyne:image/nope)\n".to_string(),
        format!("![x](mnemosyne:image/{A}) and more\n"),
        format!("see ![x](mnemosyne:image/{A})\n"),
        format!(" ![x](mnemosyne:image/{A})\n"),
        format!("![x](mnemosyne:image/{A} \"title\")\n"),
        format!("- ![x](mnemosyne:image/{A})\n"),
        // The `]` is escaped: text, not an image.
        format!("![x\\](mnemosyne:image/{A})\n"),
    ] {
        let m = parse(&md);
        assert!(
            m.lines
                .iter()
                .all(|l| l.kind != Some(Block::Image) && l.image.is_none()),
            "{md:?}: {m:?}"
        );
        canonical(&md);
    }
    // A paragraph whose text looks like an image line renders so that it stays text.
    let mut m = parse("x\n");
    m.text = format!("![x](mnemosyne:image/{A})\n").chars().collect();
    m.fmt = vec![Default::default(); m.text.len()];
    let out = render(&m).markdown;
    assert_eq!(out, format!("![x\\](mnemosyne:image/{A})\n"));
    let back = parse(&out);
    assert_eq!(back.lines[0].kind, None);
    assert_eq!(back.text, m.text);
}

#[test]
fn blank_lines_keep_lists_and_tables_apart_from_images() {
    let img = line("cap", A);
    for (md, want) in [
        // After a list, quote or table line, before a list or table head: a blank line.
        (format!("- a\n{img}\n"), format!("- a\n\n{img}\n")),
        (format!("- [ ] a\n{img}\n"), format!("- [ ] a\n\n{img}\n")),
        (format!("> q\n{img}\n"), format!("> q\n\n{img}\n")),
        (
            format!("| a |\n| --- |\n\n{img}\n"),
            format!("| a |\n| --- |\n\n{img}\n"),
        ),
        (format!("{img}\n- a\n"), format!("{img}\n\n- a\n")),
        (format!("{img}\n1. a\n"), format!("{img}\n\n1. a\n")),
        (
            format!("{img}\n| a |\n| --- |\n"),
            format!("{img}\n\n| a |\n| --- |\n"),
        ),
        // Paragraphs, headings, code and other images need none.
        (format!("p\n{img}\np\n"), format!("p\n{img}\np\n")),
        (
            format!("# h\n{img}\n{img}\n"),
            format!("# h\n{img}\n{img}\n"),
        ),
        (
            format!("```\nx\n```\n{img}\n"),
            format!("```\nx\n```\n{img}\n"),
        ),
    ] {
        assert_eq!(canonical(&md), want, "{md:?}");
        // The blank line the renderer adds is not an empty line of the document.
        let n = parse(&want).lines.len();
        assert_eq!(n, parse(&md).lines.len(), "{md:?}");
    }
    // A blank line the user typed survives next to the added one.
    let md = format!("- a\n\n\n{img}\n");
    assert_eq!(canonical(&md), md);
}

#[test]
fn set_text_adds_changes_and_removes_an_image() {
    let mut doc = Notes::new(origin(1));
    doc.set_text("# Board\nafter\n");
    // Add: a new line with its value; the value is over 14 bytes, so an artifact.
    let with_a = format!("# Board\n{}\nafter\n", line("cap", A));
    let frames = doc.set_text(&with_a);
    assert!(frames.iter().any(|f| f[0] == ARTIFACT_TAG));
    assert_eq!(doc.render().markdown, with_a);
    assert_eq!(doc.media(), [A]);
    assert!(doc.set_text(&with_a).is_empty(), "idempotent");
    // Change the picture, the caption kept.
    let with_b = with_a.replace(A, B);
    assert!(!doc.set_text(&with_b).is_empty());
    assert_eq!(doc.render().markdown, with_b);
    assert_eq!(doc.media(), [B]);
    assert!(doc.set_text(&with_b).is_empty());
    // Change the caption only.
    let recaptioned = with_b.replace("![cap]", "![new caption]");
    doc.set_text(&recaptioned);
    assert_eq!(doc.render().markdown, recaptioned);
    // Remove: the line becomes a paragraph with the same text; the picture goes with it.
    doc.set_text("# Board\nnew caption\nafter\n");
    assert_eq!(doc.render().markdown, "# Board\nnew caption\nafter\n");
    assert!(doc.media().is_empty());
    // Making it an image again needs the value again: no stale mark comes back.
    doc.mark_line(
        doc.text().find("after").unwrap() - 1,
        KIND_BLOCK,
        Some("image"),
        &mut Vec::new(),
    );
    assert_eq!(doc.render().markdown, "# Board\nnew caption\nafter\n");
}

#[test]
fn a_line_turned_into_an_image_keeps_its_terminator() {
    let mut doc = Notes::new(origin(1));
    doc.set_text("a whiteboard\n");
    let nl_before = doc.seq().id_at(12);
    doc.set_text(&format!("{}\n", line("a whiteboard", A)));
    assert_eq!(doc.seq().id_at(12), nl_before, "same line, now an image");
    assert_eq!(doc.model().lines[0].kind, Some(Block::Image));
}

#[test]
fn two_live_values_resolve_to_the_first() {
    let mut base = Notes::new(origin(4));
    base.set_text(&format!("{}\n", line("cap", A)));
    let snap = base.snapshot();
    let mut x = Notes::from_snapshot(origin(4), &snap).unwrap();
    let mut y = Notes::from_snapshot(origin(4), &snap).unwrap();
    let c = &A.replace(".webp@1600x900", ".jpeg@1x1");
    let fx = x.set_text(&format!("{}\n", line("cap", B)));
    let fy = y.set_text(&format!("{}\n", line("cap", c)));
    let xy = fold(origin(4), Some(&snap), &[fx.clone(), fy.clone()].concat()).unwrap();
    let yx = fold(origin(4), Some(&snap), &[fy, fx].concat()).unwrap();
    assert_eq!(xy.render(), yx.render(), "the same on every replica");
    let media = xy.media();
    assert_eq!(media.len(), 2, "both pictures are wanted: {media:?}");
    assert!(media.contains(&B.to_string()) && media.contains(c));
    // The one shown is the first value in markedSpans order, which media lists first.
    assert_eq!(
        xy.render().markdown,
        format!("{}\n", line("cap", &media[0]))
    );
    assert_eq!(
        xy.model().lines[0].image.as_deref(),
        Some(media[0].as_str())
    );
}

#[test]
fn an_unknown_image_value_is_a_paragraph_and_its_marks_stay() {
    let mut doc = Notes::new(origin(5));
    doc.set_text("cap\nnext\n");
    let mut minted = Vec::new();
    doc.mark_line(3, KIND_BLOCK, Some("image"), &mut minted);
    // No value: a paragraph.
    assert_eq!(doc.render().markdown, "cap\nnext\n");
    doc.mark_line(3, KIND_IMAGE, Some("not-an-image.png"), &mut minted);
    assert_eq!(doc.render().markdown, "cap\nnext\n");
    assert_eq!(doc.model().lines[0].image, None);
    assert!(doc.media().is_empty());
    // Setting the rendering back changes nothing: the marks are kept, not dropped.
    assert!(doc.set_text("cap\nnext\n").is_empty());
    // A valid value later makes it an image.
    doc.mark_line(3, KIND_IMAGE, Some(A), &mut minted);
    assert_eq!(doc.render().markdown, format!("{}\nnext\n", line("cap", A)));
    // An `image` mark on a line of another kind means nothing.
    doc.mark_line(8, KIND_IMAGE, Some(B), &mut minted);
    doc.mark_line(8, KIND_BLOCK, Some("bullet"), &mut minted);
    assert_eq!(
        doc.render().markdown,
        format!("{}\n\n- next\n", line("cap", A))
    );
    assert_eq!(doc.media(), [A]);
    // A link mark on the caption is ignored (formats on image lines are don't-care).
    doc.mark_inline(
        0,
        3,
        KIND_LINK,
        Some("https://example.com"),
        false,
        &mut minted,
    );
    assert_eq!(
        doc.render().markdown,
        format!("{}\n\n- next\n", line("cap", A))
    );
}

fn call(request: Value) -> Value {
    handle(request).expect("answer")
}

#[test]
fn every_answer_lists_the_media() {
    let mut doc = Notes::new(origin(6));
    let md = format!(
        "# Board\n{}\n\n- [ ] Ana: send it\n\n{}\n{}\n",
        line("one", A),
        line("two", B),
        line("one again", A)
    );
    let frames = doc.set_text(&md);
    let notes = id_to_hex(&origin(6));
    let b64 = |f: &[u8]| STANDARD.encode(f);
    let frames: Vec<String> = frames.iter().map(|f| b64(f)).collect();
    let folded = call(json!({"op":"fold","notes":notes,"snapshot":null,"frames":frames}));
    assert_eq!(
        folded["media"],
        json!([A, B]),
        "distinct, in document order"
    );
    assert_eq!(folded["markdown"], md);
    // An image line is not a task.
    assert_eq!(folded["tasks"].as_array().unwrap().len(), 1);
    let snap = folded["snapshot"].as_str().unwrap().to_string();

    let set = call(
        json!({"op":"set_text","notes":notes,"snapshot":snap,"frames":[],
        "markdown": format!("{}\n", line("only", B))}),
    );
    assert_eq!(set["media"], json!([B]));
    let empty = call(
        json!({"op":"set_text","notes":notes,"snapshot":snap,"frames":[],
        "markdown":"no pictures\n"}),
    );
    assert_eq!(empty["media"], json!([]));

    let other = set["snapshot"].as_str().unwrap();
    let merged = call(json!({"op":"merge","notes":notes,"snapshot":snap,"other":other}));
    // `other` descends from `snapshot`: the union is `other`.
    assert_eq!(merged["media"], json!([B]));

    let nothing = call(json!({"op":"fold","notes":notes,"snapshot":null,"frames":[]}));
    assert_eq!(nothing["media"], json!([]));
}

#[test]
fn the_client_model_carries_the_image() {
    let md = format!("# t\n{}\n", line("cap", A));
    let v: Value = serde_json::from_str(&parse_markdown(&md)).unwrap();
    assert_eq!(
        v["lines"][0],
        json!({"kind": "h1"}),
        "no image field elsewhere"
    );
    assert_eq!(v["lines"][1], json!({"kind": "image", "image": A}));
    assert_eq!(render_json(&parse_markdown(&md)).unwrap(), md);
    let back = from_json(&to_json(&parse(&md))).unwrap();
    assert_eq!(back.lines[1].image.as_deref(), Some(A));
    // An image line without a valid value is refused.
    for line in [
        json!({"kind": "image"}),
        json!({"kind": "image", "image": "x"}),
    ] {
        let model = json!({"text": "cap\n", "lines": [line]}).to_string();
        assert_eq!(
            from_json(&model).unwrap_err().0,
            "lines[0]: an image line needs an image"
        );
    }
    // A value on another line is ignored.
    let model = json!({"text": "cap\n", "lines": [{"kind": null, "image": A}]}).to_string();
    assert_eq!(render_json(&model).unwrap(), "cap\n");
}

/// The property tests (tests/props.rs) see image lines: random documents carry them.
#[test]
fn random_documents_have_image_lines() {
    let images: usize = (0..500)
        .map(|seed| {
            let m = common::random_model(&mut common::Rng::new(seed));
            m.lines
                .iter()
                .filter(|l| l.kind == Some(Block::Image))
                .count()
        })
        .sum();
    assert!(images > 100, "{images} image lines in 500 documents");
}
