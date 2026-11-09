"""Who reads what on a main computer that hosts a team (docs/plans/2026-11-09-team-hosting.md
2.3, invariants I7, I8, I12; risks R1, R2, R4).

H = the owner. A meeting is PERSONAL when its owner is '' or H, or a contact shared it with H
(`shared_from`); else it is a TEAM meeting (a member's). The owner reads everything; a member
their own and what is granted to them or to everyone; a reviewer every team meeting and the
personal ones granted to them (never a contact's); a contact only its own peer shares, never a
team grant to everyone, on any computer.

The machine and its callers come from test_hosting_access.py (`make_machine`).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from mnemosyne import access
from mnemosyne.api import auth
from mnemosyne.api.routes.settings import PERSONAL_SETTINGS
from mnemosyne.api.websocket import TEAM_EVENTS, visibility
from mnemosyne.models.comment import CommentCreate
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import comments
from mnemosyne.services.shares import make_contribution
from tests.test_hosting_access import (  # noqa: F401 - make_machine is a fixture
    CONTACT_PERSON,
    Machine,
    make_machine,
)

COMMON = "zanzibar"  # in every meeting: a search for it finds whatever the caller may read

# Meetings of the host, by key. Owner "H" = the host's owner, "Sam" = a member, "" = nobody.
# (owner, granted to, shared_from)
MEETINGS = {
    "own": ("H", set(), ""),
    "stray": ("", set(), ""),  # unowned (from an older peer before adoption)
    "granted": ("H", {"Rae"}, ""),  # personal, shared with the reviewer
    "everyone": ("H", {"*"}, ""),  # personal, shared with the whole team
    "team": ("Sam", set(), ""),  # a member's meeting
    "teamgrant": ("Ann", {"Sam"}, ""),  # another member's, shared with Sam
    "contact": ("", set(), CONTACT_PERSON),  # a contact shared it with H
    "contactstar": ("", {"*"}, CONTACT_PERSON),  # ... and H shared it on with everyone
}
EVERY = set(MEETINGS)
# Who reads which, on a computer that hosts a team.
SEES = {
    "window": EVERY,
    "api_token": EVERY,
    "device": EVERY,
    "owner_main": EVERY,
    "owner_team": EVERY,  # the owner's browser on the team listener reads as the admin
    "member": {"team", "teamgrant", "everyone"},
    "reviewer": {"team", "teamgrant", "granted", "everyone"},
    "other_admin": {"team", "teamgrant", "everyone"},  # I2 forbids one; if left over: no more
}


def marker(key: str) -> str:
    """A word only this meeting has (in its name, transcript, speaker, topic, action item)."""
    return f"qx{key}qx"


def _found(text: str) -> set[str]:
    return {k for k in MEETINGS if marker(k) in text}


@pytest.fixture
def host(make_machine):  # noqa: F811
    """A computer that hosts a team, with every kind of meeting (MEETINGS) and its ids."""
    m: Machine = make_machine("hosting")
    m.ctx.follow_hosting()
    users = {u.name: u.id for u in m.ctx.users.list()}
    users["Ann"] = m.ctx.users.add("Ann", "", "member").id
    owner = m.ctx.settings.team_owner_id
    ids = {}
    for key, (who, granted, shared_from) in MEETINGS.items():
        ids[key] = _meeting(m, key, owner if who == "H" else users.get(who, ""))
        if granted:
            m.ctx.repo.set_shares(ids[key], {users.get(g, g) for g in granted}, "test")
        if shared_from:
            sql = "UPDATE sessions SET shared_from=?, owner_id='' WHERE id=?"
            _raw(m, sql, shared_from, ids[key])
    _raw(m, "UPDATE sessions SET owner_id='' WHERE id=?", ids["stray"])
    m.ids = ids
    m.users = users
    return m


def _raw(m: Machine, sql: str, *params) -> None:
    repo = m.ctx.repo
    with repo._lock, repo._conn:
        repo._conn.execute(sql, params)


def _meeting(m: Machine, key: str, owner_id: str) -> str:
    word = marker(key)
    s = Session(
        name=f"Meeting {word}",
        owner_id=owner_id,
        created_at=datetime(2026, 11, 2, 10, 0),
        summary=f"About {word}.",
        participants=[f"Speaker{word}"],
        transcript=[
            TranscriptSegment(
                text=f"We talk about {COMMON} and {word}.",
                speaker=f"Speaker{word}",
                start=0,
                end=4,
            )
        ],
        summary_data=SummaryData(
            topics=[f"topic {word}"],
            decisions=[f"decided {word}"],
            action_items=[ActionItem(text=f"do {word}")],
        ),
    )
    sid = m.ctx.repo.save(s).id
    comments.create(m.ctx, sid, CommentCreate(text=f"{COMMON} comment {word}"))
    m.ctx.repo.set_flags(
        sid,
        [{"idx": 0, "start": 0.0, "speaker": "x", "phrase": COMMON, "text": word}],
        replace=True,
    )
    return sid


def _get(m: Machine, caller: str, path: str, **params):
    client, headers, _ = m.client(caller)
    return client.get(path, headers=headers, params=params)


# ---- I7: listings -----------------------------------------------------------------------------

LISTINGS = [
    ("/api/sessions", {}),
    ("/api/search", {"q": COMMON}),
    ("/api/action-items", {}),
    ("/api/tasks", {}),
    ("/api/decisions", {}),
    ("/api/people", {}),
    ("/api/topics", {}),
    ("/api/brief", {"title": "x"}),
    ("/api/publications", {}),
]
# Listings that leave some readable meetings out on purpose: tasks never take a contact's
# meeting (task_links), decisions list only linked ones, nothing is published, no brief here.
LEFT_OUT = {"/api/tasks": {"contact", "contactstar"}}
AT_MOST = {"/api/decisions", "/api/publications", "/api/brief"}


@pytest.mark.parametrize("caller", sorted(SEES))
@pytest.mark.parametrize(("path", "params"), LISTINGS, ids=[p for p, _ in LISTINGS])
def test_every_listing_follows_the_read_rule(host, caller, path, params):
    r = _get(host, caller, path, **params)
    assert r.status_code == 200, r.text
    found = _found(r.text)
    if path in AT_MOST:  # no leak, whatever it lists
        assert found <= SEES[caller]
    else:
        assert found == SEES[caller] - LEFT_OUT.get(path, set()), path


@pytest.mark.parametrize("caller", ["reviewer", "other_admin", "window", "owner_team"])
def test_the_review_queue_follows_the_read_rule(host, caller):
    r = _get(host, caller, "/api/supervision")
    assert r.status_code == 200, r.text
    # Phrase review is for the members' meetings only (D5, test_hosting_records.py): what
    # the caller reads, less the owner's own and a contact's.
    assert {k for k, sid in host.ids.items() if sid in r.text} == SEES[caller] & {
        "team",
        "teamgrant",
    }


def test_a_member_has_no_review_queue(host):
    assert _get(host, "member", "/api/supervision").status_code == 403


PER_MEETING = [
    "/api/sessions/{id}",
    "/api/sessions/{id}/stats",
    "/api/sessions/{id}/comments",
    "/api/sessions/{id}/history",
    "/api/sessions/{id}/links",
    "/api/sessions/{id}/supervision",
    "/api/sessions/{id}/records",
    "/api/sessions/{id}/versions",
]


@pytest.mark.parametrize("caller", ["member", "reviewer", "other_admin", "owner_team"])
@pytest.mark.parametrize("path", PER_MEETING)
def test_a_meeting_one_may_not_read_is_not_there(host, caller, path):
    for key, sid in host.ids.items():
        r = _get(host, caller, path.format(id=sid))
        if caller == "member" and path.endswith("/supervision"):
            assert r.status_code == 403  # reviewers' only, alike for every id
        elif key in SEES[caller]:
            # Readable: never hidden (a reviewer-only route may still say 403 to a member).
            assert r.status_code in (200, 403), (key, r.text)
        else:
            assert r.status_code == 404, (key, path, r.status_code)


def test_writes_hide_what_one_may_not_read_and_refuse_what_one_only_reads(host):
    client, headers, _ = host.client("reviewer")
    for key, sid in host.ids.items():
        r = client.patch(f"/api/sessions/{sid}", headers=headers, json={"name": "Renamed"})
        assert r.status_code == (403 if key in SEES["reviewer"] else 404), key
    # The repository rule itself: the same for every write path.
    rae = host.ctx.users.get(host.users["Rae"])
    reset = access.current.set(access.Principal(rae.id, rae.name, rae.role))
    try:
        with pytest.raises(access.Hidden):
            host.ctx.repo._check_write(host.ids["own"])
        with pytest.raises(access.Forbidden) as err:
            host.ctx.repo._check_write(host.ids["team"])
        assert not isinstance(err.value, access.Hidden)
    finally:
        access.current.reset(reset)


def test_the_scope_of_each_caller(host):
    repo, owner = host.ctx.repo, host.ctx.settings.team_owner_id
    assert repo.host_owner == owner
    for name, kind in (
        ("Corey", access.ALL),
        ("Sam", access.OWN_AND_GRANTED),
        ("Rae", access.TEAM_WIDE),
        ("Alex", access.TEAM_WIDE),
    ):
        u = host.ctx.users.get(host.users[name])
        reset = access.current.set(access.Principal(u.id, u.name, u.role))
        try:
            assert repo.read_scope().kind == kind, name
        finally:
            access.current.reset(reset)
    reset = access.current.set(access.Principal("p:" + CONTACT_PERSON[:16], "Ada", access.MEMBER))
    try:
        assert repo.read_scope().kind == access.PERSONAL_SHARES
    finally:
        access.current.reset(reset)
    assert repo.read_scope().everything  # unrestricted


def test_off_a_host_a_reviewer_reads_everything_as_before(make_machine):  # noqa: F811
    m = make_machine("dedicated")
    m.ctx.follow_hosting()
    sid = _meeting(m, "own", "")
    r = _get(m, "reviewer", "/api/sessions")
    assert sid in r.text


def test_the_deletion_log_follows_the_read_rule(host):
    client, headers, _ = host.client("window")
    for key in ("own", "team", "granted"):
        assert client.delete(f"/api/sessions/{host.ids[key]}", headers=headers).status_code == 200
    names = lambda caller: _found(_get(host, caller, "/api/records/deletions").text)  # noqa: E731
    assert names("window") == {"own", "team", "granted"}
    # Grants go with the meeting: a reviewer is left with the team meeting only.
    assert names("reviewer") == {"team"}


def test_active_recordings_follow_the_read_rule(host, monkeypatch):
    from mnemosyne.api.routes import audio

    monkeypatch.setattr(audio, "recording_clock", lambda ctx, sid: None)

    class Rec:
        is_recording = True
        started_at = 0.0
        processes = []
        part = 1
        problems = {}

    host.ctx.active_recordings[host.ids["own"]] = Rec()
    host.ctx.active_recordings[host.ids["team"]] = Rec()
    try:
        seen = lambda caller: {  # noqa: E731
            r["session_id"] for r in _get(host, caller, "/api/audio/active").json()
        }
        assert seen("window") == {host.ids["own"], host.ids["team"]}
        assert seen("reviewer") == {host.ids["team"]}
        st = _get(host, "reviewer", f"/api/audio/status/{host.ids['own']}").json()
        assert st["exists"] is False
        client, headers, _ = host.client("reviewer")
        r = client.post(f"/api/audio/stop/{host.ids['own']}", headers=headers, json={})
        assert r.status_code == 404
    finally:
        host.ctx.active_recordings.clear()


def test_a_reviewer_reading_a_recording_cannot_stop_or_restart_it(host):
    class Rec:
        is_recording = True
        started_at = 0.0
        processes = []
        part = 1
        problems = {}

    sid = host.ids["team"]  # a member's meeting, which a reviewer reads
    host.ctx.active_recordings[sid] = Rec()
    try:
        client, headers, _ = host.client("reviewer")
        assert client.get(f"/api/audio/status/{sid}", headers=headers).json()["exists"] is True
        for path in (f"/api/audio/stop/{sid}", f"/api/audio/restart/{sid}"):
            assert client.post(path, headers=headers, json={}).status_code == 403, path
        assert host.ctx.active_recordings[sid].is_recording
    finally:
        host.ctx.active_recordings.clear()


# ---- contacts: I8 -----------------------------------------------------------------------------


def _as_contact():
    return access.current.set(access.Principal("p:" + CONTACT_PERSON[:16], "Ada", access.MEMBER))


@pytest.mark.parametrize("kind", ["plain", "hosting"])
def test_a_contact_reads_only_its_peer_shares_never_a_grant_to_everyone(make_machine, kind):  # noqa: F811
    m = make_machine(kind)
    m.ctx.follow_hosting()
    repo = m.ctx.repo
    mine = _meeting(m, "own", "")
    everyone = _meeting(m, "everyone", "")
    repo.set_shares(everyone, {"*"}, "test")
    repo.put_peer_share(mine, CONTACT_PERSON, {}, "Corey")
    reset = _as_contact()
    try:
        assert repo.visible_ids() == {mine}
        assert [s.id for s in repo.list_summaries()] == [mine]
        assert repo.get(everyone) is None and not repo.exists(everyone)
        with pytest.raises(access.Hidden):
            repo._check_write(everyone)
        assert not access.can_edit_notes("", {"*"})
    finally:
        access.current.reset(reset)


def test_a_contact_cannot_contribute_to_a_meeting_shared_with_everyone(ctx, client, tmp_path):
    """On a plain computer (the hub is closed in team mode until people sharing comes to hosts):
    a meeting the team shares with everyone (a grant left from team mode) is not the contact's."""
    from tests.test_share_contributions import create, results, send
    from tests.test_share_hub import EP_ADA, Contact

    ctx.settings.api_token = "s3cret"
    ada = Contact(ctx, tmp_path, "Ada", EP_ADA)
    sid = ctx.repo.save(Session(name="Team call", notes="", summary="x")).id
    ctx.repo.set_shares(sid, {"*"}, "test")
    rec = create(ada, sid)
    assert results(send(client, ada, [rec])) == [(False, "This meeting is not shared with you")]
    pulled = client.get("/api/share/changes", headers=ada.headers).json()["sessions"]
    assert pulled == []
    # A record about it, signed: refused before anything is applied.
    flag = make_contribution(ada.keys, "item_flag", sid, {"item_id": "x", "done": True})
    assert results(send(client, ada, [flag]))[0][0] is False


