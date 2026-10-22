//! Property tests over seeded random documents.

mod common;

use common::{origin, random_model, Rng};
use notes_core::model::same;
use notes_core::{parse, render, Notes};

const CASES: u64 = 2000;

/// `render(parse(md))` is a fixpoint for rendered markdown, and parsing gives the document
/// back.
#[test]
fn render_parse_is_a_fixpoint() {
    for seed in 0..CASES {
        let mut rng = Rng::new(seed);
        let m = random_model(&mut rng);
        let md = render(&m).markdown;
        let back = parse(&md);
        let again = render(&back).markdown;
        assert_eq!(
            again, md,
            "seed {seed}: render(parse(md)) != md\nmodel {m:?}"
        );
        assert!(
            same(&back, &m),
            "seed {seed}: parse(render(m)) != m\nmd:\n{md}\nmodel {m:?}\nback {back:?}"
        );
    }
}

/// Seeding a fresh doc with set_text gives the same rendering, and setting it again does
/// nothing.
#[test]
fn set_text_round_trips_and_is_idempotent() {
    for seed in 0..CASES {
        let mut rng = Rng::new(seed);
        let m = random_model(&mut rng);
        let md = render(&m).markdown;
        let mut doc = Notes::new(origin(1));
        doc.set_text(&md);
        assert_eq!(doc.render().markdown, md, "seed {seed}");
        assert!(
            doc.set_text(&md).is_empty(),
            "seed {seed}: second set_text emitted ops"
        );
    }
}

/// A document built through the API (set_text, then random edits and marks the way a client
/// makes them): its rendering is a fixpoint and setting it back changes nothing.
#[test]
fn set_text_of_the_rendering_changes_nothing() {
    use notes_core::model::{Block, CELL};
    use notes_core::{KIND_ALIGN, KIND_BLOCK, KIND_LANG};
    let mut checked = 0;
    for seed in 0..CASES {
        let mut rng = Rng::new(seed);
        let m = random_model(&mut rng);
        let mut doc = Notes::new(origin(1));
        doc.set_text(&render(&m).markdown);
        let mut minted = Vec::new();
        for _ in 0..rng.below(12) {
            let text: Vec<char> = doc.text().chars().collect();
            if text.is_empty() {
                break;
            }
            let model = doc.model();
            let line_of = |pos: usize| text[..pos].iter().filter(|&&c| c == '\n').count();
            let spans = model.line_spans();
            match rng.below(4) {
                0 => {
                    let pos = rng.below(text.len());
                    let kind = model.lines[line_of(pos)].kind;
                    let table = kind.is_some_and(Block::is_table);
                    let n = 1 + rng.below(2);
                    let mut w = common::words(&mut rng, n, table);
                    if !table && rng.chance(30) {
                        w.push(' ');
                    }
                    doc.insert(pos, &w);
                }
                1 => {
                    let pos = rng.below(text.len());
                    let n = text[pos..]
                        .iter()
                        .take_while(|&&c| c != '\n' && c != CELL)
                        .count();
                    if n > 0 {
                        doc.remove(pos, 1 + rng.below(n));
                    }
                }
                2 => {
                    let (s, e, _) = spans[rng.below(spans.len())];
                    if s == e {
                        continue;
                    }
                    let a = s + rng.below(e - s);
                    let b = a + 1 + rng.below(e - a);
                    let (kind, expanding) = *rng.pick(&[
                        ("bold", true),
                        ("italic", true),
                        ("strike", true),
                        ("code", false),
                        ("link", false),
                    ]);
                    let value = if kind == "link" {
                        Some(*rng.pick(&common::URLS))
                    } else {
                        Some("true")
                    };
                    let value = if rng.chance(25) { None } else { value };
                    doc.mark_inline(a, b, kind, value, expanding, &mut minted);
                }
                _ => {
                    let li = rng.below(spans.len());
                    let (s, e, nl) = spans[li];
                    let Some(nl) = nl else { continue };
                    match rng.below(6) {
                        0 => doc.mark_line(
                            nl,
                            KIND_LANG,
                            Some(*rng.pick(&["rust", "py"])),
                            &mut minted,
                        ),
                        1 => doc.mark_line(
                            nl,
                            KIND_ALIGN,
                            Some(*rng.pick(&["lr", "c", "-cr", "x"])),
                            &mut minted,
                        ),
                        _ => {
                            let kind = rng.pick(&Block::ALL).as_str();
                            let spaced = text[s..e].iter().any(|c| c.is_whitespace());
                            if (kind == "table" || kind == "table_head") && spaced {
                                continue;
                            }
                            let value = if rng.chance(20) { None } else { Some(kind) };
                            doc.mark_line(nl, KIND_BLOCK, value, &mut minted);
                        }
                    }
                }
            }
        }
        // Ragged tables are padded when rendered: setting that back adds the empty cells.
        let n = notes_core::normalize(&doc.model());
        let spans = n.line_spans();
        let mut ragged = false;
        let mut li = 0;
        while li < spans.len() {
            if n.lines[li].kind == Some(Block::TableHead) {
                let cols = |l: usize| {
                    n.text[spans[l].0..spans[l].1]
                        .iter()
                        .filter(|&&c| c == CELL)
                        .count()
                };
                let c0 = cols(li);
                li += 1;
                while li < spans.len() && n.lines[li].kind == Some(Block::Table) {
                    ragged |= cols(li) != c0;
                    li += 1;
                }
            } else {
                li += 1;
            }
        }
        if ragged {
            continue;
        }
        checked += 1;
        let md = doc.render().markdown;
        assert_eq!(
            render(&parse(&md)).markdown,
            md,
            "seed {seed}: not a fixpoint"
        );
        let frames = doc.set_text(&md);
        assert!(
            frames.is_empty(),
            "seed {seed}: set_text of the rendering emitted ops\n{md}\n{:?}",
            doc.model()
        );
    }
    assert!(
        checked > CASES / 2,
        "too many ragged tables: {checked} checked"
    );
}
