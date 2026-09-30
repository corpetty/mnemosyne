# Pilot checklist

For running a month-long Mnemosyne pilot at a financial advisory firm. The server set-up is in
docs/firm-server.md; the compliance officer's overview is docs/security-overview.md.

## Before the first client meeting

- [ ] The server passes `scripts/firm-server-check.sh <address>` (GPU, HTTPS, firm mode, model,
      disk, clock, encryption key).
- [ ] Encryption is on and the **recovery code is printed** and stored with the firm's other
      recovery codes.
- [ ] Backups: a folder on a second disk or network storage, daily, a week or more kept.
- [ ] The compliance officer has read docs/security-overview.md and agreed:
  - [ ] the **consent wording** (Settings → Recording → Consent),
  - [ ] the **records period** (Settings → General),
  - [ ] whether identifiers are also masked in stored transcripts (Settings → AI),
  - [ ] the **supervision phrases** (Settings → General → Supervision).
- [ ] People added (Settings → General → People and access): the admin, each advisor, the
      compliance officer as reviewer. Each has opened their own invite link.
- [ ] The **demo meeting**: `python3 scripts/make-demo-meeting.py`, then Import the WAV and name it
      "Annual review (demo)". Check together: two speakers told apart, the summary and its client
      facts, action items, "[SSN]" in the meeting's Markdown export (Export tab; in the transcript
      too when stored transcripts are masked), the Record card's seal. (Synthetic voices
      are harder to transcribe than people; a few misheard words are expected.)
- [ ] HubSpot (if used): token and owner email in Settings → Notes & sharing, **Test**, then send
      the demo meeting to a test contact and look at it in HubSpot.
- [ ] Each advisor has tried a short test recording in their browser: microphone, and the call's
      sound shared (Windows: Entire screen + Share system audio; Mac: the call in a browser tab).

## During the pilot

- [ ] After the first week: review a few real meetings with the advisors and the compliance
      officer. Transcript quality, summaries, client facts, anything missed or wrong.
- [ ] Weekly: the reviewer works through the Review view; the server's check script; that backups
      are arriving.
- [ ] Problems: note the meeting and the time; the admin's Settings → General → Troubleshooting
      has "Copy diagnostics" (settings and recent log lines, no recordings or transcripts; the log
      can name meetings, so read it before sending it outside the firm).

## At the end

- [ ] Decide: continue, change, or stop.
- [ ] If stopping: export what must be kept (Settings → General → Records → Export for an exam),
      then erase as described at the end of docs/security-overview.md.