# ---- I12: the event stream --------------------------------------------------------------------

# Every session-less bus event type (grep `bus.publish` / `emit` for dicts without a session id;
# the test below checks this list against the code) and whether the team hears it on a host.
SESSIONLESS = {
    "hello": True,  # the connection's own snapshot, filtered item by item
    "pong": True,
    "shares": True,  # a team share changed: reload (no id, no names)
    "meeting_app": False,  # an app with a call started on the owner's desktop
    "sync": False,  # library sync progress with the owner's computers
    "share_pull": False,  # pulls from the owner's contacts
}


def _state(caller: str, user_id: str = "") -> dict:
    """The middleware's state for a caller (api/auth.py), as test_hosting_access checks it."""
    return {
        "window": {"channel": auth.DESKTOP_WINDOW, "loopback_main": True, "on_team_port": False},
        "api_token": {"channel": auth.API_TOKEN, "loopback_main": True, "on_team_port": False},
        "device": {"channel": auth.DEVICE, "loopback_main": False, "on_team_port": False},
        "owner_main": {
            "channel": auth.USER,
            "user_id": user_id,
            "loopback_main": True,
            "on_team_port": False,
        },
        "owner_team": {
            "channel": auth.USER,
            "user_id": user_id,
            "loopback_main": False,
            "on_team_port": True,
        },
        "member": {"channel": auth.USER, "user_id": user_id, "on_team_port": True},
        "reviewer": {"channel": auth.USER, "user_id": user_id, "on_team_port": True},
    }[caller]


