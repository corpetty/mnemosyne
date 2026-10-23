# Collaboration on hashseq

Status: proposal, 2026-10-12. Nothing agreed or built. Source: a read of
https://github.com/davidrusu/hashseq (HEAD 94f5e3c) after David pointed Corey at it (2026-09-28:
"more interesting would probably be to build your own system by using the library directly").

Goal: notes and summaries that more than one person (or one person on several machines, or
Obsidian and the app) can edit, merge without a winner silently picked, and later publish. Start
local, with no network and no security questions; add peers only once the privacy layer exists.

## What hashseq is

- A Rust crate (`hashseq` 0.1, MIT, pre-release; wasm bindings in `src/wasm.rs`). Every op is
  content-addressed: its id is a BLAKE3 hash of the op and the ops it references, so history is a
  self-certifying DAG. No actor ids, vector clocks or timestamps; anyone can join.
- **HashSeq**: text and lists. `Insert`, `Remove`, `Move` (reorder within one list), `Mark`
  (formatting, comments: span-anchored, survive concurrent edits). Concurrent typing never
  interleaves.
- **HashKv**: a map of multi-value registers. No last-writer-wins: concurrent puts read as a
  conflict (`Read::Conflict`) until a put names all heads.
- **HashWeb**: a flat store of many seqs and kvs, linked by id. Large values (> 15 bytes) live in
  a content-addressed side store, sync lazily, and can be erased (drop bytes, keep the id; every
  op still verifies).
- Sync: a peer states its frontier (0xC1 frame: per-object tips), the other sends the missing ops
  (0xDE) and pushes artifacts once (0xAF). A keystroke was 107 bytes on the wire in David's kb.
  Equal op sets encode to identical bytes, so convergence is a byte comparison.
- Apps in the repo: `kb` (web/kb.html + kb.js + sync-server/: a Notion-like block editor with
  pages, comments as marks with derived reply threads, tables, images; the relay is a replica),
  and `nool` (git-like history for plain files: `commit` diffs the working file into ops, `merge`
  never conflicts at the text level, file-based deltas). David also has a Logos Basecamp module
  syncing "spaces" over Logos Delivery (`basecamp/`, not in this repo, not in the official module
  set).
- APP_NOTES.md (35 entries from building kb) is the best guide to modelling an app on it: blocks
  as objects, derived origins for implicit structure, comments as minted mark kinds.

What it does not give us:

- **No authorship.** Ops are not signed; nothing says who wrote what.
- **No access control or encryption.** Anyone holding a document can write to it; the relay is
  open-write (QUEUE.md 37).
- **No Python bindings.**
- **Typed text is permanent.** A char is its own value id (identity form), so text is inside op
  hashes and cannot be erased from history once it has spread. Only large artifacts can be.
- **Open core questions** (QUEUE.md): parent/child op replay across the composition convention
  (56: convergence holds, integrity and attribution do not), orphans never evicted (54), unknown op
  kinds rejected rather than carried (48: matters for upgrades between peers).
- **Concurrent identical edits double.** Two sides each turning `[ ]` into `[x]` both insert an
  `x`: `[xx]`. Any state that must be one value belongs in a register, not in text.

## Where it meets Mnemosyne today

