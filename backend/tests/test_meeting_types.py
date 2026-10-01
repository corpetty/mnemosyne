"""Meeting types: chosen by title, they set summary style and instructions, the Obsidian
folder and local-only (services/meeting_types.py)."""

from mnemosyne.config import MeetingType
from mnemosyne.models.session import Session
from mnemosyne.services.meeting_types import (
    match_type,
    obsidian_folder,
    session_type,
    summary_instructions,
    summary_style,
)

TYPES = [
    MeetingType(name="Standup", match="standup, daily sync", summary_style="standup"),
    MeetingType(
        name="1:1", match="1:1, one-on-one", instructions="Note feedback given.", local_only=True
    ),
    MeetingType(name="Customer", match="northwind", obsidian_folder="clients"),
]


def test_the_first_type_whose_words_the_title_has():
    assert match_type("Weekly STANDUP", TYPES).name == "Standup"
    assert match_type("Daily sync with infra", TYPES).name == "Standup"
    assert match_type("1:1 with Daniel", TYPES).name == "1:1"
    assert match_type("Roadmap review", TYPES) is None
    assert match_type("anything", [MeetingType(name="Empty")]) is None  # no words: never


def test_a_type_is_given_when_a_meeting_is_named(client, ctx):
    ctx.settings.meeting_types = TYPES
    s = client.post("/api/sessions", json={"name": "1:1 with Daniel"}).json()
    assert s["meeting_type"] == "1:1" and s["local_only"] is True
    untitled = client.post("/api/sessions", json={}).json()
    assert untitled["meeting_type"] == ""
    renamed = client.patch(f"/api/sessions/{untitled['id']}", json={"name": "Northwind QBR"}).json()
    assert renamed["meeting_type"] == "Customer"
    # A type is not changed by a later rename; it can be chosen (or "none") by hand.
    again = client.patch(f"/api/sessions/{untitled['id']}", json={"name": "Daily standup"}).json()
    assert again["meeting_type"] == "Customer"
    chosen = client.put(f"/api/sessions/{untitled['id']}/meeting-type", json={"name": "none"})
    assert chosen.json()["meeting_type"] == "none"
    bad = client.put(f"/api/sessions/{untitled['id']}/meeting-type", json={"name": "Nope"})
    assert bad.status_code == 400


def test_what_a_type_changes(settings):
    settings.meeting_types = TYPES
    settings.summary_instructions = "Be brief."
    one = Session(name="x", meeting_type="1:1")
    assert session_type(settings, one).name == "1:1"
    assert summary_style(settings, one) == "meeting"  # its style is blank: the default
    assert summary_instructions(settings, one) == "Be brief.\nNote feedback given."
    standup = Session(name="x", meeting_type="Standup")
    assert summary_style(settings, standup) == "standup"
    assert obsidian_folder(settings, Session(name="x", meeting_type="Customer")) == "clients"
    assert (
        obsidian_folder(settings, Session(name="x", meeting_type="none"))
        == settings.obsidian_subfolder
    )
    assert session_type(settings, Session(name="x", meeting_type="Gone")) is None


def test_settings_keep_the_types(client):
    types = [t.model_dump() for t in TYPES]
    got = client.put("/api/settings", json={"meeting_types": types}).json()
    assert [t["name"] for t in got["values"]["meeting_types"]] == ["Standup", "1:1", "Customer"]
    from mnemosyne.config import load_settings

    reloaded = load_settings().meeting_types  # written to config.toml as a list of tables
    assert [t.name for t in reloaded] == ["Standup", "1:1", "Customer"] and reloaded[1].local_only
