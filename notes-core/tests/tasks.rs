//! Task lines in every `notes doc` answer, and the `set_kind` op. Every note here is synthetic.

mod common;

use base64::{engine::general_purpose::STANDARD, Engine};
use common::origin;
use hashseq::Id;
use notes_core::api::handle;
use notes_core::model::Block;
use notes_core::{id_to_hex, Notes};
use serde_json::{json, Value};

const BASE: &str = "# Plan\n\n- [ ] Ana: draft the [deck](https://example.com/q3/deck) today\n- [x] Ben: 日本 [é](u) and [two](v)[three](w)\n- plain bullet\n- [ ] \n";

fn b64(bytes: &[u8]) -> String {
    STANDARD.encode(bytes)
}

fn frames_b64(frames: &[Vec<u8>]) -> Vec<String> {
    frames.iter().map(|f| b64(f)).collect()
}

fn hex(o: u8) -> String {
    id_to_hex(&origin(o))
}

fn call(request: Value) -> Result<Value, String> {
    handle(request).map_err(|e| e.0)
}

fn tasks(answer: &Value) -> &Vec<Value> {
    answer["tasks"].as_array().expect("tasks in the answer")
}

/// A replica from `snapshot` that makes `edits` one after the other, giving each one's frames.
fn log(snapshot: &[u8], o: u8, edits: &[&str]) -> Vec<Vec<Vec<u8>>> {
    let mut replica = Notes::from_snapshot(origin(o), snapshot).unwrap();
    edits.iter().map(|md| replica.set_text(md)).collect()
}

fn base_snapshot(o: u8) -> Vec<u8> {
    let mut doc = Notes::new(origin(o));
    doc.set_text(BASE);
    doc.snapshot()
}

#[test]
fn fold_lists_task_lines_in_order_with_text_and_links_in_code_points() {
    let snap = base_snapshot(1);
    let answer =
        call(json!({"op":"fold","notes":hex(1),"snapshot":b64(&snap),"frames":[]})).unwrap();
    let t = tasks(&answer);
    assert_eq!(t.len(), 3, "bullets and headings are not tasks: {t:?}");
    assert_eq!(t[0]["done"], false);
    assert_eq!(t[0]["text"], "Ana: draft the deck today");
    assert_eq!(
        t[0]["links"],
        json!([[15, 19, "https://example.com/q3/deck"]])
    );
    assert_eq!(t[1]["done"], true);
    assert_eq!(t[1]["text"], "Ben: 日本 é and twothree");
    // 日本 is two code points (not 6 bytes or 2 UTF-16 units of anything else); adjacent
    // marks with different URLs stay two spans.
    assert_eq!(
        t[1]["links"],
        json!([[8, 9, "u"], [14, 17, "v"], [17, 22, "w"]])
    );
    assert_eq!(t[2]["done"], false);
    assert_eq!(t[2]["text"], "");
    assert_eq!(t[2]["links"], json!([]));
    for task in t {
        assert_eq!(task["line"].as_str().unwrap().len(), 64);
        assert!(task["frame"].is_null(), "all from the snapshot: {task}");
    }
    // `line` is the terminator's element id: the character before it is the line's last.
    let doc = Notes::from_snapshot(origin(1), &snap).unwrap();
    let text: Vec<char> = doc.text().chars().collect();
    let nl_positions: Vec<usize> = text
        .iter()
        .enumerate()
        .filter(|(_, c)| **c == '\n')
        .map(|(i, _)| i)
        .collect();
    let want = [nl_positions[2], nl_positions[3], nl_positions[5]];
    for (task, pos) in t.iter().zip(want) {
        assert_eq!(task["line"], id_to_hex(&doc.seq().id_at(pos).unwrap()));
    }
}

#[test]
fn a_document_without_tasks_has_an_empty_list() {
    let answer = call(json!({"op":"fold","notes":hex(1),"snapshot":null,"frames":[]})).unwrap();
    assert_eq!(answer["tasks"], json!([]));
    let mut doc = Notes::new(origin(1));
    doc.set_text("# Only a heading\n\nand text\n");
    let answer =
        call(json!({"op":"fold","notes":hex(1),"snapshot":b64(&doc.snapshot()),"frames":[]}))
            .unwrap();
    assert_eq!(answer["tasks"], json!([]));
}

