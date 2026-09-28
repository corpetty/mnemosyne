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
    assert await models.unload_if_idle(busy=False, now=t + 15 * 60)
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
