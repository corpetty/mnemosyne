"""Publishing a meeting (services/publishing.py, api/routes/publishing.py)."""

import hashlib
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.publication import PublicationSelection, QuoteSpan
from mnemosyne.models.session import ActionItem, Chapter, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import clips, publishing
from mnemosyne.services import publication_format as pf

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _meeting(ctx, audio=None, **kw) -> Session:
    lines = [
        ("Ana Lopez", 0.0, 4.0, "We ship the release on Friday."),
        ("Ana Lopez", 4.0, 6.0, "Bo agreed to write the notes."),
        ("Bo Chen", 6.5, 9.0, "My account number is 123456789012, send it there."),
        ("Ana Lopez", 9.5, 12.0, "Great, thanks Bo."),
    ]
    s = Session(
        name="Release sync with Bo Chen",
        created_at=datetime(2026, 10, 20, 10, 0),
        audio_file=audio,
        attendees=["Ana Lopez", "Bo Chen", "Cy Park"],
        participants=["Ana Lopez", "Bo Chen"],
        transcript=[TranscriptSegment(text=t, speaker=sp, start=a, end=b) for sp, a, b, t in lines],
        summary="Ana Lopez and Bo decided to ship Friday. Cy Park was away.",
        notes="Bo's private scratch notes",
        summary_data=SummaryData(
            topics=["Release"],
            decisions=["Ship on Friday (Ana)"],
            decision_ids=["dec1"],
            decision_at=[0.0],
            open_questions=["Does Cy Park know?"],
            question_ids=["que1"],
            action_items=[ActionItem(id="act1", text="Write the notes", owner="Bo Chen")],
            chapters=[Chapter(start=0.0, title="Release with Ana")],
        ),
        **kw,
    )
    return ctx.repo.save(s)


@pytest.fixture
def fake_cut(monkeypatch):
    """cut_clip without ffmpeg: a few bytes per span; counts the cuts."""
    calls = []

    async def cut(session, clips_dir, first, last, file_key=None):
        calls.append((first, last))
        clips_dir.mkdir(parents=True, exist_ok=True)
        out = clips_dir / f"{first}-{last}.ogg"
        out.write_bytes(b"OggS" + f"clip {first}-{last}".encode())
        return out

    monkeypatch.setattr(clips, "cut_clip", cut)
    return calls


@pytest.fixture
def seals(monkeypatch):
    from mnemosyne.services import records

    calls = []
    monkeypatch.setattr(records, "seal_later", lambda app, sid, reason: calls.append((sid, reason)))
    return calls


def _wait(client, job_id, timeout=20, headers=None):
    deadline = time.monotonic() + timeout
    while (job := client.get(f"/api/jobs/{job_id}", headers=headers).json())["status"] not in (
        "completed",
        "failed",
        "cancelled",
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return job


def _publish(client, sid, headers=None, **sel):
    body = {"where": "local", **sel}
    r = client.put(f"/api/sessions/{sid}/publication", json=body, headers=headers)
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["id"], headers=headers)
    assert job["status"] == "completed", job
    return job["result"]["publication"]


def _key_and_id(url):
    pub_id = url.split("/p/")[1].split("#")[0]
    return pub_id, pf.decode_key(url.split("#")[1])


def _open(client, url):
    pub_id, key = _key_and_id(url)
    r = client.get(f"/pub/{pub_id}")
    assert r.status_code == 200, r.text
    return pf.open_manifest(pub_id, int(r.headers["x-pub-version"]), r.content, key)


# ---- the manifest ----


