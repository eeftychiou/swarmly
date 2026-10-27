"""
Agent-opened forum topics: a bot moves a piece of work into its own discussion.

An agent starts its reply with ``NEW_TOPIC: <title>`` and the brief below it. The transport
opens a forum topic named "<alias>: <title>" in the home group, posts the brief there, and
links the topic where the work was asked for. A brief that mentions a peer hands the work
over; one that mentions nobody becomes this agent's own task in the new topic. While working
in a topic it opened, the agent can start a reply with ``REPORT_BACK: <summary>``: the reply
is posted in the topic as usual and the summary also goes back where the work came from.

Telegram side: topics must be enabled in the group, and the bot needs the admin right to
manage topics. Without either, the brief is posted where the agent was asked, with a note.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

NEW_TOPIC = "NEW_TOPIC:"
REPORT_BACK = "REPORT_BACK:"
#: Topics one agent may open per hour: a guard against a loop opening topics.
MAX_TOPICS_PER_HOUR = 5
#: Telegram's limit on a topic name.
MAX_TOPIC_NAME = 128

_LEAD = re.compile(r"^(?:\s*@\w+[:,]?)*\s*")


def parse_new_topic(body: str) -> Optional[Tuple[str, str]]:
    """``(title, brief)`` when the reply opens with ``NEW_TOPIC: <title>``, else None."""
    first, _, rest = _LEAD.sub("", body or "").lstrip().partition("\n")
    if not first.upper().startswith(NEW_TOPIC):
        return None
    title = " ".join(first[len(NEW_TOPIC):].split()).strip("*_` ")
    brief = rest.strip()
    if not title or not brief:
        return None
    return title, brief


def parse_report_back(body: str) -> Optional[str]:
    """The summary after a ``REPORT_BACK:`` line, else None."""
    match = re.search(r"(?im)^\s*\**REPORT_BACK:\**\s*(.*)", body or "")
    if not match:
        return None
    summary = (match.group(1) + body[match.end():]).strip()
    return summary or None


_URL = re.compile(r"\(?https?://\S+\)?")


def topic_name(alias: str, title: str) -> str:
    title = " ".join(_URL.sub("", title).split()) or "work"
    return f"{alias}: {title}"[:MAX_TOPIC_NAME]


def defuse_mentions(text: str) -> str:
    """@handles that read the same but mention nobody (a zero-width space after the @)."""
    return re.sub(r"@(?=\w)", "@\u200b", text or "")


def strip_report_marker(body: str) -> str:
    """The reply as shown in the topic: the REPORT_BACK: marker is for the transport."""
    return re.sub(r"(?im)^(\s*)\**REPORT_BACK:\**\s*", r"\1", body or "", count=1)


_REF = re.compile(r"(?:(?<![\w/])#|/(?:pull|issues)/|\bPR ?#?)(\d{1,7})\b")

#: What a cut number looks like: a title the transport truncated ("(#42…"), so the digits are
#: the start of a longer number, not a ref (#457: the live phantom `#42` for #421). One
#: ellipsis character always is one; three dots only right after an opening bracket, because
#: "see #123..." is a real ref with prose after it (#457 review).


def issue_refs(text: str, topic_ids=()) -> list:
    """The issue and PR numbers a message is about ("#12", ".../pull/12", "PR 12"), as "#12".

    Two things are not refs (#457). A number a truncated title cut off -- ``(#42…`` is the
    start of #421, and learning ``#42`` invented an issue nobody linked -- and a forum topic's
    own thread id, which reads exactly like a number and is not one (`#13221` was a topic).
    *topic_ids* is the caller's list of the thread ids it knows in that chat.
    """
    body = text or ""
    skip = set()
    for value in topic_ids or ():
        try:
            skip.add(int(value))
        except (TypeError, ValueError):
            continue
    found = []
    for match in _REF.finditer(body):
        tail = body[match.end():match.end() + 3]
        bracketed = match.start() > 0 and body[match.start() - 1] == "("
        if tail.startswith("\u2026") or (bracketed and tail.startswith("...")):
            continue                                # a title cut mid-number, not a ref
        try:
            number = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if number in skip:
            continue                                # a topic id, not an issue
        ref = f"#{number}"
        if ref not in found:
            found.append(ref)
    return found


def topic_link(chat_id: int, thread_id: int) -> str:
    """A t.me link to a topic of a supergroup (-100<id>)."""
    internal = str(chat_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    return f"https://t.me/c/{internal}/{thread_id}"


SELF_TASK_NOTE = ("You opened this topic for the work below; do it here. Stay within what was "
                  "asked: research means findings, not issues, branches or pull requests, "
                  "unless the request asks for them. When it is done, start your final reply "
                  "with REPORT_BACK: and a short summary: the reply is posted here and the "
                  "summary goes back to where the work was asked for.")

#: Sent to the opener when its topic goes quiet before it reported back.
REPORT_NOTE = ("The work in this topic you opened has gone quiet and you have not reported "
               "back yet. Reply now with a first line REPORT_BACK: and a short summary of the "
               "outcome (findings, links, what is left), or exactly NO_REPLY if the work is "
               "not finished.")

#: Added to a message that asks for a topic, so a model that skims the rules still opens one.
TOPIC_ASKED_NOTE = ("[The sender asks for this work in its own topic: start your reply with a "
                    "first line NEW_TOPIC: <short title>, then the brief for the work.]")

_TOPIC_ASK = re.compile(r"(?i)\b(?:new|own|separate|dedicated)\s+(?:forum\s+)?topic\b"
                        r"|\bopen\s+(?:up\s+)?(?:a\s+)?topic\b|\bin\s+a\s+topic\b")


def asks_for_topic(text: str) -> bool:
    """Whether a message asks for the work to go into its own topic, in plain words."""
    return bool(_TOPIC_ASK.search(text or "")) and NEW_TOPIC not in (text or "").upper()


def hands_over(brief: str, peer_handle: "re.Pattern[str]") -> bool:
    """Whether a brief hands the work to a peer: it opens by addressing one. A peer named
    later (to confirm scope, say) leaves the work with the opener."""
    return bool(peer_handle.match(brief.lstrip()))


#: A reply in General longer than this is work, and moves to its own topic.
GENERAL_REPLY_CHARS = 800
#: The same for a reply to a peer bot: a one-line answer ("my role is ...") stays in General.
GENERAL_PEER_REPLY_CHARS = 400

_TITLE_NOISE = re.compile(r"^(?:\s*(?:@\w+|\[[^\]\n]{1,40}\])[:,]?)+\s*")


def title_from(text: str, words: int = 6) -> str:
    """A short topic title from the message that started the work."""
    first = next((line for line in _URL.sub("", _TITLE_NOISE.sub("", text or "")).splitlines()
                  if line.strip()), "")
    first = re.sub(r"@\w+", "", first)          # handles: whom, not what (and their _)
    first = re.sub(r"(?i)^\W*(?:please|pls|can you|could you)\b", "", first.strip())
    title = " ".join(re.sub(r"[*`#>]|(?<!\w)_|_(?!\w)", "", first).split()[:words])
    title = title.strip(" .,:;!?")
    return title[:60] or "work"
