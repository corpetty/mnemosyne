"""Keeping meetings away from cloud LLMs: local-only meetings and redaction.

`CLOUD_PROVIDERS` are the ones that send text off this machine to a third party. Ollama and
vLLM are treated as local (they may run on another machine you control)."""

from __future__ import annotations

import re
from collections.abc import Callable

CLOUD_PROVIDERS = frozenset({"openai", "anthropic"})

LOCAL_ONLY_ERROR = (
    "This meeting is marked local-only, so it is never sent to {provider}. "
    "Use Ollama or vLLM, or turn off local-only for this meeting."
)


def is_cloud(provider: str) -> bool:
    return provider in CLOUD_PROVIDERS


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# 9+ digits with the usual separators; avoids timestamps like 12:30 and short numbers.
_PHONE = re.compile(r"(?<![\w:])\+?\d[\d ()./-]{7,}\d(?![\w:])")
_DATE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_GROUPED = re.compile(r"^\d{1,3}(?:[ .]\d{3})+$")  # 1 000 000, 1.000.000


def _is_phone(candidate: str) -> bool:
    """A 9 to 15 digit number that is not a date or a thousands-grouped amount."""
    digits = sum(c.isdigit() for c in candidate)
    c = candidate.strip()
    return 9 <= digits <= 15 and not _DATE.match(c) and not _GROUPED.match(c)


# Financial identifiers: Social Security, account, routing and card numbers, dates of birth.
# Transcripts come from speech recognition, so a number may be written in digits
# ("123-45-6789") or spelled out ("four five six, seven eight, ..."). A spelled number only
# counts after a cue word ("my social is ..."); a digit run of 6 to 17 digits counts on its own
# unless it looks like money, a date, a year, a time or a phone number.