def test_the_manifest_follows_the_selection_and_never_holds_the_transcript(ctx):
    s = _meeting(ctx)
    sel = PublicationSelection(
        summary=False,
        topics=False,
        attendees=["Ana Lopez", "Bo Chen", "Cy Park"],
        hide_identifiers=False,
        quotes=[QuoteSpan(start=0.0, end=6.0)],
    )
    manifest, specs = publishing.build_manifest(ctx, s, sel, "0123456789abcdef", 1)
    assert "segments" not in pf.Manifest.model_fields
    assert "transcript" not in pf.Manifest.model_fields
    assert manifest.summary == "" and manifest.topics == [] and manifest.notes is None
    assert [d.text for d in manifest.decisions] == ["Ship on Friday (Ana)"]
    assert manifest.decisions[0].id == "dec1" and manifest.questions[0].id == "que1"
    assert manifest.action_items[0].owner == "Bo Chen"
    assert manifest.chapters[0].title == "Release with Ana"
    assert manifest.attendees == ["Ana Lopez", "Bo Chen", "Cy Park"]
    assert manifest.title == "Release sync with Bo Chen" and manifest.date == "2026-10-20"
    # The quote is only the lines in its span, one speaker: joined, with that speaker.
    (quote,) = manifest.quotes
    assert quote.text == "We ship the release on Friday. Bo agreed to write the notes."
    assert quote.speaker == "Ana Lopez" and quote.clip is None
    assert specs[0]["first"] == 0 and specs[0]["last"] == 1
    body = pf.manifest_bytes(manifest).decode()
    assert "123456789012" not in body and "Great, thanks" not in body

    notes = publishing.build_manifest(
        ctx, s, PublicationSelection(notes=True, attendees=["Bo Chen"]), "0123456789abcdef", 1
    )[0]
    assert notes.notes == "Bo's private scratch notes"
    # Nobody ticked (the model's default): every name is hidden.
    nobody = publishing.build_manifest(ctx, s, PublicationSelection(notes=True), "0" * 16, 1)[0]
    assert nobody.notes == "Person 2's private scratch notes" and nobody.attendees == []
    # Two speakers, neither dominating: each line keeps its speaker.
    mixed = publishing.build_manifest(
        ctx,
        s,
        PublicationSelection(
            attendees=["Ana Lopez", "Bo Chen"], quotes=[QuoteSpan(start=4, end=12)]
        ),
        "0123456789abcdef",
        1,
    )[0]
    assert mixed.quotes[0].speaker is None
    assert mixed.quotes[0].text.split("\n") == [
        "Ana Lopez: Bo agreed to write the notes.",
        "Bo Chen: My account number is [account ••9012], send it there.",
        "Ana Lopez: Great, thanks Bo.",
    ]


def test_diarizer_labels_are_not_published(ctx):
    s = _meeting(ctx)
    s.transcript = [
        TranscriptSegment(text="First point.", speaker="SPEAKER_01", start=0, end=2),
        TranscriptSegment(text="Second point.", speaker="Bo Chen", start=2.5, end=4),
        TranscriptSegment(text="Third point.", speaker="UNKNOWN", start=4.5, end=6),
    ]
    s.summary = ""
    s.summary_data = SummaryData(
        action_items=[
            ActionItem(id="a1", text="Send it", owner="SPEAKER_01"),
            ActionItem(id="a2", text="Review it", owner="Speaker 2"),
            ActionItem(id="a3", text="Ship it", owner="Bo Chen"),
        ]
    )
    sel = PublicationSelection(
        attendees=["Bo Chen"],
        names="hidden",
        quotes=[QuoteSpan(start=0, end=6), QuoteSpan(start=0, end=2.2)],
    )
    m = publishing.build_manifest(ctx, s, sel, "0" * 16, 1)[0]
    assert [a.owner for a in m.action_items] == [None, None, "Person 2"]
    one, many = m.quotes[1], m.quotes[0]  # a lone unnamed speaker, then a mix
    assert one.speaker is None and one.text == "First point."
    assert many.speaker is None
    assert many.text.split("\n") == ["First point.", "Person 2: Second point.", "Third point."]
    body = pf.manifest_bytes(m).decode()
    assert "SPEAKER_" not in body and "UNKNOWN" not in body


def test_preview_shows_the_version_a_publish_would_make(client, ctx, seals):
    s = _meeting(ctx)
    url = f"/api/sessions/{s.id}/publication/preview"
    assert client.post(url, json={}).json()["version"] == 1
    pub = _publish(client, s.id)
    assert pub["version"] == 1
    assert client.post(url, json={}).json()["version"] == 2


