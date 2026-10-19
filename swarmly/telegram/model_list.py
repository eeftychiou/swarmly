"""The models a bot's CLI can run, for the bare ``/model`` reply.

Each CLI says it differently, so each has its own source:

- Antigravity (``agy``): ``agy models`` prints ``<id>\\t<engine name>`` per line.
- Codex: the CLI keeps the model list it fetched in ``~/.codex/models_cache.json``
  (``$CODEX_HOME`` when set); models marked ``visibility: hide`` are left out, as in its picker.
- Reasonix: ``reasonix doctor --json`` lists each configured provider and its models.
- Claude Code has no list command; it takes the aliases (opus, sonnet, ...) or any claude-* name.

A list that cannot be read is an empty list: the reply then only says where to look. Lists are
kept for ``CACHE_SECONDS`` so a repeated ``/model`` does not run the CLI again (``agy models``
asks the server and takes a few seconds).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("swarmly.telegram.model_list")

#: How long a fetched list is reused.
CACHE_SECONDS = 600.0
#: How long a CLI may take to list its models.
TIMEOUT_SECONDS = 20.0
#: At most this many models in one reply.
MAX_LISTED = 30

#: adapter type -> (expires at, models)
_cache: Dict[str, Tuple[float, List[str]]] = {}

Runner = Callable[..., Any]


def available_models(adapter_type: str, *, runner: Runner = subprocess.run,
                     clock: Callable[[], float] = time.monotonic,
                     codex_home: Optional[Path] = None) -> List[str]:
    """The models this adapter type's CLI can run, as ``name`` or ``name (description)``
    entries; the part before a space is what ``/model`` takes."""
    now = clock()
    cached = _cache.get(adapter_type)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        models = _fetch(adapter_type, runner, codex_home)
    except Exception as exc:                      # noqa: BLE001 - a list is a nicety
        logger.info("could not list the %s models: %s", adapter_type, exc)
        models = []
    _cache[adapter_type] = (now + CACHE_SECONDS, models)
    return models


def _fetch(adapter_type: str, runner: Runner, codex_home: Optional[Path]) -> List[str]:
    from swarmly.telegram.config import _AGY_MODEL_TYPES, _CLAUDE_MODEL_ALIASES
    if adapter_type in _AGY_MODEL_TYPES:
        return parse_agy_models(_run(runner, ["agy", "models"]))
    if adapter_type.startswith("codex"):
        home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return parse_codex_cache((home / "models_cache.json").read_text(encoding="utf-8"))
    if adapter_type == "reasonix":
        return parse_reasonix_doctor(_run(runner, ["reasonix", "doctor", "--json"]))
    if adapter_type.startswith("claude"):
        return list(_CLAUDE_MODEL_ALIASES)
    return []


def _run(runner: Runner, argv: List[str]) -> str:
    out = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                 timeout=TIMEOUT_SECONDS, check=False, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {out.returncode}")
    return out.stdout or ""


_AGY_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\t+(\S.*?)\s*$")


def parse_agy_models(text: str) -> List[str]:
    """``gemini-3.8-flash-high\\tGemini 3.8 Flash (High)`` lines -> ``id (engine name)``."""
    return [f"{m.group(1)} ({m.group(2)})" for m in map(_AGY_LINE.match, text.splitlines()) if m]


def parse_codex_cache(text: str) -> List[str]:
    """The slugs Codex lists in its picker, in its own order (by priority)."""
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
    for provider in data.get("providers") or []:
        for model in provider.get("models") or []:
            if str(model) not in out:
                out.append(str(model))
    return out


def models_line(adapter_type: str, models: List[str]) -> str:
    """The sentence the ``/model`` reply ends with."""
    if not models:
        where = {"antigravity": "`agy models`", "antigravity_thread": "`agy models`",
                 "reasonix": "reasonix.toml"}.get(adapter_type, "the CLI's documentation")
        return f"I could not read the list of models; see {where}."
    shown = models[:MAX_LISTED]
    more = f" (and {len(models) - len(shown)} more)" if len(models) > len(shown) else ""
    extra = " or any claude-* name" if adapter_type.startswith("claude") else ""
    return "Available: " + ", ".join(shown) + more + extra + "."
