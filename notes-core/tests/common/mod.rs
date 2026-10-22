//! Shared helpers: a seeded generator (no proptest dependency) and random documents.
#![allow(dead_code)]

use notes_core::model::{Block, Fmt, LineAttr, Link, Model, BOLD, CELL, CODE, ITALIC, STRIKE};

/// xorshift64*: deterministic per seed.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }
    pub fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    pub fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
    pub fn chance(&mut self, percent: usize) -> bool {
        self.below(100) < percent
    }
    pub fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len())]
    }
}

pub const URLS: [&str; 6] = [
    "https://example.com/a",
    "https://example.com/a/very/long/path?with=query&and=more#fragment",
    "http://x.y/a_(b)",
    "with space",
    "",
    "a|b\\c<d>",
];

const WORDS: [&str; 24] = [
    "a",
    "word",
    "Ünïcödé",
    "日本",
    "x1",
    "snake_case",
    "*",
    "_",
    "~",
    "`",
    "[",
    "]",
    "(",
    ")",
    "!",
    "<b>",
    "&amp;",
    "#",
    "\\",
    "|",
    "-",
    "1.",
    "> q",
    "**",
];

/// Random inline text: words and punctuation joined by single spaces (no whitespace at the
/// edges when `tight`, none at all when `no_space`).
pub fn words(rng: &mut Rng, n: usize, no_space: bool) -> String {
    let mut out = String::new();
    for i in 0..n {
        if i > 0 && !no_space {
            out.push(' ');
        }
        let w = rng.pick(&WORDS);
        if no_space {
            out.push_str(&w.replace(' ', ""));
        } else {
            out.push_str(w);
        }
    }
    out
}

/// A random document in canonical form (normalized), with rectangular tables whose cells
/// have no whitespace (cell edges are not kept by markdown).
pub fn random_model(rng: &mut Rng) -> Model {
    let mut m = Model::default();
    let lines = rng.below(10);
    let mut li = 0;
    while li < lines {
        let roll = rng.below(14);
        if roll == 0 {
            // a table
            let cols = 1 + rng.below(3);
            let rows = 1 + rng.below(3);
            for r in 0..rows {
                let mut text = String::new();
                for c in 0..cols {
                    if c > 0 {
                        text.push(CELL);
                    }
                    let n = rng.below(3);
                    text.push_str(&words(rng, n, true));
                }
                let kind = if r == 0 {
                    Block::TableHead
                } else {
                    Block::Table
                };
                let align = (r == 0 && rng.chance(50)).then(|| {
                    (0..cols)
                        .map(|_| *rng.pick(&['l', 'c', 'r', '-']))
                        .collect::<String>()
                });
                push_line(&mut m, Some(kind), &text, None, align);
            }
            li += rows;
            continue;
        }
        if roll == 1 {
            let n = 1 + rng.below(3);
            let lang = rng
                .pick(&[None, Some("rust"), Some("a b"), Some("x`y")])
                .map(str::to_string);
            for i in 0..n {
                let text = match rng.below(5) {
                    0 => "```".to_string(),
                    1 => String::new(),
                    2 => "  ~~~ x".to_string(),
                    _ => words(rng, 3, false),
                };
                push_line(
                    &mut m,
                    Some(Block::Code),
                    &text,
                    if i == 0 { lang.clone() } else { None },
                    None,
                );
            }
            li += n;
            continue;
        }
        let kind = match roll {
            2 => Some(Block::H1),
            3 => Some(Block::H2),
            4 => Some(Block::H3),
            5 => Some(Block::Bullet),
            6 => Some(Block::Number),
            7 => Some(Block::Task),
            8 => Some(Block::TaskDone),
            9 => Some(Block::Quote),
            _ => None,
        };
        let n = rng.below(5);
        let mut text = words(rng, n, false);
        if rng.chance(10) {
            text.insert_str(0, "  ");
        }
        push_line(&mut m, kind, &text, None, None);
        li += 1;
    }
    // Random inline formats.
    let len = m.text.len();
    if len > 0 {
        for _ in 0..rng.below(10) {
            let s = rng.below(len);
            let e = (s + 1 + rng.below(8)).min(len);
            let what = rng.below(5);
            let link = if what == 4 {
                let url = *rng.pick(&URLS);
                m.intern_url(url)
            } else {
                Link::None
            };
            for f in &mut m.fmt[s..e] {
                match what {
                    0 => f.set(BOLD, true),
                    1 => f.set(ITALIC, true),
                    2 => f.set(STRIKE, true),
                    3 => f.set(CODE, true),
                    _ => f.link = link,
                }
            }
        }
    }
    notes_core::normalize(&m)
}

fn push_line(
    m: &mut Model,
    kind: Option<Block>,
    text: &str,
    lang: Option<String>,
    align: Option<String>,
) {
    for c in text.chars() {
        m.text.push(c);
        m.fmt.push(Fmt::default());
    }
    m.text.push('\n');
    m.fmt.push(Fmt::default());
    m.lines.push(LineAttr { kind, lang, align });
}

pub fn origin(n: u8) -> hashseq::Id {
    hashseq::Id([n; 32])
}
