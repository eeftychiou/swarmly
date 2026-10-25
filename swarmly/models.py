"""
Data models and canonical schemas for the Swarmly protocol.
Pure standard-library implementation compatible across all Python environments.
"""

from __future__ import annotations
import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class MessageIntent(str, Enum):
    STATUS_UPDATE = "STATUS_UPDATE"
    TASK_PROPOSE = "TASK_PROPOSE"
    TASK_PROPOSAL = "TASK_PROPOSE"
    TASK_CLAIM = "TASK_CLAIM"
    TASK_APPROVE = "TASK_APPROVE"
    TASK_HANDOFF = "TASK_HANDOFF"
    PR_REVIEW_REQUEST = "PR_REVIEW_REQUEST"
    PR_REVIEW_VERDICT = "PR_REVIEW_VERDICT"
    FALSIFICATION_REPORT = "FALSIFICATION_REPORT"
    EXPERIMENT_REGISTER = "EXPERIMENT_REGISTER"
    HEARTBEAT_PING = "HEARTBEAT_PING"
    HEARTBEAT_ACK = "HEARTBEAT_ACK"
    QUERY_REQUEST = "QUERY_REQUEST"
    QUERY_RESPONSE = "QUERY_RESPONSE"
    ESCALATE_BLOCKER = "ESCALATE_BLOCKER"


class MessagePriority(str, Enum):
    ROUTINE = "ROUTINE"
    HIGH = "HIGH"
    BLOCKING = "BLOCKING"


#: Review P0-7: the header fields that become a file name or a front-matter line.
MESSAGE_ID = re.compile(r"msg-[0-9]{8}T[0-9]{6}Z-[a-z0-9_]{4,16}")
AGENT_ID = re.compile(r"[a-z0-9_-]{2,32}")
#: C0, DEL, C1 (U+0085 NEL among them) and the Unicode line/paragraph separators: anything
#: ``str.splitlines`` or an editor could treat as a line break.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def header_problem(message: "A2AMessage") -> Optional[str]:
    """Why *message* must not be written to the bus, or None.

    ``message_id`` and the agent ids end up in the file name and every header field on a
    front-matter line, so a ``../`` or a newline would write outside the inbox or forge a
    ``sender:`` line.
    """
    if not MESSAGE_ID.fullmatch(str(message.message_id or "")):
        return f"message_id {message.message_id!r} is not msg-<YYYYMMDDTHHMMSSZ>-<token>"
    if not AGENT_ID.fullmatch(str(message.sender.agent_id or "")):
        return f"sender {message.sender.agent_id!r} is not an agent id ([a-z0-9_-]{{2,32}})"
    recipient = str(message.recipient.agent_id or "")
    if recipient != "*" and not AGENT_ID.fullmatch(recipient):
        return f"recipient {recipient!r} is neither '*' nor an agent id"
    for name, value in (("protocol", message.protocol),
                        ("correlation_id", message.correlation_id),
                        ("timestamp_utc", message.timestamp_utc),
                        ("model_family", message.sender.model_family),
                        ("framework", message.sender.framework),
                        ("timeout_utc", message.timeout_utc),
                        ("signature", message.signature)):
        if value is not None and _CONTROL.search(str(value)):
            return f"{name} contains a control character"
    return None


def bus_copy_problem(message: "A2AMessage") -> Optional[str]:
    """Why the bus file of *message* would not read back as *message*, or None.

    A quoted or padded header value, or a number where text belongs, passes the gateway's
    checks but reads back differently from the front matter, so its signature would then
    fail for every reader of the bus copy (review P1-3 follow-up)."""
    try:
        copy = A2AMessage.from_yaml_frontmatter(message.to_yaml_frontmatter())
        if copy.signed_fields() != message.signed_fields():
            return "a header field does not survive the bus file format"
    except ValueError as exc:
        return str(exc)
    if (copy.signature or "") != (message.signature or ""):
        return "the signature does not survive the bus file format"
    return None


@dataclass
class SenderInfo:
    agent_id: str
    model_family: str  # gemini, hermes, deepseek, minimax, openai, human, custom
    framework: str     # antigravity, autogen, crewai, langgraph, swarm, aider, cli_bus, custom

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SenderInfo:
        return cls(
            agent_id=str(data.get("agent_id", "unknown")),
            model_family=str(data.get("model_family", "custom")),
            framework=str(data.get("framework", "custom")),
        )


