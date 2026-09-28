"""Seed a data folder with a few realistic meetings, for the landing page's screenshots.

Run by site/shots/start.sh (MNEMOSYNE_DATA_DIR and MNEMOSYNE_CONFIG_FILE point at a throwaway
folder). The people and the company are made up.
"""

import subprocess
from datetime import date, datetime, timedelta

from mnemosyne.api.context import AppContext
from mnemosyne.config import load_settings
from mnemosyne.models.session import (
    ActionItem,
    Chapter,
    CopilotItem,
    CopilotNotes,
    Recording,
    Session,
    SessionStatus,
    SummaryData,
)
from mnemosyne.models.transcript import TranscriptSegment

NOW = datetime.now().replace(second=0, microsecond=0)


def t(mmss: str) -> float:
    m, s = mmss.split(":")
    return int(m) * 60 + int(s)


def lines(rows):
    out = []
    for i, (at, who, text) in enumerate(rows):
        start = t(at)
        end = t(rows[i + 1][0]) - 0.4 if i + 1 < len(rows) else start + 6
        out.append(TranscriptSegment(text=text, speaker=who, start=start, end=max(end, start + 2)))
    return out


def audio(ctx, sid: str, seconds: float) -> tuple[str, list[Recording]]:
    folder = ctx.settings.recordings_dir / sid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "meeting_mixed.ogg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"anoisesrc=d={seconds}:c=pink:a=0.02", "-ac", "1", "-c:a", "libopus", "-b:a", "16k",
         str(path)],
        check=True,
    )  # fmt: skip
    return str(path), [
        Recording(source="system", device_id=1, device_name="Speakers", path=str(path))
    ]


LAUNCH = [
    ("0:04", "Ana Lima", "Okay, we're recording. Goal for today: lock the plan for the Atlas 2.0 launch."),
    ("0:12", "Ana Lima", "Daniel, can you start with where sync stands?"),
    ("0:17", "Daniel Okafor", "Sync is feature complete. Conflict resolution passed the soak test over the weekend: forty thousand edits, zero lost writes."),
    ("0:31", "Daniel Okafor", "The one thing I'm not happy with is first sync on large workspaces. It's taking about ninety seconds for ten thousand documents."),
    ("0:45", "Me", "What does it look like with the batching change?"),
    ("0:49", "Daniel Okafor", "Around twenty seconds. I'd like that in before beta, it's two days of work."),
    ("1:02", "Ana Lima", "Let's do it. Twenty seconds is a first impression we can live with; ninety isn't."),
    ("1:10", "Priya Raman", "Can we show progress while it runs? Even at twenty seconds people will think it's stuck."),
    ("1:18", "Daniel Okafor", "Yes, the counts are already there, we just never surfaced them."),
    ("3:40", "Ana Lima", "Next: the beta date. We said October twentieth. Is that still real?"),
    ("3:47", "Daniel Okafor", "It is if we cut offline editing for mobile. That one is at least two more weeks."),
    ("3:58", "Priya Raman", "Mobile users mostly read. The editing research was clear on that."),
    ("4:06", "Ana Lima", "Then offline editing on mobile moves to 2.1. Beta stays on the twentieth."),
    ("4:14", "Me", "I'll tell the two customers who asked for it. Northwind will want a date."),
    ("4:21", "Ana Lima", "Give them early December, not a day."),
    ("9:32", "Priya Raman", "On onboarding: the new empty state tested well. Seven of eight people found import without help."),
    ("9:44", "Priya Raman", "The eighth was looking for it under settings, which, fair."),
    ("9:51", "Ana Lima", "Can we link import from settings too?"),
    ("9:55", "Priya Raman", "Easy. I'll add it and send the final screens by Thursday."),
    ("15:08", "Ana Lima", "Pricing. Finance wants the team plan at twelve a seat."),
    ("15:15", "Me", "Our interviews pointed at ten. Twelve is fine if sync history comes with it."),
    ("15:24", "Daniel Okafor", "History is cheap for us. Thirty days is nothing in storage."),
    ("15:31", "Ana Lima", "So twelve a seat, with thirty days of history on the team plan. I'll take that back to finance."),
    ("15:40", "Priya Raman", "Are we still doing the free tier with three workspaces?"),
    ("15:45", "Ana Lima", "That's the open one. Let's decide it with finance on Monday."),
    ("21:12", "Daniel Okafor", "Docs. The migration guide is half written. Who owns the rest?"),
    ("21:18", "Me", "I'll take it. I have the import edge cases from support anyway."),
    ("21:25", "Ana Lima", "Great. It needs to be live before the beta mail goes out."),
    ("27:02", "Ana Lima", "Recap: batching and progress before beta, mobile offline editing to 2.1, twelve a seat with history, free tier on Monday."),
    ("27:18", "Daniel Okafor", "And the beta on the twentieth."),
    ("27:21", "Ana Lima", "And the beta on the twentieth. Thanks everyone."),
]


