"""Three-way merge of one meeting edited on two machines (library sync, "Phase 3 design" in
docs/plans/2026-10-12-hashseq-collaboration.md, bullet "Merge").

`merge` is pure: no database, no clock but `updated_at` of the result. The caller has already
decided the two versions are concurrent (`vv_relation`) and found the base: our state the peer
last incorporated. Texts (the summary body, the notes, every decision, open question and action
item by id, bookmark notes, comment texts) go through the sidecar's one-off three-way form
(`base`), all of a meeting in one `merge_many` run; everything else is rules in Python:

- a scalar changed on one side takes that side, changed on both the side with the newer
  `updated_at` (listed in the report's `fields`);
- sets (topics, participants, attendees) and rows by id (bookmarks, external notes, live flags,
  comments, asset links, speaker embeddings) merge present/absent three ways: added on one
  side → added, removed on one side and untouched on the other → removed, removed vs changed →
  the change;
- summary items by id per section the same way, with Corey's rule that a touched item survives
  a deletion on the other side; the order is ours with theirs' additions placed by `place`;
- chapters, client facts and the agenda are whole lists; agenda `covered` is OR'd per point;
- the transcript is one unit: changed on one side → it, on both → the recording machine's;
- history events are a union by uid; recordings rows are the origin's;
- the notes: when both sides have a live notes document and they differ, the documents are
  merged by op-set union (services/sync_notes.py, run by the caller) and `notes` is not merged as
  text here (it is ours until the caller sets the union's render); when only theirs has one the
  text is merged as above and the adopted document catches up with it.

Without a base (`base is None`) the side with the newer `updated_at` is taken whole.
`apply` writes a merge through `summary_doc.apply_human` (the summary, so the local document
records it) and `sync_state.write_session` (everything), then logs `sync_merged`.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..export.note_merge import MergeError, merge_many
from ..models.session import AgendaItem, Chapter, ClientFact, SummaryData
from ..models.sync_session import SyncSession
from .summary_doc import _AT, _IDS, SECTIONS, merge_topics, place, section, whole_list
from .tasks import _norm, same_item

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

BOTH_TEXTS = 5
TEXT_PREVIEW = 120

# Session-level and summary-level scalars: changed on one side → it, both → the newer side.
SESSION_SCALARS = (
    "name",
    "meeting_type",
    "local_only",
    "speakers_reviewed",
    "owner_id",
    "copilot_notes",
)
SUMMARY_SCALARS = (
    "title",
    "style",
    "provider",
    "model",
    "source_hash",
    "followup",
    "edited_at",
    "edited_by",
)
ITEM_SCALARS = ("owner", "due", "at", "live", "done", "follow", "issue_url", "task_id")


# ---- version vectors ------------------------------------------------------------------------


def both_docs(ours: SyncSession, theirs: SyncSession) -> bool:
    """Both sides have a notes document and the documents differ: their union is the merge."""
    return (
        ours.notes_doc is not None
        and theirs.notes_doc is not None
        and ours.notes_doc != theirs.notes_doc
    )


def vv_relation(ours: dict[str, int], theirs: dict[str, int]) -> str:
    """How two version vectors relate: "equal", "dominates" (ours has everything theirs has
    and more), "dominated" (the reverse: theirs is a fast-forward of ours) or "concurrent"
    (a merge is needed)."""
    keys = set(ours) | set(theirs)
    ahead = any(ours.get(k, 0) > theirs.get(k, 0) for k in keys)
    behind = any(theirs.get(k, 0) > ours.get(k, 0) for k in keys)
    if ahead and behind:
        return "concurrent"
    if ahead:
        return "dominates"
    if behind:
        return "dominated"
    return "equal"


def vv_max(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in sorted(set(a) | set(b))}


# ---- the merge -----------------------------------------------------------------------------


class _Merge:
    """One merge: the three documents, the text requests gathered for one sidecar run, and
    the report."""

    def __init__(
        self,
        base: SyncSession,
        ours: SyncSession,
        theirs: SyncSession,
        *,
        ours_is_origin: bool,
        binary: str | None,
    ) -> None:
        self.base, self.ours, self.theirs = base, ours, theirs
        self.ours_is_origin = ours_is_origin
        self.binary = binary
        self.newer = _newer(ours, theirs)
        self.report: dict[str, Any] = _report()
        # Text merges: (base, ours, theirs, label) and where their results go.
        self._texts: list[tuple[str, str, str, str]] = []
        self._slots: list[tuple[Any, Any]] = []  # (container, key) set from the results

    # -- texts --------------------------------------------------------------------------

    def text(self, base: str, ours: str, theirs: str, label: str, container, key) -> str:
        """The three-way text, when it needs no sidecar; otherwise the side taken for now, the
        sidecar's answer put into container[key] after the run."""
        if ours == theirs or theirs == base:
            return ours
        if ours == base:
            self.report["kept"] += 1
            return theirs
        self.report["both"] += 1
        self._texts.append((base, ours, theirs, label))
        self._slots.append((container, key))
        return ours if self.newer == "ours" else theirs

    def run_texts(self) -> None:
        """One sidecar run for every text both sides changed; without a sidecar (or when it
        fails) the newer side's text stands and the report says so."""
        results: list[Any] = []
        if self._texts and self.binary is not None:
            requests = [{"base": b, "app": o, "vault": t} for b, o, t, _ in self._texts]
            try:
                results = merge_many(self.binary, requests)
            except MergeError as e:
                logger.warning("Sync merge: the sidecar failed, newer texts stand: %s", e)
                results = []
        for i, (_base, ours, theirs, label) in enumerate(self._texts):
            container, key = self._slots[i]
            result = results[i] if i < len(results) else None
            if isinstance(result, tuple):
                text = result[1]
            else:
                if isinstance(result, MergeError):
                    logger.warning("Sync merge of %s failed: %s", label, result)
                text = ours if self.newer == "ours" else theirs
                self.report["fields"].append(f"{label}: {self.newer} (no text merge)")
            _set(container, key, text)
            if len(self.report["both_texts"]) < BOTH_TEXTS:
                self.report["both_texts"].append(text.strip()[:TEXT_PREVIEW])

    # -- scalars ------------------------------------------------------------------------

    def scalar(self, base, ours, theirs, label: str):
        if ours == theirs or theirs == base:
            return ours
        if ours == base:
            self.report["kept"] += 1
            return theirs
        self.report["both"] += 1
        self.report["fields"].append(f"{label}: {self.newer}")
        return ours if self.newer == "ours" else theirs

    def whole(self, base: list, ours: list, theirs: list, label: str) -> list:
        """A whole list (chapters, client facts, agenda): `whole_list` when one side kept the
        base, the newer side when both changed it."""
        if ours == theirs or theirs == base or ours == base:
            if ours == base and theirs != base:
                self.report["kept"] += 1
            return whole_list(base, ours, theirs)
        self.report["both"] += 1
        self.report["fields"].append(f"{label}: {self.newer}")
        return ours if self.newer == "ours" else theirs

    def by_id(self, base: dict, ours: dict, theirs: dict, label: str, text_field=None) -> dict:
        """Rows by id, present/absent three ways. A row on both sides merges field by field
        (`text_field` as a text, the rest as scalars). Ours' order, then theirs' additions."""
        out: dict = {}
        for key in list(ours) + [k for k in theirs if k not in ours]:
            b, o, t = base.get(key), ours.get(key), theirs.get(key)
            if o is not None and t is not None:
                out[key] = self._row(b, o, t, f"{label} {key}", text_field)
            elif o is not None:  # theirs has not got it
                if b is None or o != b:  # added here, or changed here after they removed it
                    out[key] = o
                else:
                    self.report["kept"] += 1
            elif b is None or t != b:
                self.report["kept"] += 1
                out[key] = t
        return out

    def _row(self, b, o, t, label: str, text_field):
        if o == t:
            return o
        if b is None:  # added on both sides under one id: the newer one
            if o == t:
                return o
            self.report["both"] += 1
            self.report["fields"].append(f"{label}: {self.newer}")
            return o if self.newer == "ours" else t
        if not hasattr(o, "model_fields"):  # a plain value (an embedding)
            return self.scalar(b, o, t, label)
        merged = o.model_copy(deep=True)
        for name in type(o).model_fields:
            bv, ov, tv = getattr(b, name), getattr(o, name), getattr(t, name)
            if name == text_field:
                value = self.text(bv, ov, tv, f"{label} {name}", merged, name)
            else:
                value = self.scalar(bv, ov, tv, f"{label} {name}")
            setattr(merged, name, value)
        return merged

    # -- summary items ------------------------------------------------------------------

    def items(self, sec: str, base: SummaryData, ours: SummaryData, theirs: SummaryData):
        """One section: the ids that survive in order, their texts and (for action items)
        the merged items / (for the others) the merged `*_at` values."""
        b_text, o_text, t_text = (dict(section(d, sec)) for d in (base, ours, theirs))
        b_val, o_val, t_val = (_values(d, sec) for d in (base, ours, theirs))
        o_order = [i for i, _ in section(ours, sec)]
        t_order = [i for i, _ in section(theirs, sec)]
        texts: dict[str, str] = {}
        values: dict[str, Any] = {}
        kept: list[str] = []
        inserted: set[str] = set()
        # An item added on both sides with different ids but the same wording: one block.
        added_here = [i for i in o_order if i not in b_text and i not in t_text]
        alias: dict[str, str] = {}
        for t_id in t_order:
            if t_id in b_text or t_id in o_text:
                continue
            twin = next(
                (
                    i
                    for i in added_here
                    if i not in alias.values() and same_item(o_text[i], t_text[t_id])
                ),
                None,
            )
            if twin is not None:
                alias[t_id] = twin
        t_text = {alias.get(i, i): t for i, t in t_text.items()}
        t_val = {alias.get(i, i): v for i, v in t_val.items()}
        t_order = [alias.get(i, i) for i in t_order]

        for item_id in o_order:
            o_t, b_t, t_t = o_text[item_id], b_text.get(item_id), t_text.get(item_id)
            if t_t is None:
                if b_t is None:  # added here
                    kept.append(item_id)
                    texts[item_id], values[item_id] = o_t, o_val[item_id]
                elif o_t != b_t or o_val[item_id] != b_val.get(item_id):  # touched here
                    kept.append(item_id)
                    texts[item_id], values[item_id] = o_t, o_val[item_id]
                else:  # deleted there, untouched here
                    self.report["kept"] += 1
                continue
            kept.append(item_id)
            label = f"{sec} {item_id}"
            if b_t is None:  # added on both sides (one id, or twins by wording): no base
                if o_t == t_t:
                    texts[item_id] = o_t
                else:
                    self.report["both"] += 1
                    self.report["fields"].append(f"{label}: {self.newer}")
                    texts[item_id] = o_t if self.newer == "ours" else t_t
                values[item_id] = _merge_values(self, None, o_val[item_id], t_val[item_id], label)
                continue
            texts[item_id] = self.text(b_t, o_t, t_t, label, texts, item_id)
            values[item_id] = _merge_values(
                self, b_val.get(item_id), o_val[item_id], t_val[item_id], label
            )
        for item_id in t_order:
            if item_id in o_text or item_id in alias.values():
                continue
            b_t, t_t = b_text.get(item_id), t_text[item_id]
            if b_t is None or t_t != b_t or t_val[item_id] != b_val.get(item_id):
                # Added there, or touched there after we deleted it: it survives.
                self.report["kept"] += 1
                inserted.add(item_id)
                texts[item_id], values[item_id] = t_t, t_val[item_id]
        order = place(kept, t_order, inserted)
        return order, texts, values


