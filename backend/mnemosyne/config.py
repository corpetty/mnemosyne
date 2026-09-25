"""Application settings.

Precedence, highest first:
1. Environment variables (including backend/.env, loaded below).
2. The user config file (TOML) at MNEMOSYNE_CONFIG_FILE or
   $XDG_CONFIG_HOME/mnemosyne/config.toml.
3. Defaults.

The Settings UI writes the config file. Anything also set in the environment
keeps winning until it is removed from the environment; `env_overrides()`
tells the UI which fields are in that state.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import tomli_w
from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

logger = logging.getLogger(__name__)


def backend_dir() -> Path:
    """The directory holding pyproject.toml and .env (backend/ in a checkout)."""
    return Path(__file__).resolve().parents[1]


# Load backend/.env so its variables are visible to the env settings source.
load_dotenv(backend_dir() / ".env")


def default_data_dir() -> Path:
    return backend_dir().parent / "data"


def config_file_path() -> Path:
    override = os.environ.get("MNEMOSYNE_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "mnemosyne" / "config.toml"


SECRET_FIELDS = frozenset(
    {
        "hf_token",
        "openai_api_key",
        "anthropic_api_key",
        "remote_stt_api_key",
        "api_token",
        "calendar_ics_url",
        "github_token",
        "linear_api_key",
        "jira_api_token",
        "slack_webhook_url",
        "matrix_access_token",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Paths
    data_dir: Path = Field(
        default_factory=default_data_dir,
        validation_alias=AliasChoices("MNEMOSYNE_DATA_DIR", "data_dir"),
    )

    # Transcription pipeline
    # The first-run setup wizard has been completed or skipped.
    setup_complete: bool = False

    transcriber: str = "whisperx"  # whisperx | parakeet | remote
    diarizer: str = "pyannote"  # pyannote | none
    language: str = ""  # blank = auto-detect
    min_speakers: int | None = None
    max_speakers: int | None = 10
    auto_transcribe: bool = True
    # Names and jargon, one per line; "wrong -> right" lines are applied as corrections.
    glossary: str = ""
    # Also let the LLM fix misheard glossary terms after transcription (default provider).
    glossary_llm_correct: bool = False
    auto_summarize: bool = False  # queue a summary after each transcription
    auto_name_sessions: bool = True  # rename untitled sessions from the summary title
    # When mic and system audio are captured separately, the mic file is
    # labelled with this name instead of being diarized.
    local_speaker_name: str = "Me"
    # PipeWire WebRTC echo cancellation (virtual "echo cancelled" mic source).
    echo_cancel: bool = False
    # Drop mic segments that repeat what came out of the speakers (no headphones).
    echo_dedup: bool = True
    echo_similarity: float = 0.8
    # Voice profiles: auto-label diarized speakers whose embedding matches a
    # known speaker with cosine similarity >= this.
    auto_label_speakers: bool = True
    speaker_match_threshold: float = 0.6
    remote_speaker_name: str = "Remote"  # live label for the system channel
    per_source_transcription: bool = True

    # Live transcription while recording (provisional; replaced by the final job)
    live_transcription: bool = True
    live_transcriber: str = "parakeet"  # parakeet | whisperx | remote
    live_interval_seconds: float = 5.0
    # Label live lines by voice (needs the pyannote diarizer's torch stack).
    live_diarization: bool = True
    # CPU threads for the live transcriber (Parakeet/ONNX). It re-runs every few seconds for
    # the whole recording; all cores would starve the desktop.
    live_threads: int = 2
    # Live audio quieter than this (dBFS) is not sent to the transcriber at all.
    live_silence_db: float = -55.0
    # Transcribe less often (up to 4x the interval) while the live transcript falls behind
    # or the CPU is busy, instead of competing with the desktop.
    live_adaptive: bool = True
    # Live copilot: running notes (summary, decisions, action items, open questions) from
    # the live transcript while recording, refreshed at most this often, using the default
    # model. Needs the live transcript.
    copilot: bool = True
    copilot_interval_seconds: int = 180

    # Alert (toast + desktop notification) when one of these comma-separated words is
    # heard in the live transcript from anyone but your own mic. Usually your name.
    mention_keywords: str = ""
    live_speaker_threshold: float = 0.45

    # whisperx transcriber
    hf_token: str = ""
    whisper_model_size: str = "medium.en"
    whisper_compute_type: str = "float16"
    whisper_batch_size: int = 8
    whisper_vad: str = "silero"  # silero | pyannote

    # parakeet transcriber (ONNX Runtime, no torch)
    parakeet_model: str = "nemo-parakeet-tdt-0.6b-v3"
    # "int8" (default: 4x smaller, single file) or "" (fp32, external-data file)
    parakeet_quantization: str = "int8"
    onnx_provider: str = "cpu"  # cpu | cuda

    # remote transcriber: any OpenAI-compatible /audio/transcriptions server
    remote_stt_url: str = ""  # e.g. http://127.0.0.1:8484/v1
    remote_stt_model: str = "whisper-1"
    remote_stt_api_key: str = ""

    # pyannote diarizer
    diarization_model: str = "pyannote/speaker-diarization-community-1"

    # LLM providers
    ollama_url: str = "http://localhost:11434"
    vllm_url: str = "http://localhost:8000"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    default_provider: str = "ollama"
    default_model: str = ""
    # Before sending anything to a cloud provider (openai, anthropic), replace emails, phone
    # numbers and the names of people in your meetings with placeholders, and put them back
    # in the reply.
    cloud_redaction: bool = False

    # Server mode: when set, every /api request and the WebSocket must carry it
    # (Authorization: Bearer <token>, or ?token=). /health stays open.
    api_token: str = ""

    # Calendar: private ICS address (or a local .ics path). Recordings started during
    # a meeting are named after it and remember its attendees.
    calendar_ics_url: str = ""
    calendar_auto_name: bool = True

    # Storage: delete audio (never transcripts) of transcribed sessions older than
    # this many days. 0 keeps audio forever.
    audio_retention_days: int = 0

    # GitHub issues from action items
    github_repo: str = ""  # owner/name
    github_token: str = ""  # fine-grained token with Issues: read and write on that repo
    github_labels: str = "meeting-action"  # comma-separated

    # Linear issues from action items: a personal API key and the team key (e.g. "ENG").
    linear_api_key: str = ""
    linear_team: str = ""

    # Jira Cloud issues from action items.
    jira_url: str = ""  # https://yourteam.atlassian.net
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project: str = ""  # project key, e.g. "OPS"
    jira_issue_type: str = "Task"

    # Posting follow-ups: a Slack incoming webhook, and/or a Matrix room.
    slack_webhook_url: str = ""
    matrix_homeserver: str = ""  # https://matrix.org
    matrix_access_token: str = ""
    matrix_room_id: str = ""  # !abc123:matrix.org

    # Search and Ask: add meaning-based matches to keyword search (model2vec, CPU).
    semantic_search: bool = True
    embedding_model: str = "minishlab/potion-retrieval-32M"

    # Summaries
    summary_style: str = "meeting"  # see summarization/prompts.py STYLES
    summary_instructions: str = ""  # appended to every summary prompt
    # Transcripts longer than this (formatted characters, about 4 per token) are summarized
    # in parts and then merged, so small context windows still work. 0 never splits.
    summary_chunk_chars: int = 40000

    # Auto-record: "off", "ask" (offer to record when a meeting app starts using the mic or a
    # calendar meeting starts) or "auto" (just start). Auto-started recordings stop after the
    # app stops (60 s grace), 5 minutes after the calendar meeting ends, or after
    # auto_stop_silence_minutes of silence on every source (0 = never).
    auto_record: str = "off"
    auto_stop_silence_minutes: int = 10
    auto_record_ignore_apps: str = "easyeffects, jamesdsp, pavucontrol, obs, audacity"

    # Digest: a scheduled digest of the week's meetings. digest_weekday is 0 (Monday)
    # to 6 (Sunday), -1 turns the schedule off; it runs at or after digest_hour.
    digest_weekday: int = -1
    digest_hour: int = 17

    # Export
    obsidian_vault_path: str = ""
    obsidian_subfolder: str = "meetings/mnemosyne"
    obsidian_tags: str = "meeting, mnemosyne"  # comma-separated
    obsidian_link_people: bool = True  # participants as [[Name]] links
    obsidian_include_transcript: bool = True
    obsidian_auto_export: bool = False  # export after every summary
    # Also keep a note per person (meetings, tasks) in <subfolder>/people/, unless the vault
    # already has a note with that person's name.
    obsidian_people_notes: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml = TomlConfigSettingsSource(settings_cls, toml_file=config_file_path())
        return (init_settings, env_settings, toml)

    # ---- helpers -------------------------------------------------------

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mnemosyne.db"

    def env_overrides(self) -> list[str]:
        """Field names whose value is being forced by the environment."""
        names = []
        for name in type(self).model_fields:
            if name == "data_dir":
                if os.environ.get("MNEMOSYNE_DATA_DIR"):
                    names.append(name)
                continue
            if os.environ.get(name.upper()):
                names.append(name)
        return names

    def secrets_set(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in SECRET_FIELDS}

    def public_dict(self) -> dict:
        """Serializable view with secrets blanked."""
        data = self.model_dump(mode="json")
        for name in SECRET_FIELDS:
            data[name] = ""
        return data


def load_settings() -> Settings:
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist all settings to the TOML config file (mode 0600)."""
    path = path or config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.model_dump(mode="json")
    data["data_dir"] = str(settings.data_dir)
    # TOML has no null; omitted keys fall back to defaults on load.
    data = {k: v for k, v in data.items() if v is not None}
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    logger.info("Saved settings to %s", path)
    return path