#[test]
fn frame_is_the_latest_request_frame_that_added_the_line_or_its_kind() {
    const BASE: &str = "# Plan\n- [ ] Ana: draft\n- [x] Ben: review\n- plain bullet\n";
    let mut seed = Notes::new(origin(2));
    seed.set_text(BASE);
    let snap = seed.snapshot();
    let new_line = format!("{BASE}- [ ] Cho: send the notes\n");
    let flipped = new_line.replacen("- [ ] Ana", "- [x] Ana", 1);
    let ticked = flipped.replacen("- [ ] Cho", "- [x] Cho", 1);
    // Frame group 0 adds a line, group 1 ticks an old one, group 2 ticks the new one.
    let groups = log(&snap, 2, &[&new_line, &flipped, &ticked]);
    assert!(groups.iter().all(|g| !g.is_empty()));
    let mut frames = Vec::new();
    let mut at = Vec::new(); // the index of each group's last frame
    for g in &groups {
        frames.extend(g.iter().cloned());
        at.push(frames.len() - 1);
    }
    let answer = call(
        json!({"op":"fold","notes":hex(2),"snapshot":b64(&snap),"frames":frames_b64(&frames)}),
    )
    .unwrap();
    let t = tasks(&answer);
    assert_eq!(t.len(), 3);
    let frame = |i: usize| t[i]["frame"].as_u64().map(|n| n as usize);
    // Ana's terminator is in the snapshot; the tick is in group 1.
    assert_eq!(t[0]["done"], true);
    assert_eq!(frame(0), Some(at[1]));
    // Ben is untouched: older than every frame.
    assert_eq!(frame(1), None);
    // Cho's line was typed in group 0 and ticked in group 2: the later one.
    assert_eq!(t[2]["text"], "Cho: send the notes");
    assert_eq!(t[2]["done"], true);
    assert_eq!(frame(2), Some(at[2]));
    // Without the tick the line's own frames give the index (the group's delta).
    let answer = call(json!({
        "op":"fold","notes":hex(2),"snapshot":b64(&snap),"frames":frames_b64(&groups[0])
    }))
    .unwrap();
    let t = tasks(&answer);
    assert_eq!(t[2]["done"], false);
    assert_eq!(
        t[2]["frame"].as_u64().map(|n| n as usize),
        Some(groups[0].len() - 1)
    );
    // The same frames twice: the first delivery counts.
    let twice: Vec<Vec<u8>> = groups[0].iter().chain(&groups[0]).cloned().collect();
    let answer =
        call(json!({"op":"fold","notes":hex(2),"snapshot":b64(&snap),"frames":frames_b64(&twice)}))
            .unwrap();
    assert_eq!(
        tasks(&answer)[2]["frame"].as_u64().map(|n| n as usize),
        Some(groups[0].len() - 1)
    );
}

#[test]
fn without_a_snapshot_every_line_comes_from_a_frame() {
    let mut a = Notes::new(origin(3));
    let first = a.set_text("- [ ] one\n");
    let second = a.set_text("- [ ] one\n- [x] two\n");
    let frames: Vec<Vec<u8>> = first.iter().chain(&second).cloned().collect();
    let answer =
        call(json!({"op":"fold","notes":hex(3),"snapshot":null,"frames":frames_b64(&frames)}))
            .unwrap();
    let t = tasks(&answer);
    assert_eq!(t.len(), 2);
    assert_eq!(t[0]["frame"], first.len() as u64 - 1);
    assert_eq!(t[1]["frame"], frames.len() as u64 - 1);
}