@dataclass
class RecipientInfo:
    agent_id: str  # target agent_id, or '*' for broadcast

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RecipientInfo:
        return cls(agent_id=str(data.get("agent_id", "*")))


@dataclass
class TaskClaimPayload:
    task_id: str
    topic: str
    branch: Optional[str] = None
    claimed_files: List[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskHandoffPayload:
    task_id: str
    git_commit_sha: str
    description: str
    pre_registered_hypothesis: str
    expected_bounds: List[float] = field(default_factory=list)
    verification_command: str = ""
    target_output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PRReviewRequestPayload:
    pr_number: int
    branch: str
    files_modified: List[str] = field(default_factory=list)
    review_focus: List[str] = field(default_factory=list)
    response_deadline_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PRReviewVerdictPayload:
    pr_number: int
    verdict: str  # APPROVED, CHANGES_REQUESTED, COMMENT
    summary: str
    blocking_issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FalsificationPayload:
    hypothesis: str
    predicted_metric: str
    measured_metric: str
    bounds_violated: bool
    verdict: str
    citation_scripts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class A2AMessage:
    protocol: str
    message_id: str
    timestamp_utc: str
    sender: SenderInfo
    recipient: RecipientInfo
    intent: MessageIntent
    priority: MessagePriority
    signature: str
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    timeout_utc: Optional[str] = None

    @classmethod
    def create(
        cls,
        sender_id: str,
        intent: Union[MessageIntent, str],
        payload: Union[Dict[str, Any], Any],
        recipient_id: str = "*",
        model_family: str = "custom",
        framework: str = "custom",
        priority: Union[MessagePriority, str] = MessagePriority.ROUTINE,
        correlation_id: Optional[str] = None,
        timeout_utc: Optional[str] = None,
        signature: str = "",
    ) -> A2AMessage:
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y%m%dT%H%M%SZ")
        iso_str = now.isoformat()
        
        # Keep the established timestamped message-id shape, but add a
        # per-message token so same-agent sends in one second cannot collide.
        # The token occupies the existing schema's [a-z0-9_]{4,16} segment;
        # sender identity remains available in the envelope's sender field.
        unique_token = uuid.uuid4().hex[:16]
        msg_id = f"msg-{ts_str}-{unique_token}"

        # Normalize intent and priority
        if isinstance(intent, str):
            intent = MessageIntent(intent)
        if isinstance(priority, str):
            priority = MessagePriority(priority)

        # Handle payload if it's a dataclass
        if hasattr(payload, "to_dict"):
            payload_dict = payload.to_dict()
        elif isinstance(payload, dict):
            payload_dict = payload
        else:
            payload_dict = {"content": str(payload)}

        return cls(
            protocol="a2a/v1.0",
            message_id=msg_id,
            timestamp_utc=iso_str,
            sender=SenderInfo(agent_id=sender_id, model_family=model_family, framework=framework),
            recipient=RecipientInfo(agent_id=recipient_id),
            intent=intent,
            priority=priority,
            signature=signature,
            payload=payload_dict,
            correlation_id=correlation_id,
            timeout_utc=timeout_utc,
        )

    def signed_fields(self) -> Dict[str, Any]:
        """What a v2 signature covers (review P1-3): every header field and the payload,
        in a form that survives the front-matter round trip (an empty field reads back
        as ``None`` or ``{}``, so those are normalised to ``""``). Any other non-string
        value raises ValueError: normalising it too would let a signed empty field be
        refilled with a number or a nested section and still verify."""
        def text(value: Any) -> str:
            if isinstance(value, str):
                return value
            if value is None or value == {}:
                return ""
            raise ValueError(f"header field {value!r} is not text")

        def name(value: Any) -> str:
            return value.value if isinstance(value, Enum) else text(value)

        return {
            "a2a_signature": 2,
            "protocol": text(self.protocol),
            "message_id": text(self.message_id),
            "correlation_id": text(self.correlation_id),
            "timestamp_utc": text(self.timestamp_utc),
            "sender": {"agent_id": text(self.sender.agent_id),
                       "model_family": text(self.sender.model_family),
                       "framework": text(self.sender.framework)},
            "recipient": text(self.recipient.agent_id),
            "intent": name(self.intent),
            "priority": name(self.priority),
            "timeout_utc": text(self.timeout_utc),
            "payload": self.payload,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "timestamp_utc": self.timestamp_utc,
            "sender": self.sender.to_dict(),
            "recipient": self.recipient.to_dict(),
            "intent": self.intent.value if isinstance(self.intent, Enum) else str(self.intent),
            "priority": self.priority.value if isinstance(self.priority, Enum) else str(self.priority),
            "timeout_utc": self.timeout_utc,
            "signature": self.signature,
            "payload": self.payload,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> A2AMessage:
        sender_data = data.get("sender", {})
        recipient_data = data.get("recipient", {})
        intent_raw = data.get("intent", "QUERY_REQUEST")
        priority_raw = data.get("priority", "ROUTINE")

        return cls(
            protocol=data.get("protocol", "a2a/v1.0"),
            message_id=data.get("message_id", ""),
            correlation_id=data.get("correlation_id"),
            timestamp_utc=data.get("timestamp_utc", datetime.now(timezone.utc).isoformat()),
            sender=SenderInfo.from_dict(sender_data),
            recipient=RecipientInfo.from_dict(recipient_data),
            intent=MessageIntent(intent_raw) if intent_raw in MessageIntent.__members__ else MessageIntent.QUERY_REQUEST,
            priority=MessagePriority(priority_raw) if priority_raw in MessagePriority.__members__ else MessagePriority.ROUTINE,
            timeout_utc=data.get("timeout_utc"),
            signature=data.get("signature", ""),
            payload=data.get("payload", {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> A2AMessage:
        return cls.from_dict(json.loads(json_str))

    def to_yaml_frontmatter(self, body_markdown: str = "") -> str:
        """Render standard YAML frontmatter for human-readable Git storage."""
        lines = [
            "---",
            f"protocol: {self.protocol}",
            f"message_id: {self.message_id}",
            f"correlation_id: {self.correlation_id or ''}",
            f"timestamp_utc: '{self.timestamp_utc}'",
            "sender:",
            f"  agent_id: {self.sender.agent_id}",
            f"  model_family: {self.sender.model_family}",
            f"  framework: {self.sender.framework}",
            "recipient:",
            f"  agent_id: {self.recipient.agent_id}",
            f"intent: {self.intent.value}",
            f"priority: {self.priority.value}",
            f"timeout_utc: {self.timeout_utc or ''}",
            f"signature: '{self.signature}'",
            f"payload_json: '{json.dumps(self.payload, sort_keys=True)}'",
            "---",
            "",
            body_markdown or self.payload.get("body") or f"# A2A Message: {self.intent.value}\n\n```json\n{json.dumps(self.payload, indent=2)}\n```",
        ]
        return "\n".join(lines)

    @classmethod
    def from_yaml_frontmatter(cls, content: str) -> A2AMessage:
        """Parse A2A message from YAML frontmatter markdown file."""
        # Review P0-7: lines end at "\n" only (never U+0085, U+2028 or a lone "\r"), and
        # the header ends at the first line that is exactly "---", so neither a separator
        # inside a value nor a "---" inside payload_json can end it early or add a field.
        lines = content.split("\n")
        if lines[0].rstrip("\r") != "---":
            raise ValueError("File does not contain YAML frontmatter (missing leading ---)")
        end = next((i for i, line in enumerate(lines) if i and line.rstrip("\r") == "---"), 0)
        if not end:
            raise ValueError("Malformed YAML frontmatter")

        data: Dict[str, Any] = {}

        # Simple zero-dependency YAML parser for key-values
        curr_section = None
        for line in lines[1:end]:
            line = line.rstrip("\r")
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            if line.startswith("  ") and curr_section:
                sub_k, _, sub_v = line_str.partition(":")
                data[curr_section][sub_k.strip()] = sub_v.strip().strip("'").strip('"')
            elif ":" in line_str:
                k, _, v = line_str.partition(":")
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if not v:
                    curr_section = k
                    data[k] = {}
                else:
                    curr_section = None
                    data[k] = v

        # Parse embedded payload_json
        payload_raw = data.get("payload_json", "{}")
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {"raw": payload_raw}
        data["payload"] = payload

        return cls.from_dict(data)
