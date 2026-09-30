# A pilot for a financial advisory firm

Drafted 2026-09-30 for Corey's review; nothing here is agreed yet. Goal: one advisory firm can
run Mnemosyne for real client meetings for a month, and its chief compliance officer (CCO) is
comfortable with it.

What we know about the firm: advisors are on Windows and macOS; the CRM is HubSpot; meetings
happen on video calls (Zoom/Teams), by phone and in person; no archiving vendor (email or
otherwise), so Mnemosyne has to be its own system of record for meeting records.

Why they would pick Mnemosyne over the advisor notetakers already on the market: everything
(audio, transcripts, summaries, the models) stays on a machine the firm owns. That promise holds
only with a local LLM, so the pilot ships with one configured.

Checks before every commit: in `backend/` `uv run ruff check . && uv run ruff format --check . &&
uv run pytest`; `bash scripts/gen-api-types.sh --check` (without `--check` after model changes);
`pnpm check`; `pnpm test:e2e`. Test imports that only the gpu extra has with them hidden
(CI has no ML packages). One commit per item; items in phase order.

Constraints (CLAUDE.md): never play audio on this machine; nothing is sent to HubSpot or any other
outside service during development except a HubSpot developer test account Corey sets up;
Mnemosyne never gives financial advice itself (summaries record what was said).

Which records rules apply to the firm (SEC Rule 204-2 for an RIA, FINRA for a broker-dealer,
state recording-consent laws) is the CCO's call. The job here is to make every likely question
answerable with "yes, here is how", and to write it down (item 12).

---

## Phase A: what the pilot needs

### 1. Firm server: an appliance, reachable over HTTPS

Advisors don't install anything; one machine in the office (or a VM in the firm's own cloud
account) runs the backend in server mode.

- `docs/firm-server.md`: hardware (any NVIDIA GPU with 8 GB+; a small office box is enough for a
  handful of advisors), install (the backend from source with `uv sync --extra gpu --extra onnx`,
  a systemd unit running `mnemosyne-backend --host 0.0.0.0`), Ollama with one recommended model
  preloaded, backups to a second disk (services/backup.py).
- HTTPS is required: browsers only allow microphone and screen-audio capture on secure pages.
  Document two ways: Tailscale (`tailscale serve`, which already has a guide in
  docs/remote-access.md) and a certificate from the firm's own domain behind Caddy.
- `scripts/firm-server-check.sh`: GPU visible, models load, Ollama answers, HTTPS works, the
  backup folder is writable, the clock is right (timestamps are records).

### 2. Advisors and roles

Today one token opens everything. A firm needs each advisor to see their own meetings and a
reviewer to see all of them.

- `users` table: name, email, role (`advisor`, `reviewer`, `admin`), token hash, created/disabled.
  Sign-in by invite link, like phone pairing (services/pairing.py): the admin creates an advisor,
  the advisor opens the one-time link, and their browser gets a per-user token. No passwords to
  store or reset in the pilot.
- `sessions.owner_id`. Advisors list, search, ask and export only their own meetings; reviewers
  and admins see all; the existing single-user token stays an admin (so the desktop app keeps
  working unchanged).
- Every read of a meeting (open, play, export, search hit opened) is logged per user in
  `session_events` (services/history.py), so "who looked at this client's meeting" has an answer.

### 3. Browser recorder

A `/record` page on the firm server that works in Chrome and Edge on Windows and macOS:

- Microphone plus the call's audio: `getDisplayMedia({audio: true})` for the other side of a
  Zoom/Teams call. On Windows, sharing the entire screen with "Share system audio" gives
  everything the computer plays. **To verify first:** system audio on macOS (Chrome may only
  offer a tab's audio there). Fallbacks, in order: join the call in a browser tab (tab audio
  works everywhere), or a small native helper for macOS later (ScreenCaptureKit).
- Audio goes to the server as it is recorded, not at the end: an AudioWorklet sends 16-bit PCM
  frames per source over the existing WebSocket; the server writes them into the same growing
  WAV files pw-record writes today. That way live transcription, Nemotron streaming, the copilot,
  recovery after a crash and multi-part meetings all work unchanged, and a closed laptop loses
  seconds, not the meeting.
- Mic and call audio are separate sources, as on Linux: the advisor is their mic, clients are
  diarized on the call channel.
- In-person meetings: the same page on a laptop or tablet with one mic (diarized), or the phone
  page (`/m`) as today.
- Phone calls: through a softphone on the computer (Teams, Zoom Phone, RingCentral app), the page
  records them like any call. Desk or mobile calls: upload the phone system's recording afterwards
  (the import already exists); a RingCentral/Zoom Phone import is a later item if they use one.

### 4. Recording consent

- Before recording, the page asks which consent applies (all parties informed and agreed / one
  party only where allowed / in person, notice given) and optionally shows a short script to read
  out. Starting without a choice is not possible when `require_consent` is on (default on in
  firm mode).
