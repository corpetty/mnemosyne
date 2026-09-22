"""Settings endpoints: read effective settings, update and persist them."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, create_model

from ...config import SECRET_FIELDS, Settings, config_file_path, save_settings
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    values: dict
    secrets_set: dict[str, bool]
    env_overrides: list[str]
    config_file: str
    obsidian_vault_exists: bool


# Every Settings field except data_dir, all optional. Derived from Settings so a
# new field is updatable without touching this file. Secrets: "" keeps, null clears.
_UPDATABLE = {
    name: (field.annotation | None, None)
    for name, field in Settings.model_fields.items()
    if name != "data_dir"
}
SettingsUpdate = create_model("SettingsUpdate", **_UPDATABLE)


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
        else:
            # null is a real value for optional fields (e.g. min_speakers);
            # validation below rejects it for required ones.
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
