"""The record's pure functions (storage/records.py): part digests, the manifest, the format 2
chain and the comparison helpers, pinned by tests/vectors/records_v2.json."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from mnemosyne.models.comment import Comment
from mnemosyne.models.session import Session, SummaryData
from mnemosyne.storage import records as r

VECTORS = Path(__file__).parent / "vectors" / "records_v2.json"
SCRIPT = Path(__file__).parent.parent / "scripts" / "make_records_vectors.py"


@pytest.fixture(scope="module")
def vec():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


@pytest.fixture
def session(vec):
    return Session.model_validate(vec["session"])


@pytest.fixture
def comments(vec):
    return [Comment.model_validate(c) for c in vec["comments"]]


def digests(session, comments):
    return r.part_digests(r.part_payloads(session, comments))


# ---- vectors ---------------------------------------------------------------------------------


def test_vectors_reproduce(vec):
    # The committed file is exactly what the script writes now.
    spec = importlib.util.spec_from_file_location("make_records_vectors", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build() == vec


def test_payloads_and_digests_from_models(vec, session, comments):
    payloads = r.part_payloads(session, comments)
    assert set(payloads) == set(r.PARTS)
    for name, payload in payloads.items():
        assert json.dumps(payload, ensure_ascii=False) == vec["payloads_json"][name]
        canon = r.canonical({"part": name, "v": 1, "data": payload}).decode()
        assert canon == vec["payloads_canonical"][name]
        assert hashlib.sha256(canon.encode()).hexdigest() == vec["part_digests"][name]
    assert r.content_hash(r.content_of(session)) == vec["content_v1"]


def test_payloads_from_plain_dicts_match_the_models(vec, session, comments):
    # An export's JSON files (dicts) hash like the live objects.
    as_dict = r.part_payloads(vec["session"], vec["comments"])
    for name in r.PARTS:
        if name == "summary_data":  # a dict is taken to be the payload already
            continue
        assert as_dict[name] == r.part_payloads(session, comments)[name]
    assert r.summary_data_payload(json.loads(vec["payloads_json"]["summary_data"])) == (
        r.summary_data_payload(session.summary_data)
    )


def test_manifests_and_chain_hashes(vec):
    for key in ("manifest_v1", "manifest_erasure", "manifest_origin"):
        m = vec[key]
        assert r.canonical(m["manifest"]).decode() == m["canonical"]
        assert r.manifest_hash(m["manifest"]) == m["hash"]
        assert r.manifest_problems(m["manifest"]) == []
    c = vec["chain"]
    v1 = c["v1_seal"]
    assert (
        r.chain_hash(v1["prev"], v1["content"], v1["audio"], v1["at"], v1["reason"])
        == (v1["chain"])
    )
    s1, s2 = c["seal_1"], c["seal_2"]
    assert (
        r.chain_hash_v2(v1["chain"], vec["manifest_v1"]["hash"], s1["at"], s1["reason"])
        == (s1["chain"])
    )
    assert (
        r.chain_hash_v2(s1["chain"], vec["manifest_erasure"]["hash"], s2["at"], s2["reason"])
        == (s2["chain"])
    )
    assert vec["manifest_origin"]["manifest"]["audio"] == {}  # no audio on this computer


def test_manifest_for_matches_the_vector(vec, session, comments):
    m = r.manifest_for(
        session,
        comments,
        rev=1234,
        audio=vec["manifest_v1"]["manifest"]["audio"],
    )
    assert m == vec["manifest_v1"]["manifest"]


def test_format_1_hashes_are_unchanged():
    # Old seals depend on these exact bytes: never edit this test's expectation.
    content = {
        "transcript": [{"speaker": "A", "start": 0.0, "end": 1.5, "text": "é"}],
        "summary": "s",
    }
    assert (
        r.canonical(content)
        == (
            '{"summary":"s","transcript":[{"end":1.5,"speaker":"A","start":0.0,"text":"é"}]}'
        ).encode()
    )
    assert r.content_hash(content) == hashlib.sha256(r.canonical(content)).hexdigest()
    assert (
        r.chain_hash("p", "c", {"a": "1"}, "t", "r")
        == hashlib.sha256(b'["p","c",{"a":"1"},"t","r"]').hexdigest()
    )


# ---- determinism and sensitivity -------------------------------------------------------------


def test_digests_ignore_key_and_input_order(vec, session, comments):
    base = digests(session, comments)
    shuffled = copy.deepcopy(vec["session"])
    shuffled["external_notes"].reverse()
    shuffled["summary_data"] = dict(reversed(list(shuffled["summary_data"].items())))
    session2 = Session.model_validate(shuffled)
    assert digests(session2, list(reversed(comments))) == base
    assert r.canonical({"b": 1, "a": {"d": 1, "c": 2}}) == b'{"a":{"c":2,"d":1},"b":1}'


def test_each_part_changes_only_its_own_digest(session, comments):
    base = digests(session, comments)

    def changed(mutate, comments_=None):
        s = session.model_copy(deep=True)
        mutate(s)
        return {
            n
            for n, d in digests(s, comments if comments_ is None else comments_).items()
            if d != base[n]
        }

    assert changed(lambda s: setattr(s.transcript[0], "text", "x")) == {"transcript"}
    assert changed(lambda s: setattr(s.transcript[0], "speaker", "Z")) == {"transcript"}
    assert changed(lambda s: setattr(s, "summary", "other")) == {"summary"}
    assert changed(lambda s: setattr(s.summary_data, "followup", "hi")) == {"summary_data"}
    assert changed(lambda s: setattr(s.summary_data.action_items[0], "done", True)) == {
        "summary_data"
    }
    assert changed(lambda s: setattr(s, "notes", s.notes + "x")) == {"notes"}
    assert changed(lambda s: setattr(s, "name", "Renamed")) == {"meta"}
    assert changed(lambda s: s.attendees.append("Ada")) == {"meta"}
    assert changed(lambda s: setattr(s.agenda[1], "covered", True)) == {"meta"}
    assert changed(lambda s: setattr(s, "owner_id", "u-9")) == {"meta"}
    assert changed(lambda s: setattr(s.external_notes[0], "text", "edited")) == {"external_notes"}
    edited = [c.model_copy(update={"text": "edited"}) for c in comments]
    assert changed(lambda s: None, edited) == {"comments"}


def test_parts_with_equal_payloads_still_differ():
    assert r.part_digest("summary", "") != r.part_digest("notes", "")
    assert r.part_digest("summary", "x") != r.part_digest("summary", "y")


def test_what_is_not_in_the_record_does_not_change_it(session, comments):
    base = digests(session, comments)
    s = session.model_copy(deep=True)
    s.transcript[0].words = None  # per-word timings are not part of the record
    s.bookmarks = []
    s.local_only = True
    s.speakers_reviewed = True
    s.status = "completed"
    c2 = [
        c.model_copy(update={"changed": True, "detached": True, "can_edit": True}) for c in comments
    ]
    assert digests(s, c2) == base


def test_summary_data_exclude_defaults_keeps_an_unchanged_summary_stable():
    class Newer(SummaryData):  # a later release adds a defaulted field
        brand_new: str = ""
        brand_new_list: list[str] = []  # noqa: RUF012

    old = SummaryData(title="T", decisions=["a"])
    new = Newer(title="T", decisions=["a"])
    assert r.summary_data_payload(old) == r.summary_data_payload(new)
    assert "style" not in r.summary_data_payload(old)  # defaults are left out
    assert r.summary_data_payload(Newer(title="T", brand_new="x"))["brand_new"] == "x"
    assert r.summary_data_payload(None) is None


def test_no_summary_data_and_empty_meeting():
    s = Session(id="aaaaaaaaaaaa")
    p = r.part_payloads(s)
    assert p["summary_data"] is None and p["transcript"] == [] and p["comments"] == []
    assert p["summary"] == "" and p["notes"] == ""
    assert r.part_digest("summary_data", None) != r.part_digest("summary", "")


def test_integer_times_hash_like_floats():
    a = r.transcript_payload([{"speaker": "A", "start": 1, "end": 2, "text": "t"}])
    b = r.transcript_payload([{"speaker": "A", "start": 1.0, "end": 2.0, "text": "t"}])
    assert r.canonical(a) == r.canonical(b)
    # ... and survive a JSON round trip of the payload.
    assert r.canonical(json.loads(r.canonical(a))) == r.canonical(a)


def test_unicode_is_hashed_as_written():
    nfc, nfd = "é", "é"
    assert r.part_digest("summary", nfc) != r.part_digest("summary", nfd)
    assert r.canonical(nfc) == '"é"'.encode()  # UTF-8, not \u escapes


def test_times_are_isoformat_and_bad_ones_refused():
    from datetime import UTC, date, datetime

    base = {"id": "c", "text": "t"}
    p = r.comments_payload(
        [
            {
                **base,
                "created_at": datetime(2026, 1, 2, 3, 4, 5),
                "updated_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                "resolved_at": "verbatim",
            }
        ]
    )[0]
    assert p["created_at"] == "2026-01-02T03:04:05"
    assert p["updated_at"] == "2026-01-02T03:04:05+00:00"
    assert p["resolved_at"] == "verbatim" and p["approved_at"] is None
    assert (
        r.external_notes_payload(
            [{"id": "e", "source": "s", "text": "t", "added_at": date(2026, 1, 2)}]
        )[0]["added_at"]
        == "2026-01-02"
    )
    with pytest.raises(TypeError):
        r.comments_payload([{**base, "created_at": 5}])


def test_chain_hash_v2_depends_on_everything_and_is_not_v1():
    base = ("prev", "mh", "2026-01-01T00:00:00", "edited")
    h = r.chain_hash_v2(*base)
    for i in range(4):
        other = list(base)
        other[i] += "x"
        assert r.chain_hash_v2(*other) != h
    assert (
        h
        == hashlib.sha256(
            b'["mnemosyne/seal/2","prev","mh","2026-01-01T00:00:00","edited"]'
        ).hexdigest()
    )
    assert h != r.chain_hash("prev", "mh", {}, base[2], base[3])


# ---- the manifest ----------------------------------------------------------------------------


def parts():
    return {name: r.part_digest(name, name) for name in r.PARTS}


def test_build_manifest_shape_and_rules():
    m = r.build_manifest(session_id="s", rev=3, content_v1="c", parts=parts())
    assert m == {
        "format": 2,
        "session": "s",
        "rev": 3,
        "content_v1": "c",
        "parts": parts(),
        "audio": {},
        "audio_here": True,
        "erased": {},
    }
    assert r.manifest_problems(m) == []
    with pytest.raises(ValueError):
        r.build_manifest(session_id="s", rev=3, content_v1="c", parts={"transcript": "x"})
    # Audio of a meeting recorded elsewhere is dropped.
    m = r.build_manifest(
        session_id="s",
        rev=3,
        content_v1="c",
        parts=parts(),
        audio={"a.ogg": r.audio_entry("h", 1)},
        audio_here=False,
    )
    assert m["audio"] == {} and m["audio_here"] is False
    # The inputs are copied, not shared.
    audio = {"a.ogg": r.audio_entry("h", 1)}
    m = r.build_manifest(session_id="s", rev=3, content_v1="c", parts=parts(), audio=audio)
    audio["a.ogg"]["sha256"] = "changed"
    assert m["audio"]["a.ogg"]["sha256"] == "h"


def test_manifest_problems():
    good = r.build_manifest(session_id="s", rev=3, content_v1="c", parts=parts())
    assert r.manifest_problems([]) != []
    bad = {**good, "format": 1}
    assert r.manifest_problems(bad)
    bad = {**good, "rev": True}
    assert r.manifest_problems(bad)
    bad = {**good, "parts": {"transcript": "x"}}
    assert r.manifest_problems(bad)
    bad = {**good, "audio": {"a": {"sha256": "x"}}}
    assert "audio entry a" in r.manifest_problems(bad)[0]
    bad = {k: v for k, v in good.items() if k != "erased"}
    assert r.manifest_problems(bad)


def test_audio_key_drops_the_encrypted_suffix():
    assert r.audio_key("x_mixed.ogg.enc") == "x_mixed.ogg"
    assert r.audio_key("x_mixed.ogg") == "x_mixed.ogg"
    assert r.audio_key(".enc") == ""


# ---- comparisons -----------------------------------------------------------------------------


def test_compare_parts():
    sealed = {"transcript": "a", "summary": "b", "notes": "c", "gone": "z"}
    now = {"transcript": "a", "summary": "B", "notes": "c", "comments": "n"}
    assert r.compare_parts(sealed, now) == [
        r.PartDiff("summary", "differs"),
        r.PartDiff("comments", "added"),
        r.PartDiff("gone", "removed"),
    ]
    assert r.compare_parts(sealed, sealed) == []


def test_compare_audio():
    e = r.audio_entry
    sealed = {"a.ogg": e("1", 1), "b.ogg": e("2", 1), "c.ogg": e("3", 1)}
    erased = {"d.ogg": {**e("4", 1), "matched": True}}
    now = {"a.ogg": e("1", 1), "b.ogg": e("X", 1), "new.ogg": e("5", 1)}
    assert r.compare_audio(sealed, erased, now) == [
        r.AudioDiff("b.ogg", "differs"),
        r.AudioDiff("c.ogg", "missing"),
        r.AudioDiff("new.ogg", "added"),
    ]
    assert r.compare_audio(sealed, erased, sealed) == []
    # An erased file is expected to be gone; a copy put back is fine, another file is not.
    assert r.compare_audio({}, erased, {"d.ogg": e("4", 1)}) == []
    assert r.compare_audio({}, erased, {"d.ogg": e("9", 1)}) == [r.AudioDiff("d.ogg", "differs")]


def test_erase_audio():
    e = r.audio_entry
    audio = {"a.ogg": e("1", 10), "b.ogg": e("2", 20)}
    measured = {"a.ogg": e("1", 10), "b.ogg": e("changed", 21), "ghost.ogg": e("7", 1)}
    new_audio, erased = r.erase_audio(
        audio, {}, measured, at="T", by="admin", reason="old", deletion=4
    )
    assert new_audio == {}
    assert erased["a.ogg"] == {
        "sha256": "1", "bytes": 10, "at": "T", "by": "admin", "reason": "old",
        "deletion": 4, "matched": True,
    }  # fmt: skip
    assert erased["b.ogg"]["matched"] is False and erased["b.ogg"]["sha256"] == "changed"
    assert erased["ghost.ogg"]["matched"] is False  # never sealed
    assert audio == {"a.ogg": e("1", 10), "b.ogg": e("2", 20)}  # inputs untouched
    # Files not measured stay; a second erasure keeps the first entry and never rewrites it.
    kept, erased2 = r.erase_audio(
        {"c.ogg": e("3", 1)},
        erased,
        {"a.ogg": e("9", 9)},
        at="U",
        by="x",
        reason="r",
        deletion=None,
    )
    assert kept == {"c.ogg": e("3", 1)} and erased2 == erased


def step_manifests():
    e = r.audio_entry
    prev = r.build_manifest(
        session_id="s",
        rev=1,
        content_v1="c",
        parts=parts(),
        audio={"a.ogg": e("1", 1), "b.ogg": e("2", 1)},
    )
    return prev


def test_compare_step_accepts_an_erasure_and_ordinary_seals():
    prev = step_manifests()
    same = copy.deepcopy(prev)
    same["rev"] = 2
    assert r.compare_step(prev, same, 2) == []
    audio, erased = r.erase_audio(
        prev["audio"], {}, {"a.ogg": r.audio_entry("1", 1)}, at="T", by="b", reason="r", deletion=1
    )
    nxt = r.build_manifest(
        session_id="s", rev=2, content_v1="c", parts=parts(), audio=audio, erased=erased
    )
    assert r.compare_step(prev, nxt, 2) == []
    # A later seal keeps the erased entry and still passes.
    later = copy.deepcopy(nxt)
    later["rev"] = 3
    assert r.compare_step(nxt, later, 3) == []
    # An erasure of a file that changed is recorded with its own digest and matched False.
    audio, erased = r.erase_audio(
        prev["audio"], {}, {"a.ogg": r.audio_entry("X", 1)}, at="T", by="b", reason="r", deletion=1
    )
    nxt = r.build_manifest(
        session_id="s", rev=2, content_v1="c", parts=parts(), audio=audio, erased=erased
    )
    assert nxt["erased"]["a.ogg"]["matched"] is False
    assert r.compare_step(prev, nxt, 2) == []


def test_compare_step_catches_vanishing_and_rewritten_erasures():
    prev = step_manifests()
    gone = copy.deepcopy(prev)
    del gone["audio"]["a.ogg"]
    problems = r.compare_step(prev, gone, 2)
    assert problems == ["Audio file a.ogg left the record at seal 2 without being erased"]

    audio, erased = r.erase_audio(
        prev["audio"], {}, {"a.ogg": r.audio_entry("1", 1)}, at="T", by="b", reason="r", deletion=1
    )
    erasure = r.build_manifest(
        session_id="s", rev=2, content_v1="c", parts=parts(), audio=audio, erased=erased
    )
    # An erased entry that disappears or is edited later.
    dropped = copy.deepcopy(erasure)
    dropped["erased"] = {}
    assert "left the record" in r.compare_step(erasure, dropped, 3)[0]
    edited = copy.deepcopy(erasure)
    edited["erased"]["a.ogg"]["reason"] = "something else"
    assert "was changed" in r.compare_step(erasure, edited, 3)[0]
    # An erasure that claims a match it does not have, or denies one it has.
    lie = copy.deepcopy(erasure)
    lie["erased"]["a.ogg"]["sha256"] = "forged"
    assert "matching wrongly" in r.compare_step(prev, lie, 2)[0]
    lie = copy.deepcopy(erasure)
    lie["erased"]["a.ogg"]["matched"] = False
    assert "matching wrongly" in r.compare_step(prev, lie, 2)[0]


def test_compare_step_skips_audio_on_a_computer_without_it():
    prev = step_manifests()
    nxt = r.build_manifest(session_id="s", rev=2, content_v1="c", parts=parts(), audio_here=False)
    assert r.compare_step(prev, nxt, 2) == []
    assert r.compare_step(nxt, prev, 3) == []
