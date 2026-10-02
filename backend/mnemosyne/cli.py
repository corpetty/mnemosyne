"""Command-line entry point: `mnemosyne-backend --host 127.0.0.1 --port 8008`."""

import argparse
import logging
import os
import sys
import warnings


def quiet_known_warnings() -> None:
    """Silence warnings that are expected and harmless here.

    pyannote warns on import that torchcodec (its own audio decoder) cannot load, e.g. on
    Fedora 44 whose FFmpeg 8 torchcodec 0.7 does not support. We never use that decoder:
    audio reaches pyannote as in-memory waveforms. The message starts with a newline, so
    the pattern must allow leading whitespace ('.*' does not cross a newline).
    """
    warnings.filterwarnings("ignore", message=r"\s*torchcodec is not installed correctly")
    # pyannote turns TF32 off for reproducibility and says so on every run.
    warnings.filterwarnings("ignore", message=r"\s*TensorFloat-32 \(TF32\) has been disabled")
    # pyannote's statistics pooling on a one-frame chunk (very short speech turns).
    warnings.filterwarnings("ignore", message=r"\s*std\(\): degrees of freedom is <= 0")


def users_command(argv: list[str]) -> None:
    """`mnemosyne-backend users ...`: people on a team server, for the first admin (who has
    nobody to invite them) and for scripts. Works while the server runs (services/users.py)."""
    from datetime import datetime

    from .access import ROLES
    from .config import load_settings
    from .services.users import UserService

    parser = argparse.ArgumentParser(prog="mnemosyne-backend users")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", help="add a person and print their invite link")
    add.add_argument("name")
    add.add_argument("--email", default="")
    add.add_argument("--role", choices=ROLES, default="member")
    add.add_argument("--address", default="https://<this server>", help="the address people open")
    inv = sub.add_parser("invite", help="a new invite link for someone (another computer)")
    inv.add_argument("user_id")
    inv.add_argument("--address", default="https://<this server>")
    sub.add_parser("list", help="everyone, with their id and role")
    args = parser.parse_args(argv)

    users = UserService(load_settings().data_dir / "users.json")
    if args.action == "list":
        for u in users.list():
            state = " (disabled)" if u.disabled else ""
            print(f"{u.id}  {u.role:<8}  {u.name} <{u.email}>{state}")
        return
    if args.action == "add":
        try:
            user = users.add(args.name, args.email, args.role)
        except ValueError as e:
            raise SystemExit(str(e)) from e
        user_id = user.id
    else:
        user_id = args.user_id
    try:
        code, expires = users.invite(user_id)
    except KeyError:
        hint = "see: mnemosyne-backend users list"
        raise SystemExit(f"No person with id {user_id} ({hint})") from None
    until = datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
    print(f"Invite link (works once, until {until}):")
    print(f"  {args.address.rstrip('/')}/?invite={code}")


def prefetch_command(argv: list[str]) -> None:
    """Download the CPU engines' models (Parakeet, the speaker models, the search model) into a
    folder laid out for the offline AppImage (scripts/build-offline-appimage.sh): <dest>/hf is a
    Hugging Face cache, <dest>/data/models is a `models_dir`. The shell copies them into place on
    first launch (install_offline in src-tauri/src/lib.rs)."""
    import asyncio
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="mnemosyne-backend prefetch")
    parser.add_argument("dest", help="the folder to fill")
    dest = Path(parser.parse_args(argv).dest).resolve()
    # Before anything imports huggingface_hub, which reads these once.
    os.environ["HF_HUB_CACHE"] = str(dest / "hf")
    os.environ["MNEMOSYNE_CONFIG_FILE"] = str(dest / "unused-config.toml")  # not this user's
    from .config import Settings
    from .search.embeddings import build_embedder
    from .transcription.registry import build_diarizer, build_transcriber

    settings = Settings(data_dir=dest / "data", transcriber="parakeet", diarizer="onnx")

    async def fetch() -> None:
        await build_transcriber(settings, "parakeet").load()
        diarizer = build_diarizer(settings)
        if diarizer is not None:
            await diarizer.load()

    asyncio.run(fetch())
    build_embedder(settings)
    for folder in (dest / "hf", settings.models_dir):
        size = sum(
            f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink()
        )
        print(f"{folder}: {size / 1e6:.0f} MB")


def main() -> None:
    quiet_known_warnings()
    if sys.argv[1:2] == ["users"]:
        users_command(sys.argv[2:])
        return
    if sys.argv[1:2] == ["prefetch"]:
        prefetch_command(sys.argv[2:])
        return
    import uvicorn

    from .api.app import create_app
    from .config import STARTUP_PROBLEMS, load_settings
    from .logs import setup_logging

    parser = argparse.ArgumentParser(description="Mnemosyne backend")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (0.0.0.0 for server mode; set API_TOKEN)"
    )
    parser.add_argument("--port", type=int, default=8008, help="Bind port")
    args = parser.parse_args()
    # The phone page (routes/mobile.py) tells whether other devices can reach us.
    os.environ["MNEMOSYNE_BIND_HOST"] = args.host
    os.environ["MNEMOSYNE_BIND_PORT"] = str(args.port)
    settings = load_settings()
    setup_logging(settings.data_dir)
    for note in STARTUP_PROBLEMS:  # found before the log file was set up
        logging.getLogger("mnemosyne").error("%s", note)
    sock = bind(args.host, args.port)
    config = uvicorn.Config(create_app(settings), host=args.host, port=args.port)
    uvicorn.Server(config).run(sockets=[sock])


def bind(host: str, port: int):
    """Take the port before anything starts. uvicorn binds only after the app's startup, and a
    second backend's startup is not harmless: it would stop the first one's recorders (to
    recover their "interrupted" recording) and its echo canceller, then fail to bind."""
    import logging
    import socket
    import sys

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        return socket.create_server((host, port), family=family)
    except OSError as e:
        logging.getLogger("mnemosyne").error(
            "Cannot listen on %s:%d (%s): another Mnemosyne backend is probably running. "
            "Not starting a second one.",
            host,
            port,
            e.strerror or e,
        )
        sys.exit(3)


if __name__ == "__main__":
    main()
