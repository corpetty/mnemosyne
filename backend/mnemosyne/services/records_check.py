"""Check a records export on its own: `mnemosyne-backend records verify <export.zip or folder>`.

Pure: it reads the zip (or the unpacked folder) and imports only storage/records.py, never the
app, the database or the sidecar, so an auditor can run it years later on any computer.

What it checks (docs/plans/2026-11-09-records-by-frontier.md, 3.9):

- SHA256SUMS: every listed file is there and matches, and no file is missing from the list.
- Per meeting (a folder with record.json / seals.json): the chain in seals.json recomputes (format 1
  rows with `chain_hash`, format 2 rows with `chain_hash_v2`), each row follows the one before, each
  format 2 manifest hashes to its row and passes `compare_step` against the previous one;
  record.json's `chain_head`, `format` and `manifest` are the last seal's.
- The last seal's part digests recompute from the files record.json names. If record.json says the
  meeting was `pending` (the app changed it after the last seal) a part that differs is a note, not
  a failure: those changes are simply not covered yet. Otherwise it is a problem.
- Audio in `audio/` recomputes against the manifest (an export holds PLAINTEXT audio, even when the
  app keeps it encrypted); erased audio is listed as erased, never as missing.
- A format 1 last seal is checked as it always was: transcript + summary, and audio files that are
  plain (sealed as stored; a file sealed encrypted cannot be compared and is noted).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from ..storage import records as r

NOT_COVERED = "changes after the last seal are not covered"


class Unreadable(Exception):
    """The export itself could not be opened (exit code 2)."""


@dataclass
class Erased:
    name: str
    sha256: str
    at: str = ""
    by: str = ""
    reason: str = ""


@dataclass
class MeetingResult:
    folder: str
    id: str = ""
    name: str = ""
    seals: int = 0
    format: int = 0
    chain_head: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    erased: list[Erased] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class ExportResult:
    source: str
    problems: list[str] = field(default_factory=list)  # about the export as a whole
    meetings: list[MeetingResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(m.ok for m in self.meetings)

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "problems": self.problems,
            "meetings": [{**asdict(m), "ok": m.ok} for m in self.meetings],
        }


class _Files:
    """Read access to an export, a zip or a folder: names are posix paths below its root."""

    def __init__(self, path: Path):
        self._zip: zipfile.ZipFile | None = None
        self._root: Path | None = None
        try:
            if path.is_dir():
                self._root = path
                self.names = sorted(
                    p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()
                )
            else:
                self._zip = zipfile.ZipFile(path)
                self.names = [n for n in self._zip.namelist() if not n.endswith("/")]
        except (OSError, zipfile.BadZipFile) as e:
            raise Unreadable(f"Cannot read {path}: {e}") from e
        self._set = set(self.names)

    def has(self, name: str) -> bool:
        return name in self._set

    def read(self, name: str) -> bytes:
        try:
            if self._zip is not None:
                return self._zip.read(name)
            assert self._root is not None
            return (self._root / name).read_bytes()
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as e:
            raise Unreadable(f"Cannot read {name}: {e}") from e

    def sha256(self, name: str) -> str:
        return hashlib.sha256(self.read(name)).hexdigest()


def check_export(path: str | Path) -> ExportResult:
    """Check an export zip or an unpacked export folder. Raises `Unreadable` when it cannot be
    opened at all; everything else is reported in the result."""
    path = Path(path)
    files = _Files(path)
    result = ExportResult(source=str(path))
    folders = sorted(
        {
            PurePosixPath(n).parts[0]
            for n in files.names
            if len(PurePosixPath(n).parts) > 1
            and PurePosixPath(n).name in ("record.json", "seals.json", "meeting.json")
        }
    )
    by_folder = {f: MeetingResult(folder=f) for f in folders}
    result.meetings = list(by_folder.values())

    def report(name: str, problem: str) -> None:
        parts = PurePosixPath(name).parts
        target = by_folder.get(parts[0]) if len(parts) > 1 else None
        (target.problems if target else result.problems).append(problem)

    _check_sums(files, report)
    if not folders:
        result.problems.append("The export holds no meetings")
    for m in result.meetings:
        try:
            _check_meeting(files, m)
        except Unreadable as e:
            m.problems.append(str(e))
    return result


def _check_sums(files: _Files, report: Callable[[str, str], None]) -> None:
    if not files.has("SHA256SUMS"):
        report("SHA256SUMS", "SHA256SUMS is missing")
        return
    listed: dict[str, str] = {}
    for line in files.read("SHA256SUMS").decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64:
            report("SHA256SUMS", f"SHA256SUMS has a line that cannot be read: {line[:80]!r}")
            continue
        listed[name] = digest
    for name, digest in listed.items():
        if not files.has(name):
            report(name, f"{name} is listed in SHA256SUMS but is not in the export")
        elif files.sha256(name) != digest:
            report(name, f"{name} was changed: it does not match SHA256SUMS")
    for name in files.names:
        if name != "SHA256SUMS" and name not in listed:
            report(name, f"{name} is not listed in SHA256SUMS")


def _json(files: _Files, folder: str, name: str, m: MeetingResult):
    path = f"{folder}/{name}"
    if not files.has(path):
        m.problems.append(f"{path} is missing")
        return None
    try:
        return json.loads(files.read(path))
    except ValueError as e:
        m.problems.append(f"{path} is not valid JSON: {e}")
        return None


def _check_meeting(files: _Files, m: MeetingResult) -> None:
    folder = m.folder
    meeting = _json(files, folder, "meeting.json", m)
    if isinstance(meeting, dict):
        m.id, m.name = str(meeting.get("id", "")), str(meeting.get("name", ""))
    seals_doc = _json(files, folder, "seals.json", m)
    record = _json(files, folder, "record.json", m)
    seals = seals_doc.get("seals") if isinstance(seals_doc, dict) else None
    if not isinstance(seals, list):
        if seals_doc is not None:
            m.problems.append(f"{folder}/seals.json has no list of seals")
        return
    m.seals = len(seals)
    _check_chain(m, seals)
    last = seals[-1] if seals else None
    m.format = int(last.get("format", 0)) if last else 0
    m.chain_head = last.get("chain", "") if last else ""
    if not isinstance(record, dict):
        if record is not None:
            m.problems.append(f"{folder}/record.json is not an object")
        return
    if record.get("chain_head", "") != m.chain_head:
        m.problems.append("record.json's chain head is not the last seal's")
    if record.get("format", 0) != m.format:
        m.problems.append("record.json's format is not the last seal's")
    if last is None:
        m.notes.append("The meeting was never sealed")
        return
    if m.format != 2:
        _check_v1(files, m, last, record)
        return
    manifest = last.get("manifest")
    if record.get("manifest") != manifest:
        m.problems.append("record.json's manifest is not the last seal's")
    if not isinstance(manifest, dict) or r.manifest_problems(manifest):
        return  # the chain check named it
    verification = record.get("verification")
    pending = bool(isinstance(verification, dict) and verification.get("pending"))
    _check_parts(files, m, record, manifest, pending)
    _check_audio(files, m, manifest, pending)
    m.erased = [
        Erased(
            name,
            e.get("sha256", ""),
            str(e.get("at", "")),
            str(e.get("by", "")),
            str(e.get("reason", "")),
        )
        for name, e in sorted(manifest.get("erased", {}).items())
    ]


def _check_chain(m: MeetingResult, seals: list[dict]) -> None:
    prev, prev_manifest = "", None
    for i, s in enumerate(seals, 1):
        try:
            if s["prev"] != prev:
                m.problems.append(f"Seal {i} does not follow seal {i - 1}")
            changed = f"Seal {i} ({str(s['at'])[:16]}) was changed after it was made"
            if s["format"] == 2:
                man = s.get("manifest")
                if (
                    not isinstance(man, dict)
                    or r.manifest_problems(man)
                    or r.manifest_hash(man) != s["content_hash"]
                    or r.chain_hash_v2(s["prev"], s["content_hash"], s["at"], s["reason"])
                    != s["chain"]
                ):
                    m.problems.append(changed)
                    prev_manifest = None
                elif m.id and man["session"] != m.id:
                    m.problems.append(f"Seal {i} ({str(s['at'])[:16]}) is another meeting's")
                    prev_manifest = None
                else:
                    if prev_manifest is not None:
                        m.problems.extend(r.compare_step(prev_manifest, man, i))
                    prev_manifest = man
            else:
                if (
                    r.chain_hash(s["prev"], s["content_hash"], s["audio"], s["at"], s["reason"])
                    != s["chain"]
                ):
                    m.problems.append(changed)
                prev_manifest = None
            prev = s["chain"]
        except (KeyError, TypeError):
            m.problems.append(f"Seal {i} is malformed")
            prev, prev_manifest = "", None


def _payload(files: _Files, m: MeetingResult, record: dict, part: str):
    """One part's payload from the file record.json names for it; None when it cannot be."""
    name = (record.get("parts") or {}).get(part)
    if not isinstance(name, str):
        m.problems.append(f"record.json does not say which file holds the {part}")
        return None
    path = f"{m.folder}/{name}"
    if not files.has(path):
        m.problems.append(f"{path} is missing, the {part} cannot be checked")
        return None
    raw = files.read(path)
    try:
        if part in ("summary", "notes"):
            text = raw.decode()
            return r.summary_payload(text) if part == "summary" else r.notes_payload(text)
        data = json.loads(raw)
        if part == "transcript":
            return r.transcript_payload(data)
        if part == "summary_data":
            return r.summary_data_payload(data)
        if part == "comments":
            return r.comments_payload(data)
        if part == "meta":
            return r.meta_payload(data)
        return r.external_notes_payload(data)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        m.problems.append(f"{path} cannot be read as the {part}: {e}")
        return None


