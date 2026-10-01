"""The client summary style: client facts parsed from the model's reply, placed on the
transcript, merged across parts, exported, and a prompt that keeps to what was said."""

import json

import pytest

from mnemosyne import demo
from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.models.session import ClientFact, Session, SummaryData
from mnemosyne.services.summarization_service import merge_parts
from mnemosyne.summarization.prompts import (
    CLIENT_FACT_KINDS,
    STYLES,
    get_system_prompt,
    parse_summary_response,
    part_payload,
    reduce_system_prompt,
    snap_item_times,
)
from tests.conftest import run_summarize


def test_the_client_prompt_asks_for_facts_as_said():
    p = get_system_prompt(50, style="client")
    assert p.startswith(STYLES["client"])
    assert '"client_facts"' in p
    assert "add no advice" in p
    assert "Client said" in p
    assert all(kind in p for kind in CLIENT_FACT_KINDS)
    # The merge step of long meetings follows the same rules.
    r = reduce_system_prompt("client")
    assert '"client_facts"' in r and "add no advice" in r
    # Other styles are not asked for client facts.
    assert "client_facts" not in get_system_prompt(50, style="meeting")
    assert "client_facts" not in reduce_system_prompt("meeting")


def test_client_facts_are_parsed_from_the_reply():
    raw = json.dumps(
        {
            "summary": "s",
            "client_facts": [
                {"kind": "goal", "text": "Client said they want to retire at 62.", "at": "01:02"},
                {"kind": "Pain Point", "text": "Client said onboarding is slow.", "at": 30},
                {"kind": "next-review", "text": "Sam said March.", "time": "[00:45]"},
                {"kind": "background", "text": "Client said they have ten people."},
                {"kind": "crypto tips", "text": "Something else."},  # unknown kind: other
                {"fact": "Client said they rent.", "kind": None, "at": "soon"},
                "Client said the house is paid off.",  # a bare string: kind other
                {"kind": "goal", "text": "  "},  # no text: dropped
                {"kind": "goal"},  # no text: dropped
                42,  # not a fact: dropped
            ],
        }
    )
    _, data = parse_summary_response(raw, style="client")
    assert data.style == "client"
    assert [(f.kind, f.text, f.at) for f in data.client_facts] == [
        ("goal", "Client said they want to retire at 62.", 62.0),
        ("concern", "Client said onboarding is slow.", 30.0),
        ("next_meeting", "Sam said March.", 45.0),
        ("context", "Client said they have ten people.", None),
        ("other", "Something else.", None),
        ("other", "Client said they rent.", None),
        ("other", "Client said the house is paid off.", None),
    ]


@pytest.mark.parametrize("value", [None, "Client wants to retire", {"kind": "goal"}, 3])
def test_malformed_client_facts_become_an_empty_list(value):
    _, data = parse_summary_response(json.dumps({"summary": "s", "client_facts": value}))
    assert data.client_facts == []


def test_a_reply_without_client_facts_has_none():
    _, data = parse_summary_response('{"summary": "s"}', style="client")
    assert data.client_facts == []
    _, data = parse_summary_response("not json at all", style="client")
    assert data.client_facts == []


def test_fact_times_snap_to_the_line_where_they_were_said():
    data = SummaryData(
        client_facts=[
            ClientFact(kind="goal", text="a", at=61.0),
            ClientFact(kind="concern", text="b", at=5000.0),  # past the end: dropped
            ClientFact(kind="preference", text="c"),
        ]
    )
    snap_item_times(data, starts=[0.0, 10.0, 60.5, 90.0])
    assert [f.at for f in data.client_facts] == [60.5, None, None]


