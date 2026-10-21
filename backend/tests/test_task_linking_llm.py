"""The summary model may say that an action item continues an open task of an earlier related
meeting (`task_linking_llm`, off by default; never the built-in model): the prompt offers the
tasks by label, only offered labels link (services/task_links.py `apply_hints`)."""

import json
from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.summarization_service import merge_parts
from mnemosyne.summarization.prompts import (
    FORMAT_INSTRUCTIONS,
    open_tasks_hint,
    parse_summary_response,
    part_payload,
)
from tests.conftest import run_summarize

# Different wording from the earlier task: only the model can tell it is the same one.
EARLIER = "Ship the migration guide before the freeze"
NEW_WORDS = "Write up how to migrate, due before the freeze"
OTHER = "Order new badges for the booth"
MARKER = "Open tasks from earlier related meetings"


def _earlier(ctx, name="Infra weekly") -> Session:
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, 1, 10),
            summary="s",
            summary_data=SummaryData(
                action_items=[
                    ActionItem(id="a1", text=EARLIER, owner="Ann"),
                    ActionItem(id="a2", text=OTHER, owner="Ann"),
                ]
            ),
        )
    )


def _new(ctx, name="Infra weekly 2") -> Session:
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, 8, 10),
            transcript=[TranscriptSegment(text="the guide", speaker="Ann", start=0, end=2)],
        )
    )


def _reply(*items) -> str:
    return json.dumps(
        {
            "summary": ["Notes."],
            "decisions": [],
            "open_questions": [],
            "action_items": [{"owner": "Ann", **i} for i in items],
        }
    )


def _items(ctx, sid):
    return ctx.repo.get(sid).summary_data.action_items


def _label_of(prompt: str, text: str) -> str:
    (line,) = [x for x in prompt.splitlines() if x.startswith("- T") and text in x]
    return line[2:].split(":")[0]


def test_an_offered_label_links_and_a_bogus_one_does_not(client, ctx, fake_provider):
    ctx.settings.task_linking_llm = True
    a, b = _earlier(ctx), _new(ctx)
    seen: list[str] = []

    async def answer(transcript, model, system_prompt):  # the labels are in the prompt
        seen.append(system_prompt)
        return _reply(
            {"text": NEW_WORDS, "continues": _label_of(system_prompt, EARLIER)},
            {"text": "Print the posters", "continues": "T99"},
            {"text": "Water the plants", "continues": "nonsense"},
        )

    fake_provider.summarize = answer
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    prompt = seen[0]
    assert MARKER in prompt and EARLIER in prompt and OTHER in prompt
    new = {i.text: i for i in _items(ctx, b.id)}
    old = {i.id: i for i in _items(ctx, a.id)}
    assert len(new[NEW_WORDS].task_id) == 12
    assert new[NEW_WORDS].task_id == old["a1"].task_id
    assert new["Print the posters"].task_id == "" and new["Water the plants"].task_id == ""
    assert old["a2"].task_id == ""
    # The hint never reaches the saved summary.
    assert "continues" not in json.dumps(ctx.repo.get(b.id).summary_data.model_dump(mode="json"))


def test_an_offered_task_is_used_once(client, ctx, fake_provider):
    ctx.settings.task_linking_llm = True
    _earlier(ctx)
    b = _new(ctx)

    async def answer(transcript, model, system_prompt):
        label = _label_of(system_prompt, EARLIER)
        return _reply(
            {"text": NEW_WORDS, "continues": label},
            {"text": "Also the guide again", "continues": label},
        )

    fake_provider.summarize = answer
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    linked = [i for i in _items(ctx, b.id) if i.task_id]
    assert [i.text for i in linked] == [NEW_WORDS]


def test_another_owner_is_not_linked(client, ctx, fake_provider):
    ctx.settings.task_linking_llm = True
    _earlier(ctx)
    b = _new(ctx)

    async def answer(transcript, model, system_prompt):
        label = _label_of(system_prompt, EARLIER)
        return _reply({"text": NEW_WORDS, "owner": "Bob", "continues": label})

    fake_provider.summarize = answer
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    assert _items(ctx, b.id)[0].task_id == ""


def test_off_by_default_no_hint_block_and_no_llm_links(client, ctx, fake_provider):
    assert ctx.settings.task_linking_llm is False
    a, b = _earlier(ctx), _new(ctx)
    fake_provider.summary = _reply({"text": NEW_WORDS, "continues": "T1"})
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    assert MARKER not in fake_provider.calls[0]["system_prompt"]
    assert _items(ctx, b.id)[0].task_id == ""
    assert _items(ctx, a.id)[0].task_id == ""


def test_task_linking_off_wins(client, ctx, fake_provider):
    ctx.settings.task_linking_llm = True
    ctx.settings.task_linking = "off"
    _earlier(ctx)
    b = _new(ctx)
    fake_provider.summary = _reply({"text": NEW_WORDS, "continues": "T1"})
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    assert MARKER not in fake_provider.calls[0]["system_prompt"]
    assert _items(ctx, b.id)[0].task_id == ""


