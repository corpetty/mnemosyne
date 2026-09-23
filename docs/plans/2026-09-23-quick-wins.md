# Plan: quick wins (2026-09-23)

Four small, independent-ish improvements. Do them in order; each ends with a green
`pnpm test:backend`, `pnpm lint:backend`, `pnpm check`, and a commit. Read `CLAUDE.md`
first (conventions, gotchas). Never `uv pip install`; edit `pyproject.toml` and re-lock.

Verification commands used throughout:

```bash
cd backend && uv run ruff check --fix . && uv run ruff format . && uv run pytest -q -o faulthandler_timeout=40
cd .. && pnpm check
```

Test helpers you will reuse (already in `backend/tests/conftest.py`): fixtures `client`,
`ctx`, `fake_engine`, `fake_provider`, `transcribed_session`; function
`drain_until_job(ws, job_id)` that reads WebSocket events until the job is terminal.
Gotcha: `PUT /api/settings` rebuilds `ctx.summarizer` and drops the injected
`fake_provider`; in tests set `ctx.settings.<field>` directly instead.

---

## 1. Summarization as a background job (+ auto-summarize)

Today `POST /api/sessions/{id}/summarize` runs the LLM call inline and the UI blocks (50 s on a
thinking model). Make it a Job like transcription.

### Backend

1. `backend/src/mnemosyne/services/pipeline.py`: add

   ```python
   def summarize_session(app, session_id, provider="", model="", style="", instructions=None):
       """Build the summarize job runner."""
       async def run(ctx: JobContext) -> dict:
           session = app.sessions.get_session(session_id)
           if session is None: raise ValueError(f"Session {session_id} not found")
           if not session.transcript: raise ValueError("Session has no transcript")
           st = app.settings
           prov = provider or st.default_provider
           mdl = model or st.default_model
           sty = style or st.summary_style
           instr = st.summary_instructions if instructions is None else instructions
           ctx.update(f"Summarizing with {prov}/{mdl or 'default model'}")
           ctx.emit({"type": "status", "session_id": session_id, "message": "Summarizing..."})
           try:
               result = await app.summarizer.summarize(
                   segments=[s.model_dump() for s in session.transcript],
                   provider_name=prov, model=mdl, style=sty, instructions=instr)
           except Exception as e:
               ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
               raise
           app.sessions.set_summary(session_id, result["summary"], result["data"])
           # item 2 hooks in here (auto-naming)
           ctx.update("Summary ready")
           return {"provider": result["provider"], "model": result["model"]}
       return run
   ```

   `app.sessions.set_summary` already publishes a `session` event.

2. `backend/src/mnemosyne/api/routes/models.py`: the summarize route keeps its validation
   (404 unknown session, 400 no transcript, 400 unknown style via `STYLES`) and then:
   - `409` if `ctx.jobs.list(session_id=session_id, active_only=True)` contains a
     `summarize` job;
   - `return ctx.jobs.submit("summarize", summarize_session(ctx, session_id, request.provider,
     request.model, request.style, request.instructions), session_id=session_id)` with
     `response_model=Job` (import `Job` from `...jobs`). Delete `SummarizeResponse`; grep for
     other uses (none expected outside tests).

3. `backend/src/mnemosyne/api/context.py`: `JobManager(bus, concurrency={"transcribe": 1,
   "summarize": 2})`.

4. `backend/src/mnemosyne/config.py`: add `auto_summarize: bool = False` next to
   `auto_transcribe`. In `pipeline.py::transcribe_session`, right after
   `app.sessions.set_transcript(...)`:

   ```python
   if settings.auto_summarize and segments:
       app.jobs.submit("summarize", summarize_session(app, session_id), session_id=session_id)
   ```

5. Tests. Update `tests/test_summarization.py` (`test_summarize_endpoint_saves_summary`,
   `test_summarize_uses_default_provider_from_settings`, `test_summarize_unknown_provider_is_400`)
   and `tests/test_summary_structured.py` (`test_summarize_endpoint_stores_structured`,
   `test_summary_instructions_setting_used`): the POST now returns a Job (`kind ==
   "summarize"`); open `client.websocket_connect("/ws")` *before* posting, `drain_until_job`,
   then assert on `GET /api/sessions/{id}` (`summary`, `summary_data`) and on
   `GET /api/jobs/{id}` (`status == "completed"`, `result["provider"]`). An unknown provider
   now yields a *failed* job with `error` containing "not available" (not a 400). Add:
   - `test_summarize_409_when_already_running` (post twice inside the WS block before
     draining; second is 409 or, if the first finished already, accept 200);
   - `test_auto_summarize_chains_after_transcribe`: `ctx.settings.auto_summarize = True`,
     `fake_provider`, `fake_engine`; create session, `ctx.sessions.set_audio(sid, "/fake.ogg",
     [])`, post `/transcribe`, drain that job, then read events until a `job` event with
     `kind == "summarize"` and `status == "completed"`; assert the session has a summary.