def _merge_values(m: _Merge, base: dict | None, ours: dict, theirs: dict, label: str) -> dict:
    """An item's fields (scalars by id)."""
    out = {}
    for k in ours:
        bv = base.get(k) if base is not None else None
        if base is None:  # added on both sides: a value set on one side only is taken
            if ours[k] == theirs[k] or theirs[k] in (None, False, ""):
                out[k] = ours[k]
            elif ours[k] in (None, False, ""):
                out[k] = theirs[k]
            else:
                out[k] = m.scalar(None, ours[k], theirs[k], f"{label} {k}")
        else:
            out[k] = m.scalar(bv, ours[k], theirs[k], f"{label} {k}")
    return out


def _values(data: SummaryData, sec: str) -> dict[str, Any]:
    """id -> the item's non-text fields: action item scalars, or {"at": ...}."""
    if sec == "action_items":
        return {a.id: {k: getattr(a, k) for k in ITEM_SCALARS} for a in data.action_items}
    ids = getattr(data, _IDS[sec])
    ats = getattr(data, _AT[sec])
    return {i: {"at": ats[k] if k < len(ats) else None} for k, i in enumerate(ids)}


def _set(container, key, value) -> None:
    if isinstance(container, dict):
        container[key] = value
    else:
        setattr(container, key, value)


