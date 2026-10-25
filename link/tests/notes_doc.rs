//! Tests for `notes doc` (the live shared notes document), driving the binary. Every note
//! here is synthetic. Client frames are made with notes-core, as the browser's wasm does.

use std::io::Write;
use std::process::{Command, Stdio};

use base64::{engine::general_purpose::STANDARD, Engine};
use notes_core::Notes;
use serde_json::{json, Value};

/// sha256("mnemosyne/notes/" + session_id) in the backend; any 32 bytes here.
const NOTES: &str = "4d4e4f5445532d544553542d4f524947494e2d30303030303030303030303031";

fn origin() -> hashseq::Id {
    notes_core::id_from_hex(NOTES).unwrap()
}

fn run(input: &str) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_mnemosyne-link"))
        .args(["notes", "doc"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

fn reply(request: Value) -> Value {
    let out = run(&request.to_string());
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert_eq!(stdout.lines().count(), 1, "{stdout}");
    serde_json::from_str(&stdout).unwrap()
}

fn b64(frames: &[Vec<u8>]) -> Vec<String> {
    frames.iter().map(|f| STANDARD.encode(f)).collect()
}

#[test]
fn fold_applies_client_frames_to_a_snapshot() {
    // Two clients typing: one seeds the notes, the other adds a line with a long link.
    let mut a = Notes::new(origin());
    let first = a.set_text("# Notes\n- [ ] Ana: send the deck\n");
    let mut b = a.clone();
    let second =
        b.set_text("# Notes\n- [ ] Ana: send the deck\n- see [the deck](https://example.com/decks/q3-planning)\n");
    assert_eq!(second.len(), 2, "an artifact and a delta");

    let folded = reply(json!({
        "op": "fold", "notes": NOTES, "snapshot": null,
        "frames": b64(&[first.clone(), second.clone(), first].concat()),
    }));
    assert_eq!(
        folded["markdown"],
        "# Notes\n- [ ] Ana: send the deck\n- see [the deck](https://example.com/decks/q3-planning)\n"
    );
    assert_eq!(folded["plain"], "Notes\nAna: send the deck\nsee the deck\n");
    assert_eq!(folded["comments"], json!({}));
    assert_eq!(folded["snapshot"], STANDARD.encode(b.snapshot()));

    // The task line came in the first request frame.
    assert_eq!(folded["tasks"][0]["frame"], json!(0));

    // Folding nothing more onto the snapshot changes nothing, except that the task line now
    // comes from the snapshot, not from a frame of this request.
    let mut again = reply(json!({
        "op": "fold", "notes": NOTES, "snapshot": folded["snapshot"], "frames": [],
    }));
    assert_eq!(again["tasks"][0]["frame"], Value::Null);
    again["tasks"][0]["frame"] = json!(0);
    assert_eq!(again, folded);
}

#[test]
fn set_text_returns_frames_for_the_clients() {
    let start = reply(json!({"op": "fold", "notes": NOTES, "snapshot": null, "frames": []}));
    assert_eq!(start["markdown"], "");
    let md = "Agenda\n\n1. budget\n2. [hiring plan](https://example.com/hiring/plan-2026)\n";
    let set = reply(json!({
        "op": "set_text", "notes": NOTES, "snapshot": start["snapshot"], "frames": [],
        "markdown": md,
    }));
    assert_eq!(set["markdown"], md);
    let frames: Vec<Vec<u8>> = set["frames"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| STANDARD.decode(f.as_str().unwrap()).unwrap())
        .collect();
    assert_eq!(frames[0][0], 0xAF);
    assert_eq!(frames.last().unwrap()[0], 0xDE);

    // A client holding the old snapshot applies the frames and sees the same notes.
    let mut client = Notes::from_snapshot(
        origin(),
        &STANDARD
            .decode(start["snapshot"].as_str().unwrap())
            .unwrap(),
    )
    .unwrap();
    for f in &frames {
        client.apply_frame(f).unwrap();
    }
    assert_eq!(client.render().markdown, md);
    assert_eq!(set["snapshot"], STANDARD.encode(client.snapshot()));

    // The same markdown again: nothing to send.
    let same = reply(json!({
        "op": "set_text", "notes": NOTES, "snapshot": set["snapshot"], "frames": [],
        "markdown": md,
    }));
    assert_eq!(same["frames"], json!([]));
    assert_eq!(same["snapshot"], set["snapshot"]);
}

#[test]
fn merge_is_a_union_in_either_order() {
    let mut base = Notes::new(origin());
    base.set_text("Shared line\n");
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("Shared line\nlaptop edit\n");
    b.set_text("Shared line\ndesktop edit\n");
    let (sa, sb) = (STANDARD.encode(a.snapshot()), STANDARD.encode(b.snapshot()));
    let ab = reply(json!({"op": "merge", "notes": NOTES, "snapshot": sa, "other": sb}));
    let ba = reply(json!({"op": "merge", "notes": NOTES, "snapshot": sb, "other": sa}));
    // The union is the same; the frames are what each side lacked, so they differ.
    for key in ["snapshot", "markdown", "plain", "comments"] {
        assert_eq!(ab[key], ba[key], "{key}");
    }
    let md = ab["markdown"].as_str().unwrap();
    assert!(
        md.contains("laptop edit\n") && md.contains("desktop edit\n"),
        "{md}"
    );
}

fn frames_of(answer: &Value) -> Vec<Vec<u8>> {
    answer["frames"]
        .as_array()
        .expect("a merge answers with frames")
        .iter()
        .map(|f| STANDARD.decode(f.as_str().unwrap()).unwrap())
        .collect()
}

#[test]
fn merge_frames_bring_the_local_replica_to_the_union() {
    // Both sides have the base; each adds a line, one with a long link (an artifact).
    let mut base = Notes::new(origin());
    base.set_text("Shared line\n");
    let (mut a, mut b) = (base.clone(), base.clone());
    a.set_text("Shared line\nlaptop edit\n");
    b.set_text(
        "Shared line\ndesktop edit [the deck](https://example.com/decks/q3-planning-long)\n",
    );
    let (sa, sb) = (STANDARD.encode(a.snapshot()), STANDARD.encode(b.snapshot()));

    for (local, local_snapshot, other_snapshot, lacks_link) in
        [(&a, &sa, &sb, true), (&b, &sb, &sa, false)]
    {
        let merged = reply(json!({
            "op": "merge", "notes": NOTES, "snapshot": local_snapshot, "other": other_snapshot,
        }));
        let frames = frames_of(&merged);
        assert_eq!(frames.last().unwrap()[0], 0xDE, "a delta last");
        // A client holding the local document, given the frames, shows the union.
        let mut client = Notes::from_snapshot(origin(), &local.snapshot()).unwrap();
        for frame in &frames {
            client.apply_frame(frame).unwrap();
        }
        assert_eq!(
            client.snapshot(),
            STANDARD
                .decode(merged["snapshot"].as_str().unwrap())
                .unwrap()
        );
        assert_eq!(
            client.render().markdown,
            merged["markdown"].as_str().unwrap()
        );
        // The artifact for the link comes along when the other side's text is the new one.
        if lacks_link {
            assert_eq!(frames.len(), 2, "the link's URL travels as an artifact");
            assert_eq!(frames[0][0], 0xAF);
        } else {
            assert_eq!(frames.len(), 1);
        }
    }
}

#[test]
fn merge_with_nothing_new_has_no_frames() {
    let mut doc = Notes::new(origin());
    doc.set_text("Same\n");
    let snap = STANDARD.encode(doc.snapshot());
    let same = reply(json!({"op": "merge", "notes": NOTES, "snapshot": snap, "other": snap}));
    assert_eq!(same["frames"], json!([]));
    // The other side only has less: still nothing for the local one.
    let empty = STANDARD.encode(Notes::new(origin()).snapshot());
    let less = reply(json!({"op": "merge", "notes": NOTES, "snapshot": snap, "other": empty}));
    assert_eq!(less["frames"], json!([]));
    assert_eq!(less["snapshot"], same["snapshot"]);
}

#[test]
fn comment_ranges_are_reported() {
    let mut doc = Notes::new(origin());
    let mut frames = doc.set_text("alpha beta gamma\n");
    let mut minted = Vec::new();
    let before = doc.web.clock();
    doc.mark_inline(
        6,
        10,
        "comment:0123456789ab",
        Some("thread"),
        false,
        &mut minted,
    );
    doc.mark_inline(
        0,
        5,
        "comment:ba9876543210",
        Some("thread"),
        false,
        &mut minted,
    );
    for id in &minted {
        let mut f = vec![0xAF];
        f.extend_from_slice(doc.web.artifact_bytes(id).unwrap());
        frames.push(f);
    }
    frames.push(hashseq::encoding::encode_delta(
        &doc.web.deltas_for(&before),
    ));
    let folded =
        reply(json!({"op": "fold", "notes": NOTES, "snapshot": null, "frames": b64(&frames)}));
    assert_eq!(
        folded["comments"],
        json!({"0123456789ab": [6, 10], "ba9876543210": [0, 5]})
    );
    // An API write deletes "alpha": that comment is detached.
    let set = reply(json!({
        "op": "set_text", "notes": NOTES, "snapshot": folded["snapshot"], "frames": [],
        "markdown": "beta gamma\n",
    }));
    assert_eq!(
        set["comments"],
        json!({"0123456789ab": [0, 4], "ba9876543210": null})
    );
}

#[test]
fn errors_go_to_stderr_with_exit_1() {
    let mut doc = Notes::new(origin());
    let good = STANDARD.encode(&doc.set_text("x\n")[0]);
    for (input, message) in [
        ("not json".to_string(), "invalid request"),
        (
            json!({"op": "nope", "notes": NOTES}).to_string(),
            "invalid request",
        ),
        (
            json!({"op": "fold", "notes": "abc", "snapshot": null, "frames": []}).to_string(),
            "notes:",
        ),
        (
            json!({"op": "fold", "notes": NOTES, "snapshot": "!!", "frames": []}).to_string(),
            "snapshot: not base64",
        ),
        (
            json!({"op": "fold", "notes": NOTES, "snapshot": null,
                   "frames": [good, STANDARD.encode([0xDE, 1, 2, 3])]})
            .to_string(),
            "frame 1:",
        ),
        (
            json!({"op": "merge", "notes": NOTES, "snapshot": STANDARD.encode(b"junk"),
                   "other": STANDARD.encode(b"junk")})
            .to_string(),
            "snapshot:",
        ),
    ] {
        let out = run(&input);
        assert_eq!(out.status.code(), Some(1), "{input}");
        assert!(out.stdout.is_empty(), "{input}");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(stderr.contains(message), "{input}: {stderr}");
    }
}

#[test]
fn mark_comment_marks_a_range_for_a_reader() {
    let mut a = Notes::new(origin());
    let seeded = a.set_text("Our launch plan is set\nSecond line\n");
    let (h4, h14) = (
        notes_core::id_to_hex(&a.seq().id_at(4).unwrap()),
        notes_core::id_to_hex(&a.seq().id_at(14).unwrap()),
    );
    let request = json!({
        "op": "mark_comment", "notes": NOTES, "snapshot": null, "frames": b64(&seeded),
        "first": h4, "last": h14, "comment": "c0ffee123456",
    });
    let marked = reply(request.clone());
    assert_eq!(marked["comments"], json!({"c0ffee123456": [4, 15]}));
    assert_eq!(marked["plain"], "Our launch plan is set\nSecond line\n");
    let frames = marked["frames"].as_array().unwrap();
    assert_eq!(frames.len(), 2, "the mark's kind, then the delta");
    // The frames bring the editor's replica to the answer's snapshot.
    let mut client = a.clone();
    for f in frames {
        client
            .apply_frame(&STANDARD.decode(f.as_str().unwrap()).unwrap())
            .unwrap();
    }
    assert_eq!(STANDARD.encode(client.snapshot()), marked["snapshot"]);

    // A deleted character is an error naming it.
    a.remove(14, 1);
    let gone = a.snapshot();
    let out = run(&json!({
        "op": "mark_comment", "notes": NOTES, "snapshot": STANDARD.encode(gone), "frames": [],
        "first": h4, "last": h14, "comment": "c0ffee123456",
    })
    .to_string());
    assert!(!out.status.success());
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(err.contains("last: no such character"), "{err}");
}
