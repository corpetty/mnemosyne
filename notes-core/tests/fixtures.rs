//! Render and parse fixtures. Every note here is synthetic.

mod common;

use common::origin;
use notes_core::model::{Block, CELL};
use notes_core::{parse, render, Notes, KIND_BLOCK, KIND_LINK};

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
fn blocks() {
    let cases = [
        (
            "# a\n## b\n### c\n#### d\n###### e\n",
            "# a\n## b\n### c\n### d\n### e\n",
        ),
        (
            "- a\n* b\n+ c\n1. x\n7) y\n- [ ] t\n* [X] u\n+ [x] v\n",
            "- a\n- b\n- c\n1. x\n2. y\n- [ ] t\n- [x] u\n- [x] v\n",
        ),
        ("1. a\n\n1. b\n2. c\n", "1. a\n\n1. b\n2. c\n"),
        ("> a\n> > b\n>c\n", "> a\n> b\n> c\n"),
        ("  - nested\n    1. deeper\n", "- nested\n1. deeper\n"),
        ("-\n#\n>\n1.\n- [ ]\n", "-\n#\n>\n1.\n- [ ]\n"),
        ("# Title ##\n## C#\n", "# Title\n## C#\n"),
        ("# a \\#\n", "# a \\#\n"),
        ("a\r\nb", "a\nb\n"),
        ("", ""),
        ("\n", "\n"),
        ("a\n\n\n", "a\n\n\n"),
    ];
    for (md, want) in cases {
        assert_eq!(canonical(md), want, "input {md:?}");
    }
}

#[test]
fn code_fences() {
    let cases = [
        ("```py\nx = 1\n\n```\n", "```py\nx = 1\n\n```\n"),
        ("~~~\n**not bold**\n~~~\n", "```\n**not bold**\n```\n"),
        ("````\n```\n````\n", "````\n```\n````\n"),
        ("```\n```\n", "```\n\n```\n"),
        ("```js\nunclosed\n", "```js\nunclosed\n```\n"),
        // Consecutive code lines are one fence (the first language wins).
        ("```a\nx\n```\n```b\ny\n```\n", "```a\nx\ny\n```\n"),
        (
            "```\nx\n```\n\n```\ny\n```\n",
            "```\nx\n```\n\n```\ny\n```\n",
        ),
    ];
    for (md, want) in cases {
        assert_eq!(canonical(md), want, "input {md:?}");
    }
}

#[test]
fn inline() {
    let cases = [
        (
            "**b** *i* ~~s~~ `c` [l](http://u)",
            "**b** *i* ~~s~~ `c` [l](http://u)\n",
        ),
        ("__b__ _i_ ___bi___", "**b** *i* ***bi***\n"),
        ("**bold *both* bold**", "**bold *both* bold**\n"),
        ("*it **both** it*", "*it* ***both*** *it*\n"),
        ("[**a** b](u)", "[**a** b](u)\n"),
        ("**a [b](u) c**", "**a** [**b**](u) **c**\n"),
        ("[a](http://u \"title\")", "[a](http://u)\n"),
        ("[a](<b c>) [x](a_(b))", "[a](<b c>) [x](a_\\(b\\))\n"),
        ("``a ` b`` ` x `", "``a ` b`` `x`\n"),
        ("mid**word**bold", "mid**word**bold\n"),
        ("~single~ ~~double~~", "~single~ ~~double~~\n"),
    ];
    for (md, want) in cases {
        assert_eq!(canonical(md), want, "input {md:?}");
    }
}

