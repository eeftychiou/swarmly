"""The models a bot's CLI can switch to, for ``/model`` with no name.

Each CLI says it differently, so each has its own source (checked live on the CLIs):

- Antigravity: ``agy models`` prints ``<id>\\t<engine name>`` per line.
- Codex: the CLI keeps the list it fetched in ``~/.codex/models_cache.json`` (``$CODEX_HOME``
  when set); models marked ``visibility: hide`` are left out, as in its own picker.
- Reasonix: ``reasonix doctor --json`` lists each configured provider and its models.
- Claude Code has no list command: its aliases, or any claude-* name.

A CLI is asked once and its answer kept for a while, so a ``/model`` does not start it each
time; the lookup runs without blocking the transport (``agy models`` takes a few seconds).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("swarmly.telegram")

DISCOVERY_TIMEOUT = 20.0
#: How long a list is reused; a failed lookup is retried sooner.
LIST_TTL_SECONDS = 3600.0
FAILED_TTL_SECONDS = 120.0
#: At most this many names in one reply.
MAX_LISTED = 40

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
                           clock: Callable[[], float] = time.monotonic,
                           codex_home: Optional[Path] = None) -> Optional[List[str]]:
    """The models this adapter's CLI offers, or None when it cannot say (the CLI is missing,
    signed out or too slow, or its list is unreadable)."""
    static = static_models(adapter_type)
    if static is not None:
        return static
    family = _family(adapter_type)
    if family not in ("antigravity", "codex", "reasonix"):
        return None
    entry = _cache.get(family)
    now = clock()
    if entry is not None:
        ttl = LIST_TTL_SECONDS if entry[1] else FAILED_TTL_SECONDS
        if now - entry[0] < ttl:
            return entry[1]
    spawn = spawn or _spawn
    found: List[str]
    try:
        if family == "antigravity":
            found = parse_agy_models(await _output(spawn, ["agy", "models"], env))
        elif family == "reasonix":
            found = parse_reasonix_doctor(
                await _output(spawn, ["reasonix", "doctor", "--json"], env))
        else:
            source = env if env is not None else os.environ
            home = codex_home or Path(source.get("CODEX_HOME") or Path.home() / ".codex")
            text = await asyncio.to_thread((home / "models_cache.json").read_text,
                                           encoding="utf-8")
            found = parse_codex_cache(text)
    except Exception as exc:                           # noqa: BLE001 - no list then
        logger.info("could not list the %s models: %s", family, exc)
        found = []
    _cache[family] = (now, found or None)
    return found or None


def _family(adapter_type: str) -> str:
    if adapter_type.startswith("antigravity"):
        return "antigravity"
    if adapter_type.startswith("codex"):
        return "codex"
    return adapter_type


async def _spawn(argv: Sequence[str], env: Optional[Dict[str, str]]) -> Any:
    from swarmly.telegram.adapters.base import windows_argv
    exec_argv = windows_argv(argv) if os.name == "nt" else list(argv)
    return await asyncio.create_subprocess_exec(
        *exec_argv, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)


async def _output(spawn: Spawner, argv: List[str], env: Optional[Dict[str, str]]) -> str:
    process = await spawn(argv, env)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=DISCOVERY_TIMEOUT)
    except asyncio.TimeoutError:
        if getattr(process, "returncode", 0) is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                process.kill()
        raise RuntimeError(f"{argv[0]} took over {DISCOVERY_TIMEOUT:.0f}s") from None
    if process.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {process.returncode}")
    return bytes(stdout or b"").decode("utf-8", errors="replace")


_AGY_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\t+(\S.*?)\s*$")


def parse_agy_models(text: str) -> List[str]:
    """``gemini-3.8-flash-high<TAB>Gemini 3.8 Flash (High)`` lines -> ``id (engine name)``."""
    return [f"{m.group(1)} ({m.group(2)})" for m in map(_AGY_LINE.match, text.splitlines()) if m]


def parse_codex_cache(text: str) -> List[str]:
    """The slugs Codex shows in its picker, in its own order (by priority)."""
    data = json.loads(text)
    models = data.get("models") if isinstance(data, dict) else data
    shown = [m for m in models or [] if isinstance(m, dict) and m.get("slug")
             and m.get("visibility", "list") != "hide"]
    shown.sort(key=lambda m: m.get("priority", 0))
    return [str(m["slug"]) for m in shown]


def parse_reasonix_doctor(text: str) -> List[str]:
    """Every model of every provider in ``reasonix doctor --json``."""
    data = json.loads(text)
    out: List[str] = []
    for provider in (data.get("providers") if isinstance(data, dict) else None) or []:
        for model in (provider.get("models") if isinstance(provider, dict) else None) or []:
            if str(model) not in out:
                out.append(str(model))
    return out


def format_list(names: Sequence[str]) -> str:
    """The names one per line, at most MAX_LISTED of them."""
    shown = [f"• {name}" for name in names[:MAX_LISTED]]
    if len(names) > MAX_LISTED:
        shown.append(f"… and {len(names) - MAX_LISTED} more")
    return "\n".join(shown)
