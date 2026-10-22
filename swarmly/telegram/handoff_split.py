"""One message that names several bots is one hand-off **per bot**, never a refusal (#181).

Found in the 22 Oct exports: 44 of Claudebot's 62 round-5.5 messages named two or more bot
handles, and a bot that was not the first handle answered

    ⚠️ … took no turn: your message names @X, but a peer asks another bot to hand this bot
    the work

and dropped its share of the work (8 times in round 5, once more in 5.5). The refusal's
reason was real -- a *delegation* ("@A please hand @me the task") is A's to act on, and two
bots running the same work is the bug it was written for -- but it also swallowed requests
that were genuinely addressed to several bots at once.

So the two cases are told apart here, in one place:

* **A bot has a request** when the words that follow its handle (up to the next handle) ask
  something -- or when the message opens with a run of handles and a sentence, where every
  handle in that run shares it ("@A @B: please review this").
* **A multi-recipient hand-off** is a message where two or more bots have a request. It is
  split: each bot reads the shared lines plus its own (:func:`own_brief`), and no bot refuses
  it.
* Anything else -- a single request, a delegation, a name mentioned in passing -- is left
  exactly as it was, so the delegation rule keeps its teeth.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

#: A leading ``[Alias]`` label, a bullet or numbered item, and the handles themselves.
_LABEL = re.compile(r"^\s*\[[^\]\n]{1,40}\]\s*")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_HANDLE = re.compile(r"@(\w+)")
#: Words that follow a handle before the ask itself ("@A: please", "@A Bot, can you").
_LEAD = re.compile(r"^[\w.'’-]*[:,]?\s*")
#: What makes a segment an *ask* rather than a mention: a politeness word, a question, or the
#: verbs the swarm actually uses ("re-review #195", "QA it", "push the fix", "merge it").
_ASKS = re.compile(
    r"(?i)(?<![a-z])(?:please|re-?review|review|qa|test|check|verify|look|read|push|fix|"
    r"rebase|merge|land|file|open|add|post|comment|update|tell|send|confirm|answer|reply|"
    r"take|handle|decide|run|pick|split|fold|close|tick|assign|report)(?![a-z])|\?")


def _norm(handle: str) -> str:
    return str(handle or "").strip().lstrip("@").lower()


def _known(handles: Iterable[str]) -> List[str]:
    """The handles to look for, normalized, in the order given, without duplicates."""
    out: List[str] = []
    for handle in handles or ():
        name = _norm(handle)
        if name and name not in out:
            out.append(name)
    return out


def _segments(text: str, known: Sequence[str]) -> List[Tuple[int, str, str]]:
    """``(position, handle, what follows it)`` for every known handle, in the text's order.

    "What follows" ends at the next known handle, so one line can hold two asks
    ("@A: review it. @B: QA it.") and each bot gets its own.
    """
    spots = [(match.start(), match.start() + len(match.group(1)) + 1, _norm(match.group(1)))
             for match in _HANDLE.finditer(text)]
    spots = [spot for spot in spots if spot[2] in known]
    out: List[Tuple[int, str, str]] = []
    for index, (start, after, name) in enumerate(spots):
        end = spots[index + 1][0] if index + 1 < len(spots) else len(text)
        out.append((start, name, text[after:end]))
    return out


def requests(text: str, handles: Iterable[str]) -> List[str]:
    """The bots this message asks something of, in the order they are named.

    A delegation ("@A please hand @B the task") asks A and only A: B's segment is what A's
    sentence is about, and B's segment holds no ask of its own.
    """
    known = _known(handles)
    body = _LABEL.sub("", str(text or ""))
    if not known or not body.strip():
        return []
    segments = _segments(body, known)
    if not segments:
        return []
    asked: List[str] = []
    # A leading run of handles shares the sentence that follows it: "@A @B: please review".
    opening = re.match(r"^(?:\s*@\w+[:,]?)+", body)
    lead = [_norm(raw) for raw in re.findall(r"@(\w+)", opening.group(0))] if opening else []
    lead = [name for name in lead if name in known]
    if len(lead) > 1 and body[opening.end():].strip():
        asked.extend(lead)
    for _start, name, tail in segments:
        body_after = _LEAD.sub("", tail).strip()
        if name in asked or not body_after:
            continue
        # An ask carries a politeness word, a question mark, or one of the verbs the swarm
        # uses. A mention in passing carries none of those ("... and @B agreed yesterday"),
        # which is context: `is_multi_recipient` must not split a message over it.
        if _ASKS.search(body_after):
            asked.append(name)
    return asked


def is_multi_recipient(text: str, handles: Iterable[str]) -> bool:
    """Two or more bots each asked something: the case that must never be refused."""
    return len(requests(text, handles)) >= 2


def own_brief(text: str, me: str, handles: Iterable[str]) -> str:
    """*text* as one bot reads it: the shared lines, plus the lines that name it.

    Lines that ask something of another bot are dropped -- they are that bot's brief, and the
    point of the split is that each bot runs its own share and no more. Lines that name nobody
    stay: they are the context every recipient needs. When nothing is left (a message that is
    only a list of handles and bullets for others), the text is returned unchanged rather than
    emptied: losing the context is worse than repeating it.
    """
    mine = _norm(me)
    known = _known(handles)
    body = str(text or "")
    if not mine or not known or not is_multi_recipient(body, known):
        return body
    kept: List[str] = []
    for line in body.splitlines():
        names = [_norm(match.group(1)) for match in _HANDLE.finditer(line)]
        names = [name for name in names if name in known]
        if not names or mine in names:
            kept.append(line)               # shared context, or this bot's own line
    kept_text = "\n".join(kept).strip()
    return kept_text or body


def split(text: str, handles: Iterable[str]) -> dict:
    """Every bot with a request, and the brief it should read: ``{handle: brief}``.

    The transport uses this to say, in one place, who was handed what -- and a test can assert
    the whole split instead of one bot's view of it.
    """
    known = _known(handles)
    asked = requests(text, known)
    if len(asked) < 2:
        return {}
    return {name: own_brief(text, name, known) for name in asked}
