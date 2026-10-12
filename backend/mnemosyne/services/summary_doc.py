"""A meeting's summary as a mergeable document, so a new summary (re-summarize, revise) keeps
the hand edits made to the last one (docs/plans/2026-10-12-hashseq-collaboration.md, "Phase 2
design").

The summary body is one block; every decision, open question and action item is a block keyed
by its stable id (services/summary_ids.py). Each block has a merge history kept by the
`mnemosyne-link notes merge` sidecar (export/note_merge.py) with two branches: `llm`, what the
LLM last produced (the sidecar's app branch; its text is cached here), and the combined text,
which is what summary_data says. Which blocks exist, their order and the item fields are rules on
ids, in Python; the sidecar only merges texts.

The doc (`summary_docs`, JSON) also keeps `llm`, the LLM's last output as the base of the
three-way rules (order per section, item fields, topics, chapters, client facts), `tombstones`
(ids the human deleted, with their last LLM text so a reworded copy is still recognized) and
`vault` (what the last Obsidian export rendered; carried along here, filled by the export).

summary_data stays the projection everything reads. `apply_llm` / `apply_human` return the new
summary data and body; the caller writes them, this module stores the doc. Without a working
sidecar both raise `SummaryDocUnavailable` (the doc is forgotten first) and the caller falls back
to replacing the summary (`tasks.carry_over`).
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ..models.session import ActionItem, Chapter, ClientFact, SummaryData
from .summary_ids import ensure_ids, new_id
from .tasks import _norm, same_item
from .topics import _norm as _topic_key
from .topics import apply_aliases

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

VERSION = 1
SECTIONS = ("decisions", "open_questions", "action_items")
_AT = {"decisions": "decision_at", "open_questions": "question_at"}
_IDS = {"decisions": "decision_ids", "open_questions": "question_ids"}
SCALARS = ("owner", "due", "at", "live")  # three-way against the LLM's last output
BOTH_TEXTS = 5

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_warned: set[str] = set()


class SummaryDocUnavailable(Exception):
    """No sidecar, or it failed: replace the summary as before (the doc is forgotten)."""


# ---- the document --------------------------------------------------------------------------


@dataclass
class Block:
    history: bytes
    llm: str | None  # the LLM branch's text; None: added by hand, the branch is empty

    def to_json(self) -> dict:
        return {"history": base64.b64encode(self.history).decode("ascii"), "llm": self.llm}

    @classmethod
    def from_json(cls, d: dict) -> Block:
        return cls(history=base64.b64decode(d["history"]), llm=d.get("llm"))


@dataclass
class LlmBase:
    """The LLM's last output, the base of the three-way rules."""

    order: dict[str, list[str]] = field(default_factory=lambda: {s: [] for s in SECTIONS})
    action_items: dict[str, dict] = field(default_factory=dict)  # id -> owner/due/at/live
    decision_at: dict[str, float | None] = field(default_factory=dict)
    question_at: dict[str, float | None] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    client_facts: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "order": {s: list(self.order.get(s, [])) for s in SECTIONS},
            "action_items": self.action_items,
            "decision_at": self.decision_at,
            "question_at": self.question_at,
            "topics": self.topics,
            "chapters": self.chapters,
            "client_facts": self.client_facts,
        }

    @classmethod
    def from_json(cls, d: dict) -> LlmBase:
        order = d.get("order") or {}
        return cls(
            order={s: list(order.get(s, [])) for s in SECTIONS},
            action_items=dict(d.get("action_items") or {}),
            decision_at=dict(d.get("decision_at") or {}),
            question_at=dict(d.get("question_at") or {}),
            topics=list(d.get("topics") or []),
            chapters=list(d.get("chapters") or []),
            client_facts=list(d.get("client_facts") or []),
        )

    @classmethod
    def of(cls, data: SummaryData) -> LlmBase:
        """The base for `data` (ids already set)."""
        return cls(
            order={s: [i for i, _ in section(data, s)] for s in SECTIONS},
            action_items={a.id: scalars(a) for a in data.action_items},
            decision_at=dict(zip(data.decision_ids, _ats(data, "decisions"), strict=True)),
            question_at=dict(zip(data.question_ids, _ats(data, "open_questions"), strict=True)),
            topics=list(data.topics),
            chapters=[c.model_dump(mode="json") for c in data.chapters],
            client_facts=[f.model_dump(mode="json") for f in data.client_facts],
        )

    def at(self, sec: str) -> dict[str, float | None]:
        return self.decision_at if sec == "decisions" else self.question_at


