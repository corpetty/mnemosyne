from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import sharing
from mnemosyne.services.tasks import mine_matcher, rename_owner
from tests.test_my_work import _meeting, team  # noqa: F401


def test_me_myself_and_i_are_the_asker_and_compound_owners_are_split():
    mine = mine_matcher(["Corey Petty"], ["Jonny Zerah"])
    for owner in ("Me", "myself", "I", "Jonny Zerah and Me", "Ann, Corey", "Ann & me", "Bob/I"):
        assert mine(owner), owner
    for owner in ("Jonny Zerah", "Ann and Bob", "you", "Speaker 12", "Mei", None, ""):
        assert not mine(owner), owner
    assert not mine("Me", me=False) and not mine("Ann and Me", me=False)
    assert mine("Corey Petty and Me", me=False)


def test_rename_owner_handles_exact_and_compound_owners():
    assert rename_owner("Speaker 12", "speaker 12", "Dana") == "Dana"
    assert rename_owner("Jonny and Speaker 12", "Speaker 12", "Dana") == "Jonny and Dana"
    assert rename_owner("Speaker 1", "Speaker 12", "Dana") == "Speaker 1"
    assert rename_owner(None, "Speaker 12", "Dana") is None


def test_renaming_a_speaker_renames_item_owners(client, ctx):
    s = ctx.repo.save(
        Session(
            name="m",
            transcript=[TranscriptSegment(start=0, end=1, text="hi", speaker="Speaker 12")],
            summary="s",
            summary_data=SummaryData(
                action_items=[
                    ActionItem(text="a", owner="Speaker 12"),
                    ActionItem(text="b", owner="Ann and Speaker 12"),
                    ActionItem(text="c", owner="Ann"),
                ]
            ),
        )
    )
    r = client.post(
        f"/api/sessions/{s.id}/speakers/rename",
        json={"label": "Speaker 12", "name": "Dana", "enroll": False},
    )
    assert r.status_code == 200
    owners = [a.owner for a in ctx.repo.get(s.id).summary_data.action_items]
    assert owners == ["Dana", "Ann and Dana", "Ann"]


def test_desktop_me_is_mine(client, ctx):
    ctx.settings.local_speaker_name = "Corey Petty"
    ctx.repo.save(
        Session(
            name="m",
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(text="a", owner="Me"), ActionItem(text="b", owner="Ann")]
            ),
        )
    )
    got = client.get("/api/action-items", params={"mine": True}).json()
    assert [t["text"] for t in got] == ["a"]


def test_on_a_team_server_me_is_only_mine_in_my_own_meetings(team):  # noqa: F811
    client, ctx, people = team
    ann, bob = people["Ann"], people["Bob"]
    _meeting(ctx, ann[0], "Ann's", ["Me"])
    theirs = _meeting(ctx, bob[0], "Bob's", ["Me"])
    sharing.set_shares(ctx, theirs.id, {ann[0].id})
    got = client.get("/api/action-items", headers=ann[1]).json()
    assert {t["session_name"]: t["mine"] for t in got} == {"Ann's": True, "Bob's": False}
