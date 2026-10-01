# Team server: sharing, per-person preferences, your tasks and digest

Corey, 2026-10-01: "plan 1 and 2 and 3, then build them" (after 0.12.0). On a team server a meeting
is only its recorder's (and the reviewers'); every setting is the admin's. These three items make
it a place a group works in. The desktop app (no one signed in) does not change.

Checks before every commit: in `backend/` `uv run ruff check . && uv run ruff format --check . &&
uv run pytest`; `bash scripts/gen-api-types.sh --check` (without `--check` after model changes);
`pnpm check`; `pnpm test:e2e`. One commit per item.

## 1. Sharing meetings

- `session_shares(session_id, user_id, by, at)`; `user_id = "*"` is everyone on the server.
- A member reads a meeting they own or that is shared with them (by name or with everyone).
  Reviewers and admins read everything, as now. Sharing gives read access only, with one
  exception: anyone who can read a meeting can tick its action items done (item 3).
- One rule in one place: SessionRepository's visibility (`_readable`, `visible_ids`,
  `list_summaries`, `get`) gains "or shared with me", so lists, search, Ask, tasks, people,
  topics, organizations, digests and exports follow. The WebSocket filter asks the repository
  (with a short cache), since sharing changes what someone may see during a connection.
- The owner and admins share and unshare (`GET/PUT /api/sessions/{id}/shares` with
  `{team, user_ids}`), logged in the meeting's history. `GET /api/users/directory` lists everyone's
  id and name for any signed-in person (the full user list stays admin-only).
- Automatic shares: with team members whose email is among the meeting's calendar invitees
  (`share_with_invitees`, on by default), and everything a person records when their preference
  says so (item 2, `share_new_meetings`).
- UI: a Share button in the meeting header (owner and admins, team mode): everyone, or chosen
  people. In the sidebar, a meeting that is not yours shows whose it is. The record export lists
  the shares in meeting.json.

## 2. Per-person preferences

- `user_prefs(user_id, data)` in the database (encrypted at rest with the rest). A `UserPrefs`
  model, every field optional (unset = the server's setting): `language`, `summary_style`,
  `summary_instructions`, `mention_keywords`, `calendar_ics_url`, `hubspot_owner_email`,
  `share_new_meetings` ("private" or "team"), `digest_weekday`, `digest_hour`.
- `prefs.effective(app, user_id)`: the server settings with that person's preferences on top. Work
  on a meeting uses its owner's (language, summary style and instructions, mention alerts, the
  HubSpot owner); views of your own use yours (calendar).
- On a team server the microphone channel is labelled with the recorder's name, not
  `local_speaker_name`.
- Calendar: one CalendarService per feed, so each person's banner, invitees and auto-naming come
  from their own ICS link.
- `GET/PUT /api/users/me/prefs` for anyone signed in (400 on the desktop app, which has settings).
  UI: "My preferences" at the top of Settings → General on a team server, for everyone; the rest
  stays read-only for non-admins.

## 3. Your tasks and digest

- `TaskItem.mine`: the action item's owner is you (your full name, your first name when nobody
  else on the server shares it, or your email). Tasks gets a "Mine" filter, the default on a team
  server; tasks from meetings shared with you are there too.
- Ticking an action item done is allowed to anyone who can read the meeting, and logged with who.
- Weekly digest per person: on a team server the schedule runs for each person whose
  `digest_weekday` (theirs, else the server's) is due, as that person, so it covers their own and
  shared meetings and is saved as theirs. "Already made this week" is checked per person.