def _newer(ours: SyncSession, theirs: SyncSession) -> str:
    return "theirs" if theirs.updated_at > ours.updated_at else "ours"


def _report() -> dict:
    return {
        "kept": 0,
        "both": 0,
        "both_texts": [],
        "fields": [],
        "transcript": "ours",
        "replaced": False,
    }


def _stable_ids(data: SummaryData | None) -> SummaryData:
    """A copy with ids on every entry: entries without one (older meetings) get an id from
    their wording, so the three sides agree on it."""
    data = data.model_copy(deep=True) if data is not None else SummaryData()
    taken: set[str] = set()

    def fill(ids: list[str], texts: list[str], prefix: str) -> list[str]:
        out = []
        for i, text in enumerate(texts):
            item_id = ids[i] if i < len(ids) else ""
            if not item_id or item_id in taken:
                item_id = prefix + hashlib.sha1(_norm(text).encode()).hexdigest()[:7]
                while item_id in taken:
                    item_id += "x"
            taken.add(item_id)
            out.append(item_id)
        return out

    ids = fill([a.id for a in data.action_items], [a.text for a in data.action_items], "a")
    for item, item_id in zip(data.action_items, ids, strict=True):
        item.id = item_id
    data.decision_ids = fill(data.decision_ids, data.decisions, "d")
    data.question_ids = fill(data.question_ids, data.open_questions, "q")
    return data


