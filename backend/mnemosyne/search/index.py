"""Vector index over transcript windows and summaries.

Each session is cut into windows of a few consecutive lines (and one chunk for its summary,
decisions, items and chapter titles). Vectors live in SQLite (`chunk_vectors`, float16) and in
an in-memory matrix for queries. An indexer task follows `session` events on the EventBus, so
transcripts, edits and summaries are (re)indexed a few seconds after they change; unchanged
chunks are detected by hash and not re-embedded.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from dataclasses import dataclass

import numpy as np

from ..models.base import ApiModel
from ..models.session import Session

logger = logging.getLogger(__name__)

WINDOW_CHARS = 600
WINDOW_LINES = 8


@dataclass
class Chunk:
    kind: str  # lines | summary
    first: int
    last: int
    text: str

    @property
    def hash(self) -> str:
        return hashlib.sha1(self.text.encode()).hexdigest()[:16]


@dataclass
class VecHit:
    session_id: str
    kind: str
    first: int
    last: int
    score: float


class IndexStatus(ApiModel):
    enabled: bool
    model: str
    ready: bool  # the embedder is loaded
    indexed_sessions: int
    total_sessions: int
    pending: int
    error: str | None


def chunk_session(session: Session) -> list[Chunk]:
    chunks: list[Chunk] = []
    start, buf = 0, []
    for i, seg in enumerate(session.transcript):
        buf.append(seg.text.strip())
        if sum(len(x) for x in buf) >= WINDOW_CHARS or len(buf) >= WINDOW_LINES:
            chunks.append(Chunk("lines", start, i, " ".join(buf)))
            start, buf = i + 1, []
    if buf:
        chunks.append(Chunk("lines", start, len(session.transcript) - 1, " ".join(buf)))
    parts = [session.name, session.summary.strip()]
    d = session.summary_data
    if d is not None:
        parts += d.topics + d.decisions + [a.text for a in d.action_items]
        parts += d.open_questions + [c.title for c in d.chapters]
    text = "\n".join(p for p in parts if p)
    if session.summary.strip():
        chunks.append(Chunk("summary", -1, -1, text))
    return [c for c in chunks if c.text.strip()]


class VectorIndex:
    def __init__(self, repo, settings, embedder_factory=None):
        self.repo = repo
        self.enabled = settings.semantic_search
        self.model = settings.embedding_model
        self._settings = settings
        self._factory = embedder_factory
        self._embedder = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._matrix: np.ndarray | None = None
        self._keys: list[tuple[str, str, int, int]] = []
        self._dirty = True
        self._pending: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- embedder --------------------------------------------------------

    def embedder(self):
        """The loaded embedder, loading it on first use; None when disabled or broken."""
        if not self.enabled or self.error:
            return None
        with self._lock:
            if self._embedder is None:
                from . import embeddings

                try:
                    factory = self._factory or embeddings.build_embedder
                    self._embedder = factory(self._settings)
                except Exception as e:  # no network for the first download, bad model name
                    self.error = f"Could not load {self.model}: {e}"
                    logger.warning(self.error)
                    return None
                if self.repo.get_meta("embedder") != self._embedder.name:
                    self.repo.clear_vectors()
                    self.repo.set_meta("embedder", self._embedder.name)
                    self._dirty = True
            return self._embedder

    # ---- indexing --------------------------------------------------------

    def index_session(self, session_id: str) -> int:
        """(Re)index one session. Returns how many chunks were embedded."""
        emb = self.embedder()
        if emb is None:
            return 0
        session = self.repo.get(session_id)
        if session is None:
            return 0
        chunks = chunk_session(session)
        known = self.repo.vector_hashes(session_id)
        if {(c.kind, c.first): c.hash for c in chunks} == known:
            return 0
        vecs = emb.embed([c.text for c in chunks]) if chunks else np.zeros((0, 1))
        self.repo.replace_vectors(
            session_id,
            [
                (c.kind, c.first, c.last, c.hash, np.asarray(v, dtype=np.float16).tobytes())
                for c, v in zip(chunks, vecs, strict=True)
            ],
        )
        self._dirty = True
        return len(chunks)

    def _load(self) -> None:
        rows = self.repo.all_vectors()
        self._keys = [(r["session_id"], r["kind"], r["first_idx"], r["last_idx"]) for r in rows]
        self._matrix = (
            np.vstack([np.frombuffer(r["vec"], dtype=np.float16) for r in rows]).astype(np.float32)
            if rows
            else None
        )
        self._dirty = False

    # ---- queries ---------------------------------------------------------

    def query(self, text: str, k: int = 40, strict: bool = False) -> list[VecHit]:
        """Nearest chunks above the embedder's noise floor (`strict` raises it, for the
        sidebar search where a loose match is more confusing than useful)."""
        emb = self.embedder()
        if emb is None or not text.strip():
            return []
        min_score = emb.min_score + (0.05 if strict else 0.0)
        if self._dirty:
            self._load()
        if self._matrix is None:
            return []
        q = emb.embed([text])[0]
        scores = self._matrix @ q
        order = np.argsort(-scores)[:k]
        return [
            VecHit(*self._keys[i], score=float(scores[i])) for i in order if scores[i] >= min_score
        ]

    def status(self) -> IndexStatus:
        return IndexStatus(
            enabled=self.enabled,
            model=self.model,
            ready=self._embedder is not None,
            indexed_sessions=self.repo.indexed_session_count(),
            total_sessions=len(self.repo.session_ids()),
            pending=len(self._pending),
            error=self.error,
        )

    # ---- background indexer ----------------------------------------------

    def schedule(self, session_id: str) -> None:
        self._pending.add(session_id)
        self._wake.set()

    def schedule_all(self) -> None:
        for sid in self.repo.session_ids():
            self._pending.add(sid)
        self._wake.set()

    async def _follow(self, bus) -> None:
        q = bus.subscribe()
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "session" and ev.get("session_id"):
                    if ev.get("status") != "deleted":
                        self.schedule(ev["session_id"])
                    else:
                        self._dirty = True
        finally:
            bus.unsubscribe(q)

    async def _work(self, debounce: float) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await asyncio.sleep(debounce)  # let a burst of edits settle
            while self._pending:
                sid = self._pending.pop()
                try:
                    await asyncio.to_thread(self.index_session, sid)
                except Exception:
                    logger.exception("Indexing session %s failed", sid)

    def start(self, bus, debounce: float = 3.0) -> None:
        if not self.enabled:
            return
        self.schedule_all()  # backfill anything missing or changed while we were off
        loop = asyncio.get_running_loop()
        self._task = asyncio.gather(
            loop.create_task(self._follow(bus)), loop.create_task(self._work(debounce))
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def rebuild(self) -> None:
        self.error = None
        await asyncio.to_thread(self.repo.clear_vectors)
        self._dirty = True
        self.schedule_all()
