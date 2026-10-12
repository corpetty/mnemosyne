//! Tests for `notes merge`. Every note here is synthetic.

use std::io::Write;
use std::process::{Command, Stdio};

use base64::{engine::general_purpose::STANDARD, Engine};
use mnemosyne_link::notes::{merge, text_of};

const SPEAKERS: [&str; 3] = ["Ana", "Ben", "Cho"];

/// The transcript: 40 lines, blank-line separated. `ben` is how Ben is named; `fixed` says
/// whether line 5 (Cho's) has its typo fixed.
fn transcript(ben: &str, fixed: bool) -> String {
    let mut out = String::new();
    for i in 0..40 {
        let secs = 11 + i * 7;
        let speaker = match SPEAKERS[i % 3] {
            "Ben" => ben,
            other => other,
        };
        let word = if i == 5 && !fixed {
            "reveiwed"
        } else {
            "reviewed"
        };
        out.push_str(&format!(
            "**[{:02}:{:02}] {speaker}:** we {word} the numbers for item {i} and moved on\n\n",
            secs / 60,
            secs % 60
        ));
    }
    out
}

struct Parts<'a> {
    topics: &'a str,
    tags: &'a str,
    summary: &'a str,
    decisions: &'a str,
    actions: &'a str,
    notes: &'a str,
    ben: &'a str,
    fixed: bool,
}

fn note(p: &Parts) -> String {
    format!(
        "---\n\
         title: Quarterly Planning\n\
         date: 2026-03-02\n\
         source: mnemosyne\n\
         participants: [Ana, Ben, Cho]\n\
         topics: [{}]\n\
         tags: [{}]\n\
         ---\n\n\
         # Quarterly Planning\n\n\
         ## Summary\n\n{}\n\
         ## Decisions\n\n{}\n\
         ## Action Items\n\n{}\n\
         ## Notes\n\n{}\n\
         ## Transcript\n\n{}",
        p.topics,
        p.tags,
        p.summary,
        p.decisions,
        p.actions,
        p.notes,
        transcript(p.ben, p.fixed)
    )
}

const BASE_SUMMARY: &str = "The team reviewed the budget for the next quarter and agreed the hiring plan needs another pass.\n\n\
Ben raised concerns about the vendor contract renewal.\n";
const BASE_DECISIONS: &str = "- Freeze new vendor contracts until the review is done\n\
- Hire one backend engineer in April\n\
- Move the planning meeting to Tuesdays\n";
const BASE_ACTIONS: &str = "- [ ] Draft the revised hiring plan ([[Ana]])\n\
- [ ] Send the vendor comparison to finance ([[Ben]])\n\
- [ ] Book a room for the offsite ([[Cho]])\n";
const BASE_NOTES: &str = "Remember to bring the printed budget sheets.\n";

fn base() -> String {
    note(&Parts {
        topics: "budget, hiring",
        tags: "meeting",
        summary: BASE_SUMMARY,
        decisions: BASE_DECISIONS,
        actions: BASE_ACTIONS,
        notes: BASE_NOTES,
        ben: "Ben",
        fixed: false,
    })
}

const APP_SUMMARY: &str =
    "Participants went through next quarter's budget and settled on a revised hiring approach.\n\n\
The vendor contract renewal drew objections, mostly from Ben.\n";
const APP_DECISIONS: &str = "- Pause new vendor contracts pending the review\n\
- Hire a backend engineer in May\n\
- Move the planning meeting to Tuesdays\n";
const APP_ACTIONS: &str = "- [ ] Share the hiring plan draft with the team ([[Ana]])\n\
- [ ] Send the vendor shortlist to finance by Friday ([[Ben]])\n\
- [ ] Book a room for the offsite ([[Cho]])\n";

fn app_version() -> String {
    note(&Parts {
        topics: "budget, vendors",
        tags: "meeting",
        summary: APP_SUMMARY,
        decisions: APP_DECISIONS,
        actions: APP_ACTIONS,
        notes: BASE_NOTES,
        ben: "Ben Ortiz",
        fixed: false,
    })
}

