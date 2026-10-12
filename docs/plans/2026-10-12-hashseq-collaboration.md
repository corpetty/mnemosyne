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

## Phase 1: two-way Obsidian sync (local only)

The history of each exported meeting note lives in the data directory, never in the vault (only
notes with our `source: mnemosyne` frontmatter, as today).

- On export: render the note (export/obsidian.py `render`) and diff it into ops against the text
  last written (the app's side). If the file in the vault changed since we wrote it, diff it into
  ops against the same base (the user's side). The two are concurrent forks of one frontier;
  merge them, write the merged text to the vault, remember it as the new base.
- `sync_vault` (every 5 min) does the user's side alone, and merges back into the app what it
  understands: ticks (`parse_ticks` stays; the mtime rules go), edited action item text, edited
  summary paragraphs (as a summary edit, `edited_by` "Obsidian").
- After a merge, normalize what must be one value: a checkbox with more than one status char,
  duplicated frontmatter keys. Gap conflicts (two inserts at the same anchor, which hashseq can
  detect) on a line both sides changed are listed in the meeting's history (services/history.py)
  so they can be looked at; nothing is thrown away.
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

## Phase 3: Corey's own machines

Sync meeting documents between two paired computers (the "desktop" devices link/ already trusts)
over iroh: frontier hello, missing ops, artifacts on demand. mDNS first, relays only when needed,
as for remote access. No authorship or access questions yet: every peer is the same person.

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
- **Team sharing without a central copy**: each member's machine holds the meetings shared with
  it.
- **Publishing**: a curated meeting (summary, decisions, clips) as a space, through David's
  Basecamp module over Logos Delivery or a relay, with a static wasm viewer. Fits "Logos
  Messaging is later".
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

## Questions for David

- Are signatures or authorship planned in the core or for an app layer?
- Private (encrypted) spaces?
- An app-conventions crate (APP_NOTES.md 5: the wrappers every app rewrites)?
- Is the canonical encoding stable enough to persist user data now?
- Is the Basecamp module headed for the official Logos set, and over which delivery topics?
- Which way is QUEUE.md 56 going?

## Out of scope

Real-time co-editing in the desktop UI before phase 4; replacing SQLite; anything that sends
meeting content to a relay or another person before prerequisites 1 and 2.
