"""Records rules and flagged phrases on a main computer that hosts a team
(docs/plans/2026-11-09-team-hosting.md D5, invariant I16).

The records period (`records_retention_years`) and phrase review cover the members' meetings.
The owner's own meetings (owner '' or the host) stay outside both: he can delete them and their
audio, nothing flags them and the Review queue does not list them. A legal hold is his explicit
choice and still holds any meeting. A plain computer, a dedicated server and a host with hosting
turned off keep the old behaviour for every meeting.

The machines come from test_hosting_access.py (`make_machine`).
"""

from __future__ import annotations

from datetime import datetime

from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import records, supervision
from tests import test_hosting_access
from tests.test_hosting_access import Machine

make_machine = test_hosting_access.make_machine  # the fixture, shared

PHRASE = "guarantee"
LINE = "I can guarantee the result."


def _meeting(m: Machine, tmp_path, owner_id: str, name: str, *, shared_from: str = "") -> str:
    """A transcribed meeting with audio, created today (inside any records period)."""
    sid = m.ctx.repo.save(
        Session(
            name=name,
            owner_id=owner_id,
            created_at=datetime.now(),
            transcript=[TranscriptSegment(text=LINE, speaker="A", start=0, end=3)],
        )
    ).id
    audio = tmp_path / f"{sid}_mixed.ogg"
    audio.write_bytes(b"OggS" + b"\x01" * 4000)
    m.ctx.sessions.set_audio(sid, str(audio), [])
    if shared_from:
        with m.ctx.repo._lock, m.ctx.repo._conn:
            m.ctx.repo._conn.execute(
                "UPDATE sessions SET shared_from=?, owner_id='' WHERE id=?", (shared_from, sid)
            )
    return sid


def _host(make_machine, tmp_path):
    m = make_machine("hosting")
    m.ctx.follow_hosting()
    m.ctx.settings.records_retention_years = 6
    users = {u.name: u.id for u in m.ctx.users.list()}
    owner = m.ctx.settings.team_owner_id
    m.ids = {
        "own": _meeting(m, tmp_path, owner, "Own"),
        "stray": _meeting(m, tmp_path, "", "Stray"),
        "team": _meeting(m, tmp_path, users["Sam"], "Team"),
        "contact": _meeting(m, tmp_path, "", "Contact", shared_from="ab" * 32),
    }
    m.users = users
    return m


def _delete(m: Machine, caller: str, path: str, **params):
    client, headers, _ = m.client(caller)
    return client.delete(path, headers=headers, params=params)


# ---- the records period -----------------------------------------------------------------------


def test_the_owner_can_delete_his_own_meeting_and_audio_inside_the_period(make_machine, tmp_path):
    m = _host(make_machine, tmp_path)
    assert records.kept_until(m.ctx, m.ctx.repo.get(m.ids["own"])) is None
    r = _delete(m, "window", f"/api/sessions/{m.ids['own']}/audio")
    assert r.status_code == 200, r.text
    assert _delete(m, "window", f"/api/sessions/{m.ids['own']}").status_code == 200
    # Unowned (from an older peer) and a contact's meeting are outside the team too.
    assert _delete(m, "window", f"/api/sessions/{m.ids['stray']}/audio").status_code == 200
    assert _delete(m, "window", f"/api/sessions/{m.ids['stray']}").status_code == 200
    assert _delete(m, "window", f"/api/sessions/{m.ids['contact']}").status_code == 200


def test_a_members_meeting_keeps_the_period(make_machine, tmp_path):
    m = _host(make_machine, tmp_path)
    sid = m.ids["team"]
    assert records.kept_until(m.ctx, m.ctx.repo.get(sid)) is not None
    # The member cannot delete it; the owner (an admin) needs a reason, as on any computer.
    assert _delete(m, "member", f"/api/sessions/{sid}").status_code == 403
    assert _delete(m, "member", f"/api/sessions/{sid}/audio").status_code == 403
    assert _delete(m, "window", f"/api/sessions/{sid}").status_code == 400
    assert _delete(m, "window", f"/api/sessions/{sid}/audio").status_code == 400
    assert _delete(m, "window", f"/api/sessions/{sid}", reason="Asked to").status_code == 200


def test_retention_by_age_spares_members_meetings_only(make_machine, tmp_path):
    m = _host(make_machine, tmp_path)
    protected = records.protected_ids(m.ctx)
    assert m.ids["team"] in protected
    assert not protected & {m.ids["own"], m.ids["stray"], m.ids["contact"]}
    team = m.ctx.repo.get(m.ids["team"])
    own = m.ctx.repo.get(m.ids["own"])
    assert not records.automatic_deletion_allowed(m.ctx, team)
    assert records.automatic_deletion_allowed(m.ctx, own)


