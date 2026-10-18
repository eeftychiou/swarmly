"""
The Telegram home channel: one group, one bot per agent, everyone sees everything.

Design notes that matter operationally
--------------------------------------
* **Identity is the bot token.** Holding the token *is* being that bot in Telegram, so there
  is no second authentication layer: the channel trusts Telegram for identity and uses the
  configured ``admin_user_id`` for authority.
* **The alias is the address.** Messages are posted as ``**[Hermes]** …`` and a message is
  treated as addressed to this agent when it mentions the alias or the bot username, replies
  to this bot, or comes from the admin.
* **Loop prevention is mandatory** — Telegram says so for bot-to-bot mode, and two agents
  that always answer each other will happily talk until the quota runs out. The channel
  counts consecutive agent-authored messages and stops responding once the budget is spent,
  until the admin speaks again.
* **Pacing** — Telegram allows ~20 messages per minute per group, so sends are spaced and
  ``TelegramRetryAfter`` is honoured rather than hammered.

Everything here runs through :class:`ChannelEvent`, a plain dataclass. The aiogram layer
only converts updates into events and posts outgoing text, so the routing rules are testable
with no network and no aiogram installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import socket
import subprocess
import time
import dataclasses
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from swarmly.telegram.acks import AckTracker, parse_ttl
from swarmly.telegram.adapters import AgentAdapter, IncomingMessage, build_adapter
from swarmly.telegram.adapters.base import (AdapterReply, is_silent_reply, load_state,
                                           save_state, topic_mode)
from swarmly.telegram.commands import (
    DIAG_COMMANDS,
    command_args,
    diag_reply,
    is_command_reply,
    parse_command,
    ping_reply,
    status_reply,
    where_reply,
)
from swarmly.telegram.config import EFFORT_LEVELS, AgentConfig, effort_problem, model_problem
from swarmly.telegram.poll import bus_poll_once
from swarmly.telegram.projects import ProjectRouter
from swarmly.telegram.redact import Redactor
from swarmly.telegram.topic_open import (GENERAL_PEER_REPLY_CHARS, GENERAL_REPLY_CHARS,
                                         MAX_TOPICS_PER_HOUR, REPORT_NOTE, SELF_TASK_NOTE,
                                         TOPIC_ASKED_NOTE, asks_for_topic, defuse_mentions,
                                         hands_over, issue_refs, parse_new_topic,
                                         parse_report_back, strip_report_marker, title_from,
                                         topic_link, topic_name)
from swarmly.telegram.topic_memory import TopicMemory
from swarmly.telegram.topics import TopicMap, name_from_message
from swarmly.telegram.wire import (Envelope, MessageType, addressed, format_message,
                               has_explicit_envelope, ignored_envelope_marker,
                               markdown_entities, markdown_html, parse_message,
                               strip_autolinks,
                               utf16_length)

logger = logging.getLogger("swarmly.telegram")


class _ReceiptSuperseded(Exception):
    """The queued receipt became unnecessary before its paced network attempt."""


_SEND_SUPPRESSED = object()


def _git_head(*rev_parse_args: str) -> str:
    """Best-effort ``git rev-parse`` of HEAD for the checkout this process runs from.

    Never raises: outside a git checkout (a source tarball, a container without
    git) it degrades to ``"unknown"``. That fallback is deliberately fail-closed
    and shared by both callers — a revision we cannot state is not a revision we
    can attest to, and the two revisions below must not disagree about how they
    behave when git is missing.
    """
    try:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", *rev_parse_args, "HEAD"],
            capture_output=True, text=True, timeout=5)
        revision = result.stdout.strip()
        if result.returncode == 0 and revision:
            return revision
    except Exception:  # pragma: no cover - defensive: never block startup
        pass
    return "unknown"


def _local_revision() -> str:
    """Best-effort short git revision of the checkout this process runs from.

    Announced in the online notice so an upgrade (or a stale deployment) is
    visible in-channel at a glance.
    """
    rev = _git_head("--short")
    if rev == "unknown":
        # An installed wheel has no checkout: the package version is the revision.
        from swarmly import __version__
        return f"v{__version__}"
    return rev


def _local_revision_full() -> str:
    """Best-effort FULL 40-char git revision of the checkout this process runs from.

    Used only by the machine receipt line (issue #70). Gate 3 of
    ``scripts/upgrade_transport.sh`` matches the receipt's ``rev=`` against the
    target sha, so it needs the whole revision rather than the short form the
    human-facing notice carries.
    """
    rev = _git_head()
    return _local_revision() if rev == "unknown" else rev


#: Messages older than this when the transport starts are a backlog, not a conversation.
_MAX_BACKLOG_SECONDS = 30 * 60

# Retained ids in the gitbus seen-ledger; oldest are pruned first (insertion order).
_GITBUS_SEEN_MAX = 5000


def _default_gitbus_pull(root: Path) -> None:
    """Best-effort ref refresh for the checkout whose durable bus is polled (never raises).

    Fetches only — the working tree is never touched, so a dirty or diverged checkout cannot
    block the fallback (the reader prefers the fetched ref anyway).
    """
    from swarmly.transports import GitTransport
    GitTransport(Path(root)).refresh()


def read_transport_messages(transport: Any, keys: Any = None) -> List[Any]:
    """Parse every envelope a transport lists into ``(message, verified, status)``.

    *keys* is a :class:`~swarmly.bus.DurableBus` whose registered keys and revocation list
    check each signature (review P0-1). Without it nothing is verified: every item reads
    ``(message, None, "unverified")`` and the poller delivers none of them unless
    ``bus.allow_unsigned`` is set. Unreadable files are skipped with a warning (a
    half-synced file is picked up on the next cycle).
    """
    from swarmly.models import A2AMessage

    items: List[Any] = []
    for name, data in transport.list_messages():
        try:
            message = A2AMessage.from_yaml_frontmatter(data)
        except Exception:  # noqa: BLE001 - one bad file must not stop the other deliveries
            logger.warning("bus: unreadable message file %s", name)
            continue
        if keys is None:
            items.append((message, None, "unverified"))
        else:
            items.append((message, *keys.verify_message(message)))
    return items


def bus_keys(root: Path, transport: Any) -> Any:
    """The :class:`~swarmly.bus.DurableBus` whose ``config/a2a_config.json`` under *root*
    holds the keys bus messages are verified against."""
    from swarmly.bus import DurableBus
    return DurableBus(root_dir=Path(root), transport=transport)


def _default_gitbus_read(root: Path, ref: Optional[str] = None) -> List[Any]:
    """Read the durable gitbus inbox: freshest view from *ref* when possible, local files
    otherwise (see :class:`~swarmly.transports.git.GitTransport`)."""
    from swarmly.transports import GitTransport
    transport = GitTransport(Path(root), ref=ref)
    return read_transport_messages(transport, bus_keys(root, transport))


_BANNER_AND_MENTIONS = re.compile(r"^(?:\s*(?:\[[^\]\n]{1,40}\]|@\w+)[:,]?)+\s*")
_NOTICE_PREFIXES = ("⚠️", "↪", "❌", "🟢", "✅", "👀", "⏳", "♻️", "[system]", "[status]")

#: Why a peer bot's mention of this bot is dropped without a notice back (#27): machine traffic
#: never asked for a turn, a bot's own echo is addressed to nobody, and a stranger's or a
#: split-reply fragment's mention is not the sender handing this bot work.
_NO_DROP_NOTICE_REASONS = frozenset({
    "not a text message",
    "this agent's own brief (a topic it opened, or a membership turn)",
    "own message",
    "not a member of the swarm (limits.strangers: ignore)",
    "continuation of a split message",
    "automated status/notice from peer bot",
    "built-in command reply from peer bot (machine traffic)",
    "terminal acknowledgment/sign-off from peer bot (no reply needed)",
})
#: One drop notice per reason per topic per window: a peer that keeps naming this bot in the
#: same dropped shape hears it once, not once per message.
DROP_NOTICE_SECONDS = 600.0


def machine_prose(text: str) -> str:
    """A bot message without its "[Alias]" banner and leading @mentions: what classifiers
    should read. (Before, only mentions were stripped, so "[Claude] ⚠️ ..." notices from a
    peer read as prose and started turns.)"""
    return _BANNER_AND_MENTIONS.sub("", (text or "").strip())


def is_terminal_peer_ack(text: str) -> bool:
    """Check if an incoming message from a peer bot is a terminal acknowledgment / sign-off.

    Peer bots frequently send polite closing statements ('Acknowledged', 'Standing down',
    'Idle until maintainer sign-off', 'Closing here'). Answering these creates infinite ping-pong
    loops and pollutes the channel. If no question is asked and terminal acknowledgment phrases
    are present, bots must remain silent.
    """
    if not text:
        return False
    # If the peer is asking a question, it is not a pure acknowledgment
    if "?" in text:
        return False

    lowered = text.lower()
    # A status note can contain an acknowledgment word and still carry a real task.
    # Preserve those mixed messages for the active adapter; only pure closures are silent.
    terminal_directive = re.search(
        r"\b(?:please|kindly)\s+(?:stand(?:ing)? down|remain idle|stay(?:ing)? idle|"
        r"wait(?: here)?|hold(?: here)?)\b",
        lowered,
    )
    request_pattern = (
        r"\b(?:can|could|would)\s+you\b|\b(?:please|kindly)\s+\w+\b|"
        r"\b(?:need|want)\s+you\s+to\b|\b(?:you\s+should|you\s+need\s+to|you\s+must)\b|"
        r"(?:^|[.;:—-])\s*(?:review|check|verify|fix|update|post|comment|merge|pull|roll|upgrade|"
        r"implement|investigate|rebase|run|send|reply|respond|open|close|add|remove|"
        r"take|handle|start|stop)\b|\b(?:your call|request:)\b|"
        r"\b(?:and|but|then|also)\s+(?:(?:please|kindly)\s+)?(?:review|see|check|verify|"
        r"fix|update|post|comment|merge|pull|roll|upgrade|implement|investigate|rebase|"
        r"run|send|reply|respond|open|close|add|remove|take|handle|start|stop)\b|"
        r"\b(?:and|but|then|also)\s+(?:need\s+you\s+to|want\s+you\s+to|you\s+should|"
        r"you\s+need\s+to|you\s+must|must)\b"
    )
    action_required_pattern = re.compile(r"\baction(?:\s+is)?\s+required\b")
    negated_action_pattern = re.compile(
        r"\b(?:no|without)\b(?:\s+\w+){0,4}\s+action(?:\s+is)?\s+required\b"
    )

    def has_request(value: str) -> bool:
        if re.search(request_pattern, value):
            return True
        negated_spans = [match.span() for match in negated_action_pattern.finditer(value)]
        return any(
            not any(start <= match.start() < end for start, end in negated_spans)
            for match in action_required_pattern.finditer(value)
        )

    terminal_reinforcement = re.search(
        r"\b(?:and|but|then|also)\s+(?:take\s+no\s+further\s+action|stop(?:\s+here)?)\b",
        lowered,
    )
    followup_action = None
    pre_terminal_action = None
    middle_terminal_action = None
    if terminal_directive:
        pre_terminal_action = has_request(lowered[:terminal_directive.start()])
    if terminal_reinforcement:
        if terminal_directive:
            middle_terminal_action = has_request(
                lowered[terminal_directive.end():terminal_reinforcement.start()],
            )
        followup_action = has_request(lowered[terminal_reinforcement.end():])
    elif terminal_directive:
        followup_action = has_request(lowered[terminal_directive.end():])
    if (not terminal_directive or pre_terminal_action or middle_terminal_action or followup_action) and has_request(lowered):
        return False
    ack_patterns = [
        r"\back(?:nowledged|nowledging)?\b",
        r"\bstand(?:ing)? down\b",
        r"\bclosing here\b",
        r"\b(?:remain|stay(?:ing)?)\s+idle\b",
        r"\bidle until\b",
        r"\bidle pending\b",
        r"\bno further action\b",
        r"\bnothing further needed\b",
        r"\baligned on both sides\b",
        r"\broger that\b",
        r"\bpulled\s+(?:origin|main|both|repo|latest)\b",
        r"\bup to date\b",
        r"\bsynced\b",
        r"\breceived\b",
        r"\bconfirmed\b",
        r"\bonline\b",
    ]
    return any(re.search(pat, lowered) for pat in ack_patterns)


@dataclass
class ChannelEvent:
    """A normalised inbound message — everything the routing rules need, nothing else."""

    text: str
    chat_id: int
    author_id: Optional[int] = None
    author_username: Optional[str] = None
    author_is_bot: bool = False
    message_id: Optional[int] = None
    replies_to_bot: bool = False
    raw: Any = None
    thread_id: Optional[int] = None        # forum topic the message was posted in, if any
    topic_name: Optional[str] = None       # that topic's name, when this message carries it
    topic_name_current: bool = True        # False: the creation name, possibly renamed since
    attachments: List[Dict[str, Any]] = field(default_factory=list)   # files, photos, voice
    handoff: bool = False                  # set by routing: an expected hand-off, no cooldown
    cooldown_left: float = 0.0             # set by routing: a message to this bot the cooldown dropped
    self_task: bool = False                # a brief this agent gave itself (topic or membership)


@dataclass
class ChannelStats:
    handled: int = 0
    ignored: int = 0
    sent: int = 0
    errors: int = 0
    loop_pauses: int = 0
    machine_events: int = 0
    last_error: str = ""
    busy_dropped: int = 0                                # #9: addressed msgs dropped while a turn was in flight
    busy_queued: int = 0                                 # addressed msgs held and run after the turn in flight
    stopped: int = 0                                     # turns cancelled with /stop


class AgentChannel:
    """Routes one Telegram group between a set of agent bots and the admin."""

    def __init__(self, config: AgentConfig, adapter: Optional[AgentAdapter] = None,
                 sender: Optional[Callable[[str, Optional[int]], Any]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Optional[Callable[[float], Any]] = None,
                 gitbus_pull: Optional[Callable[[Path], Any]] = None,
                 gitbus_read: Optional[Callable[[Path], List[Any]]] = None,
                 transport: bool = False):
        self.config = config
        # Issue #123: only the long-running transport may arm the fail-fast gate;
        # it calls `AgentChannel(config, transport=True)` from cli._run.
        self.adapter = adapter or build_adapter(config, transport=transport)
        sessions = getattr(self.adapter, "SESSIONS_STATE", None)
        if adapter is None and sessions and hasattr(self.adapter, "use_state_file"):
            self.adapter.use_state_file(_state_file(config, sessions))
        # durable-plane fallback (fallback.gitbus_poll_minutes)
        self._bus_task: Optional[asyncio.Task] = None
        self._lane_poll_task: Optional[asyncio.Task] = None    # one lane poll per doorbell burst
        self._gitbus_busy = False
        self._gitbus_pull = gitbus_pull or self._default_bus_pull
        self._gitbus_read = gitbus_read or self._default_bus_read
        self._gitbus_seen: Optional[dict] = None
        self._sender = sender                    # injected in tests; aiogram Bot in production
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep
        self.stats = ChannelStats()

        # vestigial guard state (the turn cap was removed; loop safety is routing) — retained
        # for compatibility with tooling that introspects the channel
        self._consecutive_agent_messages = 0
        self._loop_notice_sent = False

        # pacing state
        self._last_send = float("-inf")
        self._sent_timestamps: List[float] = []
        self._send_lock = asyncio.Lock()
        self._topic_send_locks: Dict[Any, asyncio.Lock] = {}   # parallel topics: per topic
        self._topic_paces: Dict[Any, Dict[str, Any]] = {}
        self._flood_until = float("-inf")     # Telegram's flood wait (429) holds every topic
        self.last_sent_message_id: Optional[int] = None
        self.last_first_message_id: Optional[int] = None   # part 1 of the latest send()
        # Telegram messages this bot took a turn on: a bus mirror of one is not run twice.
        # Kept across restarts (live test: a restarted bot re-ran a hand-off it had handled).
        self._tg_admitted: deque = deque(maxlen=500)
        self._admitted_path = _state_file(config, "admitted")
        for pair in load_state(self._admitted_path, "handled Telegram messages").get("ids", []):
            if isinstance(pair, list) and len(pair) == 2 and all(
                    isinstance(v, int) and not isinstance(v, bool) for v in pair):
                self._tg_admitted.append((pair[0], pair[1]))
        self._mirror_tasks: Set[asyncio.Task] = set()
        # Hand-offs this bot made that the receiving bot has not answered yet:
        # (chat, thread, peer alias) -> {"at", "message_id", "nudged"}.
        self._pending_handoffs: Dict[Tuple[int, Optional[int], str], Dict[str, Any]] = {}
        # Each discussion's task, progress and latest messages (anyone in the swarm, this
        # bot's replies included), kept across restarts: see topic_memory.py.
        self._topic_memory = TopicMemory(
            _state_file(config, "topic-memory"),
            tuple(str(handle) for handle in config.peers.values()))
        self._pause_reminded: Dict[Any, float] = {}

        # conversational turn state & concurrency guard. With limits.max_parallel_turns 1
        # (the default) every message shares _turn_lock: one turn at a time. Above 1, each
        # forum topic gets its own lock (one turn per topic, in order) and _turn_slots caps
        # how many topics run at once.
        self._turn_lock = asyncio.Lock()
        self._turn_locks: Dict[Tuple[int, Optional[int]], asyncio.Lock] = {}
        parallel = int(getattr(config.limits, "max_parallel_turns", 1) or 1)
        self._parallel_turns = max(parallel, 1)
        self._turn_slots: Optional[asyncio.Semaphore] = (
            asyncio.Semaphore(parallel) if parallel > 1 else None)
        home = getattr(config.identity, "home_channel_id", None)
        self.topics = TopicMap(_topics_path(config), home_chat_id=int(home) if home else None)
        if parallel > 1 and getattr(config.limits, "allow_shared_cwd", False):
            logger.warning("%s runs up to %d topic turns at once in one working directory (%s): "
                           "they can overwrite each other's edits",
                           config.identity.alias, parallel, config.adapter.cwd or ".")
        self._last_agent_turn = float("-inf")     # the bot-to-bot cooldown, per lock key
        self._last_turn_at: Dict[Any, float] = {}
        self._peer_turns: Dict[Any, int] = {}     # consecutive peer-started turns, per topic
        self._cooldown_waiting: Dict[Any, int] = {}   # peer messages waiting out the cooldown
        self._strangers_seen: Set[Any] = set()        # authors already warned about once
        self._hold_gates: Dict[Any, asyncio.Lock] = {}   # those messages, oldest first
        self._handoff_notes: Dict[Any, Tuple[str, float]] = {}   # topic -> (peer, expiry)
        self._paused_topics: set = set()           # topics told they hit the peer-turn cap
        # Both survive a restart (live test: a restart silently lifted every pause).
        self._turns_path = _state_file(config, "topic-turns")
        saved_turns = load_state(self._turns_path, "topic turn counts")
        for item in saved_turns.get("peer_turns", []):
            if isinstance(item, list) and len(item) == 3 and isinstance(item[2], int):
                with contextlib.suppress(TypeError, ValueError):
                    self._peer_turns[(int(item[0]), None if item[1] is None
                                      else int(item[1]))] = item[2]
        for item in saved_turns.get("paused", []):
            if isinstance(item, list) and len(item) == 2:
                with contextlib.suppress(TypeError, ValueError):
                    self._paused_topics.add((int(item[0]), None if item[1] is None
                                             else int(item[1])))
        # Agent-opened topics (topic_open.py): topic -> where its work was asked for.
        self._topic_origins: Dict[Tuple[int, Optional[int]], Tuple[int, Optional[int]]] = {}
        self._topics_opened: List[float] = []
        self._cooldown_noticed = float("-inf")
        self._anonymous_noticed = float("-inf")
        # #27: (chat, topic, reason) -> clock of the last notice telling a peer bot its mention
        # was dropped, so one drop is reported once, not once per message.
        self._drop_notices: Dict[Tuple[int, Optional[int], str], float] = {}
        self._transient_retry_delays: Tuple[float, ...] = TRANSIENT_RETRY_DELAYS
        self._progress_marks: Dict[Tuple[int, Optional[int]], Set[str]] = {}
        # /pause with turns running: take no new ones, freeze when the last one ends.
        self._pause_pending = False
        self._pause_where: Tuple[Optional[int], Optional[int]] = (None, None)
        self._queue_full_noticed = float("-inf")
        self._ref_topics: Dict[str, Tuple[int, int]] = {}     # "#12" -> the topic about it
        self._topics_reported: Set[Tuple[int, Optional[int]]] = set()   # sent REPORT_BACK
        self._report_asked: Set[Tuple[int, Optional[int]]] = set()      # REPORT_NOTE sent
        self._self_tasks: Set[asyncio.Task] = set()
        self._gone_topics: set = set()             # topics a reply in flight found closed
        # Whether a post to a closed or deleted topic goes to General with a note (a reply
        # must land somewhere); ``swarmly-telegram send --topic`` turns it off so a wrong
        # topic id fails instead of posting in General.
        self.reroute_gone_topics = True
        self._frozen = False
        self._freeze_ref: Optional[str] = None
        self._quota_failures = 0  # #49: consecutive quota-signature adapter failures
        # Token economy (issue #43): machine-class traffic recorded as state, not model turns.
        self._machine_log: deque = deque(maxlen=50)
        self._peer_last_seen: Dict[str, Dict[str, Any]] = {}

        # #9: busy-drop notice state (limits.busy_behavior == "notice")
        self._busy_notice_at = float("-inf")   # clock of the last busy notice; -inf = never
        self._busy_drops_since_notice = 0      # addressed msgs dropped since that notice
        self._busy_waiting = 0                 # "queue": handlers waiting for the turn lock
        self._busy_waiting_by: Dict[Any, int] = {}   # the same, per topic (parallel turns)
        self._busy_since: Dict[Any, float] = {}      # topic -> clock its running turn started
        self._busy_noticed: Dict[Any, float] = {}    # topic -> clock of the last ⏳ busy notice
        self._queue_seq: Dict[Any, int] = {}         # topic -> number of the last queued message
        self._folded: Dict[Any, List[str]] = {}      # topic -> queued messages awaiting one turn
        self._stop_generation = 0              # bumped by /stop: queued messages from before it are dropped
        self._stop_topic_generation: Dict[Any, int] = {}   # the same, for /stop in one topic
        self._turn_keys: Dict[Any, Any] = {}               # turn in flight -> its topic
        self._current_turn: Optional[asyncio.Future] = None     # the turn started last
        self._turns: set = set()                               # every turn in flight
        self._stopping_turns: set = set()

        # v1.1 §5–7 delivery machinery: ACK ladder + transport-answered commands
        ack_ttl = getattr(config.limits, "ack_ttl_seconds", 1800.0)
        self.acks = AckTracker(clock=clock, default_ttl_seconds=float(ack_ttl))
        self._ack_announced: Dict[str, int] = {}
        self._ack_attempted: Dict[str, int] = {}
        self._ack_task: Optional[asyncio.Task] = None
        from swarmly.telegram.selftest import PeerSelfTest
        self.selftest = PeerSelfTest(self)       # startup check that the bots see each other
        from swarmly.telegram.roster import Roster
        self.roster = Roster(config, clock=lambda: self._clock())   # bots admitted at runtime
        from swarmly.telegram.membership import Membership
        self.membership = Membership(self)        # join, roles and drop-outs (membership.py)
        # /model: the model each discussion runs, set by the admin at runtime and kept
        # across restarts. Key: the topic's context id, or "general".
        self._models_path = _state_file(config, "models")
        self._models: Dict[str, str] = {}
        for key, value in load_state(self._models_path, "model choices").items():
            # The file sits where an agent may write: keep only names /model would accept.
            if isinstance(value, str) and (value == config.adapter.model or (
                    config.adapter.model_switchable
                    and model_problem(config.adapter.type, value) is None)):
                self._models[str(key)] = value
            else:
                logger.warning("ignoring model choice %r for %s in %s: not a model /model "
                               "accepts", value, key, self._models_path)
        # /reasoning: the reasoning effort each discussion runs at, the same way.
        self._efforts_path = _state_file(config, "efforts")
        self._efforts: Dict[str, str] = {}
        for key, value in load_state(self._efforts_path, "effort choices").items():
            if isinstance(value, str) and not config.adapter.command \
                    and effort_problem(config.adapter.type, value) is None:
                self._efforts[str(key)] = value
            else:
                logger.warning("ignoring effort choice %r for %s in %s", value, key,
                               self._efforts_path)
        self._outbound_request_seq = 0

        self.bot: Any = None
        self.dispatcher: Any = None
        self.restart_requested = False           # request_restart(): the launcher starts again
        self.replies = None
        self._reply_task: Optional[asyncio.Task] = None
        if config.adapter.type == "codex_thread" and (config.source_path or config.adapter.reply_state_path):
            from swarmly.telegram.replies import ReplyStore, reply_state_path
            if config.source_path is None:
                raise ValueError("codex_thread reply tickets require config.source_path")
            self.replies = ReplyStore(reply_state_path(config))

        self.router = ProjectRouter(list(config.projects.values()), config.identity.home_channel_id)
        self._project_adapters: Dict[str, AgentAdapter] = {}
        self._github_task: Optional[asyncio.Task] = None
        self._github_last_poll: Optional[str] = None
        self._github_alert_at: Optional[float] = None
        self._topic_recent_mentions: Dict[Tuple[int, Optional[int]], List[Tuple[float, str]]] = {}

    def record_topic_turn(self, chat_id: int, thread_id: Optional[int], text: str) -> None:
        """Track recent text/references in a topic to prevent duplicate backstop turns."""
        key = (chat_id, thread_id)
        if key not in self._topic_recent_mentions:
            self._topic_recent_mentions[key] = []
        now = self._clock()
        self._topic_recent_mentions[key] = [
            (t, txt) for t, txt in self._topic_recent_mentions[key] if now - t < 7200
        ]
        self._topic_recent_mentions[key].append((now, text))

    def has_recent_ref(self, chat_id: int, thread_id: Optional[int], repo: str, number: int) -> bool:
        """Whether a given issue/PR number or reference was mentioned recently in the topic."""
        key = (chat_id, thread_id)
        entries = self._topic_recent_mentions.get(key, [])
        now = self._clock()
        pat = re.compile(
            rf"(?:(?:\b(?:PR|issue|pull/|issues/)\s*#?|(?<!\w)#){number}\b|{re.escape(repo)}[#/]{number}\b)",
            re.IGNORECASE,
        )
        for t, txt in entries:
            if now - t < 7200 and pat.search(txt):
                return True
        return False

    def _workdir_for(self, chat_id: int, thread_id: Optional[int]) -> Path:
        project = self.router.project_for(chat_id, thread_id)
        if project is not None and thread_id is not None:
            return self.router.workdir_for(project, thread_id)
        return Path(self.config.adapter.cwd or ".").expanduser().resolve()

    def _adapter_for(self, chat_id: int, thread_id: Optional[int]) -> AgentAdapter:
        project = self.router.project_for(chat_id, thread_id)
        if project is None or thread_id is None:
            return self.adapter
        workdir = self.router.workdir_for(project, thread_id)
        cache_key = str(workdir)
        if cache_key not in self._project_adapters:
            cfg_adapter = dataclasses.replace(self.config.adapter, cwd=str(workdir))
            cfg = dataclasses.replace(self.config, adapter=cfg_adapter)
            adapter = build_adapter(cfg, transport=True)
            sessions = getattr(adapter, "SESSIONS_STATE", None)
            if sessions and hasattr(adapter, "use_state_file"):
                suffix = f"{project.key}-t{thread_id}" if project.worktrees else project.key
                adapter.use_state_file(_state_file(self.config, sessions, suffix=suffix))
            self._project_adapters[cache_key] = adapter
        return self._project_adapters[cache_key]

    # ----------------------------------------------------------------------------------
    # Addressing
    # ----------------------------------------------------------------------------------
    def mentions_me(self, text: str) -> bool:
        if not text:
            return False

        # 1. Exact Telegram username mention (e.g. @your_agent_bot)
        username = self.config.identity.name.lstrip("@")
        if username and re.search(rf"(?i)@{re.escape(username)}\b", text):
            return True

        # 2. Alias mention with @ prefix: @Antigravity, @Codex, @Hermes
        alias = self.config.identity.alias.strip()
        if alias and re.search(rf"(?i)@{re.escape(alias)}\b", text):
            return True

        # 3. Direct addressing at start of line or message: 'Antigravity, ...' or 'Hermes: ...'
        # Must not be preceded by '[' (e.g. sender banner like [Codex])
        if alias and re.search(rf"(?i)(?:^|[\r\n]+)\s*{re.escape(alias)}\s*[:,]", text):
            return True

        # 4. Swarm broadcast mentions (@all, @everyone, @agents, @swarm)
        if re.search(r"(?i)@(all|everyone|agents|swarm)\b", text):
            return True

        return False

    def exact_username_mentioned(self, text: str) -> bool:
        """Return whether the configured Telegram username is explicitly addressed.

        This is intentionally narrower than :meth:`mentions_me`; the caller decides
        whether the configured adapter opts into exact-username delivery.
        """
        if not text:
            return False
        username = self.config.identity.name.lstrip("@")
        if username and re.search(rf"(?i)@{re.escape(username)}\b", text):
            return True
        return False

    def _dropped_handles(self, text: str) -> List[str]:
        """The handles in *text* by which its author named this bot (issue #27).

        A hand-off names its recipients; every one of those handles is the sender telling this
        bot it is addressed, whichever handle (Telegram username or swarm alias) it used."""
        found: List[str] = []
        for handle in (self.config.identity.name.lstrip("@"),
                       self.config.identity.alias.strip()):
            if not handle or not text:
                continue
            if re.search(rf"(?i)@{re.escape(handle)}\b", text):
                written = f"@{handle}"
                if written.lower() not in {f.lower() for f in found}:
                    found.append(written)
        return found

    def _relayed_through_peer(self, event: ChannelEvent) -> bool:
        """A peer bot's message that opens with another bot's handle and names this bot only
        as whom that bot should hand the work to ("@A please hand @me a task", "@A ask @me
        to ...") addresses A, not this bot (live test: both ran the work, and this bot opened
        a stray topic). A line that opens with this bot's handle still addresses it."""
        if event.replies_to_bot or not self.mentions_me(event.text):
            return False
        text = re.sub(r"^\s*\[[^\]\n]{1,40}\]\s*", "", event.text or "")
        me = [re.escape(n) for n in (self.config.identity.name.lstrip("@"),
                                     self.config.identity.alias.strip()) if n]
        if not me:
            return False
        mine = "|".join(me)
        leading = re.match(r"^(?:\s*@\w+[:,]?)+", text)
        if not leading or re.search(rf"(?i)@(?:{mine})\b", leading.group(0)):
            return False
        if not self._mentions_other_peer(leading.group(0)):
            return False
        if re.search(rf"(?im)^\s*(?:[-*•]|\d+[.)])?\s*@(?:{mine})\b", text):
            return False
        delegated = re.compile(
            r"(?i)\b(?:hand|ask|tell|have|get|assign|delegate|give|pass|forward|send|route|"
            r"ping|nudge|let)\w*\s+(?:(?:it|this|that|the\s+\w+|a\s+\w+)\s+)?"
            rf"(?:(?:over\s+)?to\s+)?@(?:{mine})\b")
        mentions = re.findall(rf"(?i)@(?:{mine})\b", text)
        return len(delegated.findall(text)) >= len(mentions)

    def _mentions_other_peer(self, text: str) -> bool:
        if not text:
            return False
        for peer_alias, peer_target in self.config.peers.items():
            bare_target = str(peer_target).lstrip("@")
            if bare_target and re.search(rf"(?i)@{re.escape(bare_target)}\b", text):
                return True
            if peer_alias and re.search(rf"(?i)@{re.escape(peer_alias)}\b", text):
                return True
            if peer_alias and re.search(rf"(?i)(?:^|[\r\n]+)\s*{re.escape(peer_alias)}\s*[:,]", text):
                return True
        return False

    def _first_mention_at(self, text: str, username: str, alias: str) -> Optional[int]:
        """Where *text* first names a bot, by @username, @alias or a leading "Alias:"."""
        patterns = []
        if username:
            patterns.append(rf"(?i)@{re.escape(username)}\b")
        if alias:
            patterns.append(rf"(?i)@{re.escape(alias)}\b")
            patterns.append(rf"(?i)(?:^|[\r\n]+)\s*{re.escape(alias)}\s*[:,]")
        found = [m.start() for pat in patterns for m in [re.search(pat, text)] if m]
        return min(found) if found else None

    def _named_bots(self, text: str) -> Tuple[Optional[int], List[Tuple[int, str]]]:
        """Where *text* first names this bot, and (position, alias) for each peer bot it
        names. Peers that are the owner or not bots are left out."""
        mine = self._first_mention_at(text, self.config.identity.name.lstrip("@"),
                                      self.config.identity.alias.strip())
        owners = set(self.config.owner_peers())
        peers = []
        for peer_alias, peer_target in self.config.peers.items():
            if peer_alias in owners:
                continue
            at = self._first_mention_at(text, str(peer_target).lstrip("@"), peer_alias)
            if at is not None:
                peers.append((at, peer_alias))
        return mine, sorted(peers)

    def _first_named_rule(self, text: str) -> bool:
        """Whether ``limits.multi_mention: first`` applies: a swarm broadcast (@all,
        @agents...) still reaches every bot."""
        return (getattr(self.config.limits, "multi_mention", "first") == "first"
                and not re.search(r"(?i)@(all|everyone|agents|swarm)\b", text))

    def _peer_named_first(self, text: str) -> Optional[str]:
        """With ``limits.multi_mention: first``, a person's message that names several bots
        starts a turn only on the first one named; the rest wait for a hand-off. Returns the
        alias of the peer named before this bot, if any."""
        if not self._first_named_rule(text):
            return None
        mine, peers = self._named_bots(text)
        if mine is None or not peers or peers[0][0] > mine:
            return None
        return peers[0][1]

    def _peers_waiting(self, text: str) -> List[str]:
        """The peer bots a person's message names after this one: they wait for this bot to
        hand the work on."""
        if not self._first_named_rule(text):
            return []
        mine, peers = self._named_bots(text)
        if mine is None:
            return []
        return [alias for at, alias in peers if at > mine]

    def _waiting_note(self, text: str) -> str:
        """A line for the bot that acts on a person's multi-bot message, naming the bots that
        wait for it to hand the work on (they did not see the message)."""
        waiting = self._peers_waiting(text)
        if not waiting:
            return ""
        handles = []
        for alias in waiting:
            target = str(self.config.peers.get(alias, "")).lstrip("@")
            handles.append(f"@{target}" if target and not target.isdigit() else alias)
        return (f"\n\n[Also named in this message: {', '.join(handles)}. They have not "
                "started on it and wait until you hand the work on with an @mention.]")

    def _expect_handoff(self, event: ChannelEvent, peer_alias: str) -> None:
        """Remember, for a while, that this bot waits for *peer_alias* to hand it the work in
        this topic: that hand-off then skips the bot-to-bot cooldown once. Nothing of the
        message itself is kept."""
        key = (event.chat_id, event.thread_id)
        notes = self._handoff_notes
        notes.pop(key, None)
        notes[key] = (peer_alias.lower(), self._clock() + HANDOFF_NOTE_SECONDS)
        while len(notes) > _PACE_STATE_LIMIT:
            notes.pop(next(iter(notes)))

    def _is_expected_handoff(self, event: ChannelEvent) -> bool:
        key = (event.chat_id, event.thread_id)
        note = self._handoff_notes.get(key)
        if note is None:
            return False
        if note[1] < self._clock():
            del self._handoff_notes[key]
            return False
        author = self.config.peer_alias_for(event.author_username, event.author_id)
        if author is None or author.lower() != note[0]:
            return False
        del self._handoff_notes[key]
        return True

    def strip_mention(self, text: str) -> str:
        out = text or ""
        username = self.config.identity.name.lstrip("@")
        alias = self.config.identity.alias.strip()
        patterns = [
            rf"(?i)@{re.escape(username)}\b",
            rf"(?i)@{re.escape(alias)}\b",
            rf"(?i)(?:^|[\r\n]+)\s*{re.escape(alias)}\s*[:,]?",
        ]
        for pat in patterns:
            out = re.sub(pat, " ", out)
        return " ".join(out.split())

    def _may_start_turns(self, event: ChannelEvent) -> bool:
        """A bot runs commands on its machine (adapter.permissions), so by default only the
        swarm can start a turn or steer one: the admin, the maintainer, agent.allowed_users
        and peer bots. Each ignored author is logged once, as a WARNING, so a peer missing
        from ``peers:`` or a wrong ``admin_user_id`` shows up."""
        if event.self_task:
            return True
        if getattr(self.config.limits, "strangers", "ignore") != "ignore" or \
                self.config.is_member(event.author_id, event.author_username,
                                      event.author_is_bot):
            return True
        who = (event.author_id, (event.author_username or "").lower())
        if who not in self._strangers_seen and len(self._strangers_seen) < 256:
            self._strangers_seen.add(who)
            logger.warning(
                "%s ignores @%s (id=%s, bot=%s): not the admin, an allowed user or a peer. "
                "List the bot in peers: or /admit it, check agent.admin_user_id, or set "
                "limits.strangers: answer", self.config.identity.alias,
                event.author_username, event.author_id, event.author_is_bot)
        return False

    def _is_admin_dm(self, event: ChannelEvent, in_home_channel: bool) -> bool:
        """The admin's private chat with this bot. Only a private chat: its id is the user's
        own id. Any other group the bot is in is not the home channel and is ignored; before,
        every admin message in such a group counted as a DM and was answered unaddressed."""
        return (not in_home_channel and self.config.is_admin(event.author_id)
                and event.author_id is not None and int(event.chat_id) == int(event.author_id))

    def should_respond(self, event: ChannelEvent) -> tuple[bool, str]:
        """Decide whether this bot answers. Returns (decision, reason) — reason aids debugging."""
        if event.text is None:
            return False, "not a text message"
        if event.self_task:
            return True, "this agent's own brief (a topic it opened, or a membership turn)"
        if event.author_id is not None and self.bot is not None:
            me = getattr(self.bot, "id", None)
            if me is not None and int(event.author_id) == int(me):
                return False, "own message"

        # Direct message from admin outside home channel is always answered
        in_home_channel = (self.config.identity.home_channel_id is None
                           or event.chat_id == self.config.identity.home_channel_id)
        is_admin_dm = self._is_admin_dm(event, in_home_channel)
        if is_admin_dm:
            return True, "admin DM"

        if not self._may_start_turns(event):
            return False, "not a member of the swarm (limits.strangers: ignore)"

        # The second and later parts of a split reply complete a message already delivered;
        # on their own they are fragments, so they never start a turn (not even a stateful one).
        if event.author_is_bot and event.text and _CONTINUATION_RE.match(event.text.strip()):
            return False, "continuation of a split message"

        # A peer transport's own notices ("[Alias] ⚠️ paused ...", "↪ moved this to topic")
        # and built-in command answers are machine traffic, however they are addressed (a
        # notice goes out as a reply, so it used to start a "reply to this bot" turn).
        if event.author_is_bot and event.text:
            prose = machine_prose(event.text)
            if prose.startswith(_NOTICE_PREFIXES):
                return False, "automated status/notice from peer bot"
            if is_command_reply(event.text):
                return False, "built-in command reply from peer bot (machine traffic)"
            # Issue #27: a list a peer bot writes ("@a, @b, @c" on one line or several) is a
            # hand-off, not a roster: every @handle in it counts as addressed. Only the
            # relay rule below still keeps a named bot from taking work meant for another,
            # and a drop there is reported to the sender (``_mention_drop_notice``).
            if self._relayed_through_peer(event):
                return False, "a peer asks another bot to hand this bot the work"

        # A stateful adapter explicitly opts in to delivery of every message that
        # contains its Telegram username. This bypasses status/ack and cooldown filtering:
        # the addressed message belongs in the active conversation.
        adapter = self._adapter_for(event.chat_id, event.thread_id)
        explicit_username_delivery = (
            self.mentions_me(event.text)
            and adapter.accepts_explicit_username_delivery()
        )

        # Terminal closures are never task delivery, even for stateful adapters that otherwise
        # opt into every explicit username mention. This prevents FYI notes from opening tickets.
        if event.author_is_bot and event.text:
            # Mentions are how Telegram delivers; strip leading @mentions so an addressed
            # closure ("@bot: closing here") classifies as terminal too.
            if is_terminal_peer_ack(machine_prose(event.text)):
                return False, "terminal acknowledgment/sign-off from peer bot (no reply needed)"

        # Peer bot handling for non-opted-in adapters: filter automated status notices and respect
        # turn cooldown. Explicit username delivery intentionally bypasses these two filters,
        # and so do commands (/ping, /status, ...): they are answered by the transport, not a
        # turn, and a dropped /ping reads as a dead bot.
        command = parse_command(event.text)
        is_control = command is not None and command[0] in ("stop", "model", "reasoning",
                                                             "effort", *DIAG_COMMANDS)
        if is_control and command is not None and command[1] and command[1].lstrip(
                "@").lower() != self.config.identity.name.lstrip("@").lower():
            return False, "a command for another bot"
        if event.author_is_bot and not explicit_username_delivery and not is_control:
            # The cooldown applies in General only, where it is the only loop brake, and drops
            # the message. A forum topic has the peer-turn cap instead, and no cooldown.
            # The hand-off a person's multi-bot message left this bot waiting for skips the
            # cooldown once, so it is never lost to it; the note is spent only then. A dropped
            # message that named this bot gets a notice, so the loss is visible (live test:
            # a real request vanished without a word).
            min_cooldown = getattr(self.config.limits, "min_seconds_between_agent_turns", 0.0)
            if min_cooldown > 0 and event.thread_id is None:
                elapsed = self._clock() - self._last_turn_for(event)
                if elapsed < min_cooldown:
                    if self.mentions_me(event.text) and self._is_expected_handoff(event):
                        event.handoff = True
                    else:
                        if self.mentions_me(event.text):
                            event.cooldown_left = min_cooldown - elapsed
                        return False, f"cooldown active ({elapsed:.1f}s < {min_cooldown}s)"

        if event.replies_to_bot:
            return True, "reply to this bot"

        # A person naming several bots in one message gets one turn: the first bot named acts
        # and the others wait to be handed the work with an @mention. /stop and the liveness
        # commands are not turns: every bot they name answers them.
        if not event.author_is_bot and not is_control and self.mentions_me(event.text):
            first = self._peer_named_first(event.text)
            if first is not None:
                self._expect_handoff(event, first)
                return False, f"{first} is named first (limits.multi_mention: first)"

        # If this agent is mentioned, ALWAYS DELIVER to current conversation!
        if self.mentions_me(event.text):
            return True, "mentioned"

        if self._mentions_other_peer(event.text):
            return False, "addressed to another peer"

        if not event.author_is_bot and self.config.limits.respond_without_mention:
            return True, "non-bot author and respond_without_mention is on"

        return False, "not addressed to this agent"

    def register_author(self, event: ChannelEvent) -> None:
        """No-op: loop guard removed per user contract."""
        pass

    def loop_budget_exhausted(self) -> bool:
        """No-op: loop guard removed per user contract."""
        return False

    # ----------------------------------------------------------------------------------
    # v1.1 delivery semantics — commands (§6), ACKs and escalation (§5)
    # ----------------------------------------------------------------------------------
    def _short_id(self) -> str:
        """A short id unique per sender per day (spec §4)."""
        self._outbound_request_seq += 1
        day = time.strftime("%Y%m%d", time.gmtime())
        return f"h-{day}-{self._outbound_request_seq}"

    def _commands_enabled(self) -> bool:
        return bool(getattr(self.config.limits, "commands_enabled", True))

    @staticmethod
    def _bot_account_post(event: ChannelEvent) -> bool:
        """A bot account wrote this post itself. Posts made on behalf of a chat come from
        Telegram's stand-in users @GroupAnonymousBot (anonymous admins) and @Channel_Bot
        (sent as a channel), both ``is_bot``; forwards and automatic forwards carry someone
        else's words."""
        raw = event.raw
        return event.author_is_bot and not (
            getattr(raw, "sender_chat", None) is not None
            or getattr(raw, "is_automatic_forward", False)
            or getattr(raw, "forward_origin", None) is not None
            or getattr(raw, "forward_date", None) is not None
            or getattr(raw, "via_bot", None) is not None)

    def _alias_ref(self) -> Optional[str]:
        """This bot's alias for a ``ref=`` field: one header token, or left out."""
        from swarmly.telegram.roster import ALIAS_RE
        alias = self.config.identity.alias
        return alias if ALIAS_RE.match(alias) else None

    async def _handle_roster(self, envelope: Envelope, event: ChannelEvent) -> bool:
        """Swarm membership (#152): note unknown bots' HELLOs, apply the admin's ``/admit``,
        ``/remove`` and ``/sync``, and take the swarm's welcomes and peer lists. True when the
        message is consumed. Joins, roles and drop-outs go to :mod:`membership` first."""
        if envelope.type in (MessageType.HELLO, MessageType.BYE) and self._bot_account_post(event):
            if await self.membership.handle(envelope, parse_message(event.text or "")[1], event):
                return True
        elif not event.author_is_bot and await self.membership.handle_command(event):
            return True
        if envelope.type == MessageType.HELLO and self._bot_account_post(event):
            intent = (envelope.intent or "").upper()
            ref = str(envelope.ref) if envelope.ref else None
            if intent == "ONLINE":
                self.roster.saw_online(ref, event.author_username)
            elif (intent == "WELCOME"
                    and (envelope.to or "").lower() == self.config.identity.alias.lower()
                    and self.exact_username_mentioned(event.text or "")):
                self.roster.welcomed(ref, event.author_username)
            elif intent == "SYNC":
                self.roster.synced(event.author_username, event.text or "")
            return False                  # HELLO stays machine traffic, recorded as before
        parsed = parse_command(event.text or "")
        if parsed is None or parsed[0] not in ("admit", "remove", "sync") or event.author_is_bot:
            return False
        command, target = parsed
        if target and target.lstrip("@").lower() != self.config.identity.name.lstrip("@").lower():
            return True                   # aimed at another bot
        if not self.config.is_admin(event.author_id):
            logger.warning("/%s from @%s ignored: only the admin changes the swarm",
                           command, event.author_username)
            return True
        if command == "sync":
            self.roster.start_sync()
            await self.send(format_message(
                Envelope(type=MessageType.HELLO, to="*", intent="SYNC",
                         ref=self._alias_ref(), rev=_local_revision()),
                f"🔄 {self.config.identity.alias} peers: {self.roster.sync_list() or 'none'}"),
                chat_id=event.chat_id, thread_id=event.thread_id, silent=True,
                allow_when_frozen=True)
            return True
        handles = self.roster.handles(event.text or "")
        if command == "remove":
            for alias in self.roster.remove(handles):
                self.membership.forget(alias)
            return True
        for alias, username in self.roster.admit(handles):
            await self.send(format_message(
                Envelope(type=MessageType.HELLO, to=alias, intent="WELCOME",
                         ref=self._alias_ref(), rev=_local_revision()),
                f"👋 @{username} {self.config.identity.alias} has added {alias} as a peer"),
                chat_id=event.chat_id, thread_id=event.thread_id, silent=True,
                allow_when_frozen=True)
        return True

    def _command_reply(self, command: str, event: Optional[ChannelEvent] = None) -> str:
        alias = self.config.identity.alias
        if command == "ping":
            return ping_reply(alias, state="online")   # wall-clock time; _clock is monotonic
        if command == "status":
            result = f"handled={self.stats.handled} machine={self.stats.machine_events}"
            # #93(1): busy drops are counted inside `seen` (they bump `ignored`, which /diag sums
            # into it) and additionally surfaced here as their own `busy_dropped=` token. /status is
            # the lighter surface where a loss anomaly is first noticed, so it must not hide it.
            result += f" busy_dropped={self.stats.busy_dropped}"
            result += f" peers={','.join(self.config.peers) or 'none'}"
            if self.config.identity.role:
                result += f" role={self._role_name()}"
            if self.replies is not None:
                result += f" replies={self.replies.summary()}"
            if self.config.adapter.model_switchable:
                result += f" model={self._model_for(event) or 'cli-default'}"
                result += f" effort={self._effort_for(event) or 'cli-default'}"
            if event is not None:
                proj = self.router.project_for(event.chat_id, event.thread_id)
                if proj:
                    result += f" project={proj.key}"
            if self.config.github and self.config.github.login:
                last_poll = getattr(self, "_github_last_poll", None) or "never"
                result += f" github: @{self.config.github.login} (backstop: {self.config.github.poll_minutes}m, last: {last_poll})"
            else:
                result += " github: disabled"
            return status_reply(alias, task=f"adapter:{self.config.adapter.type}",
                                result=result,
                                blocker=self.stats.last_error or "none")
        if command == "role":
            role = self._role()
            if role is None:
                mine = (f"ROLE {alias} none (the coordinator gives one; the admin can "
                        f"/role {self.config.identity.username} <role>)")
            else:
                mine = f"ROLE {alias} {role.name}: {role.description}"
            from swarmly.telegram.roles import team_line
            team = team_line(self.config.peer_roles)
            return f"{mine}\n{team}" if team else mine
        if command == "where":
            chat_id = event.chat_id if event is not None else int(self.config.identity.home_channel_id or 0)
            thread_id = event.thread_id if event is not None else None
            project = self.router.project_for(chat_id, thread_id)
            workdir = str(self.router.workdir_for(project, thread_id)) if project and thread_id is not None else None
            return where_reply(
                alias=alias,
                chat_id=chat_id,
                thread_id=thread_id,
                project=project.key if project else None,
                repo=project.repo if project else None,
                workdir=workdir,
            )
        context = (self.topics.context(event.chat_id, event.thread_id)
                   if event is not None else None)
        diag = diag_reply(alias, bot=self.config.identity.username, node=socket.gethostname(),
                          transport="standalone", session=self._session_label(context, event),
                          group=str(self.config.identity.home_channel_id),
                          admin=None, seen=self.stats.handled + self.stats.ignored)
        # #9: a busy drop is inside `seen` — it bumps `ignored`, which the sum above counts — and is
        # also surfaced as its own `busy_dropped=` token. The separate token is the property to pin,
        # since `seen` alone cannot tell a drop apart from any other ignored message.
        topic = f" context={context}" if context else ""
        return f"{diag} busy_dropped={self.stats.busy_dropped}{topic}"

    def _session_label(self, context_id: Optional[str], event: Optional[ChannelEvent] = None) -> Optional[str]:
        """The adapter session a turn in this discussion resumes, when the adapter keeps one
        per discussion; else the configured conversation."""
        adapter = (self._adapter_for(event.chat_id, event.thread_id)
                   if event is not None else self.adapter)
        session_for = getattr(adapter, "session_for", None)
        if callable(session_for):
            try:
                return str(session_for(IncomingMessage(text="", author_alias="",
                                                       context_id=context_id)))
            except Exception:                         # noqa: BLE001 - diagnostics only
                logger.debug("could not work out the session for /diag", exc_info=True)
        return self.config.adapter.conversation

    def _model_key(self, event: Optional[ChannelEvent]) -> str:
        context = (self.topics.context(event.chat_id, event.thread_id)
                   if event is not None else None)
        return context or "general"

    def _model_for(self, event: Optional[ChannelEvent]) -> Optional[str]:
        """The model this discussion runs: its /model choice, else adapter.model (None: the
        CLI's own default)."""
        return self._models.get(self._model_key(event)) or self.config.adapter.model

    def _effort_for(self, event: Optional[ChannelEvent]) -> Optional[str]:
        """The reasoning effort this discussion runs at: its /reasoning choice, else
        adapter.effort (None: the CLI's own default)."""
        return self._efforts.get(self._model_key(event)) or self.config.adapter.effort

    def _reasoning_command(self, event: ChannelEvent) -> str:
        """``/reasoning`` shows the discussion's effort; ``/reasoning <level>`` (admin only)
        sets it from the next turn; ``/reasoning default`` returns to adapter.effort. Only
        this bot's runtime choice changes: no config file or CLI default is touched."""
        alias = self.config.identity.alias
        adapter = self.config.adapter
        args = command_args(event.text or "")
        if adapter.command or effort_problem(adapter.type, "high"):
            why = ("adapter.command sets the command line" if adapter.command
                   else f"the {adapter.type} adapter has no effort setting")
            return f"{alias} cannot set reasoning effort: {why}."
        key = self._model_key(event)
        if not args:
            current = self._effort_for(event)
            source = ("/reasoning" if key in self._efforts
                      else "adapter.effort" if adapter.effort else "the CLI's default")
            return (f"{alias} reasons at {current or 'its CLI default effort'} here ({source}). "
                    f"The admin can set it with /reasoning@{self.config.identity.name} "
                    f"<{'|'.join(EFFORT_LEVELS)}>.")
        if len(args) > 1:
            return f"{alias}: /reasoning takes one level (or default)."
        if not self.config.is_admin(event.author_id):
            logger.warning("/reasoning from @%s ignored: only the admin sets it",
                           event.author_username)
            return f"{alias}: only the admin can set my reasoning effort."
        level = args[0].lower()
        if level == "default":
            self._efforts.pop(key, None)
            save_state(self._efforts_path, self._efforts, "effort choices")
            return (f"{alias} goes back to {adapter.effort or 'its CLI default effort'} here "
                    "from the next turn.")
        problem = effort_problem(adapter.type, level)
        if problem:
            return f"{alias} kept {self._effort_for(event) or 'its effort'}: {problem}."
        self._efforts[key] = level
        save_state(self._efforts_path, self._efforts, "effort choices")
        logger.info("/reasoning: %s now reasons at %s in %s", alias, level, key)
        return f"{alias} reasons at {level} here from the next turn."

    def _model_command(self, event: ChannelEvent) -> str:
        """``/model`` shows the discussion's model; ``/model <name>`` (admin only) switches it
        from the next turn, keeping the session; ``/model default`` returns to adapter.model."""
        alias = self.config.identity.alias
        adapter = self.config.adapter
        args = command_args(event.text or "")                 # without the bots it names
        if not adapter.model_switchable:
            why = ("adapter.command sets the command line" if adapter.command
                   else f"the {adapter.type} adapter has no model setting")
            return f"{alias} cannot switch models: {why}."
        key = self._model_key(event)
        if not args:
            current = self._model_for(event)
            source = ("/model" if key in self._models
                      else "adapter.model" if adapter.model else "the CLI's default")
            return (f"{alias} runs {current or 'its CLI default model'} here ({source}). "
                    f"The admin can switch it with /model@{self.config.identity.name} <name>.")
        if len(args) > 1:
            return f"{alias}: /model takes one model name (or default)."
        if not self.config.is_admin(event.author_id):
            logger.warning("/model from @%s ignored: only the admin switches models",
                           event.author_username)
            return f"{alias}: only the admin can switch my model."
        name = args[0]
        if name.lower() == "default":
            self._models.pop(key, None)
            save_state(self._models_path, self._models, "model choices")
            return (f"{alias} goes back to {adapter.model or 'its CLI default model'} here "
                    "from the next turn.")
        if name != adapter.model:
            problem = model_problem(adapter.type, name)
            if problem:
                return f"{alias} kept {self._model_for(event) or 'its model'}: {problem}."
        self._models[key] = name
        save_state(self._models_path, self._models, "model choices")
        logger.info("/model: %s now runs %s in %s", alias, name, key)
        return f"{alias} runs {name} here from the next turn; the conversation carries on."

    def _direct_chat(self, event: ChannelEvent) -> bool:
        """The admin's private chat with this bot, where a bare command can only mean this bot.

        In Telegram a private chat's id is the user's id; any other chat outside the home
        group is a group that may hold several bots, where a bare command names none.
        """
        home = self.config.identity.home_channel_id
        return (home is not None and event.chat_id != home and event.author_id is not None
                and event.chat_id == event.author_id and self.config.is_admin(event.author_id))

    def _handle_command(self, event: ChannelEvent) -> Optional[str]:
        """Transport-level reply for /ping /status /diag, or None when this is not one for us.

        A liveness probe must not cost a runtime turn (spec §6), so commands are answered here
        and never handed to the adapter. A command aimed at another bot's username — or a bare
        one that does not address this bot — gets no reply.
        """
        if not self._commands_enabled():
            return None
        parsed = parse_command(event.text or "")
        if not parsed:
            return None
        command, target = parsed
        if command == "effort":
            command = "reasoning"
        if command not in (*DIAG_COMMANDS, "model", "reasoning"):
            return None
        if target:
            if target.lstrip("@").lower() != self.config.identity.name.lstrip("@").lower():
                return None
        elif not (self.mentions_me(event.text or "") or event.replies_to_bot
                  or self._direct_chat(event)):
            return None
        if command == "model":
            return self._model_command(event)
        if command == "reasoning":
            return self._reasoning_command(event)
        return self._command_reply(command, event)

    def _envelope_addressed_to_me(self, envelope: Envelope) -> bool:
        target = (envelope.to or "").strip().lstrip("@").lower()
        if not target or target == "*":
            return False
        return target in {self.config.identity.alias.strip().lower(),
                          self.config.identity.name.lstrip("@").lower()}

    def _handle_inbound_ack(self, envelope: Envelope, event: ChannelEvent) -> bool:
        """Bookkeeping for a machine ACK addressed to this agent; True when consumed.

        An ACK is never answered and never reaches the adapter (never ACK an ACK); it exists
        to close an outstanding REQUEST. This runs before the routing gate because the
        terminal-ack filter would otherwise swallow the message as chatter.
        """
        if envelope.type != MessageType.ACK:
            return False
        if not (self.mentions_me(event.text or "") or self._envelope_addressed_to_me(envelope)):
            return False
        ref = str(envelope.ref) if envelope.ref else ""
        closed = self.acks.ack(ref) if ref else False
        logger.info("inbound ACK ref=%s closed=%s", ref or "<missing>", closed)
        return True

    async def _ack_inbound_request(self, envelope: Envelope, event: ChannelEvent) -> None:
        """Post the protocol ACK for an addressed REQUEST from a peer (spec §5, Rule 5)."""
        if envelope.type != MessageType.REQUEST or not event.author_is_bot:
            return
        ref = str(envelope.id) if envelope.id else (
            str(event.message_id) if event.message_id is not None else "unknown")
        ack = Envelope(type=MessageType.ACK, to=self._alias_for(event), ref=ref, id=self._short_id())
        await self.send(format_message(ack), chat_id=event.chat_id, thread_id=event.thread_id)

    def _freeze_status_for_me(self, envelope: Envelope) -> bool:
        target = (envelope.to or "*").strip().lstrip("@").lower()
        if target == "*":
            return True
        return target in {self.config.identity.alias.strip().lower(),
                          self.config.identity.name.lstrip("@").lower()}

    async def _handle_freeze_status(self, envelope: Envelope, event: ChannelEvent) -> Optional[str]:
        """Apply channel-reversible FREEZE/RESUME controls without waking the adapter."""
        if envelope.type != MessageType.STATUS or (envelope.intent or "").upper() not in {
                "FREEZE", "RESUME"} or not self._freeze_status_for_me(envelope):
            return None
        if ((envelope.to or "*").strip().lstrip("@") in {"", "*"}      # "@*" is a broadcast
                and self.config.limits.require_admin_for_broadcast
                and not self.config.is_admin(event.author_id)):
            return format_message(Envelope(
                type=MessageType.BLOCKED, to=self._alias_for(event), intent="UNAUTHORIZED",
                ref=envelope.ref, rev=_local_revision()))
        intent = (envelope.intent or "").upper()
        if intent == "FREEZE":
            self._set_frozen(True, envelope.ref)
            ack_intent = "FROZEN"
        else:
            self._set_frozen(False, envelope.ref)
            ack_intent = "RESUMED"
        return format_message(Envelope(
            type=MessageType.STATUS, to="*", intent=ack_intent,
            ref=envelope.ref, rev=_local_revision()))

    # ------------------------------------------------------------------------------
    # #49: freeze persistence, auto-freeze on quota exhaustion, alert discipline
    # ------------------------------------------------------------------------------

    _QUOTA_SIGNATURE = re.compile(r"429|quota|usage limit|token plan|rate limit", re.IGNORECASE)

    def _freeze_state_path(self) -> Optional[Path]:
        """Where the freeze flag persists next to the config (survives restarts, #49.F)."""
        source = self.config.source_path
        if source is None:
            return None
        return Path(source).with_suffix(".freeze.json")

    def _set_frozen(self, frozen: bool, ref: Optional[str]) -> None:
        """Apply and persist the freeze flag so a restart cannot silently release it."""
        self._frozen = frozen
        self._freeze_ref = ref if frozen else None
        if not frozen:
            self._quota_failures = 0
        path = self._freeze_state_path()
        if path is None:
            return
        try:
            if frozen:
                path.write_text(json.dumps({"frozen": True, "ref": ref, "at": self._clock()}))
            elif path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("freeze state could not be persisted: %s", exc)

    async def _restore_freeze_state(self) -> None:
        """Re-apply a persisted freeze on startup and re-announce it once."""
        path = self._freeze_state_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("unreadable freeze state at %s; ignoring", path)
            return
        if not isinstance(data, dict) or not data.get("frozen"):
            return
        ref = data.get("ref")
        self._frozen = True
        self._freeze_ref = str(ref) if ref else None
        logger.info("freeze state restored from %s (ref=%s)", path, ref)
        await self.send(format_message(Envelope(
            type=MessageType.STATUS, to="*", intent="FROZEN",
            ref=self._freeze_ref, rev=_local_revision())), allow_when_frozen=True)

    async def _note_adapter_failure(self, detail: str) -> None:
        """#49.E: repeated quota-signature failures freeze the channel before the ticket storm.

        Deliberately narrow: only failure text carrying a known quota/429 signature counts,
        a non-quota failure resets the streak, and the freeze never fires twice (RESUME or
        the persisted state is the only way back).
        """
        if self._frozen or not self.config.limits.auto_freeze_on_quota:
            return
        if not self._QUOTA_SIGNATURE.search(detail or ""):
            self._quota_failures = 0
            return
        self._quota_failures += 1
        threshold = max(1, int(self.config.limits.quota_freeze_threshold))
        logger.warning("quota-signature adapter failure %d/%d", self._quota_failures, threshold)
        if self._quota_failures >= threshold:
            self._set_frozen(True, "quota")
            self.stats.last_error = "auto-frozen: adapter quota exhausted"
            await self.send(format_message(Envelope(
                type=MessageType.STATUS, to="*", intent="FROZEN", ref="quota",
                rev=_local_revision())), allow_when_frozen=True)

    # Machine-class envelope kinds recorded as state without waking the adapter (token
    # economy, issue #43). REQUEST/RESPONSE/BLOCKED are never machine-class.
    _MACHINE_STATUS_INTENTS = frozenset({"ONLINE", "OFFLINE", "FROZEN", "RESUMED"})
    _MACHINE_NOTE_INTENTS = frozenset({"FYI", "CLOSED", "RECEIPT", "ALERT"})

    def _machine_event_class(self, envelope: Envelope) -> Optional[str]:
        """Classify a machine-only envelope; ``None`` means it follows the normal path.

        Delegates to the shared :class:`~swarmly.telegram.poll.MachineEventClassifier`
        so the in-transport tick and the new ``bus-poll --once`` CLI subcommand
        agree on what counts as machine-class traffic (issue #59 deliverable 8).
        """
        from swarmly.telegram.poll import MachineEventClassifier
        return MachineEventClassifier(self).classify(envelope)

    def _handle_machine_event(self, envelope: Envelope, event: ChannelEvent) -> bool:
        """Record machine-class traffic as state instead of dispatching a model turn.

        Fail-open by construction: only the enumerated machine classes are consumed here,
        and only from bot authors — REQUEST, RESPONSE, BLOCKED, unknown envelope types,
        plain prose and any human message all return False and take the normal routing
        path, so nothing actionable is ever swallowed.
        """
        if not event.author_is_bot:
            return False
        if envelope.type in (MessageType.REQUEST, MessageType.RESPONSE, MessageType.BLOCKED):
            return False
        kind = self._machine_event_class(envelope)
        if kind is None:
            return False
        alias = self._alias_for(event)
        self._peer_last_seen[alias] = {"at": self._clock(), "kind": kind,
                                       "ref": envelope.ref, "rev": envelope.rev}
        self._machine_log.append({"at": self._clock(), "kind": kind, "type": envelope.type,
                                  "from": alias, "ref": envelope.ref,
                                  "intent": envelope.intent})
        self.stats.machine_events += 1
        logger.info("machine event recorded without dispatch: kind=%s from=%s ref=%s",
                    kind, alias, envelope.ref)
        if (envelope.type == MessageType.NOTE and (envelope.intent or "").upper() == "CLOSED"
                and envelope.ref and self.replies is not None):
            resolved = self.replies.resolve_prefix(str(envelope.ref), author=event.author_username)
            if resolved:
                logger.info("closed %d ticket(s) via ref=%s from @%s",
                            resolved, envelope.ref, event.author_username)
        return True

    #: Coalesce a burst of doorbell pings into one lane poll.
    LANE_POLL_DELAY_SECONDS = 2.0

    def _on_lane_doorbell(self, envelope: Envelope) -> None:
        """A swarm doorbell (``bus=lanes:<id>``) means new lane mail: poll the lanes now.

        The ping itself never reaches the adapter; the poll delivers the signed envelope
        if, and only if, the projection accepts it.
        """
        if not str(envelope.bus or "").startswith("lanes:") or not self.config.bus.swarm:
            return
        task = self._lane_poll_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        logger.info("doorbell %s: polling the swarm lanes now", envelope.ref)

        async def poll_soon() -> None:
            await self._sleep(self.LANE_POLL_DELAY_SECONDS)
            try:
                await self.check_gitbus()
            except Exception:                          # noqa: BLE001 - the tick retries
                logger.exception("lane poll after a doorbell failed")
        self._lane_poll_task = loop.create_task(poll_soon())

    def peer_last_seen(self) -> Dict[str, Dict[str, Any]]:
        """Last machine-class event per peer alias — the token-economy state surface."""
        return {alias: dict(entry) for alias, entry in self._peer_last_seen.items()}

    def _alias_for_username(self, username: Optional[str]) -> str:
        """Map a Telegram username (or numeric id) back to its configured alias.

        Alert envelopes carry ``to=<Alias>`` per the protocol; ticket rows store the
        username/pseudonym they were registered with, so reverse-map through ``peers``.
        Unknown targets pass through unchanged, ``*`` when there is nothing to map.
        """
        handle = (username or "").lstrip("@").lower()
        for alias, target in self.config.peers.items():
            if str(target).lstrip("@").lower() == handle:
                return alias
        return (username or "").lstrip("@") or "*"

    def _select_digest(self, rows: List[dict]) -> List[dict]:
        """#49.B / #51.N1: when one recipient accumulates >= threshold overdue tickets in a chat, return them."""
        threshold = max(2, int(self.config.limits.overdue_digest_threshold))
        by_recipient: Dict[Tuple[str, Optional[int]], List[dict]] = {}
        for item in rows:
            if item.get("alert_kind") != "task_overdue":
                continue
            key = (item["recipient"] or "", item.get("chat_id"))
            by_recipient.setdefault(key, []).append(item)
        for group in by_recipient.values():
            if len(group) >= threshold:
                return group
        return []

    def _digest_alert(self, group: List[dict]) -> str:
        refs = ", ".join(item["id"][:8] for item in group[:8])
        return format_message(Envelope(
            type=MessageType.ALERT, to=self._alias_for_username(group[0]["recipient"]),
            intent="OVERDUE_DIGEST", ref=str(len(group)), id=self._short_id(),
        ), (
            f"⚠️ @{group[0]['recipient']}: {len(group)} reply tickets overdue in one batch — refs: "
            f"{refs}. Receipts were not completion; maintainer attention required."
        ))

    def _track_outbound_request(self, text: str) -> None:
        """Register an outbound REQUEST with the ACK tracker (spec §5).

        Tracking keys on the envelope's ``id`` — the ref a compliant peer echoes back. A
        REQUEST without an id is not tracked rather than tracked under a value nobody saw.
        """
        envelope, _body = parse_message(text)
        if envelope.type != MessageType.REQUEST:
            return
        if not envelope.id:
            logger.debug("outbound REQUEST without an id is not tracked: %.60r", text)
            return
        self.acks.expect(str(envelope.id), to=envelope.to or "*",
                         ttl_seconds=parse_ttl(envelope.ttl))

    async def check_acks(self) -> List[str]:
        """Announce newly-due escalation rungs (spec §5). Returns the lines posted this pass."""
        if self._frozen:
            return []
        posted: List[str] = []
        for escalation in self.acks.escalations():
            if self._frozen:
                return posted
            if (self._ack_announced.get(escalation.msg_id, 0) >= escalation.rung
                    or self._ack_attempted.get(escalation.msg_id, 0) >= escalation.rung):
                continue
            line = format_message(Envelope(
                type=MessageType.ALERT, to="*", intent="ESCALATION",
                ref=escalation.msg_id, id=self._short_id(),
            ), f"⚠️ {escalation.announcement}")

            def mark_attempted(msg_id=escalation.msg_id, rung=escalation.rung):
                self._ack_attempted[msg_id] = rung

            sent = await self.send(
                line,
                on_attempt=mark_attempted,
            )
            if sent is None:
                return posted
            self._ack_announced[escalation.msg_id] = escalation.rung
            self._ack_attempted.pop(escalation.msg_id, None)
            posted.append(line)
        return posted

    async def _ack_tick(self) -> None:
        """Periodic escalation check; the tracker only posts when a rung is actually due."""
        while True:
            await asyncio.sleep(30)
            try:
                await self.check_acks()
            except Exception:                              # noqa: BLE001 - a tick must never die silently
                logger.exception("ack tick failed")
            try:
                await self.check_stalled_handoffs()
            except Exception:                              # noqa: BLE001 - a tick must never die silently
                logger.exception("hand-off check failed")

    # -- durable-plane fallback --------------------------------------------------------
    def _gitbus_root(self) -> Path:
        candidate = self.config.fallback.gitbus_root
        if candidate:
            return Path(candidate).expanduser()
        from swarmly.telegram.poll import transport_state_root
        return transport_state_root(self.config)   # the same no-bus default as the poller

    def _gitbus_seen_path(self, root: Path) -> Path:
        safe = re.sub(r"[^a-z0-9_-]", "", self.config.identity.alias.strip().lower()) or "agent"
        return root / ".agents" / "state" / f"seen-{safe}.json"

    def _load_gitbus_seen(self, root: Path) -> dict:
        """Load the ledger as an insertion-ordered mapping (``{message_id: None}``)."""
        if self._gitbus_seen is None:
            path = self._gitbus_seen_path(root)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError(f"gitbus: expected dict root in ledger, got {type(data).__name__}")
                self._gitbus_seen = {str(item): None for item in (data.get("seen") or [])}
            except FileNotFoundError:
                self._gitbus_seen = {}                     # first run: no ledger yet
            except Exception:                              # noqa: BLE001 - quarantine, don't storm
                quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
                try:
                    path.rename(quarantine)
                    logger.warning("gitbus: corrupt ledger quarantined at %s", quarantine)
                except Exception:                          # noqa: BLE001
                    logger.warning("gitbus: corrupt ledger present and could not be moved",
                                   exc_info=True)
                self._gitbus_seen = {}
        return self._gitbus_seen

    def bus_transport(self, root: Path) -> Any:
        """The durable-plane transport this agent polls (``bus.transport``; gitbus by default).

        *root* is the local checkout the poller works from: the gitbus repository for
        ``git``, and the home of local state (locks, ledgers, caches) for every transport.
        """
        from swarmly.transports import build_transport
        key = str(root)
        cache = self.__dict__.setdefault("_bus_transports", {})
        if key not in cache:
            bus = self.config.bus
            cache[key] = build_transport(
                bus.transport, repo=Path(root), ref=self.config.fallback.gitbus_ref,
                folder=bus.folder, gdrive=bus.gdrive,
                state_dir=Path(root) / ".agents" / "state",
                identity=tuple(self.config.identity.git_identity))
        return cache[key]

    def _default_bus_pull(self, root: Path) -> None:
        self.bus_transport(root).refresh()

    def _default_bus_read(self, root: Path) -> List[Any]:
        transport = self.bus_transport(root)
        return read_transport_messages(transport, bus_keys(root, transport))

    def _save_gitbus_seen(self, root: Path, seen: dict) -> None:
        """Persist the ledger bounded to the most recent `_GITBUS_SEEN_MAX` ids.

        Insertion order (oldest first) is preserved as the file order, so a reload keeps the
        newest ids and drops the oldest — an unbounded ledger grows forever, and truncating a
        set re-delivered exactly the dropped ids (round-1 review).
        """
        try:
            path = self._gitbus_seen_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            cap = max(1, _GITBUS_SEEN_MAX)
            items = list(seen)[-cap:]
            path.write_text(json.dumps({"seen": items}, indent=0), encoding="utf-8")
        except Exception:                                  # noqa: BLE001
            logger.warning("gitbus: could not persist the seen ledger", exc_info=True)

    @staticmethod
    def _gitbus_text(message: Any) -> str:
        payload = getattr(message, "payload", None) or {}
        if isinstance(payload, dict):
            for key in ("text", "content", "message"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(payload, ensure_ascii=False) if payload else ""
        return str(payload).strip()

    async def check_gitbus(self) -> List[str]:
        """Deliver messages addressed to this agent from the durable git bus (fallback path).

        Active when ``fallback.gitbus_poll_minutes`` is non-zero. Every unseen, specifically
        addressed message is delivered to the adapter first — pacing never throttles inbound
        delivery — then Telegram echoes (replies + one receipt) drain afterwards. The ledger
        (``.agents/state/seen-<alias>.json``) is persisted bounded to the most recent ids so
        restarts never re-deliver what is still on the bus; a corrupt ledger is quarantined
        instead of silently reset. Broadcast
        (``*``) messages and this agent's own posts are skipped in v1, and a poll that is
        still running makes the next tick a no-op rather than stacking.

        Issue #59: this method is now a thin wrapper over the shared
        :func:`swarmly.telegram.poll.bus_poll_once` core so the in-transport tick and the new
        ``bus-poll --once`` CLI subcommand use the same load -> inject -> ledger logic.
        Behaviour of the transport path is preserved: same skip-on-frozen, same skip-when-
        adapter-disabled, same skip-when-poll-minutes-zero, same skip-when-prev-poll-still-
        draining, and the return value is still the list of delivered message ids.
        """
        if self._gitbus_busy:
            logger.info("gitbus: previous poll still draining; skipping this tick")
            return []
        if self._frozen or self._pause_pending or self.config.fallback.gitbus_poll_minutes <= 0 \
                or not self.config.adapter.enabled \
                or not self.config.bus.enabled:
            return []
        self._gitbus_busy = True
        try:
            # Shared core (issue #59 deliverable 2): same load -> inject -> ledger
            # logic the ``bus-poll --once`` CLI subcommand uses. ``transport_compat``
            # keeps the legacy rules (broadcasts and own posts skipped, no machine-
            # class delivery) so existing transport tests stay green.
            outcome = await bus_poll_once(self, transport_compat=True)
            delivered: List[str] = list(outcome.delivered)
            # Review B2: escalations must surface on the transport path too —
            # outcome was ignored here entirely, so a BLOCKED envelope (or the
            # lack of a bus write) was invisible to the operator's logs.
            if outcome.escalations:
                logger.warning(
                    "gitbus: %d delivery escalation(s) this cycle: %s",
                    len(outcome.escalations),
                    ", ".join(str(e.get("envelope_id")) for e in outcome.escalations))
            # A delivery whose reply did not show in the chat is logged, not posted: General
            # is for setup, and a receipt there is noise (live test, build 9).
            if delivered and outcome.replies_posted < len(delivered):
                logger.info("gitbus fallback delivered %d message(s), %d reply(ies) posted",
                            len(delivered), outcome.replies_posted)
            return delivered
        finally:
            self._gitbus_busy = False

    async def _bus_tick(self) -> None:
        """Periodic durable-bus poll; interval from ``fallback.gitbus_poll_minutes``."""
        interval = max(60.0, float(self.config.fallback.gitbus_poll_minutes) * 60.0)
        logger.info("gitbus: polling %s every %d min", self.config.bus.repo,
                    self.config.fallback.gitbus_poll_minutes)
        while True:
            # Poll first, then wait: the first cycle sets the seen-ledger baseline, and a
            # baseline taken minutes after start would swallow mail sent in between.
            if self.config.fallback.gitbus_poll_minutes > 0 and self.config.bus.enabled:
                try:
                    await self.check_gitbus()
                except Exception:                          # noqa: BLE001 - a tick must never die silently
                    logger.exception("gitbus tick failed")
            await self._sleep(interval)

    async def _github_tick(self) -> None:
        """Periodic GitHub notifications backstop poll."""
        interval = max(60.0, float(self.config.github.poll_minutes) * 60.0) if self.config.github else 300.0
        while True:
            await self._sleep(interval)
            if not self.config.github or self.config.github.poll_minutes <= 0:
                continue
            try:
                await self.check_github()
            except Exception:
                logger.exception("github backstop tick failed")

    async def check_github(self) -> Any:
        from swarmly.github.watch import github_poll_once, _utc
        outcome = await github_poll_once(self.config, channel=self)
        self._github_last_poll = _utc()
        if outcome.errors:
            for err in outcome.errors:
                logger.warning("github backstop: %s", err)
            # Say so in the group, at most hourly: a backstop that silently stopped
            # working is worse than none.
            now = self._clock()
            last = self._github_alert_at
            if last is None or now - last >= 3600:
                self._github_alert_at = now
                try:
                    await self.send(f"⚠️ {self.config.identity.alias}: the GitHub backstop "
                                    f"cannot poll: {outcome.errors[0][:300]} (check `gh auth "
                                    "status` and github.token_env on this host)", silent=True)
                except Exception:                              # noqa: BLE001 - best effort
                    logger.exception("could not post the github backstop alert")
        return outcome

    # ----------------------------------------------------------------------------------
    # Inbound handling
    # ----------------------------------------------------------------------------------
    async def handle_event(self, event: ChannelEvent) -> Optional[str]:
        """
        Route one inbound message. Returns the reply text that was posted (or None).
        Pure decision + adapter call + send; safe to unit test with a fake sender.
        """
        in_home_channel = (self.config.identity.home_channel_id is None
                           or event.chat_id == self.config.identity.home_channel_id)
        is_admin_dm = self._is_admin_dm(event, in_home_channel)
        if not (in_home_channel or is_admin_dm):
            self.stats.ignored += 1
            return None
        # Issue #22: a threaded topic that cannot get a context id is refused here, before a
        # turn can reach the adapter with context_id=None, which is the shared General
        # session. The GitHub backstop is gated the same way.
        from swarmly.telegram.topics import context_block_reason
        blocked = context_block_reason(event.chat_id, event.thread_id,
                                       self.config.identity.home_channel_id)
        if blocked:
            self.stats.ignored += 1
            logger.error("refusing a turn in chat=%s topic=%s: %s",
                         event.chat_id, event.thread_id, blocked)
            return None

        # Every message that names its topic teaches the name, addressed to us or not.
        if event.thread_id is not None:
            self.topics.remember(event.chat_id, event.thread_id, event.topic_name,
                                 current=event.topic_name_current)
        self.record_topic_turn(event.chat_id, event.thread_id, event.text or "")
        self._note_progress(event)
        if in_home_channel and event.text and self._anonymous_admin_post(event) \
                and getattr(self.config.limits, "strangers", "ignore") == "ignore":
            await self._anonymous_admin_notice(event)
        if not event.author_is_bot and parse_command(event.text or "") is None \
                and self._may_start_turns(event):
            key = (event.chat_id, event.thread_id)
            if key in self._peer_turns or key in self._paused_topics:
                self._peer_turns.pop(key, None)                            # a human resets
                self._paused_topics.discard(key)
                self._save_turn_state()

        if event.author_is_bot and event.thread_id is not None \
                and "paused after" in machine_prose(event.text or "")[:40]:
            # A peer already said this topic is paused: one notice per pause, not one per bot,
            # and no "still paused" reminder right behind it (live run: three notices in 14 s).
            self._paused_topics.add((event.chat_id, event.thread_id))
            self._pause_reminded[(event.chat_id, event.thread_id)] = self._clock()
            self._save_turn_state()
        if event.author_is_bot and event.thread_id is not None \
                and "still paused here" in machine_prose(event.text or "")[:40]:
            # The same for the reminder: one per topic per window, whichever bot posts it.
            self._pause_reminded[(event.chat_id, event.thread_id)] = self._clock()
        if event.thread_id is not None:
            self._learn_topic_refs(event.chat_id, event.thread_id, event.text or "")
        if event.author_is_bot:
            self._handoff_answered(event)
        if self.config.is_member(event.author_id, event.author_username, event.author_is_bot) \
                and not event.self_task and not self._presence_traffic(event):
            # Only the swarm's words go into a recap: a stranger's text never reaches a prompt.
            self._log_topic(event.chat_id, event.thread_id, self._alias_for(event),
                            event.text or "", event.message_id)

        logger.info("inbound from @%s (id=%s, bot=%s): %r",
                    event.author_username or "unknown", event.author_id,
                    event.author_is_bot, (event.text[:120] if event.text else "<no text>"))

        envelope, _body = parse_message(event.text or "")
        if in_home_channel:
            self.selftest.observe(envelope, event)
            if await self._handle_roster(envelope, event):
                self.stats.handled += 1
                return None
        near_miss = ignored_envelope_marker(event.text or "")
        if near_miss is not None:
            logger.warning("near-miss A2A envelope from @%s - ignored: %s",
                           event.author_username or "unknown", near_miss)

        control_reply = await self._handle_freeze_status(envelope, event)
        if control_reply is not None:
            self.stats.handled += 1
            return await self.send(control_reply, reply_to_message_id=event.message_id,
                                   chat_id=event.chat_id, thread_id=event.thread_id,
                                   allow_when_frozen=True)

        # §5: a machine ACK addressed to us closes an outstanding request and never reaches the
        # adapter — handled before the routing gate, whose terminal-ack filter would drop it.
        if self._handle_inbound_ack(envelope, event):
            self.stats.ignored += 1
            return None

        # Token economy (issue #43): machine-class traffic (receipts, alerts, liveness,
        # FYI/closed notes) is recorded as state — never dispatched, never ticketed.
        if self._handle_machine_event(envelope, event):
            self.stats.handled += 1
            self._on_lane_doorbell(envelope)
            return None

        # An explicit non-REQUEST envelope is informational or a response to an earlier
        # request.  It may mention this bot so Telegram delivers it, but it must not create a
        # durable task-reply obligation.  Bare prose remains backward-compatible and is
        # handled normally because it may contain an implicit question or action.
        no_reply_types = {
            MessageType.HELLO, MessageType.RESPONSE, MessageType.STATUS,
            MessageType.BLOCKED, MessageType.NOTE, MessageType.BYE,
        }
        if has_explicit_envelope(event.text) and envelope.type in no_reply_types:
            logger.info("routing decision: respond=False (reason: explicit %s is informational)",
                        envelope.type)
            self.stats.ignored += 1
            return None

        # /pause and /resume hold and release every bot (or one, named) for the admin.
        pause_reply = self._handle_pause(event)
        if pause_reply == "":
            return None
        if pause_reply is not None:
            self.stats.handled += 1
            return await self.send(pause_reply, reply_to_message_id=event.message_id,
                                   chat_id=event.chat_id, thread_id=event.thread_id,
                                   allow_when_frozen=True)

        if self._frozen or self._pause_pending:
            command_reply = (self._handle_command(event)
                             if self._may_start_turns(event) else None)
            if command_reply is not None:
                self.stats.handled += 1
                return await self.send(command_reply, reply_to_message_id=event.message_id,
                                       chat_id=event.chat_id, thread_id=event.thread_id,
                                   allow_when_frozen=True)
            logger.info("routing decision: respond=False (reason: transport frozen)")
            self.stats.ignored += 1
            return None

        decision, reason = self.should_respond(event)
        logger.info("routing decision: respond=%s (reason: %s)", decision, reason)
        if decision and event.message_id is not None:
            self._tg_admitted.append((int(event.chat_id), int(event.message_id)))
            save_state(self._admitted_path, {"ids": [list(p) for p in self._tg_admitted]},
                       "handled Telegram messages")
        if not decision:
            self.stats.ignored += 1
            if event.cooldown_left > 0:
                await self._cooldown_notice(event)     # a notice, not a turn: still None
            else:
                # #27: a peer bot that named this bot and got no turn hears which handle was
                # dropped and why, so a lost hand-off never reads as one that was taken.
                await self._mention_drop_notice(event, reason)
            return None
        topic_key = (event.chat_id, event.thread_id)
        generation = (self._stop_generation, self._stop_topic_generation.get(topic_key, 0))

        # /stop cancels the running turn and clears the queue; it never waits for the turn.
        stop_reply = self._handle_stop(event)
        if stop_reply == "":
            return None                        # a peer's /stop: consumed, never a turn
        if stop_reply is not None:
            self.stats.handled += 1
            return await self.send(stop_reply, reply_to_message_id=event.message_id,
                                   chat_id=event.chat_id, thread_id=event.thread_id,
                                   allow_when_frozen=True)

        # §6: /ping /status /diag are transport-level liveness — answered without a runtime turn.
        command_reply = self._handle_command(event)
        if command_reply is not None:
            self.stats.handled += 1
            return await self.send(command_reply, reply_to_message_id=event.message_id,
                                   chat_id=event.chat_id, thread_id=event.thread_id,
                                   allow_when_frozen=True)

        # §5, Rule 5: an addressed REQUEST from a peer is acknowledged going in.
        if self.replies is None:
            await self._ack_inbound_request(envelope, event)

        # The loop brake comes after commands and the ACK: a capped topic still answers
        # /ping and acknowledges a REQUEST (so no sender escalates), it only takes no turn.
        if self._peer_turns_capped(event):
            return await self._paused(event)

        lock = self._lock_for(event)
        busy = lock.locked() or (self._turn_slots is not None and self._turn_slots.locked())
        queued = False
        queue_seq = 0
        busy_key = topic_key
        if busy and self.replies is None and self._may_queue(busy_key):
            # Ported from ductor's per-chat queue: hold the message and run it as soon as the
            # turn in flight ends, instead of losing it. asyncio.Lock wakes waiters in order.
            queued = True
            self._busy_waiting += 1
            self._busy_waiting_by[busy_key] = self._busy_waiting_by.get(busy_key, 0) + 1
            self.stats.busy_queued += 1
            logger.info("turn in progress for %s; queued message from @%s (%d waiting)",
                        self.config.identity.alias, event.author_username, self._busy_waiting)
            queue_seq = self._queue_seq[busy_key] = self._queue_seq.get(busy_key, 0) + 1
            await self._busy_notice(event, busy_key)
        elif busy and self.replies is None:
            # #9: with busy_behavior drop/notice, or a full queue, an addressed message that
            # arrives mid-turn is dropped; the loss is counted, and when limits.busy_behavior == "notice" it is
            # reported once per window so a 60% drop rate stops reading as a healthy transport.
            # #93(2): resolve the window *first*, and without any call that could raise. The
            # counters below must not move before the notice path is known to be usable — a drop
            # that half-registers (counted, then the handler raises instead of returning) is worse
            # than either outcome alone. The value is validated at config load, so this read is a
            # plain attribute access with a default for hosts built before the field existed.
            window = 900.0
            busy_behavior = getattr(self.config.limits, "busy_behavior", "queue")
            if busy_behavior == "notice":
                window = getattr(self.config.limits, "busy_notice_window_seconds", 900.0)
                if not isinstance(window, (int, float)) or isinstance(window, bool):
                    logger.warning(
                        "busy notice window %r is not a number; using the 900s default", window)
                    window = 900.0
                elif not (0 < window < float("inf")):
                    # Same rule as the config layer: nan, 0, negatives and inf all leave the
                    # feature unusable (nan/inf make the comparison false forever), so fall back
                    # to the declared default rather than silently suppressing every notice.
                    logger.warning(
                        "busy notice window %r is not a usable positive number; using the 900s "
                        "default", window)
                    window = 900.0
            self.stats.ignored += 1
            self.stats.busy_dropped += 1
            self._busy_drops_since_notice += 1
            if busy_behavior == "notice":
                now = self._clock()
                if now - self._busy_notice_at >= window:
                    self._busy_notice_at = now
                    dropped = self._busy_drops_since_notice
                    self._busy_drops_since_notice = 0
                    # One marker-led line on the module logger: no await, no network, no newline.
                    logger.warning(
                        "⚠️ [%s] busy: %d addressed msg(s) from @%s dropped (turn in progress)",
                        self.config.identity.alias, dropped, event.author_username)
            elif busy_behavior == "queue":
                logger.warning("busy queue for %s is full (%d waiting); dropped message from @%s",
                               self.config.identity.alias,
                               self._busy_waiting if self._turn_slots is None
                               else self._busy_waiting_by.get(busy_key, 0),
                               event.author_username)
                # Say so in the chat, once per window: the drop used to be silent (live test).
                now = self._clock()
                if now - self._queue_full_noticed >= 300:
                    self._queue_full_noticed = now
                    await self.send(
                        f"⚠️ {self.config.identity.alias} is busy and its queue is full; it "
                        f"dropped this message from {event.author_username or 'someone'}. "
                        "Send it again once it has answered.",
                        reply_to_message_id=event.message_id, chat_id=event.chat_id,
                        thread_id=event.thread_id, silent=True)
            else:
                logger.warning("turn already in progress for %s; ignoring concurrent event from @%s",
                               self.config.identity.alias, event.author_username)
            return None

        author_alias = self._alias_for(event)
        author_username = event.author_username
        peer_username = self.config.peer_username_for(author_alias)
        # Persist before waiting: aiogram may advance its update offset while this handler
        # waits behind another turn or a paced receipt. A restart must not erase that work.
        project_settings = self.router.project_for(event.chat_id, event.thread_id)
        ticket = None
        instructions = ""
        workdir = (self.router.workdir_for(project_settings, event.thread_id)
                   if project_settings and event.thread_id is not None else None)
        if self.replies is not None:
            from swarmly.telegram.replies import reply_instructions
            ticket, created = self.replies.register(
                chat_id=event.chat_id, message_id=event.message_id,
                recipient=author_username or peer_username, text=event.text,
                timeout=self.config.adapter.reply_timeout_seconds,
            )
            if not created:
                self.stats.ignored += 1
                return None
            instructions = reply_instructions(self.config, ticket, workdir=workdir)
        text = await self._with_attachments(event)
        recap = self._topic_recap(event)
        text = recap + text
        if not event.author_is_bot and not is_admin_dm:     # peers can't see a DM
            text += self._waiting_note(event.text)
        if not event.self_task and asks_for_topic(event.text or ""):
            text += f"\n\n{TOPIC_ASKED_NOTE}"
        incoming = IncomingMessage(
            # Keep every mention and paragraph, especially in multi-recipient messages.
            text=text, author_alias=author_alias, author_id=event.author_id,
            author_username=author_username, peer_username=peer_username,
            is_admin=self.config.is_admin(event.author_id), message_id=event.message_id,
            raw=event.raw, response_instructions=instructions,
            mentions=dict(self.config.peers),
            maintainer_username=self.config.identity.maintainer_username,
            context_id=self.topics.context(event.chat_id, event.thread_id),
            topic_name=self.topics.name(event.chat_id, event.thread_id),
            model=self._models.get(self._model_key(event)),
            effort=self._efforts.get(self._model_key(event)),
            project=project_settings.key if project_settings else None,
            project_repo=project_settings.repo if project_settings else None,
            general_is_for_setup=self._work_moves_to_topics(event),
            role_brief=self._role_brief(),
            fresh_text=self._fresh_text(event, text[len(recap):]),
        )

        try:
            await self._take_turn(lock)
        finally:
            if queued:
                self._busy_waiting -= 1
                left = self._busy_waiting_by.get(busy_key, 1) - 1
                if left > 0:
                    self._busy_waiting_by[busy_key] = left
                else:
                    self._busy_waiting_by.pop(busy_key, None)

        def stopped() -> bool:
            if generation == (self._stop_generation,
                              self._stop_topic_generation.get(topic_key, 0)):
                return False
            logger.info("dropping a message held before /stop (from @%s)",
                        event.author_username)
            self.stats.ignored += 1
            return True

        held, gate = True, None
        try:
            if stopped():
                return None
            if self._frozen or self._pause_pending:
                logger.info("dropping a message held before /pause (from @%s)",
                            event.author_username)
                self.stats.ignored += 1
                return None
            if queued and event.thread_id is not None \
                    and self._queue_seq.get(busy_key, 0) > queue_seq:
                # Newer messages for this topic wait behind this one: hand it to the last of
                # them, so a pile-up behind a long turn gets one answer (live run: five
                # reminders and nudges became five turns once a 15-minute turn ended).
                self._folded.setdefault(busy_key, []).append(incoming.text or "")
                logger.info("folded a queued message from @%s into the next queued turn",
                            event.author_username)
                return None
            incoming = self._with_folded(incoming, busy_key)
            # A queued peer message still inside the bot-to-bot cooldown waits it out once,
            # without holding the turn: other messages and topics go on meanwhile. It then
            # runs even if turns ran in between, so a busy chat cannot starve it.
            if self._cooldown_left(event) > 0:
                self._end_turn(lock)
                held = False
                gate = await self._hold_for_cooldown(event, topic_key, lock, stopped)
                if gate is None:
                    return None
                held = True
            if self._peer_turns_capped(event):
                return await self._paused(event)
            incoming = await self._with_git_state(
                incoming, str(workdir) if workdir is not None else None)
            return await self._run_turn(event, envelope, incoming, ticket)
        finally:
            if held:
                self._end_turn(lock)
            if gate is not None:                    # the next held message may start its wait
                gate.release()
                if not self._cooldown_waiting.get(topic_key) and \
                        self._hold_gates.get(topic_key) is gate:
                    self._hold_gates.pop(topic_key, None)

    async def _with_git_state(self, incoming: IncomingMessage,
                              workdir: Optional[str]) -> IncomingMessage:
        """With adapter.git_sync, fetch and fast-forward the turn's checkout as the real user,
        outside the agent's sandbox, and add where it stands to the prompt (git_sync.py)."""
        adapter = self.config.adapter
        if not adapter.syncs_git:
            return incoming
        path = workdir or adapter.cwd or "."
        from swarmly.telegram.git_sync import sync_checkout
        try:
            line = await asyncio.to_thread(sync_checkout, path)
        except Exception:                                  # noqa: BLE001 - the turn goes on
            logger.warning("git sync in %s failed", path, exc_info=True)
            return incoming
        if not line:
            return incoming
        return dataclasses.replace(incoming, text=f"{incoming.text}\n\n{line}")

    async def _take_turn(self, lock: asyncio.Lock) -> None:
        """Take the turn lock, then a parallel-topic slot."""
        await lock.acquire()
        if self._turn_slots is not None:
            try:
                await self._turn_slots.acquire()
            except BaseException:
                lock.release()
                raise

    def _end_turn(self, lock: asyncio.Lock) -> None:
        if self._turn_slots is not None:
            self._turn_slots.release()
        lock.release()
        if self._pause_pending:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self._finish_pause()))

    def _turn_running(self) -> bool:
        return self._turn_lock.locked() or any(
            lock.locked() for lock in self._turn_locks.values())

    async def _finish_pause(self) -> None:
        """The last turn running at /pause has ended (and posted): hold now."""
        if not self._pause_pending or self._turn_running():
            return
        self._pause_pending = False
        self._set_frozen(True, "pause")
        chat_id, thread_id = self._pause_where
        await self.send(f"⏸ {self.config.identity.alias} paused. /resume to continue.",
                        chat_id=chat_id, thread_id=thread_id, allow_when_frozen=True,
                        silent=True)

    def _handle_pause(self, event: ChannelEvent) -> Optional[str]:
        """``/pause`` and ``/resume`` from the admin: a bare one reaches every bot, and
        ``/pause@bot`` one. Turns already running finish and post first; then the bot holds
        (the freeze persists across restarts) until ``/resume``. Anyone else's bare command
        is ignored silently, so four bots don't answer it four times."""
        parsed = parse_command(event.text or "")
        if not parsed or parsed[0] not in ("pause", "resume"):
            return None
        command, target = parsed
        named = bool(target) or self.mentions_me(event.text or "")
        if target and target.lstrip("@").lower() != self.config.identity.name.lstrip(
                "@").lower():
            return ""
        if not self.config.is_admin(event.author_id):
            return "⛔ only the admin can pause or resume the bots" \
                if named and not event.author_is_bot else ""
        alias = self.config.identity.alias
        if command == "resume":
            was = self._frozen or self._pause_pending
            self._pause_pending = False
            self._set_frozen(False, None)
            return f"▶ {alias} resumed." if was else f"▶ {alias} was not paused."
        if self._frozen:
            return f"⏸ {alias} is already paused. /resume to continue."
        if not self._turn_running():
            self._set_frozen(True, "pause")
            return f"⏸ {alias} paused. /resume to continue."
        self._pause_pending = True
        self._pause_where = (event.chat_id, event.thread_id)
        return (f"⏸ {alias} pausing: it finishes the turn it is running, then holds. "
                "/resume to continue.")

    async def _hold_for_cooldown(self, event: ChannelEvent, key: Any, lock: asyncio.Lock,
                                 stopped: Callable[[], bool]) -> Optional[asyncio.Lock]:
        """Sleep out the cooldown outside the turn lock, then take the turn back (the gate is
        returned, with the lock held) or drop the message (None). At most
        ``limits.busy_queue_max`` messages wait per topic; one more is dropped. They take the
        turn back one at a time, oldest first: they share one deadline, and the event loop
        wakes equal deadlines in no particular order. Each waits once, so a busy chat cannot
        starve it. The caller keeps the gate until its turn ends, so the next one measures its
        wait from that turn: held peer turns stay a cooldown apart."""
        waiting = self._cooldown_waiting.get(key, 0)
        if waiting >= getattr(self.config.limits, "busy_queue_max", 5):
            logger.warning("cooldown queue for %s is full (%d waiting); dropped message from @%s",
                           self.config.identity.alias, waiting, event.author_username)
            self.stats.ignored += 1
            self.stats.busy_dropped += 1
            return None
        self._cooldown_waiting[key] = waiting + 1
        gate = self._hold_gates.get(key)
        if gate is None:
            gate = self._hold_gates[key] = asyncio.Lock()
        kept = None
        try:
            await gate.acquire()
            try:
                await self._wait_out_cooldown(event)
                if stopped():
                    return None
                await self._take_turn(lock)
                if stopped():                       # a /stop while it waited for the turn
                    self._end_turn(lock)
                    return None
                kept = gate                         # kept until this message's turn ends
                return gate
            finally:
                if kept is None:
                    gate.release()
        finally:
            left = self._cooldown_waiting.get(key, 1) - 1
            if left > 0:
                self._cooldown_waiting[key] = left
            else:
                self._cooldown_waiting.pop(key, None)
                if not gate.locked():               # no one holds or awaits it
                    self._hold_gates.pop(key, None)

    #: A message queued behind a turn this old gets a ⏳ line, so peers see busy, not gone.
    BUSY_NOTICE_SECONDS = 120.0
    #: At most one ⏳ line per topic per this many seconds.
    BUSY_NOTICE_REPEAT_SECONDS = 300.0

    async def _busy_notice(self, event: ChannelEvent, key: Any) -> None:
        """Say once in a while that a long turn is running: to the swarm a busy bot used to
        look exactly like a dead one, and peers reminded and escalated (live run). A peer's
        hand-off timer restarts on this line instead of counting the silence."""
        if not self._busy_since:
            return
        now = self._clock()
        started = min(self._busy_since.values())
        if now - started < self.BUSY_NOTICE_SECONDS or \
                now - self._busy_noticed.get(key, float("-inf")) < self.BUSY_NOTICE_REPEAT_SECONDS:
            return
        self._busy_noticed[key] = now
        since = time.strftime("%H:%M", time.gmtime(time.time() - (now - started)))
        waiting = self._busy_waiting_by.get(key, 0)
        await self.send(f"⏳ {self.config.identity.alias} is busy with a turn running since "
                        f"{since} UTC; {waiting} message(s) here wait for it and get one "
                        "answer when it ends.", chat_id=event.chat_id,
                        thread_id=event.thread_id, silent=True)

    def _with_folded(self, incoming: IncomingMessage, key: Any) -> IncomingMessage:
        """The queued messages folded into this turn go first, so one answer covers them."""
        folded = self._folded.pop(key, None)
        if not folded:
            return incoming
        lead = ("[Messages that arrived while your last turn ran; answer them together with "
                "the newest one below.]\n\n" + "\n\n---\n\n".join(folded) + "\n\n---\n\n")
        return dataclasses.replace(
            incoming, text=lead + (incoming.text or ""),
            fresh_text=lead + incoming.fresh_text if incoming.fresh_text else None)

    def _last_turn_for(self, event: ChannelEvent) -> float:
        """When the last turn in this discussion ended. A turn in a topic does not hold off
        peer messages in General, even when topics share one turn slot (live test: a topic
        turn set off General's cooldown)."""
        return self._last_turn_at.get((event.chat_id, event.thread_id), float("-inf"))

    def _note_progress(self, event: ChannelEvent) -> None:
        """Visible progress resets a topic's run of bot turns: a swarm message naming an
        issue, PR or commit (a link or a short SHA in the text) not named there before, or a merge,
        approval, QA verdict or change request or approval of one. The cap
        is a loop brake, and a loop repeats itself; four bots shipping PRs in a tracking
        topic used to pause it every eight turns."""
        if event.thread_id is None or not event.author_is_bot or not self.config.is_member(
                event.author_id, event.author_username, event.author_is_bot):
            return
        key = (event.chat_id, event.thread_id)
        if key in self._paused_topics:
            return                                   # a person lifts a pause, not a bot
        text = event.text or ""
        refs = issue_refs(text)
        shas = set(_COMMIT_LINK.findall(text)) | {
            sha for sha in _BARE_SHA.findall(text)
            if re.search(r"\d", sha) and re.search(r"[a-f]", sha)}
        marks = set(refs) | {f"commit {sha[:7]}" for sha in shas}
        targets = set(refs) | {sha[:7] for sha in shas}
        for verb in _PROGRESS_VERBS.findall(text):
            # A verdict on a PR or commit it was not given for before: QA and review rounds
            # are progress too (live run: a fix, QA and review cycle paused at 8 turns).
            verb = re.sub(r"\s+", " ", verb.lower()).replace("passed", "pass").replace(
                "failed", "fail")
            marks |= {f"{verb} {target}" for target in targets}
        seen = self._progress_marks.setdefault(key, set())
        if marks - seen:
            seen |= marks
            if self._peer_turns.pop(key, None):
                self._save_turn_state()

    def _peer_turns_capped(self, event: ChannelEvent) -> bool:
        """A forum topic takes at most ``limits.max_peer_turns_per_topic`` peer-started turns
        in a row; a human posting there resets it. This is the loop brake: it holds even for
        adapters whose explicit mentions skip the cooldown."""
        cap = int(getattr(self.config.limits, "max_peer_turns_per_topic", 0) or 0)
        if cap <= 0 or not event.author_is_bot or event.thread_id is None:   # General: as before
            return False
        return self._peer_turns.get((event.chat_id, event.thread_id), 0) >= cap

    async def _paused(self, event: ChannelEvent) -> None:
        """Refuse a peer turn in a capped topic; say so there once, until a human posts."""
        key = (event.chat_id, event.thread_id)
        cap = self.config.limits.max_peer_turns_per_topic
        logger.info("routing decision: respond=False (reason: %d peer turns in a row in topic "
                    "%s; a human message there resets it)", cap, event.thread_id)
        self.stats.ignored += 1
        if key in self._paused_topics:
            # A request that names this bot in a paused topic is not dropped without a word
            # (live test: a sender never learned why); at most one reminder per 10 minutes.
            now = self._clock()
            if self.mentions_me(event.text or "") and \
                    now - self._pause_reminded.get(key, float("-inf")) >= 600:
                self._pause_reminded[key] = now
                await self.send("⚠️ still paused here: a person needs to post in this topic "
                                "before bots take more turns.", chat_id=event.chat_id,
                                thread_id=event.thread_id,
                                reply_to_message_id=event.message_id, silent=True)
            return None
        self._paused_topics.add(key)
        self._pause_reminded[key] = self._clock()     # the notice is this window's reminder
        self._save_turn_state()
        # send() adds the [alias] banner
        await self.send(f"⚠️ paused after {cap} bot turns in a row in this topic. "
                        "Post here to continue.",
                        chat_id=event.chat_id, thread_id=event.thread_id)
        return None

    def _lock_for(self, event: ChannelEvent) -> asyncio.Lock:
        """The lock this message's turn runs under: its topic's, or the shared one."""
        return self._lock_for_key(event.chat_id, event.thread_id)

    @contextlib.asynccontextmanager
    async def admitted(self, chat_id: Optional[int] = None, thread_id: Optional[int] = None):
        """Run one adapter turn that did not come from a Telegram message (a bus delivery,
        a swarm lane item) under the same admission as chat turns: the lock of its topic
        (the General topic of the home chat by default) and a parallel-turn slot. The
        channel is the only place turns are admitted, so the poller can never push the
        adapter past ``limits.max_parallel_turns``."""
        if chat_id is None:
            chat_id = int(self.config.identity.home_channel_id or 0)
        lock = self._lock_for_key(chat_id, thread_id)
        async with lock:
            if self._turn_slots is None:
                yield
            else:
                async with self._turn_slots:
                    yield

    def _lock_for_key(self, chat_id: int, thread_id: Optional[int]) -> asyncio.Lock:
        if self._turn_slots is None:
            return self._turn_lock
        key = (chat_id, thread_id)
        lock = self._turn_locks.get(key)
        if lock is None:
            lock = self._turn_locks[key] = asyncio.Lock()
        return lock

    def _command_for_me(self, event: ChannelEvent) -> Optional[str]:
        """The ``/command`` in *event* when it targets this bot, else None."""
        parsed = parse_command(event.text or "")
        if not parsed:
            return None
        command, target = parsed
        if target:
            if target.lstrip("@").lower() != self.config.identity.name.lstrip("@").lower():
                return None
        elif not (self.mentions_me(event.text or "") or event.replies_to_bot):
            return None
        return command

    def _handle_stop(self, event: ChannelEvent) -> Optional[str]:
        """``/stop``: cancel the turn in flight and drop the messages queued behind it.

        In a forum topic it stops that topic's turn and queue only; in the General topic,
        or as ``/stop all``, it stops everything. Ported from ductor's abort handling. Only
        the admin may stop a turn; anyone else gets one line saying so. A peer bot's /stop
        returns "": consumed silently, so it neither stops the turn nor reaches the adapter.
        """
        if self._command_for_me(event) != "stop":
            return None
        if not self.config.is_admin(event.author_id):
            if event.author_is_bot:
                logger.warning("ignored /stop from peer bot @%s", event.author_username)
                self.stats.ignored += 1
                return ""
            return "⛔ only the admin can stop a turn"
        rest = [word for word in re.sub(r"^\s*/\w+(?:@\w+)?", "", event.text or "").split()
                if not word.startswith("@")]
        everything = event.thread_id is None or [w.lower() for w in rest[:1]] == ["all"]
        key = (event.chat_id, event.thread_id)
        if everything:
            cleared = self._busy_waiting + sum(self._cooldown_waiting.values())
            self._stop_generation += 1
            running = [turn for turn in self._turns if not turn.done()]
        else:
            cleared = self._busy_waiting_by.get(key, 0) + self._cooldown_waiting.get(key, 0)
            self._stop_topic_generation[key] = self._stop_topic_generation.get(key, 0) + 1
            running = [turn for turn in self._turns
                       if not turn.done() and self._turn_keys.get(turn) == key]
        if not running:
            done = "nothing was running" + ("" if everything else " in this topic")
        else:
            for turn in running:
                self._stopping_turns.add(turn)
                turn.cancel()
            self.stats.stopped += len(running)
            done = ("stopped the running turn" if len(running) == 1
                    else f"stopped {len(running)} running turns")
        if cleared:
            done += f" and cleared {cleared} queued message(s)"
        others = 0 if everything else sum(
            1 for turn in self._turns if not turn.done() and turn not in running)
        if others:
            done += f" ({others} in other topics still running; /stop all stops them)"
        logger.info("/stop from the admin: %s", done)
        return f"⏹ {done}"

    async def _respond(self, incoming: IncomingMessage,
                       event: ChannelEvent) -> Optional[AdapterReply]:
        """Run the adapter as a cancellable task, with a typing indicator while it works."""
        adapter = self._adapter_for(event.chat_id, event.thread_id)
        turn = asyncio.ensure_future(adapter.respond(incoming))
        self._current_turn = turn
        self._turns.add(turn)
        self._turn_keys[turn] = (event.chat_id, event.thread_id)
        typing = self._start_typing(event)
        if not event.author_is_bot and event.message_id is not None:
            asyncio.ensure_future(self._ack_reaction(event))
        busy_topic = (event.chat_id, event.thread_id)
        self._busy_since[busy_topic] = self._clock()
        started = time.monotonic()
        logger.info("turn start: message %s from %s in chat %s%s (%d chars) -> %s",
                    event.message_id, incoming.author_alias, event.chat_id,
                    f" topic {event.thread_id} ({incoming.context_id})"
                    if event.thread_id is not None else "",
                    len(incoming.text or ""), type(adapter).__name__)
        logger.debug("turn prompt (message %s):\n%s", event.message_id,
                     (incoming.text or "")[:2000])
        try:
            reply = await turn
            for delay in self._transient_retry_delays:
                if reply is None or reply.ok or not _transient_failure(reply.error or ""):
                    break
                # A provider hiccup ("model is at capacity", 503, overloaded) is not the
                # agent's failure: try again after a pause before posting ❌. The retry is a
                # registered turn, so /stop still cancels it while it waits.
                logger.warning("turn: message %s hit a transient provider error (%s); "
                               "retrying in %ss", event.message_id, reply.error, delay)
                self._turns.discard(turn)
                self._turn_keys.pop(turn, None)
                turn = asyncio.ensure_future(self._respond_later(adapter, incoming, delay))
                self._current_turn = turn
                self._turns.add(turn)
                self._turn_keys[turn] = (event.chat_id, event.thread_id)
                reply = await turn
            if reply is None:
                logger.info("turn end: message %s, no reply (%.1fs)", event.message_id,
                            time.monotonic() - started)
            elif reply.ok:
                logger.info("turn end: message %s ok in %.1fs, %d chars, meta %s",
                            event.message_id, time.monotonic() - started,
                            len(reply.text or ""), reply.meta or {})
                if reply.meta.get("rotated"):
                    # People see that the session changed and that nothing was dropped.
                    await self.send(f"♻️ {self.config.identity.alias} wrote a handover and "
                                    "starts a new session for this topic from it next turn.",
                                    chat_id=event.chat_id, thread_id=event.thread_id,
                                    silent=True)
            else:
                logger.warning("turn end: message %s FAILED in %.1fs: %s (meta %s)",
                               event.message_id, time.monotonic() - started, reply.error,
                               reply.meta or {})
            return reply
        except asyncio.CancelledError:
            if turn in self._stopping_turns and not _current_task_cancelling():
                logger.info("turn cancelled by /stop")
                return None
            raise
        except Exception:
            logger.exception("turn crashed: message %s after %.1fs", event.message_id,
                             time.monotonic() - started)
            raise
        finally:
            self._turns.discard(turn)
            self._turn_keys.pop(turn, None)
            self._busy_since.pop(busy_topic, None)
            if self._current_turn is turn:
                self._current_turn = None
            self._stopping_turns.discard(turn)
            if typing is not None:
                typing.cancel()

    @staticmethod
    async def _respond_later(adapter: Any, incoming: IncomingMessage,
                             delay: float) -> Optional[AdapterReply]:
        await asyncio.sleep(delay)
        return await adapter.respond(incoming)

    def _start_typing(self, event: ChannelEvent) -> Optional[asyncio.Task]:
        if self.bot is None or not getattr(self.config.limits, "typing_indicator", True):
            return None
        return asyncio.ensure_future(self._typing_loop(event.chat_id, event.thread_id))

    async def _ack_reaction(self, event: ChannelEvent) -> None:
        """👀 on the person's message: a visible receipt while a long turn runs."""
        if self.bot is None or not getattr(self.config.limits, "ack_reaction", True):
            return
        try:
            from aiogram.types import ReactionTypeEmoji
            await self.bot.set_message_reaction(chat_id=event.chat_id,
                                                message_id=event.message_id,
                                                reaction=[ReactionTypeEmoji(emoji="👀")])
        except Exception as exc:                              # noqa: BLE001 - cosmetic only
            logger.debug("ack reaction failed: %s", exc)

    async def _typing_loop(self, chat_id: int, thread_id: Optional[int]) -> None:
        """Keep "typing…" visible (Telegram clears it after about five seconds)."""
        while True:
            try:
                await self.bot.send_chat_action(chat_id=chat_id, action="typing",
                                                message_thread_id=thread_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001 - cosmetic only
                logger.debug("typing indicator failed: %s", exc)
            await asyncio.sleep(_TYPING_INTERVAL_SECONDS)

    async def _with_attachments(self, event: ChannelEvent) -> str:
        """The message text plus one line per downloaded attachment.

        Ported from ductor's media handling: files are saved where the agent can read them,
        under ``<adapter cwd>/.a2a-attachments/`` (self-ignored by git), and the prompt names
        each local path. Files over ``limits.max_attachment_mb`` are listed but not fetched.
        """
        text = event.text or ""
        if not event.attachments or self.bot is None:
            return text
        folder = self._workdir_for(event.chat_id, event.thread_id) / ATTACHMENTS_DIRNAME
        limit_mb = float(getattr(self.config.limits, "max_attachment_mb", 20.0))
        lines = []
        for item in event.attachments:
            name = _safe_filename(f"{event.message_id or 0}-{item.get('name') or 'file'}")
            size = int(item.get("size") or 0)
            kind = item.get("mime") or "application/octet-stream"
            if size > limit_mb * 1024 * 1024:
                lines.append(f"[attachment not downloaded: {item.get('name')} is "
                             f"{size / 1048576:.1f} MB, over the {limit_mb:g} MB limit]")
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
                ignore = folder / ".gitignore"
                if not ignore.exists():
                    ignore.write_text("*\n", encoding="utf-8")
                path = folder / name
                await self.bot.download(item["file_id"], destination=path)
            except Exception as exc:                          # noqa: BLE001 - the text still goes through
                logger.warning("attachment download failed (%s): %s", item.get("name"), exc)
                lines.append(f"[attachment could not be downloaded: {item.get('name')}]")
                continue
            lines.append(f"[attachment: {path} ({kind}, {max(size // 1024, 1)} KB)]")
        return (text + "\n\n" + "\n".join(lines)).strip() if lines else text

    def _may_queue(self, key: Any) -> bool:
        """``limits.busy_queue_max`` caps the queue of one topic when topics run in
        parallel (a busy topic must not starve the others), else the one shared queue."""
        limits = self.config.limits
        waiting = (self._busy_waiting if self._turn_slots is None
                   else self._busy_waiting_by.get(key, 0))
        return (getattr(limits, "busy_behavior", "queue") == "queue"
                and waiting < getattr(limits, "busy_queue_max", 5))

    def _cooldown_left(self, event: ChannelEvent) -> float:
        """How long a peer-bot message still has to wait for the bot-to-bot cooldown; 0 for
        messages that skip it (a person's, one in a forum topic, an expected hand-off, an
        explicit username delivery)."""
        if not event.author_is_bot or event.handoff or event.thread_id is not None:
            return 0.0                  # a forum topic has no cooldown: the peer-turn cap brakes
        adapter = self._adapter_for(event.chat_id, event.thread_id)
        if self.mentions_me(event.text) and adapter.accepts_explicit_username_delivery():
            return 0.0
        cooldown = getattr(self.config.limits, "min_seconds_between_agent_turns", 0.0)
        return max(cooldown - (self._clock() - self._last_turn_for(event)), 0.0)

    async def _wait_out_cooldown(self, event: ChannelEvent) -> None:
        """A queued peer-bot message keeps the bot-to-bot turn cooldown: it is delayed, not
        dropped. Messages that bypass the cooldown bypass it here."""
        remaining = self._cooldown_left(event)
        if remaining > 0:
            logger.info("bot-to-bot cooldown: holding the message from @%s for %.1fs",
                        event.author_username, remaining)
            await self._sleep(remaining)

    async def _run_turn(self, event: ChannelEvent, envelope: Envelope,
                        incoming: IncomingMessage, ticket: Optional[Dict[str, Any]]):
        started = self._clock()
        try:
            reply = await self._respond(incoming, event)
        except Exception as exc:                          # noqa: BLE001 - a broken adapter must not kill the loop
            if ticket:
                self.replies.queue_failed(ticket['id'], "adapter raised before queue confirmation")
            await self._note_adapter_failure(str(exc))
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            logger.exception("adapter failure")
            await self.send(f"❌ internal error: {exc}",
                            chat_id=event.chat_id, thread_id=event.thread_id)
            return None
        finally:
            self._last_agent_turn = self._clock()
            key = (event.chat_id, event.thread_id)
            self._last_turn_at[key] = self._last_agent_turn
            if event.author_is_bot and event.thread_id is not None:
                self._peer_turns[key] = self._peer_turns.get(key, 0) + 1
                self._save_turn_state()

        if reply is None:
            if ticket:
                self.replies.queue_failed(ticket['id'], "adapter did not confirm queue acceptance")
            self.stats.ignored += 1
            return None

        if not reply.ok and reply.meta.get("empty") and self._bot_account_post(event):
            # A bot's message (often a closing "DONE ... @you") that the agent had nothing
            # to say to: end quietly instead of posting an error into the bots' exchange.
            # An anonymous admin's post is not a bot's; a quota line still counts (#49.E).
            if ticket:
                self.replies.queue_failed(ticket['id'], "agent had nothing to say")
            await self._note_adapter_failure(str(reply.error or ""))
            logger.info("%s had nothing to say to @%s; no reply posted",
                        self.config.identity.alias, event.author_username)
            self.stats.ignored += 1
            return None

        if not reply.ok:
            if ticket:
                self.replies.queue_failed(ticket['id'], "queue submission failed")
            await self._note_adapter_failure(str(reply.error or ""))
            self.stats.errors += 1
            self.stats.last_error = reply.error
            resume = self._continues_after_timeout(event, reply)
            # A resumable session keeps the work so far (Reasonix recovers the interrupted
            # turn), so this is a pause, not a failure: ⏳, which peers read as busy.
            await self.send(f"⏳ {reply.error}; it continues where it stopped in its next turn."
                            if resume else f"❌ {reply.error}",
                            chat_id=event.chat_id, thread_id=event.thread_id)
            if resume and not self._busy_waiting_by.get((event.chat_id, event.thread_id)):
                self._run_own_task(CONTINUE_NOTE, event.chat_id, event.thread_id,
                                   self.topics.name(event.chat_id, event.thread_id))
            return None

        if reply.meta.get("queued"):
            if ticket:
                self.replies.queued(ticket['id'])
                # Protocol REQUESTs must be ACKed even when the task answered or skipped
                # so quickly that the human-facing receipt is no longer needed.
                await self._ack_inbound_request(envelope, event)
                if self.replies.get(ticket['id'])['state'] not in ('queued', 'overdue'):
                    self.stats.handled += 1
                    return None  # already answered/skipped; do not emit a stale receipt
            # A protocol REQUEST already received its machine ACK above. For ordinary
            # addressed prose, expose successful queueing so silence cannot be mistaken
            # for a dead transport; the active Codex task remains the substantive responder.
            if envelope.type == MessageType.REQUEST:
                self.stats.handled += 1
                return None
            receipt_body = f"✅ [{self.config.identity.alias}] queued in active task"
            if ticket:
                receipt_body += f" (receipt only; reply pending, ticket {ticket['id'][:8]})"
            receipt = format_message(Envelope(
                type=MessageType.RECEIPT, to=self._alias_for(event), intent="QUEUED",
                ref=(ticket['id'][:8] if ticket else str(event.message_id)),
                id=self._short_id(),
            ), receipt_body)
            self.stats.handled += 1
            reply_to_message_id = event.message_id if event.replies_to_bot else None
            try:
                sent = await self.send(
                    receipt, reply_to_message_id=reply_to_message_id, chat_id=event.chat_id,
                    thread_id=event.thread_id,
                    on_attempt=(lambda: self._assert_receipt_pending(ticket['id'])) if ticket else None,
                    return_message_id=True,
                )
                if sent is _SEND_SUPPRESSED:
                    return None
                if ticket:
                    self.replies.receipt_sent(ticket['id'], sent)
                return receipt
            except _ReceiptSuperseded:
                return None
            except BaseException as exc:
                if ticket:
                    self.replies.receipt_unknown(ticket['id'], f"receipt send failed: {exc}")
                raise

        body = reply.text.strip()
        if not body or is_silent_reply(body):
            if body:
                logger.info("turn for message %s chose to stay silent", event.message_id)
            self.stats.ignored += 1
            self._ask_for_report(event)
            return None

        if await self.membership.on_reply(body, event):
            self.stats.handled += 1                 # a role decision, applied, not posted
            return None
        self.stats.handled += 1
        opened = parse_new_topic(body)
        if opened is not None:
            return await self._open_topic(event, *opened)
        limit = GENERAL_PEER_REPLY_CHARS if event.author_is_bot else GENERAL_REPLY_CHARS
        hands_off = bool(self._work_moves_to_topics(event) and [
            p for p in self.assigned_peers(body)
            if p.lower() != (self._asker(event) or "").lstrip("@").lower()
            and str(self.config.peers.get(p, "")).lstrip("@").lower()
            != (self._asker(event) or "").lstrip("@").lower()])
        if self._work_moves_to_topics(event) and not is_command_reply(body) \
                and (len(body) > limit or hands_off):
            # Work in General: a substantial reply to a peer, or a long answer. It goes to the
            # topic already about the same issue or PR when there is one, else a new topic
            # (Efty: General is for setup, not development). Short answers stay.
            known = self._topic_for_refs(f"{event.text or ''}\n{body}")
            if known is not None:
                return await self._post_in_known_topic(event, body, known)
            if not event.author_is_bot or hands_off:
                # A person's request is a new work item: it gets its own topic, and so does a
                # hand-off (the export of a live test showed short hand-offs staying in
                # General). Otherwise, between bots a topic is opened on purpose (NEW_TOPIC),
                # never as a reaction to a reply: that spawned a topic per hop in a live
                # test, each with a fresh context.
                return await self._open_topic(event, title_from(event.text or ""), body,
                                              moved=True)
        origin = self._topic_origins.get((event.chat_id, event.thread_id))
        report = parse_report_back(body) if origin is not None else None
        elapsed = self._clock() - started
        suffix = f"\n\n({elapsed:.0f}s)" if elapsed >= 60 else ""
        reply_to_message_id = event.message_id if event.replies_to_bot else None
        shown = strip_report_marker(body) if report is not None else body
        sent = await self.send(f"{shown}{suffix}", reply_to_message_id=reply_to_message_id,
                               chat_id=event.chat_id, thread_id=event.thread_id)
        self._log_topic(event.chat_id, event.thread_id, self.config.identity.alias, shown,
                        self.last_first_message_id)
        self._mirror_handoffs(body, event.chat_id, event.thread_id, self.last_first_message_id,
                              asker=self._asker(event))
        if report is not None and origin is not None:
            self._topics_reported.add((event.chat_id, event.thread_id))
            link = topic_link(event.chat_id, event.thread_id) if event.thread_id else ""
            # A summary for people: its @mentions would start turns for every bot it names.
            await self.send(f"{defuse_mentions(report)}\n\n↩ from {link}".rstrip(),
                            chat_id=origin[0], thread_id=origin[1])
        return sent

    @staticmethod
    def _anonymous_admin_post(event: ChannelEvent) -> bool:
        """Posted by a group admin with "Remain anonymous" on: Telegram sends it as
        @GroupAnonymousBot on behalf of the group itself, so nobody can tell which admin
        wrote it and the swarm treats it as a stranger."""
        sender_chat = getattr(event.raw, "sender_chat", None)
        chat_id = getattr(sender_chat, "id", None)
        return event.author_is_bot and chat_id is not None and int(chat_id) == int(event.chat_id)

    async def _anonymous_admin_notice(self, event: ChannelEvent) -> None:
        """Tell the human, once every few hours, why a message to this bot gets no answer:
        an anonymous admin cannot be told apart from any other admin, so it never starts a
        turn. Only the bot it names says so, so the group gets one notice, not one per bot."""
        username = self.config.identity.name.lstrip("@")
        if not username or not re.search(rf"(?i)@{re.escape(username)}\b", event.text or ""):
            return
        now = self._clock()
        if now - self._anonymous_noticed < ANONYMOUS_NOTICE_SECONDS:
            return
        self._anonymous_noticed = now
        await self.send(
            f"⚠️ {self.config.identity.alias} can't act on this: it was posted anonymously, so "
            "it can't be told apart from any other admin's. Turn off \"Remain anonymous\" "
            "(group settings, Administrators, your entry) and send it again.",
            reply_to_message_id=event.message_id, chat_id=event.chat_id,
            thread_id=event.thread_id, silent=True)

    async def _mention_drop_notice(self, event: ChannelEvent, reason: str) -> None:
        """Tell a peer bot which of its handles this bot left unanswered, and why (issue #27).

        Every @handle in a bot's hand-off counts as addressed, so a message that names this bot
        and still gets no turn is a lost mention: the sender hears which handle was dropped
        instead of silently assuming the work was taken. Machine traffic (a notice, a command
        answer, a sign-off) never asked for a turn and a bot's own echo is addressed to nobody,
        so those stay silent; peers' transports drop "⚠️" notices, so this starts no turn back.
        """
        if not event.author_is_bot or not event.text or reason in _NO_DROP_NOTICE_REASONS:
            return
        handles = self._dropped_handles(event.text)
        if not handles:
            return
        key = (event.chat_id, event.thread_id, reason)
        now = self._clock()
        last = self._drop_notices.get(key)
        if last is not None and now - last < DROP_NOTICE_SECONDS:
            return
        self._drop_notices[key] = now
        # No @ on the sender: mentioning it back would hand it a turn (and a notice of ours).
        await self.send(
            f"⚠️ {self.config.identity.alias} took no turn: your message names "
            f"{', '.join(handles)}, but {reason}. Address one of those handles alone if the "
            "work is meant for me.",
            reply_to_message_id=event.message_id, chat_id=event.chat_id,
            thread_id=event.thread_id, silent=True)

    async def _cooldown_notice(self, event: ChannelEvent) -> None:
        """Say once per cooldown window that a message to this bot was dropped, so the sender's
        human sees it. Peers' transports drop "⚠️" notices, so this starts no turn."""
        window = getattr(self.config.limits, "min_seconds_between_agent_turns", 0.0)
        now = self._clock()
        if now - self._cooldown_noticed < window:
            return
        self._cooldown_noticed = now
        # No @: a mention would land in the sender's own cooldown and draw its notice back.
        sender = event.author_username or "a peer"
        await self.send(
            f"⚠️ {self.config.identity.alias} skipped this message from {sender}: bot-to-bot "
            f"cooldown in General, {event.cooldown_left:.0f}s left. Send it again after that, "
            "or use a topic (topics have no cooldown).",
            reply_to_message_id=event.message_id, chat_id=event.chat_id, silent=True)

    def _learn_topic_refs(self, chat_id: int, thread_id: int, text: str) -> None:
        """Remember which topic first talked about each issue or PR (any bot's topic: every
        transport sees the group), so later work on it goes there, not to a new topic."""
        for ref in issue_refs(text):
            self._ref_topics.setdefault(ref, (chat_id, thread_id))

    def _topic_for_refs(self, text: str) -> Optional[Tuple[int, int]]:
        for ref in issue_refs(text):
            if ref in self._ref_topics:
                return self._ref_topics[ref]
        return None

    async def _post_in_known_topic(self, event: ChannelEvent, body: str,
                                   topic: Tuple[int, int]) -> Any:
        chat_id, thread_id = topic
        logger.info("moving work from General to topic %s, which is about the same item",
                    thread_id)
        await self.send(body, chat_id=chat_id, thread_id=thread_id)
        self._log_topic(chat_id, thread_id, self.config.identity.alias, body,
                        self.last_first_message_id)
        # The moved reply may be the hand-off that matters (live test: it had no bus copy).
        self._mirror_handoffs(body, chat_id, thread_id, self.last_first_message_id,
                              asker=self._asker(event))
        return await self.send(f"↪ moved this to {topic_link(chat_id, thread_id)}",
                               chat_id=event.chat_id, reply_to_message_id=event.message_id)

    def _work_moves_to_topics(self, event: ChannelEvent) -> bool:
        """Whether this message is in the home group's General with limits.work_in_topics on
        (and a Telegram connection that can open topics)."""
        home = self.config.identity.home_channel_id
        return (bool(getattr(self.config.limits, "work_in_topics", False))
                and event.thread_id is None and home is not None
                and int(event.chat_id) == int(home) and not event.self_task
                and self.bot is not None)

    async def _open_topic(self, event: ChannelEvent, title: str, brief: str,
                          moved: bool = False) -> Any:
        """NEW_TOPIC: open a forum topic for the brief (topic_open.py), or say why not.
        ``moved``: the brief is this agent's reply to work started in General, posted in the
        new topic instead; it already is the work, so nothing more is run."""
        home = self.config.identity.home_channel_id
        now = self._clock()
        self._topics_opened = [t for t in self._topics_opened if now - t < 3600]
        reason = ""
        thread_id: Optional[int] = None
        name = topic_name(self.config.identity.alias, title)
        if len(self._topics_opened) >= MAX_TOPICS_PER_HOUR:
            reason = f"{MAX_TOPICS_PER_HOUR} topics opened in the last hour already"
        elif self.bot is None or not home:
            reason = "no Telegram connection"
        else:
            try:
                topic = await self.bot.create_forum_topic(chat_id=home, name=name)
                thread_id = int(topic.message_thread_id)
            except Exception as exc:                       # noqa: BLE001 - reported in chat
                reason = str(exc)
        if (thread_id is None or home is None) and moved:
            logger.warning("could not move work to topic %r: %s", name, reason)
            sent = await self.send(brief, chat_id=event.chat_id, thread_id=event.thread_id)
            self._mirror_handoffs(brief, event.chat_id, event.thread_id,
                                  self.last_first_message_id, asker=self._asker(event))
            return sent
        if thread_id is None or home is None:
            logger.warning("could not open topic %r: %s", name, reason)
            return await self.send(
                f"{brief}\n\n(could not open a topic for this: {reason}; turn on Topics in "
                "the group and give the bot the admin right to manage topics)",
                chat_id=event.chat_id, thread_id=event.thread_id)
        self._topics_opened.append(now)
        self.topics.remember(home, thread_id, name, current=True)
        self._learn_topic_refs(home, thread_id, f"{title}\n{brief}\n{event.text or ''}")
        self._topic_origins[(home, thread_id)] = (event.chat_id, event.thread_id)
        logger.info("opened topic %r (thread %s) for message %s", name, thread_id,
                    event.message_id)
        if moved:
            # Mentions defused: quoting "@peer do X" must not hand the peer a second turn.
            quoted = defuse_mentions(" ".join((event.text or "").split())[:300])
            await self.send(f"Moved here from General. The request:\n> {quoted}",
                            chat_id=home, thread_id=thread_id, silent=True)
            # A bot never receives its own posts: its topic memory learns the task here.
            self._log_topic(home, thread_id, self._alias_for(event), event.text or "",
                            self.last_first_message_id)
        await self.send(brief, chat_id=home, thread_id=thread_id)
        self._log_topic(home, thread_id, self.config.identity.alias, brief,
                        self.last_first_message_id)
        self._mirror_handoffs(brief, home, thread_id, self.last_first_message_id,
                              asker=self._asker(event))
        verb = "moved this to" if moved else "opened"
        sent = await self.send(f"↪ {verb} topic «{name}»: {topic_link(home, thread_id)}",
                               chat_id=event.chat_id, thread_id=event.thread_id,
                               reply_to_message_id=event.message_id if moved else None)
        if moved:
            return sent
        if not hands_over(brief, self._peer_handle()):
            # Not addressed to a peer: the brief is this agent's own task, in the new topic.
            # A peer named further down (to confirm scope, say) still sees the brief.
            self._run_own_task(f"{SELF_TASK_NOTE}\n\n{brief}", home, thread_id, name)
        return sent

    def assigned_peers(self, text: str) -> List[str]:
        """Peers a reply hands work to: those whose @handle opens the reply or one of its
        lines or bullets (the same structural rule receivers use to tell an assignment from
        a list). Decided here, not by the model remembering to use the bus."""
        lead = set(re.findall(r"@(\w+)", (re.match(r"^(?:\s*@\w+[:,]?)*", text or "")
                                          or re.match("", "")).group(0)))
        lead |= set(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])?\s*@(\w+)[:,]?[ \t]+(?!\()\S",
                               text or ""))
        lowered = {h.lower() for h in lead}
        return [alias for alias, target in self.config.peers.items()
                if str(target).lstrip("@").lower() in lowered or alias.lower() in lowered]

    def _mirror_handoffs(self, body: str, chat_id: int, thread_id: Optional[int],
                         message_id: Optional[int], asker: Optional[str] = None) -> None:
        """Every hand-off is also written to the durable bus, signed, by the transport: the
        agents never did it themselves (live test), and Telegram can drop a message (busy
        queue, cooldown). A receiver that already took the Telegram copy skips the mirror;
        one that missed it gets the work from the bus on its next poll."""
        peers = self.assigned_peers(body)
        if asker:
            # The bot this reply answers is in the conversation already: an answer that opens
            # with its handle is not a hand-off (live test: plain answers were mirrored).
            who = asker.lstrip("@").lower()
            peers = [p for p in peers
                     if p.lower() != who and str(self.config.peers.get(p, "")).lstrip("@").lower() != who]
        if not peers or self.bot is None:
            return
        for peer in peers:
            self._pending_handoffs[(int(chat_id), thread_id, peer)] = {
                "at": self._clock(), "message_id": message_id, "nudged": False}
        if not (self.config.bus.repo and getattr(self.config.bus, "mirror_handoffs", True)):
            return
        # Issue #22: the bus carries a topic token, never the chat id. The token resolves
        # through each peer's own config, so dedupe survives without an id leaving the host;
        # an unbound topic falls back to an HMAC token, which no peer can resolve (it then
        # runs the mirror too: processed twice, never lost).
        ref = dict(Redactor.from_config(self.config).topic_ref(
            int(chat_id), thread_id, getattr(self, "router", None)))
        if message_id is not None:
            ref["message_id"] = message_id

        def write() -> None:
            from swarmly.telegram.relay import post_and_notify
            for peer in peers:
                try:
                    asyncio.run(post_and_notify(self, to=peer, text=body,
                                                intent="TASK_HANDOFF", notify=False,
                                                extra={"telegram": ref}))
                except Exception:                          # noqa: BLE001 - best effort
                    logger.warning("could not mirror the hand-off to %s on the bus", peer,
                                   exc_info=True)
                else:
                    logger.info("mirrored the hand-off to %s on the bus (telegram %s)", peer,
                                message_id)

        task = asyncio.ensure_future(asyncio.to_thread(write))
        self._mirror_tasks.add(task)
        task.add_done_callback(self._mirror_tasks.discard)

    def _save_turn_state(self) -> None:
        save_state(self._turns_path, {
            "peer_turns": [[k[0], k[1], n] for k, n in self._peer_turns.items()
                           if isinstance(k, tuple) and len(k) == 2],
            "paused": [list(k) for k in self._paused_topics
                       if isinstance(k, tuple) and len(k) == 2],
        }, "topic turn counts")

    @staticmethod
    def _presence_traffic(event: ChannelEvent) -> bool:
        """A bot's online, seen, bye or receipt line: liveness, not discussion, so it stays
        out of topic memory (a restart once filled a catch-up with eleven of them)."""
        if not event.author_is_bot:
            return False
        envelope, _body = parse_message(event.text or "")
        if envelope is not None and envelope.type in (MessageType.HELLO, MessageType.BYE,
                                                      MessageType.RECEIPT, MessageType.ACK):
            return True
        return machine_prose(event.text or "").startswith(("🟢", "👀", "⏳", "♻️"))

    def _log_topic(self, chat_id: int, thread_id: Optional[int], who: str, text: str,
                   message_id: Optional[int]) -> None:
        self._topic_memory.record(chat_id, thread_id, who, text, message_id)

    def _topic_recap(self, event: ChannelEvent) -> str:
        """What the discussion is about, before the new message. A runtime that starts every
        turn fresh gets the task, the progress so far and the latest messages; one that
        resumes its own session per topic gets what others posted since its last reply."""
        adapter = self._adapter_for(event.chat_id, event.thread_id)
        mode = topic_mode(adapter, self.topics.context(event.chat_id, event.thread_id))
        if not mode:
            return ""
        return self._topic_memory.recap(
            event.chat_id, event.thread_id, current_id=event.message_id,
            fresh=mode == "fresh", own_alias=self.config.identity.alias)

    def _fresh_text(self, event: ChannelEvent, message: str) -> Optional[str]:
        """For a runtime that resumes the topic's session: the message as a fresh run would
        get it (task and progress first), in case the session turns out to be gone and the
        adapter starts a new one. None otherwise."""
        adapter = self._adapter_for(event.chat_id, event.thread_id)
        if topic_mode(adapter, self.topics.context(event.chat_id, event.thread_id)) \
                != "session":
            return None
        return self._topic_memory.recap(
            event.chat_id, event.thread_id, current_id=event.message_id, fresh=True,
            own_alias=self.config.identity.alias, mark_seen=False) + message

    def _continues_after_timeout(self, event: ChannelEvent, reply: AdapterReply) -> bool:
        """A topic turn that hit the time limit in a resumable session goes on from where it
        stopped (the session holds the work so far) instead of being dropped: once, so a
        task that keeps timing out reaches a person."""
        if not reply.meta.get("timed_out") or event.thread_id is None:
            return False
        if event.self_task and (event.text or "").startswith(CONTINUE_NOTE[:40]):
            return False
        adapter = self._adapter_for(event.chat_id, event.thread_id)
        return topic_mode(adapter, self.topics.context(event.chat_id, event.thread_id)) \
            == "session"

    def _handoff_answered(self, event: ChannelEvent) -> None:
        """A peer bot posting in a discussion answers the hand-offs it had there; its ⏳ busy
        line restarts their clock instead (busy is not an answer, and not gone either)."""
        who = (event.author_username or "").lstrip("@").lower()
        prose = machine_prose(event.text or "")
        for alias, target in self.config.peers.items():
            if who and str(target).lstrip("@").lower() == who:
                key = (int(event.chat_id), event.thread_id, alias)
                if prose.startswith("⏳") and key in self._pending_handoffs:
                    self._pending_handoffs[key].update(at=self._clock(), nudged=False)
                else:
                    self._pending_handoffs.pop(key, None)
            elif event.thread_id is not None:
                self._peer_followed_up(event, prose, alias, str(target).lstrip("@"))

    def _peer_followed_up(self, event: ChannelEvent, prose: str, alias: str,
                          handle: str) -> None:
        """Another bot already reminded (or reported) a silent peer here: one reminder per
        hand-off, not one per bot that handed it work (live run: two at the same second)."""
        key = (int(event.chat_id), event.thread_id, alias)
        entry = self._pending_handoffs.get(key)
        if entry is None:
            return
        if re.search(rf"(?i)@{re.escape(handle)}\s+reminder: the hand-off above",
                     event.text or ""):
            entry["nudged"] = True
        elif f"{alias} has not answered the hand-off above" in prose:
            self._pending_handoffs.pop(key, None)

    async def check_stalled_handoffs(self) -> None:
        """A hand-off its bot has not answered after ``limits.handoff_nudge_minutes`` gets one
        reminder that mentions the bot (it wakes it, and the bus copy is re-read); after twice
        that, one notice for the people (live test: a hand-off sat for 20 minutes until a
        person noticed). Decided by the clock, not by the model remembering to follow up."""
        minutes = float(getattr(self.config.limits, "handoff_nudge_minutes", 0) or 0)
        if minutes <= 0 or not self._pending_handoffs:
            return
        now, wait = self._clock(), minutes * 60.0
        for key, entry in list(self._pending_handoffs.items()):
            chat_id, thread_id, alias = key
            handle = str(self.config.peers.get(alias, alias)).lstrip("@")
            age = now - float(entry["at"])
            if not entry["nudged"] and age >= wait:
                entry["nudged"] = True
                logger.warning("hand-off to %s in %s has had no answer for %.0f min: reminding",
                               alias, thread_id or "General", age / 60)
                await self.send(f"@{handle} reminder: the hand-off above is still waiting for "
                                "you. Answer it, or say why you can't.",
                                chat_id=chat_id, thread_id=thread_id,
                                reply_to_message_id=entry.get("message_id"))
            elif entry["nudged"] and age >= 2 * wait:
                self._pending_handoffs.pop(key, None)
                logger.warning("hand-off to %s in %s is still unanswered after %.0f min",
                               alias, thread_id or "General", age / 60)
                admin = self.config.identity.maintainer_username
                lead = f"@{admin.lstrip('@')} " if admin else ""
                await self.send(f"⚠️ {lead}{alias} has not answered the hand-off above for "
                                f"{age / 60:.0f} minutes. It may be stopped or stuck.",
                                chat_id=chat_id, thread_id=thread_id,
                                reply_to_message_id=entry.get("message_id"), silent=True)
                # The bot that handed the work over decides what happens to it now: a peer
                # that is down must not hold the task (live run: nobody reassigned it).
                self._run_own_task(REASSIGN_NOTE.format(alias=alias, minutes=round(age / 60)),
                                   chat_id, thread_id, self.topics.name(chat_id, thread_id))

    @staticmethod
    def _asker(event: ChannelEvent) -> Optional[str]:
        """The peer bot a reply answers, if a bot asked."""
        return event.author_username if event.author_is_bot else None

    def took_telegram_copy(self, ref: Any) -> bool:
        """Whether this bot already ran (or queued) the Telegram message a bus mirror names.

        Issue #22: a mirror names a topic token rather than a chat id. An un-resolvable token
        (the HMAC fallback, a project this agent does not configure) reports "not taken", so
        the turn runs from the bus as well -- a duplicate, never a lost hand-off. The old
        ``{"chat": id}`` form is still accepted, so a mirror written before the change keeps
        its dedupe.
        """
        if not isinstance(ref, dict):
            return False
        try:
            message_id = int(ref["message_id"])
        except (KeyError, TypeError, ValueError):
            return False
        token = ref.get("topic")
        if isinstance(token, str) and token:
            from swarmly.telegram.redact import resolve_topic_token
            import os
            candidates = resolve_topic_token(token, getattr(self, "router", None),
                                             secret=os.environ.get(
                                                 "SWARMLY_REDACTION_SECRET", "").strip())
            # An ambiguous token (one project bound to two chats on the same thread id)
            # cannot say which chat the mirror came from, and message ids repeat per chat:
            # claiming it was taken could swallow another chat's hand-off. Run it instead --
            # processed twice, never lost (review of PR #23).
            if len(candidates) != 1:
                return False
            chat_id, _thread = candidates[0]
            return (int(chat_id), message_id) in self._tg_admitted
        try:
            return (int(ref["chat"]), message_id) in self._tg_admitted
        except (KeyError, TypeError, ValueError):
            return False

    def _peer_handle(self) -> "re.Pattern[str]":
        """Matches a peer's @username or @alias, or ``Alias:``, at the start of a text."""
        names = []
        for alias, target in self.config.peers.items():
            names += [re.escape(str(target).lstrip("@")), re.escape(alias)]
        names = [n for n in names if n]
        if not names:
            return re.compile(r"(?!)")
        joined = "|".join(names)
        return re.compile(rf"(?i)(?:@(?:{joined})\b|(?:{joined})\s*[:,])")

    def peers_changed(self) -> None:
        """The roster changed: who counts as a peer when a discussion is recapped."""
        self._topic_memory.peer_handles = {str(h).lstrip("@").lower()
                                           for h in self.config.peers.values() if h}

    def roles_changed(self) -> None:
        """This agent's or a peer's role changed: every adapter's next turn carries it."""
        from swarmly.telegram.adapters import apply_role
        for adapter in {id(a): a for a in [self.adapter, *self._project_adapters.values()]
                        }.values():
            apply_role(adapter, self.config)

    def _run_own_task(self, text: str, chat_id: int, thread_id: Optional[int],
                      name: Optional[str]) -> None:
        task = ChannelEvent(text=text, chat_id=chat_id, thread_id=thread_id, topic_name=name,
                            self_task=True,
                            author_username=f"{self.config.identity.alias} (own brief)")
        self._self_tasks.add(asyncio.create_task(self.handle_event(task)))
        self._self_tasks = {t for t in self._self_tasks if not t.done()}

    def _ask_for_report(self, event: ChannelEvent) -> None:
        """A topic this agent opened went quiet (its turn chose silence) before it reported
        back: ask it once for the REPORT_BACK, so the origin hears the outcome (live test:
        neither topic ever reported back)."""
        key = (event.chat_id, event.thread_id)
        if key not in self._topic_origins or key in self._topics_reported \
                or key in self._report_asked:
            return
        self._report_asked.add(key)
        logger.info("topic %s went quiet without a report back; asking for one", key)
        self._run_own_task(REPORT_NOTE, event.chat_id, event.thread_id,
                           self.topics.name(event.chat_id, event.thread_id))

    def _alias_for(self, event: ChannelEvent) -> str:
        if self.config.is_admin(event.author_id):
            return "admin"
        alias = self.config.peer_alias_for(event.author_username, event.author_id)
        return alias or (event.author_username or f"user:{event.author_id}")

    def _admin_label(self) -> str:
        admin = self.config.identity.admin_user_id
        return f"the admin (id {admin})" if admin else "the admin"

    def _assert_receipt_pending(self, ticket_id: str) -> None:
        """Atomically claim the receipt after pacing, immediately before network I/O."""
        if not self.replies.claim_receipt(ticket_id):
            row = self.replies.get(ticket_id)
            if row['receipt_state'] != 'none':
                reason = f"already reserved ({row['receipt_state']})"
            else:
                reason = f"is {row['state']}"
            raise _ReceiptSuperseded(f"reply ticket {ticket_id[:8]} {reason}")

    # ----------------------------------------------------------------------------------
    # Outbound
    # ----------------------------------------------------------------------------------
    def format_outgoing(self, text: str) -> str:
        """Escape everything except the alias banner, then clamp to Telegram's 4096 limit."""
        body = html.escape(text, quote=False)
        limit = self.config.limits.max_message_chars
        if len(body) > limit:
            body = body[: limit - 32].rstrip() + "\n… [message truncated]"
        return markdown_html(body)

    async def _wait_for_slot(self, topic: Any = None) -> None:
        """Wait until the next send would not have to wait for its pacing gap."""
        last = self._last_send if self._turn_slots is None else self._topic_pace(topic)["last"]
        gap = self.config.limits.min_seconds_between_messages - (self._clock() - last)
        if gap > 0:
            await self._sleep(gap)

    def _topic_pace(self, topic: Any) -> Dict[str, Any]:
        state = self._topic_paces.get(topic)
        if state is None:
            state = self._topic_paces[topic] = {"last": float("-inf"), "stamps": []}
        return state

    def _send_lock_for(self, topic: Any) -> asyncio.Lock:
        """One send lock for the bot, or one per topic when topics run in parallel (so a
        reply waiting out its topic's pacing never holds up another topic)."""
        if self._turn_slots is None:
            return self._send_lock
        lock = self._topic_send_locks.get(topic)
        if lock is None:
            lock = self._topic_send_locks[topic] = asyncio.Lock()
        return lock

    async def _pace(self, topic: Any = None) -> None:
        """Keep sends apart. With parallel topics, ``min_seconds_between_messages`` and
        ``max_messages_per_minute`` apply to each topic, and the whole group stays under
        Telegram's ceiling of about 20 messages a minute."""
        if self._turn_slots is not None:
            await self._pace_topic(topic)
            return
        limit = self.config.limits.min_seconds_between_messages
        now = self._clock()
        gap = limit - (now - self._last_send)
        if gap > 0:
            await self._sleep(gap)
        self._sent_timestamps = [t for t in self._sent_timestamps if self._clock() - t < 60]
        max_rate = getattr(self.config.limits, "max_messages_per_minute", None) or self.config.limits.max_group_messages_per_minute
        if len(self._sent_timestamps) >= max_rate:
            await self._sleep(60 - (self._clock() - self._sent_timestamps[0]) + 0.1)
            # Forget only what aged out: the rest of the window still counts.
            self._sent_timestamps = [t for t in self._sent_timestamps if self._clock() - t < 60]
        self._last_send = self._clock()
        self._sent_timestamps.append(self._last_send)

    async def _pace_topic(self, topic: Any) -> None:
        state = self._topic_pace(topic)
        gap = self.config.limits.min_seconds_between_messages - (self._clock() - state["last"])
        if gap > 0:
            await self._sleep(gap)
        max_rate = (getattr(self.config.limits, "max_messages_per_minute", None)
                    or self.config.limits.max_group_messages_per_minute)
        while True:         # no await between the last check and the stamps below
            now = self._clock()
            state["stamps"] = [t for t in state["stamps"] if now - t < 60]
            self._sent_timestamps = [t for t in self._sent_timestamps if now - t < 60]
            waits = []
            if len(state["stamps"]) >= max_rate:
                waits.append(60 - (now - state["stamps"][0]) + 0.1)
            if len(self._sent_timestamps) >= GROUP_MESSAGES_PER_MINUTE:
                waits.append(60 - (now - self._sent_timestamps[0]) + 0.1)
            if self._sent_timestamps and now - self._sent_timestamps[-1] < GROUP_MIN_GAP_SECONDS:
                waits.append(GROUP_MIN_GAP_SECONDS - (now - self._sent_timestamps[-1]))
            if self._flood_until > now:
                waits.append(self._flood_until - now)
            if not waits:
                break
            await self._sleep(max(waits))
        state["last"] = self._last_send = self._clock()
        state["stamps"].append(state["last"])
        self._sent_timestamps.append(state["last"])
        if len(self._topic_paces) > _PACE_STATE_LIMIT:
            self._prune_topic_paces()

    async def _pace_group(self) -> None:
        """Wait for, and take, a slot under the group's gap and cap only (a retry)."""
        while True:         # no await between the last check and the stamp below
            now = self._clock()
            self._sent_timestamps = [t for t in self._sent_timestamps if now - t < 60]
            waits = []
            if len(self._sent_timestamps) >= GROUP_MESSAGES_PER_MINUTE:
                waits.append(60 - (now - self._sent_timestamps[0]) + 0.1)
            if self._sent_timestamps and now - self._sent_timestamps[-1] < GROUP_MIN_GAP_SECONDS:
                waits.append(GROUP_MIN_GAP_SECONDS - (now - self._sent_timestamps[-1]))
            if self._flood_until > now:
                waits.append(self._flood_until - now)
            if not waits:
                break
            await self._sleep(max(waits))
        self._sent_timestamps.append(self._clock())

    def _prune_topic_paces(self) -> None:
        """Forget the pacing state of topics idle for over a minute. Their send locks stay:
        a reply may be about to take one that has just been released."""
        horizon = self._clock() - max(60.0, self.config.limits.min_seconds_between_messages)
        for key, state in list(self._topic_paces.items()):
            if state["last"] < horizon:
                del self._topic_paces[key]

    def addressed_payload(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Render *text* for the channel, with mention entities for any peer it addresses.

        Peer-facing messages need Telegram's own mention metadata or the peer's bot never
        receives them (spec rule 3). Offsets are computed against the *rendered* message —
        banner included — because that is the text Telegram counts against. Entity-bearing
        messages are sent as plain text with parse_mode=None, not double-encoded as HTML.

        Like the plain path, the rendered message is held to ``max_message_chars``: over-long
        text is clamped from the tail *before* the handles are appended, so a clamped message
        never loses its addressing, and no entity is ever sent over characters that were cut.
        """
        limit = self.config.limits.max_message_chars
        rendered, entities = self._render_addressed(text)
        if utf16_length(rendered) <= limit:
            return rendered, entities
        overhead = max(utf16_length(rendered) - utf16_length(text), 0)
        budget = max(limit - overhead - 32, 256)
        clamped = _truncate_utf16(text, budget) + "\n… [message truncated]"
        rendered, entities = self._render_addressed(clamped)
        if utf16_length(rendered) > limit:                      # pathological expansion — hard clamp
            rendered = _truncate_utf16(rendered, limit - 32) + "\n… [message truncated]"
            width = utf16_length(rendered)
            entities = [e for e in entities if int(e["offset"]) + int(e["length"]) <= width]
        return rendered, entities

    def _render_addressed(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        body, styles = markdown_entities(text)
        found: List[Dict[str, Any]] = []
        # Bots a newcomer heard of before it knows their aliases: its JOIN mentions them.
        targets = {**self.membership.candidates, **(self.config.peers or {})}
        for alias, username in targets.items():
            if not username:
                continue
            body, entities = addressed(body, username, alias=alias)
            found.extend(entities)

        banner = f"[{self.config.identity.alias}] "
        if not found:
            # No mention: the message goes out on the HTML path, which renders the banner
            # and the Markdown itself; an empty list is how callers pick that path.
            return banner + text, []

        def overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
            return (int(a["offset"]) < int(b["offset"]) + int(b["length"])
                    and int(b["offset"]) < int(a["offset"]) + int(a["length"]))
        # A mention must stay a mention (it is what delivers the message): a code span over
        # one is dropped rather than risk Telegram refusing the overlap.
        found += [s for s in styles if s["type"] == "bold"
                  or not any(overlaps(s, m) for m in found)]
        rendered = banner + body
        shift = utf16_length(banner)
        # The banner is bold on this path too, as on the HTML path.
        bold = {"type": "bold", "offset": 0, "length": utf16_length(banner.rstrip())}
        return rendered, [bold] + [dict(entity, offset=int(entity["offset"]) + shift)
                                   for entity in found]

    def split_outgoing(self, text: str) -> List[str]:
        """Split *text* into at most ``limits.max_reply_parts`` messages that each fit.

        Ported from ductor's ``split_html_message``: cut at a paragraph break, else a line
        break, else a space, else hard. Sizes are checked against what is actually sent (the
        escaped body, the alias banner and any peer handles, in UTF-16 units). Part 1 keeps
        the envelope header and the peer addressing and ends with ``(1/n)``; later parts start
        with ``↪ (k/n)``, which receiving transports treat as a continuation, never a new
        turn. Text that still remains after the last part is clamped as before.
        """
        max_parts = max(int(getattr(self.config.limits, "max_reply_parts", 1) or 1), 1)
        if max_parts == 1 or self._fits(text):
            return [text]
        worst = f"{max_parts}/{max_parts}"
        head_room, tail_room = f"\n\n({worst})", f"↪ ({worst}) "
        pieces: List[str] = []
        rest = text
        while rest and len(pieces) < max_parts - 1:
            first = not pieces
            if self._fits((tail_room + rest) if not first else rest + head_room):
                break
            cut = self._split_point(rest, lambda p: self._fits(
                (p + head_room) if first else (tail_room + p)))
            pieces.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip("\n")
        if rest.strip():
            pieces.append(rest)
        if len(pieces) == 1:
            return pieces
        total = len(pieces)
        parts = [f"{pieces[0]}\n\n(1/{total})"]
        parts += [f"↪ ({index}/{total}) {piece}" for index, piece in enumerate(pieces[1:], 2)]
        return parts

    def _fits(self, text: str) -> bool:
        limit = self.config.limits.max_message_chars
        banner = f"[{self.config.identity.alias}] "
        return (len(html.escape(text, quote=False)) <= limit
                and utf16_length(banner + text) <= limit
                and utf16_length(self._render_addressed(text)[0]) <= limit)

    @staticmethod
    def _split_point(text: str, fits: Callable[[str], bool]) -> int:
        """The cut index: the longest prefix that fits, pulled back to a natural break."""
        low, high = 1, len(text)
        while low < high:                                   # longest fitting prefix
            middle = (low + high + 1) // 2
            if fits(text[:middle]):
                low = middle
            else:
                high = middle - 1
        floor = low // 2                                    # don't cut a sliver for a break
        for separator in ("\n\n", "\n", " "):
            at = text.rfind(separator, floor, low)
            if at > 0:
                return at + len(separator)
        return low

    async def send(self, text: Union[str, Callable[[], Optional[str]]],
                   reply_to_message_id: Optional[int] = None,
                   *, chat_id: Optional[int] = None, thread_id: Optional[int] = None,
                   on_attempt=None, return_message_id: bool = False,
                   allow_when_frozen: bool = False, silent: bool = False):
        """Post to the home channel, paced, escaped, and split when it is too long.

        A split message goes out as consecutive parts under one hold of the send lock, so
        nothing interleaves; the return value describes the first part.

        ``text`` may be a callable: it is called once the pacing slot is free, just before
        the send, so the message says what is true when it goes out; returning None or ""
        sends nothing. ``silent`` sends without a notification sound.
        """
        destination = self.config.identity.home_channel_id if chat_id is None else chat_id
        topic = (destination, thread_id)
        async with self._send_lock_for(topic):
            if callable(text):
                await self._wait_for_slot(topic)
                text = text()
                if not text:
                    return None
            parts = self.split_outgoing(strip_autolinks(str(text)))
            first = None
            self._gone_topics.discard(topic)
            for index, part in enumerate(parts):
                if thread_id is not None and topic in self._gone_topics:
                    # One lock for the bot: the first part was rerouted in place.
                    await self._send_rest_in_general(parts[index:], index, chat_id,
                                                     return_message_id, allow_when_frozen,
                                                     silent)
                    break
                try:
                    result = await self._send_locked(
                        part, reply_to_message_id if index == 0 else None, chat_id,
                        on_attempt if index == 0 else None, return_message_id,
                        allow_when_frozen, address_peers=index == 0, thread_id=thread_id,
                        silent=silent,
                    )
                except TopicGone:
                    if not self.reroute_gone_topics:
                        raise
                    # The topic went away mid-reply: the rest goes to General, from this part
                    # to the last under General's own lock, so nothing interleaves there.
                    result = await self._send_rest_in_general(
                        parts[index:], index, chat_id, return_message_id, allow_when_frozen,
                        silent, gone_from=thread_id)
                    if index == 0:
                        first = result
                    break
                if index == 0:
                    first = result
                    self.last_first_message_id = self.last_sent_message_id
                if result is _SEND_SUPPRESSED:
                    break
            self._gone_topics.discard(topic)
            return first

    async def _send_rest_in_general(self, parts: List[str], start: int,
                                    chat_id: Optional[int], return_message_id: bool,
                                    allow_when_frozen: bool, silent: bool,
                                    gone_from: Optional[int] = None) -> Any:
        """Post ``parts`` (from index ``start`` of a reply) in General; ``gone_from`` names
        the topic for the note on the first of them."""
        destination = self.config.identity.home_channel_id if chat_id is None else chat_id

        async def rest() -> Any:
            first = None
            for offset, part in enumerate(parts):
                result = await self._send_locked(
                    part, None, chat_id, None, return_message_id, allow_when_frozen,
                    address_peers=start + offset == 0, thread_id=None, silent=silent,
                    note_for=gone_from if offset == 0 else None)
                if offset == 0:
                    first = result
                if result is _SEND_SUPPRESSED:
                    break
            return first
        if self._turn_slots is None:            # one lock for the bot, and it is held already
            return await rest()
        async with self._send_lock_for((destination, None)):
            return await rest()

    async def _send_locked(self, text: str, reply_to_message_id: Optional[int],
                           chat_id: Optional[int], on_attempt=None,
                           return_message_id: bool = False,
                           allow_when_frozen: bool = False, *, address_peers: bool = True,
                           thread_id: Optional[int] = None, silent: bool = False,
                           note_for: Optional[int] = None):
        if self._frozen and not allow_when_frozen:
            logger.info("outbound automation suppressed while transport is frozen")
            return _SEND_SUPPRESSED if return_message_id else None
        self.last_sent_message_id = None
        destination = self.config.identity.home_channel_id if chat_id is None else chat_id
        payload = self.format_outgoing(text)
        rendered, entities = self.addressed_payload(text) if address_peers else (text, [])
        logger.info("outbound to chat %s: %r", destination, payload[:120])
        await self._pace((destination, thread_id))
        if self._frozen and not allow_when_frozen:
            logger.info("outbound automation suppressed after pacing while transport is frozen")
            return _SEND_SUPPRESSED if return_message_id else None
        self._track_outbound_request(text)
        if on_attempt is not None:
            on_attempt()  # persisted immediately before the first possible network send
        if self._sender is not None:
            result = await _maybe_await(self._sender(payload, reply_to_message_id))
        elif self.bot is not None:
            try:
                if entities:
                    # Plain rendering: the entities address real characters in the message.
                    result = await self._send_with_retry(
                        rendered, reply_to_message_id, entities=entities,
                        chat_id=destination, thread_id=thread_id, silent=silent,
                        note_for=note_for)
                else:
                    alias = html.escape(self.config.identity.alias)
                    result = await self._send_with_retry(
                        f"<b>[{alias}]</b> {payload}", reply_to_message_id,
                        chat_id=destination, thread_id=thread_id, silent=silent,
                        note_for=note_for)
            except TopicGone:
                raise                           # send() finishes the reply in General
            except Exception as exc:
                self.stats.errors += 1
                where = f"chat {destination}" + (f" topic {thread_id}" if thread_id else "")
                self.stats.last_error = f"send to {where} failed: {exc}"
                raise
        else:
            raise RuntimeError("channel has no bot: call start() or inject a sender")
        self.stats.sent += 1
        self.last_sent_message_id = getattr(result, "message_id", None)
        logger.info("Telegram accepted outbound message id=%s", self.last_sent_message_id)
        return self.last_sent_message_id if return_message_id else payload

    async def _send_with_retry(self, payload: str, reply_to_message_id: Optional[int],
                               attempts: int = 3, entities: Optional[List[Dict[str, Any]]] = None,
                               chat_id: Optional[int] = None,
                               thread_id: Optional[int] = None, silent: bool = False,
                               note_for: Optional[int] = None) -> Any:
        """Send via aiogram, honouring TelegramRetryAfter instead of hammering the API.

        A topic that was closed or deleted since the turn started refuses the post; it then
        goes to the General topic with a note saying which topic it was meant for. With
        parallel topics ``send`` does that under General's lock (``TopicGone`` tells it to);
        with one lock for the bot it happens here. ``note_for`` posts in General with that
        note. ``attempts`` is kept for callers; flood waits are bounded by time instead.
        """
        destination = self.config.identity.home_channel_id if chat_id is None else chat_id
        plain = bool(entities)          # entity payloads are plain text, never HTML
        if note_for is not None:
            return await self._post_in_general(payload, entities, plain, destination,
                                               note_for, silent)
        if thread_id is None:
            return await self._post(payload, reply_to_message_id, entities, plain,
                                    destination, None, silent)
        try:
            return await self._post(payload, reply_to_message_id, entities, plain,
                                    destination, thread_id, silent)
        except TopicGone as gone:
            logger.warning("topic %s refused the post (%s)%s", thread_id, gone.__cause__,
                           "; posting in General" if self.reroute_gone_topics else "")
            self.stats.errors += 1      # not last_error: the reply still lands, no blocker
            self._gone_topics.add((destination, thread_id))
            if self._turn_slots is not None or not self.reroute_gone_topics:
                raise
        return await self._post_in_general(payload, entities, plain, destination, thread_id,
                                           silent)

    async def _post_in_general(self, payload: str, entities: Optional[List[Dict[str, Any]]],
                               plain: bool, destination: int, thread_id: int,
                               silent: bool) -> Any:
        """``payload`` in General with the note naming its topic: appended when it fits,
        otherwise posted on its own right after, so no part of the reply is cut."""
        # The topic's id, not its name: a name such as "@CodexBot deploy" would address a peer.
        note = f"(meant for topic #{thread_id}, which is closed or deleted)"
        if utf16_length(payload) + 2 + utf16_length(note) <= self.config.limits.max_message_chars:
            note = note if plain else html.escape(note)
            return await self._post(f"{payload}\n\n{note}", None, entities, plain,
                                    destination, None, silent)
        result = await self._post(payload, None, entities, plain, destination, None, silent)
        await self._pace((destination, None))
        if self._frozen:
            return result
        try:                            # the reply itself is out; the note is a courtesy
            await self._post(f"(the message above was {note[1:]}", None, None, True,
                             destination, None, silent)
        except Exception as exc:
            self.stats.errors += 1
            logger.warning("the note for topic %s did not post: %s", thread_id, exc)
        return result

    async def _post(self, payload: str, reply_to_message_id: Optional[int],
                    entities: Optional[List[Dict[str, Any]]], plain: bool, destination: int,
                    thread_id: Optional[int], silent: bool) -> Any:
        from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
        waited = 0.0
        while True:
            try:
                return await self.bot.send_message(
                    chat_id=destination,
                    text=payload,
                    parse_mode=None if plain else "HTML",
                    reply_to_message_id=reply_to_message_id,
                    disable_web_page_preview=True,
                    entities=entities or None,
                    message_thread_id=thread_id,
                    disable_notification=silent or None,
                )
            except TelegramBadRequest as exc:
                if thread_id is not None and _is_topic_gone(exc):
                    raise TopicGone() from exc
                raise
            except TelegramRetryAfter as exc:
                wait = min(float(exc.retry_after) + 1.0, 60.0)
                if waited + wait > FLOOD_WAIT_BUDGET_SECONDS:
                    raise RuntimeError("send failed repeatedly under Telegram flood "
                                       "control") from exc
                waited += wait
                # Telegram's flood wait covers the whole bot: hold the other topics too.
                self._flood_until = max(self._flood_until, self._clock() + wait)
                logger.warning("flood control: waiting %.1fs (%.0fs so far)", wait, waited)
                await self._sleep(wait)
                if self._turn_slots is not None:
                    await self._pace_group()    # the retry is a post too: gap and cap

    def online_notice(self) -> str:
        """Startup announcement, carrying the running revision for upgrade visibility."""
        role = f", role: {self._role_name()}" if self.config.identity.role else ""
        return format_message(
            Envelope(type=MessageType.HELLO, to="*", intent="ONLINE",
                     ref=self._alias_ref(), rev=_local_revision()),
            f"🟢 {self.config.identity.alias} online "
            f"(adapter: {self.config.adapter.type}{role}, rev: {_local_revision()})",
        )

    def _role(self) -> Any:
        try:
            return self.config.role()
        except Exception:                                  # noqa: BLE001 - check reports it
            return None

    def _role_brief(self) -> Optional[str]:
        """"<role>: <description>" for the turn card, or None without a readable role."""
        role = self._role() if self.config.identity.role else None
        return f"{role.name}: {role.description}" if role is not None else None

    def _role_name(self) -> str:
        role = self._role()
        return role.name if role is not None else f"{self.config.identity.role} (unreadable)"

    # ----------------------------------------------------------------------------------
    # aiogram wiring
    # ----------------------------------------------------------------------------------
    async def start(self) -> None:
        """Create the Bot/Dispatcher and start long polling."""
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties

        if not self.config.identity.token_is_present():
            raise RuntimeError("no bot token in config; set agent.bot_token or bot_token_env")
        self.bot = Bot(token=self.config.identity.bot_token,
                       default=DefaultBotProperties(parse_mode="HTML"))
        self.dispatcher = Dispatcher()
        self.dispatcher.message.register(self._on_aiogram_message)
        self._started_wall = time.time()
        me = await self.bot.get_me()
        logger.info("connected as @%s (alias %s) in chat %s",
                    me.username, self.config.identity.alias, self.config.identity.home_channel_id)
        # Issue #70: a dedicated MACHINE receipt line, so the upgrade gate does not
        # have to regex a human-facing payload. Gate 3 of upgrade_transport.sh used
        # to match a substring of the notice text, which is spoofable by
        # construction — the transport logs peer message text verbatim, so any line
        # quoting the notice satisfied it. That cost four selector rewrites (PR #61
        # rounds 4-7). This literal is emitted by exactly one call site, so the
        # selector can key on a fixed string instead of a guess at a format; a peer
        # can still echo it in message text, which is why the selector also drops
        # `inbound from` lines (see upgrade_transport.sh gate 3).
        #
        # Emitted AFTER `connected as @` (which already attests Telegram
        # reachability: get_me() returned) and BEFORE the notice send, because
        # send() can raise under flood control and a receipt that only appears when
        # the network cooperates would fail-closed on a healthy switch.
        # Additive, not a replacement (UPGRADE.md rule 7): the human-facing HELLO
        # notice below is unchanged, so old and new transports coexist during a
        # rolling upgrade. Full sha, and a direct logger.info (never the
        # payload[:120]-capped outbound path).
        logger.info("A2A_ONLINE_NOTICE rev=%s alias=%s chat=%s",
                    _local_revision_full(), self.config.identity.alias,
                    self.config.identity.home_channel_id)
        try:
            await self.send(self.online_notice(), silent=True)    # machine traffic: no sound
        except Exception as exc:
            if "chat not found" in str(exc).lower():
                raise RuntimeError(
                    f"@{me.username} cannot post in the home channel "
                    f"{self.config.identity.home_channel_id} (Telegram: chat not found): add the "
                    "bot to the group and make it an admin, or fix agent.home_channel_id "
                    "(swarmly-telegram discover lists the chats it sees)") from exc
            raise
        self.selftest.start(getattr(me, "can_read_all_group_messages", None))
        self.membership.lookup = self._group_member
        await self.membership.on_start()
        await self._restore_freeze_state()
        if self.replies is not None:
            self.replies.recover()
            self._reply_task = asyncio.create_task(self._reply_tick())
        if self._ack_task is None:
            self._ack_task = asyncio.create_task(self._ack_tick())
        if self._bus_task is None and self.config.fallback.gitbus_poll_minutes > 0 \
                and self.config.bus.enabled:
            self._bus_task = asyncio.create_task(self._bus_tick())
        if self._github_task is None and self.config.github and self.config.github.poll_minutes > 0:
            self._github_task = asyncio.create_task(self._github_tick())
        try:
            # A webhook left set by another deployment makes getUpdates fail with a conflict.
            # Pending updates are kept: a peer's request sent during a restart must still arrive.
            await self.bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:                              # noqa: BLE001 - polling still reports it
            logger.warning("could not clear a webhook before polling: %s", exc)
        await self.dispatcher.start_polling(self.bot)

    def request_restart(self) -> None:
        """Stop polling so the ``swarmly --cli`` launcher starts the transport again with
        the config it just rewrote (a new bus moves the transport's state and lock)."""
        self.restart_requested = True
        if self.dispatcher is not None:
            asyncio.ensure_future(self.dispatcher.stop_polling())

    async def _group_member(self, user_id: int) -> Optional[Tuple[str, str, bool]]:
        """``(status, username, is_bot)`` of a user in the home group (getChatMember)."""
        member = await self.bot.get_chat_member(chat_id=self.config.identity.home_channel_id,
                                                user_id=user_id)
        status = getattr(member, "status", "")
        user = getattr(member, "user", None)
        return (str(getattr(status, "value", status)), str(getattr(user, "username", "") or ""),
                bool(getattr(user, "is_bot", False)))

    async def stop(self) -> None:
        self._topic_memory.save()
        if self.bot is not None and getattr(self, "_started_wall", None) is not None \
                and not self.restart_requested:
            try:                                  # peers mark this agent offline
                await asyncio.wait_for(self.membership.on_stop(), timeout=5.0)
            except Exception as exc:              # noqa: BLE001 - stopping goes on
                logger.info("could not post the offline notice: %s", exc)
        self.membership.cancel()
        try:
            if self._reply_task is not None:
                self._reply_task.cancel()
                try:
                    await self._reply_task
                except asyncio.CancelledError:
                    pass
                self._reply_task = None
            if self._ack_task is not None:
                self._ack_task.cancel()
                self._ack_task = None
            if self._bus_task is not None:
                self._bus_task.cancel()
                self._bus_task = None
            if self._github_task is not None:
                self._github_task.cancel()
                self._github_task = None
            self.selftest.cancel()
            if self._lane_poll_task is not None:
                self._lane_poll_task.cancel()
                self._lane_poll_task = None
            if self.dispatcher is not None:
                try:
                    await self.dispatcher.stop_polling()
                except RuntimeError:
                    # Polling never started (start() failed first): the original error is
                    # the one to report, not aiogram's "Polling is not started".
                    pass
        finally:
            if self.bot is not None:
                await self.bot.session.close()
            await self.adapter.close()
            for pa in self._project_adapters.values():
                if pa is not self.adapter:
                    await pa.close()

    async def flush_replies(self) -> None:
        """Send explicitly submitted task answers and report overdue obligations."""
        if self._frozen or self.replies is None:
            return
        row = self.replies.claim_ready()
        if row:
            if self._frozen:
                self.replies.release_claim(row['id'])
                return
            try:
                telegram_message_id = await self.send(
                    row['text'], reply_to_message_id=row['message_id'], chat_id=row['chat_id'],
                    on_attempt=lambda: self.replies.attempting(row['id']),
                    return_message_id=True,
                )
                if telegram_message_id is _SEND_SUPPRESSED:
                    self.replies.release_claim(row['id'])
                    return
                self.replies.sent(row['id'], telegram_message_id)
            except BaseException:
                # Cancellation before on_attempt is safely retryable; after it, Telegram may
                # have accepted the message and automatic retry could duplicate it.
                if self.replies.get(row['id'])['state'] == 'claimed':
                    self.replies.release_claim(row['id'])
                else:
                    self.replies.uncertain(row['id'])
                raise
        overdue_rows = self.replies.overdue(alert_cap=self.config.limits.overdue_alert_cap)
        if not overdue_rows:
            return
        digest = self._select_digest(overdue_rows)
        if digest:
            if self._frozen:
                return
            try:
                sent = await self.send(self._digest_alert(digest), chat_id=digest[0]["chat_id"])
                if sent is None:
                    return
            except Exception:
                self.stats.errors += 1
                logger.exception("overdue digest alert failed")
                return
            for item in digest:
                self.replies.notified(item["id"])
            return
        for row in overdue_rows[:1]:
            if self._frozen:
                return
            tag = f"@{row['recipient']}: " if row['recipient'] else ""
            if row.get('alert_kind') == 'receipt_failure':
                detail = row['receipt_detail'] or 'receipt outcome is unknown; inspect Telegram before retry'
                self.stats.last_error = f"receipt ticket {row['id'][:8]}: unknown: {detail}"
                notice = format_message(Envelope(
                    type=MessageType.ALERT, to=self._alias_for_username(row['recipient']),
                    intent="RECEIPT_FAILED", ref=row['id'][:8], id=self._short_id(),
                ), (
                    f"⚠️ {tag}[{self.config.identity.alias}] receipt delivery failed for message "
                    f"{row['message_id']} (ticket {row['id'][:8]}): {detail}. "
                    "This receipt was not task completion; maintainer attention required."
                ))
                mark_notified = self.replies.receipt_notified
            else:
                self.stats.last_error = f"reply ticket {row['id'][:8]}: {row['state']}"
                notice = format_message(Envelope(
                    type=MessageType.ALERT, to=self._alias_for_username(row['recipient']),
                    intent="OVERDUE", ref=row['id'][:8], id=self._short_id(),
                ), (
                    f"⚠️ {tag}[{self.config.identity.alias}] {row['state']} for message "
                    f"{row['message_id']} (ticket {row['id'][:8]}). {row['detail']}. "
                    "Receipt was not completion; maintainer attention required."
                ))
                mark_notified = self.replies.notified
            try:
                try:
                    sent = await self.send(notice, reply_to_message_id=row['message_id'], chat_id=row['chat_id'])
                    if sent is None:
                        return
                except Exception as exc:
                    # Telegram rejects a stale reply_to target before delivering the notice.
                    # Retry that specific case unthreaded, in the original chat; other errors
                    # remain retryable but must not starve later overdue tickets.
                    if not _is_missing_reply_target(exc):
                        raise
                    logger.warning("reply target missing for ticket %s; sending unthreaded alert", row['id'][:8])
                    sent = await self.send(notice, reply_to_message_id=None, chat_id=row['chat_id'])
                    if sent is None:
                        return
                mark_notified(row['id'])
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats.errors += 1
                logger.exception("overdue reply alert failed for ticket %s", row['id'][:8])
            # One alert per tick keeps the ready-answer check at the top of the next pass.
            # A large backlog must never monopolize the outbox and delay a substantive reply.
            return

    async def _reply_tick(self) -> None:
        while True:
            try:
                await self.flush_replies()
            except Exception:
                self.stats.errors += 1
                logger.exception("reply outbox failed; ticket retained")
            await asyncio.sleep(1)

    def event_from_aiogram(self, message: Any) -> ChannelEvent:
        """Convert an aiogram Message into a ChannelEvent (the only aiogram-aware step)."""
        author = getattr(message, "from_user", None)
        reply_to = getattr(message, "reply_to_message", None)
        topic_name, topic_name_current = name_from_message(message)
        # In a forum topic Telegram makes every post a reply to the topic's opening service
        # message, written by whoever opened the topic: that is not a reply to this bot (live
        # test: the opener took a turn on every message in its topic).
        topic_root = reply_to is not None and (
            getattr(reply_to, "forum_topic_created", None) is not None
            or (getattr(message, "is_topic_message", False)
                and getattr(reply_to, "message_id", None) is not None
                and getattr(reply_to, "message_id", None)
                == getattr(message, "message_thread_id", None)))
        replies_to_bot = bool(
            reply_to is not None and self.bot is not None and not topic_root
            and getattr(getattr(reply_to, "from_user", None), "id", None) == getattr(self.bot, "id", None)
        )
        return ChannelEvent(
            text=getattr(message, "text", None) or getattr(message, "caption", None) or "",
            chat_id=int(getattr(getattr(message, "chat", None), "id", 0)),
            author_id=getattr(author, "id", None),
            author_username=getattr(author, "username", None),
            author_is_bot=bool(getattr(author, "is_bot", False)),
            message_id=getattr(message, "message_id", None),
            replies_to_bot=replies_to_bot,
            raw=message,
            thread_id=(getattr(message, "message_thread_id", None)
                       if getattr(message, "is_topic_message", False) else None),
            topic_name=topic_name,
            topic_name_current=topic_name_current,
            attachments=attachments_from_aiogram(message),
        )

    async def _on_aiogram_message(self, message: Any) -> None:
        # Pending updates are kept across a restart (a peer's request must still arrive), but
        # not a backlog from hours ago: a bot that was off all afternoon must not answer this
        # morning's messages when it starts (seen live: a 3.5-hour-old "Test" answered).
        date = getattr(message, "date", None)
        started = getattr(self, "_started_wall", None)
        if started is not None and date is not None and hasattr(date, "timestamp") \
                and date.timestamp() < started - _MAX_BACKLOG_SECONDS:
            logger.info("ignoring message %s from %s: sent %.0f min before this start",
                        getattr(message, "message_id", "?"), date.isoformat(),
                        (started - date.timestamp()) / 60)
            self.stats.ignored += 1
            return
        left = getattr(message, "left_chat_member", None)
        chat = getattr(getattr(message, "chat", None), "id", None)
        if left is not None and chat == self.config.identity.home_channel_id:
            await self.membership.member_left(getattr(left, "id", None),
                                              getattr(left, "username", None))
            return
        await self.handle_event(self.event_from_aiogram(message))


class TopicGone(Exception):
    """The forum topic was closed or deleted; the post goes to General instead."""


_TOPIC_GONE = ("topic_closed", "topic_deleted", "message thread not found",
               "topic_id_invalid")


def _is_topic_gone(exc: BaseException) -> bool:
    """Telegram's refusal of a post into a topic that is closed or no longer exists."""
    text = str(exc).lower()
    return any(marker in text for marker in _TOPIC_GONE)


def _topics_path(config: AgentConfig) -> Optional[Path]:
    """``topics.json`` beside the transport's other state; None when it has no home."""
    return _state_file(config, "topics")


def _state_file(config: AgentConfig, stem: str, suffix: Optional[str] = None) -> Optional[Path]:
    """``<stem>-<alias>[-<suffix>].json`` beside the transport's other state; None when it has no
    home (then that state lives in memory only)."""
    if config.source_path is None:
        return None
    try:
        from swarmly.telegram.poll import state_dir
        alias = re.sub(r"[^a-z0-9_.-]+", "_", config.identity.alias.lower()).strip("._") or "agent"
        suf = f"-{re.sub(r'[^a-z0-9_.-]+', '_', suffix.lower()).strip('._')}" if suffix else ""
        return state_dir(config) / f"{stem}-{alias}{suf}.json"
    except Exception as exc:                  # noqa: BLE001 - kept in memory only
        logger.warning("%s will not be kept across restarts: %s", stem, exc)
        return None


def _truncate_utf16(text: str, max_units: int) -> str:
    """Cut *text* to at most *max_units* UTF-16 units without splitting a surrogate pair.

    Telegram counts message length in UTF-16 units; slicing Python strings by characters
    silently disagrees the moment a non-BMP character is involved.
    """
    encoded = text.encode("utf-16-le")
    if len(encoded) // 2 <= max_units:
        return text
    return encoded[: max_units * 2].decode("utf-16-le", errors="ignore")


ATTACHMENTS_DIRNAME = ".a2a-attachments"
#: Telegram's limit for one bot posting in one group is about 20 a minute; parallel topics
#: share this cap, with headroom for network jitter and posts from other processes.
GROUP_MESSAGES_PER_MINUTE = 18
#: How long a bot left waiting by a person's multi-bot message expects the hand-off.
HANDOFF_NOTE_SECONDS = 1800.0
#: Progress a topic's bot-turn cap resets on (see ``_note_progress``).
_COMMIT_LINK = re.compile(r"github\.com/[\w.-]+/[\w.-]+/commit/([0-9a-f]{7,40})")
#: Run by the bot that handed work to a peer that never answered (check_stalled_handoffs).
REASSIGN_NOTE = ("{alias} has not answered your hand-off in this topic for {minutes} minutes and "
                 "has not said it is busy, so it may be down. Decide what happens to the work "
                 "now: hand it to another peer whose role fits, or do it yourself, and say so "
                 "here. If it can wait for {alias}, reply exactly NO_REPLY.")
#: Run after a topic turn hit the time limit in a resumable session.
CONTINUE_NOTE = ("Your previous turn in this topic hit the time limit and was stopped. Your "
                 "session still holds the work so far: continue the task from where it "
                 "stopped, in smaller steps, and post the result here.")
#: A commit named in prose ("pushed b0da422"): 7-40 hex characters, letters and digits both.
_BARE_SHA = re.compile(r"(?<![\w/#-])([0-9a-f]{7,40})(?![\w-])")
_PROGRESS_VERBS = re.compile(
    r"(?i)\b(merged|approved|qa\s+pass(?:ed)?|qa\s+fail(?:ed)?|changes\s+requested)\b")
#: Pauses before retrying a turn that failed on a passing provider error, then ❌.
TRANSIENT_RETRY_DELAYS = (30.0, 120.0)
#: Provider errors that pass by themselves. Quota and usage limits are not among them: they
#: last hours and have their own handling (auto-freeze).
_TRANSIENT = re.compile(
    r"(?i)at capacity|overloaded|temporarily unavailable|service unavailable|try again later|"
    r"\b(?:500|502|503|504|529)\b|internal server error|bad gateway|gateway timeout")
_NOT_TRANSIENT = re.compile(r"(?i)quota|usage limit|token plan|insufficient|billing")


def _transient_failure(error: str) -> bool:
    return bool(_TRANSIENT.search(error)) and not _NOT_TRANSIENT.search(error)


#: How often a bot may say that an anonymous admin's message to it was not acted on.
ANONYMOUS_NOTICE_SECONDS = 6 * 3600.0
#: Telegram asks for about one message a second per chat; parallel topics keep this gap.
GROUP_MIN_GAP_SECONDS = 1.0
_PACE_STATE_LIMIT = 256
#: How long one post may wait out Telegram's flood control (429s) before it gives up.
FLOOD_WAIT_BUDGET_SECONDS = 300.0
_TYPING_INTERVAL_SECONDS = 4.5


def attachments_from_aiogram(message: Any) -> List[Dict[str, Any]]:
    """Describe the files on an aiogram Message (largest photo size, document, voice, ...)."""
    found: List[Dict[str, Any]] = []
    photos = getattr(message, "photo", None) or []
    if photos:
        best = photos[-1]                                     # Telegram lists sizes small to large
        found.append({"file_id": best.file_id, "name": "photo.jpg", "mime": "image/jpeg",
                      "size": getattr(best, "file_size", None)})
    for kind, default_name in (("document", "file"), ("voice", "voice.ogg"),
                               ("audio", "audio"), ("video", "video.mp4")):
        item = getattr(message, kind, None)
        if item is None:
            continue
        found.append({"file_id": item.file_id,
                      "name": getattr(item, "file_name", None) or default_name,
                      "mime": getattr(item, "mime_type", None),
                      "size": getattr(item, "file_size", None)})
    return found


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return cleaned[-120:]


def _current_task_cancelling() -> bool:
    """True when the task running this code was itself cancelled (shutdown), not /stop."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)            # Python 3.11+
    return bool(cancelling()) if cancelling is not None else False


# A later part of a split message, as received: optional leading mentions, the sender's
# "[Alias]" banner, then the "↪ (k/n)" marker that split_outgoing puts on parts 2..n.
_CONTINUATION_RE = re.compile(r"^(?:@\w+\s+)*(?:\[[^\]\n]{1,64}\]\s*)?↪ \(\d+/\d+\)")


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _is_missing_reply_target(exc: Exception) -> bool:
    text = str(exc).lower()
    return "repl" in text and any(marker in text for marker in ("not found", "can't be found", "cannot be found"))
