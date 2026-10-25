"""Pre-meeting brief: matching earlier meetings and collecting what is open."""

from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services.brief import norm_title, people_match, title_match


def test_title_matching():
    assert norm_title("Infra weekly 2026-09-17") == "infra weekly"
    assert norm_title("Infra Weekly — #42") == "infra weekly"
    assert title_match("Infra weekly", "Infra Weekly 09/24")
    assert title_match("Waku release sync", "Waku release-sync")
    assert not title_match("Infra weekly", "Hiring loop debrief")
    assert not title_match("2026-09-24", "2026-09-17")  # nothing but dates
    assert not title_match("Untitled Session", "Untitled Session")
    assert not title_match("Meeting", "meeting 3")


def test_people_matching():
    assert people_match(["Alice", "Bob", "Carol"], ["bob", "alice"])
    assert people_match(["Alice"], ["alice"])  # 1:1 with the same person
    assert not people_match(["Alice", "Bob", "Carol"], ["Alice", "Dan"])
    assert not people_match([], [])


def _seed(ctx):
    old = Session(
        name="Infra weekly",
        created_at=datetime(2026, 9, 10, 15),
        attendees=["Jakub", "Corey"],
        summary="Old infra.",
        summary_data=SummaryData(
            action_items=[
                ActionItem(text="Rotate the keys", owner="Jakub"),
                ActionItem(text="Old done thing", done=True),
            ],
            open_questions=["Stale question?"],
        ),
    )
    last = Session(
        name="Infra weekly 09/17",
        created_at=datetime(2026, 9, 17, 15),
        attendees=["Jakub", "Corey"],
        summary="Costs are up 18%.",
        summary_data=SummaryData(
            action_items=[ActionItem(text="Draft a cost breakdown", owner="Jakub")],
            open_questions=["Can we cut retention?"],
        ),
    )
    people = Session(
        name="Ad hoc costs chat",
        created_at=datetime(2026, 9, 20, 11),
        attendees=["corey", "jakub"],
    )
    other = Session(
        name="Hiring loop",
        created_at=datetime(2026, 9, 19),
        attendees=["Maria"],
        summary="x",
        summary_data=SummaryData(action_items=[ActionItem(text="Schedule final round")]),
    )
    for s in (old, last, people, other):
        ctx.repo.save(s)
    return old, last, people


def test_brief_route(client, ctx):
    old, last, people = _seed(ctx)
    b = client.get(
        "/api/brief", params={"title": "Infra weekly", "attendees": ["Jakub", "Corey"]}
    ).json()
    assert [(m["name"], m["match"]) for m in b["meetings"]] == [
        ("Ad hoc costs chat", "people"),
        ("Infra weekly 09/17", "both"),
        ("Infra weekly", "both"),
    ]
    assert [i["text"] for i in b["open_items"]] == ["Draft a cost breakdown", "Rotate the keys"]
    # Questions and summary come from the most recent meeting that has a summary.
    assert [q["text"] for q in b["open_questions"]] == ["Can we cut retention?"]
    assert b["last_summary"] == "Costs are up 18%."

    only_title = client.get(
        "/api/brief", params={"title": "Infra weekly", "exclude": last.id}
    ).json()
    assert [m["name"] for m in only_title["meetings"]] == ["Infra weekly"]

    none = client.get("/api/brief", params={"title": "Board meeting"}).json()
    assert none == {
        "meetings": [],
        "open_items": [],
        "open_questions": [],
        "last_summary": "",
        "organization": None,
    }


def test_brief_collapses_linked_task_and_hides_one_done_elsewhere(client, ctx):
    def meet(name, day, *items):
        s = Session(
            name=name,
            created_at=datetime(2026, 9, day, 15),
            attendees=["Jakub", "Corey"],
            summary="s",
            summary_data=SummaryData(action_items=list(items)),
        )
        ctx.repo.save(s)

    meet(
        "Infra weekly",
        10,
        ActionItem(id="a1", text="Rotate keys", owner="Jakub", task_id="t1"),
        ActionItem(id="a2", text="Ship the docs", task_id="t2"),
    )
    meet(
        "Infra weekly 09/17",
        17,
        ActionItem(id="b1", text="Rotate all the keys", owner="Jakub", task_id="t1"),
        ActionItem(id="b2", text="Ship the docs", task_id="t2", done=True),
    )
    b = client.get("/api/brief", params={"title": "Infra weekly"}).json()
    assert [(i["text"], i["task_id"], i["session_name"]) for i in b["open_items"]] == [
        ("Rotate all the keys", "t1", "Infra weekly 09/17")
    ]


def _notes_item(item_id, text, owner=None, done=False, **kw):
    from mnemosyne.models.session import NotesItem

    return NotesItem(id=item_id * 16, line=item_id * 64, text=text, owner=owner, done=done, **kw)


def test_the_brief_lists_open_notes_items_like_summary_items(client, ctx):
    old, last, _ = _seed(ctx)
    ctx.repo.set_notes_items(
        last.id,
        [
            _notes_item("a", "Send the deck", "Ada"),
            _notes_item("b", "Already done", "Ada", done=True),
        ],
    )
    b = client.get(
        "/api/brief", params={"title": "Infra weekly", "attendees": ["Jakub", "Corey"]}
    ).json()
    rows = [(i["text"], i["owner"], i["source"], i["idx"], i["item_id"]) for i in b["open_items"]]
    assert rows == [
        ("Draft a cost breakdown", "Jakub", "summary", 0, ""),
        ("Send the deck", "Ada", "notes", -1, "a" * 16),
        ("Rotate the keys", "Jakub", "summary", 0, ""),
    ]
    assert {i["session_name"] for i in b["open_items"][:2]} == {"Infra weekly 09/17"}


def test_a_meeting_with_only_notes_items_has_open_items(client, ctx):
    s = ctx.repo.save(
        Session(name="Infra weekly", created_at=datetime(2026, 9, 10, 15), attendees=["Ann"])
    )
    ctx.repo.set_notes_items(s.id, [_notes_item("c", "Call the vendor", "Ann")])
    b = client.get("/api/brief", params={"title": "Infra weekly"}).json()
    assert [(i["text"], i["source"]) for i in b["open_items"]] == [("Call the vendor", "notes")]


def test_a_linked_notes_item_is_one_row_with_its_task(client, ctx):
    a = ctx.repo.save(
        Session(
            name="Infra weekly",
            created_at=datetime(2026, 9, 10, 15),
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(id="a1", text="Rotate keys", task_id="t1")]
            ),
        )
    )
    b = ctx.repo.save(Session(name="Infra weekly 2", created_at=datetime(2026, 9, 17, 15)))
    ctx.repo.set_notes_items(b.id, [_notes_item("d", "Rotate all keys", task_id="t1")])
    got = client.get("/api/brief", params={"title": "Infra weekly"}).json()["open_items"]
    assert [(i["text"], i["task_id"], i["session_name"], i["source"]) for i in got] == [
        ("Rotate all keys", "t1", "Infra weekly 2", "notes")
    ]
    assert a.id != b.id
