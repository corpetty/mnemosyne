"""Settings endpoints: read effective settings, update and persist them."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import create_model

from ... import access
from ...config import SECRET_FIELDS, Settings, config_file_path, save_settings
from ...models.base import ApiModel
from ...services.meeting_types import available_types
from ..auth import is_owner_channel, require_owner_channel
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/settings", tags=["settings"])
# Team-wide: an admin's job (access.py). On a computer that hosts a team the settings are the
# owner's own configuration, so only the owner channel may change them (guard first).
ADMIN = [Depends(require_owner_channel), Depends(access.require_admin)]


# Every setting as it is sent to the UI (secrets blanked); derived from Settings so the
# generated TypeScript type follows new fields automatically.
_VALUES = {
    name: (
        (str, ...)
        if name == "data_dir"
        # the effective value (Settings.public_dict), never "automatic"
        else (bool, ...)
        if name == "use_our_servers"
        else (field.annotation, ...)
    )
    for name, field in Settings.model_fields.items()
}
SettingsValues = create_model("SettingsValues", __base__=ApiModel, **_VALUES)


class SettingsResponse(ApiModel):
    values: SettingsValues  # type: ignore[valid-type]
    secrets_set: dict[str, bool]
    env_overrides: list[str]
    config_file: str
    obsidian_vault_exists: bool
    # Every type a meeting can be given (meeting_types).
    meeting_type_names: list[str]


# Every Settings field except data_dir, all optional. Derived from Settings so a
# new field is updatable without touching this file. Secrets: "" keeps, null clears.
# Encryption is turned on and off through /api/encryption, which also converts the data:
# flipping the flag alone would lock the data away.
_NOT_UPDATABLE = {"data_dir", "encrypt_at_rest", "encryption_check"}
_UPDATABLE = {
    name: (field.annotation | None, None)
    for name, field in Settings.model_fields.items()
    if name not in _NOT_UPDATABLE
}
SettingsUpdate = create_model("SettingsUpdate", **_UPDATABLE)


# On a computer that hosts a team (config.hosts_team) the settings are its owner's own
# configuration; GET /api/settings answers everyone else (members, reviewers, the owner's
# browser on the team listener) without these: they read as their defaults ("" for data_dir),
# their secrets as not set, and the config file and environment are not named. What the team's
# web app reads stays (meeting types, the summary model, consent script, publishing and guest
# notes switches, auto-record, copilot_to_notes, setup_complete). A new personal setting goes
# here (docs/plans/2026-11-09-team-hosting.md 2.6, R4).
PERSONAL_SETTINGS = frozenset(
    {
        "data_dir",  # a path in the owner's home
        "local_speaker_name",  # the owner's name
        "echo_cancel_mic",  # the owner's microphone
        # remote access, the phone, library sync and contacts (People)
        "phone_url",
        "remote_access",
        "remote_relays",
        "shared_in_ask",
        "shared_in_search",
        "shared_in_digest",
        "shared_in_obsidian",
        # the owner's calendar
        "calendar_source",
        "calendar_ics_url",
        "calendar_desktop_calendars",
        # where the owner's backups go, the bundled web app
        "backup_dir",
        "web_dir",
        # the owner's accounts in integrations
        "jira_email",
        "hubspot_owner_email",
        # the owner's Obsidian vault
        *(name for name in Settings.model_fields if name.startswith("obsidian_")),
    }
)


def _team_view(values: dict, secrets_set: dict[str, bool]) -> None:
    """Blank the personal settings (in place) for a caller who is not the owner."""
    for name in PERSONAL_SETTINGS:
        field = Settings.model_fields[name]
        values[name] = "" if name == "data_dir" else field.get_default(call_default_factory=True)
        if name in secrets_set:
            secrets_set[name] = False


def _response(settings: Settings, *, personal: bool = True) -> SettingsResponse:
    """The settings as the UI reads them; `personal=False`: without PERSONAL_SETTINGS."""
    vault = settings.obsidian_vault_path
    values, secrets_set = settings.public_dict(), settings.secrets_set()
    if not personal:
        _team_view(values, secrets_set)
    return SettingsResponse(
        values=SettingsValues.model_validate(values),
        secrets_set=secrets_set,
        env_overrides=settings.env_overrides() if personal else [],
        config_file=str(config_file_path()) if personal else "",
        obsidian_vault_exists=personal and bool(vault) and Path(vault).expanduser().is_dir(),
        meeting_type_names=[t.name for t in available_types(settings)],
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(request: Request, ctx: AppContext = Depends(get_ctx)):
    """Readable by everyone signed in; on a computer that hosts a team only the owner's channels
    get the personal settings (api/auth.py `is_owner_channel`)."""
    return _response(ctx.settings, personal=is_owner_channel(ctx, request.state))


@router.put("", response_model=SettingsResponse, dependencies=ADMIN)
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