def _tomb(sec: str, text: str | None) -> dict:
    return {"section": sec, "text": text or ""}


def _tomb_text(doc: Doc, item_id: str) -> str | None:
    t = doc.tombstones.get(item_id)
    return t["text"] if t is not None else None


def _empty_vault() -> dict:
    return {"written": {"body": None, "blocks": {}}, "annex": {}}


@dataclass
class Doc:
    body: Block
    blocks: dict[str, Block] = field(default_factory=dict)
    llm: LlmBase = field(default_factory=LlmBase)
    # id -> {"section", "text": its last known text}
    tombstones: dict[str, dict] = field(default_factory=dict)
    vault: dict = field(default_factory=_empty_vault)

    def to_json(self) -> dict:
        return {
            "version": VERSION,
            "body": self.body.to_json(),
            "blocks": {k: b.to_json() for k, b in self.blocks.items()},
            "llm": self.llm.to_json(),
            "tombstones": self.tombstones,
            "vault": self.vault,
        }

    @classmethod
    def from_json(cls, d: dict) -> Doc:
        if d.get("version") != VERSION:
            raise ValueError(f"unsupported summary doc version {d.get('version')!r}")
        return cls(
            body=Block.from_json(d["body"]),
            blocks={k: Block.from_json(b) for k, b in (d.get("blocks") or {}).items()},
            llm=LlmBase.from_json(d.get("llm") or {}),
            tombstones=dict(d.get("tombstones") or {}),
            vault=d.get("vault") or _empty_vault(),
        )


# ---- pure helpers (no sidecar) -------------------------------------------------------------


def section(data: SummaryData, sec: str) -> list[tuple[str, str]]:
    """(id, text) of a section's entries, in order."""
    if sec == "action_items":
        return [(a.id, a.text) for a in data.action_items]
    texts = data.decisions if sec == "decisions" else data.open_questions
    ids = getattr(data, _IDS[sec])
    return [(ids[i] if i < len(ids) else "", t) for i, t in enumerate(texts)]


def _ats(data: SummaryData, sec: str) -> list[float | None]:
    texts = data.decisions if sec == "decisions" else data.open_questions
    at = getattr(data, _AT[sec])
    return [at[i] if i < len(at) else None for i in range(len(texts))]


def _iso(d: date | str | None) -> str | None:
    return d.isoformat() if isinstance(d, date) else d


def scalars(item: ActionItem) -> dict:
    """An action item's three-way fields, as JSON values."""
    return {"owner": item.owner, "due": _iso(item.due), "at": item.at, "live": item.live}


def three_way(base: Any, current: Any, new: Any) -> Any:
    """The new value when the current one is still the base's, else the current one."""
    return new if current == base else current


def match_ids(
    new_texts: list[str], pools: list[list[tuple[str, str]]]
) -> list[tuple[str | None, int | None]]:
    """For each new text, (id, pool index) of the entry it is: pools in order, each by exact
    (normalized) text first and then by `same_item`; an entry matches once. (None, None) for a
    text nothing matched."""
    out: list[tuple[str | None, int | None]] = [(None, None)] * len(new_texts)
    used: set[str] = set()
    for p, pool in enumerate(pools):
        for exact in (True, False):
            for k, text in enumerate(new_texts):
                if out[k][0] is not None:
                    continue
                for item_id, other in pool:
                    if item_id in used:
                        continue
                    if _norm(text) == _norm(other) if exact else same_item(text, other):
                        out[k] = (item_id, p)
                        used.add(item_id)
                        break
    return out


def place(kept: list[str], new_order: list[str], inserted: set[str]) -> list[str]:
    """`kept` (the current order) with each id of `inserted` put after its predecessor in
    `new_order` (the LLM's), the nearest one that is there; the first of the LLM's goes first;
    one whose predecessors are all gone goes at the end."""
    out = list(kept)
    for i, item_id in enumerate(new_order):
        if item_id not in inserted:
            continue
        pos = None
        for j in range(i - 1, -1, -1):
            if new_order[j] in out:
                pos = out.index(new_order[j]) + 1
                break
        if pos is None:
            pos = 0 if i == 0 else len(out)
        out.insert(pos, item_id)
    return out


