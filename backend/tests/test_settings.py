"""Settings API: masking, persistence, env precedence, live re-apply."""

import tomllib

from mnemosyne.config import Settings


def test_get_settings_masks_secrets(client, ctx):
    ctx.settings.hf_token = "hf_secret"
    body = client.get("/api/settings").json()
    assert body["values"]["hf_token"] == ""
    assert body["secrets_set"]["hf_token"] is True
    assert body["secrets_set"]["openai_api_key"] is False
    assert body["values"]["whisper_model_size"] == "medium.en"
    assert body["env_overrides"] == []
    assert body["config_file"].endswith("config.toml")


def test_put_persists_and_applies(client, ctx, tmp_path):
    resp = client.put(
        "/api/settings",
        json={"ollama_url": "http://gpu-box:11434", "whisper_model_size": "large-v3"},
    )
    assert resp.status_code == 200
    assert resp.json()["values"]["ollama_url"] == "http://gpu-box:11434"

    # applied live
    assert ctx.settings.ollama_url == "http://gpu-box:11434"
    assert ctx.summarizer.providers["ollama"].base_url == "http://gpu-box:11434"
    assert ctx.models.settings.whisper_model_size == "large-v3"

    # persisted
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["ollama_url"] == "http://gpu-box:11434"
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600

    # a fresh load picks it up
    assert Settings().ollama_url == "http://gpu-box:11434"


def test_secret_semantics(client, ctx):
    client.put("/api/settings", json={"hf_token": "abc"})
    assert ctx.settings.hf_token == "abc"
    client.put("/api/settings", json={"hf_token": ""})  # keep
    assert ctx.settings.hf_token == "abc"
    client.put("/api/settings", json={"hf_token": None})  # clear
    assert ctx.settings.hf_token == ""


def test_env_overrides_reported(client, monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_URL", "http://from-env:1")
    s = Settings(data_dir=tmp_path / "d")
    assert s.ollama_url == "http://from-env:1"
    assert "ollama_url" in s.env_overrides()


def test_changing_whisper_settings_unloads_engine(client, ctx, fake_engine):
    fake_engine.loaded = True
    client.put("/api/settings", json={"whisper_batch_size": 4})
    assert ctx.models._engine is None  # dropped, will be rebuilt with new config


def test_invalid_value_is_422(client):
    resp = client.put("/api/settings", json={"whisper_batch_size": "lots"})
    assert resp.status_code == 422
