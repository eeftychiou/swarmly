"""The models a bot's CLI can switch to, for ``/model`` with no name.

Claude Code's names are fixed aliases. Antigravity lists its engines with ``agy models`` and
Codex with ``codex app-server`` (JSON-RPC ``model/list``); both are asked once and the answer
is kept for a while, so a ``/model`` does not start a CLI each time. Reasonix keeps its
providers in its own ``reasonix.toml``, which swarmly does not read, so it has no list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("swarmly.telegram")

DISCOVERY_TIMEOUT = 20.0
#: How long a list is reused; a failed lookup is retried sooner.
LIST_TTL_SECONDS = 3600.0
FAILED_TTL_SECONDS = 120.0
#: At most this many names in one reply.
MAX_LISTED = 40

_CODEX_INPUT = (
    json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1,
                "params": {"clientInfo": {"name": "swarmly", "version": "1.0"}}}) + "\n"
    + json.dumps({"jsonrpc": "2.0", "method": "model/list", "id": 2, "params": {}}) + "\n")

Spawner = Callable[..., Any]
_cache: Dict[str, Tuple[float, Optional[List[str]]]] = {}


def static_models(adapter_type: str) -> Optional[List[str]]:
    """Names known without asking the CLI, or None."""
    if adapter_type.startswith("claude"):
        from swarmly.telegram.config import _CLAUDE_MODEL_ALIASES
        return list(_CLAUDE_MODEL_ALIASES)
    return None


def cached_models(adapter_type: str) -> Optional[List[str]]:
    """The list as last looked up (static names included), or None when there is none."""
    static = static_models(adapter_type)
    if static is not None:
        return static
    entry = _cache.get(_family(adapter_type))
    return entry[1] if entry else None


async def available_models(adapter_type: str, env: Optional[Dict[str, str]] = None,
                           spawn: Optional[Spawner] = None,
                           clock: Callable[[], float] = time.monotonic) -> Optional[List[str]]:
    """The models this adapter's CLI offers, or None when it cannot say (no list command,
    the CLI is missing, signed out or too slow)."""
    static = static_models(adapter_type)
    if static is not None:
        return static
    family = _family(adapter_type)
    if family not in ("antigravity", "codex"):
        return None
    entry = _cache.get(family)
    now = clock()
    if entry is not None:
        ttl = LIST_TTL_SECONDS if entry[1] else FAILED_TTL_SECONDS
        if now - entry[0] < ttl:
            return entry[1]
    spawn = spawn or _spawn
    try:
        if family == "antigravity":
            found = await _agy_models(spawn, env)
        else:
            found = await _codex_models(spawn, env)
    except Exception:                                  # noqa: BLE001 - no list then
        logger.warning("could not list the %s models", family, exc_info=True)
        found = []
    _cache[family] = (now, found or None)
    return found or None


def _family(adapter_type: str) -> str:
    if adapter_type.startswith("antigravity"):
        return "antigravity"
    return adapter_type


async def _spawn(argv: Sequence[str], env: Optional[Dict[str, str]], stdin: bool) -> Any:
    from swarmly.telegram.adapters.base import windows_argv
    exec_argv = windows_argv(argv) if os.name == "nt" else list(argv)
    return await asyncio.create_subprocess_exec(
        *exec_argv, env=env,
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)


async def _end(process: Any) -> None:
    if getattr(process, "returncode", 0) is None:
        with contextlib.suppress(OSError, ProcessLookupError):
            process.kill()
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=2)


async def _agy_models(spawn: Spawner, env: Optional[Dict[str, str]]) -> List[str]:
    """``agy models``: one engine name per line, each a valid ``--model`` value."""
    try:
        process = await spawn(["agy", "models"], env, False)
    except OSError:
        return []
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=DISCOVERY_TIMEOUT)
    except asyncio.TimeoutError:
        await _end(process)
        return []
    if process.returncode != 0:
        return []
    return parse_agy_models(stdout.decode("utf-8", errors="replace"))


def parse_agy_models(output: str) -> List[str]:
    names: List[str] = []
    for raw in output.splitlines():
        name = raw.strip()
        if not name:
            continue
        if name.startswith(("Usage:", "Flags:", "Available subcommands:", "Error")):
            return []                                  # a help banner: not a list
        names.append(name)
    return names


async def _codex_models(spawn: Spawner, env: Optional[Dict[str, str]]) -> List[str]:
    """``codex app-server``: the ids its ``model/list`` reply names."""
    try:
        process = await spawn(["codex", "app-server"], env, True)
    except OSError:
        return []
    lines: List[str] = []
    try:
        process.stdin.write(_CODEX_INPUT.encode())
        await process.stdin.drain()
        # stdin stays open until the answer is in: closing it can end the server first.

        async def read() -> None:
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace")
                lines.append(text)
                with contextlib.suppress(ValueError, AttributeError):
                    if json.loads(text).get("id") == 2:
                        return
        await asyncio.wait_for(read(), timeout=DISCOVERY_TIMEOUT)
    except (asyncio.TimeoutError, OSError):
        return []
    finally:
        with contextlib.suppress(Exception):
            process.stdin.close()
        await _end(process)
    return parse_codex_models("".join(lines))


def parse_codex_models(output: str) -> List[str]:
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("id") != 2:
            continue
        result = message.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        names = []
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict):
                name = entry.get("id") or entry.get("model")
                if isinstance(name, str) and name:
                    names.append(name)
        return names
    return []


def format_list(names: Sequence[str]) -> str:
    """The names one per line, at most MAX_LISTED of them."""
    shown = [f"• {name}" for name in names[:MAX_LISTED]]
    if len(names) > MAX_LISTED:
        shown.append(f"… and {len(names) - MAX_LISTED} more")
    return "\n".join(shown)