const USER_SUMMARY_EXTRA: &str = "Follow up with legal before signing anything.\n";

fn vault_version() -> String {
    note(&Parts {
        topics: "budget, hiring",
        tags: "meeting, followup",
        summary: &format!("{BASE_SUMMARY}\n{USER_SUMMARY_EXTRA}"),
        decisions: "- Freeze new vendor contracts until the review is done\n\
- Hire one backend engineer in April, pending budget approval\n\
- Move the planning meeting to Tuesdays\n",
        actions: "- [ ] Draft the revised hiring plan ([[Ana]])\n\
- [ ] Send the vendor comparison to finance ([[Ben]])\n\
- [ ] Book a room for the offsite ([[Dee]])\n",
        notes: &format!("{BASE_NOTES}\nAsk Cho about the offsite catering.\n"),
        ben: "Ben",
        fixed: true,
    })
}

fn merged_expected() -> String {
    note(&Parts {
        topics: "budget, vendors",
        tags: "meeting, followup",
        summary: &format!("{APP_SUMMARY}\n{USER_SUMMARY_EXTRA}"),
        decisions: "- Pause new vendor contracts pending the review\n\
- Hire a backend engineer in May, pending budget approval\n\
- Move the planning meeting to Tuesdays\n",
        actions: "- [ ] Share the hiring plan draft with the team ([[Ana]])\n\
- [ ] Send the vendor shortlist to finance by Friday ([[Ben]])\n\
- [ ] Book a room for the offsite ([[Dee]])\n",
        notes: &format!("{BASE_NOTES}\nAsk Cho about the offsite catering.\n"),
        ben: "Ben Ortiz",
        fixed: true,
    })
}

fn first_export(text: &str) -> Vec<u8> {
    merge(None, text, None).unwrap().history
}

#[test]
fn resummarize_keeps_every_user_edit_and_every_app_change() {
    let base = base();
    let history = first_export(&base);
    assert_eq!(text_of(&history).unwrap(), base);

    let app = app_version();
    let vault = vault_version();
    let merged = merge(Some(&history), &app, Some(&vault)).unwrap();
    assert_eq!(merged.text, merged_expected());

    // Each user edit once.
    for edit in [
        "tags: [meeting, followup]",
        "Follow up with legal before signing anything.",
        "Ask Cho about the offsite catering.",
        "Book a room for the offsite ([[Dee]])",
        "we reviewed the numbers for item 5 and moved on",
        ", pending budget approval",
    ] {
        assert_eq!(merged.text.matches(edit).count(), 1, "{edit}");
    }
    assert!(!merged.text.contains("reveiwed"));
    assert!(!merged.text.contains("[[Cho]])"));
    // Each app change.
    for change in [
        "topics: [budget, vendors]",
        "Participants went through next quarter's budget",
        "The vendor contract renewal drew objections, mostly from Ben.",
        "- Pause new vendor contracts pending the review",
        "Share the hiring plan draft with the team ([[Ana]])",
        "Send the vendor shortlist to finance by Friday ([[Ben]])",
    ] {
        assert_eq!(merged.text.matches(change).count(), 1, "{change}");
    }
    assert_eq!(merged.text.matches("Ben Ortiz:**").count(), 13);
    assert!(!merged.text.contains("Ben:**"));
    // The stored history now holds the merged text.
    assert_eq!(text_of(&merged.history).unwrap(), merged.text);
}

#[test]
fn merging_is_the_same_in_both_orders() {
    let history = first_export(&base());
    let app = app_version();
    let vault = vault_version();
    let one = merge(Some(&history), &app, Some(&vault)).unwrap();
    let other = merge(Some(&history), &vault, Some(&app)).unwrap();
    assert_eq!(one.text, merged_expected());
    assert_eq!(other.text, one.text);
}

#[test]
fn an_unchanged_vault_gives_the_app_text() {
    let base = base();
    let history = first_export(&base);
    let app = app_version();
    let without = merge(Some(&history), &app, None).unwrap();
    assert_eq!(without.text, app);
    let same = merge(Some(&history), &app, Some(&base)).unwrap();
    assert_eq!(same.text, app);
    // And with no change at all.
    let nothing = merge(Some(&history), &base, Some(&base)).unwrap();
    assert_eq!(nothing.text, base);
}