def touched(item: ActionItem, base: dict | None, base_text: str | None) -> bool:
    """The human did something to this item since the LLM's last output: its text, owner or
    due differ, it was added by hand, or it is done, followed or has an issue."""
    if item.done or item.follow or item.issue_url:
        return True
    return edited(item.text, base_text, scalars(item), base)


def edited(text: str, base_text: str | None, values: dict | None, base: dict | None) -> bool:
    """The text, owner or due differ from the LLM's (or it was added by hand)."""
    if base_text is None:
        return True
    if text != base_text:
        return True
    if values is None:
        return False
    base = base or {}
    return values.get("owner") != base.get("owner") or values.get("due") != base.get("due")


def merge_topics(base: list[str], current: list[str], new: list[str]) -> list[str]:
    """(current - (base - new)) + (new - base), by name regardless of case and punctuation:
    the topics the LLM dropped go, the ones it added come, the human's own stay."""
    bk = {_topic_key(t) for t in base}
    nk = {_topic_key(t) for t in new}
    dropped = bk - nk
    out: list[str] = []
    seen: set[str] = set()
    for t in current:
        k = _topic_key(t)
        if k not in dropped and k not in seen:
            seen.add(k)
            out.append(t)
    for t in new:
        k = _topic_key(t)
        if k not in bk and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def whole_list(base: list[dict], current: list[dict], new: list[dict]) -> list[dict]:
    """Chapters and client facts: the new list unless the human changed the current one."""
    return new if current == base else current


def _dump(items) -> list[dict]:
    return [x.model_dump(mode="json") for x in items]


# ---- the sidecar ---------------------------------------------------------------------------


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _lock(session_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.RLock())


def _unavailable(ctx: AppContext, session_id: str, key: str, message: str, *args):
    try:
        ctx.repo.delete_summary_doc(session_id)
    except Exception:
        logger.warning("Could not forget the summary doc of %s", session_id, exc_info=True)
    _warn_once(key, message, *args)
    raise SummaryDocUnavailable(message % args if args else message)


def _binary(ctx: AppContext, session_id: str) -> str:
    from .link import find_binary

    binary = find_binary()
    if binary is None:
        _unavailable(
            ctx, session_id, "missing", "mnemosyne-link not found: a new summary replaces edits"
        )
    return binary  # type: ignore[return-value]


def _run(ctx: AppContext, session_id: str, binary: str, requests: list[dict]):
    """All merges of one operation, in one sidecar run: [(history, text)]."""
    from ..export.note_merge import MergeError, merge_many

    try:
        results = merge_many(binary, requests)
    except MergeError as e:
        _unavailable(ctx, session_id, "merge", "Merging summary edits failed: %s", e)
    bad = next((r for r in results if isinstance(r, MergeError)), None)
    if bad is not None:
        _unavailable(ctx, session_id, "merge", "Merging summary edits failed: %s", bad)
    return results


