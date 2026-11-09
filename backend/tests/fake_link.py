"""A stand-in `mnemosyne-link notes merge` for tests (export/note_merge.py), with the sidecar's
protocol: the single form, the batch form (an array in, an array of results or `{"error"}` out),
`base` (a one-off three-way merge) and `rebase`. It does a three-way merge with difflib: by lines
for multi-line texts, by words for one-line texts (a summary block). Its history is the base64
JSON of `{"combined", "app"}`, the two branches the real one keeps: what was last merged, and the
app's side as it last was, so the user's edits stay in when the vault has not changed since.

`install(tmp_path, monkeypatch)` puts it on MNEMOSYNE_LINK_BIN and returns a handle whose
`requests` are the requests it got, one per run (a list for a batch). FAKE_MERGE_FAIL makes every
run fail; FAKE_MERGE_ITEM_FAIL makes every item of a batch fail.

Run as `home` it is the sidecar of services/link.py with its control channel (link/README.md):
it prints the ready line, then answers `dial`, `hangup` and `pair` lines on stdin. The handle's
`ops` are the requests it got (without ids;
a `pair` keeps its `peer` and `card`). What it answers is set on the handle:
`endpoint_id` and `ticket` (the ready line), `pair_reply` (fields added to a successful `pair`;
default a token, the invite's ticket and a `sync` identity;
put a `card` there to answer one), and `fail` (op -> error message).

Run as `rooms` (`mnemosyne-link rooms ...`, the guest notes sidecar) it prints
`{"listen": cfg["listen"], "relay_kind": cfg.get("relay_kind", "n0")}` and answers `room_start`
(`endpoint_id` and `relay` from the config; a request's `relay` wins), `room_stop` (`closed` is
whether the room was served) and `room_list`. Each run appends its argv, the PUBLISH_* names in
its environment and its upload token file (content, octal mode) to the handle's `rooms_starts`;
its requests go to `ops` as for `home`. `fail` works too.

Run as `notes doc` it stands in for the shared notes document (notes-core), same requests and
answers (`fold`, `set_text`, `merge`; see link/src/main.rs), over a toy CRDT so tests can build
frames by hand. The handle's `doc_requests` are the requests it got; FAKE_NOTES_DOC_FAIL makes
every run fail (stderr, exit 1).

Toy frames: `0xDE` + the UTF-8 JSON `{"id": str, "ops": [op, ...]}` (build one with
`doc_frame(id, *ops)`), where an op is `["set", text]`, `["ins", pos, text]`, `["del", pos, n]`,
`["comment", comment_id, start, end]` (end null = detached), `["kind", line, "task"|
"task_done"]` (line = index of the text's line; flips its `- [ ] ` / `- [x] ` box) or `["ref",
line, value]` (sets that line's ref; "" clears it). A `set` may carry the text it was made
against and refs: `["set", text, base, {"<line of text>": ref | ""}]`; replayed on a text that
is not `base` (a frame came in between), it is a three-way merge by lines, so both stay, as the
real CRDT keeps them. Refs belong to lines and follow them through every change: lines are
matched old to new with difflib (equal ones, and a replaced block pairwise, as a reworded
line keeps its terminator); a deleted line's ref is gone.
`0xAF` + bytes is an artifact frame: accepted and kept, nothing more. A frame whose id was seen
before is skipped. The text is
every frame's ops replayed in order; markdown = the text as is, plain = the text with U+001F as
a tab. A snapshot is the base64 JSON `{"frames": [...], "artifacts": [...]}`; merge is the union
(the shared prefix of the frame lists, then the rest ordered by frame id, so it converges in either
order) and answers `frames`: what the first snapshot lacked, 0xAF artifacts then one 0xDE toy frame
per new frame. set_text emits one `["set", markdown, old text(, refs)]` frame (id = sha256 of
the old text and the new markdown, and the ops when `refs` were asked for; 16 hex), one frame of
`ref` ops when only refs change, or `[]` when nothing does. Its `refs` are as notes-core's
(keys = lines of the request's markdown; a blank line or one past the end is ignored).
mark_comment takes `first`/`last` as decimal character positions (the toy's "element ids"; a
position past the end of the text, or not a number, is "no such character") and emits one
`["comment", id, first, last + 1]` frame (id `mark-<comment>`). mark_comment_at takes `start`/`end`
(`[start, end)` in characters of the text, which is `plain`) and emits `["comment", id, start,
end]` the same way; `end <= start` or past the text is an error.
Every answer has `tasks` from the markdown's `- [ ] ` / `- [x] ` lines, as notes-core's: `line` is
the line's index (0-based, among all the text's lines) as 64 hex characters, `text` the line
after the box with `[label](url)` links reduced to their label, `links` those as `[start, end,
url]` in code points of `text`, `frame` null, `ref` the line's ref or null. Every answer has
`refs` too: `[{"line", "ref", "index", "md_line"}]` by line, `line` as a task's, `index` =
`md_line` = the line's index (the toy's markdown is its text). `set_kind` takes that `line` and a
`kind` and emits one `["kind", line, kind]` frame (id `kind-<line>-<kind>-<8 hex of the old
text's sha256>`), `[]` when the line already shows it; an index past the last line is "no such
character", a line that is not a task is "not a task".
Every answer also has `media`: the values of the text's `![caption](mnemosyne:image/<value>)` lines,
distinct, in order (unchecked: notes-core reports only valid ones, the backend checks again); such
lines stay in the markdown as they are."""