def _set_merge(base: list, ours: list, theirs: list) -> list:
    """(ours − (base − theirs)) ∪ (theirs − base), in ours' order then theirs'."""
    dropped = {x for x in base if x not in theirs}
    out = [x for x in ours if x not in dropped]
    for x in theirs:
        if x not in base and x not in out:
            out.append(x)
    return out


def _dump(items) -> list[dict]:
    return [x.model_dump(mode="json") for x in items]


def merge(
    base: SyncSession | None,
    ours: SyncSession,
    theirs: SyncSession,
    *,
    ours_is_origin: bool,
    binary: str | None,
) -> tuple[SyncSession, dict]:
    """The three-way merge of a meeting: (the merged document, a report). `base` is our state
    the peer last incorporated; None when none is known, and then the side with the newer
    `updated_at` is taken whole (`replaced` in the report). `binary` is the sidecar (None:
    texts both sides changed take the newer side's). The result's `vv` is the pointwise
    maximum of both (the caller's write bumps ours), its `updated_at` now.

    The report: `kept` (fields and blocks taken from theirs), `both` (changed on both sides),
    `both_texts` (the first few merged texts both sides changed), `fields` (what both sides
    changed and which side was taken), `transcript` ("ours", "theirs" or "merged-origin":
    both changed it and the recording machine's stands), `replaced` (no base: the other side
    was dropped whole)."""
    now = datetime.now()
    vv = vv_max(ours.vv, theirs.vv)
    if base is None:
        side = _newer(ours, theirs)
        taken = ours if side == "ours" else theirs
        out = taken.model_copy(deep=True)
        report = _report()
        report.update(replaced=True, taken=side, transcript=side)
        if side == "theirs":
            report["kept"] += 1
        elif theirs.notes_doc is not None and ours.notes_doc is None:
            # Their document is adopted: our text edits cannot be merged without a base, and
            # catching the document up with them would delete what only the document has.
            out.notes = theirs.notes
        _finish(out, ours, theirs, vv, now, ours_is_origin)
        return out, report

    m = _Merge(base, ours, theirs, ours_is_origin=ours_is_origin, binary=binary)
    out = ours.model_copy(deep=True)
    for name in SESSION_SCALARS:
        setattr(
            out,
            name,
            m.scalar(getattr(base, name), getattr(ours, name), getattr(theirs, name), name),
        )
    if not both_docs(ours, theirs):  # else the caller sets the union's render
        out.notes = m.text(base.notes, ours.notes, theirs.notes, "notes", out, "notes")
    out.participants = _set_merge(base.participants, ours.participants, theirs.participants)
    out.attendees = _set_merge(base.attendees, ours.attendees, theirs.attendees)
    for field in ("participants", "attendees"):
        if getattr(out, field) != getattr(ours, field):
            m.report["kept"] += 1
    agenda = m.whole(_agenda(base.agenda), _agenda(ours.agenda), _agenda(theirs.agenda), "agenda")
    out.agenda = _covered(agenda, ours.agenda, theirs.agenda)
    item_texts = _merge_summary(m, out)

    # Rows by id.
    out.bookmarks = list(
        m.by_id(
            _rows(base.bookmarks),
            _rows(ours.bookmarks),
            _rows(theirs.bookmarks),
            "bookmark",
            "note",
        ).values()
    )
    out.external_notes = list(
        m.by_id(
            _rows(base.external_notes),
            _rows(ours.external_notes),
            _rows(theirs.external_notes),
            "external notes",
        ).values()
    )
    out.live_flags = list(
        m.by_id(
            _rows(base.live_flags), _rows(ours.live_flags), _rows(theirs.live_flags), "live flag"
        ).values()
    )
    out.comments = list(
        m.by_id(
            _rows(base.comments), _rows(ours.comments), _rows(theirs.comments), "comment", "text"
        ).values()
    )
    out.assets = list(
        m.by_id(_rows(base.assets), _rows(ours.assets), _rows(theirs.assets), "asset").values()
    )
    out.speaker_embeddings = m.by_id(
        dict(base.speaker_embeddings),
        dict(ours.speaker_embeddings),
        dict(theirs.speaker_embeddings),
        "speaker",
    )

    # The transcript as one unit.
    b_tr, o_tr, t_tr = (_dump(d.transcript) for d in (base, ours, theirs))
    if o_tr == t_tr or t_tr == b_tr:
        out.transcript = [s.model_copy(deep=True) for s in ours.transcript]
        m.report["transcript"] = "ours"
    elif o_tr == b_tr:
        out.transcript = [s.model_copy(deep=True) for s in theirs.transcript]
        m.report["transcript"] = "theirs"
        m.report["kept"] += 1
    else:
        origin = ours if ours_is_origin else theirs
        out.transcript = [s.model_copy(deep=True) for s in origin.transcript]
        m.report["transcript"] = "merged-origin"
        m.report["both"] += 1
        m.report["fields"].append(f"transcript: {'ours' if ours_is_origin else 'theirs'} (origin)")

    m.run_texts()  # one sidecar run: body, notes, items, bookmark notes and comments together
    for c in out.comments:
        if c.deleted:  # deleted on one side, edited on the other: the deletion empties it
            c.text, c.mentions = "", []
    if item_texts is not None:
        _set_item_texts(out.summary_data, item_texts)
    _finish(out, ours, theirs, vv, now, ours_is_origin)
    return out, m.report