def _check_parts(
    files: _Files, m: MeetingResult, record: dict, manifest: dict, pending: bool
) -> None:
    sealed = manifest["parts"]
    payloads = {part: _payload(files, m, record, part) for part in r.PARTS}
    for part in r.PARTS:
        payload = payloads[part]
        if payload is None:
            continue
        if r.part_digest(part, payload) != sealed[part]:
            if pending:
                m.notes.append(f"{part}: {NOT_COVERED}")
            else:
                name = record["parts"][part]
                m.problems.append(f"The {part} ({name}) differs from the last seal")
    # content_v1 follows from the transcript and summary: a mismatch with equal parts is a forgery
    transcript, summary = payloads["transcript"], payloads["summary"]
    if transcript is not None and summary is not None and not pending:
        now = r.content_hash({"transcript": transcript, "summary": summary})
        if now != manifest["content_v1"] and not any("differs" in p for p in m.problems):
            m.problems.append("The transcript and summary do not match the seal's content hash")


def _audio_files(files: _Files, folder: str) -> dict[str, str]:
    """Stored file name (as the manifest keys it) -> path in the export."""
    prefix = f"{folder}/audio/"
    return {r.audio_key(n[len(prefix) :]): n for n in files.names if n.startswith(prefix)}


def _check_audio(files: _Files, m: MeetingResult, manifest: dict, pending: bool) -> None:
    if not manifest["audio_here"]:
        return
    present = _audio_files(files, m.folder)
    current = {}
    for key, path in present.items():
        data = files.read(path)
        current[key] = r.audio_entry(hashlib.sha256(data).hexdigest(), len(data))
    for d in r.compare_audio(manifest["audio"], manifest["erased"], current):
        if d.kind == "added" and pending:
            m.notes.append(f"audio/{d.name}: {NOT_COVERED}")
        elif d.kind == "missing":
            m.problems.append(f"Audio file {d.name} is in the record but not in the export")
        elif d.kind == "differs":
            m.problems.append(f"Audio file {d.name} differs from the record")
        else:
            m.problems.append(f"Audio file {d.name} is in the export but not in the record")
    for key, entry in manifest["audio"].items():
        if key in current and current[key]["sha256"] == entry["sha256"]:
            if current[key]["bytes"] != entry["bytes"]:
                m.problems.append(f"Audio file {key} has a different length than recorded")