WHO = {"owner_main": "Corey", "owner_team": "Corey", "member": "Sam", "reviewer": "Rae"}


def _hears(m: Machine, caller: str, event: dict) -> bool:
    name = WHO.get(caller)
    user = m.ctx.users.get(m.users[name]) if name else None
    reset = access.current.set(access.Principal(user.id, user.name, user.role) if user else None)
    try:
        return visibility(m.ctx, _state(caller, user.id if user else ""))(event)
    finally:
        access.current.reset(reset)


OWNER_CHANNEL_CALLERS = ("window", "api_token", "device", "owner_main")
TEAM_CALLERS = ("owner_team", "member", "reviewer")


@pytest.mark.parametrize("caller", OWNER_CHANNEL_CALLERS + TEAM_CALLERS)
@pytest.mark.parametrize("kind", sorted(SESSIONLESS))
def test_session_less_events_reach_the_team_only_when_allowed(host, caller, kind):
    hears = _hears(host, caller, {"type": kind})
    assert hears == (caller in OWNER_CHANNEL_CALLERS or SESSIONLESS[kind])


def test_an_unknown_session_less_event_stays_with_the_owner(host):
    for caller in TEAM_CALLERS:
        assert not _hears(host, caller, {"type": "something_new"}), caller
        assert not _hears(host, caller, {"type": "levels", "session_id": ""}), caller
    assert _hears(host, "window", {"type": "something_new"})


