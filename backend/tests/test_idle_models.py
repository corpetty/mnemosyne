"""Idle speech models are unloaded to free GPU memory (ModelService.unload_if_idle)."""

import pytest

from mnemosyne.config import Settings
from mnemosyne.services.model_service import ModelService
from tests.fakes import FakeEngine


@pytest.fixture
def models(tmp_path):
    m = ModelService(Settings(data_dir=tmp_path, unload_models_after_minutes=15))
    m._engine = FakeEngine()
    return m


async def _loaded(m):
    await m._engine.load()
    assert m.loaded


@pytest.mark.anyio
async def test_unloaded_after_the_idle_time(models):
    await _loaded(models)
    t = models.last_used
    assert not await models.unload_if_idle(busy=False, now=t + 14 * 60)
    assert await models.unload_if_idle(busy=False, now=t + 15 * 60 + 0.001)  # float-safe
    assert not models.loaded and models._engine is None


@pytest.mark.anyio
async def test_never_while_busy_and_busy_counts_as_use(models):
    await _loaded(models)
    t = models.last_used
    assert not await models.unload_if_idle(busy=True, now=t + 60 * 60)
    # being busy just now reset the clock
    assert not await models.unload_if_idle(busy=False, now=models.last_used + 60)


@pytest.mark.anyio
async def test_zero_keeps_them_loaded(models):
    models.settings.unload_models_after_minutes = 0
    await _loaded(models)
    assert not await models.unload_if_idle(busy=False, now=models.last_used + 10**6)
    assert models.loaded


@pytest.mark.anyio
async def test_unloading_hands_memory_back(models, monkeypatch, caplog):
    from mnemosyne.services import model_service

    released = []
    monkeypatch.setattr(model_service, "release_memory", lambda: released.append(True))
    await _loaded(models)
    with caplog.at_level("INFO", logger="mnemosyne.services.model_service"):
        assert await models.unload_if_idle(busy=False, now=models.last_used + 16 * 60)
    assert released == [True]
    assert "process" in caplog.text and "MB" in caplog.text


def test_release_memory_and_rss_work_without_torch():
    from mnemosyne.services.model_service import release_memory, rss_mb

    assert rss_mb() > 0
    release_memory()  # torch not imported in tests: nothing CUDA, still trims


def test_compile_workers_are_off():
    """PyTorch kept a pool of 21 compile workers alive (~350 MB each); importing the package
    sets one in-process compile thread before torch can be imported."""
    import os

    import mnemosyne  # noqa: F401

    assert os.environ["TORCHINDUCTOR_COMPILE_THREADS"] == "1"