_ONES = {
    "zero": 0,
    "oh": 0,
    "o": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS = {
    w: 10 + i
    for i, w in enumerate(
        "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    w: 2 + i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())
}
_REPEAT = {"double": 2, "triple": 3}
_NUMBER_WORD = "|".join(sorted([*_ONES, *_TEENS, *_TENS, *_REPEAT], key=len, reverse=True))
# Separators stay on one line: two lines of a note, or of a transcript, are not one number.
_SPELLED = re.compile(
    rf"\b(?:{_NUMBER_WORD})(?:(?:[ \t]*[,;.][ \t]*|[ \t]+dash[ \t]+|[ \t]+|[ \t]*-[ \t]*)"
    rf"(?:{_NUMBER_WORD}))*\b",
    re.IGNORECASE,
)
# A digit run, or groups of up to 5 digits joined by spaces or dashes (123-45-6789,
# 4242 4242 4242 4242). Not part of a word, a decimal, an amount ($, 1,000), a time or a path.
_DIGITS = re.compile(
    r"(?<![\w.,$€£¥:/+•])(?:[0-9]{1,5}(?:[ -][0-9]{1,5})+|[0-9]+)(?![\w%:/•]|[.,][0-9])"
)
_COMPACT_DATE = re.compile(r"(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])")
_SSN_SHAPE = re.compile(r"[0-9]{3}([ -])[0-9]{2}\1[0-9]{4}")
_MAGNITUDE = re.compile(
    r"[ \t-]*(?:hundred|thousand|million|billion|trillion|percent|dollars?|bucks|cents|grand|"
    r"k|point|euros?|pounds?|shares|basis\s+points|bps|a\s+(?:year|month))\b",
    re.IGNORECASE,
)
_AFTER_MAGNITUDE = re.compile(r"(?:hundred|thousand|million|billion)(?:\s+and)?\s*$", re.I)
_CUES = [
    ("ssn", r"social(?:\s+security)?|s\.?s\.?n|ss\s*#"),
    ("routing", r"routing|aba|transit\s+number|rtn"),
    ("card", r"cards?|visa|master\s?card|amex|american\s+express"),
    ("account", r"accounts?|acct|checking|savings|brokerage|ira"),
    ("dob", r"date\s+of\s+birth|birth\s*date|birthday|d\.?o\.?b|born"),
    ("phone", r"call|phone|cell|mobile|fax|landline|ext(?:ension)?|text\s+me"),
    (
        "money",
        r"balance|worth|value|valued|salary|income|paid|costs?|price|total|amount|budget|"
        r"revenue|fees?|owes?|dollars?|bucks",
    ),
]
_CUE = re.compile("|".join(rf"\b(?P<{kind}>{pattern})\b" for kind, pattern in _CUES), re.I)
_CUE_WINDOW = 60  # characters before a number that may say what it is
# "account ending in 1234", "card, last four 4242": a partial number, masked all the same.
# Not "the year ending 2024".
_ENDING = re.compile(
    r"(?<!year )(?<!quarter )(?<!period )(?<!month )(?<!week )"
    r"(?:\bending|\bends|\blast\s+(?:four|4)(?:\s+digits)?)(?:\s+(?:in|with|is|are|of\s+it))*"
    r"[\s:,-]*$",
    re.IGNORECASE,
)

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
_ONES_ORD = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)"
_ORDINAL = (
    rf"(?:(?:twenty|thirty)[\s-]+{_ONES_ORD}|{_ONES_ORD}|tenth|eleventh|twelfth|thirteenth|"
    r"fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|thirtieth)"
)
_ONE_TO_NINE = r"(?:one|two|three|four|five|six|seven|eight|nine)"
_TWO_SPELLED = (
    rf"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+{_ONE_TO_NINE})?|"
    r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    rf"(?:oh|o)[\s-]+{_ONE_TO_NINE})"
)
_SPELLED_YEAR = (
    rf"(?:(?:nineteen|twenty)[\s-]+(?:hundred|{_TWO_SPELLED})|two\s+thousand"
    rf"(?:\s+and)?(?:[\s-]+(?:{_TWO_SPELLED}|{_ONE_TO_NINE}))?)"
)
_DAY = rf"(?:[0-9]{{1,2}}(?:st|nd|rd|th)?|{_ORDINAL})"
_YEAR = rf"(?:[0-9]{{4}}|'[0-9]{{2}}|{_SPELLED_YEAR})"
_DATE_EXPR = (
    r"[0-9]{1,2}[/.-][0-9]{1,2}[/.-](?:[0-9]{4}|[0-9]{2})\b"
    r"|[0-9]{4}[/.-][0-9]{1,2}[/.-][0-9]{1,2}\b"
    rf"|{_MONTH}\.?\s+(?:the\s+)?{_DAY}\b(?:,?\s+{_YEAR}\b)?"
    rf"|(?:the\s+)?{_DAY}\s+(?:of\s+)?{_MONTH}(?:,?\s+{_YEAR}\b)?"
    rf"|{_MONTH}\.?,?\s+{_YEAR}\b"
)
# "date of birth is 3/15/1962", "born on March third, nineteen sixty-two", "DOB: 1962-03-15"
_DOB = re.compile(
    r"\b(?:date\s+of\s+birth|birth\s*date|birthday|d\.?o\.?b\b\.?|born)"
    r"(?:[\s:,.'’-]+(?:is|was|on|of|in|it's|its|s|would\s+be|will\s+be)\b)*"
    rf"[\s:,.'’-]+(?P<date>{_DATE_EXPR})",
    re.IGNORECASE,
)


def _spelled_digits(s: str) -> str:
    """ "four five six, seventy-eight, double nine" -> "4567899"; "" when it is not digits."""
    out: list[str] = []
    words = list(re.finditer(r"[a-z]+", s.lower()))
    i = 0
    while i < len(words):
        w = words[i].group(0)
        nxt = words[i + 1].group(0) if i + 1 < len(words) else ""
        gap = s[words[i].end() : words[i + 1].start()] if nxt else ""
        if w == "dash":
            pass
        elif w in _REPEAT:
            if nxt not in _ONES:
                return ""
            out.append(str(_ONES[nxt]) * _REPEAT[w])
            i += 1
        elif w in _TENS:
            if _ONES.get(nxt) and gap.strip() in ("", "-"):
                out.append(f"{_TENS[w]}{_ONES[nxt]}")  # seventy-eight
                i += 1
            else:
                out.append(f"{_TENS[w]}0")
        elif w in _TEENS:
            out.append(str(_TEENS[w]))
        else:
            out.append(str(_ONES[w]))
        i += 1
    return "".join(out)


def _luhn(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _aba(digits: str) -> bool:
    """The ABA routing number checksum: 3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) = 0 mod 10."""
    d = [int(c) for c in digits]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def _routing_prefix(digits: str) -> bool:
    """Federal Reserve routing symbols: 00-12, 21-32, 61-72 and 80."""
    p = int(digits[:2])
    return p <= 12 or 21 <= p <= 32 or 61 <= p <= 72 or p == 80


def _classify(raw: str, digits: str, spelled: bool, before: str, after: str, inherited):
    """What a number is, from its digits and the words just before it; None: leave it."""
    cues = [m.lastgroup for m in _CUE.finditer(before)]
    nearest = cues[-1] if cues else inherited
    present = set(cues) | ({inherited} if inherited else set())
    n = len(digits)
    ssn_shape = not spelled and _SSN_SHAPE.fullmatch(raw) is not None
    if spelled and not nearest:
        return None
    if _MAGNITUDE.match(after) or (spelled and _AFTER_MAGNITUDE.search(before)):
        return None  # "two hundred fifty thousand", "250000 dollars", "15 percent"
    if not spelled and _DATE.match(raw):
        return "dob" if nearest == "dob" else None
    if nearest in ("money", "phone"):
        return "ssn" if ssn_shape else None
    # After "social", every number is (part of) one, however it was grouped or spelled ("one
    # two three / four five / ..."): never shown, not even its last four. Not a year ("in 2024").
    year = not spelled and n == 4 and digits[:2] in ("19", "20") and not _ENDING.search(before)
    if ssn_shape or (nearest == "ssn" and not year) or ("ssn" in present and n == 9):
        return "ssn"
    if "routing" in present and n == 9 and _aba(digits):
        return "routing"
    if (13 <= n <= 19 and _luhn(digits)) or (nearest == "card" and 12 <= n <= 19):
        return "card"
    if nearest == "dob" and 6 <= n <= 8:  # 031562, 03151962; not "born in 1962"
        return "dob"
    if 4 <= n <= 5 and nearest in ("account", "card") and _ENDING.search(before):
        return nearest
    if not 6 <= n <= 17:
        return None
    if nearest in ("account", "card", "routing", "ssn", "dob"):
        return "account"
    if spelled or not raw.isdigit():
        return None  # uncued groups (415 555 0100, 1 000 000) are not taken for accounts
    if digits.endswith("000") or _COMPACT_DATE.fullmatch(digits):
        return None  # a round amount (1500000), a date (20260930)
    if n == 9 and _aba(digits) and _routing_prefix(digits):
        return "routing"
    return "account"


_GROUP_GAP = re.compile(r"[\s,.-]{1,3}")
_ID_CUE = re.compile(
    r"\b(?:" + "|".join(p for k, p in _CUES if k in ("ssn", "routing", "card", "account")) + r")\b",
    re.I,
)


def _join_groups(text: str, candidates: list[tuple[int, int, bool]]) -> list[tuple[int, int, bool]]:
    """Speech recognition splits a number said in groups ("123 456789", "1234 5678 9012"):
    after an identifier's cue, digit groups with only spaces, commas or hyphens between them are
    one number. Without a cue they stay apart (phone numbers, amounts)."""
    out: list[tuple[int, int, bool]] = []
    for start, end, spelled in candidates:
        if out and not spelled and not out[-1][2]:
            p_start, p_end, _ = out[-1]
            cued = _ID_CUE.search(text[max(0, p_start - _CUE_WINDOW) : p_start])
            if cued and _GROUP_GAP.fullmatch(text[p_end:start]):
                if len(re.sub(r"[^0-9]", "", text[p_start:end])) <= 19:
                    out[-1] = (p_start, end, False)
                    continue
        out.append((start, end, spelled))
    return out


def _find_identifiers(text: str, start_at: int = 0) -> list[tuple[int, int, str, str]]:
    """(start, end, kind, digits) of the financial identifiers in text, in order. Text before
    `start_at` is only context: what was said just before, e.g. the previous transcript line."""
    dobs = [
        (m.start("date"), m.end("date"), "dob", "")
        for m in _DOB.finditer(text)
        if m.start("date") >= start_at
    ]
    found = list(dobs)
    candidates = sorted(
        [(m.start(), m.end(), False) for m in _DIGITS.finditer(text)]
        + [(m.start(), m.end(), True) for m in _SPELLED.finditer(text)]
    )
    candidates = _join_groups(text, candidates)
    prev_end, prev_kind = 0, None
    for start, end, spelled in candidates:
        if any(s < end and start < e for s, e, _, _ in dobs):
            continue
        for _, e, _, _ in dobs:  # a date of birth said just before counts as the last one
            if prev_end < e <= start:
                prev_end, prev_kind = e, "dob"
        raw = text[start:end]
        digits = _spelled_digits(raw) if spelled else re.sub(r"[^0-9]", "", raw)
        if not digits:
            continue
        # "My social is 123-45-6789 and hers is ..." : the second one is a social too.
        inherited = prev_kind if prev_kind and start - prev_end <= _CUE_WINDOW else None
        before = text[max(prev_end, start - _CUE_WINDOW) : start]
        kind = _classify(raw, digits, spelled, before, text[end : end + 30], inherited)
        if kind:
            if start >= start_at:
                found.append((start, end, kind, digits))
            prev_end, prev_kind = end, kind
    return sorted(found)


_MARKERS = {
    "ssn": lambda d: "[SSN]",
    "account": lambda d: f"[account ••{d[-4:]}]",
    "card": lambda d: f"[card ••{d[-4:]}]",
    "routing": lambda d: "[routing number]",
    "dob": lambda d: "[date of birth]",
}


def _replace(text: str, spans, repl: Callable[[str, str, str], str]) -> str:
    out, last = [], 0
    for start, end, kind, digits in spans:
        out += [text[last:start], repl(kind, text[start:end], digits)]
        last = end
    return "".join(out) + text[last:]


def _marker(kind: str, raw: str, digits: str) -> str:
    return _MARKERS[kind](digits)


def redact_identifiers(text: str, context: str = "") -> str:
    """Replace Social Security, account, routing and card numbers and dates of birth with
    markers that keep at most the last four digits ([SSN], [account ••1234]). One way: for text
    that leaves Mnemosyne or is stored redacted. `context` is what was said just before (the
    previous line), which may hold the cue ("What's your social?")."""
    if not text:
        return text
    offset = len(context) + 1 if context else 0
    full = f"{context}\n{text}" if context else text
    spans = _find_identifiers(full, offset)
    return _replace(full, spans, _marker)[offset:] if spans else text


def redact_transcript(segments: list) -> list:
    """redact_identifiers over transcript lines and their words. Each line is read after the
    end of the previous one, where the question often is. A number spread over several words
    becomes one word holding the marker, spanning their times."""
    out = []
    said = ""  # the end of what was said before: a cue, or a number begun on earlier lines
    for seg in segments:
        context = said[-2 * _CUE_WINDOW :]
        update: dict = {}
        text = redact_identifiers(seg.text, context)
        if text != seg.text:
            update["text"] = text
        if seg.words:
            words = _redact_words(seg.words, context)
            if words is not seg.words:
                update["words"] = words
        out.append(seg.model_copy(update=update) if update else seg)
        said = f"{said} {seg.text}"[-2 * _CUE_WINDOW :]
    return out


def _redact_words(words: list, context: str) -> list:
    joined, pos = "", []
    for w in words:
        if joined:
            joined += " "
        pos.append((len(joined), len(joined) + len(w.word.strip())))
        joined += w.word.strip()
    offset = len(context) + 1 if context else 0
    full = f"{context}\n{joined}" if context else joined
    spans = [(s - offset, e - offset, k, d) for s, e, k, d in _find_identifiers(full, offset)]
    if not spans:
        return words
    clusters: list[list] = []  # [first word, last word, spans]
    for span in spans:
        hit = [i for i, (a, b) in enumerate(pos) if a < span[1] and span[0] < b]
        if not hit:
            continue
        if clusters and hit[0] <= clusters[-1][1]:
            clusters[-1][1] = max(clusters[-1][1], hit[-1])
            clusters[-1][2].append(span)
        else:
            clusters.append([hit[0], hit[-1], [span]])
    out, i = [], 0
    for first, last, group in clusters:
        out += words[i:first]
        a = pos[first][0]
        shifted = [(s - a, e - a, k, d) for s, e, k, d in group]
        word = words[first].word
        lead = word[: len(word) - len(word.lstrip())]
        text = lead + _replace(joined[a : pos[last][1]], shifted, _marker)
        out.append(words[first].model_copy(update={"word": text, "end": words[last].end}))
        i = last + 1
    return out + words[i:]


_PLACEHOLDER_KINDS = {
    "ssn": "SSN",
    "account": "ACCOUNT",
    "card": "CARD",
    "routing": "ROUTING",
    "dob": "DOB",
}


class Redactor:
    """Swap emails, phone numbers, financial identifiers and known names for placeholders, and
    back again.

    One Redactor covers one exchange (prompt out, reply in) so the placeholders in the reply
    map to the same values."""

    def __init__(self, names: list[str]):
        full: set[str] = set()  # "Jakub Sokołowski": matched in any case
        single: set[str] = set()  # "Will", "Jakub": matched as written, so "will" stays
        for n in names:
            n = " ".join(n.split())
            if len(n) < 2:
                continue
            parts = n.split(" ")
            if len(parts) > 1:
                full.add(n)
                single.update(p for p in parts if len(p) >= 3 and p[0].isupper())
            else:
                single.add(n)

        def pattern(words: set[str], flags: int = 0):
            if not words:
                return None
            alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
            return re.compile(r"(?<!\w)(" + alts + r")(?!\w)", flags)

        self._full_re = pattern(full, re.IGNORECASE)
        self._single_re = pattern(single)
        self._forward: dict[str, str] = {}
        self._back: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        key = f"{kind}:{value.casefold()}"
        if key not in self._forward:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            ph = f"[{kind}_{self._counts[kind]}]"
            self._forward[key] = ph
            self._back[ph] = value
        return self._forward[key]

    def redact(self, text: str) -> str:
        text = _EMAIL.sub(lambda m: self._placeholder("EMAIL", m.group(0)), text)
        # Before phone numbers, which would take 123-45-6789 for one.
        text = _replace(
            text,
            _find_identifiers(text),
            lambda kind, raw, d: self._placeholder(_PLACEHOLDER_KINDS[kind], raw),
        )
        text = _PHONE.sub(
            lambda m: (
                self._placeholder("PHONE", m.group(0)) if _is_phone(m.group(0)) else m.group(0)
            ),
            text,
        )
        for name_re in (self._full_re, self._single_re):
            if name_re is not None:
                text = name_re.sub(lambda m: self._placeholder("PERSON", m.group(0)), text)
        return text

    def restore(self, text: str) -> str:
        if not self._back:
            return text
        return re.sub(
            r"\[(?:EMAIL|PHONE|PERSON|SSN|ACCOUNT|CARD|ROUTING|DOB)_\d+\]",
            lambda m: self._back.get(m.group(0), m.group(0)),
            text,
        )


class RedactingProvider:
    """Wraps a cloud provider: every prompt is redacted, every reply restored."""

    def __init__(self, inner, names: Callable[[], list[str]]):
        self.inner = inner
        self.name = getattr(inner, "name", "")
        self._names = names

    async def list_models(self) -> list[str]:
        return await self.inner.list_models()

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        r = Redactor(self._names())
        reply = await self.inner.complete(r.redact(system_prompt), r.redact(user_prompt), model)
        return r.restore(reply)

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        r = Redactor(self._names())
        reply = await self.inner.summarize(r.redact(transcript), model, r.redact(system_prompt))
        return r.restore(reply)

    def __getattr__(self, item):
        return getattr(self.inner, item)
