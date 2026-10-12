"""A minimal MCP server (stdio, JSON-RPC 2.0) that exposes the governor as tools.

It has no SDK dependency. The identity is fixed when the server starts
(``swarmly --as codex mcp``), so a tool call cannot pick who it speaks for. Register it in
each CLI's MCP config; see docs/SWARM.md.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any, Dict, IO, Optional

from .. import __version__ as A2A_VERSION
from .envelope import EnvelopeError
from .governor import Governor, Rejected
from .lanes import LaneConflict
from .protocol import SwarmConfigError

log = logging.getLogger("swarmly.swarm.mcp")

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")

_STR = {"type": "string"}
_READ_ONLY = {"readOnlyHint": True}
TOOLS = [
    {"name": "a2a_next",
     "description": "What you owe the swarm and every move that is legal for you right now. "
                    "Call this first in every session and whenever unsure.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": _READ_ONLY},
    {"name": "a2a_post",
     "description": "Send one envelope through the governor. It is validated, signed and "
                    "appended to your lane; a rejection lists the reasons and the legal moves.",
     "inputSchema": {"type": "object", "required": ["intent"], "properties": {
         "intent": _STR, "task_id": _STR, "re": _STR, "context_id": _STR,
         "to": {"type": "array", "items": _STR},
         "body": {"type": "object"},
         "evidence": {"type": "array", "items": {
             "type": "object", "required": ["kind", "ref"],
             "properties": {"kind": _STR, "ref": _STR}}}}}},
    {"name": "a2a_inbox",
     "description": "Recent envelopes addressed to you or to everyone.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
     "annotations": _READ_ONLY},
    {"name": "a2a_tasks",
     "description": "Tasks and their states.",
     "inputSchema": {"type": "object", "properties": {"all": {"type": "boolean"}}},
     "annotations": _READ_ONLY},
    {"name": "a2a_escalations",
     "description": "What is waiting on the owner (safety list and stuck list).",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": _READ_ONLY},
    {"name": "a2a_guard",
     "description": "Check a shell command or file paths against the safety list before "
                    "running it. allowed=false means: do not run it; ask the owner with "
                    "REQUEST_ACTION.",
     "inputSchema": {"type": "object", "properties": {
         "command": _STR, "paths": {"type": "array", "items": _STR}}}},
    # Not read-only: a match records ACTION_TAKEN and spends the owner's one-shot approval.
]


class Server:
    def __init__(self, gov: Governor) -> None:
        self.gov = gov

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:
            return None  # a notification
        params = msg.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise _RpcError(-32602, "params must be an object")
            result = self._dispatch(method, params)
        except _RpcError as err:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": err.code, "message": err.message}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in SUPPORTED else SUPPORTED[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "a2a-swarm", "version": A2A_VERSION},
                    "instructions": f"You are {self.gov.agent} in the {self.gov.swarm.project} "
                                    "swarm. Call a2a_next first. Post only through a2a_post."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(-32602, "arguments must be an object")
            return self._call(str(params.get("name")), arguments)
        raise _RpcError(-32601, f"method not found: {method}")

    def _call(self, name: str, a: Dict[str, Any]) -> Dict[str, Any]:
        gov = self.gov
        log.info("tool call %s %s", name, json.dumps(a, ensure_ascii=False, default=str)[:2000])
        try:
            if name == "a2a_next":
                data: Any = gov.next()
            elif name == "a2a_post":
                env = gov.post(str(a.get("intent", "")).upper(), to=a.get("to"),
                               task_id=a.get("task_id"), body=a.get("body") or {},
                               evidence=a.get("evidence") or [], re=a.get("re"),
                               context_id=a.get("context_id"))
                data = {"ok": True, "envelope": env.to_dict(),
                        "next": gov.next()["obligations"]}
            elif name == "a2a_inbox":
                data = [e.to_dict() for e in gov.view().inbox(gov.agent,
                                                             int(a.get("limit") or 20))]
            elif name == "a2a_tasks":
                proj = gov.view()
                terminal = gov.swarm.protocol.terminal_states
                data = [t.to_dict() for t in proj.tasks.values()
                        if a.get("all") or t.state not in terminal]
            elif name == "a2a_escalations":
                data = gov.view().escalations()
            elif name == "a2a_guard":
                data = gov.guard(a.get("command"), a.get("paths") or []).to_dict()
            else:
                raise _RpcError(-32602, f"unknown tool {name}")
        except Rejected as rej:
            return _content(rej.to_dict(), error=True)
        except (SwarmConfigError, EnvelopeError, LaneConflict, ValueError) as exc:
            log.warning("tool %s failed: %s", name, exc)
            return _content({"ok": False, "errors": [str(exc)]}, error=True)
        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the server
            log.exception("tool %s crashed", name)
            return _content({"ok": False, "errors": [f"internal error: {exc}"]}, error=True)
        return _content(data)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _content(data: Any, *, error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False,
                                                            default=str)}],
            "isError": error}


def serve(gov: Governor, stdin: Optional[IO[str]] = None,
          stdout: Optional[IO[str]] = None) -> int:
    """Answer JSON-RPC lines until stdin closes. Always UTF-8, whatever the locale says
    (Windows pipes default to the ANSI code page)."""
    if stdin is None:
        stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    if stdout is None:
        stdout = sys.stdout
        reconfigure = getattr(stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")
    server = Server(gov)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.warning("MCP: unparseable line: %r", line[:500])
            reply: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600,
                                                        "message": "invalid request"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