class _Batch:
    """Merge requests of one operation; `run` sends them together."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.results: list[tuple[bytes, str]] = []

    def add(self, **request) -> int:
        self.requests.append(request)
        return len(self.requests) - 1

    def start(self, llm: str | None, text: str) -> int:
        """A new history: the LLM branch holds `llm` ("" when None), the combined `text`."""
        base = llm or ""
        return self.add(base=base, app=base, vault=text if text != base else None)

    def run(self, ctx: AppContext, session_id: str, binary: str) -> None:
        self.results = _run(ctx, session_id, binary, self.requests) if self.requests else []

    def __getitem__(self, i: int) -> tuple[bytes, str]:
        return self.results[i]


# ---- loading and creating ------------------------------------------------------------------


def _current(session: Session) -> SummaryData:
    data = session.summary_data.model_copy(deep=True) if session.summary_data else SummaryData()
    return ensure_ids(data)


def _ids_of(data: SummaryData | None) -> tuple:
    if data is None:
        return ()
    return (
        tuple(a.id for a in data.action_items),
        tuple(data.decision_ids),
        tuple(data.question_ids),
    )


def _baseline(ctx: AppContext, session_id: str) -> tuple[SummaryData, str] | None:
    """The LLM output the current summary was edited from: the first "edited" version after
    the last "summarized again" or "restored ..." one (versions hold the state before each
    change). None when there is no such version (the current state is the baseline)."""
    versions = ctx.repo.versions(session_id, with_content=True)
    start = 0
    for i, v in enumerate(versions):
        if v["reason"] == "summarized again" or v["reason"].startswith("restored"):
            start = i + 1
    for v in versions[start:]:
        if v["reason"] == "edited":
            if v.get("summary_data") is None:
                return None
            data = SummaryData.model_validate(v["summary_data"])
            return data, (v.get("content") or {}).get("summary") or ""
    return None


def _load(ctx: AppContext, session_id: str) -> Doc | None:
    raw = ctx.repo.get_summary_doc(session_id)
    if raw is None:
        return None
    try:
        return Doc.from_json(raw)
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Summary doc of %s is unreadable (%s): starting again", session_id, e)
        return None


def _ensure(ctx: AppContext, session: Session) -> tuple[Doc | None, SummaryData]:
    """The doc of a meeting (created when missing) and its current data with ids (written back
    when they were missing). (None, data) for a meeting without a summary."""
    data = _current(session)
    if session.summary_data is not None and _ids_of(session.summary_data) != _ids_of(data):
        ctx.repo.update_fields(session.id, summary_data=data)
    if session.summary_data is None and not session.summary:
        return None, data
    doc = _load(ctx, session.id)
    if doc is not None:
        return doc, data
    binary = _binary(ctx, session.id)
    return _create(ctx, session, data, binary), data


def _create(ctx: AppContext, session: Session, data: SummaryData, binary: str) -> Doc:
    """A doc for a meeting that has none: the LLM base from versions (else the current state),
    the current state as the combined text of every block. One sidecar run."""
    body_now = session.summary or ""
    found = _baseline(ctx, session.id)
    if found is None:
        base, base_body = data.model_copy(deep=True), body_now
    else:
        base, base_body = found
    base = base.model_copy(deep=True)
    batch = _Batch()
    doc = Doc(body=Block(b"", base_body))
    pending: dict[str, int] = {}
    tombstones: dict[str, dict] = {}
    taken = {i for s in SECTIONS for i, _ in section(data, s)}
    for sec in SECTIONS:
        now = section(data, sec)
        ids_now = {i for i, _ in now}
        was = section(base, sec)
        by_id: dict[int, str] = {}
        for k, (old_id, _) in enumerate(was):
            if old_id and old_id in ids_now:
                by_id[k] = old_id
        rest = [k for k in range(len(was)) if k not in by_id]
        free = [(i, t) for i, t in now if i not in by_id.values()]
        matched = match_ids([was[k][1] for k in rest], [free])
        for k, (item_id, _) in zip(rest, matched, strict=True):
            if item_id is not None:
                by_id[k] = item_id
        base_ids = []
        for k, (_, text) in enumerate(was):
            item_id = by_id.get(k)
            if item_id is None:  # deleted by hand since
                old_id = was[k][0]
                if old_id and old_id not in taken:
                    item_id = old_id
                    taken.add(old_id)
                else:
                    item_id = _fresh_id(taken)
                tombstones[item_id] = _tomb(sec, text)
            base_ids.append(item_id)
        _set_ids(base, sec, base_ids)
        base_text = {i: t for i, t in section(base, sec)}
        for item_id, text in now:
            llm = base_text.get(item_id)
            pending[item_id] = batch.start(llm, text)
            doc.blocks[item_id] = Block(b"", llm)
    body_req = batch.start(base_body, body_now)
    batch.run(ctx, session.id, binary)
    for item_id, i in pending.items():
        doc.blocks[item_id].history = batch[i][0]
    doc.body.history = batch[body_req][0]
    doc.llm = LlmBase.of(base)
    doc.tombstones = tombstones
    ctx.repo.put_summary_doc(session.id, doc.to_json())
    return doc


def _fresh_id(taken: set[str]) -> str:
    item_id = new_id()
    while item_id in taken:
        item_id = new_id()
    taken.add(item_id)
    return item_id


def _set_ids(data: SummaryData, sec: str, ids: list[str]) -> None:
    if sec == "action_items":
        for item, item_id in zip(data.action_items, ids, strict=True):
            item.id = item_id
    else:
        setattr(data, _IDS[sec], list(ids))


def ensure_doc(ctx: AppContext, session_id: str) -> Doc | None:
    """The meeting's summary doc, created when missing (None: no such meeting or no summary).
    Raises SummaryDocUnavailable when it has to be created and the sidecar cannot."""
    with _lock(session_id):
        session = ctx.repo.get(session_id)
        if session is None:
            return None
        return _ensure(ctx, session)[0]


def _session(ctx: AppContext, session_id: str) -> Session:
    session = ctx.repo.get(session_id)
    if session is None:
        raise SummaryDocUnavailable(f"no meeting {session_id}")
    return session


def _first_doc(
    ctx: AppContext, session_id: str, data: SummaryData, body: str, binary: str, llm: bool
) -> Doc:
    """A doc for a meeting's first summary: from the LLM (`llm`) or written by hand."""
    batch = _Batch()
    reqs = {i: batch.start(t if llm else None, t) for s in SECTIONS for i, t in section(data, s)}
    body_req = batch.start(body if llm else None, body)
    batch.run(ctx, session_id, binary)
    doc = Doc(
        body=Block(batch[body_req][0], body if llm else ""),
        blocks={i: Block(batch[r][0], text_of(data, i) if llm else None) for i, r in reqs.items()},
        llm=LlmBase.of(data) if llm else LlmBase(),
    )
    ctx.repo.put_summary_doc(session_id, doc.to_json())
    return doc