#[test]
fn escapes_only_where_needed() {
    let cases = [
        ("\\*not\\* a * b 2\\*3", "\\*not\\* a * b 2\\*3\n"),
        ("snake_case_name \\_x\\_", "snake_case_name \\_x\\_\n"),
        ("\\# not heading", "\\# not heading\n"),
        ("\\- not a bullet", "\\- not a bullet\n"),
        ("1\\. not a number", "1\\. not a number\n"),
        ("\\> not a quote", "\\> not a quote\n"),
        ("<div>x</div> a < b", "\\<div>x\\</div> a < b\n"),
        ("&amp; & AT&T", "\\&amp; & AT&T\n"),
        ("![img](src)", "![img\\](src)\n"),
        ("[[Wiki Link]] and [x]", "[[Wiki Link]] and [x]\n"),
        ("C:\\path\\to", "C:\\path\\to\n"),
        ("a\\", "a\\\\\n"),
        ("a\n===", "a\n\\===\n"),
        ("---\n***", "\\---\n\\*\\*\\*\n"),
        ("- \\[ ] not a task", "- \\[ ] not a task\n"),
        ("[ref]: http://x", "\\[ref]: http://x\n"),
        ("`` ` ``", "`` ` ``\n"),
    ];
    for (md, want) in cases {
        assert_eq!(canonical(md), want, "input {md:?}");
    }
}

#[test]
fn blank_lines_between_blocks() {
    let cases = [
        // A paragraph after a list, quote or table would continue it without a blank line.
        ("- a\nb\n", "- a\n\nb\n"),
        ("- a\n\nb\n", "- a\n\nb\n"),
        ("- a\n\n\nb\n", "- a\n\n\nb\n"),
        ("> q\nb\n", "> q\n\nb\n"),
        ("| a |\n| - |\n\nafter\n", "| a |\n| --- |\n\nafter\n"),
        (
            "| a |\n| - |\n\n| b |\n| - |\n",
            "| a |\n| --- |\n\n| b |\n| --- |\n",
        ),
        ("para\n| a |\n| - |\n", "para\n\n| a |\n| --- |\n"),
        ("# h\n- a\n> q\n", "# h\n- a\n> q\n"),
    ];
    for (md, want) in cases {
        assert_eq!(canonical(md), want, "input {md:?}");
    }
}

#[test]
fn tables() {
    let md = "| a | b |\n|:-|:-:|\n| 1 | x\\|y |\n| **B** | `c\\|d` |\n";
    assert_eq!(
        canonical(md),
        "| a | b |\n| :--- | :---: |\n| 1 | x\\|y |\n| **B** | `c\\|d` |\n"
    );
    let m = parse(md);
    let text: String = m.text.iter().collect();
    assert_eq!(text, format!("a{CELL}b\n1{CELL}x|y\nB{CELL}c|d\n"));
    assert_eq!(m.lines[0].kind, Some(Block::TableHead));
    assert_eq!(m.lines[0].align.as_deref(), Some("lc"));
    assert_eq!(m.lines[1].kind, Some(Block::Table));
    // Right alignment, no pipes at the edges, a row continuing without pipes.
    assert_eq!(
        canonical("a | b\n--: | ---\nc | d\ne\n"),
        "| a | b |\n| ---: | --- |\n| c | d |\n| e |  |\n"
    );
    // Header and separator cell counts must agree, else it is text.
    assert_eq!(canonical("a | b\n| - |\n"), "a | b\n\\| - |\n");
}

#[test]
fn ragged_rows_render_padded() {
    let mut doc = Notes::new(origin(7));
    doc.set_text("| a | b |\n| --- | --- |\n| 1 | 2 |\n");
    // A concurrent column edit: a third cell in the last row only.
    let text = doc.text();
    let end = text.chars().count() - 1;
    doc.insert(end, &format!("{CELL}3"));
    assert_eq!(
        doc.render().markdown,
        "| a | b |  |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n"
    );
}

#[test]
fn plain_text() {
    let m = parse("# Title\n\n- **one** [two](u)\n| a | b |\n| - | - |\n");
    let r = render(&m);
    assert_eq!(r.plain, "Title\n\none two\na\tb\n");
    // One character for one: positions in plain are positions in the document.
    assert_eq!(r.plain.chars().count(), m.text.len());
}

#[test]
fn parse_gives_text_kinds_and_ranges() {
    let parsed = parse("## Plan\n- [x] ship **it** [now](https://x.y)\n").to_parsed();
    assert_eq!(parsed.text, "Plan\nship it now\n");
    let kinds: Vec<_> = parsed.lines.iter().map(|l| l.kind).collect();
    assert_eq!(kinds, [Some(Block::H2), Some(Block::TaskDone)]);
    let ranges: Vec<_> = parsed
        .inline
        .iter()
        .map(|r| (r.start, r.end, r.kind, r.value.as_deref()))
        .collect();
    assert_eq!(
        ranges,
        [
            (10, 12, "bold", None),
            (13, 16, "link", Some("https://x.y"))
        ]
    );
}

