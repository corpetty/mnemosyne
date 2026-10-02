"""Summaries with nothing else installed: the built-in model (services/local_llm.py), and, when
Ollama is installed, pulling a model sized to this machine into it. For the setup wizard and
Settings → AI."""

import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...jobs import Job
from ...models.base import ApiModel
from ...services import local_llm
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["local-model"])
ADMIN = [Depends(access.require_admin)]

# The same models in Ollama's library, for machines that already run Ollama.
OLLAMA_MODELS = {
    "qwen3-4b": "qwen3:4b-instruct-2507-q4_K_M",
    "qwen3-30b-a3b": "qwen3:30b-a3b-instruct-2507-q4_K_M",
}


class LocalModelInfo(ApiModel):
    id: str
    label: str
    size: int  # bytes to download
    downloaded: bool
    recommended: bool  # the biggest this machine has the memory for


class OllamaInfo(ApiModel):
    reachable: bool  # Ollama answers at ollama_url
    models: list[str]
    suggested: str  # the model to pull, sized to this machine


class LocalModelStatus(ApiModel):
    ram_gb: int
    vulkan: bool  # a GPU through Vulkan; else the CPU
    server_installed: bool
    running: str | None  # the built-in model being served now
    models: list[LocalModelInfo]
    ollama: OllamaInfo


class ModelRequest(ApiModel):
    model: str


def _llm(ctx: AppContext) -> local_llm.LocalLLM:
    return local_llm.manager(ctx.settings.models_dir)


async def _ollama(ctx: AppContext) -> tuple[bool, list[str]]:
    try:
        async with httpx.AsyncClient(timeout=2, transport=ctx.http_transport) as client:
            r = await client.get(f"{ctx.settings.ollama_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            return True, [m["name"] for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        return False, []


@router.get("/local-model", response_model=LocalModelStatus)
async def status(ctx: AppContext = Depends(get_ctx)):
    llm = _llm(ctx)
    best = local_llm.recommended()
    here = set(llm.downloaded())
    reachable, models = await _ollama(ctx)
    return LocalModelStatus(
        ram_gb=local_llm.ram_gb(),
        vulkan=local_llm.has_vulkan(),
        server_installed=llm.server_binary().is_file(),
        running=llm.running,
        models=[
            LocalModelInfo(
                id=m.id,
                label=m.label,
                size=m.file.size,
                downloaded=m.id in here,
                recommended=m.id == best,
            )
            for m in local_llm.MODELS.values()
        ],
        ollama=OllamaInfo(reachable=reachable, models=models, suggested=OLLAMA_MODELS[best]),
    )


@router.post("/local-model/download", response_model=Job, dependencies=ADMIN)
async def download(request: ModelRequest, ctx: AppContext = Depends(get_ctx)):
    """Download the built-in model (and its server, the first time): a `model_download` job
    whose progress is the share of bytes fetched. When it finishes, summaries use it unless
    another provider was already chosen and works."""
    if request.model not in local_llm.MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model {request.model!r}")
    if any(j.kind == "model_download" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="A model is already downloading")
    llm = _llm(ctx)
    model = local_llm.MODELS[request.model]

    async def run(job) -> dict:
        def progress(done: int, total: int) -> None:
            mb, of = done // 1_000_000, total // 1_000_000
            job.update(f"Downloading {model.label}: {mb} of {of} MB", progress=done / total)

        job.update(f"Downloading {model.label}", progress=0.0)
        await asyncio.to_thread(llm.install, request.model, progress)
        return {"model": request.model}

    return ctx.jobs.submit("model_download", run)


@router.delete("/local-model/{model_id}", dependencies=ADMIN)
async def remove(model_id: str, ctx: AppContext = Depends(get_ctx)):
    """Free the disk space a downloaded model takes."""
    if model_id not in local_llm.MODELS:
        raise HTTPException(status_code=404, detail="Unknown model")
    _llm(ctx).remove(model_id)
    return {"removed": model_id}


@router.post("/ollama/pull", response_model=Job, dependencies=ADMIN)
async def ollama_pull(request: ModelRequest, ctx: AppContext = Depends(get_ctx)):
    """Pull a model into the Ollama this backend talks to: an `ollama_pull` job with Ollama's
    own progress."""
    if not request.model.strip():
        raise HTTPException(status_code=400, detail="Name a model")
    base = ctx.settings.ollama_url.rstrip("/")

    async def run(job) -> dict:
        job.update(f"Pulling {request.model} into Ollama", progress=0.0)
        timeout = httpx.Timeout(None, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, transport=ctx.http_transport) as client:
            body = {"model": request.model, "stream": True}
            async with client.stream("POST", f"{base}/api/pull", json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"Ollama answered {r.status_code}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(f"Ollama: {event['error']}")
                    total, done = event.get("total"), event.get("completed")
                    share = done / total if total and done is not None else None
                    job.update(f"Ollama: {event.get('status', '')}", progress=share)
        return {"model": request.model}

    return ctx.jobs.submit("ollama_pull", run)
