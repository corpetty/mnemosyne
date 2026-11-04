# Summaries in the notes: design and tasks

Status: draft 2026-11-04, nothing built (branch summaries-in-notes at 5c41172 = Release 0.28.0). Design and
build order in one file. Phases 2, 4, 6 and 11 of docs/plans/2026-10-12-hashseq-collaboration.md and the
images work (docs/plans/2026-11-04-images-tasks.md) are the surroundings. Every statement below about the
code was checked against it; the files and functions are named.

The request (Corey, 2026-11-04): (1) while recording, the live copilot appends each new point, decision,
action or question ONCE as a line under a "Copilot" heading at the end of the meeting's shared notes,
visibly as the copilot's, never rewriting what is there, never re-adding what it added or someone deleted;
(2) when the summary is made, a "Summary" section (overview, decisions, action items, open questions)
replaces the copilot section; people's notes above stay; the Summary tab stays; (3) one summary, two
views: edits in the notes' Summary section flow into the summary the way Obsidian edits do, a re-summarize
or revise merges with them, ticking an item there ticks the task, and summary action items in the notes are
never also notes items.

## The design in one paragraph

A new line mark `ref` in the notes document (notes-core, closed on the line's `\n` like `lang` / `image`)
names what a line IS: `sec:summary`, `sec:decisions`, `sec:action_items`, `sec:open_questions`, `sec:copilot`
on the headings, `sum:<summary block id>` on each decision / action item / question line, `cp:<n>` on each
copilot line. Refs are never in the markdown (not rendered, not parsed), so `Session.notes`, search,
publishing and old clients see ordinary lines; the sidecar reports them in every answer (`refs`, and `ref`
on each `tasks` entry) and takes them in `set_text` (`refs` by markdown line). The backend is the only
writer of those lines, through the sidecar under the meeting's notes lock (a fold, a composition of the
whole markdown with only ref'd lines changed, and `set_text` with the same frames, so concurrent typing
merges by the CRDT as it does today), as two new silent authors, "Copilot" and "Summary". Authorship is
visible as content: each copilot line starts with a chip `[Copilot](mnemosyne:by/copilot)` (a fourth chip
kind beside time, person and meeting), which survives moves, copies, sync and old pages. The Summary
section mirrors `summary_data` block by block (ids from services/summary_ids.py = the refs), is read back
at every fold through the vault pull-back code (export/note_parse.py + the core of vault_sync's
`apply_vault_edits`, moved to a shared module and fed the section's lines with canonical headings and
` ^m-<id>` appended from the refs), and is rewritten from the summary by a repository hook whenever
summary_data changes (ticks included), on the origin machine only. Summary action items render their owner
as `(Owner)` text, never as a person chip, and `notes_items.lines_of` skips any task line with a `sum:` /
`cp:` ref, so they never become notes items.

## Decided while planning (safe defaults; * = Corey may want to revisit)

- One new mark kind, `ref`, for identity; authorship by a chip. hashseq has no per-character author and
  the editor shows none (only `last_edit` "by" in the status line, shared_notes.py `_announce_edit`), so
  "visible as the copilot's" has to be content. Chip URL `mnemosyne:by/copilot`, text `Copilot`, drawn as a
  muted pill (`ne-chip-by`); plain_notes renders it as `Copilot: `, obsidian_notes the same. *
- Copilot lines keep today's shapes from src/lib/notes/copilotLines.ts `copilotLine` (bullet; `Decided: …`;
  a task line `text (owner)`; `Question: …`) with the chip first; owners stay parenthesised text, never
  a person chip (a chip would make the line a notes item, services/notes_items.py). *
- Dedup is server state, not the document: `CopilotNotes.added: list[CopilotAdded]` ({kind, text, owner,
  n}) is everything the copilot ever appended to this meeting; an item is new when no `added` entry of the
  same kind is `same_item` to it (services/tasks.py `same_item`, ratio >= 0.85). A deleted or reworded
  line is therefore never re-added. All four kinds are appended, summary points included; if the
  rewritten "summary" bullets flood the notes, limit the setting to decisions/actions/questions. *
