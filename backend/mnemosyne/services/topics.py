"""Topic threads: follow one subject across meetings."""

from __future__ import annotations

import re
from datetime import datetime

import numpy as np

from ..models.base import ApiModel
from ..models.session import Chapter, SummaryData
from .tasks import TaskItem


class TopicCount(ApiModel):
    topic: str
    meetings: int
    last_seen: datetime


class ThreadMeeting(ApiModel):
    id: str
    name: str
    created_at: datetime
    summary: str  # first few sentences
    chapters: list[Chapter]
    decisions: list[str]
    action_items: list[TaskItem]
    open_questions: list[str]


class Thread(ApiModel):
    query: str
    meetings: list[ThreadMeeting]  # oldest first: a thread reads as a timeline


def _norm(t: str) -> str:
    return " ".join(re.findall(r"\w+", t.lower()))


def frequent_topics(repo, limit: int = 30) -> list[TopicCount]:
    seen: dict[str, dict] = {}
    for r in repo.people_rows():
        if not r["summary_data"]:
            continue
        data = SummaryData.model_validate_json(r["summary_data"])
        created = datetime.fromisoformat(r["created_at"])
        for t in {_norm(x): x for x in data.topics if _norm(x)}.items():
            key, label = t
            e = seen.setdefault(key, {"label": label, "ids": set(), "last": created})
            e["ids"].add(r["id"])
            e["last"] = max(e["last"], created)
    out = [
        TopicCount(topic=e["label"], meetings=len(e["ids"]), last_seen=e["last"])
        for e in seen.values()
    ]
    out.sort(key=lambda t: (t.meetings, t.last_seen), reverse=True)
    return out[:limit]


class _Matcher:
    """Does a short text (decision, item, chapter title) belong to the topic? By words, and
    by meaning when the semantic index has an embedder."""

    def __init__(self, query: str, embedder=None):
        self.words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        self.embedder = embedder
        self.q = embedder.embed([query])[0] if embedder is not None else None

    def filter(self, texts: list[str]) -> list[bool]:
        hits = [any(w in t.lower() for w in self.words) for t in texts]
        if self.embedder is not None and texts:
            sims = self.embedder.embed(texts) @ self.q
            floor = self.embedder.min_score + 0.05
            hits = [h or bool(s >= floor) for h, s in zip(hits, np.asarray(sims), strict=True)]
        return hits


def build_thread(repo, index, query: str, limit: int = 12) -> Thread:
    q = query.strip()
    rank: dict[str, float] = {}
    # Meetings whose topics name it.
    nq = _norm(q)
    for r in repo.people_rows():
        if r["summary_data"] and nq:
            data = SummaryData.model_validate_json(r["summary_data"])
            if any(nq in _norm(t) or _norm(t) in nq for t in data.topics if _norm(t)):
                rank[r["id"]] = rank.get(r["id"], 0.0) + 1.0
    # Keyword hits and meaning hits, by reciprocal rank.
    for i, h in enumerate(repo.search(q, limit=limit * 2)):
        rank[h.session_id] = rank.get(h.session_id, 0.0) + 1.0 / (60 + i)
    seen: list[str] = []
    for h in index.query(q, k=limit * 8) if index is not None else []:
        if h.session_id not in seen:
            seen.append(h.session_id)
    for i, sid in enumerate(seen):
        rank[sid] = rank.get(sid, 0.0) + 1.0 / (60 + i)
    ids = sorted(rank, key=rank.get, reverse=True)[:limit]

    matcher = _Matcher(q, index.embedder() if index is not None else None)
    meetings = []
    for sid in ids:
        s = repo.get(sid)
        if s is None:
            continue
        d = s.summary_data or SummaryData()
        keep_ch = matcher.filter([c.title for c in d.chapters])
        keep_dec = matcher.filter(d.decisions)
        keep_act = matcher.filter([a.text for a in d.action_items])
        keep_q = matcher.filter(d.open_questions)
        first = re.split(r"(?<=[.!?])\s+", s.summary.strip())
        meetings.append(
            ThreadMeeting(
                id=s.id,
                name=s.name,
                created_at=s.created_at,
                summary=" ".join(first[:3]),
                chapters=[c for c, k in zip(d.chapters, keep_ch, strict=True) if k],
                decisions=[x for x, k in zip(d.decisions, keep_dec, strict=True) if k],
                action_items=[
                    TaskItem(
                        session_id=s.id,
                        session_name=s.name,
                        created_at=s.created_at,
                        idx=i,
                        text=a.text,
                        owner=a.owner,
                        done=a.done,
                        issue_url=a.issue_url,
                    )
                    for i, (a, k) in enumerate(zip(d.action_items, keep_act, strict=True))
                    if k
                ],
                open_questions=[x for x, k in zip(d.open_questions, keep_q, strict=True) if k],
            )
        )
    meetings.sort(key=lambda m: m.created_at)
    return Thread(query=q, meetings=meetings)


THREAD_PROMPT = """\
You explain where a topic stands, from notes of the meetings that discussed it (oldest first).
Write markdown with: a one-paragraph "Where it stands" (the current state), "How it got here"
as 3 to 6 dated bullets (the turns: decisions, changes of plan), and "Still open" bullets
(open tasks with owners, unanswered questions). Use only the notes; say when the notes do not
cover something. Be concise."""


def thread_notes(thread: Thread) -> str:
    blocks = [f"Topic: {thread.query}"]
    for m in thread.meetings:
        parts = [f'## "{m.name}" ({m.created_at.strftime("%Y-%m-%d")})', m.summary]
        if m.chapters:
            parts.append("Chapters: " + "; ".join(c.title for c in m.chapters))
        parts += [f"Decision: {x}" for x in m.decisions]
        parts += [
            f"Action item{' (done)' if a.done else ''}: {a.text}"
            + (f" (owner: {a.owner})" if a.owner else "")
            for a in m.action_items
        ]
        parts += [f"Open question: {x}" for x in m.open_questions]
        blocks.append("\n".join(p for p in parts if p))
    return "\n\n".join(blocks)
