"""Meeting types: chosen by title, they set summary style and instructions, the Obsidian
folder and local-only (services/meeting_types.py)."""

from mnemosyne.config import MeetingType
from mnemosyne.models.session import Session
from mnemosyne.services.meeting_types import (
    ADVISOR_MEETING_TYPES,
    available_types,
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


def test_the_advisor_pack_only_when_turned_on(settings):
    names = [t.name for t in ADVISOR_MEETING_TYPES]
    assert names == [
        "Discovery meeting",
        "Annual review",
        "Onboarding",
        "Plan presentation",
        "Service call",
    ]
    assert all(t.summary_style == "advisory" and t.instructions for t in ADVISOR_MEETING_TYPES)
    settings.meeting_types = TYPES
    assert available_types(settings) == TYPES
    review = Session(name="x", meeting_type="Annual review")
    assert session_type(settings, review) is None
    settings.advisor_meeting_types = True
    assert [t.name for t in available_types(settings)] == [t.name for t in TYPES] + names
    assert summary_style(settings, review) == "advisory"
    assert "next review" in summary_instructions(settings, review)
    assert settings.meeting_types == TYPES  # never written into the user's own types


def test_advisor_types_are_picked_by_title(client, ctx):
    def named(name: str) -> str:
        return client.post("/api/sessions", json={"name": name}).json()["meeting_type"]

    assert named("Annual review: the Smiths") == ""  # the pack is off
    ctx.settings.advisor_meeting_types = True
    assert named("Annual Review: the Smiths") == "Annual review"
    assert named("Discovery call with J. Doe") == "Discovery meeting"
    assert named("Onboarding - Garcia household") == "Onboarding"
    assert named("Financial plan presentation") == "Plan presentation"
    assert named("Service call: address change") == "Service call"
    sid = client.post("/api/sessions", json={"name": "Coffee"}).json()["id"]
    chosen = client.put(f"/api/sessions/{sid}/meeting-type", json={"name": "Onboarding"})
    assert chosen.status_code == 200 and chosen.json()["meeting_type"] == "Onboarding"
    assert "Service call" in client.get("/api/settings").json()["meeting_type_names"]
    ctx.settings.advisor_meeting_types = False
    assert client.get("/api/settings").json()["meeting_type_names"] == []
    refused = client.put(f"/api/sessions/{sid}/meeting-type", json={"name": "Onboarding"})
    assert refused.status_code == 400


def test_the_users_own_type_wins_over_the_pack(client, ctx):
    first = MeetingType(name="Reviews", match="annual review", instructions="Mine.")
    mine = MeetingType(name="annual REVIEW", match="yearly", summary_style="meeting")
    ctx.settings.meeting_types = [first, mine]
    ctx.settings.advisor_meeting_types = True
    kinds = available_types(ctx.settings)
    assert kinds[:2] == [first, mine]
    assert "Annual review" not in [t.name for t in kinds]  # replaced by "annual REVIEW"
    assert len(kinds) == 2 + len(ADVISOR_MEETING_TYPES) - 1
    # The user's types are matched first, so their words win too.
    s = client.post("/api/sessions", json={"name": "Annual review 2026"}).json()
    assert s["meeting_type"] == "Reviews"
    assert summary_style(ctx.settings, Session(name="x", meeting_type="Annual review")) == "meeting"


def test_the_advisor_setting_is_saved(client):
    got = client.put("/api/settings", json={"advisor_meeting_types": True}).json()
    assert got["values"]["advisor_meeting_types"] is True
    assert "Annual review" in got["meeting_type_names"]
    from mnemosyne.config import load_settings

    assert load_settings().advisor_meeting_types is True