#[test]
fn every_op_answers_with_tasks() {
    let snap = base_snapshot(4);
    let doc = Notes::from_snapshot(origin(4), &snap).unwrap();
    let md = BASE.replace("plain bullet", "plain bullet again");
    let set_text = call(json!({
        "op":"set_text","notes":hex(4),"snapshot":b64(&snap),"frames":[],"markdown":md
    }))
    .unwrap();
    assert_eq!(tasks(&set_text).len(), 3);
    // A line the request itself makes has no request frame.
    let set_text = call(json!({
        "op":"set_text","notes":hex(4),"snapshot":b64(&snap),"frames":[],
        "markdown":format!("{BASE}- [ ] fresh\n")
    }))
    .unwrap();
    let t = tasks(&set_text);
    assert_eq!(t.len(), 4);
    assert!(t[3]["frame"].is_null());
    let comment = call(json!({
        "op":"mark_comment","notes":hex(4),"snapshot":b64(&snap),"frames":[],
        "first":id_to_hex(&doc.seq().id_at(0).unwrap()),
        "last":id_to_hex(&doc.seq().id_at(1).unwrap()),"comment":"c1c1c1c1c1c1"
    }))
    .unwrap();
    assert_eq!(tasks(&comment).len(), 3);
    // merge: the union's lines are older than the request's frames.
    let groups = log(
        &snap,
        4,
        &["- [ ] Ana: draft the [deck](https://example.com/q3/deck) today\n"],
    );
    let other = {
        let mut o = Notes::from_snapshot(origin(4), &snap).unwrap();
        o.set_text(&format!("{BASE}- [ ] from the other side\n"));
        o.snapshot()
    };
    let merged = call(json!({
        "op":"merge","notes":hex(4),"snapshot":b64(&snap),"other":b64(&other),
        "frames":frames_b64(&groups[0])
    }))
    .unwrap();
    assert!(!tasks(&merged).is_empty());
    assert!(tasks(&merged)
        .iter()
        .any(|t| t["text"] == "from the other side" && t["frame"].is_null()));
}

#[test]
fn set_kind_flips_a_task_and_the_frames_reach_another_replica() {
    let snap = base_snapshot(5);
    let answer =
        call(json!({"op":"fold","notes":hex(5),"snapshot":b64(&snap),"frames":[]})).unwrap();
    let line = answer["tasks"][0]["line"].as_str().unwrap().to_string();
    let done = call(json!({
        "op":"set_kind","notes":hex(5),"snapshot":b64(&snap),"frames":[],
        "line":line,"kind":"task_done"
    }))
    .unwrap();
    let t = tasks(&done);
    assert_eq!(t[0]["done"], true);
    assert_eq!(t[0]["line"], line);
    assert!(
        done["markdown"].as_str().unwrap().contains("- [x] Ana"),
        "{}",
        done["markdown"]
    );
    for key in ["snapshot", "plain", "comments"] {
        assert!(done.get(key).is_some(), "{key}");
    }
    let frames: Vec<Vec<u8>> = done["frames"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| STANDARD.decode(f.as_str().unwrap()).unwrap())
        .collect();
    assert!(!frames.is_empty());
    // Another replica applies them and agrees.
    let mut other = Notes::from_snapshot(origin(5), &snap).unwrap();
    for f in &frames {
        other.apply_frame(f).unwrap();
    }
    assert_eq!(b64(&other.snapshot()), done["snapshot"].as_str().unwrap());
    // The same kind again: nothing to send.
    let again = call(json!({
        "op":"set_kind","notes":hex(5),"snapshot":done["snapshot"],"frames":[],
        "line":line,"kind":"task_done"
    }))
    .unwrap();
    assert_eq!(again["frames"], json!([]));
    assert_eq!(again["snapshot"], done["snapshot"]);
    // And back to open, from the request's own frames instead of a snapshot.
    let back = call(json!({
        "op":"set_kind","notes":hex(5),"snapshot":b64(&snap),
        "frames":frames_b64(&frames),"line":line,"kind":"task"
    }))
    .unwrap();
    assert_eq!(tasks(&back)[0]["done"], false);
    assert!(!back["frames"].as_array().unwrap().is_empty());
    // The line's kind is now the request's own doing: no request frame.
    assert!(tasks(&back)[0]["frame"].is_null());
    // Folding the frames alone names the one that carried the mark.
    let folded = call(json!({
        "op":"fold","notes":hex(5),"snapshot":b64(&snap),"frames":frames_b64(&frames)
    }))
    .unwrap();
    assert_eq!(
        tasks(&folded)[0]["frame"].as_u64().map(|n| n as usize),
        Some(frames.len() - 1)
    );
}

