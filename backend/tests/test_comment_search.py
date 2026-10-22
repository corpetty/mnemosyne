"""Comments in keyword search (comments_fts): found by their words, gone when deleted, only on
meetings the caller can read; Ask and the semantic index never see them."""

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import sharing


def _meeting(repo, name="Planning") -> str:
    return repo.save(
        Session(
            name=name,
            transcript=[TranscriptSegment(text="the quarterly plan", speaker="A", start=0, end=2)],
            summary="We planned.",
        )
    ).id


def _kw(client, q):
    return client.get("/api/search", params={"q": q, "mode": "keyword"}).json()


def test_a_comments_words_are_found(client, ctx):
    sid = _meeting(ctx.repo)
    cid = ctx.repo.add_comment(
        sid, text="Is the zeppelin budget approved?", target="item", target_id="ab12", author="Ann"
    )
    hits = _kw(client, "zeppelin")
    assert [h["session_id"] for h in hits] == [sid]
    (c,) = hits[0]["comments"]
    assert (c["id"], c["author"], c["target"], c["target_id"]) == (cid, "Ann", "item", "ab12")
    assert "[[zeppelin]]" in c["snippet"]
    assert hits[0]["segments"] == [] and hits[0]["session_snippet"] is None


def test_the_quote_is_searched_and_an_edit_reindexes(client, ctx):
    sid = _meeting(ctx.repo)
    cid = ctx.repo.add_comment(sid, text="check", quote="walrus migration", target="body")
    assert _kw(client, "walrus")[0]["comments"][0]["id"] == cid
    ctx.repo.update_comment_text(cid, "now about narwhals")
    assert _kw(client, "narwhals")[0]["comments"][0]["id"] == cid
    assert _kw(client, "check") == []


def test_a_deleted_comment_is_not_found(client, ctx):
    sid = _meeting(ctx.repo)
    gone = ctx.repo.add_comment(sid, text="remove the kraken")
    assert _kw(client, "kraken")
    ctx.repo.delete_comment(gone)
    assert _kw(client, "kraken") == []
    # One that stays as an empty row because a reply hangs on it is not indexed either.
    root = ctx.repo.add_comment(sid, text="the basilisk problem")
    ctx.repo.add_comment(sid, text="agreed", parent_id=root)
    assert _kw(client, "basilisk")
    ctx.repo.delete_comment(root)
    assert _kw(client, "basilisk") == []
    assert _kw(client, "agreed")[0]["comments"][0]["snippet"].count("[[") == 1


def test_deleting_the_meeting_drops_its_comments(client, ctx):
    sid = _meeting(ctx.repo)
    ctx.repo.add_comment(sid, text="phoenix rising")
    ctx.repo.delete(sid)
    assert _kw(client, "phoenix") == []
    assert ctx.repo._conn.execute("SELECT count(*) FROM comments_fts").fetchone()[0] == 0


def test_comments_made_before_the_index_existed_are_backfilled(client, ctx):
    sid = _meeting(ctx.repo)
    ctx.repo.add_comment(sid, text="legacy gryphon")
    with ctx.repo._conn:
        ctx.repo._conn.execute("DELETE FROM comments_fts")
    assert _kw(client, "gryphon") == []
    ctx.repo._backfill_fts()
    assert _kw(client, "gryphon")


def test_ask_and_the_semantic_index_do_not_see_comments(client, ctx):
    sid = _meeting(ctx.repo)
    ctx.repo.add_comment(sid, text="the chimera secret")
    assert ctx.repo.retrieve("chimera secret") == []
    assert "chimera" not in (ctx.repo.summary_passage(sid, 0.0).text or "")
    import inspect

    from mnemosyne.search import index

    assert "comment" not in inspect.getsource(index).lower()


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name in ("Ann", "Bob"):
        user = ctx.users.add(name, f"{name.lower()}@team.test", "member")
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, {"Authorization": f"Bearer {ctx.users.redeem(code, 't')[1]}"})
    with TestClient(app) as client:
        yield client, ctx, people


def test_a_comment_on_a_meeting_the_caller_cannot_see_is_not_returned(team):
    client, ctx, people = team
    ann = people["Ann"][0]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        sid = _meeting(ctx.repo)
        ctx.repo.add_comment(sid, text="confidential minotaur", author="Ann")
    finally:
        access.current.reset(reset)

    def search(who):
        return client.get(
            "/api/search",
            params={"q": "minotaur", "mode": "keyword"},
            headers=people[who][1],
        ).json()

    assert len(search("Ann")) == 1 and search("Ann")[0]["comments"]
    assert search("Bob") == []
    sharing.set_shares(ctx, sid, {people["Bob"][0].id})
    assert search("Bob")[0]["comments"][0]["author"] == "Ann"