def test_identifiers_and_names(ctx):
    s = _meeting(ctx)
    people = ["Ana Lopez", "Bo Chen", "Cy Park"]
    quote = [QuoteSpan(start=6.5, end=9.0)]
    hidden = publishing.build_manifest(
        ctx, s, PublicationSelection(attendees=people, names="hidden", quotes=quote), "0" * 16, 1
    )[0]
    assert hidden.summary == "Person 1 and Person 2 decided to ship Friday. Person 3 was away."
    assert hidden.attendees == ["Person 1", "Person 2", "Person 3"]
    assert hidden.title == "Release sync with Person 2"
    assert hidden.action_items[0].owner == "Person 2"
    assert hidden.quotes[0].speaker == "Person 2"
    assert hidden.quotes[0].text == "My account number is [account ••9012], send it there."

    first = publishing.build_manifest(
        ctx, s, PublicationSelection(attendees=people, names="first"), "0" * 16, 1
    )[0]
    assert first.summary == "Ana and Bo decided to ship Friday. Cy was away."
    assert first.attendees == ["Ana", "Bo", "Cy"] and first.action_items[0].owner == "Bo"

    # Cy left out of the attendees: gone from the list and from every text.
    some = publishing.build_manifest(
        ctx, s, PublicationSelection(attendees=["Ana Lopez", "Bo Chen"]), "0" * 16, 1
    )[0]
    assert some.attendees == ["Ana Lopez", "Bo Chen"]
    assert some.summary == "Ana Lopez and Bo decided to ship Friday. Person 1 was away."
    assert some.questions[0].text == "Does Person 1 know?"


def test_defaults_and_preview(client, ctx, settings):
    s = _meeting(ctx, audio="/nowhere/mixed.ogg")
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["summary"] and d["decisions"] and not d["notes"] and d["quotes"] == []
    assert d["names"] == "as_is" and d["hide_identifiers"] and d["where"] == "local"
    assert d["expires_days"] == 90 and d["attendees"] == ["Ana Lopez", "Bo Chen", "Cy Park"]
    settings.publish_host = "https://pub.example.org"
    assert client.get(f"/api/sessions/{s.id}/publication/defaults").json()["where"] == "host"

    body = {**d, "quotes": [{"start": 0, "end": 6, "with_clip": True}]}
    p = client.post(f"/api/sessions/{s.id}/publication/preview", json=body).json()
    assert p["id"] == "0" * 16 and p["summary"].startswith("Ana Lopez and Bo")
    clip = p["quotes"][0]["clip"]
    assert clip["preview_url"] == f"/api/audio/file/{s.id}#t=0.00,6.30"


# ---- publishing from this computer ----