def test_facts_survive_long_meetings_split_in_parts():
    one = SummaryData(client_facts=[ClientFact(kind="goal", text="Retire at 62", at=60.0)])
    two = SummaryData(
        client_facts=[
            ClientFact(kind="goal", text="Retire at 62.", at=700.0),  # a duplicate
            ClientFact(kind="next_meeting", text="March", at=900.0),
        ]
    )
    parts = [
        part_payload(1, "00:00", "10:00", "a", one),
        part_payload(2, "10:00", "20:00", "b", two),
    ]
    assert parts[0]["client_facts"] == [{"kind": "goal", "text": "Retire at 62", "at": "01:00"}]
    assert "client_facts" not in part_payload(1, "00:00", "1:00", "a", SummaryData())
    _, merged = merge_parts(parts, "client")
    assert [(f.kind, f.text, f.at) for f in merged.client_facts] == [
        ("goal", "Retire at 62", 60.0),
        ("next_meeting", "March", 900.0),
    ]


def test_a_client_summary_has_client_facts(client, ctx, fake_provider, transcribed_session):
    fake_provider.summary = json.dumps(
        {
            "summary": "A review.",
            "client_facts": [
                {"kind": "goal", "text": "Client said they want to retire early.", "at": "00:02"},
                {"kind": "next_meeting", "text": "Sam said next spring.", "at": "09:00"},
            ],
        }
    )
    job = run_summarize(client, transcribed_session["id"], {"provider": "fake", "style": "client"})
    assert job["status"] == "completed", job
    assert "add no advice" in fake_provider.calls[-1]["system_prompt"]
    facts = client.get(f"/api/sessions/{transcribed_session['id']}").json()["summary_data"][
        "client_facts"
    ]
    # 00:02 snaps to the line at 1.5 s; 09:00 is past the end of the meeting.
    assert facts == [
        {"kind": "goal", "text": "Client said they want to retire early.", "at": 1.5},
        {"kind": "next_meeting", "text": "Sam said next spring.", "at": None},
    ]


@pytest.mark.anyio
async def test_demo_mode_answers_the_client_style_with_client_facts():
    provider = demo.DemoProvider()
    plain = json.loads(await provider.complete(get_system_prompt(6), "t", "m"))
    assert "client_facts" not in plain and plain == demo.DEMO_SUMMARY
    raw = await provider.complete(get_system_prompt(6, style="client"), "t", "m")
    _, data = parse_summary_response(raw, style="client")
    assert data.decisions == ["Ship the Waku migration in October"]  # the rest is unchanged
    assert {f.kind for f in data.client_facts} >= {"goal", "concern", "next_meeting"}
    assert all(f.at is not None for f in data.client_facts)


def test_exports_have_a_client_facts_section_grouped_by_kind(tmp_path):
    session = Session(
        name="Annual review",
        summary="s",
        summary_data=SummaryData(
            style="client",
            client_facts=[
                ClientFact(kind="next_meeting", text="Sam said March.", at=125.0),
                ClientFact(kind="goal", text="Client said retire at 62."),
                ClientFact(kind="goal", text="Client said pay for college.", at=5.0),
            ],
        ),
    )
    note = ObsidianExporter(str(tmp_path)).render(session)
    section = note.split("## Client Facts\n", 1)[1]
    assert section.index("### Goals") < section.index("### Next meeting")
    assert "- Client said retire at 62.\n" in section
    assert "- Client said pay for college. (00:05)" in section
    assert "- Sam said March. (02:05)" in section
    # No facts, no section.
    assert "Client Facts" not in ObsidianExporter(str(tmp_path)).render(
        Session(name="m", summary="s", summary_data=SummaryData())
    )


def test_markdown_export_has_client_facts(client, ctx):
    sid = client.post("/api/sessions", json={"name": "Review"}).json()["id"]
    data = SummaryData(client_facts=[ClientFact(kind="context", text="Client has a will.")])
    ctx.sessions.set_summary(sid, "s", data)
    md = client.get(f"/api/sessions/{sid}/export/markdown").json()["markdown"]
    assert "## Client Facts" in md and "### Context" in md and "- Client has a will." in md