def text_of(data: SummaryData, item_id: str) -> str | None:
    for sec in SECTIONS:
        for i, t in section(data, sec):
            if i == item_id:
                return t
    return None


# ---- a new LLM output ----------------------------------------------------------------------


def _report() -> dict:
    return {"kept": 0, "both": 0, "both_texts": [], "dropped_kept": 0, "new": 0}


def apply_llm(
    ctx: AppContext,
    session_id: str,
    new_data: SummaryData,
    new_summary: str,
    *,
    rebase: bool = False,
) -> tuple[SummaryData, str, dict]:
    """Merge a new LLM summary into the meeting's current one, keeping hand edits. Returns
    (summary data, body, report) for the caller to write; the doc is stored here. `rebase`
    (a revise, whose prompt showed the LLM the current summary): the current state is the base,
    so edits the output echoes are not added twice. Raises SummaryDocUnavailable."""
    with _lock(session_id):
        session = _session(ctx, session_id)
        doc, current = _ensure(ctx, session)
        binary = _binary(ctx, session_id)
        if doc is None:
            new = ensure_ids(new_data.model_copy(deep=True))
            _first_doc(ctx, session_id, new, new_summary, binary, llm=True)
            return new, new_summary, _report()
        return _merge_llm(ctx, session, doc, current, new_data, new_summary, rebase, binary)


