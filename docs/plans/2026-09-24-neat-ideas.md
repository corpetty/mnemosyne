# Neat ideas (2026-09-24, built while Corey was away)

Each idea is one commit on main so any of them can be reverted on its own. Not released.

1. **Talk time and meeting stats.** `GET /api/sessions/{id}/stats`: per speaker talk time,
   share, turns, longest monologue, words per minute; overall duration and silence. UI: a
   "who spoke when" strip above the transcript (click to seek) and a small stats table.
   MCP `get_meeting` includes the stats.
2. **Chapters.** The summary prompt also asks for 3 to 8 chapters with start times.
   `SummaryData.chapters = [{start, title}]`. Shown in the Summary tab (click to jump) and as
   headings inside the transcript; exported to Obsidian as a chapter list.
3. **Tasks across meetings.** `ActionItem.done`. `GET /api/action-items?status=&owner=` lists
   every item with its meeting; `PATCH /api/sessions/{id}/action-items/{idx}` toggles done.
   A Tasks view in the header (filter by owner and status, link to the meeting). Obsidian
   export and the digest show done items as `- [x]`.
4. **Follow-up draft.** A `followup` job asks the LLM for a short recap message (email or chat)
   from the summary, decisions and action items; saved on `SummaryData.followup`, shown with
   Copy in the Summary tab.
5. **Pre-meeting brief.** When the calendar banner shows a meeting, it also shows what is open
   from earlier meetings with the same title or overlapping attendees: open action items and
   questions, with links. `GET /api/brief?title=&attendees=`.
6. **Mention alerts.** Setting `mention_keywords` (e.g. your name). While recording, a live line
   from anyone but your own mic that contains a keyword triggers a `mention` event: a toast,
   a desktop notification (tauri-plugin-notification) and a highlight in the live transcript.

Checks after each: ruff, pytest, gen-api-types --check, pnpm check, pnpm test:e2e.
