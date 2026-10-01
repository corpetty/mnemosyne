"""a team server: the web app served by the backend, team mode, and a key from systemd."""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.api.web import MARKER
from mnemosyne.services.summarization_service import SummarizationService
from mnemosyne.storage.crypto import CredentialKeyStore


@pytest.fixture
def web_dir(tmp_path):
    root = tmp_path / "build"
    (root / "_app" / "immutable").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><html><head><title>M</title></head></html>")
    (root / "_app" / "immutable" / "app.abc.js").write_text("console.log(1)")
    (root / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("not for you")
    return root


def _client(settings, keystore, **kw):
    for k, v in kw.items():
        setattr(settings, k, v)
    return TestClient(create_app(settings, keystore=keystore))


def test_the_web_app_is_served_with_its_backend_marker(settings, keystore, web_dir):
    with _client(settings, keystore, web_dir=str(web_dir)) as c:
        index = c.get("/")
        assert index.status_code == 200 and MARKER in index.text
        assert c.get("/sessions/abc").text == index.text  # the SPA routes itself
        asset = c.get("/_app/immutable/app.abc.js")
        assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
        assert c.get("/favicon.svg").text == "<svg/>"
        assert "not for you" not in c.get("/../secret.txt").text
        assert c.get("/api/nope").status_code == 404
        assert c.get("/api/nope").headers["content-type"].startswith("application/json")
        assert c.get("/health").json()["status"] == "ok"  # other routes still win


def test_a_rebuilt_web_app_is_served_without_a_restart(settings, keystore, web_dir):

    with _client(settings, keystore, web_dir=str(web_dir)) as c:
        assert "<title>M</title>" in c.get("/").text
        index = web_dir / "index.html"
        index.write_text("<!doctype html><html><head><title>New</title></head></html>")
        os.utime(index, (1, 2_000_000_000))  # a different mtime, however fast the test runs
        text = c.get("/").text
        assert "<title>New</title>" in text and MARKER in text


def test_the_web_app_opens_without_a_token_but_the_api_does_not(settings, keystore, web_dir):
    with _client(settings, keystore, web_dir=str(web_dir), api_token="t0k") as c:
        assert c.get("/").status_code == 200
        assert c.get("/_app/immutable/app.abc.js").status_code == 200
        assert c.get("/api/sessions").status_code == 401
        headers = {"Authorization": "Bearer t0k"}
        assert c.get("/api/sessions", headers=headers).status_code == 200


def test_no_web_app_without_web_dir_or_without_a_build(settings, keystore, tmp_path):
    with _client(settings, keystore) as c:
        assert c.get("/").status_code == 404
    with _client(settings, keystore, web_dir=str(tmp_path / "missing")) as c:
        assert c.get("/").status_code == 404


def test_with_cloud_models_off_no_cloud_provider_exists(settings):
    settings.openai_api_key = "sk-x"
    settings.anthropic_api_key = "sk-y"
    assert {"openai", "anthropic"} <= set(SummarizationService(settings).providers)
    settings.cloud_models = False
    assert not {"openai", "anthropic"} & set(SummarizationService(settings).providers)


def test_health_says_team_mode(settings, keystore):
    with _client(settings, keystore, team_mode=True, cloud_models=False) as c:
        health = c.get("/health").json()
        assert health["team_mode"] is True and health["cloud_models"] is False


def test_credential_key_store_reads_a_systemd_credential(tmp_path):
    key = bytes(range(32))
    path = tmp_path / "mnemosyne-key"
    path.write_text(base64.b64encode(key).decode() + "\n")
    store = CredentialKeyStore(path)
    assert store.get() == key
    store.set(key)  # the same key: fine
    with pytest.raises(RuntimeError, match="MNEMOSYNE_KEY_FILE"):
        store.set(bytes(32))
    store.delete()
    assert path.exists()  # the system owns it
    path.write_text("not base64!")
    assert store.get() is None
    path.write_text(base64.b64encode(b"short").decode())
    assert store.get() is None
    assert CredentialKeyStore(tmp_path / "nope").get() is None


def test_encryption_adopts_the_credential_key(settings, tmp_path):
    key = bytes(range(32))
    path = tmp_path / "mnemosyne-key"
    path.write_text(base64.b64encode(key).decode())
    with TestClient(create_app(settings, keystore=CredentialKeyStore(path))) as c:
        assert c.post("/api/encryption/enable").status_code == 200
        assert c.app.state.ctx.master_key == key
    # Restarted with the same credential: unlocked.
    with TestClient(create_app(settings, keystore=CredentialKeyStore(path))) as c:
        assert c.get("/api/encryption").json() == {"enabled": True, "locked": False}


def test_encryption_refuses_an_unusable_credential(settings, tmp_path):
    path = tmp_path / "mnemosyne-key"
    path.write_text("garbage")
    with TestClient(create_app(settings, keystore=CredentialKeyStore(path))) as c:
        r = c.post("/api/encryption/enable")
        assert r.status_code >= 400
        assert c.get("/api/encryption").json()["enabled"] is False


def test_the_key_file_env_picks_the_credential_store(settings, tmp_path, monkeypatch):
    path = tmp_path / "k"
    path.write_text(base64.b64encode(bytes(32)).decode())
    monkeypatch.setenv("MNEMOSYNE_KEY_FILE", str(path))
    app = create_app(settings)
    assert isinstance(app.state.ctx.keystore, CredentialKeyStore)
