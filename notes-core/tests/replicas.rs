//! Replicas, frames, snapshots and merges. Every note here is synthetic.

mod common;

use common::origin;
use hashseq::encoding::{ARTIFACT_TAG, DELTA_TAG};
use notes_core::{fold, Notes};

const NOTES: &str = "# Standup\n\n- [ ] Ana: draft the **budget**\n- Ben reviewed the [deck](https://example.com/q3/deck)\n\n| who | what |\n| :--- | --- |\n| Cho | `deploy` |\n";

#[test]
fn a_set_text_delta_gives_a_second_replica_the_same_document() {
    let mut a = Notes::new(origin(1));
    let before = a.snapshot();
    let frames = a.set_text(NOTES);
    assert_eq!(frames.last().unwrap()[0], DELTA_TAG);
    let b = fold(origin(1), Some(&before), &frames).unwrap();
    assert_eq!(b.render(), a.render());
    assert_eq!(b.render().markdown, NOTES);
    assert_eq!(b.snapshot(), a.snapshot());
}

#[test]
fn set_text_deltas_hold_only_the_new_ops() {
    let mut a = Notes::new(origin(1));
    a.set_text(NOTES);
    let snap = a.snapshot();
    // A replica decoded from the snapshot (as the sidecar does) edits one word.
    let mut b = Notes::from_snapshot(origin(1), &snap).unwrap();
    let edited = NOTES.replace("budget", "plan");
    let frames = b.set_text(&edited);
    assert_eq!(frames.len(), 1, "no new artifacts");
    let delta = &frames[0];
    assert!(
        delta.len() < snap.len(),
        "delta {} vs snapshot {}",
        delta.len(),
        snap.len()
    );
    let mut c = Notes::from_snapshot(origin(1), &snap).unwrap();
    let delivered = c.apply_frame(delta).unwrap();
    // A remove, four inserts, and the bold mark again: marks grow at their end only, so the
    // new word, typed where the old one started, is outside the old mark.
    assert_eq!(delivered, 6);
    assert_eq!(
        c.apply_frame(delta).unwrap(),
        0,
        "a duplicate changes nothing"
    );
    assert_eq!(c.render().markdown, edited);
}

#[test]
fn long_urls_travel_as_artifact_frames() {
    let url = "https://example.com/a/very/long/path/to/the/meeting/deck?version=3";
    let md = format!("see [the deck]({url})\n");
    let mut a = Notes::new(origin(2));
    let frames = a.set_text(&md);
    assert_eq!(frames.len(), 2);
    assert_eq!(frames[0][0], ARTIFACT_TAG, "artifacts first");
    assert_eq!(frames[1][0], DELTA_TAG);
    let b = fold(origin(2), None, &frames).unwrap();
    assert_eq!(b.render().markdown, md);
    // Without the artifact the link's URL is unknown: the text renders, unlinked.
    let c = fold(origin(2), None, &frames[1..]).unwrap();
    assert_eq!(c.render().markdown, "see the deck\n");
    // The snapshot carries artifacts.
    let d = Notes::from_snapshot(origin(2), &b.snapshot()).unwrap();
    assert_eq!(d.render().markdown, md);
}

#[test]
fn merge_is_the_same_in_both_orders() {
    let mut base = Notes::new(origin(3));
    base.set_text(NOTES);
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text(&NOTES.replace("Standup", "Standup notes"));
    b.set_text(&format!(
        "{NOTES}\nA [new link](https://example.com/another/long/link/here)\n"
    ));
    let (mut ab, mut ba) = (a.clone(), b.clone());
    ab.merge(b);
    ba.merge(a);
    assert_eq!(ab.snapshot(), ba.snapshot());
    assert_eq!(
        ab.render().markdown,
        format!(
            "{}\nA [new link](https://example.com/another/long/link/here)\n",
            NOTES.replace("Standup", "Standup notes")
        )
    );
}

#[test]
fn seeding_the_same_markdown_twice_gives_the_same_ops() {
    let mut a = Notes::new(origin(4));
    let mut b = Notes::new(origin(4));
    let fa = a.set_text(NOTES);
    let fb = b.set_text(NOTES);
    assert_eq!(fa, fb);
    assert_eq!(a.snapshot(), b.snapshot());
    a.merge(b);
    assert_eq!(a.render().markdown, NOTES, "no duplication");
}

