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
    assert none == {"meetings": [], "open_items": [], "open_questions": [], "last_summary": ""}