def _merge_llm(
    ctx: AppContext,
    session: Session,
    doc: Doc,
    current: SummaryData,
    new_data: SummaryData,
    new_summary: str,
    rebase: bool,
    binary: str,
) -> tuple[SummaryData, str, dict]:
    report = _report()
    batch = _Batch()
    body_now = session.summary or ""
    taken = set(doc.blocks) | set(doc.tombstones)
    taken |= {i for s in SECTIONS for i, _ in section(current, s)}
    cur_items = {a.id: a for a in current.action_items}
    new_items: dict[str, ActionItem] = {}
    # Per block merged or started: (request index, the LLM branch's text after this merge).
    pending: dict[str, tuple[int, str | None]] = {}
    texts: dict[str, str] = {}  # the resulting text of each block kept
    orders: dict[str, list[str]] = {}
    new_llm = LlmBase(
        topics=list(new_data.topics),
        chapters=_dump(new_data.chapters),
        client_facts=_dump(new_data.client_facts),
    )
    from_human: set[str] = set()  # ids whose fields stay the human's (matched a hand-made item)
    both_texts: list[str] = []

    for sec in SECTIONS:
        now = section(current, sec)
        now_text = dict(now)
        now_ids = [i for i, _ in now]
        # Deleted by hand without going through apply_human: tombstoned now.
        for item_id in doc.llm.order[sec]:
            if item_id in doc.blocks and item_id not in now_text:
                doc.tombstones[item_id] = _tomb(sec, doc.blocks.pop(item_id).llm)
        base_order = doc.llm.order[sec]
        llm_text = {
            i: (doc.blocks[i].llm if i in doc.blocks else _tomb_text(doc, i)) or ""
            for i in base_order
        }
        if rebase:
            pool_a = list(now)
            pool_b: list[tuple[str, str]] = []
        else:
            pool_a = [(i, llm_text[i]) for i in base_order]
            pool_b = [(i, t) for i, t in now if i not in llm_text]
        in_a = {i for i, _ in pool_a}
        pool_c = [
            (i, t["text"])
            for i, t in doc.tombstones.items()
            if t["section"] == sec and i not in in_a
        ]
        fresh = section(new_data, sec)
        matched = match_ids([t for _, t in fresh], [pool_a, pool_b, pool_c])
        new_ids: list[str] = []
        kinds: dict[str, int | None] = {}
        for item_id, pool in matched:
            if item_id is None:
                item_id = _fresh_id(taken)
            new_ids.append(item_id)
            kinds[item_id] = pool
        new_text = {i: t for i, (_, t) in zip(new_ids, fresh, strict=True)}
        if sec == "action_items":
            for item_id, item in zip(new_ids, new_data.action_items, strict=True):
                new_items[item_id] = item
                new_llm.action_items[item_id] = scalars(item)
        else:
            new_llm.at(sec).update(zip(new_ids, _ats(new_data, sec), strict=True))
        new_llm.order[sec] = new_ids

        # Which blocks stay, in the current order; then the LLM's new ones.
        kept: list[str] = []
        for item_id in now_ids:
            if item_id in new_text:
                kept.append(item_id)
                continue
            base_text = llm_text.get(item_id)
            if sec == "action_items":
                keep = touched(cur_items[item_id], doc.llm.action_items.get(item_id), base_text)
            else:
                keep = edited(now_text[item_id], base_text, None, None)
            if rebase or item_id in llm_text:
                if not keep:
                    doc.blocks.pop(item_id, None)
                    continue
                report["dropped_kept"] += 1
            kept.append(item_id)
        inserted = {i for i, p in kinds.items() if p is None}
        for item_id in kinds:
            if item_id in doc.tombstones:  # deleted by hand: stays so, its new text remembered
                doc.tombstones[item_id] = _tomb(sec, new_text[item_id])
        order = place(kept, new_ids, inserted)
        orders[sec] = order
        report["new"] += len(inserted)

        for item_id in order:
            cur_text = now_text.get(item_id)
            block = doc.blocks.get(item_id)
            if item_id in inserted:
                pending[item_id] = (batch.add(app=new_text[item_id], vault=None), new_text[item_id])
                continue
            assert cur_text is not None
            texts[item_id] = cur_text
            old_llm = block.llm if block is not None else None
            values = scalars(cur_items[item_id]) if sec == "action_items" else None
            base_values = doc.llm.action_items.get(item_id) if sec == "action_items" else None
            if edited(cur_text, old_llm, values, base_values):
                report["kept"] += 1
            if item_id not in new_text:  # kept, the LLM no longer lists it
                if block is None:
                    pending[item_id] = (batch.start(None, cur_text), None)
                continue
            text = new_text[item_id]
            if block is None:  # hand-made without a history yet
                if rebase:
                    pending[item_id] = (batch.add(app=text, vault=None), text)
                else:
                    from_human.add(item_id)
                    pending[item_id] = (batch.start(text, cur_text), text)
            elif rebase:
                if text != old_llm or cur_text != old_llm:
                    req = batch.add(history=block.history, app=text, vault=cur_text, rebase=True)
                    pending[item_id] = (req, text)
            elif kinds[item_id] == 1:  # a hand-made item the LLM now lists too: the human's
                from_human.add(item_id)
                pending[item_id] = (batch.start(text, cur_text), text)
            elif text != old_llm:
                req = batch.add(history=block.history, app=text, vault=cur_text)
                pending[item_id] = (req, text)
                if cur_text != old_llm:
                    report["both"] += 1
                    both_texts.append(item_id)

    body_req = None
    body_text = body_now
    old_body = doc.body.llm or ""
    if body_now != old_body:
        report["kept"] += 1
    if rebase:
        if new_summary != old_body or body_now != old_body:
            body_req = batch.add(
                history=doc.body.history, app=new_summary, vault=body_now, rebase=True
            )
    elif new_summary != old_body:
        body_req = batch.add(history=doc.body.history, app=new_summary, vault=body_now)
        if body_now != old_body:
            report["both"] += 1
            both_texts.append("")

    batch.run(ctx, session.id, binary)

    for item_id, (req, llm) in pending.items():
        history_bytes, text = batch[req]
        doc.blocks[item_id] = Block(history_bytes, llm)
        texts[item_id] = text
    if body_req is not None:
        doc.body = Block(batch[body_req][0], new_summary)
        body_text = batch[body_req][1]

    out = _project(current, new_data, doc, new_llm, orders, texts, new_items, from_human, rebase)
    out.topics = apply_aliases(out.topics, ctx.settings.topic_aliases)
    report["both_texts"] = [
        texts[i] if i else body_text.strip()[:120] for i in both_texts[:BOTH_TEXTS]
    ]
    live_ids = {i for s in SECTIONS for i in orders[s]}
    for item_id in list(doc.blocks):
        if item_id not in live_ids:
            del doc.blocks[item_id]
    doc.llm = new_llm
    ctx.repo.put_summary_doc(session.id, doc.to_json())
    return out, body_text, report


