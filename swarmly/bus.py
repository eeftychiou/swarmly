"""
Durable Plane File and Git Bus Adapter for Swarmly.
Provides 100% auditable, persistent, zero-firewall message exchange.
"""

from __future__ import annotations
import base64
import binascii
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from swarmly.models import (A2AMessage, MessageIntent, MessagePriority, bus_copy_problem,
                        header_problem)
from swarmly.crypto import SIGNATURE_V2, CryptoAuth
from swarmly.transports import BusPushError, BusTransport, GitTransport

logger = logging.getLogger("swarmly.bus")

__all__ = ["BusPushError", "DurableBus", "MissingKeyError", "key_env_name"]

KEY_TYPES = ("hmac", "ed25519")


def key_env_name(agent_id: str) -> str:
    """``A2A_KEY_<AGENT>``: the variable holding ``agent_id``'s private key on its own host.
    Characters a shell variable cannot carry become ``_`` (``my-agent`` ->
    ``A2A_KEY_MY_AGENT``)."""
    return "A2A_KEY_" + re.sub(r"[^A-Z0-9_]", "_", agent_id.upper())


class MissingKeyError(KeyError):
    """No signing key is configured for an agent (fail closed: never sign with a guess)."""

    def __str__(self) -> str:                  # KeyError would quote the message
        return str(self.args[0]) if self.args else ""


def vouched_keys_path(root: Path) -> Path:
    """Where a host keeps the public keys the swarm vouched for (never committed)."""
    return Path(root) / ".agents" / "state" / "vouched-keys.json"