def _finish(
    out: SyncSession,
    ours: SyncSession,
    theirs: SyncSession,
    vv: dict,
    now: datetime,
    ours_is_origin: bool,
) -> None:
    """What every merge shares: ids and `created_at` are ours, events are a union, the
    recordings rows and the origin's audio path are the recording machine's."""
    out.id, out.created_at, out.origin = ours.id, ours.created_at, ours.origin
    out.notes_doc = None  # documents are merged apart from the rest (services/sync_notes.py)
    out.vv, out.updated_at = vv, now
    seen = {e.uid for e in ours.events}
    out.events = [e.model_copy() for e in ours.events] + [
        e.model_copy() for e in theirs.events if e.uid not in seen
    ]
    origin = ours if ours_is_origin else theirs
    out.recordings = [r.model_copy(deep=True) for r in origin.recordings]
    out.origin_audio = origin.origin_audio


def _rows(items) -> dict:
    return {x.id: x for x in items}


def _agenda(items: list[AgendaItem]) -> list[dict]:
    """The agenda without `covered`, which is OR'd per point rather than merged as a list."""
    return [{"text": a.text} for a in items]


def _covered(
    merged: list[dict], ours: list[AgendaItem], theirs: list[AgendaItem]
) -> list[AgendaItem]:
    done = {a.text for a in ours if a.covered} | {a.text for a in theirs if a.covered}
    return [AgendaItem(text=a["text"], covered=a["text"] in done) for a in merged]