import base64
import json
import stat
import sys

HOME_PRELUDE = "import json, os, sys\n"
HOME = r"""
if sys.argv[1:2] == ["home"]:
    cfg = json.loads(os.environ.get("FAKE_LINK_CONFIG", "{}"))
    log = os.environ.get("FAKE_LINK_LOG")
    print(json.dumps({"endpoint_id": cfg["endpoint_id"], "ticket": cfg["ticket"]}), flush=True)
    dialed = {}
    for line in sys.stdin:
        req = json.loads(line)
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({k: v for k, v in req.items() if k != "id"}) + "\n")
        reply = {"id": req["id"], "op": req["op"]}
        if req["op"] in cfg.get("fail", {}):
            reply.update(ok=False, error=cfg["fail"][req["op"]])
        elif req["op"] == "dial":
            peer = cfg.get("peers", {}).get(req["ticket"], req["ticket"])
            listen = dialed.setdefault(peer, "127.0.0.1:%d" % (41000 + len(dialed)))
            reply.update(ok=True, peer=peer, listen=listen)
        elif req["op"] == "hangup":
            closed = dialed.pop(req["peer"], None) is not None
            reply.update(ok=True, peer=req["peer"], closed=closed)
        elif req["op"] == "pair":
            ticket = req["invite"].rpartition("#")[0]
            peer = cfg.get("peers", {}).get(ticket, ticket)
            reply.update(ok=True, token="token-from-peer", ticket=ticket, peer=peer)
            reply.update(cfg.get("pair_reply", {"sync": {"id": "peerpeerpeer", "name": "Desk"}}))
        else:
            reply.update(ok=False, error="unknown op")
        print(json.dumps(reply), flush=True)
    sys.exit(0)
"""

