"""The doorbell: a short Telegram ping after the governor appends an envelope.

The lanes are the record; the ping only says where to look. It carries the envelope id,
the intent, the task and the @mentions of whoever has to act, never the body, so a
leaked chat history leaks nothing a lane reader could not already see and an agent
cannot be steered by chat text. A ping that fails is logged and dropped: the envelope is
already on the lane, and every agent's bus poller will pick it up on its own.

Configured in swarm.yaml::

    doorbell:
      telegram:
        chat_id: -1001234567890            # the shared group
        token_env: A2A_TELEGRAM_BOT_TOKEN_{AGENT}   # {AGENT} is the upper-cased sender
        quiet: [HEARTBEAT]                 # intents that never ring
    agents:
      codex:
        vendor: openai
        telegram: "@codex_bot"             # who gets mentioned when codex has to act

``A2A_DOORBELL=off`` silences it for one process (tests, dry runs).

The ping is a v1 ``NOTE intent=FYI`` header with ``bus=lanes:<id>``. Peer transports
record it as machine traffic, so it never starts a model turn; a transport whose agent
reads the lanes (``bus.swarm``) takes it as the cue to run its lane poll now instead of
waiting for the next scheduled cycle.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from swarmly.telegram.wire import Envelope as WireEnvelope
from swarmly.telegram.wire import MessageType, format_message, mention_entities

from .envelope import ID_RE, TASK_RE, Envelope
from .protocol import Swarm

log = logging.getLogger("swarmly.swarm.doorbell")

DEFAULT_TOKEN_ENV = "A2A_TELEGRAM_BOT_TOKEN_{AGENT}"
DEFAULT_QUIET = ("HEARTBEAT",)
#: Intents that always mention the owner, because only the owner can answer them.
OWNER_INTENTS = ("ESCALATE", "REQUEST_ACTION")
TIMEOUT_SECONDS = 5.0
API = "https://api.telegram.org"
#: The bus= prefix a transport keys on to start a lane poll.
BUS_PREFIX = "lanes:"
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
#: After a failed or timed-out ping, stay quiet this long (Telegram may be down).
PAUSE_SECONDS = 300.0

Sender = Callable[[str, Dict[str, Any], float], Any]


def _urllib_send(url: str, payload: Dict[str, Any], timeout: float) -> Any:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed host
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # Telegram answers a refusal with 4xx and a JSON reason; keep the reason.
        try:
            return json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            return {"ok": False, "description": f"HTTP {exc.code}"}


def _handle(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text if text.startswith("@") else f"@{text}"


class TelegramDoorbell:
    """Rings the shared group for every envelope the governor appends."""

    def __init__(self, swarm: Swarm, agent: str, *, chat_id: str, token: str,
                 handles: Dict[str, str], quiet: List[str],
                 send: Optional[Sender] = None, timeout: float = TIMEOUT_SECONDS) -> None:
        self.swarm = swarm
        self.agent = agent
        self.chat_id = chat_id
        self.token = token
        self.handles = handles
        self.quiet = {q.upper() for q in quiet}
        self.send = send or _urllib_send
        self.timeout = timeout
        self.paused_until = 0.0
        self.clock = time.monotonic

    def mentions(self, env: Envelope) -> List[str]:
        """The handles to @mention: roster handles only, never text from the envelope.

        The owner is mentioned only for the intents only the owner can answer, so routine
        traffic addressed to the owner never pages them.
        """
        names: List[str] = []
        if "*" not in env.to:
            names.extend(env.to)
        if env.body.get("assignee"):
            names.append(str(env.body["assignee"]))
        owner_needed = env.intent in OWNER_INTENTS
        if owner_needed:
            names.append(self.swarm.owner)
        out: List[str] = []
        for name in names:
            if name == self.swarm.owner and not owner_needed:
                continue
            handle = self.handles.get(name)
            if name != env.sender and handle and handle not in out:
                out.append(handle)
        return out

    def text(self, env: Envelope) -> str:
        header = WireEnvelope(type=MessageType.NOTE, to="*", intent="FYI", ref=env.id,
                              bus=f"{BUS_PREFIX}{env.id}")
        return format_message(header, self._pointer(env))

    def entities(self, text: str, env: Envelope) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for handle in self.mentions(env):
            found.extend(mention_entities(text, handle))
        return sorted(found, key=lambda e: e["offset"])

    def _pointer(self, env: Envelope) -> str:
        # Only fields the envelope check has constrained: the intent is a protocol name,
        # the task and re are id-shaped, so nothing here can carry a mention or a link.
        parts = [f"\U0001f514 {env.id} {env.intent}"]
        if env.task_id and TASK_RE.match(env.task_id):
            parts.append(f"task={env.task_id}")
        if env.re and ID_RE.match(env.re):
            parts.append(f"re={env.re}")
        who = self.mentions(env)
        if who:
            parts.append("→ " + " ".join(who))
        elif "*" in env.to:
            parts.append("→ all")
        return " ".join(parts) + "\nRead it with a2a_inbox (or `swarmly inbox`)."

    def _redact(self, text: Any) -> str:
        return str(text).replace(self.token, "<token>") if self.token else str(text)

    def __call__(self, env: Envelope) -> bool:
        """Ring once, waiting at most ``timeout`` seconds in all (DNS included).

        Never raises; returns whether Telegram accepted the ping in time.
        """
        if env.intent.upper() in self.quiet:
            return False
        if self.clock() < self.paused_until and env.intent not in OWNER_INTENTS:
            # Owner pages are never dropped by the pause: they are rare and they matter.
            log.info("doorbell paused after a failure; %s not rung", env.id)
            return False
        box: Dict[str, Any] = {}

        def run() -> None:
            try:
                box["ok"] = self._send_now(env)
            except Exception as exc:                   # noqa: BLE001 - logged by caller
                box["error"] = exc
        worker = threading.Thread(target=run, name=f"doorbell-{env.id}", daemon=True)
        worker.start()
        worker.join(self.timeout + 1.0)
        if worker.is_alive():
            log.warning("doorbell for %s gave up after %.0fs; pausing rings for %.0fs",
                        env.id, self.timeout + 1.0, PAUSE_SECONDS)
            self.paused_until = self.clock() + PAUSE_SECONDS
            return False
        if "error" in box:
            log.warning("doorbell for %s failed: %s; pausing rings for %.0fs", env.id,
                        self._redact(box["error"]), PAUSE_SECONDS)
            self.paused_until = self.clock() + PAUSE_SECONDS
            return False
        return bool(box.get("ok"))

    def _send_now(self, env: Envelope) -> bool:
        text = self.text(env)
        payload: Dict[str, Any] = {"chat_id": self.chat_id, "text": text,
                                   "disable_web_page_preview": "true"}
        if env.intent not in OWNER_INTENTS:
            payload["disable_notification"] = "true"     # routine rings are silent
        entities = self.entities(text, env)
        if entities:
            payload["entities"] = json.dumps(entities)
        result = self.send(f"{API}/bot{self.token}/sendMessage", payload, self.timeout)
        if isinstance(result, dict) and result.get("ok") is False:
            retry = (result.get("parameters") or {}).get("retry_after")
            pause = float(retry) if isinstance(retry, (int, float)) else 0.0
            if pause > 0:
                self.paused_until = self.clock() + min(pause, PAUSE_SECONDS)
            log.warning("doorbell for %s refused by Telegram: %s%s", env.id,
                        self._redact(result.get("description")),
                        f" (quiet for {pause:.0f}s)" if pause > 0 else "")
            return False
        log.info("doorbell rang for %s %s", env.id, env.intent)
        return True


def doorbell_from_swarm(swarm: Swarm, agent: str, *,
                        send: Optional[Sender] = None) -> Optional[TelegramDoorbell]:
    """Build the doorbell for ``agent`` from swarm.yaml, or None when it is not set up."""
    if os.environ.get("A2A_DOORBELL", "").strip().lower() in ("off", "0", "false", "no"):
        return None
    conf = (swarm.doorbell or {}).get("telegram") or {}
    if not isinstance(conf, dict) or not conf.get("chat_id"):
        return None
    token_env = str(conf.get("token_env") or DEFAULT_TOKEN_ENV).replace(
        "{AGENT}", agent.upper())
    token = os.environ.get(token_env, "").strip()
    if not token:
        log.warning("doorbell is configured but %s is not set; %s will not ring",
                    token_env, agent)
        return None
    if not TOKEN_RE.match(token):
        log.warning("%s does not look like a Telegram bot token; %s will not ring",
                    token_env, agent)
        return None
    handles = {name: h for name, a in swarm.agents.items()
               if (h := _handle(a.telegram)) is not None}
    quiet = conf.get("quiet")
    return TelegramDoorbell(swarm, agent, chat_id=str(conf["chat_id"]), token=token,
                            handles=handles,
                            quiet=[str(q) for q in (quiet if quiet is not None
                                                    else DEFAULT_QUIET)],
                            send=send)