- The choice, who made it, and when go into the meeting's history; the meeting shows it, and
  exports carry it.

### 5. Redaction of financial identifiers

The redactor (summarization/privacy.py) swaps emails, phone numbers and known names. Add:
Social Security numbers (with and without dashes), account and routing numbers (digit runs that
are not dates, amounts or phone numbers; routing numbers checked with their checksum), card
numbers (Luhn), and dates of birth spoken as such. Used for any cloud LLM call (none in the pilot,
but a setting could change that), for exports, and optionally (`redact_stored_transcripts`) for
the stored transcript text itself, keeping the audio untouched. Tests with realistic spoken forms
("my social is four five six ...", "account ending 1234").

### 6. Advisor meeting types and a client-facts summary

Meeting types (services/meeting_types.py) are words in the title that pick a summary style and
instructions. Ship an advisor pack, turned on in firm mode: discovery, annual review, onboarding,
plan presentation, service call. Plus a summary style `advisory` whose structured output adds
`summary_data.client_facts`: goals, life events, changes in income or risk tolerance, accounts
and beneficiaries mentioned, next review date, each with where it was said (like `decision_at`).
Summaries report what the client and advisor said; the prompt forbids adding recommendations.

### 7. HubSpot

`services/hubspot.py`, modelled on the Linear/Jira trackers (services/trackers.py), with a HubSpot
private-app token (scopes: contacts read, notes/tasks/meetings write):

- Match the meeting to HubSpot contacts: calendar attendees by email first, then names in the
  transcript's speakers against contact names; the advisor confirms the match on first push.
- Push: a meeting engagement (title, time, duration) with the summary as its body, a note with
  client facts, and the action items as tasks assigned to the advisor, all associated with the
  matched contacts (and their company, which is how firms usually model a household in HubSpot).
- Per-meeting "Send to HubSpot" button, and an automatic push after the summary for meeting types
  that ask for it. The push is logged in history; a second push updates instead of duplicating.
- Tested against a mock transport, then once against Corey's HubSpot developer test account.

### 8. Records the firm can rely on

With no archive vendor, Mnemosyne keeps the records:

- Versions, not overwrites: transcript edits, summary regenerations and speaker renames keep the
  earlier version (`session_versions`), shown in "Parts & history".
- A retention floor instead of a ceiling: `records_retention_years` (e.g. 6). Deleting a meeting or
  its audio before then is refused for advisors and allowed for admins only with a reason, which
  is logged. `legal_hold` on a meeting refuses it for everyone.
- Tamper evidence: when a meeting is finished, a SHA-256 over its audio, transcript and summary is
  recorded, and each later version is chained to the previous hash; a "Verify" action recomputes.
- Exam export: one meeting or a date range as a zip: audio, transcript (text and JSON), summary,
  all versions, consent, history and hashes, plus a readme saying how to check them.
- Offsite backup: backups already run on a schedule; document pointing them at a second location.

### 9. Pilot kit

- A demo annual-review meeting (synthetic, two voices) in demo mode, so the firm sees the result
  before recording a client.
- `docs/security-overview.md` (and a PDF of it) for the CCO: what is recorded, where it is stored,
  encryption, who can see what, retention and holds, what (if anything) leaves the building, how
  to export for an exam, how to delete at the end of a pilot.
- A one-page pilot checklist: server ready, advisors invited, consent wording agreed, HubSpot
  connected, first meeting reviewed together.

## Phase B: after the pilot starts

### 10. Supervision queue

Compliance phrases flagged on the final transcript (a configurable lexicon: "guarantee",
"can't lose", "risk-free", "you should buy/sell", performance promises), built on the mention
spotter (transcription/mentions.py). A reviewer page lists meetings with flags, unreviewed first;
the reviewer marks them reviewed with a note (logged).

### 11. Households

People (services/people.py) grouped into households; client facts from item 6 accumulate per
household across meetings, and the pre-meeting brief (services/brief.py) shows them ("last
review: daughter starts college in 2027; wants to revisit the 529"). Synced from HubSpot companies
when connected.

### 12. Native recorders, if the browser falls short

Only if item 3's macOS system-audio check fails or advisors want a tray app: the Tauri shell
already builds on Windows and macOS; what is missing is capture (WASAPI loopback on Windows,
ScreenCaptureKit on macOS) sending into the same WebSocket as the browser recorder.

---

## Open questions for Corey

1. Where would the firm's server live: a box in their office, or a VM in their own cloud account?
   (It decides whether "nothing leaves the building" is literally true.)
2. How many advisors in the pilot, and is one of them also the reviewer/CCO?
3. Is a local model's summary quality acceptable to them, or would they allow a cloud LLM with
   redaction (items 5 and 6 work either way)?
4. Do they use a softphone or a phone system that keeps recordings (RingCentral, Zoom Phone)?
5. Can you create a HubSpot developer test account for item 7?