def _check_v1(files: _Files, m: MeetingResult, last: dict, record: dict) -> None:
    """A format 1 last seal: transcript and summary, and the audio files that are plain."""
    transcript = _payload(files, m, record, "transcript")
    summary = _payload(files, m, record, "summary")
    if transcript is not None and summary is not None:
        now = r.content_hash({"transcript": transcript, "summary": summary})
        if now != last.get("content_hash"):
            m.problems.append("The transcript or summary differs from the last seal")
    present = _audio_files(files, m.folder)
    for name, digest in (last.get("audio") or {}).items():
        key = r.audio_key(name)
        if name.endswith(r.ENCRYPTED_SUFFIX):
            m.notes.append(f"audio/{key}: sealed encrypted, cannot be compared with the export")
        elif key not in present:
            m.problems.append(f"Audio file {name} is in the record but not in the export")
        elif files.sha256(present[key]) != digest:
            m.problems.append(f"Audio file {name} differs from the record")


def format_text(result: ExportResult) -> str:
    """One line per meeting ("ok" or its problems), then a summary."""
    lines = []
    for p in result.problems:
        lines.append(f"export: {p}")
    for m in result.meetings:
        label = f"{m.folder}: "
        if m.ok:
            head = m.chain_head[:16] + "..." if m.chain_head else "no seals"
            lines.append(f"{label}ok ({m.seals} seals, format {m.format}, chain head {head})")
        else:
            lines.append(f"{label}PROBLEMS")
            lines.extend(f"    - {p}" for p in m.problems)
        for e in m.erased:
            who = f" by {e.by}" if e.by else ""
            why = f": {e.reason}" if e.reason else ""
            lines.append(f"    erased: {e.name} (deleted {e.at[:10]}{who}{why}; sha256 {e.sha256})")
        lines.extend(f"    note: {n}" for n in m.notes)
    bad = sum(1 for m in result.meetings if not m.ok)
    total = len(result.meetings)
    tail = "all checks passed" if result.ok else "PROBLEMS FOUND"
    lines.append(f"{total} meetings checked, {total - bad} ok, {bad} with problems: {tail}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """`records verify <export> [--json]`: exit 0 ok, 1 problems, 2 unreadable."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="mnemosyne-backend records verify")
    parser.add_argument("export", help="the export zip, or the folder it was unpacked into")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        result = check_export(args.export)
    except Unreadable as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result.to_json(), indent=2) if args.json else format_text(result))
    return 0 if result.ok else 1
