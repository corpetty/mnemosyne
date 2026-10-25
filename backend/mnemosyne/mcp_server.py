"""MCP server exposing your meetings to Claude (Code, Desktop) and other MCP clients.

It is a thin client of a running Mnemosyne backend, so it works for the local app or a
remote backend in server mode:

    MNEMOSYNE_URL=http://127.0.0.1:8008  MNEMOSYNE_TOKEN=<api_token, if set>  mnemosyne-mcp

Tools return plain text formatted for a model to read, with session ids so follow-up
calls (get_meeting) can drill in.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import httpx

from .services.notes_chips import plain_notes

INSTRUCTIONS = """\
Mnemosyne holds the user's recorded meetings: transcripts with speaker names, summaries,
decisions, action items and notes. Use `ask_meetings` for questions that need an answer
synthesized across meetings (it cites sources), `search_meetings` to find where something
was said, `list_meetings` to browse by date, `get_meeting` to read one meeting in full, and
`get_action_items` for open tasks. Transcripts are machine-generated and may contain errors.
"""


class Backend:
    def __init__(self, url: str | None = None, token: str | None = None, transport=None):
        self.url = (url or os.environ.get("MNEMOSYNE_URL") or "http://127.0.0.1:8008").rstrip("/")
        self.token = token if token is not None else os.environ.get("MNEMOSYNE_TOKEN", "")
        self._transport = transport

    def client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.AsyncClient(
            base_url=self.url, headers=headers, timeout=30, transport=self._transport
        )

    async def get(self, path: str, **params):
        async with self.client() as c:
            r = await c.get(path, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json()

    async def post(self, path: str, body: dict):
        async with self.client() as c:
            r = await c.post(path, json=body)
            r.raise_for_status()
            return r.json()


def _date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")


def _mmss(seconds: float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _plain(snippet: str) -> str:
    return snippet.replace("[[", "**").replace("]]", "**")


# ---- tool implementations (plain async functions, testable without MCP) ---------------

# Meetings marked local-only in the app are never handed to an assistant: the MCP client is
# usually a cloud LLM, which is exactly what local-only rules out.
LOCAL_ONLY_NOTE = (
    "This meeting is marked local-only in Mnemosyne and is not shared with assistants."
)


async def _local_only_ids(backend: Backend) -> set[str]:
    return {s["id"] for s in await backend.get("/api/sessions") if s.get("local_only")}


async def list_meetings(backend: Backend, limit: int = 20, days: int | None = None) -> str:
    sessions = [s for s in await backend.get("/api/sessions") if not s.get("local_only")]
    if days:
        cutoff = datetime.now() - timedelta(days=days)
        sessions = [s for s in sessions if datetime.fromisoformat(s["created_at"]) >= cutoff]
    sessions = sessions[: max(1, min(limit, 200))]
    if not sessions:
        return "No meetings found."
    lines = [
        f"- {s['name']} ({_date(s['created_at'])}) id={s['id']}"
        f"{' · summary' if s['has_summary'] else ''}"
        f"{' · transcript' if s['has_transcript'] else ''}"
        f" · {s['participant_count']} speakers"
        for s in sessions
    ]
    return "\n".join(lines)


async def search_meetings(backend: Backend, query: str, limit: int = 10) -> str:
    hits = await backend.get("/api/search", q=query, limit=max(1, min(limit, 50)))
    hidden = await _local_only_ids(backend)
    hits = [h for h in hits if h["session_id"] not in hidden]
    if not hits:
        return f"No matches for {query!r}."
    out = []
    for h in hits:
        out.append(f"## {h['session_name']} ({_date(h['created_at'])}) id={h['session_id']}")
        if h.get("session_snippet"):
            out.append(f"  (summary/notes) {_plain(h['session_snippet'])}")
        for seg in h["segments"]:
            out.append(f"  [{_mmss(seg['start'])}] {seg['speaker']}: {_plain(seg['snippet'])}")
    return "\n".join(out)


async def ask_meetings(backend: Backend, question: str, timeout: float = 240) -> str:
    job = await backend.post("/api/ask", {"question": question, "exclude_local_only": True})
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = await backend.get(f"/api/jobs/{job['id']}")
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        if asyncio.get_running_loop().time() > deadline:
            return "The answer is taking too long; try again or use search_meetings."
        await asyncio.sleep(1.0)
    if job["status"] != "completed":
        return f"Could not answer: {job.get('error') or job['status']}"
    ask = job["result"]
    out = [ask["answer"]]
    if ask["citations"]:
        out.append("\nSources:")
        for c in ask["citations"]:
            when = _date(c["created_at"])
            at = f" at {_mmss(c['start'])}" if c.get("start") is not None else " (summary)"
            out.append(f"[{c['n']}] {c['session_name']} ({when}){at} id={c['session_id']}")
    return "\n".join(out)


async def get_meeting(
    backend: Backend,
    session_id: str,
    include_transcript: bool = True,
    offset: int = 0,
    max_lines: int = 400,
) -> str:
    s = await backend.get(f"/api/sessions/{session_id}")
    if s.get("local_only"):
        return LOCAL_ONLY_NOTE
    out = [f"# {s['name']}", f"Date: {_date(s['created_at'])} · status: {s['status']}"]
    if s.get("attendees"):
        out.append("Invited: " + ", ".join(s["attendees"]))
    if s.get("participants"):
        out.append("Speakers: " + ", ".join(s["participants"]))
    if s.get("transcript"):
        st = await backend.get(f"/api/sessions/{session_id}/stats")
        talk = ", ".join(f"{x['speaker']} {round(100 * x['share'])}%" for x in st["speakers"])
        out.append(f"Length: {_mmss(st['duration_seconds'])} · talk time: {talk}")
    if s.get("summary"):
        stale = " (out of date: transcript edited since)" if s.get("summary_stale") else ""
        out += ["", f"## Summary{stale}", s["summary"]]
    d = s.get("summary_data") or {}
    if d.get("decisions"):
        out += ["", "## Decisions"] + [f"- {x}" for x in d["decisions"]]
    if d.get("action_items"):
        out += ["", "## Action items"] + [
            ("- [x] " if a.get("done") else "- [ ] ")
            + a["text"]
            + (f" ({a['owner']})" if a.get("owner") else "")
            for a in d["action_items"]
        ]
    if d.get("open_questions"):
        out += ["", "## Open questions"] + [f"- {x}" for x in d["open_questions"]]
    if d.get("client_facts"):
        out += ["", "## Client facts"] + [
            f"- {f['kind'].replace('_', ' ')}: {f['text']}" for f in d["client_facts"]
        ]
    if d.get("chapters"):
        out += ["", "## Chapters"] + [
            f"- [{_mmss(c['start'])}] {c['title']}" for c in d["chapters"]
        ]
    if s.get("notes"):
        out += ["", "## Notes", plain_notes(s["notes"])]
    if include_transcript and s.get("transcript"):
        lines = s["transcript"]
        page = lines[max(0, offset) : max(0, offset) + max(1, min(max_lines, 2000))]
        out += ["", f"## Transcript (lines {offset}-{offset + len(page) - 1} of {len(lines)})"]
        out += [f"[{_mmss(x['start'])}] {x['speaker']}: {x['text']}" for x in page]
        if offset + len(page) < len(lines):
            out.append(f"... more: call again with offset={offset + len(page)}")
    open_threads = await _open_comments(backend, session_id)
    if open_threads:
        out += ["", "## Open comments"] + open_threads
    return "\n".join(out)


async def _open_comments(backend: Backend, session_id: str) -> list[str]:
    """Open threads (root neither resolved nor deleted) as lines, a reply indented under its
    root. Nothing when the comments cannot be read: the meeting is still worth reading."""
    try:
        data = await backend.get(f"/api/sessions/{session_id}/comments")
    except Exception:
        return []
    lines: list[str] = []
    for thread in data.get("threads") or []:
        root = thread["root"]
        if root.get("resolved_at") or root.get("deleted"):
            continue
        quote = (root.get("quote") or "").strip()
        if len(quote) > 60:
            quote = quote[:59] + "…"
        label = "Notes: " if root.get("target") == "notes" else ""
        on = f" (on {label}“{quote}”)" if quote else (" (on the notes)" if label else "")
        lines.append(f"- {root.get('author') or 'unknown'}: {root['text']}{on}")
        for reply in thread.get("replies") or []:
            if not reply.get("deleted"):
                lines.append(f"  - {reply.get('author') or 'unknown'}: {reply['text']}")
    return lines


async def get_action_items(backend: Backend, days: int = 14) -> str:
    cutoff = datetime.now() - timedelta(days=days)
    sessions = [
        s
        for s in await backend.get("/api/sessions")
        if s["has_summary"]
        and not s.get("local_only")
        and datetime.fromisoformat(s["created_at"]) >= cutoff
    ]
    details = [(s, await backend.get(f"/api/sessions/{s['id']}")) for s in sessions]
    details.sort(key=lambda sd: sd[0]["created_at"], reverse=True)  # newest first
    # A task linked across meetings is listed once, under its newest meeting, as that says.
    seen_in: dict[str, int] = {}
    for _, detail in details:
        for a in (detail.get("summary_data") or {}).get("action_items") or []:
            if a.get("task_id"):
                seen_in[a["task_id"]] = seen_in.get(a["task_id"], 0) + 1
    listed: set[str] = set()
    out = []
    for s, detail in details:
        lines = []
        for a in (detail.get("summary_data") or {}).get("action_items") or []:
            tid = a.get("task_id")
            if tid:
                if tid in listed:
                    continue
                listed.add(tid)
            more = (
                f" (also in {seen_in[tid] - 1} earlier meeting{'s' if seen_in[tid] > 2 else ''})"
                if tid and seen_in[tid] > 1
                else ""
            )
            lines.append(
                ("- [x] " if a.get("done") else "- [ ] ")
                + a["text"]
                + (f" ({a['owner']})" if a.get("owner") else "")
                + more
            )
        if lines:
            out.append(f"## {s['name']} ({_date(s['created_at'])}) id={s['id']}")
            out += lines
    return "\n".join(out) if out else f"No action items in the last {days} days."


# ---- MCP wiring ---------------------------------------------------------------------


def build_server(backend: Backend | None = None):
    from mcp.server.mcpserver import MCPServer

    be = backend or Backend()
    mcp = MCPServer("mnemosyne", title="Mnemosyne meetings", instructions=INSTRUCTIONS)

    @mcp.tool(
        name="list_meetings",
        description="List recent meetings (newest first) with ids. `days` limits how far back.",
    )
    async def list_meetings_tool(limit: int = 20, days: int | None = None) -> str:
        return await list_meetings(be, limit, days)

    @mcp.tool(
        name="search_meetings",
        description="Full-text search across transcripts, summaries and notes.",
    )
    async def search_meetings_tool(query: str, limit: int = 10) -> str:
        return await search_meetings(be, query, limit)

    @mcp.tool(
        name="ask_meetings",
        description="Answer a question from the user's meetings, with numbered sources. "
        "Takes up to a few minutes on a local LLM.",
    )
    async def ask_meetings_tool(question: str) -> str:
        return await ask_meetings(be, question)

    @mcp.tool(
        name="get_meeting",
        description=(
            "Read one meeting: summary, decisions, action items, notes and (paged) transcript."
        ),
    )
    async def get_meeting_tool(
        session_id: str, include_transcript: bool = True, offset: int = 0, max_lines: int = 400
    ) -> str:
        return await get_meeting(be, session_id, include_transcript, offset, max_lines)

    @mcp.tool(
        name="get_action_items",
        description="Action items from summarized meetings in the last `days` days.",
    )
    async def get_action_items_tool(days: int = 14) -> str:
        return await get_action_items(be, days)

    return mcp


def main() -> None:
    import logging

    # stdout carries the MCP protocol; keep stderr free of per-request noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