#[test]
fn concurrent_word_edits_do_not_interleave() {
    let mut base = Notes::new(origin(5));
    base.set_text("The meeting went well\n");
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("The quick meeting went well\n");
    b.set_text("The meeting went really well\n");
    a.merge(b);
    assert_eq!(a.text(), "The quick meeting went really well\n");

    let mut base = Notes::new(origin(5));
    base.set_text("Hello\n");
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("Hello alpha\n");
    b.set_text("Hello bravo\n");
    a.merge(b);
    // Both inserted " <word>" at the same place from the same state: hashseq's ops are
    // content-addressed, so the two leading spaces are one op. The words stay whole.
    let text = a.text();
    assert!(
        text == "Hello alphabravo\n" || text == "Hello bravoalpha\n",
        "interleaved: {text:?}"
    );
}

#[test]
fn a_new_line_after_a_kept_line_leaves_its_kind_alone() {
    let mut doc = Notes::new(origin(6));
    doc.set_text("# Title\nbody\n");
    doc.set_text("# Title\nfirst\nbody\n");
    assert_eq!(doc.render().markdown, "# Title\nfirst\nbody\n");
    doc.set_text("# Title\n- one\n");
    doc.set_text("# Title\n- one\n- two\n");
    assert_eq!(doc.render().markdown, "# Title\n- one\n- two\n");
    // The client's Enter at a line end: the new `\n` after the old terminator.
    let at = doc.text().find("one\n").unwrap() + 4;
    doc.insert(at, "\n");
    assert_eq!(doc.render().markdown, "# Title\n- one\n\n- two\n");
}

#[test]
fn frames_for_other_objects_are_ignored_and_bad_frames_named() {
    let mut other = Notes::new(origin(9));
    let theirs = other.set_text("not ours\n");
    let mut mine = Notes::new(origin(7));
    let ours = mine.set_text("ours\n");
    let frames = vec![theirs[0].clone(), ours[0].clone()];
    let folded = fold(origin(7), None, &frames).unwrap();
    assert_eq!(folded.render().markdown, "ours\n");
    assert_eq!(folded.web.object_count(), 1);

    let mut bad = ours[0].clone();
    bad.truncate(bad.len() - 3);
    let err = fold(origin(7), None, &[ours[0].clone(), bad]).unwrap_err();
    assert!(err.0.starts_with("frame 1: "), "{err}");
    let err = fold(origin(7), None, &[vec![0xC1, 1, 2]]).unwrap_err();
    assert!(err.0.starts_with("frame 0: unknown frame tag"), "{err}");
    let err = fold(origin(7), Some(b"nope"), &[]).unwrap_err();
    assert!(err.0.starts_with("snapshot: "), "{err}");
}

#[test]
fn the_notes_object_is_the_wasm_seq_id() {
    let notes = Notes::new(origin(8));
    let wasm = hashseq::wasm::WasmHashWeb::seq_id(&"08".repeat(32)).unwrap();
    let ours: String = notes.obj.0.iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(ours, wasm);
}

#[test]
fn bold_grows_at_its_end_only_on_every_replica() {
    let mut a = Notes::new(origin(10));
    let before = a.snapshot();
    let seeded = a.set_text("x **bold** y\n");
    // A second replica gets the bold from the frames, then types at both edges.
    let mut b = fold(origin(10), Some(&before), &seeded).unwrap();
    let clock = b.web.clock();
    b.insert(6, "B"); // right after "bold": inside
    b.insert(2, "A"); // right before "bold": outside
    let typed = hashseq::encoding::encode_delta(&b.web.deltas_for(&clock));
    assert_eq!(b.render().markdown, "x A**boldB** y\n");
    a.apply_frame(&typed).unwrap();
    assert_eq!(a.render().markdown, b.render().markdown);
    assert_eq!(a.snapshot(), b.snapshot());
}