@pytest.mark.parametrize("caller", ["member", "reviewer", "owner_team", "window"])
def test_meeting_events_follow_the_read_rule(host, caller):
    seen = {
        key
        for key, sid in host.ids.items()
        if _hears(host, caller, {"type": "live_segment", "session_id": sid})
    }
    assert seen == SEES[caller]


def test_job_events_keep_their_own_rule(host):
    rae = host.users["Rae"]
    nobodys = {"type": "job", "job": {"owner_id": "", "session_id": None}}
    assert _hears(host, "reviewer", {"type": "job", "job": {"owner_id": rae, "session_id": None}})
    assert not _hears(host, "reviewer", nobodys)
    job = {"owner_id": "", "session_id": host.ids["own"]}
    assert not _hears(host, "reviewer", {"type": "job", "job": job})
    job = {"owner_id": "", "session_id": host.ids["team"]}
    assert _hears(host, "reviewer", {"type": "job", "job": job})
    assert _hears(host, "window", nobodys)


def test_off_a_host_session_less_events_reach_everyone_as_before(make_machine):  # noqa: F811
    m = make_machine("dedicated")
    m.users = {u.name: u.id for u in m.ctx.users.list()}
    for caller in TEAM_CALLERS:
        assert _hears(m, caller, {"type": "sync"}), caller


