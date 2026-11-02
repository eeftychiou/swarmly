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


#: The end of a sentence: a terminator followed by a space or the end of the text. The
#: terminator itself is kept, so a question mark still counts as the ask it is.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
#: A line that opens with a run of handles ("@A @B: …"): those handles share its sentence.
_OPENING = re.compile(r"^(?:\s*@\w+[:,]?)+")


def _first_sentence(text: str) -> str:
    """*text* up to the end of its first sentence.

    A question mark two sentences after a handle belongs to whoever that later sentence is
    for: "@Codex: the review is in. Any news?" asks Codex, but "@Codex: the review is in, and
    @B agreed yesterday. Any news?" must not ask @B (#181 review).
    """
    match = _SENTENCE_END.search(text)
    return text[:match.end()] if match else text


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
        # which is context: `is_multi_recipient` must not split a message over it. Only the
        # segment's first sentence counts, so a question two sentences on is not this bot's.
        if _ASKS.search(_first_sentence(body_after)):
            asked.append(name)
    return asked


#: A line that opens with a peer's handle, with or without a bullet: a hand-off line (#425).
_OPENING_HANDLE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*@\w+[:,]?(?=\s|$)")
#: A line that is only a handle (":", then nothing): its ask follows on the lines below.
_HANDLE_ALONE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*@\w+[:,]?\s*$")


def is_multi_recipient(text: str, handles: Iterable[str]) -> bool:
    """Two or more bots each asked something: the case that must never be refused."""
    return len(requests(text, handles)) >= 2


def handoff_blocks(text: str) -> List[Tuple[str, str]]:
    """``(handle, block)`` for each hand-off in a reply, in the order they are written.

    A line (bullet or not) that opens with an ``@handle`` is a hand-off for that handle, and a
    line that is *only* a handle is a heading: its ask is the next paragraph, blank lines and
    all (review of #425 on #427: ``"@CodexBot:\\n\\nPlease review #424."`` is how a model
    writes it, and keeping only the bare handle dropped the instruction). One peer's block ends
    where the next begins, so a reply that hands #424 to one bot and #430 to another gives each
    of them its own -- which is the item each one's row records (#429 review).
    """
    lines = str(text or "").splitlines()
    blocks: List[Tuple[str, List[str]]] = []
    index = 0
    taking = False              # a handle-alone block runs on through its paragraph
    while index < len(lines):
        line = lines[index]
        index += 1
        found = _HANDLE.search(line) if _OPENING_HANDLE.match(line) else None
        if found is not None:
            block = [line]
            blocks.append((_norm(found.group(1)), block))
            taking = bool(_HANDLE_ALONE.match(line))
            if taking:
                while index < len(lines) and not lines[index].strip():
                    index += 1      # the ask is the next paragraph, after any blank line
            continue
        if taking and line.strip():
            blocks[-1][1].append(line)
        elif taking:
            taking = False          # the paragraph ended: the rest is not the hand-off
    return [(handle, "\n".join(block).strip()) for handle, block in blocks]


def handoff_lines(text: str) -> str:
    """The lines of a reply that hand work to a peer, as one block.

    Every block :func:`handoff_blocks` finds, in the order written. Those lines are carried
    out of a place no peer can see -- the admin's private chat (#425) -- into the work item's
    own topic; the rest of the reply stays where it was written. "" when nothing is handed
    over.
    """
    return "\n".join(block for _handle, block in handoff_blocks(text)).strip()


def _shared_prefix(line: str, known: Sequence[str],
                   sender: Sequence[str] = ()) -> str:
    """The words a line holds *above* its asks: every recipient's context (#496).

    A coordinator's round post opens by addressing the owner and carries the round's question
    in the same breath ("@<owner>: round 2 starts here. Which token did you get?"), with one
    line per agent below. Cutting such a line down to a bot's own segment put the question
    outside two agents' briefs, and both answered that nothing had been posted. So the text
    before the line's first bot handle is kept -- unless it is itself another bot's ask
    ("@A: review it. @B: QA it."), which is that bot's brief and nothing else's.

    A post that opens by addressing its own author is a header, not a hand-off: the round's
    coordinator writes "@<its own handle>: round 2 starts here…", and the question after it is
    every agent's. So the author's own handles are not asks, and the header's own segment is
    part of the shared words rather than a segment to cut (#496 review).
    """
    others = set(_known(sender))
    segments = [spot for spot in _segments(line, known) if spot[1] not in others]
    if not segments:
        return ""                       # the line only addresses its author: a header
    prefix = line[:segments[0][0]]
    if not re.search(r"[^\s\-*•.)\d]", prefix):
        return ""                       # a bullet, a label or a number: not words
    asked = {name for name in requests(prefix, known) if name not in others}
    return "" if asked else prefix


def own_brief(text: str, me: str, handles: Iterable[str],
              sender: Iterable[str] = ()) -> str:
    """*text* as one bot reads it: the shared lines, plus the lines that name it.

    Lines that ask something of another bot are dropped -- they are that bot's brief, and the
    point of the split is that each bot runs its own share and no more. Lines that name nobody
    stay: they are the context every recipient needs, and so does the shared brief a line
    carries above its first bot handle (:func:`_shared_prefix`, #496). When nothing is left (a
    message that is only a list of handles and bullets for others), the text is returned
    unchanged rather than emptied: losing the context is worse than repeating it.
    """
    mine = _norm(me)
    known = _known(handles)
    sender = _known(sender)
    body = str(text or "")
    if not mine or not known or not is_multi_recipient(body, known):
        return body
    kept: List[str] = []
    for line in body.splitlines():
        names = [_norm(match.group(1)) for match in _HANDLE.finditer(line)]
        names = [name for name in names if name in known]
        if not names:
            kept.append(line)               # shared context: it asks nobody
            continue
        # An ask the author's own handle carries is a header ("@<its own handle>: round 2
        # starts here…"), not work for anyone reading the split (#496 review).
        asked = [_norm(name) for name in requests(line, known) if _norm(name) not in sender]
        if mine in asked:
            # A line that opens with a run of handles shares one sentence between them
            # ("@A @B: review #195 and QA it today"), so there is nothing to cut: the whole
            # line is every bot's brief. Cutting at the next handle left the first bot a bare
            # handle and no task (#181 review).
            opening = _OPENING.match(_LABEL.sub("", _BULLET.sub("", line)))
            lead = ([_norm(raw) for raw in re.findall(r"@(\w+)", opening.group(0))]
                    if opening else [])
            if len([name for name in lead if name in known]) > 1 and mine in lead:
                kept.append(line)
                continue
            # Otherwise my share of *this* line: the shared prefix above its first bot handle
            # (the owner's opening words, #496), then my segment up to the next handle. Two
            # asks on one line therefore give each bot its own, which is the shape Claudebot
            # posts most (#181 review).
            segments = _segments(line, known)
            prefix = _shared_prefix(line, known, sender)
            for index, (offset, name, _tail) in enumerate(segments):
                if name != mine:
                    continue
                stop = segments[index + 1][0] if index + 1 < len(segments) else len(line)
                piece = (prefix + line[offset:stop]).strip()
                if piece:
                    kept.append(piece)
                break
            continue
        if asked:
            # Another bot's ask: theirs, not mine. The words above it still are mine when they
            # are the shared brief (#496): only the ask itself is dropped.
            prefix = _shared_prefix(line, known, sender)
            if prefix.strip():
                kept.append(prefix.strip())
            continue
        kept.append(line)                   # a mention only: context, not a task
    kept_text = "\n".join(kept).strip()
    return kept_text or body
