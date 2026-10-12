"""
Adapters for agent runtimes that can be driven from a command line.

``build_adapter`` is the single entry point: it reads ``adapter.type`` from the per-agent
config and returns a wired adapter. Adding a runtime means adding a class — the channel
itself never changes.

Built in: ``codex`` (OpenAI Codex CLI), ``codex_thread`` (queue into an existing Codex
session), ``claude`` (Claude Code), ``claude_thread`` (one persistent Claude Code session),
``hermes`` (Nous Research Hermes Agent),
``antigravity`` (Antigravity CLI), ``echo`` (in-process, for tests and dry runs), and
``custom`` (any command line with a ``{prompt}`` placeholder).
"""

from __future__ import annotations

import logging
from typing import Optional

from swarmly.telegram.adapters.antigravity import AntigravityAdapter, AntigravityThreadAdapter
from swarmly.telegram.adapters.base import (
    AdapterReply,
    AgentAdapter,
    IncomingMessage,
    Runner,
    SubprocessAdapter,
)
from swarmly.telegram.adapters.claude import ClaudeAdapter, ClaudeThreadAdapter
from swarmly.telegram.adapters.codex import CodexAdapter, CodexThreadAdapter
from swarmly.telegram.adapters.hermes import HermesAdapter
from swarmly.telegram.adapters.reasonix import ReasonixAdapter
from swarmly.telegram.config import AdapterSettings, AgentConfig, ConfigError
from swarmly.telegram.roles import RoleError

log = logging.getLogger(__name__)

#: What each adapter type runs, for the agent's own answer to "which model are you?".
_RUNTIME_NAMES = {
    "codex": "the OpenAI Codex CLI", "codex_thread": "the OpenAI Codex CLI",
    "claude": "Claude Code", "claude_thread": "Claude Code",
    "hermes": "Hermes Agent", "antigravity": "the Google Antigravity CLI (agy)",
    "antigravity_thread": "the Google Antigravity CLI (agy)",
    "reasonix": "the Reasonix CLI with DeepSeek models",
}

ADAPTER_TYPES = {
    "codex": CodexAdapter,
    "codex_thread": CodexThreadAdapter,
    "claude": ClaudeAdapter,
    "claude_thread": ClaudeThreadAdapter,
    "hermes": HermesAdapter,
    "antigravity": AntigravityAdapter,
    "antigravity_thread": AntigravityThreadAdapter,
    "reasonix": ReasonixAdapter,
}


class EchoAdapter(AgentAdapter):
    """Replies with what it received. Useful for bring-up and for tests."""

    TYPE = "echo"

    async def respond(self, message: IncomingMessage) -> Optional[AdapterReply]:
        if not message.text.strip():
            return None
        return AdapterReply(text=f"[{self.alias}] received: {message.text}")


def build_adapter(config: AgentConfig, runner: Optional[Runner] = None, *,
                  transport: bool = False) -> AgentAdapter:
    """Construct the adapter named by ``config.adapter.type``.

    ``transport=True`` arms transport-only safety on the built adapter (issue #123:
    the antigravity adapter refuses the blocking fallback so a bad endpoint fails
    fast instead of holding the turn lock). One-shot ``bus-poll`` and tests keep the
    default ``False`` -- the documented headless fallback must not change.
    """
    settings: AdapterSettings = config.adapter
    kind = (settings.type or "").lower()
    identity = config.identity.git_identity
    adapter: AgentAdapter
    if kind == "echo":
        adapter = EchoAdapter(config.identity.alias, settings, git_identity=identity)
    elif kind == "custom":
        if not settings.command:
            raise ConfigError("adapter.type 'custom' requires adapter.command with a {prompt} placeholder")
        adapter = SubprocessAdapter(config.identity.alias, settings, runner=runner,
                                    git_identity=identity)
    elif kind not in ADAPTER_TYPES:
        raise ConfigError(f"unknown adapter type '{kind}'; known: "
                          f"{sorted(set(ADAPTER_TYPES) | {'echo', 'custom'})}")
    else:
        adapter = ADAPTER_TYPES[kind](config.identity.alias, settings, runner=runner,
                                      git_identity=identity)
    role_of = getattr(config, "role", None)
    try:
        role = role_of() if callable(role_of) else None
    except RoleError as exc:
        # `check` validated the role at startup; a role file that broke since must not stop
        # every turn, so the agent runs without its role and says why in the log.
        log.warning("agent.role ignored for this turn: %s", exc)
        role = None
    if role is not None:
        adapter.role_text = role.prompt(getattr(config, "peer_roles", None))
        adapter.role_name = role.name
        if getattr(config, "source_path", None) is not None:
            try:
                from swarmly.telegram.poll import state_dir
                adapter.guard_dir = state_dir(config) / f"guard-{adapter.alias.lower()}"
            except Exception:                               # noqa: BLE001 - runs unguarded
                log.warning("role guard disabled: no state folder", exc_info=True)
    runtime = _RUNTIME_NAMES.get(kind)
    if runtime and not settings.command:
        # Seen live: a DeepSeek agent called itself "Claude-based" when asked its model.
        model = f", model {settings.model}" if settings.model else ""
        line = (f"YOUR RUNTIME: {runtime}{model}. When asked which model or tool you are, "
                "say this; do not guess.")
        adapter.role_text = f"{adapter.role_text}\n\n{line}" if adapter.role_text else line
    github = getattr(config, "github", None)
    if github is not None and github.token_env:
        adapter.github_token_env = github.token_env
    if transport:
        arm = getattr(adapter, "arm_transport_failfast", None)
        if callable(arm):
            arm()
    return adapter


__all__ = [
    "ADAPTER_TYPES",
    "AdapterReply",
    "AgentAdapter",
    "AntigravityAdapter",
    "AntigravityThreadAdapter",
    "ClaudeAdapter",
    "ClaudeThreadAdapter",
    "CodexAdapter",
    "CodexThreadAdapter",
    "EchoAdapter",
    "HermesAdapter",
    "IncomingMessage",
    "ReasonixAdapter",
    "Runner",
    "SubprocessAdapter",
    "build_adapter",
]
