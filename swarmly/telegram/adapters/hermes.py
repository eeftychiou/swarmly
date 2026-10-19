"""
Adapter for **Nous Research Hermes Agent** (`hermes`).

Verified surface (``hermes --help`` on the project host, 2026-09-16):

* ``hermes -z "<prompt>"`` / ``--oneshot`` — one-shot mode: sends a single prompt and prints
  **only the final response text** to stdout — no banner, no spinner, no tool previews, no
  session_id line. Tools, memory, rules and the CWD's ``AGENTS.md`` load as normal and
  approvals are auto-bypassed. This is the intended scripting/pipe entry point.
* ``--usage-file PATH`` — after the run, writes a JSON usage report (cost, tokens, model).
* ``-m/--model``, ``--provider``, ``--reasoning LEVEL``, ``-t TOOLSETS`` — per-run overrides.
* profiles are selected through the environment (``HERMES_PROFILE``), not a CLI flag.

Hermes is also itself a Telegram agent: ``hermes send`` posts to a configured platform, and
``hermes peer`` does bot-to-bot DMs between Hermes gateways across machines. Those are for
*this* machine's gateway; the channel adapter below is what lets Hermes take part in one
shared group alongside Codex and Antigravity.

Default command::

    hermes -z {prompt}
"""

from __future__ import annotations

from typing import Dict, Optional

from swarmly.telegram.adapters.base import (
    SubprocessAdapter,
    discussion_line,
    format_channel_prompt,
    project_line,
    turn_card,
)
from swarmly.telegram.config import AdapterSettings


class HermesAdapter(SubprocessAdapter):
    WANTS_TOPIC_RECAP = True                  # one-shot per message
    """Drive Hermes Agent in one-shot mode, one process per incoming message."""

    TYPE = "hermes"

    @classmethod
    def default_settings(cls, cwd: Optional[str] = None, model: Optional[str] = None,
                         profile: Optional[str] = None, timeout_seconds: int = 900,
                         reasoning: Optional[str] = None) -> AdapterSettings:
        parts = ["hermes", "-z", "{prompt}"]
        if model:
            parts += ["-m", model]
        if reasoning:
            parts += ["--reasoning", reasoning]
        extra_env: Dict[str, str] = {}
        if profile:
            extra_env["HERMES_PROFILE"] = profile
        return AdapterSettings(type=cls.TYPE, cwd=cwd, timeout_seconds=timeout_seconds,
                               command=" ".join(parts), extra_env=extra_env)

    def build_prompt(self, message) -> Optional[str]:
        text = (message.text or "").strip()
        if not text:
            return None
        return format_channel_prompt(
            message.author_alias,
            text,
            author_username=getattr(message, "author_username", None),
            peer_username=getattr(message, "peer_username", None),
            mentions=getattr(message, "mentions", None),
            maintainer=getattr(message, "maintainer_username", None),
            discussion=discussion_line(message),
            project=project_line(message),
            role=self.role_text,
            card=turn_card(message),
            reply_chars=getattr(message, "reply_chars", None),
        )