def _project(
    current: SummaryData,
    new_data: SummaryData,
    doc: Doc,
    new_llm: LlmBase,
    orders: dict[str, list[str]],
    texts: dict[str, str],
    new_items: dict[str, ActionItem],
    from_human: set[str],
    rebase: bool,
) -> SummaryData:
    """The merged summary data: blocks in `orders` with their `texts`, fields by the rules."""
    out = current.model_copy(deep=True)  # follow-up draft, edited_at / edited_by stay
    for key in ("title", "style", "provider", "model", "source_hash"):
        setattr(out, key, getattr(new_data, key))
    cur_items = {a.id: a for a in current.action_items}
    items: list[ActionItem] = []
    for item_id in orders["action_items"]:
        cur, fresh = cur_items.get(item_id), new_items.get(item_id)
        if cur is None:
            assert fresh is not None
            item = fresh.model_copy(deep=True)
        else:
            item = cur.model_copy(deep=True)
            if fresh is not None and item_id in from_human:
                if item.at is None:
                    item.at = fresh.at
            elif fresh is not None:
                base = scalars(cur) if rebase else doc.llm.action_items.get(item_id, {})
                now, new = scalars(cur), scalars(fresh)
                values = {k: three_way(base.get(k), now[k], new[k]) for k in SCALARS}
                values["due"] = date.fromisoformat(values["due"]) if values["due"] else None
                item = item.model_copy(update=values)
        item.id = item_id
        item.text = texts[item_id]
        items.append(item)
    out.action_items = items
    for sec in ("decisions", "open_questions"):
        cur_at = dict(zip(getattr(current, _IDS[sec]), _ats(current, sec), strict=True))
        new_at = new_llm.at(sec)
        base_at = cur_at if rebase else doc.llm.at(sec)
        ats = []
        for item_id in orders[sec]:
            if item_id not in cur_at:
                ats.append(new_at.get(item_id))
            elif item_id in new_at and item_id not in from_human:
                ats.append(three_way(base_at.get(item_id), cur_at[item_id], new_at[item_id]))
            elif item_id in new_at and cur_at[item_id] is None:
                ats.append(new_at[item_id])
            else:
                ats.append(cur_at[item_id])
        if sec == "decisions":
            out.decisions = [texts[i] for i in orders[sec]]
            out.decision_at = ats
            out.decision_ids = list(orders[sec])
        else:
            out.open_questions = [texts[i] for i in orders[sec]]
            out.question_at = ats
            out.question_ids = list(orders[sec])
    base_topics = current.topics if rebase else doc.llm.topics
    out.topics = merge_topics(base_topics, current.topics, new_data.topics)
    cur_ch, cur_cf = _dump(current.chapters), _dump(current.client_facts)
    base_ch = cur_ch if rebase else doc.llm.chapters
    base_cf = cur_cf if rebase else doc.llm.client_facts
    chapters = whole_list(base_ch, cur_ch, new_llm.chapters)
    out.chapters = [Chapter.model_validate(c) for c in chapters]
    facts = whole_list(base_cf, cur_cf, new_llm.client_facts)
    out.client_facts = [ClientFact.model_validate(f) for f in facts]
    return out