def test_publish_serve_update_and_unpublish(settings, keystore, fake_cut, seals):
    settings.api_token = "secret"
    auth = {"Authorization": "Bearer secret"}
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    with TestClient(app) as client:
        s = _meeting(ctx, audio="/nowhere/mixed.ogg")
        quotes = [{"start": 0, "end": 6, "with_clip": True}]
        pub = _publish(client, s.id, auth, quotes=quotes, attendees=["Ana Lopez", "Bo Chen"])
        assert pub["version"] == 1 and pub["where"] == "local" and not pub["behind"]
        assert pub["url"].startswith("http://testserver/p/")
        pub_id, key = _key_and_id(pub["url"])
        assert pub_id == pub["id"] and pub_id != s.id
        assert fake_cut == [(0, 1)]

        # Open: no token for /pub and /p.
        r = client.get(f"/pub/{pub_id}")
        assert r.status_code == 200 and r.headers["x-pub-version"] == "1"
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-pub-expires"]
        etag = r.headers["etag"]
        assert client.get(f"/pub/{pub_id}", headers={"If-None-Match": etag}).status_code == 304
        manifest = pf.open_manifest(pub_id, 1, r.content, key)
        assert manifest.summary.startswith("Ana Lopez and Bo decided")
        clip = manifest.quotes[0].clip
        blob = client.get(f"/pub/{pub_id}/{clip.sha256}")
        assert blob.status_code == 200 and "immutable" in blob.headers["cache-control"]
        assert pf.open_clip(pub_id, clip, blob.content, key) == b"OggS" + b"clip 0-1"
        page = client.get(f"/p/{pub_id}")
        assert page.status_code in (200, 503)
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["referrer-policy"] == "no-referrer"
        assert client.get(f"/pub/{'f' * 16}").status_code == 404
        assert client.get(f"/api/sessions/{s.id}/publication").status_code == 401

        # Update: same id and key, version 2, the unchanged clip reused, a new one cut.
        quotes2 = [*quotes, {"start": 9.5, "end": 12, "with_clip": True}]
        pub2 = _publish(client, s.id, auth, quotes=quotes2, attendees=["Ana Lopez"])
        assert pub2["id"] == pub_id and pub2["url"] == pub["url"] and pub2["version"] == 2
        assert fake_cut == [(0, 1), (3, 3)]
        m2 = _open(client, pub2["url"])
        assert m2.version == 2 and m2.quotes[0].clip == clip and m2.quotes[1].clip.v == 2
        assert m2.attendees == ["Ana Lopez"]
        assert client.get(f"/pub/{pub_id}", headers={"If-None-Match": etag}).status_code == 200

        events = [e["kind"] for e in ctx.repo.events(s.id)]
        assert "published" in events and "publication_updated" in events
        published = next(e for e in ctx.repo.events(s.id) if e["kind"] == "published")
        assert published["detail"]["where"] == "this computer"
        assert "summary" in published["detail"]["parts"] and published["detail"]["clips"] == 1
        assert seals == [(s.id, "published"), (s.id, "publication updated")]

        listed = client.get("/api/publications", headers=auth).json()
        assert [i["publication"]["id"] for i in listed] == [pub_id]

        # Unpublish: 410 for the manifest and the clips.
        r = client.delete(f"/api/sessions/{s.id}/publication", headers=auth)
        assert r.status_code == 200 and r.json()["unpublished_at"] and r.json()["url"] is None
        assert client.get(f"/pub/{pub_id}").status_code == 410
        assert client.get(f"/pub/{pub_id}/{clip.sha256}").status_code == 410
        assert client.get(f"/api/sessions/{s.id}/publication", headers=auth).json() is None
        assert "unpublished" in [e["kind"] for e in ctx.repo.events(s.id)]
        assert seals[-1] == (s.id, "unpublished")
        # Publishing again is a new link.
        pub3 = _publish(client, s.id, auth)
        assert pub3["id"] != pub_id and pub3["version"] == 1


def test_behind_after_a_summary_edit(client, ctx, seals):
    s = _meeting(ctx)
    pub = _publish(client, s.id)
    assert not pub["behind"]
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    assert client.get(f"/api/sessions/{s.id}/publication").json()["behind"]
    pub = _publish(client, s.id)
    assert not pub["behind"] and pub["version"] == 2


