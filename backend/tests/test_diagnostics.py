"""Backend log file and the diagnostics report (Settings → Copy diagnostics)."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from mnemosyne.logs import log_path, setup_logging, tail
from mnemosyne.services.diagnostics import SHOWN_TEXT


@pytest.fixture
def logging_to(settings):
    """setup_logging for the test's data dir, undone afterwards (it touches the root logger)."""
    root = logging.getLogger()
    before = list(root.handlers), root.level
    uvicorn_before = {n: list(logging.getLogger(n).handlers) for n in ("uvicorn", "uvicorn.error")}
    path = setup_logging(settings.data_dir)
    yield path
    for h in root.handlers:
        if h not in before[0]:
            h.close()
    root.handlers[:] = before[0]
    root.setLevel(before[1])
    for name, handlers in uvicorn_before.items():
        logging.getLogger(name).handlers[:] = handlers
    logging.captureWarnings(False)


def test_setup_logging_writes_a_file_once(settings, logging_to):
    assert logging_to == log_path(settings.data_dir)
    assert setup_logging(settings.data_dir) == logging_to  # no second handler
    ours = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(ours) == 1
    logging.getLogger("mnemosyne.test").info("hello from the test")
    assert any("hello from the test" in line for line in tail(logging_to))


def test_tail_of_a_missing_file_is_empty(tmp_path):
    assert tail(tmp_path / "nope.log") == []


def test_diagnostics_hide_secrets_and_personal_text(client, ctx, logging_to):
    hidden = {}
    for name, value in ctx.settings.model_dump().items():
        if isinstance(value, str) and name not in SHOWN_TEXT:
            hidden[name] = f"SENTINEL-{name}"
            setattr(ctx.settings, name, hidden[name])
    ctx.settings.transcriber = "parakeet"
    logging.getLogger("mnemosyne.test").warning("something went wrong")

    auth = {"Authorization": f"Bearer {hidden['api_token']}"}  # setting it turns auth on
    text = client.get("/api/system/diagnostics", headers=auth).json()["text"]

    leaked = [n for n, v in hidden.items() if v in text]
    assert leaked == []
    assert "hf_token = '<set>'" not in text and "hf_token = <set>" in text
    assert "transcriber = 'parakeet'" in text
    assert "something went wrong" in text
    assert "## Mnemosyne diagnostics" in text


def test_diagnostics_replace_the_home_directory(client, ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ctx.settings.data_dir = tmp_path / "data"
    text = client.get("/api/system/diagnostics").json()["text"]
    assert str(tmp_path) not in text
    assert "data_dir = '~/data'" in text