ROOMS = r"""
if sys.argv[1:2] == ["rooms"]:
    cfg = json.loads(os.environ.get("FAKE_LINK_CONFIG", "{}"))
    log = os.environ.get("FAKE_LINK_LOG")
    starts = os.environ.get("FAKE_ROOMS_STARTS")
    if starts:
        tf = None
        if "--upload-token-file" in sys.argv:
            tf = sys.argv[sys.argv.index("--upload-token-file") + 1]
        token = mode = None
        if tf and os.path.exists(tf):
            token = open(tf).read().strip()
            mode = oct(os.stat(tf).st_mode & 0o777)
        with open(starts, "a") as f:
            f.write(json.dumps({
                "argv": sys.argv[1:],
                "publish_env": sorted(k for k in os.environ if k.startswith("PUBLISH_")),
                "token": token,
                "token_mode": mode,
            }) + "\n")
    ready = {"listen": cfg["listen"], "relay_kind": cfg.get("relay_kind", "n0")}
    print(json.dumps(ready), flush=True)
    rooms = {}
    for line in sys.stdin:
        req = json.loads(line)
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({k: v for k, v in req.items() if k != "id"}) + "\n")
        reply = {"id": req["id"], "op": req["op"]}
        if req["op"] in cfg.get("fail", {}):
            reply.update(ok=False, error=cfg["fail"][req["op"]])
        elif req["op"] == "room_start":
            relay = req.get("relay") or cfg.get("relay", "https://relay.example/")
            fresh = {"endpoint_id": cfg["endpoint_id"], "relay": relay}
            room = rooms.setdefault(req["room"], fresh)
            reply.update(ok=True, endpoint_id=room["endpoint_id"], relay=room["relay"])
        elif req["op"] == "room_stop":
            reply.update(ok=True, closed=rooms.pop(req["room"], None) is not None)
        elif req["op"] == "room_list":
            reply.update(ok=True, rooms=[{"room": r, **v} for r, v in rooms.items()])
        else:
            reply.update(ok=False, error="unknown op")
        print(json.dumps(reply), flush=True)
    sys.exit(0)
"""