# ---- a human edit --------------------------------------------------------------------------


def apply_human(
    ctx: AppContext, session_id: str, new_data: SummaryData, new_summary: str, by: str
) -> tuple[SummaryData, str, dict]:
    """Record a hand edit (editor, Tasks, restore, Obsidian) in the doc. Returns (summary
    data, body, report) for the caller to write: `new_data` with ids for new entries and, when
    anything changed, edited_at / edited_by. New entries get blocks without an LLM side; entries
    that are gone are tombstoned. Raises SummaryDocUnavailable."""
    with _lock(session_id):
        session = _session(ctx, session_id)
        doc, current = _ensure(ctx, session)
        binary = _binary(ctx, session_id)
        data = new_data.model_copy(deep=True)
        taken = set(doc.tombstones) if doc is not None else set()
        for sec in SECTIONS:  # an id the human cannot bring back: a new one
            ids = [i if i not in taken else "" for i, _ in section(data, sec)]
            _set_ids(data, sec, ids)
        ensure_ids(data)
        body_now = session.summary or ""
        report = {"changed": 0, "added": 0, "deleted": 0}
        if doc is None:
            _first_doc(ctx, session_id, data, new_summary, binary, llm=False)
            report["added"] = sum(len(section(data, s)) for s in SECTIONS)
            return _stamp(data, current, new_summary, body_now, by), new_summary, report
        batch = _Batch()
        pending: dict[str, int] = {}
        now_text = {i: t for s in SECTIONS for i, t in section(current, s)}
        new_ids: set[str] = set()
        for sec in SECTIONS:
            for item_id, text in section(data, sec):
                new_ids.add(item_id)
                block = doc.blocks.get(item_id)
                if block is None:
                    pending[item_id] = batch.start(None, text)
                    report["added"] += 1
                elif text != now_text.get(item_id):
                    pending[item_id] = batch.add(
                        history=block.history, app=block.llm or "", vault=text
                    )
                    report["changed"] += 1
        sec_of = {i: s for s in SECTIONS for i in doc.llm.order[s]}
        sec_of.update({i: s for s in SECTIONS for i, _ in section(current, s)})
        for item_id in sorted(set(doc.blocks) | set(now_text)):
            if item_id not in new_ids:
                block = doc.blocks.pop(item_id, None)
                text = (block.llm if block else None) or now_text.get(item_id, "")
                if item_id in sec_of:
                    doc.tombstones[item_id] = _tomb(sec_of[item_id], text)
                report["deleted"] += 1
        body_req = None
        if new_summary != body_now:
            body_req = batch.add(
                history=doc.body.history, app=doc.body.llm or "", vault=new_summary
            )
            report["changed"] += 1
        batch.run(ctx, session_id, binary)
        for sec in SECTIONS:
            entries = section(data, sec)
            merged = [batch[pending[i]][1] if i in pending else t for i, t in entries]
            _set_texts(data, sec, merged)
        for item_id, req in pending.items():
            old = doc.blocks.get(item_id)
            doc.blocks[item_id] = Block(batch[req][0], old.llm if old is not None else None)
        body = new_summary
        if body_req is not None:
            doc.body = Block(batch[body_req][0], doc.body.llm)
            body = batch[body_req][1]
        ctx.repo.put_summary_doc(session_id, doc.to_json())
        return _stamp(data, current, body, body_now, by), body, report


def _set_texts(data: SummaryData, sec: str, texts: list[str]) -> None:
    if sec == "action_items":
        for item, text in zip(data.action_items, texts, strict=True):
            item.text = text
    elif sec == "decisions":
        data.decisions = list(texts)
    else:
        data.open_questions = list(texts)


_UNSTAMPED = {"edited_at", "edited_by", "followup"}


def _stamp(data: SummaryData, current: SummaryData, body: str, body_now: str, by: str):
    """edited_at / edited_by when the edit changed anything."""
    if body != body_now or data.model_dump(exclude=_UNSTAMPED) != current.model_dump(
        exclude=_UNSTAMPED
    ):
        data.edited_at = datetime.now()
        data.edited_by = by
    return data