def test_a_legal_hold_still_holds_his_own_meeting(make_machine, tmp_path):
    m = _host(make_machine, tmp_path)
    sid = m.ids["own"]
    client, headers, _ = m.client("window")
    r = client.put(f"/api/sessions/{sid}/legal-hold", json={"reason": "Matter 7"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _delete(m, "window", f"/api/sessions/{sid}", reason="x").status_code == 403
    assert _delete(m, "window", f"/api/sessions/{sid}/audio", reason="x").status_code == 403
    assert sid in records.protected_ids(m.ctx)
    assert not records.automatic_deletion_allowed(m.ctx, m.ctx.repo.get(sid))


# ---- flagged phrases --------------------------------------------------------------------------


def test_the_scan_flags_a_members_meeting_and_not_the_owners(make_machine, tmp_path):
    m = _host(make_machine, tmp_path)
    m.ctx.settings.review_phrases = PHRASE
    for key in m.ids:
        supervision.scan(m.ctx, m.ids[key])
    assert m.ctx.repo.flags(m.ids["team"])
    for key in ("own", "stray", "contact"):
        assert m.ctx.repo.flags(m.ids[key]) == [], key
    # The job over every meeting (a change of phrases) leaves them alone too.
    supervision.scan_all(m.ctx)
    assert m.ctx.repo.flags(m.ids["own"]) == []


def test_the_review_queue_lists_only_team_meetings(make_machine, tmp_path):
    m = _host(make_machine, tmp_path)
    m.ctx.settings.review_phrases = PHRASE
    # Flags the owner's meetings got before hosting started (written under the plain rules).
    for key in m.ids:
        m.ctx.repo.set_flags(
            m.ids[key],
            [{"idx": 0, "start": 0.0, "speaker": "A", "phrase": PHRASE, "text": LINE}],
            replace=True,
        )
    for caller in ("window", "reviewer"):
        client, headers, _ = m.client(caller)
        queue = client.get("/api/supervision", headers=headers).json()
        assert [i["session_id"] for i in queue] == [m.ids["team"]], caller
    # The old flags are hidden, not deleted: the per-meeting view is empty, the rows remain.
    client, headers, _ = m.client("window")
    own = client.get(f"/api/sessions/{m.ids['own']}/supervision", headers=headers).json()
    assert own["flags"] == [] and own["reviews"] == []
    assert len(m.ctx.repo.flags(m.ids["own"])) == 1
    # Hosting off: a plain computer, they are listed again.
    m.ctx.settings.team_mode = False
    m.ctx.follow_hosting()
    assert {r["id"] for r in m.ctx.repo.supervision_queue()} == set(m.ids.values())


# ---- computers that do not host --------------------------------------------------------------


def _plain_rules(m: Machine, tmp_path, caller: str, owners: list[str]) -> None:
    m.ctx.follow_hosting()
    m.ctx.settings.records_retention_years = 6
    m.ctx.settings.review_phrases = PHRASE
    for n, owner in enumerate(owners):
        sid = _meeting(m, tmp_path, owner, f"M{n}")
        assert records.kept_until(m.ctx, m.ctx.repo.get(sid)) is not None
        assert sid in records.protected_ids(m.ctx)
        supervision.scan(m.ctx, sid)
        assert m.ctx.repo.flags(sid), owner
        assert _delete(m, caller, f"/api/sessions/{sid}/audio").status_code == 400
        assert _delete(m, caller, f"/api/sessions/{sid}").status_code == 400
    assert len(m.ctx.repo.supervision_queue()) == len(owners)


def test_a_plain_computer_keeps_the_rules_for_every_meeting(make_machine, tmp_path):
    m = make_machine("plain")
    _plain_rules(m, tmp_path, "api_token", [""])


def test_a_dedicated_server_keeps_the_rules_for_every_meeting(make_machine, tmp_path):
    m = make_machine("dedicated")
    owner = {u.name: u.id for u in m.ctx.users.list()}["Sam"]
    _plain_rules(m, tmp_path, "api_token", [owner, ""])


def test_a_host_with_hosting_turned_off_is_plain_again(make_machine, tmp_path):
    m = make_machine("off")
    owner = m.ctx.settings.team_owner_id
    _plain_rules(m, tmp_path, "api_token", [owner, ""])