DOC = r"""
if sys.argv[1:] == ["notes", "doc"]:
    import base64, difflib, hashlib

    request = json.load(sys.stdin)
    log = os.environ.get("FAKE_NOTES_DOC_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(request) + "\n")
    if os.environ.get("FAKE_NOTES_DOC_FAIL"):
        sys.stderr.write("boom\n")
        sys.exit(1)

    def fail(message):
        sys.stderr.write(message + "\n")
        sys.exit(1)

    def load(snapshot, what="snapshot"):
        if snapshot is None:
            return {"frames": [], "artifacts": []}
        try:
            return json.loads(base64.b64decode(snapshot, validate=True))
        except Exception:
            fail(f"{what}: not a snapshot")

    def add(state, frames):
        seen = {f["id"] for f in state["frames"]}
        for i, b64 in enumerate(frames):
            try:
                raw = base64.b64decode(b64, validate=True)
                if raw[:1] == b"\xaf" and len(raw) > 1:
                    state["artifacts"].append(base64.b64encode(raw[1:]).decode())
                    continue
                if raw[:1] != b"\xde":
                    raise ValueError("unknown frame tag")
                frame = json.loads(raw[1:])
                assert isinstance(frame["id"], str) and isinstance(frame["ops"], list)
            except Exception as e:
                fail(f"frame {i}: {e or 'malformed'}")
            if frame["id"] not in seen:
                seen.add(frame["id"])
                state["frames"].append(frame)

    import re

    BOX = re.compile(r"^- \[( |x|X)\] ?")
    LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

    def with_box(line, kind):
        return BOX.sub("- [x] " if kind == "task_done" else "- [ ] ", line, count=1)

    # Old line index -> new line index for the lines that stayed (equal) or were reworded (a
    # replaced block, pairwise): what keeps a line's ref, as notes-core's terminators do.
    def line_map(old, new):
        a, b = old.split("\n"), new.split("\n")
        out = {}
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("equal", "replace"):
                for k in range(min(i2 - i1, j2 - j1)):
                    out[i1 + k] = j1 + k
        return out

    def reanchor(refs, old, new):
        if old == new or not refs:
            return refs
        moved = line_map(old, new)
        return {moved[i]: v for i, v in refs.items() if i in moved}

    # A three-way merge by lines; both sides' edits kept (mine first where they meet).
    def merge3(base, mine, theirs):
        b = base.split("\n")
        edits = []
        for side in (mine.split("\n"), theirs.split("\n")):
            sm = difflib.SequenceMatcher(None, b, side, autojunk=False)
            edits += [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes()
                      if tag != "equal"]
        edits.sort(key=lambda e: (e[0], e[1]))
        out, pos, last = [], 0, None
        for i1, i2, new in edits:
            if (i1, i2, new) == last:
                continue
            last = (i1, i2, new)
            out += new if i1 < pos else b[pos:i1] + new
            pos = max(pos, i2)
        return "\n".join(out + b[pos:])

    def set_refs(refs, wanted, lines):
        for n, value in wanted:
            if 0 <= n < lines:
                if value:
                    refs[n] = value
                else:
                    refs.pop(n, None)

    def replay(state):
        text, comments, refs = "", {}, {}
        for frame in state["frames"]:
            for op in frame["ops"]:
                old = text
                if op[0] == "set":
                    md = op[1]
                    base = op[2] if len(op) > 2 else text
                    text = md if base == text else merge3(base, text, md)
                    refs = reanchor(refs, old, text)
                    wanted = op[3] if len(op) > 3 else {}
                    to_text = line_map(md, text) if text != md else None
                    set_refs(
                        refs,
                        [((int(n) if to_text is None else to_text.get(int(n), -1)), v)
                         for n, v in wanted.items()],
                        len(text.split("\n")),
                    )
                    continue
                if op[0] == "ins":
                    text = text[: op[1]] + op[2] + text[op[1] :]
                elif op[0] == "del":
                    text = text[: op[1]] + text[op[1] + op[2] :]
                elif op[0] == "comment":
                    comments[op[1]] = None if op[3] is None else [op[2], op[3]]
                elif op[0] == "kind":
                    lines = text.split("\n")
                    if op[1] < len(lines) and BOX.match(lines[op[1]]):
                        lines[op[1]] = with_box(lines[op[1]], op[2])
                    text = "\n".join(lines)
                elif op[0] == "ref":
                    set_refs(refs, [(op[1], op[2])], len(text.split("\n")))
                refs = reanchor(refs, old, text)
        return text, comments, refs

    def tasks_of(text, refs):
        out = []
        for i, line in enumerate(text.split("\n")):
            box = BOX.match(line)
            if not box:
                continue
            body, plain, links = line[box.end():], "", []
            at = 0
            for m in LINK.finditer(body):
                plain += body[at : m.start()]
                links.append([len(plain), len(plain) + len(m.group(1)), m.group(2)])
                plain += m.group(1)
                at = m.end()
            plain += body[at:]
            out.append({"line": format(i, "064x"), "done": box.group(1) != " ", "text": plain,
                        "links": links, "frame": None, "ref": refs.get(i)})
        return out

    IMAGE = re.compile(r"^!\[(?:[^\]\\\n]|\\.)*\]\(mnemosyne:image/([^)\s]+)\)$")

    def media_of(text):
        out = []
        for line in text.split("\n"):
            m = IMAGE.match(line)
            if m and m.group(1) not in out:
                out.append(m.group(1))
        return out

    def answer(state, **extra):
        text, comments, refs = replay(state)
        snapshot = base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()
        n_lines = len(text.split("\n"))
        listed = [{"line": format(i, "064x"), "ref": refs[i], "index": i, "md_line": i}
                  for i in sorted(refs) if i < n_lines]
        out = {"snapshot": snapshot, "markdown": text, "plain": text.replace("\x1f", "\t"),
               "comments": comments, "tasks": tasks_of(text, refs), "media": media_of(text),
               "refs": listed}
        out.update(extra)
        print(json.dumps(out))
        sys.exit(0)

    op = request.get("op")
    if not isinstance(request.get("notes"), str) or len(request["notes"]) != 64:
        fail("notes: expected 64 hex characters")
    if op == "fold":
        state = load(request.get("snapshot"))
        add(state, request.get("frames", []))
        answer(state)
    elif op == "set_text":
        state = load(request.get("snapshot"))
        add(state, request.get("frames", []))
        old, _, old_refs = replay(state)
        md = request["markdown"]
        md_lines = md.split("\n")
        wanted = {}
        for key, value in (request.get("refs") or {}).items():
            if not key.isdigit() or not isinstance(value, str):
                fail(f"refs: {key!r} is not a line number")
            # Keys that start no line are ignored (a blank line, past the end).
            if int(key) < len(md_lines) and md_lines[int(key)].strip():
                wanted[str(int(key))] = value
        if md == old:
            ops = [["ref", int(n), v] for n, v in sorted(wanted.items(), key=lambda kv: int(kv[0]))
                   if (old_refs.get(int(n)) or "") != v]
            if not ops:
                answer(state, frames=[])
        else:
            ops = [["set", md, old] + ([wanted] if wanted else [])]
        extra = "\0" + json.dumps(ops, sort_keys=True) if wanted else ""
        fid = hashlib.sha256((old + "\0" + md + extra).encode()).hexdigest()[:16]
        frame = {"id": fid, "ops": ops}
        state["frames"].append(frame)
        raw = b"\xde" + json.dumps(frame).encode()
        answer(state, frames=[base64.b64encode(raw).decode()])
    elif op == "mark_comment":
        state = load(request.get("snapshot"))
        add(state, request.get("frames", []))
        text, _, _ = replay(state)
        found = {}
        for what in ("first", "last"):
            raw = request.get(what)
            if not (isinstance(raw, str) and raw.isdigit() and int(raw) < len(text)):
                fail(f"{what}: no such character (deleted or unknown)")
            found[what] = int(raw)
        if found["last"] < found["first"]:
            fail("last is before first")
        if not request.get("comment"):
            fail("comment: empty")
        frame = {
            "id": f"mark-{request['comment']}",
            "ops": [["comment", request["comment"], found["first"], found["last"] + 1]],
        }
        if frame["id"] in {f["id"] for f in state["frames"]}:
            answer(state, frames=[])
        state["frames"].append(frame)
        raw = b"\xde" + json.dumps(frame).encode()
        answer(state, frames=[base64.b64encode(raw).decode()])
    elif op == "mark_comment_at":
        state = load(request.get("snapshot"))
        add(state, request.get("frames", []))
        text, _, _ = replay(state)
        start, end = request.get("start"), request.get("end")
        if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (start, end)):
            fail("start/end: expected numbers")
        if end <= start:
            fail("end is not after start")
        if end > len(text):
            fail("end: past the end of the text")
        if not request.get("comment"):
            fail("comment: empty")
        frame = {
            "id": f"mark-{request['comment']}",
            "ops": [["comment", request["comment"], start, end]],
        }
        if frame["id"] in {f["id"] for f in state["frames"]}:
            answer(state, frames=[])
        state["frames"].append(frame)
        raw = b"\xde" + json.dumps(frame).encode()
        answer(state, frames=[base64.b64encode(raw).decode()])
    elif op == "set_kind":
        state = load(request.get("snapshot"))
        add(state, request.get("frames", []))
        text, _, _ = replay(state)
        kind = request.get("kind")
        if kind not in ("task", "task_done"):
            fail("kind: expected task or task_done")
        raw = request.get("line")
        try:
            if not (isinstance(raw, str) and len(raw) == 64):
                raise ValueError
            index = int(raw, 16)
        except ValueError:
            fail("line: expected 64 hex characters")
        lines = text.split("\n")
        if index >= len(lines):
            fail("line: no such character (deleted or unknown)")
        box = BOX.match(lines[index])
        if not box:
            fail("line: not a task")
        if (box.group(1) != " ") == (kind == "task_done"):
            answer(state, frames=[])
        fid = hashlib.sha256(text.encode()).hexdigest()[:8]
        frame = {"id": f"kind-{index}-{kind}-{fid}", "ops": [["kind", index, kind]]}
        state["frames"].append(frame)
        raw = b"\xde" + json.dumps(frame).encode()
        answer(state, frames=[base64.b64encode(raw).decode()])
    elif op == "merge":
        state = load(request.get("snapshot"))
        other = load(request.get("other"), "other")
        had_ids = {f["id"] for f in state["frames"]}
        had_artifacts = list(state["artifacts"])
        # The union converges in either order: the shared prefix, then the rest by frame id.
        mine, theirs = state["frames"], other["frames"]
        n = 0
        while n < min(len(mine), len(theirs)) and mine[n]["id"] == theirs[n]["id"]:
            n += 1
        rest = {f["id"]: f for f in mine[n:] + theirs[n:]}
        state["frames"] = mine[:n] + [rest[i] for i in sorted(rest)]
        state["artifacts"] = sorted(set(state["artifacts"]) | set(other["artifacts"]))
        add(state, request.get("frames", []))
        # What the local snapshot lacked, for its editors: artifacts, then the new frames
        # (the real sidecar packs the new ops into one delta).
        news = [
            base64.b64encode(b"\xaf" + base64.b64decode(a)).decode()
            for a in state["artifacts"]
            if a not in had_artifacts
        ]
        news += [
            base64.b64encode(b"\xde" + json.dumps(f).encode()).decode()
            for f in state["frames"]
            if f["id"] not in had_ids
        ]
        answer(state, frames=news)
    fail("invalid request: unknown op")
"""

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