6. Docs: `docs/api-reference.md` summarize section (returns `Job`; `409`; results arrive over
   the WebSocket; `auto_summarize`), `docs/architecture.md` Jobs paragraph (mention
   `summarize` kind and concurrency 2).

### Frontend

7. New `src/lib/stores/jobs.svelte.ts`:

   ```ts
   import type { BackendEvent, Job } from '$lib/types/index.js';
   import { wsState } from './websocket.svelte.js';
   class JobsState {
     jobs = $state<Record<string, Job>>({});
     private unsubscribe: (() => void) | null = null;
     private completeHandlers: ((job: Job) => void)[] = [];
     init() { this.unsubscribe = wsState.onMessage((raw) => this.handle(raw as BackendEvent)); }
     destroy() { this.unsubscribe?.(); this.unsubscribe = null; }
     onComplete(cb: (job: Job) => void) { this.completeHandlers.push(cb); return () => { /* remove */ }; }
     private handle(msg: BackendEvent) {
       if (msg.type === 'hello') { for (const j of msg.jobs) this.jobs[j.id] = j; }
       else if (msg.type === 'job') {
         const prev = this.jobs[msg.job.id];
         this.jobs[msg.job.id] = msg.job;
         if (msg.job.status === 'completed' && prev?.status !== 'completed') this.completeHandlers.forEach((h) => h(msg.job));
       }
     }
     active(sessionId: string, kind?: string): Job | null { /* first job with session_id, (kind), status queued|running */ }
     last(sessionId: string, kind: string): Job | null { /* most recent by created_at */ }
   }
   export const jobsState = new JobsState();
   ```

8. `src/routes/+page.svelte`: in `onConnected()` call `jobsState.init()` after
   `transcriptState.init()`, and `jobsState.onComplete((job) => { if (job.kind === 'summarize') {
   if (sessionState.activeSession?.id === job.session_id) sessionState.refreshActive();
   sessionState.loadSessions(); toastState.success('Summary ready'); } })`. Call
   `jobsState.destroy()` in the cleanup.

9. `src/lib/api/backend.ts`: `summarizeSession(...)` returns `Promise<Job>`.

10. `src/lib/components/SummaryView.svelte`: replace the local `loading` flag with
    `const job = $derived(sessionState.activeSession ? jobsState.active(sessionState.activeSession.id, 'summarize') : null)`;
    button disabled while `job` exists, label `job ? (job.message || 'Summarizing...') : ...`;
    show `jobsState.last(id,'summarize')?.error` in red when the last job failed. Remove the
    `await sessionState.refreshActive()` after posting (the completion handler does it).

11. Settings: add `auto_summarize` to `SettingsValues` in `src/lib/types/index.ts` and a
    checkbox "Summarize automatically after transcription" in `SettingsPanel.svelte`
    ("Summaries and export" section; also add it to the `form = {...}` initialiser).

Commit: `Summarization runs as a background job; auto_summarize setting`.

---

## 2. Auto-name untitled sessions

Zero extra LLM calls: the summary JSON gains a `title`.

1. `backend/src/mnemosyne/models/session.py`: add `DEFAULT_SESSION_NAME = "Untitled Session"`
   and use it as the `Session.name` default; add `title: str = ""` to `SummaryData`.
   Use the constant in `services/session_service.py::create_session` and
   `api/routes/sessions.py::CreateSessionRequest`.
2. `backend/src/mnemosyne/summarization/prompts.py`: in `FORMAT_INSTRUCTIONS` add, as the first
   key, `"title": "<3 to 7 word title for this conversation, no quotes>"`; in
   `parse_summary_response` set `data.title = str(obj.get("title", "")).strip()[:80]`.
3. `backend/src/mnemosyne/config.py`: `auto_name_sessions: bool = True`.
4. In `pipeline.py::summarize_session` after `set_summary`: if
   `st.auto_name_sessions and result["data"].title and session.name == DEFAULT_SESSION_NAME`:
   `app.sessions.rename_session(session_id, result["data"].title)` (publishes a `session`
   event, so the sidebar updates). Include `"title"` in the job result.