def main() -> None:
    ctx = AppContext.build(load_settings())
    beta = date.today() + timedelta(days=22)

    # -- the flagship meeting ------------------------------------------------------------
    launch = Session(
        name="Atlas 2.0 launch plan",
        status=SessionStatus.COMPLETED,
        created_at=NOW - timedelta(hours=2),
        attendees=["Ana Lima", "Daniel Okafor", "Priya Raman"],
        notes="Northwind asked about mobile offline editing again: give them early December.",
        speakers_reviewed=True,
    )
    ctx.repo.save(launch)
    path, recs = audio(ctx, launch.id, t("27:30"))
    ctx.sessions.set_audio(launch.id, path, recs)
    ctx.sessions.set_transcript(launch.id, lines(LAUNCH))
    data = SummaryData(
        title="Atlas 2.0 launch plan",
        style="meeting",
        provider="ollama",
        model="qwen3:32b",
        topics=["Atlas 2.0", "sync performance", "beta", "pricing", "onboarding", "docs"],
        decisions=[
            "Ship the sync batching change and a progress indicator before the beta",
            "Offline editing on mobile moves to 2.1; the beta stays on October 20",
            "Team plan at $12 a seat, with 30 days of sync history",
        ],
        decision_at=[t("1:02"), t("4:06"), t("15:31")],
        action_items=[
            ActionItem(text="Land sync batching and surface progress counts", owner="Daniel Okafor", at=t("0:49"), due=beta - timedelta(days=5)),
            ActionItem(text="Tell Northwind and Ferro mobile offline editing is planned for early December", owner="Me", at=t("4:14"), done=True),
            ActionItem(text="Link import from settings and send the final onboarding screens", owner="Priya Raman", at=t("9:55"), due=date.today() + timedelta(days=3)),
            ActionItem(text="Bring $12 with history back to finance", owner="Ana Lima", at=t("15:31")),
            ActionItem(text="Finish the migration guide before the beta mail", owner="Me", at=t("21:18"), due=beta - timedelta(days=2)),
        ],
        open_questions=["Does the free tier keep three workspaces? (with finance, Monday)"],
        question_at=[t("15:45")],
        chapters=[
            Chapter(start=t("0:04"), title="Sync status and first-sync speed"),
            Chapter(start=t("3:40"), title="Beta date and mobile scope"),
            Chapter(start=t("9:32"), title="Onboarding test results"),
            Chapter(start=t("15:08"), title="Pricing and the free tier"),
            Chapter(start=t("21:12"), title="Migration docs"),
            Chapter(start=t("27:02"), title="Recap"),
        ],
    )
    summary = (
        "The team locked the plan for the **Atlas 2.0** beta on October 20. Sync is feature "
        "complete and passed its soak test; the remaining work is first-sync speed on large "
        "workspaces, where batching brings ten thousand documents from about 90 s to 20 s. "
        "Offline editing on mobile moves to 2.1 so the date holds. Pricing converges on $12 a "
        "seat with 30 days of history; the free tier is decided with finance on Monday."
    )
    ctx.sessions.set_summary(launch.id, summary, data)
    ctx.repo.update_fields(
        launch.id,
        copilot_notes=CopilotNotes(
            session_id=launch.id,
            summary=["Sync is feature complete; first sync on big workspaces is the risk"],
            decisions=["Batching change goes in before beta"],
            action_items=[CopilotItem(text="Surface sync progress counts", owner="Daniel Okafor")],
            lines=31,
        ),
    )

    # -- more meetings, for the sidebar, tasks and search ----------------------------------
    others = [
        ("Design crit: onboarding v3", 26, "Priya Raman", [
            ("0:05", "Priya Raman", "Three flows today. The import-first empty state is the one I'd ship."),
            ("0:40", "Me", "The checklist version felt like homework."),
            ("1:12", "Priya Raman", "Agreed, checklist is out. Import-first it is."),
        ], ["Go with the import-first empty state"],
           [ActionItem(text="Prototype the import-first flow on mobile", owner="Priya Raman")]),
        ("Northwind: quarterly check-in", 50, "Sam Whitfield", [
            ("0:03", "Sam Whitfield", "Our field teams live on tablets, so offline matters most to us."),
            ("0:55", "Me", "Offline reading ships in the beta; editing follows in 2.1."),
            ("1:30", "Sam Whitfield", "Early December works if we can pilot it first."),
        ], ["Northwind pilots offline editing before general release"],
           [ActionItem(text="Set up the Northwind pilot workspace", owner="Me")]),
        ("1:1 with Daniel", 74, "Daniel Okafor", [
            ("0:02", "Daniel Okafor", "I'd like to own the sync roadmap after 2.0."),
            ("0:48", "Me", "Let's write down what that covers and review it next week."),
        ], [], [ActionItem(text="Draft the sync roadmap scope", owner="Daniel Okafor")]),
        ("Weekly standup", 98, "Ana Lima", [
            ("0:01", "Ana Lima", "Quick one. Blockers?"),
            ("0:10", "Daniel Okafor", "Waiting on the staging database upgrade."),
            ("0:21", "Me", "I'll chase infra this morning."),
        ], [], [ActionItem(text="Chase infra about the staging database upgrade", owner="Me", done=True)]),
    ]
    for name, hours_ago, lead, rows, decisions, items in others:
        s = Session(
            name=name,
            status=SessionStatus.COMPLETED,
            created_at=NOW - timedelta(hours=hours_ago),
            speakers_reviewed=True,
        )
        ctx.repo.save(s)
        ctx.sessions.set_transcript(s.id, lines(rows))
        ctx.sessions.set_summary(
            s.id,
            f"{lead} and the team went through {name.lower()}.",
            SummaryData(title=name, decisions=decisions, action_items=items),
        )
    print("seeded", len(ctx.sessions.list_sessions()), "meetings")


main()