def test_the_built_in_model_gets_no_hint_block(client, ctx, fake_provider):
    ctx.settings.task_linking_llm = True
    ctx.summarizer.providers = {"local": fake_provider}
    _earlier(ctx)
    b = _new(ctx)
    fake_provider.summary = _reply({"text": NEW_WORDS, "continues": "T1"})
    job = run_summarize(client, b.id, {"provider": "local", "model": "m"})
    assert job["status"] == "completed"
    assert MARKER not in fake_provider.calls[0]["system_prompt"]
    assert _items(ctx, b.id)[0].task_id == ""


def test_a_done_task_is_not_offered(ctx):
    from mnemosyne.services import task_links

    a = _earlier(ctx)
    task_links.set_done(ctx, f"{a.id}:a1", True)
    b = _new(ctx)
    assert [o.item_id for o in task_links.open_task_offers(ctx, b.id)] == ["a2"]


def test_on_a_team_server_only_what_the_owner_sees_is_offered(ctx):
    from mnemosyne.services import task_links

    ctx.settings.team_mode = True
    a = _earlier(ctx)
    ctx.repo._conn.execute("UPDATE sessions SET owner_id='ann' WHERE id=?", (a.id,))
    b = _new(ctx)
    ctx.repo._conn.execute("UPDATE sessions SET owner_id='bob' WHERE id=?", (b.id,))
    ctx.repo._conn.commit()
    assert task_links.open_task_offers(ctx, b.id) == []
    ctx.repo._conn.execute(
        "INSERT INTO session_shares (session_id, user_id, at) VALUES (?, 'bob', '2026-10-02')",
        (a.id,),
    )
    ctx.repo._conn.commit()
    assert len(task_links.open_task_offers(ctx, b.id)) == 2


def test_unrelated_meetings_are_not_offered(client, ctx, fake_provider):
    ctx.settings.task_linking_llm = True
    _earlier(ctx, name="Board prep")
    b = _new(ctx, name="Garden club")
    fake_provider.summary = _reply({"text": NEW_WORDS})
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    assert MARKER not in fake_provider.calls[0]["system_prompt"]


def test_local_only_meetings_are_not_offered_to_a_cloud_provider(ctx):
    from mnemosyne.services import task_links

    a = _earlier(ctx)
    ctx.repo.update_fields(a.id, local_only=True)
    b = _new(ctx)
    assert len(task_links.open_task_offers(ctx, b.id)) == 2
    assert task_links.open_task_offers(ctx, b.id, skip_local_only=True) == []


def test_offers_are_labelled_newest_first_and_capped(ctx):
    from mnemosyne.services import task_links

    ctx.repo.save(
        Session(
            name="Infra weekly",
            created_at=datetime(2026, 10, 1, 10),
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(id=f"i{n}", text=f"Task number {n}") for n in range(40)]
            ),
        )
    )
    b = _new(ctx)
    offers = task_links.open_task_offers(ctx, b.id)
    assert [o.label for o in offers] == [f"T{n}" for n in range(1, 31)]
    block = open_tasks_hint([(o.label, o.text, o.owner, o.meeting) for o in offers])
    assert block.startswith(MARKER) and '"continues"' in block


def test_parse_keeps_continues_but_does_not_save_it():
    raw = _reply({"text": "Do it", "continues": " T3 "}, {"text": "Plain"})
    _, data = parse_summary_response(raw)
    assert [a._continues for a in data.action_items] == ["T3", ""]
    assert "continues" not in json.dumps(data.model_dump(mode="json"))
    assert "continues" not in data.model_dump()["action_items"][0]
    assert '"continues"' in FORMAT_INSTRUCTIONS


def test_map_reduce_keeps_continues_when_parts_merge():
    def part(n, items):
        data = SummaryData(action_items=items)
        return part_payload(n, "00:00", "10:00", f"Part {n}.", data)

    first = ActionItem(text="Send the migration guide to the team")
    first._continues = "T2"
    second = ActionItem(text="Send the migration guide to the team.")  # the same, repeated
    third = ActionItem(text="Book the room")
    third._continues = "T5"
    fourth = ActionItem(text="Book the room again for Friday please")
    fourth._continues = "T9"  # a repeat of nothing: its own item
    parts = [part(1, [first, third]), part(2, [second, fourth])]
    assert parts[0]["action_items"][0]["continues"] == "T2"
    assert "continues" not in json.dumps(part(3, [ActionItem(text="Plain")]))
    _, merged = merge_parts(parts, "meeting")
    got = {a.text: a._continues for a in merged.action_items}
    assert got["Send the migration guide to the team"] == "T2"
    assert got["Book the room"] == "T5"
    assert got["Book the room again for Friday please"] == "T9"
    assert len(merged.action_items) == 3


def test_a_late_duplicate_hint_is_kept_by_the_merge():
    a = ActionItem(text="Send the migration guide to the team")
    b = ActionItem(text="Send the migration guide to the team")
    b._continues = "T4"
    parts = [
        part_payload(1, "00:00", "05:00", "One.", SummaryData(action_items=[a])),
        part_payload(2, "05:00", "10:00", "Two.", SummaryData(action_items=[b])),
    ]
    _, merged = merge_parts(parts, "meeting")
    assert [x._continues for x in merged.action_items] == ["T4"]