def _merge_summary(m: _Merge, out: SyncSession) -> dict[str, dict[str, str]] | None:
    """The summary (body and data) into `out`. Returns the item texts per section, to be put
    on the data after the sidecar run (None when no item merge was needed)."""
    base, ours, theirs = m.base, m.ours, m.theirs
    if ours.summary_data is None and theirs.summary_data is None:
        out.summary = m.text(base.summary, ours.summary, theirs.summary, "summary", out, "summary")
        return None
    o_changed = ours.summary != base.summary or ours.summary_data != base.summary_data
    t_changed = theirs.summary != base.summary or theirs.summary_data != base.summary_data
    same = ours.summary == theirs.summary and ours.summary_data == theirs.summary_data
    if not t_changed or same:
        return None  # ours stands (it is already in `out`)
    if not o_changed:
        m.report["kept"] += 1
        out.summary = theirs.summary
        out.summary_data = (
            theirs.summary_data.model_copy(deep=True) if theirs.summary_data is not None else None
        )
        return None
    b, o, t = (_stable_ids(d.summary_data) for d in (base, ours, theirs))
    data = o.model_copy(deep=True)
    for name in SUMMARY_SCALARS:
        setattr(
            data,
            name,
            m.scalar(getattr(b, name), getattr(o, name), getattr(t, name), f"summary {name}"),
        )
    data.topics = merge_topics(b.topics, o.topics, t.topics)
    if data.topics != o.topics:
        m.report["kept"] += 1
    chapters = m.whole(_dump(b.chapters), _dump(o.chapters), _dump(t.chapters), "chapters")
    data.chapters = [Chapter.model_validate(c) for c in chapters]
    facts = m.whole(
        _dump(b.client_facts), _dump(o.client_facts), _dump(t.client_facts), "client facts"
    )
    data.client_facts = [ClientFact.model_validate(f) for f in facts]
    o_items = {a.id: a for a in o.action_items}
    t_items = {a.id: a for a in t.action_items}
    texts_by_sec: dict[str, dict[str, str]] = {}
    for sec in SECTIONS:
        order, texts, values = m.items(sec, b, o, t)
        texts_by_sec[sec] = texts
        if sec == "action_items":
            items = []
            for item_id in order:
                src = o_items.get(item_id) or t_items[item_id]
                item = src.model_copy(deep=True, update=values[item_id])
                item.id = item_id
                items.append(item)
            data.action_items = items
        else:
            ats = [values[i]["at"] for i in order]
            if sec == "decisions":
                data.decision_ids, data.decision_at = list(order), ats
                links = m.by_id(
                    dict(b.decision_links),
                    dict(o.decision_links),
                    dict(t.decision_links),
                    "decision link",
                )
                data.decision_links = {k: v for k, v in links.items() if k in set(order)}
            else:
                data.question_ids, data.question_at = list(order), ats
    out.summary_data = data
    out.summary = m.text(base.summary, ours.summary, theirs.summary, "summary", out, "summary")
    return texts_by_sec


def _set_item_texts(data: SummaryData, texts_by_sec: dict[str, dict[str, str]]) -> None:
    for sec in SECTIONS:
        texts = texts_by_sec[sec]
        if sec == "action_items":
            for item in data.action_items:
                item.text = texts[item.id]
        elif sec == "decisions":
            data.decisions = [texts[i] for i in data.decision_ids]
        else:
            data.open_questions = [texts[i] for i in data.question_ids]


# ---- writing a merge ------------------------------------------------------------------------


def apply(
    ctx: AppContext,
    merged: SyncSession,
    *,
    local_device_id: str,
    by: str,
    report: dict,
    source: str | None = None,
) -> str:
    """Write a merge into the library: the summary first through `summary_doc.apply_human`
    (by `by`, so the local summary document records the edit; the merge's `edited_at` /
    `edited_by` stand), everything through `sync_state.write_session(bump=True)`, then a
    `sync_merged` history line (`source`: who it came from, `by` when not given). When the
    transcript is not ours any more, ours is kept as a version first. Returns the meeting id."""
    from . import history
    from .summary_doc import SummaryDocUnavailable, apply_human
    from .sync_state import write_session

    sid = merged.id
    doc = merged.model_copy(deep=True)
    before = ctx.repo.get(sid)
    if before is not None and report.get("transcript") in ("theirs", "merged-origin"):
        try:
            ctx.repo.keep_version(before, "synced")
        except Exception:
            logger.warning("Could not keep a version of %s before a sync merge", sid, exc_info=True)
    if before is not None and (doc.summary_data is not None or doc.summary):
        data = doc.summary_data.model_copy(deep=True) if doc.summary_data else SummaryData()
        try:
            out, body, _ = apply_human(ctx, sid, data, doc.summary, by)
        except SummaryDocUnavailable:
            pass  # the summary is written as merged, the doc starts again later
        else:
            out.edited_at, out.edited_by = data.edited_at, data.edited_by
            doc.summary_data, doc.summary = out, body
    write_session(ctx.repo, doc, local_device_id=local_device_id, vv=doc.vv, bump=True)
    history.log(
        ctx,
        sid,
        "sync_merged",
        source=by if source is None else source,
        kept=report.get("kept", 0),
        both=report.get("both", 0),
        both_texts=list(report.get("both_texts", [])),
        transcript=report.get("transcript", "ours"),
    )
    return sid
