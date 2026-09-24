"""Fuse keyword (FTS5/bm25) and semantic (vector) results with reciprocal rank fusion."""

from __future__ import annotations

from ..models.ask import Passage
from ..models.search import SearchHit, SegmentHit
from .index import VectorIndex

RRF_K = 60


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


def hybrid_passages(repo, index: VectorIndex | None, question: str, limit: int = 20):
    """Passages for Ask: bm25 windows plus semantically close windows and summaries.
    A semantic window overlapping a keyword window of the same session boosts it instead
    of adding a duplicate."""
    keyword = repo.retrieve(question, limit=limit)
    hits = index.query(question, k=limit * 2) if index is not None else []
    fused: list[list] = [[p, _rrf(r)] for r, p in enumerate(keyword)]
    for r, h in enumerate(hits):
        score = _rrf(r)
        same = next(
            (
                e
                for e in fused
                if e[0].session_id == h.session_id
                and (
                    (h.kind == "summary" and e[0].kind == "summary")
                    or (
                        h.kind == "lines"
                        and e[0].kind == "transcript"
                        and e[0].lines
                        and h.first <= e[0].lines[-1].idx
                        and h.last >= e[0].lines[0].idx
                    )
                )
            ),
            None,
        )
        if same is not None:
            same[1] += score
            continue
        if h.kind == "summary":
            p = repo.summary_passage(h.session_id, h.score)
        else:
            p = repo.passage_window(h.session_id, h.first - 1, h.last + 1, h.first, h.score)
        if p is not None:
            fused.append([p, score])
    fused.sort(key=lambda e: e[1], reverse=True)
    out: list[Passage] = []
    for p, score in fused[:limit]:
        p.score = score
        out.append(p)
    return out


def hybrid_search(repo, index: VectorIndex | None, query: str, limit: int = 20, per_session=5):
    """Sidebar search: keyword hits, plus meetings that match by meaning."""
    keyword = repo.search(query, limit=limit, per_session=per_session)
    hits = index.query(query, k=limit * 4, strict=True) if index is not None else []
    if not hits:
        return keyword
    by_id = {h.session_id: h for h in keyword}
    scores = {h.session_id: _rrf(r) for r, h in enumerate(keyword)}
    sem_rank: dict[str, int] = {}
    for h in hits:
        sem_rank.setdefault(h.session_id, len(sem_rank))
    summaries = {s.id: s for s in repo.list_summaries()}
    for sid, rank in sem_rank.items():
        if sid not in summaries:
            continue
        scores[sid] = scores.get(sid, 0.0) + _rrf(rank)
        hit = by_id.get(sid)
        if hit is None:
            sm = summaries[sid]
            hit = by_id[sid] = SearchHit(
                session_id=sid,
                session_name=sm.name,
                created_at=sm.created_at,
                score=0.0,
                match="semantic",
            )
        elif hit.match == "keyword":
            hit.match = "both"
        seen = {s.idx for s in hit.segments}
        for h in (x for x in hits if x.session_id == sid):
            if len(hit.segments) >= per_session:
                break
            if h.kind == "summary":
                if not hit.session_snippet:
                    p = repo.summary_passage(sid, h.score)
                    if p is not None:
                        hit.session_snippet = p.text[:160] + ("…" if len(p.text) > 160 else "")
                continue
            if any(h.first <= i <= h.last for i in seen):
                continue
            p = repo.passage_window(sid, h.first, h.last, h.first, h.score)
            if p is None:
                continue
            line = p.lines[0]
            text = " ".join(ln.text for ln in p.lines)
            hit.segments.append(
                SegmentHit(
                    idx=line.idx,
                    speaker=line.speaker,
                    start=line.start,
                    snippet=text[:140] + ("…" if len(text) > 140 else ""),
                    semantic=True,
                )
            )
            seen.add(line.idx)
    results = list(by_id.values())
    for h in results:
        h.score = scores.get(h.session_id, 0.0)
    results.sort(key=lambda h: h.score, reverse=True)
    return results[:limit]