def vouch_key(root: Path, agent_id: str, public_key: str) -> bool:
    """Remember *agent_id*'s Ed25519 public key, as posted in its JOIN by a bot Telegram
    confirmed is an admin of the swarm's group. False when the key is not Ed25519-shaped."""
    if not _looks_ed25519_public(public_key):
        return False
    path = vouched_keys_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            vouched = json.load(f)
    except (OSError, ValueError):
        vouched = {}
    if not isinstance(vouched, dict):
        vouched = {}
    vouched[agent_id] = public_key
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(vouched, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return True


def unvouch_key(root: Path, agent_id: str) -> bool:
    """Forget *agent_id*'s vouched key (it left the swarm); True when there was one."""
    path = vouched_keys_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            vouched = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(vouched, dict) or agent_id not in vouched:
        return False
    del vouched[agent_id]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(vouched, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return True


def _looks_ed25519_public(value: str) -> bool:
    if len(value) != 44 or not value.endswith("="):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


#: Review P1-3: how old (or how far ahead) a v2 message's own signed timestamp may be. The
#: seen-ledger stops a replay inside the window; the window stops one after the ledger has
#: forgotten the id.
MAX_MESSAGE_AGE = timedelta(days=7)
MAX_CLOCK_AHEAD = timedelta(minutes=15)


def _outside_window(timestamp: object, now: Optional[datetime] = None) -> Optional[str]:
    """Why a signed timestamp is outside the accepted window, or None."""
    try:
        stamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return f"its timestamp {timestamp!r} is not a time"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if stamp < now - MAX_MESSAGE_AGE:
        return f"it is older than {MAX_MESSAGE_AGE.days} days"
    if stamp > now + MAX_CLOCK_AHEAD:
        return "its timestamp is in the future"
    return None


class DurableBus:
    """Signs, stores and verifies envelopes on a durable-plane transport.

    The transport decides where envelope files live (gitbus by default; see
    :mod:`swarmly.transports`). ``root_dir`` is still where the cluster config is found.
    """

    def __init__(self, root_dir: Optional[Path] = None, config_path: Optional[Path] = None,
                 transport: Optional[BusTransport] = None, create_dirs: bool = True):
        self.root = root_dir or Path.cwd()
        self.transport: BusTransport = transport or GitTransport(self.root)
        self.agents_dir = self.root / ".agents"
        self.inbox_dir = self.agents_dir / "inbox"
        self.outbox_dir = self.agents_dir / "outbox"
        self.blackboard_file = self.agents_dir / "BLACKBOARD.md"
        
        if config_path is not None and not Path(config_path).exists():
            # A registry named on purpose must not silently become another one.
            raise FileNotFoundError(f"agent registry {config_path} not found")
        self.config_path = config_path or (self.root / "config" / "a2a_config.json")
        if not self.config_path.exists():
            self.config_path = self.agents_dir / "A2A_CONFIG.json"

        self.crypto = CryptoAuth()
        self.config = self._load_config()
        # Issue #105: one shadow-visibility warning per agent per process.
        self._shadow_warned: set = set()

        # Ensure directories exist (gitbus only: other transports own their storage)
        if create_dirs and isinstance(self.transport, GitTransport):
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> Dict:
        """Load cluster config if available, with the keys the swarm vouched for on top."""
        config: Dict = {
            "authorized_agents": {},
            "revocation_list": [],
            "coordination": {"primary_master": "antigravity"}
        }
        if self.config_path and self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
        self._add_vouched_keys(config)
        return config

    def _add_vouched_keys(self, config: Dict) -> None:
        """Ed25519 keys this host's transport took from a JOIN in the group (see
        :func:`vouch_key`): they verify an agent that joined before this checkout pulled the
        registry commit that registers it. The registry always wins, and only agents it
        does not list are added."""
        agents = config.get("authorized_agents")
        if not isinstance(agents, dict):
            return
        try:
            with open(vouched_keys_path(self.root), "r", encoding="utf-8") as f:
                vouched = json.load(f)
        except (OSError, ValueError):
            return
        for agent_id, key in (vouched.items() if isinstance(vouched, dict) else ()):
            if agent_id not in agents and isinstance(key, str) and _looks_ed25519_public(key):
                agents[agent_id] = {"public_key_or_secret_name": key, "key_type": "ed25519",
                                    "role_scope": "worker", "vouched": "telegram"}

    def key_type_for(self, agent_id: str) -> str:
        """The registered key type of ``agent_id``: ``key_type`` in its config entry, else
        ``ed25519`` for an inline Ed25519 public key, else ``hmac``."""
        entry = (self.config.get("authorized_agents", {}) or {}).get(agent_id)
        if isinstance(entry, dict):
            declared = str(entry.get("key_type") or "").strip().lower()
            if declared:
                if declared not in KEY_TYPES:
                    raise ValueError(f"authorized_agents.{agent_id}.key_type must be one of "
                                     f"{', '.join(KEY_TYPES)} (got {declared!r})")
                return declared
            value = str(entry.get("public_key_or_secret_name", "") or "").strip()
            if _looks_ed25519_public(value):
                return "ed25519"
        return "hmac"

    def _configured_value(self, agent_id: str) -> Optional[str]:
        """The key ``config/a2a_config.json`` registers for ``agent_id``.

        For HMAC the value is always the *name* of an environment variable, whatever its
        case or characters, and an unset variable is no key at all: the name is never the
        secret, and a secret never sits in the (committed) config. Only an Ed25519 public
        key may be written inline, since it is public.
        """
        entry = (self.config.get("authorized_agents", {}) or {}).get(agent_id)
        if not isinstance(entry, dict):
            return None
        value = str(entry.get("public_key_or_secret_name", "") or "").strip()
        if not value:
            return None
        if self.key_type_for(agent_id) == "ed25519" and _looks_ed25519_public(value):
            return value
        return os.environ.get(value) or None

    def get_agent_key(self, agent_id: str) -> Optional[str]:
        """The key that verifies ``agent_id``'s signatures (an HMAC secret or an Ed25519
        public key), or None when none is configured."""
        if self.key_type_for(agent_id) == "ed25519":
            # The public key is registered in the config; A2A_KEY_<AGENT> holds the
            # private key on the agent's own host and never verifies anything.
            return self._configured_value(agent_id)
        env_key = key_env_name(agent_id)
        if os.environ.get(env_key):
            # Issue #105 (pin B of #103): precedence stays (env > .env > config) --
            # this is visibility only: once per agent per process, source named,
            # NEVER the value.
            self._warn_if_shadowing(agent_id, env_key, os.environ[env_key])
            return os.environ[env_key]
        return self._configured_value(agent_id)

    def verification_key(self, agent_id: str) -> Optional[Tuple[str, str]]:
        """``(key, key_type)`` that verifies ``agent_id``, or None.

        Only a registered agent verifies: an ambient ``A2A_KEY_<ID>`` without a registry
        entry is not enough. Two registered ids that share one variable name (``a-b`` and
        ``a_b``) verify neither, since either could sign as the other.
        """
        agents = self.config.get("authorized_agents", {}) or {}
        if not isinstance(agents.get(agent_id), dict):
            return None
        env_key = key_env_name(agent_id)
        twins = sorted(other for other in agents
                       if other != agent_id and key_env_name(str(other)) == env_key)
        if twins:
            logger.warning("bus: %r and %s share the key variable %s; neither is verified",
                           agent_id, ", ".join(repr(t) for t in twins), env_key)
            return None
        key = self.get_agent_key(agent_id)
        return (key, self.key_type_for(agent_id)) if key else None

    def signing_key(self, agent_id: str) -> Tuple[str, str]:
        """``(private key, key_type)`` this host signs as ``agent_id`` with.

        Raises :class:`MissingKeyError` when ``A2A_KEY_<AGENT>`` (or, for HMAC, the
        variable the config names) is unset: a message is never signed with a public
        string or a default.
        """
        key_type = self.key_type_for(agent_id)
        env_key = key_env_name(agent_id)
        key = os.environ.get(env_key) or None
        if key is None and key_type == "hmac":
            key = self._configured_value(agent_id)
        if not key:
            raise MissingKeyError(
                f"no signing key for {agent_id!r}: set {env_key} (create one with "
                f"`swarmly-keygen --agent {agent_id} --env-file <.env> --registry "
                f"config/a2a_config.json`)")
        return key, key_type

    def _warn_if_shadowing(self, agent_id: str, env_key: str, env_value: str) -> None:
        """Issue #105: an ambient ``A2A_KEY_*`` that SHADOWS the configured value
        is otherwise invisible in every log. Emit one line when the resolved value
        differs from the configured one; never log either value."""
        if agent_id in self._shadow_warned:
            return
        agents = self.config.get("authorized_agents", {}) or {}
        entry = agents.get(agent_id)
        if not isinstance(entry, dict):
            return
        config_value = self._configured_value(agent_id)
        if config_value and config_value != env_value:
            self._shadow_warned.add(agent_id)
            logger.info(
                "agent key for %s: using %s env override (config value differs)",
                agent_id, env_key)

    def post_message(
        self,
        message: A2AMessage,
        private_key: Optional[str] = None,
        key_type: str = "hmac",
        git_push: bool = False
    ) -> Path:
        """Sign and save an A2A message to the inbox."""
        # Revocation check
        revocation = self.config.get("revocation_list", [])
        if self.crypto.is_revoked(message.sender.agent_id, revocation):
            raise PermissionError(f"Agent '{message.sender.agent_id}' is on the cluster revocation list.")

        problem = header_problem(message)
        if problem:
            raise ValueError(f"not written to the bus: {problem}")

        # Sign if private key provided
        if private_key and not message.signature:
            # Review P1-3: sign the whole envelope, not the payload alone.
            message.signature = self.crypto.sign_envelope(message.signed_fields(), private_key,
                                                          key_type)
        problem = bus_copy_problem(message)
        if problem:
            raise ValueError(f"not written to the bus: {problem}")

        # The agent id goes in as it is (header_problem allows only [a-z0-9_-]): mapping
        # "-" to "_" gave agents a-b and a_b the same file name (P0-7 review m-2).
        safe_intent = message.intent.value.lower()
        filename = f"{message.message_id}_{message.sender.agent_id}_{safe_intent}.md"

        location = self.transport.post(
            filename, message.to_yaml_frontmatter(), publish=git_push,
            summary=(f"bus(a2a): {message.sender.agent_id} -> "
                     f"{message.recipient.agent_id} [{message.intent.value}]"))
        return Path(location)

    def verify_message(self, msg: A2AMessage) -> Tuple[bool, str]:
        """``(verified, status)`` for one message: revocation, then the registered key."""
        sender = msg.sender.agent_id
        if self.crypto.is_revoked(sender, self.config.get("revocation_list", [])):
            return False, f"Sender '{sender}' is revoked."
        try:
            # Inside the try: a bad key_type in the registry (ValueError) rejects this
            # sender's messages instead of aborting the whole read.
            registered = self.verification_key(sender)
            if not registered:
                return False, f"No registered public key for sender '{sender}'"
            signature = msg.signature if isinstance(msg.signature, str) else ""
            if signature.startswith(SIGNATURE_V2):
                valid = self.crypto.verify_envelope(msg.signed_fields(), signature, *registered)
                legacy = False
            elif isinstance(msg.payload, dict) and "a2a_signature" in msg.payload:
                # Domain separation: every v2 signature covers a dict with this key, so a
                # v1 payload carrying it could be a v2 envelope replayed as v1 with a
                # rewritten header and the "v2:" prefix dropped.
                return False, "v1 signature over a v2 envelope; refused"
            else:
                valid = self.crypto.verify_payload(msg.payload, signature, *registered)
                legacy = True
        except (RuntimeError, ValueError) as exc:
            return False, f"Signature not checked: {exc}"
        if not valid:
            return False, "Invalid signature: message tampered or wrong key"
        if legacy:
            # Review P1-3: a v1 signature covers the payload only, so the header (who it
            # is for, its id and time) can be rewritten. Readable, but not accepted unless
            # the registry opts in during a rollout.
            if self.config.get("accept_v1_signatures") is True:
                return True, "Valid v1 signature (payload only; accepted by the registry)"
            return False, ("v1 signature covers the payload only; not accepted "
                           "(sender must upgrade, or set accept_v1_signatures in the registry)")
        stale = _outside_window(msg.timestamp_utc)
        if stale:
            return False, f"Signature valid but {stale}"
        return True, "Valid signature"

    def read_inbox(self, verify_signatures: bool = True) -> List[Tuple[A2AMessage, bool, str]]:
        """Read all messages from inbox, returning (message, is_verified, verification_status)."""
        results = []
        revocation = self.config.get("revocation_list", [])

        for _name, content in self.transport.list_messages():
            try:
                msg = A2AMessage.from_yaml_frontmatter(content)

                if not verify_signatures:
                    if self.crypto.is_revoked(msg.sender.agent_id, revocation):
                        results.append((msg, False, f"Sender '{msg.sender.agent_id}' is revoked."))
                    else:
                        results.append((msg, True, "Unverified (signature check disabled)"))
                    continue
                results.append((msg, *self.verify_message(msg)))
            except Exception as e:
                # Malformed file
                pass

        return results

    def _git_commit_and_push(self, commit_msg: str) -> bool:
        """Commit and push ``.agents`` (gitbus only); kept for existing callers."""
        if isinstance(self.transport, GitTransport):
            return self.transport.commit_and_push(commit_msg)
        return GitTransport(self.root).commit_and_push(commit_msg)