def test_expired_publications_are_swept(client, ctx, seals):
    s = _meeting(ctx)
    pub = _publish(client, s.id, expires_days=1)
    meta_path = publishing.pub_dir(ctx, pub["id"]) / "meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["expires"] - time.time() == pytest.approx(86400, abs=60)
    assert "key" not in meta
    # A day later.
    meta["expires"] = int(time.time()) - 1
    meta_path.write_text(json.dumps(meta))
    row = ctx.repo.live_publication(s.id)
    row["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    ctx.repo.put_publication(row)
    assert client.get(f"/pub/{pub['id']}").status_code == 410  # before the sweep too
    assert publishing.sweep_expired(ctx) == 1
    assert not (publishing.pub_dir(ctx, pub["id"]) / "manifest").exists()
    assert client.get(f"/pub/{pub['id']}").status_code == 410
    assert ctx.repo.live_publication(s.id) is None


def test_deleting_the_meeting_removes_its_publication(client, ctx, seals, fake_cut):
    s = _meeting(ctx, audio="/nowhere/mixed.ogg")
    pub = _publish(client, s.id, quotes=[{"start": 0, "end": 6, "with_clip": True}])
    assert client.delete(f"/api/sessions/{s.id}").status_code == 200
    assert client.get(f"/pub/{pub['id']}").status_code == 410
    assert not (publishing.pub_dir(ctx, pub["id"]) / "blobs").exists()


def test_clips_of_a_meeting_recorded_elsewhere_are_refused(client, ctx, fake_cut):
    s = _meeting(ctx, audio="/nowhere/mixed.ogg", origin="peer0001")
    r = client.put(
        f"/api/sessions/{s.id}/publication",
        json={"where": "local", "quotes": [{"start": 0, "end": 6, "with_clip": True}]},
    )
    assert r.status_code == 409 and "The audio is on" in r.json()["detail"]
    # Without clips it publishes.
    r = client.put(
        f"/api/sessions/{s.id}/publication",
        json={"where": "local", "quotes": [{"start": 0, "end": 6}]},
    )
    assert r.status_code == 200 and fake_cut == []


@needs_ffmpeg
def test_a_real_clip(client, ctx, tmp_path, seals):
    audio = tmp_path / "mixed.ogg"
    subprocess.run(  # 14 s of a quiet tone, never played
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=14"]
        + ["-c:a", "libopus", str(audio)],
        check=True,
    )
    s = _meeting(ctx, audio=str(audio))
    pub = _publish(client, s.id, quotes=[{"start": 0, "end": 6, "with_clip": True}])
    manifest = _open(client, pub["url"])
    clip = manifest.quotes[0].clip
    blob = client.get(f"/pub/{pub['id']}/{clip.sha256}").content
    ogg = pf.open_clip(pub["id"], clip, blob, _key_and_id(pub["url"])[1])
    assert ogg.startswith(b"OggS") and clip.seconds == pytest.approx(6.3)


# ---- the publish host ----


class FakeHost:
    """The publish host contract (publish-host/), enough for the client."""

    def __init__(self):
        self.calls = []
        self.token = None
        self.blobs: dict[str, bytes] = {}
        self.manifest = None
        self.version = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, dict(request.headers)))
        if request.method == "GET" and path.count("/") == 2:  # the manifest: open, like the host
            if self.manifest is None:
                return httpx.Response(404)
            etag = '"' + hashlib.sha256(self.manifest).hexdigest() + '"'
            return httpx.Response(200, content=self.manifest, headers={"etag": etag})
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return httpx.Response(401, json={"error": "unauthorized"})
        token = auth[7:]
        if self.token is None:
            self.token = token
        elif token != self.token:
            return httpx.Response(403, json={"error": "wrong_token"})
        parts = path.strip("/").split("/")
        if request.method == "PUT" and len(parts) == 3:
            known = parts[2] in self.blobs
            self.blobs[parts[2]] = request.content
            return httpx.Response(204 if known else 201)
        if request.method == "PUT":
            version = int(request.headers["x-pub-version"])
            if version <= self.version:
                return httpx.Response(409, json={"error": "stale", "current": self.version})
            refs = [b for b in request.headers["x-pub-blobs"].split(",") if b]
            missing = [b for b in refs if b not in self.blobs]
            if missing:
                return httpx.Response(409, json={"error": "missing", "missing": missing})
            assert int(request.headers["x-pub-expires"]) <= time.time() + 365 * 86400 + 60
            self.version, self.manifest = version, request.content
            return httpx.Response(200, json={"version": version})
        if request.method == "DELETE":
            self.blobs.clear()
            self.manifest = None
            return httpx.Response(204)
        return httpx.Response(404)