- The copilot heading is re-created when missing and there is something new to add (the item rule is what
  Corey asked for; the heading is furniture). *
- Settings: `copilot_to_notes: bool = True` and `summary_in_notes: bool = True` (global, also on a team
  server; not per-person prefs). Names and labels below under "UI copy". *
- With `copilot_to_notes` on and live notes available the panel's per-item "+ Add to notes" is hidden
  (everything is added already); off, or without a sidecar, the button stays as it is today
  (client-side, no dedup). *
- Summary section layout: `## Summary` (ref `sec:summary`), the body markdown, `### Decisions`, `### Action
  items`, `### Open questions` (refs `sec:*`), each entry one line with ref `sum:<id>`: decisions and
  questions `- text`; action items `- [ ] text (Owner) 📅 2026-11-10` (Obsidian Tasks date, what
  note_parse's `_DUE` reads; `[x]` when done). No time chips, issue links or "continued in" in v1. Empty
  sections are omitted. Heading levels are display only: the refs identify them. *
- Order: the notes keep their order; a new summary's new items go after their predecessor's line (the rule
  of summary_doc.place, applied to lines), else at the end of their section. *
- Rules of the section, copied from the vault (vault_sync `_work_out`): an item line reworded = the item
  edited (three-way against what we wrote when the summary moved too); a line deleted = the item deleted
  (tombstoned, so a re-summarize does not bring it back); a new bullet under Decisions / Open questions =
  a new entry; a new task line under Action items without a chip = a new action item, with a person chip
  = a notes item (never both: the ref decides, and read-back ignores unref'd task lines whose text has a
  person chip); deletions count only while the section's heading is still there; a section whose heading
  is gone is not mirrored until the next summarize job (which re-creates sections), a hand edit of the
  summary never re-creates a deleted heading. The body = the lines between the `## Summary` heading and
  the next ref'd line. *
- Who may edit the summary through the notes: whoever may edit the notes (shared_notes.may_edit). The
  owner chose to share the notes; the edit is logged with the fold's frame author. *
- Only the origin machine (services/origin.py `is_foreign` False) writes copilot or summary lines and reads
  the section back; a `shared_from` meeting never (its document is the owner's). Library sync carries the
  document; the other machine's notes edits reach the origin by the union and are read back there.
- `Session.notes` stays the whole rendered document (shared_notes `_write_notes` / `notes_drift` depend on
  it). Readers that must not see the sections (the summary prompt, Obsidian's `## Notes`, publishing's
  notes, combine, MCP) use `notes_sections.own_notes`: the markdown above the first `sec:` line. The
  single-meeting API answer gets `Session.own_notes` for MCP (lists leave it ""). *
- At the summarize job, copilot lines are deleted only when their text is still what the copilot wrote
  (chip stripped, whitespace collapsed); an edited one stays, its `cp:` ref cleared, as the person's line.
  The copilot heading goes when no copilot line is left under it. *
- Ticks in the notes section go through item_flags.set_item_flags (fan-out, history, vault tick); text
  edits through summary_doc.apply_human with `keep_version(before, "edited")`, a history line
  `notes_summary_edits_taken`, records.seal_later and the `session` bus event, as vault_sync `_take_edits`.
- Loop rule: every write of ours records what it rendered (`written`, in the summary doc's JSON under a
  new `notes` slot shaped like `vault`); read-back only acts on lines that differ from `written`; the
  summary→notes sync only rewrites lines whose rendering differs from the document. Both converge in at
  most two rounds (a merge result written back is read as equal).

## Corrections to the request and to the obvious design (from the code)

- **C1 No author on a line.** hashseq nodes carry no author; `shared_notes_ops.author` is per frame and
  only `last_edit` shows it. Hence the chip (and the `ref` mark for identity, which a chip cannot carry:
  chips need visible text).
- **C2 "Add to notes" is client-side.** CopilotPanel.svelte `addToNotes` runs `appendToDoc` through
  `NotesDocState.applyLocal`; the server never sees which item it was. It cannot feed the dedup, so with
  auto-append on it is hidden rather than taught.
- **C3 Action items with person chips become notes items, and then vanish from summaries.**
  notes_items.lines_of takes any task line with a person chip; pipeline.drop_noted_items then strips every
  LLM item that repeats a notes item. Summary lines in the notes therefore carry `(Owner)` text, and
  `lines_of` skips `sum:`/`cp:` refs as a second guard.
- **C4 `Session.notes` must stay the full render.** `_write_notes` writes the fold's markdown; `notes_drift`
  (text != the last fold's markdown) means "written around the document" and `_catch_up_locked` would
  set_text it back in. A trimmed `Session.notes` would delete the section at the next fold.
- **C5 The summary would be fed itself.** pipeline.summarize_session folds first, then
  `shared_notes_hint(plain_notes(session.notes))`; `copilot_hint(session.copilot_notes)` already carries the
  copilot's points. The hint takes `own_notes`.
- **C6 Refs survive rewording, not moves.** diff.rs `edits_by_line` makes every `\n` its own word, so a
  changed line keeps its terminator and its marks (kind, ref); a cut-and-pasted line is new (clipboard.ts
  copies kind/lang/align/image only; refs must NOT be copied). Read-back falls back to text matching for
  unref'd lines (vault_sync `_resolve`: exact, then same_item), as the vault does for lines without ids.
- **C7 Obsidian already exports the summary.** templates.render_meeting_note renders Summary / Decisions /
  Action Items / Open Questions from summary_data and `## Notes` from `notes`; export/obsidian.py passes
  `session.notes` (l.101). It must pass own notes, or the summary is exported twice and vault_sync's
  phase-1 merge of `## Notes` carries a second copy.
- **C8 Two writers would double the section.** Library sync merges documents by union
  (services/sync_notes.py `union`): two machines each inserting the section give two copies. One writer:
  the origin.
- **C9 Ticks do not pass `update_fields`.** item_flags.set_item_flags writes through
  `repo.set_action_item_done` / `task_links._fan_out` → `fan_out_action_item_fields` /
  `set_action_item_fields_by_id` (sqlite.py l.3016, 3082, 3152, 4350), and refuses `idx < 0`. The
  summary→notes hook must sit in the repository (one `_after_summary(session_id)` beside `_touch`, called
  by every statement that sets `summary` or `summary_data`), not in session_service.set_summary.
- **C10 note_parse wants `## ` headings by text and `^m-<id>` ids.** `spans()` keys on "Summary",
  "Decisions", "Action Items", "Open Questions" at `## `; `_entry` reads `_OUR_ID`. The notes have refs
  instead: the adapter replaces each ref'd heading line with its canonical `## X` and appends ` ^m-<id>`
  to each `sum:` line before `parse()`, so the parser and the pull-back core are reused unchanged.
- **C11 CopilotNotes is replaced whole and reset per recording.** copilot_runner stores `fresh` over the
  old notes; routes/audio.py l.367-371 pops them when a recording starts (a new part too). `added` must be
  carried by `update_notes` into `fresh` and kept across that reset, or a resumed meeting re-adds
  everything.
- **C12 The summarize job's writes are on the event loop.** `fold_now` is async; the section write and
  the copilot replacement run in a thread (`asyncio.to_thread`) after `app.sessions.set_summary` and before
  `_auto_export` (export excludes the section anyway, but the record and the bus event should see the
  notes as they end up).
- **C13 `apply_human` tombstones what `new_data` lacks.** Read-back must pass the full summary data with
  only the section's changes applied (vault_sync `_apply_edits` does exactly that: deletes only
  `edits.deleted`, keeps items of sections the note does not show).
- **C14 Four local fold sites and two sync ones** (images C11): `_fold_locked`, `_set_text_blocking`,
  `mark_comment_blocking`, `set_kind_blocking` call `_derive_items`; sync_notes `store_union` / `adopt`
  call their own. The read-back hook goes beside each (the sync ones matter: a union brings the other
  machine's notes edits without a later fold).
- **C15 A notes-core change is a wasm change.** The crate is compiled into src/lib/notes/pkg; CI's `notes`
  job builds it (images C1). The editor needs no new wasm API (model.ts reads `markedSpans` generically),
  but the committed pkg must be rebuilt from CI's artifact before the branch is green.
- **C16 The fake sidecar must learn refs** (backend/tests/fake_link.py `notes doc`): a toy `["ref", line,
  value]` op, `set_text`'s `refs`, `refs` in answers, `ref` on tasks, and refs re-anchored across its
  whole-text `set` op by matching lines (difflib), or no backend test sees the feature.

## Changes to notes-core (task 1)

- `KIND_REF = "ref"`; `LineAttr.ref: Option<String>` (serde skip when None); `read_marks` reads it as
  `first` (like `lang`); `normalize` keeps it on every kind; `render` ignores it; `parse` never sets it.
- `set_text(markdown, refs: &BTreeMap<usize, String>)`: `refs` keyed by 0-based line number of the request
  markdown (the parser records each model line's first source line: `Parsed`/parse.rs gets a
  `source_lines: Vec<usize>`); a value sets the ref, `""` clears it, an absent key keeps whatever the line's
  terminator has after the diff (a kept line keeps its ref; a new one has none). `reconcile` treats `ref`
  as `lang`: mark when `was.ref != want.ref`; for set_text `want.ref` = the map's value else `was.ref`.
- Every answer: `refs: [{"line": hex, "ref": s, "index": i, "md_line": n}]` in document order (`md_line`:
  the line of the answer's `markdown` where the model line's rendering starts; render.rs tracks the output
  line count per model line, after any gap); each `tasks` entry gains `"ref": s|null`.
- `set_text` request: `"refs": {"<n>": "<ref>"}` optional. `merge`, `fold`, `mark_comment`, `set_kind`
  unchanged (answers carry `refs`).
- Values: `sum:` + 8 hex = 12 bytes travel inside the delta; `sec:action_items` (16) and
  `sec:open_questions` are artifacts (nothing to do: `intern` mints them).
- client.rs: `WireLine` unchanged (refs are not pasteable); from_json never produces a ref.

## Data and state

- `shared_notes.refs TEXT` (JSON, the last fold's `refs`), stored by `store_notes_fold` /
  `store_synced_notes_doc` beside `plain` / `comment_ranges`; `repo.notes_refs(session_id)`.
- `summary_docs` JSON: a `notes` slot `{"written": {"body", "body_exact", "blocks": {id: {section, text,
  owner, due, box, exact}}}, "copilot": {...}}` beside `vault`; `vault_sync._update_vault` generalised to
  `_update_slot(name)`.
- `CopilotNotes.added: list[CopilotAdded]` ({kind, text, owner, n}); the per-line ref is `cp:<n>`.
- Settings `copilot_to_notes`, `summary_in_notes`. `pnpm gen:api` after the model changes.

## UI copy (all *)

- Headings in the notes: `Copilot`, `Summary`, `Decisions`, `Action items`, `Open questions`.
- Chip text `Copilot`; pill in the Copilot panel's purple, muted; title "Added by the copilot".
- Author names in the notes status line (`last_edit`): `Copilot`, `Summary` (today's are `api`, `seed`).
- Settings, Copilot group: checkbox "Add the copilot's points to the notes as it goes" (help: "Under a
  Copilot heading at the end of the notes; anyone may edit or delete them"). Summary group: checkbox "Keep
  the summary at the end of the notes" (help: "A Summary section follows the summary and replaces the
  copilot's lines; edits made there change the summary").
- Copilot panel header when on and the notes are live: "adds to the notes" after the status; per-item
  button hidden. No toasts.
- History lines: `copilot_notes_added` (count), `notes_summary_written`, `notes_summary_edits_taken`
  (items, decisions, questions, body, by).

## Tasks (tier, deps)

Files in brackets are what a task may edit; tasks that run at the same time never share a file. Backend
tests one file at a time (`cd backend && uv run pytest tests/<file> -q`), Vitest one file at a time
(`pnpm exec vitest run <file>`), `pnpm check` at 0 errors, ruff on touched files, cargo per crate; the wasm
build, the guest bundle, full e2e and the full suite only in CI. Never play audio. docs change only in
task 13; CLAUDE.md there too.

### M1 the mark (first)

1. **notes-core: the `ref` line mark** (heavy). [notes-core/src/lib.rs, model.rs, doc.rs, parse.rs,
   render.rs, api.rs + tests; link/src (only if `notes doc` plumbing needs it)] As "Changes to notes-core"
   above. Tests: a ref set by `set_text` is in `refs` and on the task's entry; a reworded line keeps it;
   a deleted line's ref is gone; `""` clears; `md_line` points at the right markdown line with gaps, a code
   fence and a table in the document; `merge` keeps refs from both sides; render/parse round trip unchanged
   (no ref in markdown); an unknown ref'd line kind is still a paragraph to old readers (nothing to test,
   note it). Done when `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` pass
   for notes-core and `cargo check --manifest-path link/Cargo.toml` passes; then push, take the CI `notes`
   job's `notes-wasm-pkg` artifact into src/lib/notes/pkg, commit (C15), `pnpm check`.

### M2 backend plumbing (after 1; 2 then 3/4/5 in parallel)

2. **shared_notes: refs stored, the edit helper, the fake sidecar** (heavy; 1). [backend/mnemosyne/services/
   shared_notes.py, storage/sqlite.py (`refs` column + migration in `_migrate_columns`, `notes_refs`,
   `store_notes_fold(..., refs=)`, `store_synced_notes_doc(..., refs=)`), services/sync_notes.py (pass
   `refs`), new services/notes_sections.py, backend/tests/fake_link.py, backend/tests/test_shared_notes.py,
   new backend/tests/test_notes_sections.py] shared_notes: `refs_of(reply)`; `COPILOT_AUTHOR = "Copilot"`,
   `SUMMARY_AUTHOR = "Summary"`; `edit_blocking(app, session_id, compose, author) -> list[(seq, frame)]`:
   under `hub.lock`, `_state()` once, a `fold` run for (markdown, refs, tasks), `compose(markdown, refs,
   tasks) -> (new_markdown, refs_map) | None`, then `set_text` with the SAME snapshot and frames and
   `refs`, stored like `_set_text_blocking` (fold result, `_derive_items`, media, `_write_notes`), frames
   delivered and the edit announced; `after_fold` hook list (`AFTER_FOLD: list[Callable]`, called beside
   each `_derive_items`, C14, with (app, sid, reply, seqs, author)) so tasks 5/6 attach without touching
   this file again. notes_sections.py (pure): the ref constants and parsers (`sec`, `sum`, `cp`),
   `Ref` dataclass from a reply, `split(markdown, refs) -> Sections` (own text = lines before the first
   `sec:`; per section the heading ref, its lines, each ref'd line's md and md_line), `own_notes(repo,
   session_id, notes)`, `compose(lines, edits) -> (markdown, refs_map)` (replace/delete/insert-after by
   md_line, the refs map keyed by the NEW markdown's line numbers). fake_link.py: C16. Tests: refs stored at
   every fold site; edit_blocking keeps a frame appended between its fold and set_text (the toy merges);
   split/compose on documents with gaps, a user line between items, a missing heading. Verify:
   `uv run pytest tests/test_shared_notes.py -q`, `tests/test_notes_sections.py`, ruff.

3. **notes items and own-notes readers** (medium; 2). [backend/mnemosyne/services/notes_items.py
   (`lines_of` skips entries with a `sum:`/`cp:` ref; `_SILENT` += "Copilot", "Summary"),
   services/pipeline.py (the hint from `notes_sections.own_notes`), export/obsidian.py (l.101 `notes=` own
   notes), services/publishing.py (`_notes_text` over own notes; `notes_images` likewise),
   services/combine.py (the other meeting's own notes are joined), api/routes/sessions.py +
   models/session.py (`Session.own_notes: str = ""`, filled by `repo.get`), mcp_server.py (`own_notes or
   notes`), tests: test_notes_items.py, test_export.py, test_publishing.py, test_combine.py,
   test_summarization.py (the only test naming `shared_notes_hint`), test_mcp_server.py] `pnpm gen:api`.
   Verify those files one at a time, ruff, `pnpm check`.

4. **Copilot → notes** (heavy; 2). [backend/mnemosyne/services/copilot.py, new services/copilot_notes.py,
   models/session.py (`CopilotAdded`, `CopilotNotes.added`), config.py (`copilot_to_notes`),
   api/routes/audio.py (keep `added` across the reset, C11), backend/tests/test_copilot.py, new
   backend/tests/test_copilot_notes.py] copilot_notes.py: `new_items(notes, added) -> list[CopilotItem]`
   (same_item per kind, pure), `line_md(item, n)` (the chip + copilotLine's shape), `append(app,
   session_id, items) -> list[CopilotAdded]` through `shared_notes.edit_blocking` as COPILOT_AUTHOR: find
   `sec:copilot` and the last `cp:` line in `refs`; insert after the last copilot line, else after the
   heading, else heading + lines at the document end; refs map; skipped silently when the sidecar is
   missing, `summary_in_notes`-irrelevant, the meeting is foreign or `shared_from`, or
   `copilot_to_notes` is off. copilot_runner: after `app.copilot_notes[session_id] = fresh`, `await
   asyncio.to_thread(append, ...)` and store `fresh.added`; history `copilot_notes_added`. `update_notes`
   carries `previous.added`. Tests (fake sidecar, FakeLive as test_copilot does): first update makes the
   heading and lines with the chip; a second update with a reworded decision adds nothing; a deleted line
   is not re-added; an edited line is left alone; a new part keeps `added`. Verify: both test files, ruff,
   `pnpm gen:api`, `pnpm check`.

5. **Summary → notes** (heavy; 2; 3 for `_SILENT`). [new backend/mnemosyne/services/notes_summary.py,
   services/pipeline.py (the call after `set_summary`, C12; one line in task 3's hunk: coordinate, or do 3
   first), storage/sqlite.py (`_after_summary` hook beside `_touch`, called by every `summary`/`summary_data`
   write: `update_fields`, `set_action_item_done/follow`, `fan_out_action_item_fields`,
   `set_action_item_fields_by_id`, the sync writes), api/context.py (wire the hook: debounce 1 s on the hub
   loop, run in a thread), config.py (`summary_in_notes`), new backend/tests/test_notes_summary.py]
   notes_summary.py: `render(data, body) -> list[Line(ref, md)]` (the layout above; owners as text, due as
   📅, done as the box), `sync(app, session_id, *, create: bool)` through `edit_blocking` as
   SUMMARY_AUTHOR: compare each block's rendering with the document's line (by ref), replace changed
   lines, delete lines of blocks that no longer exist, insert new blocks after their predecessor, create
   missing headings only with `create` (the summarize job), remove copilot lines (`cp:` whose text equals
   `added[n].text` after stripping the chip; others get `""`), record `written` in the doc's `notes` slot;
   skipped when the setting is off, no sidecar, foreign, `shared_from`, or no summary. Also
   `written`'s bookkeeping for task 6. Tests: first summary replaces an edited and an unedited copilot
   line correctly; a re-summarize changes one decision's line and keeps a user's annex line under it; a
   tick from the Tasks route shows `[x]` in the notes; a deleted heading is not re-created by an edit but
   is by a summarize; the hook does not fire a write when nothing differs (count sidecar runs). Verify:
   the test file, `uv run pytest tests/test_summary_doc.py -q` (unchanged), ruff.

### M3 read-back (after 5)

6. **Notes → summary** (heavy, delicate; 5). [new backend/mnemosyne/services/summary_readback.py (the
   core moved out of vault_sync: `_Edits`, `_resolve`, `_work_out`, `_three_way`, `_apply_edits`,
   `_annex`, `_known`, `_written_entry`; vault_sync keeps thin callers), services/vault_sync.py,
   services/notes_summary.py (`read_back(app, sid, reply, seqs, author)` registered in
   `shared_notes.AFTER_FOLD`), export/note_parse.py (only if the adapter needs a hook; it should not),
   backend/tests/test_vault_pullback.py (must pass unchanged), new backend/tests/test_notes_readback.py]
   read_back: origin only; cheap exit when the section's md equals `written`; the adapter (C10) builds the
   section text; ticks: `tasks` entries with `sum:` refs whose `done` differs from `ActionItem.done` →
   `item_flags.set_item_flags(app, session, idx, done=, by=who)` (who = `notes_op_authors` for the entry's
   `frame`, else `author`, else "notes"); edits: `summary_readback.take(...)` → `apply_human(by)` →
   `keep_version`, `update_fields`, history `notes_summary_edits_taken`, `records.seal_later`, bus;
   `written` updated; unref'd task lines with a person chip ignored. Tests: a reworded decision, a new
   question, a deleted item, an owner change `(Ada)` → `(Bob)`, a due date change, a body edit, a tick by
   another person (history by them), a line edited after the summary moved (three-way), our own write not
   read as an edit (no version kept), a tick fanned out to a linked older meeting (task_links). Verify:
   both test files, `uv run pytest tests/test_vault_sync.py -q`, ruff.

### M4 the clients (7 and 8 in parallel after 1's pkg; 9 after 4 and 5)

7. **The copilot chip** (medium; 1 for the pkg only). [src/lib/notes/chips.ts + chips.test.ts
   (`{kind: 'by', who: 'copilot'}`, `byUrl`), src/lib/notes/editor/editor.css (`.ne-chip-by`),
   src/lib/notes/copilotLines.ts + copilotLines.test.ts (the chip in `copilotModel`/`copilotMarkdown`, so
   the manual button writes the same shape), backend/mnemosyne/services/notes_chips.py +
   backend/tests/test_notes_chips.py (`parse_chip` "by"; `plain_notes` → `Copilot: `; `obsidian_notes`
   the same; `task_line_parts` cuts it like a time chip), guest/src (nothing: it
   imports src/lib/notes)] Verify: the two Vitest files, test_notes_chips.py, `pnpm check`.

8. **The editor and refs** (careful; 1). [src/lib/notes/model.ts (+ model.test.ts: `Line.ref`, `REF`
   constant, `lineSig`), commands.ts + commands.test.ts (`TerminatorMarks.ref`; `splitLine`: the first half
   keeps the ref, the new line none, Enter at the line's end included; `deleteRange` / `joinWithPrevious`:
   the surviving terminator's ref as today's rule for other marks; `setLineKind` keeps it), ops.ts
   (`terminatorMarks` gains `ref`), editor/clipboard.ts (`selectionModel.entry` never copies `ref`;
   paste never sets one; its tests live in markdown.test.ts / commands.test.ts: there is no
   clipboard.test.ts)] No rendering in v1. Verify: `pnpm exec vitest run src/lib/notes/model.test.ts
   src/lib/notes/commands.test.ts src/lib/notes/markdown.test.ts`, `pnpm check`.

9. **Settings and the Copilot panel** (medium; 4, 5). [src/lib/components/SettingsPanel.svelte (the two
   checkboxes), src/lib/components/CopilotPanel.svelte (hide the button when `copilot_to_notes` and the
   doc is live; the header note), src/lib/stores/settings or wherever the form maps fields, src/lib/types
   if a hand-written type needs the field] Verify: `pnpm check`; `pnpm exec vitest run` on any touched
   test.

### M5 proof, docs, close

10. **Demo hook and e2e** (careful; 3–9). [backend/mnemosyne/api/routes/demo.py (`POST
    /api/demo/copilot`: feeds canned CopilotNotes through `copilot_notes.append` + the runner's store step;
    demo only), tests-e2e/shared-notes.spec.ts or new tests-e2e/summary-in-notes.spec.ts, tests-e2e/
    fixtures.ts] Flow: import, demo copilot twice (a Copilot heading, lines with the pill, no duplicates),
    Summarize (the demo summary: "Ship the Waku migration in October"), the Notes tab shows `Summary` and
    the decision, the Copilot heading is gone; tick the action item's box in the notes → the Summary tab
    and Tasks show it done; edit the decision in the notes → the Summary tab shows the new text after the
    fold; Re-summarize keeps the edit. Chromium and WebKit. Done when it passes in CI (the e2e job builds
    link).

11. **Backend sweep** (light; 3–6). Run ruff over the backend and the single test files each task named;
    `pnpm gen:api`; `pnpm check`. Report, do not fix beyond formatting.

12. **Docs** (light; 10). [docs/api-reference.md (the sidecar's `refs`, `set_text.refs`, the `by` chip, the
    settings, `Session.own_notes`, `copilot_notes_added` etc.), docs/architecture.md (one paragraph),
    docs/plans/2026-11-04-summaries-in-notes.md (this file: status line + "changed while building"),
    CLAUDE.md (one roadmap paragraph; the rule that only the backend writes ref'd lines)]

13. **Close** (light; 10, 12). [src/lib/notes/pkg (CI artifact, if task 1 did not commit it),
    backend/mnemosyne/api/static/guest/ (`pnpm build:guest`: the guest page bundles chips.ts)] Push and watch
    the `notes`, `viewer`, `link` and e2e jobs.

## Files shared across tasks (sequence, never parallel)

- shared_notes.py: 2 only (5 and 6 attach through `AFTER_FOLD`). sqlite.py: 2 then 5. pipeline.py: 3 then
  5 (one line each: 3 first). config.py / models/session.py: 4 then 5 (or one worker does both settings
  in 4). notes_summary.py: 5 writes it, 6 adds `read_back`. vault_sync.py: 6 only. notes_chips.py: 7 only.
  copilotLines.ts: 7 only; commands.ts/model.ts/ops.ts/clipboard.ts: 8 only. CopilotPanel.svelte: 9 only.
- Generated: schema.d.ts (3, 4, 5: `pnpm gen:api` at each), src/lib/notes/pkg (task 1 / 13), static/guest
  (13).

## What only CI can check

The wasm build and the pkg diff (`notes` job), the guest bundle diff (`viewer` job), the e2e (link,
relay, publish host), the full backend suite (locally: single files only, the machine is short of RAM).

## Risks

- `set_text` on the whole markdown is the one write path (reuses today's machinery) but relies on
  `render(parse(md)) == md` for the untouched part; a document whose render is not a fixpoint (should not
  exist: every fold re-renders) would see spurious marks. The edit helper's test with a table, a fence and
  images guards it.
- A user's line typed between two summary items lands in the item's annex (kept, never read back) — the
  vault rule; in the notes it may surprise once.
- The copilot's "summary" bullets change wording every update; `same_item` at 0.85 may still let near-
  duplicates through on a long meeting. The setting's narrower value is the fallback.
- Read-back by whoever may edit the notes widens who may edit the summary (a shared-with person, a
  phase-8 contact with notes shared). Flagged above.
- Old editors (a cached guest page) hand a line's ref to the new line on Enter at its end; the text
  fallback limits the damage to one item looking edited+added.
- The refactor of vault_sync's pull-back core touches a delicate module; test_vault_pullback.py (536
  lines) is the net.

## Open questions for Corey

Decided by default (2026-11-04), listed so Corey can overrule:
- The `Copilot` chip goes on every copilot line, so a line moved elsewhere keeps its authorship.
- Owners in the Summary section are `(Owner)` text, not person chips.
- Deleting a line in the notes' Summary section deletes the item from the summary (the vault rule).
- All four copilot kinds are appended, as asked; if the points flood the notes, narrow it to decisions,
  actions and questions.
- Whoever may edit the notes may edit the summary through them.
- MCP reads `Session.own_notes` (the notes without the sections), so it does not print the summary twice.
