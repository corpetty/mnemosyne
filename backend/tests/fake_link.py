"""A stand-in `mnemosyne-link notes merge` for tests (export/note_merge.py), with the sidecar's
protocol: the single form, the batch form (an array in, an array of results or `{"error"}` out),
`base` (a one-off three-way merge) and `rebase`. It does a three-way merge with difflib: by lines
for multi-line texts, by words for one-line texts (a summary block). Its history is the base64
JSON of `{"combined", "app"}`, the two branches the real one keeps: what was last merged, and the
app's side as it last was, so the user's edits stay in when the vault has not changed since.

`install(tmp_path, monkeypatch)` puts it on MNEMOSYNE_LINK_BIN and returns a handle whose
`requests` are the requests it got, one per run (a list for a batch). FAKE_MERGE_FAIL makes every
run fail; FAKE_MERGE_ITEM_FAIL makes every item of a batch fail."""

import json
import stat
import sys

FAKE = r"""#!{python}
import base64, difflib, json, os, re, sys

assert sys.argv[1:] == ["notes", "merge"], sys.argv
request = json.load(sys.stdin)
log = os.environ.get("FAKE_MERGE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(request) + "\n")
if os.environ.get("FAKE_MERGE_FAIL"):
    sys.stderr.write("boom\n")
    sys.exit(1)


def tokens(text, by_words):
    if by_words:
        return re.findall(r"\w+|\s+|[^\w\s]", text)
    return text.splitlines(keepends=True)


def edits(base, other):
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def merge3(base, app, vault):
    by_words = all(len(t.splitlines()) <= 1 for t in (base, app, vault))
    base, app, vault = (tokens(t, by_words) for t in (base, app, vault))
    both = sorted(edits(base, app) + edits(base, vault), key=lambda e: (e[0], e[1]))
    out, pos, last = [], 0, None
    for i1, i2, new in both:
        if (i1, i2, new) == last:
            continue
        last = (i1, i2, new)
        if i1 < pos:  # overlapping edits: both are kept
            out += new
        else:
            out += base[pos:i1] + new
        pos = max(pos, i2)
    return "".join(out + base[pos:])


def pack(combined, app):
    return base64.b64encode(json.dumps({{"combined": combined, "app": app}}).encode()).decode()


def merge_with(history, app, vault, rebase):
    if history is None:
        if vault is not None:
            raise ValueError("a vault version needs a history to merge against")
        return pack(app, app), app
    kept = json.loads(base64.b64decode(history))
    combined = kept["combined"]
    old_app = combined if rebase else kept["app"]
    user = vault if vault is not None and vault != combined else combined
    text = merge3(old_app, app, user)
    return pack(text, app), text


def merge_one(r):
    if not isinstance(r, dict) or "app" not in r:
        raise ValueError("invalid request")
    unknown = set(r) - {{"history", "base", "app", "vault", "rebase"}}
    if unknown:
        raise ValueError(f"unknown fields {{sorted(unknown)}}")
    history, base = r.get("history"), r.get("base")
    if history is not None and base is not None:
        raise ValueError("base and history cannot both be given")
    if base is not None:
        history, _ = merge_with(None, base, None, False)
    history, text = merge_with(history, r["app"], r.get("vault"), bool(r.get("rebase")))
    return {{"history": history, "text": text}}


if isinstance(request, list):
    out = []
    for item in request:
        try:
            if os.environ.get("FAKE_MERGE_ITEM_FAIL"):
                raise ValueError("item boom")
            out.append(merge_one(item))
        except (ValueError, KeyError, TypeError) as e:
            out.append({{"error": str(e)}})
    print(json.dumps(out))
else:
    assert set(request) == {{"history", "app", "vault"}}, request
    try:
        print(json.dumps(merge_one(request)))
    except ValueError as e:
        sys.stderr.write(f"{{e}}\n")
        sys.exit(1)
"""


class Sidecar:
    def __init__(self, path, log):
        self.path = path
        self.log = log

    @property
    def requests(self) -> list:
        """What each run got: a dict (single form) or a list (batch)."""
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]


def install(tmp_path, monkeypatch) -> Sidecar:
    """Put the stand-in on MNEMOSYNE_LINK_BIN; its requests are logged."""
    script = tmp_path / "mnemosyne-link"
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "merge.log"
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(script))
    monkeypatch.setenv("FAKE_MERGE_LOG", str(log))
    return Sidecar(script, log)
