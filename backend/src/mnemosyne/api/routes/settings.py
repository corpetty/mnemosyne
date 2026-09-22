"""Settings endpoints: read effective settings, update and persist them."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...config import SECRET_FIELDS, Settings, config_file_path, save_settings
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    values: dict
    secrets_set: dict[str, bool]
    env_overrides: list[str]
    config_file: str
    obsidian_vault_exists: bool


class SettingsUpdate(BaseModel):
    """Partial update. Secret fields: empty string means keep, null means clear."""

    whisper_model_size: str | None = None
    whisper_compute_type: str | None = None
    whisper_batch_size: int | None = None
    auto_transcribe: bool | None = None
    hf_token: str | None = None
    ollama_url: str | None = None
    vllm_url: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    default_provider: str | None = None
    default_model: str | None = None
    obsidian_vault_path: str | None = None
    obsidian_subfolder: str | None = None


def _response(settings: Settings) -> SettingsResponse:
    vault = settings.obsidian_vault_path
    return SettingsResponse(
        values=settings.public_dict(),
        secrets_set=settings.secrets_set(),
        env_overrides=settings.env_overrides(),
        config_file=str(config_file_path()),
        obsidian_vault_exists=bool(vault) and Path(vault).expanduser().is_dir(),
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(ctx: AppContext = Depends(get_ctx)):
    return _response(ctx.settings)


@router.put("", response_model=SettingsResponse)
async def update_settings(update: SettingsUpdate, ctx: AppContext = Depends(get_ctx)):
    current = ctx.settings.model_dump()
    provided = update.model_dump(exclude_unset=True)
    for key, value in provided.items():
        if key in SECRET_FIELDS:
            if value is None:
                current[key] = ""
            elif value != "":
                current[key] = value
        elif value is not None:
            current[key] = value

    try:
        # Build without env/toml sources so the merged dict is authoritative,
        # then persist; env overrides still win on the next full load.
        new_settings = Settings.model_validate(current)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    save_settings(new_settings)
    await ctx.apply_settings(new_settings)
    return _response(new_settings)