#[test]
fn a_first_export_stores_the_text() {
    let text = "---\nsource: mnemosyne\n---\n\n# Naïve café — 会議 🎙\n";
    let merged = merge(None, text, None).unwrap();
    assert_eq!(merged.text, text);
    assert_eq!(text_of(&merged.history).unwrap(), text);
    assert!(merged.history.starts_with(b"MNHS\x01"));
    // Two first exports get different origins.
    assert_ne!(merged.history, merge(None, text, None).unwrap().history);
}

#[test]
fn non_ascii_text_merges_by_characters() {
    let history = first_export("Café ☕ meeting\n\nNotes: naïve plan 🎙 here\n");
    let app = "Café ☕ meeting — notes\n\nNotes: naïve plan 🎙 here\n";
    let vault = "Café ☕ meeting\n\nNotes: naïve plan 🎙 here, revisited 日本\n";
    let merged = merge(Some(&history), app, Some(vault)).unwrap();
    assert_eq!(
        merged.text,
        "Café ☕ meeting — notes\n\nNotes: naïve plan 🎙 here, revisited 日本\n"
    );
}

/// The app's third rendering: the second one with two more changes.
fn third_render(text: &str) -> String {
    text.replace(
        "topics: [budget, vendors]",
        "topics: [budget, vendors, offsite]",
    )
    .replace(
        "The vendor contract renewal drew objections, mostly from Ben.",
        "The vendor contract renewal drew objections.",
    )
}

#[test]
fn user_edits_survive_exports_that_do_not_merge_them() {
    // Round 1: export. The user edits the file (a paragraph, an owner, a tag, a word, a note).
    let history = first_export(&base());
    // Round 2: re-summarize, merging the user's edits.
    let round2 = merge(Some(&history), &app_version(), Some(&vault_version())).unwrap();
    assert_eq!(round2.text, merged_expected());
    // Round 3: the file is unchanged since round 2 (no vault version), the app changes more.
    let round3 = merge(Some(&round2.history), &third_render(&app_version()), None).unwrap();
    assert_eq!(round3.text, third_render(&merged_expected()));
    assert_eq!(text_of(&round3.history).unwrap(), round3.text);
    assert!(round3.text.contains(USER_SUMMARY_EXTRA));
    assert!(round3.text.contains("([[Dee]])"));
    assert!(round3.text.contains("topics: [budget, vendors, offsite]"));
    // The same when the caller passes the file it wrote (equal to the combined text).
    let again = merge(
        Some(&round2.history),
        &third_render(&app_version()),
        Some(&round2.text),
    )
    .unwrap();
    assert_eq!(again.text, round3.text);
    // Round 4: nothing changed at all.
    let round4 = merge(Some(&round3.history), &third_render(&app_version()), None).unwrap();
    assert_eq!(round4.text, round3.text);
}

