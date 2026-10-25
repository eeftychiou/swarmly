"""

The Telegram home channel: agents talk to each other, and to the human, in one group.

    from swarmly.telegram import AgentConfig, AgentChannel

    config = AgentConfig.load("agents/hermes/config.yaml")
    channel = AgentChannel(config)          # adapter built from config.adapter.type
    await channel.start()                   # aiogram long polling

No shared secret, no gateway process, no WebSocket: the bot token is the identity, the group
is the medium, the alias is how everyone addresses you, and the admin id is who can override
everything. See ``docs/telegram-channel.md`` for setup and for each runtime's adapter.
"""

from swarmly.telegram.adapters import (
    AdapterReply,
    AgentAdapter,
    AntigravityAdapter,
    CodexAdapter,
    CodexThreadAdapter,
    EchoAdapter,
    HermesAdapter,
    IncomingMessage,
    build_adapter,
)
from swarmly.telegram.acks import AckTracker, Escalation
from swarmly.telegram.commands import diag_reply, parse_command, ping_reply, status_reply
from swarmly.telegram.channel import AgentChannel, ChannelEvent, ChannelStats
from swarmly.telegram.wire import (
    CHANNEL_BUDGET_CHARS,
    Envelope,
    MessageType,
    addressed,
    format_message,
    ignored_envelope_marker,
    is_within_budget,
    mention_entities,
    parse_message,
)
from swarmly.telegram.config import (
    AdapterSettings,
    AgentConfig,
    AgentIdentity,
    ChannelLimits,
    ConfigError,
)

__all__ = [
    "AckTracker",
    "AdapterReply",
    "AdapterSettings",
    "AgentAdapter",
    "AgentChannel",
    "AgentConfig",
    "AgentIdentity",
    "AntigravityAdapter",
    "CHANNEL_BUDGET_CHARS",
    "ChannelEvent",
    "ChannelLimits",
    "ChannelStats",
    "CodexAdapter",
    "CodexThreadAdapter",
    "ConfigError",
    "EchoAdapter",
    "Envelope",
    "Escalation",
    "HermesAdapter",
    "IncomingMessage",
    "MessageType",
    "addressed",
    "build_adapter",
    "diag_reply",
    "format_message",
    "ignored_envelope_marker",
    "is_within_budget",
    "mention_entities",
    "parse_command",
    "parse_message",
    "ping_reply",
    "status_reply",
]