#[test]
fn frames_since_bring_a_replica_to_the_union_with_artifacts() {
    let mut base = Notes::new(origin(7));
    base.set_text("alpha beta gamma\n");
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("alpha beta gamma\nlocal line\n");
    // The other replica comments on "beta": the mark kind `comment:<id>` is an artifact.
    let mut minted = Vec::new();
    b.mark_inline(
        6,
        10,
        "comment:0123456789ab",
        Some("thread"),
        false,
        &mut minted,
    );
    assert_eq!(minted.len(), 1);

    let had = a.clone();
    let before = had.web.clock();
    a.merge(b.clone());
    let frames = a.frames_since(&before, &had);
    assert_eq!(frames.len(), 2, "the comment's kind, then the delta");
    assert_eq!(frames[0][0], ARTIFACT_TAG);
    assert_eq!(frames[1][0], DELTA_TAG);
    let mut client = Notes::from_snapshot(origin(7), &had.snapshot()).unwrap();
    for f in &frames {
        client.apply_frame(f).unwrap();
    }
    assert_eq!(client.snapshot(), a.snapshot());
    assert_eq!(client.comments(), a.comments());
    assert!(client.comments()["0123456789ab"].is_some());

    // Nothing new: no frames.
    let had = a.clone();
    let before = had.web.clock();
    a.merge(b);
    assert!(a.frames_since(&before, &had).is_empty());
}

fn ids(doc: &Notes, from: usize, to: usize) -> (hashseq::Id, hashseq::Id) {
    (doc.seq().id_at(from).unwrap(), doc.seq().id_at(to).unwrap())
}

#[test]
fn mark_comment_marks_the_range_between_two_element_ids() {
    let mut server = Notes::new(origin(9));
    server.set_text("alpha beta gamma\nsecond line\n");
    let snap = server.snapshot();
    let mut client = Notes::from_snapshot(origin(9), &snap).unwrap();
    // "beta" is characters 6..10; the reader's client sends the ids of 6 and 9.
    let (first, last) = ids(&server, 6, 9);
    let frames = server.mark_comment(&first, &last, "0123456789ab").unwrap();
    assert_eq!(
        frames.len(),
        2,
        "the comment's kind is an artifact, then the delta"
    );
    assert_eq!(frames[0][0], ARTIFACT_TAG);
    assert_eq!(frames[1][0], DELTA_TAG);
    assert_eq!(server.comments()["0123456789ab"], Some((6, 10)));
    // The frames bring another replica to the same state.
    for f in &frames {
        client.apply_frame(f).unwrap();
    }
    assert_eq!(client.snapshot(), server.snapshot());
    assert_eq!(client.comments()["0123456789ab"], Some((6, 10)));
    assert_eq!(client.render().markdown, "alpha beta gamma\nsecond line\n");
    // Closed at both ends: text typed next to the range is outside it.
    client.insert(10, "!");
    client.insert(6, "<");
    assert_eq!(client.comments()["0123456789ab"], Some((7, 11)));
    // A single character, and a range over a line break.
    let (a, b) = ids(&server, 0, 0);
    server.mark_comment(&a, &b, "aaaaaaaaaaaa").unwrap();
    let (a, b) = ids(&server, 13, 19);
    server.mark_comment(&a, &b, "bbbbbbbbbbbb").unwrap();
    let c = server.comments();
    assert_eq!(c["aaaaaaaaaaaa"], Some((0, 1)));
    assert_eq!(c["bbbbbbbbbbbb"], Some((13, 20)));
}

#[test]
fn mark_comment_refuses_unknown_deleted_or_reversed_ids() {
    let mut doc = Notes::new(origin(9));
    doc.set_text("alpha beta gamma\n");
    let (first, last) = ids(&doc, 6, 9);
    doc.remove(9, 1); // the last character is deleted
    let err = doc.mark_comment(&first, &last, "c1").unwrap_err();
    assert!(err.0.starts_with("last: "), "{err}");
    assert!(doc.comments().is_empty(), "nothing was marked");
    let stranger = hashseq::Id([7; 32]);
    assert!(doc.mark_comment(&stranger, &first, "c1").is_err());
    let (a, b) = ids(&doc, 2, 5);
    assert_eq!(
        doc.mark_comment(&b, &a, "c1").unwrap_err().0,
        "last is before first"
    );
    // A deleted first character fails too.
    doc.remove(2, 1);
    assert!(doc
        .mark_comment(&a, &b, "c1")
        .unwrap_err()
        .0
        .starts_with("first: "));
}