#[test]
fn three_rounds_keep_all_user_edits() {
    // Round 1: export.
    let base1 = base();
    let mut history = first_export(&base1);
    assert_eq!(text_of(&history).unwrap(), base1);

    // The user edits the exported note.
    let edit1 = base1
        .replace("tags: [meeting]", "tags: [meeting, followup]")
        .replace(
            "## Notes\n\nRemember to bring the printed budget sheets.\n",
            "## Notes\n\nRemember to bring the printed budget sheets.\nAsk Cho about the offsite catering.\n",
        );
    assert_ne!(edit1, base1);

    // Round 2: the app re-renders with its changes.
    let round2 = merge(Some(&history), &app_version(), Some(&edit1)).unwrap();
    history = round2.history;
    assert_eq!(text_of(&history).unwrap(), round2.text);
    assert!(round2.text.contains("tags: [meeting, followup]"));
    assert!(round2.text.contains("Ask Cho about the offsite catering."));
    assert!(round2.text.contains("topics: [budget, vendors]"));
    assert!(round2.text.contains("Ben Ortiz:**"));

    // Round 3: the file is as written, the app changes more.
    let app3 = third_render(&app_version());
    let round3 = merge(Some(&history), &app3, None).unwrap();
    history = round3.history;
    assert_eq!(text_of(&history).unwrap(), round3.text);
    assert_eq!(round3.text, third_render(&round2.text));
    assert!(round3.text.contains("tags: [meeting, followup]"));
    assert!(round3.text.contains("Ask Cho about the offsite catering."));

    // More user edits on the file written in round 3.
    let edit2 = round3
        .text
        .replace(
            "Hire a backend engineer in May",
            "Hire a backend engineer in June",
        )
        .replace("([[Cho]])", "([[Cho]]), ask about parking");
    assert_ne!(edit2, round3.text);

    // Round 4: the app re-renders once more (a new decision line), merging those edits.
    let app4 = app3.replace(
        "- Move the planning meeting to Tuesdays\n",
        "- Move the planning meeting to Tuesdays\n- Review the budget in June\n",
    );
    let round4 = merge(Some(&history), &app4, Some(&edit2)).unwrap();
    history = round4.history;
    let expected = edit2.replace(
        "- Move the planning meeting to Tuesdays\n",
        "- Move the planning meeting to Tuesdays\n- Review the budget in June\n",
    );
    assert_eq!(round4.text, expected);
    assert_eq!(text_of(&history).unwrap(), expected);

    // Round 5: unchanged file, unchanged app: still all there.
    let round5 = merge(Some(&history), &app4, None).unwrap();
    assert_eq!(round5.text, expected);
    assert!(round5.text.contains("tags: [meeting, followup]"));
    assert!(round5.text.contains("Ask Cho about the offsite catering."));
    assert!(round5.text.contains("Hire a backend engineer in June"));
    assert!(round5.text.contains("([[Cho]]), ask about parking"));
}

#[test]
fn a_line_the_user_deleted_stays_deleted() {
    let base = base();
    let history = first_export(&base);
    let line = "- Move the planning meeting to Tuesdays\n";
    let edited = base.replace(line, "");
    assert_ne!(edited, base);
    // The app re-renders the same note (it still has the line); the user's file lacks it.
    let round2 = merge(Some(&history), &base, Some(&edited)).unwrap();
    assert_eq!(round2.text, edited);
    // And the next time, with the file as written.
    let round3 = merge(Some(&round2.history), &base, None).unwrap();
    assert_eq!(round3.text, edited);
    let round4 = merge(Some(&round3.history), &base, Some(&edited)).unwrap();
    assert_eq!(round4.text, edited);
}

#[test]
fn a_line_the_app_drops_disappears() {
    let base = base();
    let line = "- Move the planning meeting to Tuesdays\n";
    let history = first_export(&base);
    // The user adds a note first; it is merged in round 2.
    let user = base.replace(
        BASE_NOTES,
        &format!("{BASE_NOTES}\nAsk Cho about the offsite catering.\n"),
    );
    let round2 = merge(Some(&history), &base, Some(&user)).unwrap();
    assert_eq!(round2.text, user);
    // Round 3: the re-summarize no longer has the decision.
    let dropped = base.replace(line, "");
    let round3 = merge(Some(&round2.history), &dropped, None).unwrap();
    assert_eq!(round3.text, user.replace(line, ""));
    assert!(round3.text.contains("Ask Cho about the offsite catering."));
    // And when the app brings it back, it reappears.
    let round4 = merge(Some(&round3.history), &base, None).unwrap();
    assert_eq!(round4.text.matches("Move the planning meeting").count(), 1);
    assert!(round4.text.contains("Ask Cho about the offsite catering."));
}

