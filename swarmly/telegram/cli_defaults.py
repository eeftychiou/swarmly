"""The model and reasoning effort a CLI runs when the transport passes neither.

With no ``adapter.model`` / ``adapter.effort`` (and no ``/model`` / ``/reasoning``), the CLI
picks: from its own settings file, an environment variable, or a built-in default. The admin
asked for the real names in the online notice, not "CLI default", so this module reads the
places each CLI documents. It only reads; it never runs a CLI (that would cost a turn).

* **Codex** -- ``$CODEX_HOME/config.toml`` (``~/.codex``): ``model`` and
  ``model_reasoning_effort``, from the selected ``[profiles.<profile>]`` first.
* **Reasonix** -- ``./reasonix.toml`` in the work directory, then its global
  ``config.toml`` (``%APPDATA%\\reasonix`` on Windows, ``$XDG_CONFIG_HOME/reasonix`` or
  ``~/.config/reasonix`` elsewhere): ``default_model``; the effort is that model's
  ``default_effort`` in ``model_overrides``. This is the resolution order the file states.
* **Claude Code** -- ``ANTHROPIC_MODEL`` and ``CLAUDE_CODE_EFFORT_LEVEL``, then ``model`` and
  ``effortLevel`` in ``.claude/settings.local.json``, ``.claude/settings.json`` (work
  directory) and ``~/.claude/settings.json``.
* **Antigravity** documents no settings file for either, but accepts ANTIGRAVITY_MODEL /
  AGY_MODEL and ANTIGRAVITY_EFFORT / AGY_EFFORT in the environment, and infers effort from
  tiered model names (e.g. ``Gemini 3.1 Pro (High)`` or ``gemini-3.8-flash-high``).

Whatever is not found stays None; the channel then falls back to the model the CLI reported on
its last turn, where the CLI reports one (Claude's ``modelUsage``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("swarmly.telegram.cli_defaults")


@dataclass(frozen=True)
class CliDefault:
    """What the CLI's own configuration names, or None for a field it leaves to the CLI."""

    model: Optional[str] = None
    effort: Optional[str] = None


def cli_default(adapter_type: str, cwd: Optional[str] = None, *,
                model: Optional[str] = None,
                env: Optional[Mapping[str, str]] = None,
                home: Optional[Path] = None) -> CliDefault:
    """The model and effort ``adapter_type``'s CLI would pick by itself. Never raises."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    try:
        if adapter_type.startswith("codex"):
            return _codex(env, home)
        if adapter_type == "reasonix":
            return _reasonix(cwd, env, home, model=model)
        if adapter_type.startswith("claude"):
            return _claude(cwd, env, home)
        if adapter_type.startswith("antigravity"):
            return _antigravity(cwd, env, home, model=model)
    except Exception:                                    # noqa: BLE001 - display only
        logger.debug("could not read the %s CLI's defaults", adapter_type, exc_info=True)
    return CliDefault()


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _read_toml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    try:
        import tomllib
    except ImportError:                                  # Python 3.10: top-level keys only
        return _top_level_toml(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        logger.debug("%s is not valid TOML", path)
        return {}


_TOML_STRING = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"')


def _top_level_toml(text: str) -> Dict[str, Any]:
    """``key = "value"`` lines before the first table: enough for the keys read here."""
    found: Dict[str, Any] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            break
        match = _TOML_STRING.match(line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def _codex(env: Mapping[str, str], home: Path) -> CliDefault:
    data = _read_toml(Path(env.get("CODEX_HOME") or home / ".codex") / "config.toml")
    profile = data.get("profiles", {}).get(data.get("profile"), {}) \
        if isinstance(data.get("profiles"), dict) else {}
    if not isinstance(profile, dict):
        profile = {}
    return CliDefault(model=_text(profile.get("model")) or _text(data.get("model")),
                      effort=_text(profile.get("model_reasoning_effort"))
                      or _text(data.get("model_reasoning_effort")))


def _reasonix_global(env: Mapping[str, str], home: Path) -> Path:
    if os.name == "nt" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "reasonix" / "config.toml"
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "reasonix" / "config.toml"


def _reasonix(cwd: Optional[str], env: Mapping[str, str], home: Path,
              model: Optional[str] = None) -> CliDefault:
    layers = [_read_toml(_reasonix_global(env, home))]
    if cwd:
        layers.append(_read_toml(Path(cwd) / "reasonix.toml"))
    merged: Dict[str, Any] = {}
    for layer in layers:                                 # the project file wins
        merged.update(layer)
    target_model = model or _text(merged.get("default_model")) or _text(env.get("REASONIX_MODEL"))
    # model_overrides sits in each [[providers]] entry (or at the top level).
    tables = [merged] + [p for p in merged.get("providers") or [] if isinstance(p, dict)]
    effort = None
    for table in tables:
        overrides = table.get("model_overrides")
        per_model = overrides.get(target_model) if target_model and isinstance(overrides, dict) else None
        if isinstance(per_model, dict) and _text(per_model.get("default_effort")):
            effort = _text(per_model.get("default_effort"))
            break
    effort = effort or _text(env.get("REASONIX_EFFORT"))
    if target_model and not effort:
        m = re.search(r"[-_\s]\(?\b(low|medium|high|xhigh|max)\)?$", target_model, re.I)
        if m:
            effort = m.group(1).lower()
    return CliDefault(model=target_model if not model else None, effort=effort)


def _antigravity(cwd: Optional[str], env: Mapping[str, str], home: Path,
                 model: Optional[str] = None) -> CliDefault:
    target_model = model or _text(env.get("ANTIGRAVITY_MODEL")) or _text(env.get("AGY_MODEL"))
    effort = _text(env.get("ANTIGRAVITY_EFFORT")) or _text(env.get("AGY_EFFORT"))
    if target_model and not effort:
        m = re.search(r"[-_\s]\(?\b(low|medium|high|xhigh|max)\)?$", target_model, re.I)
        if m:
            effort = m.group(1).lower()
    return CliDefault(model=target_model if not model else None, effort=effort)


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _claude(cwd: Optional[str], env: Mapping[str, str], home: Path) -> CliDefault:
    files = []
    if cwd:
        files += [Path(cwd) / ".claude" / "settings.local.json",
                  Path(cwd) / ".claude" / "settings.json"]
    files.append(home / ".claude" / "settings.json")
    settings = [_read_json(path) for path in files]       # highest precedence first
    model = _text(env.get("ANTHROPIC_MODEL")) or next(
        (_text(s.get("model")) for s in settings if _text(s.get("model"))), None)
    effort = _text(env.get("CLAUDE_CODE_EFFORT_LEVEL")) or next(
        (_text(s.get("effortLevel")) for s in settings if _text(s.get("effortLevel"))), None)
    return CliDefault(model=model, effort=effort)


def reported_model(model_usage: Any) -> Optional[str]:
    """The main model in Claude's ``modelUsage``: the one that wrote the most output (a
    subagent or a title on a small model must not name the bot's model)."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None

    def output(item: Any) -> float:
        usage = item[1] if isinstance(item[1], dict) else {}
        value = usage.get("outputTokens", 0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    name = max(model_usage.items(), key=output)[0]
    return name if isinstance(name, str) and name else None
