"""What goes to the vault, the digest and Ask on a computer with a team owner
(docs/plans/2026-11-09-team-hosting.md 2.4, task 8; invariants I10, I11; risk R7; decision D7).

H = the owner. The vault gets H's meetings and those a contact shared with him, never a member's
(`access.PERSONAL`); H's Ask and digest cover what he would read as a member plus his own
(`access.VIEW`); only H's digest is written to the vault; MCP requests are held to the same view.

The machine, its meetings (`host`: own, stray, granted, everyone, team, teamgrant, contact,
contactstar) and callers come from test_hosting_access.py / test_hosting_visibility.py.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from mnemosyne import access
from mnemosyne.api.routes.ask import AskRequest, ask
from mnemosyne.api.routes.digests import DigestRequest, create_digest
from mnemosyne.api.routes.export import export_all_to_obsidian, export_session
from mnemosyne.mcp_server import Backend
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import host_scope
from mnemosyne.services.pipeline import _auto_export
from mnemosyne.services.vault_sync import note_path, sync_vault, write_tick
from tests.fakes import FakeProvider
from tests.test_hosting_access import MAIN, make_machine  # noqa: F401 - a fixture
from tests.test_hosting_visibility import COMMON, MEETINGS, host, marker  # noqa: F401

THURSDAY = datetime(2026, 11, 5, 10, 0)  # in the week the digests cover
MEMBERS = {"team", "teamgrant"}  # a member's meetings, none granted to the owner
THE_OWNERS = {"own", "stray", "granted", "everyone"}
SHARED_WITH_H = {"contact", "contactstar"}  # a contact shared them with the owner


def _vault_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file())


def _found(text: str) -> set[str]:
    return {k for k in MEETINGS if marker(k) in text}


def _owner(m) -> access.Principal:
    return access.Principal(m.ctx.settings.team_owner_id, "Corey", access.ADMIN)


def _as(m, name: str, role: str = access.MEMBER):
    return access.current.set(access.Principal(m.users[name], name, role))


@pytest.fixture
def vault_host(host, tmp_path):  # noqa: F811
    """The host with a vault, the notes of people and days and the decisions log on, and one
    person (Pat) with a task in an own meeting and in a member's (and a task of nobody's)."""
    m = host
    root = tmp_path / "vault"
    root.mkdir()
    st = m.ctx.settings
    st.obsidian_vault_path = str(root)
    st.obsidian_subfolder = "meetings"
    st.obsidian_people_notes = True
    st.obsidian_daily_notes = True
    st.obsidian_decisions_log = True
    st.local_speaker_name = "Corey"
    for key in ("own", "team"):
        word = marker(key)
        m.ctx.repo.update_fields(
            m.ids[key],
            attendees=["Pat"],
            summary_data=SummaryData(
                topics=[f"topic {word}"],
                decisions=[f"decided {word}"],
                action_items=[
                    ActionItem(text=f"do {word}"),
                    ActionItem(text=f"pat {word}", owner="Pat"),
                ],
            ),
        )
    m.vault = root
    return m


def _grant_team_to_owner(m) -> None:
    m.ctx.repo.set_shares(m.ids["team"], {m.ctx.settings.team_owner_id}, "test")


def _principal(m, caller: str) -> access.Principal | None:
    """Who the caller is once the middleware has let it in (api/auth.py); None = unrestricted."""
    if caller == "api_token":
        return None
    if caller in ("window", "owner_team"):
        return _owner(m)
    name, role = {"member": ("Sam", access.MEMBER), "reviewer": ("Rae", access.REVIEWER)}[caller]
    return access.Principal({u.name: u.id for u in m.ctx.users.list()}[name], name, role)


def _job(m, caller: str, submit) -> dict:
    """Run what a route does for `caller`: `submit(ctx)` queues a job (the route function is
    called as the caller; the job copies its context), and the job runs to its end in one loop
    (the machine's test clients make a loop per request, which would cancel it)."""

    async def go() -> dict:
        reset = access.current.set(_principal(m, caller))
        try:
            job = await submit(m.ctx)
        finally:
            access.current.reset(reset)
        done = await m.ctx.jobs.wait(job.id)
        return done.model_dump(mode="json")

    return asyncio.run(go())


def _export_all(m, caller: str) -> dict:
    return _job(m, caller, export_all_to_obsidian)


def _digest(m, caller: str) -> dict:
    body = DigestRequest(start=date(2026, 11, 2), end=date(2026, 11, 8), provider="fake")
    return _job(m, caller, lambda ctx: create_digest(body, ctx))


def _ask_job(m, caller: str, **extra) -> dict:
    body = AskRequest(question=f"what about {COMMON}", provider="fake", **extra)
    return _job(m, caller, lambda ctx: ask(body, ctx))


# ---- the scopes ---------------------------------------------------------------------------


def test_the_scopes_in_sql(host):  # noqa: F811
    m = host
    repo, h = m.ctx.repo, m.ctx.settings.team_owner_id
    ids = m.ids

    def keys(scope):
        seen = repo.ids_seen_by(scope)
        return {k for k, v in ids.items() if v in seen}

    own = host_scope.own(m.ctx)
    personal = host_scope.vault(m.ctx)
    view = host_scope.view(m.ctx)
    assert keys(own) == THE_OWNERS
    assert keys(personal) == THE_OWNERS | SHARED_WITH_H
    assert keys(view) == THE_OWNERS | SHARED_WITH_H  # nothing of a member's is granted yet
    _grant_team_to_owner(m)
    assert keys(own) == keys(personal) - SHARED_WITH_H == THE_OWNERS
    assert keys(view) == THE_OWNERS | SHARED_WITH_H | {"team"}  # granted to H: in his view
    assert "teamgrant" not in keys(view)  # granted to Sam, not to H
    m.ctx.repo.set_shares(ids["teamgrant"], {"*"}, "test")  # to everyone: H is one of them
    assert "teamgrant" in keys(view) and "teamgrant" not in keys(personal)
    assert view.user_id == h


def test_a_limit_narrows_the_callers_scope_and_never_widens_it(host):  # noqa: F811
    m = host
    repo = m.ctx.repo
    vault = host_scope.vault(m.ctx)
    reset = _as(m, "Sam")
    try:
        mine = {i for i in repo.visible_ids()}
        assert m.ids["team"] in mine
        with access.limited_to(vault):  # a member's meetings are not the vault's
            assert repo.visible_ids() == {m.ids["everyone"]}  # the owner's, granted to all
            assert not repo.exists(m.ids["team"])
        with access.limited_to(host_scope.view(m.ctx), only_for=m.ctx.settings.team_owner_id):
            assert repo.visible_ids() == mine  # the owner's limit does not touch a member
    finally:
        access.current.reset(reset)
    assert repo.visible_ids() is None  # and nothing outside the block is left over


# ---- I10: the vault ---------------------------------------------------------------------


def test_auto_export_writes_the_owners_meetings_never_a_members(vault_host):
    m = vault_host
    for key in MEETINGS:
        result = _auto_export(m.ctx, m.ids[key])
        # (a meeting a contact shared is never auto-exported: its audio is with them)
        assert (result is None) == (key in MEMBERS | SHARED_WITH_H), key
    text = _vault_text(m.vault)
    assert not _found(text) & MEMBERS, "a member's meeting reached the vault"
    assert THE_OWNERS <= _found(text)


def test_export_refuses_a_members_meeting_and_the_notes_around_it_leave_members_out(vault_host):
    m = vault_host
    repo = m.ctx.repo
    with pytest.raises(access.Forbidden):
        export_session(m.ctx, repo.get(m.ids["team"]), str(m.vault))
    assert not any(m.vault.rglob("*.md"))
    export_session(m.ctx, repo.get(m.ids["own"]), str(m.vault))
    text = _vault_text(m.vault)
    # Pat's note collects tasks from the meetings the caller reads: a member's is not one.
    assert marker("own") in text and "Pat" in text
    assert marker("team") not in text
    assert marker("teamgrant") not in text


def test_a_meeting_granted_to_the_owner_is_still_not_written(vault_host):
    m = vault_host
    _grant_team_to_owner(m)
    assert _auto_export(m.ctx, m.ids["team"]) is None
    export_session(m.ctx, m.ctx.repo.get(m.ids["own"]), str(m.vault))
    assert marker("team") not in _vault_text(m.vault)  # also not through Pat's note or the log


def test_the_export_routes(vault_host):
    m = vault_host
    client, headers, _ = m.client("window")
    one = client.post(f"/api/sessions/{m.ids['team']}/export/obsidian", headers=headers)
    assert one.status_code == 403 and host_scope.NOT_IN_VAULT in one.text
    assert (
        client.post(f"/api/sessions/{m.ids['own']}/export/obsidian", headers=headers).status_code
        == 200
    )
    job = _export_all(m, "window")
    assert job["status"] == "completed", job.get("error")
    assert job["result"]["failed"] == 0
    text = _vault_text(m.vault)
    assert not _found(text) & MEMBERS
    assert {"own", "stray", "granted", "everyone"} <= _found(text)
    assert marker("team") not in text  # not in Decisions.md, Pat's note or a daily note either


def test_a_member_exporting_writes_nothing(vault_host):
    m = vault_host
    client, headers, _ = m.client("member")
    for key in ("team", "everyone"):  # his own, and an owner's meeting shared with the team
        r = client.post(f"/api/sessions/{m.ids[key]}/export/obsidian", headers=headers)
        assert r.status_code == 403, key
    for path in ("/api/export/obsidian/all", "/api/topics/obsidian"):
        r = client.post(path, headers=headers, json={"query": "zanzibar"})
        assert r.status_code == 403, path
    assert not any(m.vault.rglob("*"))


def test_the_read_back_never_touches_a_members_note(vault_host):
    """A member's note in the vault (made before the owner was set, say) is not read back."""
    m = vault_host
    st = m.ctx.settings
    owner = st.team_owner_id
    st.team_owner_id = ""  # as if no team owner yet: both meetings are exported
    try:
        for key in ("own", "team"):
            export_session(m.ctx, m.ctx.repo.get(m.ids[key]), str(m.vault))
    finally:
        st.team_owner_id = owner
    for key in ("own", "team"):
        path = note_path(m.ctx, m.ctx.repo.get(m.ids[key]), str(m.vault))
        text = path.read_text(encoding="utf-8")
        assert f"- [ ] do {marker(key)}" in text
        path.write_text(text.replace(f"- [ ] do {marker(key)}", f"- [x] do {marker(key)}"))
        later = time.time() + 5
        os.utime(path, (later, later))

    def done(key):
        return m.ctx.repo.get(m.ids[key]).summary_data.action_items[0].done

    assert sync_vault(m.ctx) == 1
    assert done("own") is True
    assert done("team") is False  # its note was never looked at
    # A tick in the app does not rewrite a member's note either.
    team = m.ctx.repo.get(m.ids["team"])
    assert write_tick(m.ctx, team, 0, True, str(m.vault)) is False
    assert "- [x] do " + marker("team") in note_path(m.ctx, team, str(m.vault)).read_text()


def test_it_holds_after_hosting_is_turned_off(vault_host):
    m = vault_host
    m.ctx.settings.team_mode = False  # team_owner_id is kept (services/team_host.py turn_off)
    m.ctx.follow_hosting()
    assert _auto_export(m.ctx, m.ids["team"]) is None
    assert _auto_export(m.ctx, m.ids["own"]) is not None
    assert marker("team") not in _vault_text(m.vault)


@pytest.mark.parametrize("kind", ["plain", "dedicated"])
def test_a_computer_without_an_owner_exports_as_before(make_machine, tmp_path, kind):  # noqa: F811
    m = make_machine(kind)
    st = m.ctx.settings
    assert st.team_owner_id == ""
    assert host_scope.vault(m.ctx) is None and host_scope.view(m.ctx) is None
    root = tmp_path / "vault"
    root.mkdir()
    st.obsidian_vault_path = str(root)
    sam = m.ctx.users.list()[1].id
    s = m.ctx.repo.save(Session(name="Sam's meeting", owner_id=sam, summary="x"))
    assert _auto_export(m.ctx, s.id) is not None
    assert "Sam's meeting" in _vault_text(root)


# ---- the digest ---------------------------------------------------------------------------


def _fake(m) -> FakeProvider:
    provider = FakeProvider(summary="## Week\n- things")
    m.ctx.summarizer.providers = {"fake": provider}
    m.ctx.settings.default_provider = "fake"
    return provider


def _prompts(provider: FakeProvider) -> str:
    return "\n".join(c["transcript"] for c in provider.calls)


def test_only_the_owners_digest_is_written_to_the_vault_and_it_covers_only_his_own(vault_host):
    m = vault_host
    provider = _fake(m)
    _grant_team_to_owner(m)
    m.ctx.settings.shared_in_digest = True
    job = _digest(m, "window")
    assert job["status"] == "completed", job.get("error")
    seen = _found(_prompts(provider))
    assert seen == THE_OWNERS | SHARED_WITH_H  # a member's meeting granted to him is not in it
    assert job["result"]["path"]
    files = list((m.vault / "meetings" / "digests").glob("*.md"))
    assert len(files) == 1 and files[0].exists()
    assert marker("team") not in files[0].read_text()
    assert marker("teamgrant") not in files[0].read_text()


def test_a_members_digest_stays_out_of_the_vault(vault_host):
    m = vault_host
    provider = _fake(m)
    job = _digest(m, "member")
    assert job["status"] == "completed", job.get("error")
    assert _found(_prompts(provider)) == {"team", "teamgrant", "everyone"}  # a member's view
    assert not job["result"]["path"]
    assert not (m.vault / "meetings" / "digests").exists()


def test_a_dedicated_server_writes_no_digest_to_the_vault(make_machine, tmp_path):  # noqa: F811
    m = make_machine("dedicated")
    root = tmp_path / "server-vault"
    root.mkdir()
    m.ctx.settings.obsidian_vault_path = str(root)
    provider = _fake(m)

    weekly = m.ctx.repo.save(Session(name="Weekly", summary="Talked.", created_at=THURSDAY))
    m.ctx.repo.set_shares(weekly.id, {"*"}, "test")  # everyone's, so a member's digest has it too
    for caller in ("api_token", "owner_team", "member"):
        job = _digest(m, caller)
        assert job["status"] == "completed", job.get("error")
        assert not job["result"]["path"], caller
    assert "Weekly" in _prompts(provider)  # still made and kept in the database
    assert not list(root.rglob("*"))


def test_a_plain_computer_still_writes_its_digest(make_machine, tmp_path):  # noqa: F811
    m = make_machine("plain")
    root = tmp_path / "vault"
    root.mkdir()
    m.ctx.settings.obsidian_vault_path = str(root)
    _fake(m)
    m.ctx.repo.save(Session(name="Weekly", summary="Talked.", created_at=THURSDAY))
    job = _digest(m, "api_token")
    assert job["result"]["path"] and Path(job["result"]["path"]).exists()


def test_the_owners_scheduled_digest_ignores_members_meetings(vault_host, monkeypatch):
    """Only members' meetings in the week: the owner has nothing to digest, a member does.
    The member's meeting is granted to the owner, and still not his digest's."""
    m = vault_host
    _grant_team_to_owner(m)
    ctx = m.ctx
    ctx.settings.digest_weekday = 6
    ctx.settings.digest_hour = 17
    own = {m.ids[k] for k in ("own", "stray", "granted", "everyone", "contact", "contactstar")}
    with ctx.repo._lock, ctx.repo._conn:  # move the owner's out of the week
        for sid in own:
            ctx.repo._conn.execute(
                "UPDATE sessions SET created_at=? WHERE id=?",
                (datetime(2026, 9, 1, 10).isoformat(), sid),
            )
    queued = []
    monkeypatch.setattr(
        ctx.jobs, "submit", lambda kind, runner, **_: queued.append(access.user_id())
    )
    ctx.maybe_schedule_digest(datetime(2026, 11, 8, 18))
    owner = ctx.settings.team_owner_id
    assert owner not in queued  # all that is left that week is a member's, not granted to him
    assert m.users["Sam"] in queued


# ---- I11: Ask -----------------------------------------------------------------------------


def _ask(m, caller: str) -> tuple[set[str], dict]:
    provider = _fake(m)
    provider.reply = "Answer [1]."
    job = _ask_job(m, caller)
    assert job["status"] == "completed", job.get("error")
    return _found(_prompts(provider)), job["result"]


def test_ask_by_the_owner_cites_no_member_meeting_that_was_not_granted(vault_host):
    m = vault_host
    seen, _ = _ask(m, "window")
    assert seen == THE_OWNERS, "the owner's Ask drew on a member's meeting"
    _grant_team_to_owner(m)
    m.ctx.settings.shared_in_ask = True
    seen, _ = _ask(m, "window")
    assert seen == THE_OWNERS | SHARED_WITH_H | {"team"}


def test_ask_by_the_api_token_is_the_owners_too(vault_host):
    seen, _ = _ask(vault_host, "api_token")
    assert not seen & MEMBERS


def test_ask_by_others_keeps_their_own_scope(vault_host):
    m = vault_host
    assert _ask(m, "member")[0] == {"team", "teamgrant", "everyone"}
    assert _ask(m, "reviewer")[0] == {"team", "teamgrant", "granted", "everyone"}


def test_the_ask_note_of_an_answer_drawn_on_a_members_meeting_is_refused(vault_host):
    m = vault_host
    _grant_team_to_owner(m)
    _, ask = _ask(m, "window")
    from mnemosyne.models.ask import Ask, Citation

    def cite(n: int, key: str) -> Citation:
        return Citation(
            n=n,
            session_id=m.ids[key],
            session_name=key,
            created_at=ask["created_at"],
            idx=None,
            start=None,
            excerpt="",
        )

    mine, theirs = cite(1, "own"), cite(2, "team")
    client, headers, _ = m.client("window")
    reset = access.current.set(_owner(m))  # asks are the asker's own
    try:
        for citations, status in (([mine], 200), ([mine, theirs], 403)):
            saved = m.ctx.repo.save_ask(Ask(question="q", answer="a [1]", citations=citations))
            r = client.post(f"/api/asks/{saved.id}/obsidian", headers=headers)
            assert r.status_code == status, r.text
    finally:
        access.current.reset(reset)


def test_the_semantic_index_filters_at_query_time(vault_host):
    """One matrix for every caller: the scope of whoever loaded it must not decide the rest."""
    m = vault_host
    index = m.ctx.index
    for sid in m.ids.values():
        index.index_session(sid)
    everything = {h.session_id for h in index.query(f"{COMMON} meeting", k=100)}
    # (what a contact shared is indexed only with shared_in_search)
    assert {v for k, v in m.ids.items() if k not in SHARED_WITH_H} <= everything
    reset = _as(m, "Sam")
    try:
        sam = {h.session_id for h in index.query(f"{COMMON} meeting", k=100)}
    finally:
        access.current.reset(reset)
    assert sam == {m.ids[k] for k in ("team", "teamgrant", "everyone")}
    with access.limited_to(host_scope.view(m.ctx), only_for=m.ctx.settings.team_owner_id):
        reset = access.current.set(_owner(m))
        try:
            owner = {h.session_id for h in index.query(f"{COMMON} meeting", k=100)}
        finally:
            access.current.reset(reset)
    assert not owner & {m.ids[k] for k in MEMBERS}
    # ... and the next caller still gets everything (the matrix was not narrowed).
    assert {h.session_id for h in index.query(f"{COMMON} meeting", k=100)} == everything


# ---- D7: the MCP server ---------------------------------------------------------------------

MCP = {"X-Mnemosyne-Client": "mcp"}


def _listed(m, caller: str, headers_extra: dict | None = None) -> set[str]:
    client, headers, _ = m.client(caller)
    r = client.get("/api/sessions", headers={**headers, **(headers_extra or {})})
    assert r.status_code == 200, r.text
    return {k for k, v in m.ids.items() if v in {s["id"] for s in r.json()}}


def test_mcp_requests_are_held_to_the_owners_view(vault_host):
    m = vault_host
    for caller in ("window", "api_token", "owner_main"):
        assert _listed(m, caller) == set(MEETINGS), caller  # his own client: everything
        assert _listed(m, caller, MCP) == THE_OWNERS | SHARED_WITH_H, caller
    _grant_team_to_owner(m)
    assert _listed(m, "window", MCP) == THE_OWNERS | SHARED_WITH_H | {"team"}


def test_the_mcp_header_changes_nothing_for_the_team(vault_host):
    m = vault_host
    assert _listed(m, "member", MCP) == _listed(m, "member") == {"team", "teamgrant", "everyone"}
    assert _listed(m, "reviewer", MCP) == _listed(m, "reviewer")


def test_the_mcp_header_changes_nothing_on_a_dedicated_server(make_machine):  # noqa: F811
    m = make_machine("dedicated")
    assert m.ctx.settings.team_owner_id == ""
    client, headers, _ = m.client("api_token")
    plain = client.get("/api/sessions", headers=headers)
    held = client.get("/api/sessions", headers={**headers, **MCP})
    assert plain.status_code == held.status_code == 200 and plain.json() == held.json()


def test_the_mcp_servers_backend_says_who_it_is():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=[])

    import asyncio

    backend = Backend(url="http://test", token="t0k", transport=httpx.MockTransport(handler))
    asyncio.run(backend.get("/api/sessions"))
    assert seen[0]["x-mnemosyne-client"] == "mcp" and seen[0]["authorization"] == "Bearer t0k"


def test_the_mcp_ask_runs_in_the_owners_view(vault_host):
    """Over HTTP with the header, in one loop: the job it queues keeps the request's limit."""
    m = vault_host
    provider = _fake(m)
    provider.reply = "Answer [1]."

    async def go() -> dict:
        transport = httpx.ASGITransport(app=m.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=MAIN, headers=MCP) as c:
            r = await c.post(
                "/api/ask",
                json={
                    "question": f"what about {COMMON}",
                    "provider": "fake",
                    "exclude_local_only": True,
                },
            )
            assert r.status_code == 200, r.text
            done = await m.ctx.jobs.wait(r.json()["id"])
        return done.model_dump(mode="json")

    job = asyncio.run(go())
    assert job["status"] == "completed", job.get("error")
    assert _found(_prompts(provider)) == THE_OWNERS