def doc_frame(frame_id: str, *ops) -> str:
    """A toy `notes doc` delta frame (base64), e.g. `doc_frame("f1", ["ins", 0, "hi\\n"])`."""
    raw = b"\xde" + json.dumps({"id": frame_id, "ops": list(ops)}).encode()
    return base64.b64encode(raw).decode()


class Sidecar:
    def __init__(self, path, log, ops_log=None, monkeypatch=None):
        self.path = path
        self.log = log
        self.ops_log = ops_log
        self.config: dict = {
            "endpoint_id": "e" * 64,
            "ticket": "endpoint-stand-in",
            "fail": {},
            "peers": {},  # ticket -> endpoint id, for dial and pair replies
            "listen": "127.0.0.1:41999",  # `rooms`
            "relay": "https://relay.example/",
        }
        self._monkeypatch = monkeypatch
        self.sync()

    def sync(self) -> None:
        """Hand `config` to the next run of the stand-in."""
        if self._monkeypatch is not None:
            self._monkeypatch.setenv("FAKE_LINK_CONFIG", json.dumps(self.config))

    @property
    def ops(self) -> list[dict]:
        """The control requests `home` got, in order, without their ids."""
        if self.ops_log is None or not self.ops_log.exists():
            return []
        return [json.loads(x) for x in self.ops_log.read_text().splitlines()]

    @property
    def rooms_starts(self) -> list[dict]:
        """One entry per start of `rooms`: argv, publish_env (PUBLISH_* names seen), token and
        token_mode (the upload token file as the sidecar found it)."""
        log = self.log.parent / "rooms-starts.log"
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines()]

    @property
    def doc_requests(self) -> list[dict]:
        """The `notes doc` requests, in order."""
        log = self.log.parent / "notes-doc.log"
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines()]

    @property
    def requests(self) -> list:
        """What each run got: a dict (single form) or a list (batch)."""
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]


def install(tmp_path, monkeypatch) -> Sidecar:
    """Put the stand-in on MNEMOSYNE_LINK_BIN; its requests are logged."""
    script = tmp_path / "mnemosyne-link"
    first, rest = FAKE.format(python=sys.executable).split("\n", 1)
    script.write_text(first + "\n" + HOME_PRELUDE + HOME + ROOMS + DOC + rest)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "merge.log"
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(script))
    monkeypatch.setenv("FAKE_MERGE_LOG", str(log))
    monkeypatch.setenv("FAKE_NOTES_DOC_LOG", str(tmp_path / "notes-doc.log"))
    ops_log = tmp_path / "link-ops.log"
    monkeypatch.setenv("FAKE_LINK_LOG", str(ops_log))
    monkeypatch.setenv("FAKE_ROOMS_STARTS", str(tmp_path / "rooms-starts.log"))
    return Sidecar(script, log, ops_log, monkeypatch)