#[test]
fn unterminated_document_renders_as_if_terminated() {
    let mut doc = Notes::new(origin(3));
    doc.set_text("- a\n");
    doc.remove(1, 1); // a client deletes the last `\n`
    assert_eq!(doc.text(), "a");
    assert_eq!(doc.render().markdown, "a\n");
    // set_text puts the terminator back.
    assert!(!doc.set_text("a\n").is_empty());
    assert_eq!(doc.text(), "a\n");
    // Everything deleted: an empty document.
    doc.remove(0, 2);
    assert_eq!(doc.render().markdown, "");
    assert!(doc.set_text("").is_empty());
}

#[test]
fn concurrent_values_resolve_the_same_everywhere() {
    let mut base = Notes::new(origin(4));
    base.set_text("item\nother\n");
    let (mut a, mut b) = (base.clone(), base.clone());
    let mut minted = Vec::new();
    a.mark_line(4, KIND_BLOCK, Some("task"), &mut minted);
    b.mark_line(4, KIND_BLOCK, Some("task_done"), &mut minted);
    a.mark_line(10, KIND_BLOCK, Some("bullet"), &mut minted);
    b.mark_line(10, KIND_BLOCK, Some("h2"), &mut minted);
    let (mut ab, mut ba) = (a.clone(), b.clone());
    ab.merge(b.clone());
    ba.merge(a.clone());
    let md = ab.render().markdown;
    assert_eq!(md, ba.render().markdown);
    assert!(
        md.starts_with("- [x] item\n"),
        "task + task_done is done: {md}"
    );
    assert!(md == "- [x] item\n- other\n" || md == "- [x] item\n## other\n");
    // Both values stay live: setting the same rendering back changes nothing.
    assert!(ab.set_text(&md).is_empty());
}

#[test]
fn link_conflict_renders_as_text() {
    let mut base = Notes::new(origin(5));
    base.set_text("see docs here\n");
    let (mut a, b) = (base.clone(), base.clone());
    let mut b = b;
    let mut minted = Vec::new();
    a.mark_inline(
        4,
        8,
        KIND_LINK,
        Some("https://a.example/docs"),
        false,
        &mut minted,
    );
    b.mark_inline(
        4,
        8,
        KIND_LINK,
        Some("https://b.example/docs"),
        false,
        &mut minted,
    );
    a.merge(b);
    assert_eq!(a.render().markdown, "see docs (link conflict) here\n");
}

#[test]
fn comment_ranges_follow_the_text() {
    let mut doc = Notes::new(origin(6));
    doc.set_text("alpha beta gamma\n");
    let mut minted = Vec::new();
    doc.mark_inline(
        6,
        10,
        "comment:3f2a9c1b7d4e",
        Some("thread"),
        false,
        &mut minted,
    );
    assert!(!minted.is_empty(), "comment kinds are artifacts");
    assert_eq!(doc.comments()["3f2a9c1b7d4e"], Some((6, 10)));
    doc.insert(0, "new ");
    assert_eq!(doc.comments()["3f2a9c1b7d4e"], Some((10, 14)));
    // Comments are not markdown: set_text leaves them alone.
    assert!(doc.set_text("new alpha beta gamma\n").is_empty());
    doc.set_text("new alpha gamma\n");
    assert_eq!(doc.comments()["3f2a9c1b7d4e"], None);
}

#[test]
fn indexes_are_code_points() {
    let mut doc = Notes::new(origin(8));
    doc.set_text("日本 😀 x\n");
    let mut minted = Vec::new();
    // `x` is character 5 (the emoji is one code point, two UTF-16 units).
    doc.mark_inline(5, 6, "bold", Some("true"), true, &mut minted);
    assert_eq!(doc.render().markdown, "日本 😀 **x**\n");
}
