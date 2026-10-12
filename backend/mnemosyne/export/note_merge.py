"""Keeping the user's edits to an exported meeting note (services/vault_sync.py `write_note`).

The note's text is merged three ways by the `mnemosyne-link notes merge` sidecar (link/): the
history of what we last wrote is the base, the freshly rendered note is the app's side and the
file in the vault the user's. Checkbox status stays out of that merge: merged as text, ticks on
both sides would come out as `[xx]` and wander onto other items. So every checkbox line goes in
with a blank box and without Obsidian Tasks' done date (`normalize`), and the statuses are put
back on the merged text afterwards (`restore`), by the text of the line.

`boxes` maps a normalized line (without its line ending) to [[status, done date], ...], one
entry per occurrence of that line in the note, in order.
"""

from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

from ..services.vault_sync import _BLOCK_ID, _BOX, _DONE_DATE

Boxes = dict[str, list[list[str]]]

MERGE_TIMEOUT = 30.0


class MergeError(Exception):
    """The sidecar is missing or failed."""


def split_lines(text: str) -> list[str]:
    """The lines of a note, each with its own ending (vault_sync.parse_ticks counts lines the
    same way); joining them gives the text back."""
    return text.splitlines(keepends=True)


def _box_line(line: str) -> tuple[str, str, str, str] | None:
    """(normalized line with its ending, key, status, done date) of a checkbox line."""
    m = _BOX.match(line)
    if m is None:
        return None
    head, status, close, rest, eol = m.groups()
    date = _DONE_DATE.search(rest)
    rest = _DONE_DATE.sub("", rest)
    key = f"{head} {close}{rest}"
    return key + (eol or ""), key, status, date.group(0) if date else ""


def normalize(text: str) -> tuple[str, Boxes]:
    """The note with every checkbox open and without done dates, and what they were."""
    out: list[str] = []
    boxes: Boxes = {}
    for line in split_lines(text):
        parsed = _box_line(line)
        if parsed is None:
            out.append(line)
            continue
        norm, key, status, date = parsed
        out.append(norm)
        boxes.setdefault(key, []).append([status, date])
    return "".join(out), boxes


def box_positions(text: str) -> dict[int, tuple[str, int]]:
    """Line index -> (key, n) for each checkbox line: its normalized text and which occurrence
    of that text it is, the address of its entry in `boxes`."""
    seen: dict[str, int] = {}
    out: dict[int, tuple[str, int]] = {}
    for i, line in enumerate(split_lines(text)):
        parsed = _box_line(line)
        if parsed is not None:
            key = parsed[1]
            out[i] = (key, seen.get(key, 0))
            seen[key] = seen.get(key, 0) + 1
    return out


def box_at(boxes: Boxes, key: str, n: int) -> list[str] | None:
    """The [status, done date] of the n-th occurrence of a normalized checkbox line."""
    entries = boxes.get(key)
    return entries[n] if entries is not None and n < len(entries) else None


def restore(merged_norm: str, app_boxes: Boxes, vault_boxes: Boxes) -> str:
    """Statuses back on the merged text. Per checkbox line: another status than open or done
    in the vault (Tasks' [/], [-]) stays unless the app has the item done; otherwise the app's
    status, else the vault's, else open. A done item takes the done date the vault's line had,
    never one the vault did not have."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for line in split_lines(merged_norm):
        m = _BOX.match(line)
        if m is None:
            out.append(line)
            continue
        head, _, close, rest, eol = m.groups()
        key = f"{head} {close}{rest}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        app = box_at(app_boxes, key, n)
        vault = box_at(vault_boxes, key, n)
        if vault is not None and vault[0] not in " xX" and not (app and app[0] in "xX"):
            status = vault[0]
        elif app is not None:
            status = app[0]
        elif vault is not None:
            status = vault[0]
        else:
            status = " "
        date = vault[1] if vault is not None and vault[0] in "xX" and status in "xX" else ""
        if date:  # before a block id: Obsidian needs that last on the line
            body = _BLOCK_ID.sub("", rest)
            rest = f"{body}{date}{rest[len(body) :]}"
        out.append(f"{head}{status}{close}{rest}{eol or ''}")
    return "".join(out)


def merge(binary: str, history: bytes | None, app: str, vault: str | None) -> tuple[bytes, str]:
    """Run `<binary> notes merge`: (the new history, the merged text). history None starts a
    history from `app` (vault must be None). Raises MergeError."""
    request = {
        "history": base64.b64encode(history).decode("ascii") if history is not None else None,
        "app": app,
        "vault": vault,
    }
    try:
        done = subprocess.run(
            [binary, "notes", "merge"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=MERGE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise MergeError(f"could not run {binary}: {e}") from e
    if done.returncode != 0:
        raise MergeError(f"{binary} notes merge exited {done.returncode}: {done.stderr.strip()}")
    try:
        reply: Any = json.loads(done.stdout.strip().splitlines()[-1])
        return base64.b64decode(reply["history"]), str(reply["text"])
    except (IndexError, KeyError, TypeError, ValueError) as e:
        raise MergeError(f"unreadable reply from {binary} notes merge: {e}") from e


def merge_many(binary: str, requests: list[dict]) -> list[tuple[bytes, str] | MergeError]:
    """Run several merges in one `<binary> notes merge` (its batch form). Each request has
    `app` and optionally `history` (bytes or None), `base` (a one-off three-way merge from this
    text), `vault` and `rebase` (see the `Merge` doc comment in link/src/main.rs). Returns, in
    order, (the new history, the merged text) or a MergeError for a request that failed.
    Raises MergeError when the whole batch failed."""
    if not requests:
        return []
    batch = []
    for r in requests:
        item: dict[str, Any] = {"app": r["app"], "vault": r.get("vault")}
        history = r.get("history")
        if history is not None:
            item["history"] = base64.b64encode(history).decode("ascii")
        if r.get("base") is not None:
            item["base"] = r["base"]
        if r.get("rebase"):
            item["rebase"] = True
        batch.append(item)
    timeout = MERGE_TIMEOUT + len(batch)
    try:
        done = subprocess.run(
            [binary, "notes", "merge"],
            input=json.dumps(batch),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise MergeError(f"could not run {binary}: {e}") from e
    if done.returncode != 0:
        raise MergeError(f"{binary} notes merge exited {done.returncode}: {done.stderr.strip()}")
    try:
        replies: Any = json.loads(done.stdout.strip().splitlines()[-1])
        if not isinstance(replies, list) or len(replies) != len(batch):
            raise ValueError("not one result per request")
    except (IndexError, TypeError, ValueError) as e:
        raise MergeError(f"unreadable reply from {binary} notes merge: {e}") from e
    out: list[tuple[bytes, str] | MergeError] = []
    for reply in replies:
        try:
            if "error" in reply:
                out.append(MergeError(str(reply["error"])))
            else:
                out.append((base64.b64decode(reply["history"]), str(reply["text"])))
        except (KeyError, TypeError, ValueError) as e:
            out.append(MergeError(f"unreadable result from {binary} notes merge: {e}"))
    return out


def lines_both_changed(final_norm: str, app_norm: str, vault_norm: str) -> int:
    """Non-blank lines of the merged text that neither side has as they are: lines both sides
    changed (or one side changed and the merge joined)."""
    known = {ln.rstrip("\r\n") for ln in split_lines(app_norm) + split_lines(vault_norm)}
    lines = (ln.rstrip("\r\n") for ln in split_lines(final_norm))
    return sum(1 for ln in lines if ln.strip() and ln not in known)