def test_publishing_to_the_host(client, ctx, settings, fake_cut, seals):
    host = FakeHost()
    ctx.http_transport = httpx.MockTransport(host.handler)
    settings.publish_host = "https://pub.example.org/"
    settings.publish_host_token = "open-sesame"
    s = _meeting(ctx, audio="/nowhere/mixed.ogg")
    quotes = [{"start": 0, "end": 6, "with_clip": True}]
    pub = _publish(client, s.id, where="host", quotes=quotes, expires_days=500)
    pub_id, key = _key_and_id(pub["url"])
    assert pub["url"].startswith(f"https://pub.example.org/p/{pub_id}#")
    assert pub["host_url"] == "https://pub.example.org"
    # Blobs first, then the manifest naming them; the publisher token on each.
    assert [(m, p) for m, p, _ in host.calls] == [
        ("PUT", f"/pub/{pub_id}/{next(iter(host.blobs))}"),
        ("PUT", f"/pub/{pub_id}"),
    ]
    headers = host.calls[1][2]
    assert headers["x-pub-version"] == "1" and headers["x-pub-blobs"] == next(iter(host.blobs))
    expires = int(headers["x-pub-expires"])
    assert expires == pytest.approx(time.time() + 365 * 86400, abs=60)  # clamped
    assert all(h["x-upload-token"] == "open-sesame" for _, _, h in host.calls)
    row = ctx.repo.live_publication(s.id)
    assert host.token == row["token"]
    m = pf.open_manifest(pub_id, 1, host.manifest, key)
    assert m.quotes[0].clip.sha256 in host.blobs
    # Served here too.
    assert client.get(f"/pub/{pub_id}").content == host.manifest

    # An update sends only the new manifest: the clip is there already.
    host.calls.clear()
    pub2 = _publish(client, s.id, where="host", quotes=quotes, expires_days=30)
    assert pub2["version"] == 2 and [(m, p) for m, p, _ in host.calls] == [
        ("PUT", f"/pub/{pub_id}")
    ]

    # A host that refuses: a clear error.
    host.token = "someone-else"
    r = client.put(f"/api/sessions/{s.id}/publication", json={"where": "host", "quotes": quotes})
    job = _wait(client, r.json()["id"])
    assert job["status"] == "failed" and "refused this publication's token" in job["error"]
    host.token = row["token"]

    host.calls.clear()
    assert client.delete(f"/api/sessions/{s.id}/publication").status_code == 200
    assert [(m, p) for m, p, _ in host.calls] == [("DELETE", f"/pub/{pub_id}")]
    assert host.manifest is None


def test_a_manifest_retry_already_applied_counts_as_done(client, ctx, settings, seals):
    host = FakeHost()
    first = []

    def flaky(request):
        resp = host.handler(request)
        if request.method == "PUT" and request.url.path.count("/") == 2 and not first:
            first.append(1)  # applied, but the answer is lost
            return httpx.Response(502)
        return resp

    ctx.http_transport = httpx.MockTransport(flaky)
    settings.publish_host = "https://pub.example.org"
    s = _meeting(ctx)
    pub = _publish(client, s.id, where="host")
    assert pub["version"] == 1 and host.version == 1


def test_no_host_set_and_hosted_links_expire(client, ctx):
    s = _meeting(ctx)
    r = client.put(f"/api/sessions/{s.id}/publication", json={"where": "host"})
    assert r.status_code == 400 and "No publish host" in r.json()["detail"]
    ctx.settings.publish_host = "https://pub.example.org"
    r = client.put(
        f"/api/sessions/{s.id}/publication", json={"where": "host", "expires_days": None}
    )
    assert r.status_code == 400 and "must expire" in r.json()["detail"]


# ---- team servers ----


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [("Admin", "admin"), ("Ann", "member"), ("Rex", "reviewer")]:
        user = ctx.users.add(name, f"{name.lower()}@team.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"})
    with TestClient(app) as client:
        yield client, ctx, people


def test_team_servers_publish_only_when_allowed(team, seals):
    client, ctx, people = team
    ann, ann_auth = people["Ann"]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        s = _meeting(ctx)
    finally:
        access.current.reset(reset)
    r = client.put(f"/api/sessions/{s.id}/publication", json={}, headers=ann_auth)
    assert r.status_code == 403 and "turned off" in r.json()["detail"]

    ctx.settings.allow_publishing = True
    pub = _publish(client, s.id, ann_auth)
    assert pub["url"] and pub["published_by"] == "Ann"
    # Open on the server without a token.
    assert client.get(f"/pub/{pub['id']}").status_code == 200

    # A reviewer sees that it is published, not the link, and cannot publish it.
    rex_auth = people["Rex"][1]
    seen = client.get(f"/api/sessions/{s.id}/publication", headers=rex_auth).json()
    assert seen["id"] == pub["id"] and seen["url"] is None
    listed = client.get("/api/publications", headers=rex_auth).json()
    assert [i["publication"]["url"] for i in listed] == [None]
    r = client.put(f"/api/sessions/{s.id}/publication", json={}, headers=rex_auth)
    assert r.status_code == 403
    assert client.delete(f"/api/sessions/{s.id}/publication", headers=rex_auth).status_code == 403
    # An admin sees the link.
    admin = client.get(f"/api/sessions/{s.id}/publication", headers=people["Admin"][1]).json()
    assert admin["url"] == pub["url"]


