# ruff: noqa: F401, F811
"""A publication as one HTML file (services/publication_file.py, routes/publishing.py)."""

import json
import re

import pytest

from mnemosyne.services import publication_file
from mnemosyne.services import publication_format as pf

from .test_publishing import _meeting, _wait, fake_cut, seals, team


@pytest.fixture(autouse=True)
def template(monkeypatch, tmp_path):
    path = tmp_path / "single.html"
    path.write_text(
        '<!doctype html><html><body><script type="module">/* viewer */</script>\n'
        '<script type="application/json" id="payload">__PAYLOAD__</script></body></html>\n'
    )
    monkeypatch.setattr(publication_file, "TEMPLATE", path)
    return path


def _export(client, sid, selection=None, password=None, headers=None):
    r = client.post(
        f"/api/sessions/{sid}/publication/file",
        json={"selection": selection or {}, "password": password},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["id"], headers=headers)
    assert job["status"] == "completed", job
    return job["result"]


def _payload(html: str) -> dict:
    assert html.count("__PAYLOAD__") == 0
    m = re.findall(r'<script type="application/json" id="payload">(.*?)</script>', html, re.S)
    assert len(m) == 1
    return json.loads(m[0])


def _download(client, sid, result, headers=None):
    r = client.get(f"/api/sessions/{sid}/publication/file/{result['token']}", headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_a_file_with_a_random_key(client, ctx, seals, fake_cut):
    s = _meeting(ctx, audio="/nowhere/mixed.ogg")
    sel = {
        "quotes": [{"start": 0, "end": 6, "with_clip": True}],
        "attendees": ["Ana Lopez", "Bo Chen"],
    }
    result = _export(client, s.id, sel)
    assert result["filename"] == "Release-sync-with-Bo-Chen.html"
    r = _download(client, s.id, result)
    assert r.headers["content-type"].startswith("text/html")
    assert (
        r.headers["content-disposition"] == 'attachment; filename="Release-sync-with-Bo-Chen.html"'
    )
    p = _payload(r.text)
    assert set(p) == {"format", "id", "version", "manifest", "clips", "key"}
    assert p["format"] == 1 and p["version"] == 1 and len(p["id"]) == 16 and p["id"] != s.id
    assert len(p["key"]) == 43 and "=" not in p["key"]
    key = pf.decode_key(p["key"])
    m = pf.open_manifest(p["id"], 1, pf.b64url_decode(p["manifest"]), key)
    assert m.title == "Release sync with Bo Chen" and m.decisions
    clip = m.quotes[0].clip
    assert set(p["clips"]) == {clip.sha256}
    ogg = pf.open_clip(p["id"], clip, pf.b64url_decode(p["clips"][clip.sha256]), key)
    assert ogg == b"OggS" + b"clip 0-1"
    # Not a publication, and fresh ids and keys each time.
    assert ctx.repo.live_publication(s.id) is None
    p2 = _payload(_download(client, s.id, _export(client, s.id)).text)
    assert p2["id"] != p["id"] and p2["key"] != p["key"] and p2["clips"] == {}
    # Downloaded once.
    assert client.get(f"/api/sessions/{s.id}/publication/file/{result['token']}").status_code == 404


def test_a_password_file(client, ctx, seals):
    s = _meeting(ctx)
    result = _export(client, s.id, {"attendees": ["Ana Lopez"]}, password="correct horse")
    p = _payload(_download(client, s.id, result).text)
    assert "key" not in p and p["password"]["iterations"] == 600_000
    salt = pf.b64url_decode(p["password"]["salt"])
    assert len(salt) == 16
    key = pf.derive_key("correct horse", salt)
    m = pf.open_manifest(p["id"], 1, pf.b64url_decode(p["manifest"]), key)
    assert m.summary.startswith("Ana Lopez")
    with pytest.raises(pf.PublicationFormatError):
        pf.open_manifest(
            p["id"], 1, pf.b64url_decode(p["manifest"]), pf.derive_key("wrong password", salt)
        )


def test_a_short_password_is_refused(client, ctx):
    s = _meeting(ctx)
    r = client.post(
        f"/api/sessions/{s.id}/publication/file", json={"selection": {}, "password": "short"}
    )
    assert r.status_code == 400 and "8 characters" in r.json()["detail"]


def test_no_transcript_and_redaction(client, ctx, seals):
    s = _meeting(ctx)
    sel = {"quotes": [{"start": 6.5, "end": 9.0}], "attendees": ["Ana Lopez", "Bo Chen"]}
    html = _download(client, s.id, _export(client, s.id, sel)).text
    p = _payload(html)
    m = pf.open_manifest(p["id"], 1, pf.b64url_decode(p["manifest"]), pf.decode_key(p["key"]))
    body = pf.manifest_bytes(m).decode()
    assert "123456789012" not in body and "Great, thanks" not in body
    assert "segments" not in body and "transcript" not in body
    assert m.quotes[0].text == "My account number is [account ••9012], send it there."


def test_script_closing_tags_are_escaped(client, ctx, seals):
    s = _meeting(ctx)
    ctx.repo.update_fields(s.id, summary="Evil </script><script>alert(1)</script> <!-- end")
    html = _download(client, s.id, _export(client, s.id)).text
    assert html.count("</script>") == 2  # the viewer's and the payload's own
    assert "<script>alert" not in html
    p = _payload(html)
    m = pf.open_manifest(p["id"], 1, pf.b64url_decode(p["manifest"]), pf.decode_key(p["key"]))
    assert "</script><script>alert(1)" in m.summary


def test_history_and_records(client, ctx, seals):
    s = _meeting(ctx)
    _export(client, s.id, {"attendees": ["Ana Lopez"]}, password="correct horse")
    events = [e for e in ctx.repo.events(s.id) if e["kind"] == "exported_publication"]
    assert len(events) == 1
    d = events[0]["detail"]
    assert d["password"] is True and "summary" in d["parts"] and "attendees" in d["parts"]
    assert (s.id, "publication exported") in seals
    _export(client, s.id)
    assert [
        e["detail"]["password"]
        for e in ctx.repo.events(s.id)
        if e["kind"] == "exported_publication"
    ].count(False) == 1


def test_clips_off_the_origin_are_refused(client, ctx, fake_cut):
    s = _meeting(ctx, audio="/nowhere/mixed.ogg", origin="peer0001")
    sel = {"quotes": [{"start": 0, "end": 6, "with_clip": True}]}
    r = client.post(f"/api/sessions/{s.id}/publication/file", json={"selection": sel})
    assert r.status_code == 409 and "The audio is on" in r.json()["detail"]
    r = client.post(
        f"/api/sessions/{s.id}/publication/file",
        json={"selection": {"quotes": [{"start": 0, "end": 6}]}},
    )
    assert r.status_code == 200 and fake_cut == []


def test_no_template_is_a_503(client, ctx, template):
    s = _meeting(ctx)
    template.unlink()
    r = client.post(f"/api/sessions/{s.id}/publication/file", json={"selection": {}})
    assert r.status_code == 503 and "not built" in r.json()["detail"]


def test_permissions(team, seals):  # noqa: F811
    from mnemosyne import access

    client, ctx, people = team
    ann, ann_auth = people["Ann"]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        s = _meeting(ctx)
    finally:
        access.current.reset(reset)
    url = f"/api/sessions/{s.id}/publication/file"
    body = {"selection": {}}
    # Off until an admin allows publishing.
    assert client.post(url, json=body, headers=ann_auth).status_code == 403
    ctx.settings.allow_publishing = True
    # A reviewer can read the meeting but not export it.
    assert client.post(url, json=body, headers=people["Rex"][1]).status_code == 403
    result = _export(client, s.id, headers=ann_auth)
    # Another person cannot fetch the owner's file; the owner (and an admin) can.
    dl = f"{url}/{result['token']}"
    assert client.get(dl, headers=people["Rex"][1]).status_code in (403, 404)
    assert client.get(dl, headers=ann_auth).status_code == 200
    again = _export(client, s.id, headers=ann_auth)
    assert client.get(f"{url}/{again['token']}", headers=people["Admin"][1]).status_code == 200
    assert client.post(url, json=body, headers=people["Admin"][1]).status_code == 200


def test_saving_to_a_chosen_path(client, ctx, seals, tmp_path):
    s = _meeting(ctx)
    result = _export(client, s.id)
    html = _download(client, s.id, _export(client, s.id)).text  # another file, for comparison
    url = f"/api/sessions/{s.id}/publication/file/{result['token']}/save"
    target = tmp_path / "out.html"
    # A folder that is not there, and a token that is not this meeting's.
    r = client.post(url, json={"path": str(tmp_path / "nope" / "out.html")})
    assert r.status_code == 400 and not target.exists()
    bad = f"/api/sessions/{s.id}/publication/file/{'0' * 32}/save"
    assert client.post(bad, json={"path": str(target)}).status_code == 404
    r = client.post(url, json={"path": str(target)})
    assert r.status_code == 200 and r.json() == {"path": str(target)}
    p = _payload(target.read_text())
    assert p["format"] == 1 and len(p["id"]) == 16 and html
    # Written once: the file is gone from the made files.
    assert client.post(url, json={"path": str(target)}).status_code == 404
    assert client.get(url.removesuffix("/save")).status_code == 404
    assert not list(publication_file.files_dir(ctx).glob(f"{result['token']}.*"))


def test_saving_follows_the_downloads_owner_rule(team, seals, tmp_path):  # noqa: F811
    from mnemosyne import access

    client, ctx, people = team
    ann, ann_auth = people["Ann"]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        s = _meeting(ctx)
    finally:
        access.current.reset(reset)
    ctx.settings.allow_publishing = True
    result = _export(client, s.id, headers=ann_auth)
    url = f"/api/sessions/{s.id}/publication/file/{result['token']}/save"
    target = tmp_path / "out.html"
    assert client.post(url, json={"path": str(target)}, headers=people["Rex"][1]).status_code in (
        403,
        404,
    )
    assert not target.exists()
    assert client.post(url, json={"path": str(target)}, headers=ann_auth).status_code == 200
    assert target.is_file()
    again = _export(client, s.id, headers=ann_auth)
    other = tmp_path / "admin.html"
    url = f"/api/sessions/{s.id}/publication/file/{again['token']}/save"
    admin = people["Admin"][1]
    assert client.post(url, json={"path": str(other)}, headers=admin).status_code == 200


def test_payload_json_escapes_less_than():
    html = publication_file.render(
        '<script id="payload">__PAYLOAD__</script>',
        {"x": "</script><!-- <b>", "key": "__PAYLOAD__"},
    )
    assert html.count("</script>") == 1 and "<" not in html.split(">", 1)[1].rsplit("<", 1)[0]
    body = html.split(">", 1)[1].rsplit("</script>", 1)[0]
    assert json.loads(body) == {"x": "</script><!-- <b>", "key": "__PAYLOAD__"}