- services/vault_sync.py reads ticks back from exported notes with mtime rules ("an untick only
  reopens when the note is newer than the meeting"); every export still rewrites the note from
  our state, so anything else edited in Obsidian is lost.
- `SummaryData.edited_at` (services/summary_edit.py) makes re-summarize ask before replacing
  hand edits; the alternative is a whole earlier version (`session_versions.summary_data`).
- Team sharing (services/sharing.py) is read-only for everyone but the owner, except ticking
  action items, and needs a team server.
- Action items carry over between meetings by text match (services/tasks.py `same_item`).
- link/ is already a Rust sidecar (iroh, mDNS first, end-to-end encrypted, paired devices), built
  by scripts/build-link.sh and bundled as `binaries/mnemosyne-link`.

## Where the code runs

The backend is Python and release installs only `uv sync` a lockfile: a PyO3/maturin wheel would
need publishing per platform or a Rust toolchain on users' machines. So hashseq lives in a Rust
sidecar the backend drives over stdin/stdout JSON lines, like services/link.py does, built and
bundled the same way. Decision for phase 1: a second binary in the link/ crate
(`mnemosyne-notes`) or a subcommand of `mnemosyne-link` (one binary to ship; link's iroh endpoint
is what phase 3 needs anyway). Leaning to the subcommand.

The frontend gets the wasm build only when there is live editing (phase 4); until then the UI
shows what the backend renders.

State: one HashWeb store per meeting under `recordings/<id>/notes.hweb` (or `<data_dir>/notes/`),
sealed like audio (`seal_session_audio` / `storage.crypto.plaintext()`). SQLite stays the source of
truth for everything not collaborative; for the collaborative surfaces the store is the truth and
SQLite keeps a projection (refreshed after every merge).

## Phase 0: try nool on a copy of the vault (an afternoon, nothing committed)

`cargo install --git https://github.com/davidrusu/hashseq --bin nool`, copy a few exported notes
to `~/.cache/mnemosyne-trials/nool`, `nool track` them, edit one copy as Obsidian would (tick,
reword, add a line) and another as a re-export would (a new summary), `nool merge`. Look at what
the merged notes say, especially lines both sides changed. This answers whether text-level merge
of regenerated notes reads well enough to build on before writing any code.

### Results (2026-10-12)

Done. Inputs, outputs and the trial tool are in `~/.cache/mnemosyne-trials/nool/` (`v03/`,
`tick/`, `tick2/`, `tool/`).

Main trial (`v03`): a real re-summarize from the vault's git history. "V0.3 Release and Dark
Prague Planning" (87 KB, 2,249 lines, mostly transcript) was exported at 14:36 (0391755d) and
re-summarized at 15:32 (1900ed3f): new topics, summary, decisions and action items, speaker
labels renamed. The user's side was the first export plus nine hand edits as Obsidian would make
them: three ticks (one on an item the re-export kept, two on items it reworded), an item's owner
changed, a tag, a paragraph after the summary, a line in Notes, a transcript word, and a reworded
decision extended. Each side was merged as a fork of the first export, compared with git's
three-way merge (`git merge-file`), and with three ways of turning a text change into ops
(`tool/`, the hashseq crate plus the `similar` diff crate):

| merge | result |
|---|---|
| `nool merge` | garbled: the tag, the summary paragraph, the decision note, all three ticks (`xxx`), the owner (`Roxna`) and the Notes line all landed as one run inside the transcript line at 00:59 |
| git three-way | 2 conflicts, 77 lines of conflict markers (both whole versions of Summary to Action Items) |
| char diff | all nine edits kept; the tick on a reworded item became a stray `x` inside a word (`lockx`) |
| line diff | all nine kept; the reworded lines both sides changed came back twice (old and new versions) |
| **line, then word** | **all nine kept and clean**; edits on reworded lines carried over to the new wording (the decision note at the end of the new decision, the ticks on the reworded items) |

- **hashseq itself did its job.** Every merge gave the same text in both orders, took
  0.05-0.6 s and 63-86 MB for the whole process, and the history file was 1.0x the note at the
  start and 1.16x after both sides and the merge.
- **The diff is the design.** nool diffs characters (Myers) and, once two versions differ by more
  than 1,000 characters (`MYERS_MAX_D` in src/bin/nool/diff.rs), replaces everything between the
  first and the last difference in one delete + insert. A re-summarize always exceeds that, so
  every edit made in that span is anchored on removed text and lands where the removed text
  was. Worth telling David. Lines first (patience), then words inside each replaced block, works.
- **Checkboxes cannot be text.** `tick2`: both sides tick the same item after each made some
  other edit above it, and the merge says `[xx]` (char diff) or `[x][x]` (line, then word). The
  ops only coincide, and collapse into one, when both sides started from the same state
  (`tick`). And the ticks on reworded items carried over to the new wording here because the
  word diff lined the boxes up; with a different rewrite a tick could land on the wrong item.
- **Phase 1 alone does not need a CRDT.** Two sides and a stored base is a three-way merge; a
  word-level three-way merge would do about as well. hashseq pays off with history across rounds
  and when there are more than two replicas (phases 2-3). Using it in phase 1 keeps one
  mechanism from the start, and that is the plan.

## Phase 1: two-way Obsidian sync (local only)

Status: built 2026-10-12, unreleased. Scope agreed with Corey: edits survive in the note; pulling
edited item text and summary paragraphs back into the app moved to phase 2. As built: the history
is a `vault_notes` row in SQLite (not a file: encryption, backups and deletion come with it), and
it holds two branches, the combined one (what we wrote) and the app's own (what the app last
rendered). With only the combined branch, a user edit survived just the export that merged it:
the next rendering, diffed against text that contained the edit, deleted it. The word diff
splits punctuation from words (`similar`'s word split made "April, pending" look like a rewrite).

The history of each exported meeting note lives in the data directory, never in the vault (only
notes with our `source: mnemosyne` frontmatter, as today).

- On export: render the note (export/obsidian.py `render`) and diff it into ops against the text
  last written (the app's side). If the file in the vault changed since we wrote it, diff it into
  ops against the same base (the user's side). The two are concurrent forks of one frontier;
  merge them, write the merged text to the vault, remember it as the new base.
- Diff by lines (patience), then by words inside each replaced block (phase 0). Never by
  characters across a whole note, never a coarse replace.
- Checkbox status stays out of the text: both sides' text has every box as `[ ]` before it is
  diffed. Ticks are read from the vault note by item (`parse_ticks` / `pairs`, as today), applied
  to our action items, and the boxes are filled in from our state when the note is written.
- `sync_vault` (every 5 min) does the user's side alone, and merges back into the app what it
  understands: ticks, edited action item text, edited summary paragraphs (as a summary edit,
  `edited_by` "Obsidian"). The mtime rules go.
- After a merge, normalize what must be one value (duplicated frontmatter keys). Gap conflicts
  (two inserts at the same anchor, which hashseq can detect) on a line both sides changed are
  listed in the meeting's history (services/history.py) so they can be looked at; nothing is
  thrown away.
- The phase 0 trials (`v03`, `tick2`) become the sidecar's first tests, with the expected merges
  checked in.
- Tests: the sidecar's own `cargo test` (fork, diff, merge, normalize), and backend tests with a
  fake sidecar in tests/fakes.py, as for link.

Done when: ticking, rewording an item and adding a paragraph in Obsidian all survive a re-export
and a re-summarize, with no mtime rule left in vault_sync.py.

## Phase 2: the summary as a document

Replace the "ask before re-summarize" dialog with a merge.

- Model (APP_NOTES.md conventions): a meeting kv (title, style, `source_hash`); one seq per
  section (summary, decisions, open questions) whose elements are block seqs; action items a seq
  of item kvs (`text`, `owner`, `due`, `done`, `at`). `done` is a register, so a tick from two
  places converges, and a tick against an untick shows as a conflict to settle with one click.
- A new LLM summary is the model's fork from the frontier it was generated against
  (`source_hash` + the frontier at job start), applied as a diff and merged, so hand edits made
  meanwhile survive. `SummaryData` stays the API shape; it is read from the store.
- Earlier versions are frontiers, not copies (`session_versions.summary_data` can go for
  meetings that have a store).
- The Obsidian note from phase 1 becomes a projection of this document, not a separate text
  history.

### Phase 2 design (agreed with Corey 2026-10-12)

Status: built 2026-10-12, unreleased, as designed below. Deviations found while building: tombstones
keep their section and last text (a reworded copy is still recognized); an LLM merge passes the
current text as the vault side (repairs edits that bypassed `apply_human`); on a revise, owner,
due, topics, chapters and client facts take the current state as the base; Obsidian pull-back
takes only blocks whose rendered text was exactly the summary's (not redacted); a `[[link]]` typed
in Obsidian comes back as plain text.

Drafted against the code, reviewed and accepted. Supersedes the HashWeb model sketched above:
reads stay in SQLite, ids live in `SummaryData`, and phase 1's two-branch text merge works per
block.

- **Per-block histories.** The summary body is one block; every decision, open question and
  action item is one block keyed by a stable id. Each block has a phase 1 history (two branches:
  `llm`, what the LLM last produced, standing in for phase 1's `app`; `combined`, the current
  text). One id-tagged text for the whole summary was rejected: an item the LLM drops but the
  human extended leaves fragments, an item the human deleted comes back when the LLM rewords it,
  and LLM reorders strand word edits. With blocks, those are set rules on ids in Python, and the
  sidecar merges opaque texts.
- **Ids in the model** (an API addition): `ActionItem.id`, `SummaryData.decision_ids` and
  `question_ids` (parallel lists, like `decision_at`), 8 characters like `Asset.id`. Readers are
  unchanged; the Obsidian block id `^m-<id>` is rendered from them.
- **Doc** (`summary_docs` table, JSON, cascades with the session): `body` and `blocks[id]` each
  `{history, llm}` (the llm branch's text is cached in Python); `llm` = the base for 3-way rules
  (order per section, per-item `owner/due/at/live`, topics, chapters, client facts, `*_at` by
  id); `tombstones` (ids the human deleted); `vault` (what the last export rendered per block, and
  annex lines the user added under items in Obsidian). summary_data stays the projection.
- **LLM merge** (`apply_llm`): ids for the new output by `same_item` against the llm branch's
  items, then human-added items (the human's wins), then tombstones (dropped), else new ids. Per
  id: both present and texts differ → merge with `vault=None`; only new → insert after its LLM
  predecessor in the current order, else at the section end; only in the old LLM output → delete
  unless the human touched it (text, owner or due differ from the base, or done, follow or
  issue_url set), then keep (Corey: touched items are kept, done ones included); tombstoned → skip.
  Order = current order. Scalars: `owner/due/at/live` take the new LLM value when the current one
  equals the base, else stay; `done/follow/issue_url` are human-only. Topics: (current − (base −
  new)) ∪ (new − base), aliases applied. Chapters and client facts: whole list, new if current ==
  base. Metadata (`style/provider/model/source_hash/title`) from the new output.
- **Revise rebases**: `revise_hint` shows the LLM the current summary with the hand edits, so its
  output echoes them; diffed against the llm branch they would double. A revise first sets llm :=
  combined, then merges.
- **Human edits** (`apply_human`): editor, Tasks text/owner/due, restore, Obsidian pull-back. Text
  changed → merge with `app=llm text, vault=new text`; new items get ids (no llm side); missing ids
  are tombstoned. done/follow/issue_url, speaker renames and topic renames stay direct writes.
- **Migration** (`ensure_doc`): the baseline for a meeting without a doc is the LLM output
  rebuilt from `session_versions` (the first "edited" version after the last "summarized again" or
  "restored" one is the pre-edit state), else the current state.
- **Obsidian**: block ids `^m-<id>` end every decision, question and action item line (Corey
  agreed); ticks match by id first. The projection sections (Summary, Decisions, Action Items with
  Others, Open Questions) are cut out of phase 1's text merge (a placeholder) and spliced back from
  the rendering, so an edit pulled into the doc and rendered back cannot double. Pull-back
  (`apply_vault_edits`, from `before_export` and the 5-minute pass, never exporting from the
  pass): per block against what the last export rendered; changed only in the vault → human edit
  by "Obsidian"; both changed → a one-off 3-way merge. Redacted exports pull back only lines whose
  text equals the doc's.
- **Sidecar** (`notes merge`): adds a batch form (array in, array out, per-item errors), `base`
  instead of `history` (a one-off 3-way) and `rebase: true`. The single-object form is unchanged.
- **Fallback**: no sidecar or a failure → today's replace + `carry_over`, the doc forgotten,
  warned once.
- **UI**: no confirm before re-summarize; an informational line when there are edits ("Your edits
  are kept when the summary is made again"); the summary-ready toast says how many edits were
  kept; history lines `summary_merged` and `note_edits_taken`. No per-item conflict state in v1:
  a line both sides changed keeps both at word level and is listed in the history line.

## Phase 3: Corey's own machines

Sync meeting documents between two paired computers (the "desktop" devices link/ already trusts)
over iroh: frontier hello, missing ops, artifacts on demand. mDNS first, relays only when needed,
as for remote access. No authorship or access questions yet: every peer is the same person.

### Phase 3 design (agreed with Corey 2026-10-14)

Status: built 2026-10-14, unreleased, as designed below (details in
docs/plans/2026-10-14-library-sync-rules.md). Additions found while building: fast-forwards are
also stored as merge bases; a received delete is stored with local time so a third machine still
sees it; deletes wait while the meeting is busy; the origin keeps its own recordings rows; items
added on both machines with similar wording are one item. Not yet: renaming this computer for
sync, sending only ids before documents (every change comes back once in full on the next pull),
remembering id clashes across restarts.

Scope (Corey): both machines record; ONE library; meetings recorded on either appear on both;
summary and transcript travel, audio stays on the machine that recorded it. Drafted against the
code at ea282b1, reviewed and accepted.

- **Transport**: no new protocol. Each machine's `mnemosyne-link home` also dials every sync peer
  (the client half remote.rs runs today, `Remote` + `serve_local`, same endpoint and `link.key`)
  and serves it on a loopback port; each backend PULLS the other's changes over HTTP
  (`/api/sync/*`) with the per-device token pairing issues; `POST /api/sync/poke` asks for an
  immediate pull. Control of the sidecar: JSON lines on its stdin (`pair` with `peer` info,
  `dial`, `hangup`), replies on stdout. mDNS first, relays as remote access does.
- **Pairing**: mutual, from the existing invite flow (`kind: "sync"` codes); each side records the
  other as a DESKTOP device (endpoint-bound) and as a sync peer in `<data_dir>/sync_peers.json`
  (0600; holds the token for the other machine, same class of secret as link.key; not backed
  up). Requires remote access and an API token; refused in team mode. Removing the device removes
  the peer and keeps what was synced.
- **Unit**: one meeting with its id; new meeting ids are 12 hex characters (Corey), old ones keep
  theirs; same id with a different `created_at` is a clash: reported, never merged. Travels:
  session fields, summary_data, notes, transcript, recordings rows (the origin's paths; non-origin
  `audio_file` is NULL), bookmarks, external notes, live flags, session speakers, asset links and
  link/text assets, history events (union by a new `uid`). Rebuilt locally: FTS, vectors,
  people, topics. Never: audio, keys, settings, vault_notes, summary_docs, versions, seals, voice
  profiles, shares, supervision, organizations, asks, digests.
- **Bookkeeping**: `sessions.origin` (recording machine's sync id, '' = here), `sync_vv` (version
  vector), `sync_seq` (global change counter); every local write goes through one `_touch`;
  `sync_served` keeps the last 3 states served per peer (the merge base), `sync_tombstones` travel.
- **Merge** (Corey: three-way, not history union): incoming dominated → ignore; dominates →
  fast-forward; concurrent → three-way against OUR state the peer last incorporated (served
  snapshot at its `vv[me]`), so a round trip never doubles text. Texts (body, notes, each item by
  id) through the sidecar's `base` form; scalars: one side changed → it, both → newer
  `updated_at`; sets (topics, participants, bookmarks) three-way; items by id: deleted vs edited →
  edit kept, deleted vs untouched → deleted (tombstoned through `apply_human`); transcript: one
  side → it, both → the recording machine wins (the other kept as a version). The summary enters
  the local doc through `summary_doc.apply_human(by="Sync (<machine>)")`. Deletes (Corey): delete
  everywhere, incl. the recording, after a confirm saying so; a tombstone beats edits.
- **The recording machine does the work**: transcription, auto-summarize, auto-export, the vault
  pass, export-all, recovery, clips, audio deletion, HubSpot auto-push, auto-naming: origin only
  (other machines answer 409 "The audio is on <name>"); manual summarize, revise, edits and manual
  export work anywhere. Audio plays on other machines by streaming from the origin when it is
  reachable (Corey), else "The audio is on <name>".
- **Skew**: `hello` carries protocol/schema/app versions; a newer protocol is shown as "Update
  Mnemosyne on <name>" and skipped. Locked peers (423) are skipped and shown as locked.
- **Tests**: the sync engine between two in-process apps through `httpx.ASGITransport` (no iroh);
  the sidecar's dial/pair in Rust like link/tests/tunnel.rs.

## Phase 4: tasks and decisions as objects

### Phase 4 design (agreed with Corey 2026-10-21)

Status: built 2026-10-21, unreleased, as designed below. Found while building: linking only ever
reaches OLDER meetings (else a done item with a newer open twin got linked and reopened, against
the rule; also re-summarizing an old meeting could link it forward); decisions with the same
wording read "also decided in", only different wording "supersedes"; LLM hints never offer tasks
from local-only meetings to a cloud provider; the linking settings sit in the Obsidian section of
Settings, next to the other task setting.

Drafted against the code at ae5c293, reviewed and accepted.

- **The link lives on the item; objects are derived.** `ActionItem.task_id` (12 hex, "" = not
  linked; minted on any machine). A task object = every action item (across meetings) with the
  same `task_id`; an unlinked item is its own object (key `<session_id>:<item id>`). No new table,
  nothing new travels in library sync (items travel inside summary_data; `task_id` joins
  sync_merge's ITEM_SCALARS); phase 2's `_project` copies items whole, so the link survives
  re-summarize, revise and restore. Decisions: `SummaryData.decision_links: {decision id: object
  id}` (pruned when the decision is gone).
- **Object state**: head = the mention in the newest meeting the caller can see. text, owner,
  due, at = the head's (editing a task edits its head mention). done, follow, issue_url are the
  object's: every write fans out to all mentions (each bumps its meeting, so it syncs); a
  reconcile (head wins) after sync merges and at startup repairs mentions that disagree. Team
  mode: a tick fans out into linked meetings the ticker cannot see (same task; the history names
  the ticker); lists show only visible mentions.
- **Linking** (services/task_links.py): candidates are OPEN objects of other visible meetings,
  owner-compatible, each used once. Exact normalized text always; close text (`same_item`,
  >= 20 characters) only when the earlier meeting is related (recurring title or same people)
  (Corey: exact + related, setting `task_linking`). Run at summarize (after the merge) and after
  hand edits / restore / Obsidian pull-back (exact only). A done task that comes up again is a NEW
  open task (Corey); a manual link makes the head's state the object's and fans it out. Manual
  link/unlink routes; "Suggested links" for close matches on existing data. Every link is a
  `task_linked` history line. Decisions: exact text only; "supersedes" is a manual link (the head
  is the current wording, earlier mentions what it superseded).
- **Optional LLM-assisted linking** (Corey: build it, off by default; never with the built-in
  local model): the summary prompt lists related meetings' open tasks by id and may mark an item
  `"continues": "<id>"`; only offered ids are accepted.
- **Obsidian** (Corey: one checkbox per task): only the head mention's note has the checkbox;
  earlier mentions render as plain bullets `↩ continued in [[later note]]` (keeping their `^m-`
  block id), so vault-wide task queries list each task once. A tick in the note fans out.
- **Consumers**: Tasks view rows are objects with an "in N meetings" history, link/unlink,
  suggestions; SummaryView shows "↩ continues from" and "supersedes / superseded by"; brief,
  people, organizations, MCP collapse by object; decisions log shows superseded chains;
  GitHub/Linear/Jira issue links fan out. HubSpot keeps one task per mention (later).
- **Migration** (once at startup): exact links over existing meetings, then reconcile (head wins,
  fanned out); close matches appear only as suggestions.

## Phase 5: comments and threads

### Phase 5 design (agreed with Corey 2026-10-21)

Status: built 2026-10-21, unreleased, as designed below. Found while building: a moment comment is
never detached (a time in the audio is always valid; it shows on a line within 5 s or "at m:ss");
summary-text anchors match the rendered text (markdown stripped on both sides); comments on other
meetings' threads shown in Tasks act on their own meeting.

Drafted against the code at 494741b, reviewed and accepted.

- **No hashseq marks yet.** Transcript lines have no stable id (segments are keyed by meeting and
  index; re-transcription replaces them) and summary docs are local and disposable (they never
  travel). Marks need a per-meeting store that syncs; revisit with live shared notes.
- **Anchors** (`comments` table: id 12 hex, session_id, parent_id ('' = root), target
  segment/body/decision/question/item, target_id, anchor JSON, quote, text, author_id, author,
  created/updated, resolved_at/by, deleted): transcript comments are a time span `(part, start,
  end)` like bookmarks and live flags, re-anchored at read time to the line with the largest
  overlap (follows edits, splits, merges, re-transcription; a zero-length span is a comment on a
  moment of the audio from the player, Corey: include it); decision/question/item comments by
  block id (survives rewording); summary-text comments by a text-quote selector (exact, prefix,
  suffix; fuzzy fallback). A comment whose text changed is flagged; one whose anchor is gone is
  "detached", shown with its quote in a Comments card, never deleted automatically.
- **Tasks**: a comment attaches to one mention (session + item id) and is aggregated on the task
  object (visible mentions only); posting from the Tasks view goes to the head mention.
  Decisions likewise.
- **Sync**: comments travel inside their meeting (`SyncSession.comments`), merged by id three-way
  (added/removed/edited; text through the merge, resolved/deleted as scalars).
- **Team mode**: anyone who can read the meeting comments; authors edit their own; delete:
  author, meeting owner or admin; resolve/reopen: meeting owner, admin or the thread's author.
  Desktop author = `local_speaker_name`, shown as "You". History lines `commented`,
  `comment_resolved`, `comment_reopened`.
- **Scope** (Corey): not exported to Obsidian in v1 (the pull-back would read them as annex lines);
  keyword search yes, Ask and summaries no; @mentions on a team server notify the mentioned person
  online (toast + desktop notification), no email or digest. Open comments listed in MCP.

## Phase 6: live shared notes

### Phase 6 design (agreed with Corey 2026-10-22)

Status: built 2026-10-22, unreleased, as designed below (notes-core and notes-wasm crates, the `notes`
CI job, relay and sync in the backend, the editor in src/lib/notes, Vitest and Playwright specs).
Found while building:

- Mark strings over 14 bytes (URLs, every `comment:<id>`) are artifacts: hashseq keeps them out of
  the delta, so they travel as 0xAF frames, drained before the delta, resent on every reconnect
  (duplicates are harmless), and the relay logs and forwards them like deltas.
- Enter at a line end puts the new `\n` AFTER the old terminator, so the line keeps its kind
  and mark affinity and the new line gets the continued kind; Enter mid-line puts the new
  terminator at the caret and the old one moves to the end.
- Inline anchors must equal wasm's `markRange` (`Before(first)..Before(next)`, `After(last)` at the
  document end), so bold/italic/strike grow at their end only; code, link and comments use
  `markRangeClosed`; the Rust side (notes-core) builds the same anchors.
- Reconnect: the hello's snapshot and backlog go into a throwaway replica whose frontier is what the
  server holds; they are merged into the live store without sending, then `applyFrontier` with that
  frontier makes the next drain exactly what the server lacks (offline edits included).
- Text written around the document (the notes route, library sync's text merge, a meeting with no
  sidecar for a while) is caught up into it as an `api` edit before a join or fold, else the next
  fold would render over it.
- A union that fails (sidecar missing) holds the sync cursor just before that meeting and the round
  goes on to the other meetings, which are not blocked; it is tried again next round.
- Comments on notes are marked by the SERVER from the element ids (`notes_ids`), not by the client,
  so a read-only reader (a reviewer) can comment.
- The join/fold race (a fold between reading the snapshot and reading the frames): join reads the snapshot and the frames after it together (and enters the room in
  the same step), so a frame is in the backlog or relayed, never neither or both.

Drafted against the code at 273eaf3 (hashseq 94f5e3c), reviewed and accepted.

- **The CRDT runs in every client** (the desktop webview and the team server's web app) as
  hashseq's wasm build: one `WasmHashWeb` per meeting, the notes are one seq whose origin is
  `sha256("mnemosyne/notes/" + session_id)`, computed in Python and sent in hello, so every client
  and machine opens the same object with no create race. The client fixes QUEUE 39 itself: drain
  `takeDeltas()` before applying a remote frame, then `applyFrontier(encodeFrontier())`.
- **The backend is an opaque relay** (it cannot decode hashseq): `/ws/notes/{session_id}` (binary
  0xDE frames plus JSON text frames; auth like `/ws`, `Hidden` closes 4404). Per meeting:
  `shared_notes_ops(session_id, seq, frame, author_id, author, at, folded)` and
  `shared_notes(session_id, snapshot, folded_seq, updated_at)`, both in SQLCipher, cascading on
  delete. Join = folded snapshot, unfolded frames in order, then a JSON hello (objects, you,
  present, last edit, `read_only`). A client frame is checked (tag, ≤ 256 KB), appended, relayed to
  the others (never echoed). Presence (`typing`) is relayed JSON. Authorship is recorded per frame
  from the connection (team user, desktop `local_speaker_name`, "api", "sync (<machine>)").
- **Folding** by the stateless sidecar (`mnemosyne-link notes doc fold|set_text|merge`): 2 s after
  the last frame, every 30 s while active, on last leave, before summarize/export/sync serve. It
  writes the snapshot and the projection, **`Session.notes` = the rendered markdown**, through
  `update_fields` (`_touch`, FTS), with a bus event. Search, Obsidian, MCP, combine and the summary
  keep reading `Session.notes`. No sidecar: hello says `unavailable` and the old textarea is used.
- **Document model: one flat seq, structure in marks** (not kb.js's page kv + block seqs: nested
  objects, comments cannot span blocks, ~4k lines of DOM to vendor). The text is lines ending in
  `\n` (the doc always ends with one). A line's kind is a closed `block` mark on its terminating
  `\n`: `h1|h2|h3|bullet|number|task|task_done|quote|code|table|table_head` (`lang` on a fence's
  first line). Inline: `bold`, `italic`, `strike` grow at their end (wasm `markRange` anchors:
  `Before(start)..Before(next)`), `code` and `link` (value = URL) closed (`markRangeClosed`),
  `comment:<id>` closed; flag values are `"true"`. Concurrent line kinds stay live together:
  `task`+`task_done` resolve to done, otherwise the first in `markedSpans` order; two live link
  URLs render as plain text with "(link conflict)". Mark strings over 14 bytes (URLs, comment
  kinds) travel as 0xAF artifact frames, which the relay forwards and logs like 0xDE deltas.
- **Tables** (Corey: in v1): rows are lines of kind `table` (the first row of a run
  `table_head`), cells separated by U+001F in the text, inline marks allowed in cells. Column
  alignment is an `align` mark on the head row's terminator, one of `l c r -` per column (`"lc-r"`). Adding a column inserts a separator
  in every row; ragged rows (concurrent column edits) are padded by the renderer and the editor.
  Tab / Shift+Tab move between cells, Tab in the last cell adds a row.
- **Markdown both ways, one implementation**: a `notes-core` Rust crate (hashseq + render +
  parse + `set_text`) used by the sidecar AND compiled into the wasm module, so the client's
  markdown paste and the server's fold agree. Render is canonical (fixed nesting: link, bold,
  italic, strike, code; escapes only where needed; GFM tables with `\|`). The parser covers the
  rendered subset plus `__`/`_`/`*` variants; anything else (HTML, images) stays literal text,
  normalised once. `set_text(md)` diffs the plain text by lines then words (notes.rs's diff, moved
  into notes-core) and reconciles marks, emitting nothing for an unchanged doc
  (property-tested: `set_text(render(doc))` is empty, `render(parse(md))` is a fixpoint).
- **Seeding and other writers**: a meeting with notes and no doc is seeded by `set_text`, only on
  its origin machine. The old `POST /sessions/{id}/notes` (API, MCP) goes through `set_text` as
  author "api" and is broadcast.
- **Library sync**: `SyncSession.notes_doc` (canonical snapshot, folded at serve). Both sides
  with a doc: union merge (no base needed). One side only: today's three-way text merge, then
  `set_text` into the doc. Offline edits are just ops; the union merges them later.
- **Editor: our own contenteditable** (`src/lib/notes/`, Svelte 5), porting by name (MIT,
  attributed) kb.js's proven primitives: text extraction with fillers and sentinel, DOM point ↔
  code-point offset, diff at the caret, mark affinity at boundaries, selection kept by element id
  (`seqIdAt`/`seqPositionOf`), composition deferral, input rules. One line element per line,
  re-rendered only when that line changed, so remote edits never move the caret. Plain typing is
  native and committed by diff; Enter, Backspace at a line start, formatting shortcuts
  (Ctrl+B/I/E/K, Ctrl+Shift+X), cut, paste and undo are `beforeinput` commands. Input rules: `# `,
  `- `, `1. `, `> `, `[ ] `, a fence, `|a|b|` + Enter → table. **Paste** (Corey: markdown too):
  text/plain is parsed as markdown by notes-core in wasm and inserted with its marks; copy puts
  the selection's markdown on the clipboard. Undo is local (inverse ops of your own edits).
  WebKitGTK (the desktop) is tested from the first DOM task, and the e2e runs in WebKit too.
- **Access**: owner, admin and shared-with (incl. "*") edit; reviewers on others' meetings read.
- **Copilot** (Corey): the panel stays; each line gets "Add to notes", an ordinary insert.
- **Summary**: a `shared_notes_hint` from `Session.notes` ("notes the attendees took; the
  transcript wins", 12k budget). Old meetings with hand notes get them on their next re-summary.
- **Comments on notes**: `comment:<id>` marks; the thread stays in the phase 5 table (target
  `notes`); the fold reports each comment's range, a fully deleted range = detached.
- **Not in v1**: the phone page; per-character author colours (the per-frame data is kept);
  rich HTML paste; images and math. Typed notes are permanent in the op history (no redaction
  once synced; prerequisite 1 still holds).
- **Tests**: Vitest + jsdom for the pure editor parts (Corey: yes), `cargo test` for notes-core,
  TestClient websockets for the relay, two-app sync tests, a Playwright two-page convergence spec
  in Chromium and WebKit (the e2e job builds link).

## Phase 7: publishing a meeting

### Phase 7 design (agreed with Corey 2026-10-23)

Status: built 2026-10-23, unreleased, as designed below (use: docs/publishing.md; flow:
docs/architecture.md "Publishing"). Found while building: emails and phone numbers are hidden next to
identifiers (`redact_contacts`, same `hide_identifiers` switch), since a summary names them as often as
account numbers; `local_reach` (team / phone_url / this_device), because a desktop's link to itself only
reaches others through the team listener, so the dialog and `Publication` say who can open it; the
`publications.origin` column says which computer serves a "this computer" publication (library sync);
two computers publishing the same version of a hosted publication are told apart by the manifest ETag (the
host's 409 with our version is success only when it holds our manifest); records seals are forced for
publish events (`add_seal` deduped them away when nothing else had changed); diarizer labels
(SPEAKER_01) are never published as owners or speakers; files are saved through the Tauri save dialog and
a loopback-only endpoint (`POST …/publication/file/{token}/save`), since a webview cannot download them;
viewer assets live under `/p/assets` so the same build works at `/p/<id>` on the backend and on the
publish host.

Drafted against the code at 6020e3a (0.21.0), reviewed and accepted. It uses neither hashseq nor
Logos: readers never write, and a curated copy must be able to lose a sentence for good.

- **A curated projection, never the record.** The owner (or an admin) picks parts in a Publish
  dialog with a live preview rendered by the viewer itself: title, date, summary, decisions, open
  questions, action items, chapters, attendees and topics on by default; quotes (with clips,
  chosen one by one) and the live notes off by default; the transcript never (only quote lines;
  a test asserts no segments). Identifiers redacted by default (`redact_identifiers`); names as
  they are by default (Corey), or first names, or Person 1 (`Redactor`). Clips cannot be
  redacted: the dialog plays each chosen clip ("listen before you publish").
- **Format: an encrypted snapshot per version** (Corey), not a hashseq space: a padded JSON
  manifest plus Ogg Opus clip blobs (content-addressed by ciphertext SHA-256, reused across
  versions), AES-256-GCM with AAD `"mnemosyne/pub/1" ‖ id ‖ kind ‖ version`. One random key per
  publication; versions reuse it so the link stays the same. Caps: manifest 1 MB, clips 20 MB.
- **The capability link**: `https://<host>/p/<id>#<key>` (id 16 random hex, never the session
  id). The fragment never leaves the browser. The host sees ids, padded sizes, times, readers'
  IPs; no text. Unpublish deletes the blobs (copies already opened cannot be revoked; the docs say
  so). Expiry: hosted links 90 days by default, at most 365 on our host; files never expire.
- **Where** (Corey: local + file now, host built): "this computer" (the backend serves `/p/{id}`
  and `/pub/...` read-only, open paths; the team server too, always on), "a file" (one HTML with
  the viewer and the ciphertext inlined, optional password via PBKDF2), and "our publish host"
  (`publish-host/` axum crate + `deploy/publish/`, ciphertext only, caps, rate limits, mandatory
  expiry; `publish_host` blank until one runs, then the default, as with relays). Logos
  (storage_module/delivery_module are C++ plugins in a Logos Core host; Basecamp spaces are
  unofficial) is a written trial only.
- **The viewer**: its own Vite entry (`viewer/`, Svelte 5, no SvelteKit router or app stores, no
  wasm), WebCrypto in the browser, clips as blob URLs, strict CSP, works from `file://`; built
  reproducibly into backend static files and `single.html`, checked in CI like the notes wasm.
- **Management**: `publications` table (one live publication per meeting), Update keeps the link,
  "published version is behind" after re-summarizing, history lines (`published`,
  `publication_updated`, `unpublished`), each publish sealed into the meeting's records
  (`publications.json` in the records zip, never the key). Team servers: `allow_publishing` off
  until an admin turns it on (Corey); then owner or admin. Readers see that a meeting is
  published, not the link.
- **Sync**: the publication row (key included) travels in `SyncSession.publications`; hosted
  publications can be updated from either machine (clips only from the origin); one served by
  "this computer" is served by its origin only.

## Phase 8: sharing with other people's computers

### Phase 8 design (drafted 2026-10-23, for review)

Status: draft, nothing built. Drafted against the code at 20117be. Decided by Corey (2026-10-23):
the owner picks what each share contains; recipients get team-share parity (comment, tick and
follow tasks, edit the live notes); the owner's machines are the hub; one key per person.

The team server shares meetings from one central copy. This shares them between desktops with no
server: each recipient's machine holds the meetings shared with it. It is the first phase where
peers are different people, so it is also where prerequisite 2 (authorship) gets built.

What exists today and does not carry over: library sync trusts a peer completely (a DESKTOP token
opens the whole API, `apply_synced` skips every access check, every meeting is offered, and
publication keys travel), because every peer is the same person. Authors are text labels
(`author`, `author_id`, history `by`) that any writer can set. The only key is the iroh endpoint
key in `link.key`. A sync peer's id is a random string it states about itself, tied to its
endpoint only by the pairing flow.

- **A key per person.** An Ed25519 signing key, made in Python with `cryptography` (already a
  dependency), stored as `<data_dir>/person.key` sealed with the files key (storage/crypto.py),
  so a locked app cannot sign. The person id is the public key; people compare an 8-group
  fingerprint when pairing. The display name is self-stated, and a contact can rename it locally.
  Library-sync pairing copies the key to your other machines (one more call over the tunnel,
  token-gated). If both machines already have different keys, the one with no contacts or shares
  adopts the other's; if both have them, pairing is refused ("these computers belong to different
  people"). A lost machine means a new key: rotate it, and contacts re-pair (no revocation list
  in v1).
- **Contacts.** A new invite kind `contact` (services/pairing.py), the same `<ticket>#<code>`
  string sent any way you like. Redeeming it swaps signed cards `{person key, name, endpoint ids
  of all that person's machines, at}`. Each side stores the other in a `contacts` table, which
  travels in library sync. Every endpoint on the card becomes a paired device of a new kind
  CONTACT. Its token opens only `/api/share/*`, scoped in auth.py the way PHONE tokens are; the
  sidecar needs no change, because its `T` stream already admits paired endpoints only. A re-signed
  card (a new machine, a new name) is pushed to contacts and replaces the old one only when it
  verifies against the stored key.
- **Shares.** `peer_shares(session_id, contact_id, parts, transcript, by, at, revoked_at)`; it
  travels in library sync, so any of the owner's machines can serve it. The dialog reuses the
  Publish dialog's part picker. On by default: title, date, summary, decisions, open questions,
  action items, chapters, topics, attendees and notes. The transcript is a tick, off by default,
  that warns it cannot be taken back once sent. Names come as they are, with an optional "hide
  identifiers" (publishing's `redact_identifiers` + `redact_contacts`). Never shared: audio,
  clips, recordings paths, speaker embeddings, voice profiles, publications (keys included),
  seals, versions, supervision and other people's shares. Recipients see the names of the others a
  meeting is shared with, since those people's comments and notes edits are visible to them.
- **What a recipient gets: a `SharedMeeting`** (models/shared_meeting.py), the projection of one
  share. It holds the chosen parts, comments, item done/follow state, the notes document snapshot
  when notes are shared, and `version`. It is signed by the owner's person key. Nothing outside
  the chosen parts is ever serialized. The notes part carries its whole op history, so anything
  ever typed in the notes goes with it; the dialog says so next to the tick.
- **The hub.** The recipient's backend pulls from any of the owner's machines it can reach, using
  `GET /api/share/changes?since=` through the tunnel with its contact token. That returns signed
  `SharedMeeting` documents for the meetings shared with that contact, oldest change first, plus
  tombstones for unshares and deletes. The recipient pushes its own work with
  `POST /api/share/contributions`. That is a batch of signed records `{id, kind, session_id,
  payload, author key, at}`, signed over canonical JSON; for notes, the payload is the frame's
  SHA-256 and the frame travels beside it. Record kinds: `comment` (create, edit, delete,
  resolve), `item_flag` (done or follow by item id) and `notes_frames` (0xDE / 0xAF frames).
  `POST /api/share/poke` asks for an immediate pull in either direction, so live notes typed on
  a recipient reach the owner's open editors within seconds while both are online. Edits made
  offline queue on the recipient ("2 comments waiting for Corey's computer") and are pushed on the
  next connection.
- **The owner's machine checks every record before it is written.** The signature must verify.
  The signer must be the contact bound to the endpoint the request came through; in hub mode,
  nobody forwards someone else's records. The share must be live and include the part. The rule
  must allow it: anyone shared with comments and ticks; notes edits only when notes are shared;
  edit or delete only your own comments; resolve as the thread author. Accepted records go
  through the normal write paths (comments service, `_set_action_item_flag`, the notes relay's
  `receive_frame`) with author = the contact's name and `author_id = "p:<first 16 hex of key>"`.
  They log history lines, notify open clients, travel on to the owner's other machines in library
  sync, and are served back to every recipient. Rejected records come back with a reason, which
  the recipient shows on the item. Records are idempotent by id.
- **Signatures are kept.** A table `signatures(subject, signer, sig, at)` stores records by id and
  notes frames by hash, on the owner's machines and the recipients'. Records, history and per-frame
  authorship can then be checked later, and a future mesh has something to filter on. Because the
  hub refuses a bad frame before it reaches the log, no op is ever orphaned: v1 signs frames, not
  op ids. Signing op ids, and dropping unsigned ops when rendering, is what a mesh would need
  (Later).
- **On the recipient's side.** A shared meeting is a `sessions` row with
  `shared_from = <contact id>` and a sidebar group "Shared with me". The header shows "Shared by
  <name>". `canChange` is false except for the allowed writes, and no automation runs (there is no
  audio). Keyword search covers these meetings. They are left out of Ask and semantic search, the
  digest and Obsidian export unless a setting turns each on (all off by default; open question).
  Tasks owned by you (`mine_matcher`) appear in your Tasks view. Shared meetings do not travel in
  library sync: each of your machines is on your card, so each one pulls them from the owner.
- **Unshare, delete, remove a contact.** Each one becomes a tombstone on the next pull. The
  recipient's app deletes its copy and says why. This only works with a cooperating app, and the
  dialog says copies cannot be recalled, as for publications. Removing a contact revokes all
  their shares and drops their CONTACT devices.
- **Refused in team mode** on both sides, like library sync. The team server stays the central
  option.
- **Hardening first** (found while mapping, independent of the rest): bind `SyncPeer.id` to its
  endpoint id and refuse a hello from another endpoint; keep `SyncSession.publications` between
  one person's machines only, so it never goes into any share; contact tokens can never reach
  `/api/sync/*`.
- **UI.** Settings → People (contacts: add by invite, compare fingerprints, rename, remove; your
  own fingerprint and name). ShareMenu gets a "People" section that opens the part picker for each
  person. Sidebar "Shared with me". On the recipient: a header chip and a "waiting for <owner>"
  line on queued work. On the owner: a "from <name>" byline on contributed comments and notes
  edits.
- **Prerequisites.** Privacy (1): iroh is end-to-end encrypted between paired peers, content is
  chosen per share, and the transcript is off by default; notes ops stay permanent, and the dialog
  says so. Authorship (2): person keys plus signed records and frames, enforced at the hub;
  op-level signing waits for a mesh. QUEUE 56 (3): avoided (no nested objects). Format (4): met.
- **Tests.** Three in-process apps (owner, recipient A, recipient B) through
  `httpx.ASGITransport`, like test_sync_*.py. Cover: the share projection never holds unchosen
  parts (a test asserts there is no transcript when it is unticked); contact tokens are refused
  outside `/api/share/*`; tampered, replayed and forwarded records are refused; notes frames from
  A reach B through the owner; unshare deletes on the recipient; key adoption at library-sync
  pairing; the hardening items.
- **Not in v1:** a mesh, so nothing reaches recipients while every owner machine is offline;
  presence across machines; audio and clips; resharing; recipients editing the summary or
  transcript; recalling copies; per-device keys.

Open for Corey: whether shared-with-me meetings join Ask, semantic search, the digest and Obsidian
export by default; the name in the UI ("People", "Contacts" or "Share with a person"); whether to
ship the hardening items on their own first.

## Later, each behind the prerequisites below

- **Live shared notes** during a meeting: a scratchpad attendees type into (desktop, or the team
  server's web app with the wasm build); the copilot writes as another author; the summary
  already takes external notes (services/pipeline.py).
- **Comments** on transcript lines and summary items: marks with a minted kind per comment and a
  derived reply thread (APP_NOTES.md 11, 16). Reviewers annotate without write access to the
  meeting.
- **Tasks, people and decisions as objects** that meetings link to: an action item keeps one
  identity across meetings instead of `same_item` text matching; Tasks, topic notes and the
  decisions log read those objects.
- **Team sharing without a central copy**: drafted as phase 8 (owner as hub). Later: a mesh, so
  recipients sync with each other while the owner is offline (per-op signatures, unsigned ops
  dropped when rendering).
- **Publishing**: designed as phase 7 (encrypted snapshots, not a space). Later: readers
  following live edits or commenting (a hashseq space), publishing over Logos Delivery or
  Storage once a supported module and a web reader exist.
- **Records**: a meeting's frontier is a hash of its exact state, a candidate for sealing record
  versions (services/records.py); clips as artifacts can be erased for retention while the rest
  still verifies.

## Prerequisites before anything leaves Corey's machines

1. **Privacy.** Either encrypt ops and artifacts per document (key in a capability link; the
   relay becomes a store-and-forward mailbox and can no longer merge), or sync only between
   paired peers over iroh (already end-to-end encrypted). Share derived, redacted documents
   (summarization/privacy.py), never raw transcripts: text in an op cannot be erased.
2. **Authorship.** Sign op ids (an op id commits to its content and history, so a signature over
   it is an attestation), e.g. a signature register keyed by op id, and render only ops signed by
   members. Filtering ops orphans their dependents, so this needs design with David. "Readers may
   only tick action items" is a read-time rule on top of it.
3. **QUEUE.md 56** decided upstream (we would nest objects: items inside a meeting).
4. **Format stability.** Stores we persist must decode after a hashseq upgrade; pin the crate
   by commit and keep its grammar test vectors in our CI.

## Questions for David (answered 2026-10-13)

- **Signatures or authorship in the core?** No. The plan is an authorship layer on top:
  signatures in a side table referencing the op ids they sign. Authorship in the core would
  break what the core is for: permissionless, no overhead between 1 editor and 1M; signatures
  mean tracking identities, so space grows with the number of editors. → Prerequisite 2 stays
  ours, as sketched: a signature table keyed by op id.
- **Private (encrypted) spaces?** Not in the core either: the core is like JSON; encryption is
  a layer on top and the app picks how. → Prerequisite 1 stays ours.
- **An app-conventions crate?** Would be good, but he is holding off doing it too early. → Keep
  our own thin wrappers; do not wait for it.
- **Canonical encoding stable enough to persist user data?** Yes, stable now. → Prerequisite 4
  is met; keep pinning the crate by rev.
- **Basecamp module official, over which topics?** Only in his personal registry: the company
  has no bandwidth to support it officially (a very large app; fine to be vibed on a personal
  registry, higher stakes if official). → Publishing through it would depend on an unofficial
  module; treat Logos publishing as exploratory.
- **QUEUE.md 56?** Undecided, needs exploration. A shared op space would let objects move between
  containers (e.g. elements of a parent seq moved into a child seq in a nested tree); a sharded op
  space allows some simplifications. → Prerequisite 3 stays open; our designs avoid nested
  objects (phase 2's per-block histories are independent seqs).

## Out of scope

Real-time co-editing in the desktop UI before phase 4; replacing SQLite; anything that sends
meeting content to a relay or another person before prerequisites 1 and 2.