def test_over_a_real_socket_the_team_hears_only_what_it_may(host):
    """The connection's own channel reaches the filter: a reviewer's socket gets the allowed
    event and not the owner's, the owner's (the API token: the test client's sockets do not
    come from the desktop window's address) both."""
    events = [
        {"type": "sync", "peer": "x"},
        {"type": "live_segment", "session_id": host.ids["own"]},
        {"type": "live_segment", "session_id": host.ids["team"]},
        {"type": "shares"},
    ]
    for caller, expected in (
        ("reviewer", [("live_segment", host.ids["team"]), ("shares", None)]),
        (
            "api_token",
            [
                ("sync", None),
                ("live_segment", host.ids["own"]),
                ("live_segment", host.ids["team"]),
                ("shares", None),
            ],
        ),
    ):
        client, headers, _ = host.client(caller)
        with client.websocket_connect("/ws", headers=headers) as ws:
            assert ws.receive_json()["type"] == "hello"
            for e in events:
                ws.portal.call(host.ctx.bus.publish, e)
            ws.send_json({"type": "ping"})
            got = []
            while (msg := ws.receive_json())["type"] != "pong":
                got.append((msg["type"], msg.get("session_id")))
        assert got == expected, caller


def test_the_allowlist_names_every_session_less_event_in_the_code():
    """R2: every event type published without a session id is in SESSIONLESS (reviewed here),
    and TEAM_EVENTS lets through exactly the allowed ones. A new one fails this test."""
    import ast

    root = Path(__file__).resolve().parents[1] / "mnemosyne"
    # Sent to a notes editor's own socket (services/shared_notes.py rooms), never on the bus.
    notes_socket = {"caret", "guest_caret", "guests", "join", "last_edit", "presence", "reload"}
    not_events = {"doc", "paragraph", "text"}  # Atlassian documents (services/trackers.py)
    found = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
            if "type" not in keys or "session_id" in keys or None in keys:
                continue
            value = node.values[keys.index("type")]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if not value.value.startswith(("http.", "websocket.")):
                    found.add(value.value)
    found -= notes_socket | not_events
    found.discard("job")  # its own rule (`visible`)
    assert found == set(SESSIONLESS)
    assert {k for k, allowed in SESSIONLESS.items() if allowed} == set(TEAM_EVENTS)