#[test]
fn bad_histories_are_rejected() {
    let good = first_export("hello\n");
    let err = |h: &[u8]| merge(Some(h), "hello\n", None).err().unwrap().to_string();

    let mut bad_magic = good.clone();
    bad_magic[0] = b'X';
    assert!(err(&bad_magic).contains("magic"), "{}", err(&bad_magic));

    let mut bad_version = good.clone();
    bad_version[4] = 2;
    assert!(
        err(&bad_version).contains("version 2"),
        "{}",
        err(&bad_version)
    );

    assert!(err(b"").contains("magic"));
    assert!(err(b"MNHS").contains("truncated"));
    assert!(err(b"MNHS\x01").contains("truncated"));
    assert!(err(b"MNHS\x01\x01\x00").contains("truncated"));
    // A branch length past the end of the history.
    assert!(err(b"MNHS\x01\xff\xff\x00\x00garbage").contains("truncated"));
    // Branches that do not decode.
    let mut junk = b"MNHS\x01".to_vec();
    junk.extend_from_slice(&7u32.to_le_bytes());
    junk.extend_from_slice(b"garbage");
    assert!(err(&junk).contains("corrupt"), "{}", err(&junk));
    // Branches with different origins.
    let other = first_export("hello\n");
    let split = |h: &[u8]| 9 + u32::from_le_bytes(h[5..9].try_into().unwrap()) as usize;
    let mut mixed = good[..split(&good)].to_vec();
    mixed.extend_from_slice(&other[split(&other)..]);
    assert!(err(&mixed).contains("origins"), "{}", err(&mixed));
    // A history with the app branch cut off.
    assert!(err(&good[..split(&good)]).contains("corrupt"));
    assert!(text_of(&bad_magic).is_err());
}

#[test]
fn a_vault_version_without_a_history_is_rejected() {
    assert!(merge(None, "app\n", Some("vault\n")).is_err());
}

#[test]
fn identical_concurrent_edits_converge_when_made_the_same_way() {
    // Both sides make the same edit the same way (the backend also normalizes checkboxes
    // before the call, so ticks never differ). Content-addressed operations merge into one.
    let history = first_export("- [ ] Send the invoice\n");
    let both = "- [x] Send the invoice\n";
    let merged = merge(Some(&history), both, Some(both)).unwrap();
    assert_eq!(merged.text, both);
}

fn run(input: &str) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_mnemosyne-link"))
        .args(["notes", "merge"])
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

fn reply(out: &std::process::Output) -> serde_json::Value {
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8(out.stdout.clone()).unwrap();
    assert_eq!(stdout.lines().count(), 1, "{stdout}");
    serde_json::from_str(&stdout).unwrap()
}

#[test]
fn the_binary_merges_json_on_stdin() {
    let base = base();
    let first = reply(&run(
        &serde_json::json!({"history": null, "app": base, "vault": null}).to_string(),
    ));
    assert_eq!(first["text"], base);
    let history = first["history"].as_str().unwrap().to_string();
    let bytes = STANDARD.decode(&history).unwrap();
    assert_eq!(text_of(&bytes).unwrap(), base);

    let second = reply(&run(&serde_json::json!({
        "history": history,
        "app": app_version(),
        "vault": vault_version(),
    })
    .to_string()));
    assert_eq!(second["text"], merged_expected());
    let bytes = STANDARD
        .decode(second["history"].as_str().unwrap())
        .unwrap();
    assert_eq!(text_of(&bytes).unwrap(), merged_expected());
}

#[test]
fn the_binary_fails_with_nothing_on_stdout() {
    let history = STANDARD.encode(first_export("x\n"));
    for input in [
        "not json".to_string(),
        "".to_string(),
        r#"{"history": null}"#.to_string(),
        r#"{"history": "!!!not base64", "app": "a", "vault": null}"#.to_string(),
        r#"{"history": null, "app": "a", "vault": "b"}"#.to_string(),
        serde_json::json!({"history": STANDARD.encode(b"junk"), "app": "a", "vault": null})
            .to_string(),
        serde_json::json!({"history": history, "app": 5, "vault": null}).to_string(),
    ] {
        let out = run(&input);
        assert_eq!(out.status.code(), Some(1), "{input}");
        assert!(out.stdout.is_empty(), "{input}");
        assert!(!out.stderr.is_empty(), "{input}");
    }
}
