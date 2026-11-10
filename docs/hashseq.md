# How Mnemosyne uses hashseq

For David Rusu. As of 2026-11-10 (Mnemosyne 0.31.0, hashseq pinned at `94f5e3c`).

Mnemosyne is a Linux desktop app that records meetings, transcribes them locally, summarizes them
with an LLM and exports them to Obsidian. Since mid-October hashseq has been the merge engine
under everything in it that more than one writer can edit: the Obsidian note, the summary, the
meeting's live notes, and two computers sharing one library. This page says what we built on it,
how, and what we ran into. The full design log, phase by phase, is
[plans/2026-10-12-hashseq-collaboration.md](plans/2026-10-12-hashseq-collaboration.md); this is the
short version.

## The short version

- **Two uses.** (1) A plain `HashSeq` as a three-way text merge with history (nool's idea), for
  the Obsidian note, each block of the summary, and library sync's text fields. (2) A `HashWeb`
  holding one meeting's notes as a single flat seq with structure in marks, edited live in the
  browser through your wasm bindings and relayed by a server that cannot decode it.
- **One crate, two builds.** `notes-core` (our markdown model over hashseq) is compiled both
  into the sidecar binary and into the browser's wasm module, so a paste in the browser and a fold
  on the server give the same document.
- **We kept to your answers from 2026-10-13.** Encryption and authorship are layers on top, not
  in the core; we pin by rev and treat the encoding as stable; we avoid nested objects (QUEUE 56).
- **hashseq has been solid.** We have not traced a bug to hashseq itself. What we hit is about
  conventions (how to diff into ops, what to keep out of text) and a few client-side details
  (QUEUE 39, 53).

## Where the code is

| Crate / file | What it is |
|---|---|
| [notes-core/](../notes-core/src/lib.rs) | The notes document over `HashWeb`: model, markdown render/parse, `set_text` (markdown → ops by diff), fold, merge, server-side marks. The module doc in `lib.rs` is the full document spec. |
| [notes-wasm/](../notes-wasm/src/lib.rs) | Re-exports `hashseq::wasm::*` plus two notes-core functions (`parseMarkdown`, `renderMarkdown`). Built reproducibly (pinned rustc and wasm-bindgen) into `src/lib/notes/pkg`, committed, and checked by CI. |
| [link/src/notes.rs](../link/src/notes.rs) | `mnemosyne-link notes merge`: the two-branch three-way text merge on a bare `HashSeq`. |
| [notes-core/src/api.rs](../notes-core/src/api.rs) | `mnemosyne-link notes doc`: the stateless JSON ops the backend calls (`fold`, `set_text`, `merge`, `mark_comment`, `mark_comment_at`, `set_kind`). |
| [src/lib/notes/](../src/lib/notes/) | The browser side: `sync.ts` (frames, QUEUE 39 handling), `ops.ts`, `doc.svelte.ts` (socket), `editor/` (our contenteditable; several primitives ported by name from your `web/kb.js`, MIT, attributed in `editor/dom.ts`). |
| [backend/mnemosyne/services/shared_notes.py](../backend/mnemosyne/services/shared_notes.py) | The relay: append log, folds, catch-up of text written outside the document. |

All three Rust crates depend on `hashseq = { git = "https://github.com/davidrusu/hashseq", rev =
"94f5e3c" }`. The sidecar and the browser must agree byte for byte, so they move together.

## Use 1: three-way text merges (`HashSeq`)

**Problem.** The app re-renders a meeting's Obsidian note on every export and re-summary, while
the person edits the same note in the vault. We used to overwrite the note.

**What we tried first (phase 0, 2026-10-12).** `nool merge` on a real re-summarized note
(2,249 lines) with nine hand edits garbled every edit into one run inside a transcript line. The
cause was the diff, not hashseq: nool diffs characters (Myers) and, past `MYERS_MAX_D` = 1,000,
replaces everything between the first and last difference in one delete + insert, so every edit in
that span is anchored on removed text. A re-summary always exceeds it. Diffing **by lines
(patience), then by words inside each replaced block** kept all nine edits clean, and edits on
reworded lines followed the new wording. hashseq itself converged in both orders, in 0.05–0.6 s,
with a history 1.16× the note's size. (Worth knowing for nool.)

**What we built.** A stored history holds two seqs with one origin: `combined` (everything; its
text is what we last wrote) and `app` (only the app's ops; its text is what the app last rendered).
A new rendering is diffed against `app`, the vault file against `combined`, and the two are
merged. With only one branch, a user edit survived just the export that merged it: the next
rendering, diffed against text containing the edit, deleted it. The stored form is
`MNHS` + version + the two `encode_hashseq` encodings, kept in SQLite (not in the vault).

Same mechanism, reused:

- **The summary as a document.** One two-branch history per block (the summary body, each decision,
  question and action item, keyed by stable ids), with the LLM's output standing in for `app`. A
  re-summary merges with hand edits instead of replacing them. We dropped the earlier sketch of the
  summary as one `HashWeb` with item kvs: per-block independent seqs avoided nested objects and
  kept item-level rules (an item the human deleted stays deleted) in Python.
- **Library sync** (two of one person's computers sharing a library) merges text fields three-way
  through the same op with an explicit `base`.

**Rule we learned: state that must be one value never goes in text.** Two sides each ticking the
same `[ ]` after other edits gives `[xx]` (or `[x][x]`). Checkbox state is stripped to `[ ]` before
diffing and restored from our own state afterwards.

## Use 2: the live notes document (`HashWeb` in the browser)

During and after a meeting, everyone who has it open (the desktop app, a team server's web app,
guests through a link) types into one document live.

**The model** ([notes-core/src/lib.rs](../notes-core/src/lib.rs)). One flat seq per meeting,
opened at origin `sha256("mnemosyne/notes/" + meeting_id)` so every client and machine opens the
same object with no create race. We chose this over kb's page kv + block seqs because comments must
span blocks, nested objects raise QUEUE 56, and we did not want to vendor kb's DOM layer whole.

- Text is lines ending in `\n`. A line's kind is a **closed `block` mark on its `\n`**
  (`h1 h2 h3 bullet number task task_done quote code table table_head image`), so a reworded line
  keeps its terminator and its kind. `set_text`'s diff makes each `\n` its own word for this reason.
- Inline: `bold`/`italic`/`strike` use `markRange` anchors (grow at the end); `code`, `link`
  (value = URL) and `comment:<id>` use `markRangeClosed`. Tables are `table` lines with U+001F
  between cells; column alignment is an `align` mark on the head row's `\n`.
- Concurrent values stay live (we rely on the MVR behaviour of marks): `task` + `task_done` reads as
  done; other kind conflicts show the first in `markedSpans` order; two live link URLs render as
  "(link conflict)".
- App-only marks: `ref` names what a line is for the backend (`sec:summary`, `sum:<id>`, `cp:<n>`),
  and `image` holds `<sha256>.<webp|jpeg>@<w>x<h>`. To a client that does not know them they are
  unknown marks and change nothing. **Image bytes never enter the document**; they live beside it,
  named by hash.
- Indexes are code points (one element per `char`), JS strings are UTF-16, so the client converts.

**The wire.** Clients send `takeDeltas()` (0xDE) and artifact frames (0xAF; any mark kind or value
over 14 UTF-8 bytes, which is most URLs and every `comment:<id>`), artifacts first. The backend
(`/ws/notes/{id}`) is an **opaque relay**: it checks the tag and size (≤ 2 MB), appends the frame
with its author to a log, relays it, never echoes. A join gets the folded snapshot, then the
unfolded frames, read in one step so nothing is lost or doubled.

**Folding.** The backend cannot decode hashseq, so the stateless sidecar (`notes doc fold`) folds
snapshot + frames into a new snapshot and renders markdown. That markdown is what search, the
summary, Obsidian and our MCP server read. Text written around the document (an API call, a
sync text merge) is caught up into it with `set_text` (diff into ops) before the next join.

**Things the server authors.** A read-only reader can comment, so the server places
`comment:<id>` marks from element ids (`mark_comment`) or from a position in the plain text
(`mark_comment_at`). A tick from our Tasks view flips a task line's kind with `set_kind`.

**Library sync of the notes** is a plain op-set union (`notes doc merge`), no base needed.
Because equal op sets encode to equal bytes, "did the union add anything?" is a comparison of the
merged snapshot with ours.

## Features that ride on the notes document

| Feature | How hashseq is involved |
|---|---|
| Comments on notes | `comment:<id>` closed marks; the thread lives in our comments table; the fold reports each mark's current range, or `null` once all its characters are gone (detached). |
| Action items typed in the notes | A `task` line with a person chip is a task object; ticking anywhere becomes a `set_kind` op. |
| Copilot and summary in the notes | The backend writes lines tagged with `ref` marks and reads human edits to them back. |
| Images | An `image` line kind plus mark; bytes travel separately, checked by sha. |
| Live carets | Selections are sent as element ids (`seqIdAt`) so they survive concurrent edits; presence is separate JSON, not ops. |
| Guest notes (a link for people without the app) | The same 0xDE/0xAF frames, sealed with AES-GCM under a key only the link's fragment carries, relayed by a room on a publish host or over iroh from the owner's machine. The relay becomes a store-and-forward mailbox that cannot merge; the owner posts sealed snapshots for late joiners. Replays and reordering are harmless because the CRDT is idempotent. |
| Sharing with other people's desktops | The owner's machine is the hub. Recipients send notes frames as **signed contributions** (an Ed25519 person key signs a record naming the frame's SHA-256; the frame travels beside it); the hub verifies before a frame reaches the log, so no unsigned op is ever stored and nothing is orphaned. |
| Publications | Do **not** use hashseq: an encrypted snapshot per version. |

## The two layers you said belong outside the core

- **Privacy.** Between one person's machines, sync runs over iroh (end-to-end encrypted, paired
  endpoints only). Toward guests, frames are sealed per room. Our servers relay ciphertext only.
- **Authorship.** v1 signs *frames*, at a hub, not op ids. That works because there is always a
  hub that refuses bad frames before they spread. A mesh (recipients syncing with each other while
  the owner is offline) would need what you sketched: signatures over op ids in a side table, and
  rendering only signed ops. We have not built that.

## What we ran into

- **QUEUE 39.** The upstream clock only moves when the upstream states it or when we send, so a
  delta received from the relay ships back on the next drain. Our client drains before applying a
  remote frame, then calls `applyFrontier(encodeFrontier())`
  ([src/lib/notes/sync.ts](../src/lib/notes/sync.ts)). Reconnects apply the server's backlog into a
  throwaway replica, merge it in without sending, and set the frontier from it.
- **QUEUE 53.** A no-op mark toggle still costs an op; `applyMark` skips toggles that change nothing
  ([src/lib/notes/ops.ts](../src/lib/notes/ops.ts)).
- **The 14-byte artifact threshold** is easy to miss: a peer that gets the delta without the 0xAF
  frames holds unresolved ids. We resend artifacts on every reconnect (duplicates are harmless).
  We kept the `align` value within 14 bytes on purpose.
- **Concurrent identical edits double** in text (`[xx]`), as above. Line kinds as marks fixed it for
  the notes; checkbox state is out of the text for the Obsidian merge.
- **Typed text is permanent.** Once a frame has spread, its text cannot be erased. The share dialog
  says so; transcripts are off by default when sharing, and publications carry redacted, chosen
  quotes rather than the transcript.
- **Move is unused.** Dragging a line in our flat seq would be delete + insert, so typing inside a
  moved line by someone else stays behind. We left line dragging out until we understood `Move`.
- **The frontier as a record seal.** We wanted to seal the notes' frontier ("history only grew
  since seal N"). Deferred: the frontier moves when the visible text does not (type then delete, a
  ref mark), and it exists only where a fold ran, so it differs between machines until both fold.

## Questions for today

1. **QUEUE 39**: is a fix upstream planned (the clock advancing on applied remote deltas), or is
   our client-side order the intended pattern?
2. **Move**: could moving a line (a run of elements ending in `\n`) keep concurrent edits inside it,
   in a flat seq? That is what line dragging needs.
3. **Erasure**: any plan for erasing typed text from history (retention, deletion requests), or is
   "small values are permanent, only artifacts can be erased" final?
4. **Signing op ids for a mesh**: your side-table sketch, and what happens to dependents of an
   unsigned op when rendering. Has QUEUE 56's direction changed since October?
5. **Upgrades between peers** (QUEUE 48): unknown op kinds are rejected. If a future hashseq adds
   one, our older desktops and guest pages would reject frames from newer ones. Should we plan a
   version handshake, or will unknown kinds be carried?
6. **Versioning**: we pin `94f5e3c` (your HEAD since 2026-09-28). Do you expect tags or releases,
   and should we run your grammar test vectors in our CI?
7. **A "contains" check**: a cheap way to ask whether a frontier includes an earlier one, for
   record seals.
8. **The nool diff finding** above: useful for nool? We can contribute the line-then-word diff.
