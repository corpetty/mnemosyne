# Mnemosyne: security and records overview

For the chief compliance officer of a firm piloting Mnemosyne. It describes how the pilot is set up
(docs/firm-server.md); the settings named here are the firm's to choose. Which records rules apply
to the firm is the firm's determination; this document says what the software does.

## What it is

Mnemosyne records client meetings, transcribes them, tells the speakers apart, and writes a
summary with the client's goals, life events, agreed next steps and the next review date. It runs
on **one computer in the firm's office**. Advisors use it from Chrome or Edge on their own
computers; nothing is installed on their computers.

## What is recorded, and when

- Only when an advisor presses **Record**, and only after answering how the people in the meeting
  agreed to be recorded (everyone told and agreed; in person, everyone told; one-party consent where
  the law allows). The answer, who gave it and when are kept in the meeting's history. The firm
  words the script advisors can read out.
- The advisor's microphone and, if the advisor chooses to share it, the call's sound (never the
  screen). A meeting in the room is recorded through the laptop's microphone.
- A live transcript is shown while recording; the final transcript and summary are made after.

## Where the data is, and what leaves the building

Everything (recordings, transcripts, summaries, notes) is stored on the firm's server, encrypted
at rest (the database with SQLCipher, audio files with AES-256-GCM). The list of people (names,
emails, roles, and hashes of their sign-in tokens, never the tokens) is kept beside it, not
encrypted, so that signing in works before the meetings are unlocked. The
encryption key is sealed to that machine's TPM chip, so a copied disk cannot be read elsewhere; a
recovery code, printed once and kept by the firm, is the way back in after a hardware change.

The speech recognition, speaker separation and summary models run **on that server**. In the
pilot's configuration ("firm mode"), the software does not create any connection to a cloud AI
service, whatever a setting says; this is enforced in the code, not by a setting.

What does leave the server, and only as configured:

| What | When | Content |
|---|---|---|
| HubSpot (if connected) | an advisor sends a meeting, or automatically after a summary if the firm turns that on | the summary, client facts and action items, with Social Security, account, routing and card numbers and dates of birth masked. (When an admin syncs households, Mnemosyne reads companies and contact names from HubSpot and sends nothing.) |
| Tailscale (if used for HTTPS) | while in use | encrypted connection between the advisors' browsers and the server; Tailscale's service sees which devices connect, not the content |
| Software and models | at installation and updates | downloads only (code from GitHub, models from Hugging Face and Ollama); nothing is uploaded |
| Backups | on the schedule set | encrypted copies to the location the firm chooses (a second disk or its network storage) |

No usage data, analytics or recordings are sent to the software's authors or anyone else.

## Who can see what

- Everyone signs in with a personal invite link, used once, that signs in one browser; there are
  no passwords to reuse or phish. An admin can disable a person or sign them out of every browser
  at once.
- **Advisors** see and change only their own meetings, including in search, questions across
  meetings and exports. **Reviewers** (compliance) read every meeting and change none of the
  others'. **Admins** also manage people and settings.
- Opening, listening to and exporting a meeting is recorded in its history with who did it.

## Records

- **Nothing is overwritten.** Edits to a transcript, renamed speakers, a new transcription or a new
  summary keep the earlier version, with who changed it and when.
- **Sealed.** After each transcription, summary and edit, a SHA-256 fingerprint of the content and
  of each audio file is recorded, chained to the previous one. Each meeting shows whether its
  record still matches; a change made outside the application (a database edit, a replaced file)
  shows as such. The chain's latest value is included in exports and backups, so it can be checked
  against copies kept elsewhere. (Someone with full control of the server could rewrite the whole
  chain; copies kept elsewhere are what catch that.)
- **Retention.** The firm sets a records period (for example six years). Within it, deleting a
  meeting or its audio needs an admin and a written reason, and automatic clean-up leaves it alone.
- **Legal hold.** A reviewer or admin can put a meeting on hold; then nobody can delete any of it.
- **Deletion log.** Every deletion, with who, when and why, is kept after the meeting is gone.
- **Supervision.** Lines where someone uses a phrase the firm lists ("guarantee", "can't lose",
  "you should buy", ...) are flagged, and the meeting waits in the reviewer's queue until they mark
  it reviewed with a note, which goes into its history. Editing a transcript never removes a flag.
- **Export for an exam.** One meeting, or all meetings in a date range, as a single file: audio,
  transcripts, summaries, earlier versions, histories (consent, access), the seals, the deletion log
  and a checksum list that verifies nothing in the export changed.

## Sensitive numbers

Social Security, account, routing and card numbers and dates of birth said in a meeting are masked
in exports and in anything sent to HubSpot (for example "[SSN]", "[account ••1234]"). The firm can
also choose to mask them in the stored transcript itself (the recording keeps what was said).

## Limits to know about

- Transcripts and summaries are made by software and can contain mistakes: a misheard word, a
  sentence given to the wrong person. Advisors should review a meeting's summary before relying on
  it or sending it on. The summary is told to report only what was said and never to add advice
  or recommendations.
- Masking recognises numbers the way people usually say them; an unusual phrasing may be missed.
- On a Mac, the browser shares a browser tab's sound but not other apps': calls there are joined in
  a browser tab.
- The pilot runs pre-release software on one server; the backup schedule is what protects against
  losing that server.

## Ending the pilot

Export what the firm must keep, then stop the service and erase the server's data folder
(`/srv/mnemosyne`) and its encryption key (`/etc/credstore.encrypted/mnemosyne-key`). Without the
key, backups made during the pilot can no longer be read except with the recovery code, which the
firm then destroys as well.