5. Tests (`tests/test_summary_structured.py`): title parsed and truncated; summarize job
   renames an "Untitled Session" but leaves a custom name alone; with
   `ctx.settings.auto_name_sessions = False` no rename. Make `fake_provider.summary` a JSON
   string with a `"title"`.
6. Frontend: `SummaryData.title: string` in types; checkbox "Name untitled sessions from the
   summary" in Settings (same section as above). `docs/api-reference.md`: mention `title`.

Commit: `Auto-name untitled sessions from the summary title`.

---

## 3. Rendered markdown for summaries and notes

1. `pnpm add marked dompurify` (DOMPurify 3 ships its own types; if `pnpm check` complains,
   `pnpm add -D @types/dompurify`). Both are plain JS, no build scripts to allow.
2. New `src/lib/components/Markdown.svelte`:

   ```svelte
   <script lang="ts">
     import { marked } from 'marked';
     import DOMPurify from 'dompurify';
     let { text = '' }: { text?: string } = $props();
     marked.setOptions({ gfm: true, breaks: true });
     DOMPurify.addHook('afterSanitizeAttributes', (node) => {
       if (node.tagName === 'A') { node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noopener noreferrer'); }
     });
     const html = $derived(DOMPurify.sanitize(marked.parse(text, { async: false }) as string));
   </script>
   <div class="md">{@html html}</div>
   <style> /* dark-theme styles for .md h1,h2,h3,p,ul,ol,li,code,pre,blockquote,a,strong,table; keep compact (text-sm, mb-2) */ </style>
   ```

   Register the DOMPurify hook once at module level (outside the component instance) so it
   is not added on every mount.
3. `SummaryView.svelte`: replace the `<pre ...>{summary}</pre>` block with
   `<Markdown text={sessionState.activeSession.summary} />` inside the same bordered box.
   Keep the Copy button copying raw markdown.
4. `NotesEditor.svelte`: add `let mode = $state<'edit' | 'preview'>('edit')`, a small
   Edit/Preview toggle above the textarea, `<Markdown text={localNotes} />` in preview mode,
   textarea height `h-64`, and a hint "Markdown supported · saves automatically".
5. `pnpm check` must be 0 errors; `pnpm build` must pass. Then run
   `pnpm tauri dev` (or `cd backend && uv run uvicorn main:app --port 8008` plus `pnpm dev`)
   and eyeball: a summary with headings/bullets renders; notes preview renders; a link opens
   in the system browser (Tauri) or a new tab.

Commit: `Render summaries and notes as markdown`.

---

## 4. Release v0.2.1

1. Bump the version to `0.2.1` in: `src-tauri/tauri.conf.json` (`version`),
   `src-tauri/Cargo.toml` (`version`), `package.json` (`version`),
   `backend/pyproject.toml` (`version`), and the `FastAPI(... version="0.2.0")` string in
   `backend/src/mnemosyne/api/app.py`. Run `cd src-tauri && cargo check` so `Cargo.lock`
   picks up the new version, and `cd backend && uv lock` (the lockfile records the project
   version). Commit: `Release 0.2.1`.
2. Tag and push: `git tag v0.2.1 && git push origin main v0.2.1`.
3. Watch the Release workflow. `gh` is not authenticated on this machine; use the public API:

   ```bash
   curl -s "https://api.github.com/repos/corpetty/mnemosyne/actions/runs?event=push&per_page=5" | python3 -c "import json,sys; [print(r['name'], r['head_branch'], r['status'], r['conclusion'], r['html_url']) for r in json.load(sys.stdin)['workflow_runs']]"
   ```

   It builds on `ubuntu-22.04` with `NO_STRIP=true`; expect 10 to 15 minutes. On failure,
   read the log in the browser (job logs need auth via the API) and fix `.github/workflows/release.yml`.
4. Confirm the release page lists `Mnemosyne_0.2.1_amd64.deb`, `Mnemosyne_0.2.1_amd64.AppImage`
   and `SHA256SUMS`. Optionally download the AppImage and launch it with a throwaway `HOME`
   on real disk (see CLAUDE.md gotcha about `/tmp`) to confirm it starts and installs.

---

## Done criteria

- `pnpm test:backend` green (expect ~130 tests), `pnpm lint:backend` clean, `pnpm check` 0 errors.
- Four commits on `main` as above, plus the `v0.2.1` tag with attached bundles.
- `docs/api-reference.md` and `CLAUDE.md` roadmap paragraph updated to mention: summarize
  job + auto_summarize, auto-naming, markdown rendering, 0.2.1 released.
