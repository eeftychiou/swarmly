"""Envelope v2: one signed, hash-chained record in one agent's lane.

The signature covers the canonical JSON of the whole envelope (every field except
``sig``), so nothing can be changed after signing. ``prev`` is the SHA-256 of the
previous envelope in the same lane (including its signature), which makes each lane a
tamper-evident chain. There is no unsigned or HMAC fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import keys

PROTOCOL = "a2a/2"
GENESIS = "0" * 64
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: An envelope id, ``<lane>:<seq>``: what ``re`` must look like.
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}:[1-9][0-9]{0,11}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
FIELDS = ("protocol", "id", "lane", "seq", "prev", "seen", "ts", "from", "to", "intent",
          "context_id", "task_id", "epoch", "re", "body", "evidence")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class EnvelopeError(ValueError):
    """The envelope is malformed, or its signature or chain does not check out."""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(ts: str) -> datetime:
    if not isinstance(ts, str) or not TS_RE.match(ts):
        raise ValueError(f"ts must be YYYY-MM-DDTHH:MM:SS.ffffffZ, got {ts!r}")
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


@dataclass
class Envelope:
    lane: str
    seq: int
    prev: str
    ts: str
    sender: str
    to: List[str]
    intent: str
    context_id: str
    task_id: Optional[str] = None
    epoch: Optional[int] = None
    re: Optional[str] = None
    body: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, str]] = field(default_factory=list)
    seen: Dict[str, int] = field(default_factory=dict)
    sig: Optional[str] = None

    @property
    def id(self) -> str:
        return f"{self.lane}:{self.seq}"

    def unsigned(self) -> Dict[str, Any]:
        return {
            "protocol": PROTOCOL, "id": self.id, "lane": self.lane, "seq": self.seq,
            "prev": self.prev, "seen": dict(self.seen), "ts": self.ts, "from": self.sender,
            "to": list(self.to),
            "intent": self.intent, "context_id": self.context_id, "task_id": self.task_id,
            "epoch": self.epoch, "re": self.re, "body": self.body,
            "evidence": self.evidence,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.unsigned()
        data["sig"] = {"alg": "ed25519", "value": self.sig}
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def digest(self) -> str:
        """SHA-256 of the signed envelope; the next envelope's ``prev``."""
        return hashlib.sha256(canonical(self.to_dict())).hexdigest()

    def sign_with(self, priv: Any) -> "Envelope":
        self.sig = keys.sign(priv, canonical(self.unsigned()))
        return self

    def verify(self, public_b64: Optional[str]) -> bool:
        sig = self.sig
        if not sig:
            return False
        return keys.verify(public_b64, canonical(self.unsigned()), sig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Envelope":
        if not isinstance(data, dict):
            raise EnvelopeError("envelope must be a JSON object")
        if data.get("protocol") != PROTOCOL:
            raise EnvelopeError(f"not an {PROTOCOL} envelope (protocol={data.get('protocol')!r})")
        extra = set(data) - set(FIELDS) - {"sig"}
        if extra:
            raise EnvelopeError(f"unknown fields: {sorted(extra)}")
        missing = [f for f in FIELDS if f not in data]
        if missing:
            raise EnvelopeError(f"missing fields: {missing}")
        sig = data.get("sig")
        if not isinstance(sig, dict) or sig.get("alg") != "ed25519" or not sig.get("value"):
            raise EnvelopeError("missing or non-Ed25519 signature")
        env = cls(lane=data["lane"], seq=data["seq"], prev=data["prev"], ts=data["ts"],
                  sender=data["from"], to=data["to"], intent=data["intent"],
                  context_id=data["context_id"], task_id=data["task_id"],
                  epoch=data["epoch"], re=data["re"], body=data["body"],
                  evidence=data["evidence"], seen=data["seen"], sig=sig["value"])
        env.check_shape()
        if data["id"] != env.id:
            raise EnvelopeError(f"id {data['id']!r} does not match lane:seq {env.id!r}")
        return env

    @classmethod
    def from_json(cls, text: str) -> "Envelope":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EnvelopeError(f"not JSON: {exc}") from None
        return cls.from_dict(data)

    def check_shape(self) -> None:
        """Type checks that do not need the protocol or the swarm."""
        if not isinstance(self.lane, str) or not isinstance(self.sender, str):
            raise EnvelopeError("lane and from must be strings")
        if self.lane != self.sender:
            raise EnvelopeError(f"lane {self.lane!r} must equal from {self.sender!r}")
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 1:
            raise EnvelopeError("seq must be a positive integer")
        if not isinstance(self.prev, str) or not re.match(r"^[0-9a-f]{64}$", self.prev):
            raise EnvelopeError("prev must be a SHA-256 hex digest")
        try:
            parse_ts(self.ts)
        except (TypeError, ValueError):
            raise EnvelopeError(f"bad ts {self.ts!r}") from None
        if not isinstance(self.to, list) or not self.to or \
                not all(isinstance(t, str) and t for t in self.to):
            raise EnvelopeError("to must be a non-empty list of agent names or '*'")
        if not isinstance(self.intent, str) or not self.intent:
            raise EnvelopeError("intent must be a string")
        if not isinstance(self.context_id, str) or not self.context_id:
            raise EnvelopeError("context_id must be a non-empty string")
        if self.task_id is not None and (not isinstance(self.task_id, str)
                                         or not TASK_RE.match(self.task_id)):
            raise EnvelopeError(f"bad task_id {self.task_id!r}")
        if self.epoch is not None and (not isinstance(self.epoch, int)
                                       or isinstance(self.epoch, bool) or self.epoch < 0):
            raise EnvelopeError("epoch must be a non-negative integer or null")
        # Type only: replay must keep accepting what was valid when it was written. The
        # governor checks the lane:seq form on new posts (Governor.check).
        if self.re is not None and not isinstance(self.re, str):
            raise EnvelopeError("re must be an envelope id or null")
        if not isinstance(self.seen, dict) or not all(
                isinstance(k, str) and k != self.lane and isinstance(v, int)
                and not isinstance(v, bool) and v >= 1 for k, v in self.seen.items()):
            raise EnvelopeError("seen must map other lanes to the seq last seen in each")
        if not isinstance(self.body, dict):
            raise EnvelopeError("body must be an object")
        if not isinstance(self.evidence, list) or not all(
                isinstance(e, dict) and isinstance(e.get("kind"), str)
                and isinstance(e.get("ref"), str) and e["ref"] for e in self.evidence):
            raise EnvelopeError("evidence must be a list of {kind, ref} objects")
        try:
            canonical(self.unsigned())
        except (TypeError, ValueError) as exc:
            raise EnvelopeError(f"not canonical JSON: {exc}") from None


def parse_evidence(item: str, kinds: List[str]) -> Dict[str, str]:
    """``kind:ref`` from the command line, e.g. ``ci:https://...`` or ``commit:abc123``.

    A bare URL becomes ``url`` evidence and anything else without a known kind becomes
    ``text``.
    """
    kind, sep, ref = item.partition(":")
    if sep and ref and kind in kinds:
        return {"kind": kind, "ref": ref}
    if item.startswith(("http://", "https://")):
        return {"kind": "url", "ref": item}
    return {"kind": "text", "ref": item}