#[test]
fn set_kind_resolves_concurrent_task_and_task_done() {
    // A line that is a bullet; two replicas make it a task and a done task at once.
    let mut base = Notes::new(origin(6));
    base.set_text("- a bullet\n- another\n");
    let snap = base.snapshot();
    let nl = base.text().chars().position(|c| c == '\n').unwrap();
    let line = base.seq().id_at(nl).unwrap();
    let mut a = Notes::from_snapshot(origin(6), &snap).unwrap();
    let mut b = Notes::from_snapshot(origin(6), &snap).unwrap();
    a.mark_line(nl, "block", Some("task"), &mut Vec::new());
    b.mark_line(nl, "block", Some("task_done"), &mut Vec::new());
    a.merge(b);
    assert_eq!(a.tasks().len(), 1);
    assert!(a.tasks()[0].done, "both live shows done");
    assert_eq!(a.tasks()[0].line, line);
    // Opening it replaces both marks, so it stays open.
    let frames = a.set_kind(&line, Block::Task).unwrap();
    assert!(!frames.is_empty());
    assert!(!a.tasks()[0].done);
    // Ticking an already-done line (both live) is a no-op.
    let mut c = Notes::from_snapshot(origin(6), &snap).unwrap();
    c.mark_line(nl, "block", Some("task"), &mut Vec::new());
    let mut d = Notes::from_snapshot(origin(6), &snap).unwrap();
    d.mark_line(nl, "block", Some("task_done"), &mut Vec::new());
    c.merge(d);
    assert_eq!(
        c.set_kind(&line, Block::TaskDone).unwrap(),
        Vec::<Vec<u8>>::new()
    );
}

#[test]
fn set_kind_errors() {
    let snap = base_snapshot(7);
    let doc = Notes::from_snapshot(origin(7), &snap).unwrap();
    let text: Vec<char> = doc.text().chars().collect();
    let nls: Vec<usize> = text
        .iter()
        .enumerate()
        .filter(|(_, c)| **c == '\n')
        .map(|(i, _)| i)
        .collect();
    let req = |line: String, kind: &str| {
        call(json!({
            "op":"set_kind","notes":hex(7),"snapshot":b64(&snap),"frames":[],
            "line":line,"kind":kind
        }))
    };
    let id = |pos: usize| id_to_hex(&doc.seq().id_at(pos).unwrap());
    // Unknown id.
    let err = req(id_to_hex(&Id([9; 32])), "task").unwrap_err();
    assert!(err.starts_with("line: "), "{err}");
    // A bullet and a heading are not tasks.
    let err = req(id(nls[4]), "task").unwrap_err();
    assert_eq!(err, "line: not a task");
    assert_eq!(
        req(id(nls[0]), "task_done").unwrap_err(),
        "line: not a task"
    );
    // A character that is not a line end.
    let err = req(id(3), "task").unwrap_err();
    assert_eq!(err, "line: not the end of a line");
    // Bad kinds and ids.
    assert!(req(id(nls[2]), "bullet").unwrap_err().starts_with("kind: "));
    assert!(req(id(nls[2]), "").unwrap_err().starts_with("kind: "));
    assert!(req("zz".into(), "task").unwrap_err().starts_with("line: "));
    // A deleted terminator is unknown now.
    let mut gone = Notes::from_snapshot(origin(7), &snap).unwrap();
    gone.remove(nls[2], 1);
    let err = call(json!({
        "op":"set_kind","notes":hex(7),"snapshot":b64(&gone.snapshot()),"frames":[],
        "line":id(nls[2]),"kind":"task_done"
    }))
    .unwrap_err();
    assert!(err.starts_with("line: "), "{err}");
    // Nothing was changed by the failures: the doc API refuses the same way.
    let mut direct = Notes::from_snapshot(origin(7), &snap).unwrap();
    assert!(direct
        .set_kind(&doc.seq().id_at(nls[4]).unwrap(), Block::Task)
        .is_err());
    assert_eq!(b64(&direct.snapshot()), b64(&snap));
    assert!(direct
        .set_kind(&doc.seq().id_at(nls[2]).unwrap(), Block::Bullet)
        .is_err());
}
