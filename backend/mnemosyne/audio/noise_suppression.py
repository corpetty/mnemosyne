"""DeepFilterNet noise suppression on the echo-cancelled mic (what NoiseTorch did with RNNoise).

PipeWire's filter-chain module runs DeepFilterNet's LADSPA plugin (Rikorose/DeepFilterNet,
MIT/Apache-2.0, the model compiled in) on the echo canceller's output:

    mic -> WebRTC echo cancel ("mnemosyne_aec_raw") -> DeepFilterNet -> "mnemosyne_aec_source"

and the visible source keeps its node name. The chain runs in a pw-cli of its own
(audio/echo_cancel.py): the plugin aborts its process when it falls behind, and that must never
take the canceller, which our recordings read, down with it; a plain copy then takes its place.
The plugin works on one channel at 48 kHz, adds 20 ms, and turns frames it hears only noise in
into digital silence. Its "attenuation limit" caps how far noise is turned down: 100 dB removes
it. PipeWire suspends the chain while nothing records from the source, so it costs nothing until
then (about a tenth of a core while it runs).

The plugin is downloaded on first use (pinned, SHA-256 checked) into `models_dir`.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from ..services.downloads import Download, fetch

PLUGIN = Download(
    url="https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/"
    "libdeep_filter_ladspa-0.5.6-x86_64-unknown-linux-gnu.so",
    sha256="2ca3205c2911d389604a826a240e745597d50252b5cab81c8248252b335e2236",
    size=52_711_472,
    name="libdeep_filter_ladspa-0.5.6.so",
)
LABEL = "deep_filter_mono"

# The echo canceller's own source when the noise suppressor sits behind it. Not for recording:
# capture.list_devices leaves it out and valid_mic refuses it (it starts with "mnemosyne_aec").
RAW_SOURCE_NODE = "mnemosyne_aec_raw"
CAPTURE_NODE = "mnemosyne_aec_ns_capture"
SOURCE_DESCRIPTION = "Mnemosyne: mic (clean)"
# The plain copy that stands in for a crashed noise suppressor.
COPY_DESCRIPTION = "Mnemosyne: mic (echo cancelled)"

FILTER_CHAIN_PATHS = tuple(
    p.replace("module-echo-cancel", "module-filter-chain")
    for p in (
        "/usr/lib64/pipewire-0.3/libpipewire-module-echo-cancel.so",
        "/usr/lib/pipewire-0.3/libpipewire-module-echo-cancel.so",
        "/usr/lib/x86_64-linux-gnu/pipewire-0.3/libpipewire-module-echo-cancel.so",
    )
)


@dataclass(frozen=True)
class Denoise:
    """What the echo canceller runs DeepFilterNet with."""

    plugin: Path
    limit_db: int = 100


def supported() -> tuple[bool, str | None]:
    if platform.machine() not in ("x86_64", "AMD64"):
        return False, "DeepFilterNet is only set up for x86-64 computers"
    if not any(Path(p).is_file() for p in FILTER_CHAIN_PATHS):
        return False, "libpipewire-module-filter-chain.so not installed"
    return True, None


def plugin_path(models_dir: Path) -> Path | None:
    """The downloaded plugin, or None while it is not there."""
    path = Path(models_dir) / PLUGIN.name
    return path if path.is_file() else None


def fetch_plugin(models_dir: Path) -> Path:
    """Download the plugin if needed (blocking: run in a thread). Raises DownloadError."""
    return fetch(Path(models_dir), PLUGIN)


def filter_chain_command(
    denoise: Denoise | None,
    target: str = RAW_SOURCE_NODE,
    source_name: str = "mnemosyne_aec_source",
    capture_name: str = CAPTURE_NODE,
) -> str:
    """pw-cli command loading DeepFilterNet behind `target` as a virtual source, or with `denoise`
    None a plain copy of it (what stands in when the noise suppressor crashed). The capture
    side is passive: the chain only runs while something records from the source."""
    if denoise is None:
        description = COPY_DESCRIPTION
        node = "{ type = builtin name = copy label = copy }"
    else:
        description = SOURCE_DESCRIPTION
        plugin = str(denoise.plugin)
        if '"' in plugin or "\\" in plugin:
            raise ValueError(f"Cannot load the noise suppressor from {plugin!r}")
        limit = max(1, min(100, int(denoise.limit_db)))
        node = (
            f'{{ type = ladspa name = dfn plugin = "{plugin}" label = {LABEL} '
            f'control = {{ "Attenuation Limit (dB)" = {limit} }} }}'
        )
    return (
        "load-module libpipewire-module-filter-chain { "
        f'node.description = "{description}" audio.rate = 48000 '
        "audio.channels = 1 audio.position = [ MONO ] "
        f"filter.graph = {{ nodes = [ {node} ] }} "
        f'capture.props = {{ node.name = "{capture_name}" node.passive = true '
        f'target.object = "{target}" node.dont-reconnect = true }} '
        f'playback.props = {{ node.name = "{source_name}" node.description = "{description}" '
        "media.class = Audio/Source } "
        "}\n"
    )