def test_hiding_identifiers_hides_emails_and_phones(ctx):
    s = _meeting(ctx)
    contact = "Write to ana@example.org or call +1 415 555 0134 by 12:30 on 2026-10-21, room 42."
    data = s.summary_data.model_copy(update={"decisions": [contact]})
    transcript = [
        *s.transcript,
        TranscriptSegment(text=contact, speaker="Ana Lopez", start=13, end=16),
    ]
    ctx.repo.replace_segments(s.id, transcript)
    s = ctx.repo.update_fields(s.id, summary=contact, summary_data=data)
    sel = PublicationSelection(
        attendees=["Ana Lopez", "Bo Chen", "Cy Park"], quotes=[QuoteSpan(start=13, end=16)]
    )
    m = publishing.build_manifest(ctx, s, sel, "0" * 16, 1)[0]
    hidden = "Write to [EMAIL] or call [PHONE] by 12:30 on 2026-10-21, room 42."
    assert m.summary == hidden and m.decisions[0].text == hidden and m.quotes[0].text == hidden
    shown = publishing.build_manifest(
        ctx, s, sel.model_copy(update={"hide_identifiers": False}), "0" * 16, 1
    )[0]
    assert shown.summary == contact


def test_local_reach(client, ctx, settings, monkeypatch, seals):
    from mnemosyne.services.team_host import TeamHost

    s = _meeting(ctx)
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["local_reach"] == "this_device" and d["local_url"] == ""
    pub = _publish(client, s.id)
    assert pub["local_reach"] == "this_device" and pub["url"].startswith("http://testserver/p/")

    settings.phone_url = "https://desk.example.org/"
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["local_reach"] == "phone_url" and d["local_url"] == "https://desk.example.org"
    pub = client.get(f"/api/sessions/{s.id}/publication").json()
    assert pub["local_reach"] == "phone_url"
    assert pub["url"].startswith(f"https://desk.example.org/p/{pub['id']}#")

    monkeypatch.setattr(TeamHost, "running", property(lambda self: True))
    ctx.team_host.tailscale = "desk.tail1234.ts.net"
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["local_reach"] == "team" and d["local_url"] == "https://desk.tail1234.ts.net:8443"
    pub = client.get(f"/api/sessions/{s.id}/publication").json()
    assert pub["local_reach"] == "team"
    assert pub["url"].startswith("https://desk.tail1234.ts.net:8443/p/")
    # The defaults are a selection: sent back as one, they publish.
    assert client.put(f"/api/sessions/{s.id}/publication", json=d).status_code == 200


def test_a_same_version_conflict_is_ours_only_when_the_host_holds_our_manifest(
    client, ctx, settings, fake_cut, seals
):
    class LostReply(FakeHost):
        """Applies the manifest, then answers as a retried PUT would (409, current == ours)."""

        def handler(self, request):
            resp = super().handler(request)
            if (
                request.method == "PUT"
                and request.url.path.count("/") == 2
                and resp.status_code == 200
            ):
                return httpx.Response(409, json={"error": "stale", "current": self.version})
            return resp

    host = LostReply()
    ctx.http_transport = httpx.MockTransport(host.handler)
    settings.publish_host = "https://pub.example.org"
    s = _meeting(ctx)
    pub = _publish(client, s.id, where="host")
    assert pub["version"] == 1  # the host has our manifest: a success

    # The other computer put its own version 2 there first: ours is refused, not taken as done.
    class Raced(FakeHost):
        pass

    raced = Raced()
    raced.token, raced.version, raced.manifest = host.token, 2, b"someone else's manifest"
    raced.blobs = dict(host.blobs)
    real = raced.handler

    def handler(request):
        if request.method == "PUT" and request.url.path.count("/") == 2:
            return httpx.Response(409, json={"error": "stale", "current": 2})
        return real(request)

    ctx.http_transport = httpx.MockTransport(handler)
    r = client.put(f"/api/sessions/{s.id}/publication", json={"where": "host"})
    job = _wait(client, r.json()["id"])
    assert job["status"] == "failed" and "at the same time" in job["error"]