# ---- R4: GET /api/settings --------------------------------------------------------------------


def test_the_team_reads_the_settings_without_the_owners_own(host):
    st = host.ctx.settings
    st.obsidian_vault_path = "/home/corey/vault"
    st.phone_url = "https://corey.example/m"
    st.remote_relays = "https://relay.corey.example"
    st.local_speaker_name = "Corey"
    st.calendar_ics_url = "https://calendar.example/ics"
    st.consent_script = "We record this."
    st.allow_publishing = True
    owner = _get(host, "window", "/api/settings").json()
    assert owner["values"]["obsidian_vault_path"] == "/home/corey/vault"
    assert owner["secrets_set"]["calendar_ics_url"] is True
    for caller in ("member", "reviewer", "owner_team"):
        r = _get(host, caller, "/api/settings")
        assert r.status_code == 200
        body = r.json()
        v = body["values"]
        assert v["obsidian_vault_path"] == "" and v["phone_url"] == "" and v["remote_relays"] == ""
        assert v["local_speaker_name"] == "Me" and v["data_dir"] == ""
        assert body["secrets_set"]["calendar_ics_url"] is False
        assert body["config_file"] == "" and body["env_overrides"] == []
        assert body["obsidian_vault_exists"] is False
        # What the team's web app needs is still there.
        assert v["consent_script"] == "We record this." and v["allow_publishing"] is True
        assert body["meeting_type_names"] == owner["meeting_type_names"]
        assert "corey" not in r.text.lower().replace(host.ctx.settings.team_owner_id.lower(), "")


@pytest.mark.parametrize("kind", ["plain", "dedicated"])
def test_elsewhere_everyone_reads_every_setting_as_before(make_machine, kind):  # noqa: F811
    m = make_machine(kind)
    m.ctx.settings.obsidian_vault_path = "/srv/vault"
    caller = "reviewer" if kind == "dedicated" else "api_token"
    assert _get(m, caller, "/api/settings").json()["values"]["obsidian_vault_path"] == "/srv/vault"


def test_the_personal_settings_are_settings():
    from mnemosyne.config import Settings

    assert PERSONAL_SETTINGS <= set(Settings.model_fields)
    assert {"obsidian_vault_path", "phone_url", "remote_relays", "remote_access"} <= (
        PERSONAL_SETTINGS
    )


# ---- R1: every reader of the read rule is known -----------------------------------------------

# Modules that read through the rule's raw pieces. A new one fails here until it is checked to
# go through the scope (access.read_scope / repo.visible_ids / repo.ids_seen_by), on a host too.
KNOWN_READERS = {
    "access.py",
    "storage/sqlite.py",
    "api/websocket.py",
    "api/routes/ask.py",  # the Ask note: the vault scope's ids
    "api/routes/audio.py",
    "api/routes/export.py",
    "api/routes/integrations.py",
    "api/routes/publishing.py",
    "api/routes/sharing.py",  # its own `_readable` helper: `repo.exists`
    "api/routes/tasks.py",
    "search/index.py",  # filters its shared vectors by the caller's scope at query time
    "services/comments.py",
    "services/decision_links.py",
    "services/item_flags.py",
    "services/notes_items.py",
    "services/task_links.py",
}
RULE = re.compile(r"\b(read_owner|read_scope|visible_ids|ids_seen_by|_sees|_readable|_SEES)\b")


def test_every_module_using_the_read_rule_is_known():
    root = Path(__file__).resolve().parents[1] / "mnemosyne"
    users = {str(p.relative_to(root)) for p in root.rglob("*.py") if RULE.search(p.read_text())}
    assert users == KNOWN_READERS
    # The old single-owner rule is gone: every caller went to the scope.
    assert not any("read_owner" in p.read_text() for p in root.rglob("*.py"))
