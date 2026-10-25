"""``swarmly mcp-setup``: connect an agent CLI to the swarm's MCP server, and check it works.

Each CLI keeps its MCP servers in its own file and format. This writes the same server
into each one, with absolute paths, so it starts the same way whatever directory or shell
the CLI runs from:

=========== ============================================== ==========================
CLI         file                                           how
=========== ============================================== ==========================
claude      (managed by ``claude mcp``)                    ``claude mcp add -s user``
codex       ``$CODEX_HOME/config.toml`` (``~/.codex``)     ``[mcp_servers.a2a]``
antigravity ``~/.gemini/config/mcp_config.json``           ``mcpServers.a2a``
gemini      ``~/.gemini/settings.json``                    ``mcpServers.a2a``
hermes      ``$HERMES_HOME/config.yaml`` (``~/.hermes``)   ``mcp_servers.a2a``
=========== ============================================== ==========================

The server runs as ``<python> -c <launcher> --swarm <swarm.yaml> --as <agent> mcp``. The
launcher puts the directory this ``swarmly`` package came from in place of the working
directory on ``sys.path``: the CLIs start MCP servers in the agent's workspace, and in a
swarmly checkout ``python -m`` would otherwise run the governor from whatever branch
that workspace has checked out. It also works from a checkout that was never installed.

Only the keys this tool owns are replaced (command, args, cwd, startup timeout, its env
vars); anything else the owner put in the entry stays, including a per-tool approval they
set by hand. Codex tool approvals are not written: the swarm's ``codex exec`` runs get
them on their own command line, so the owner's interactive Codex keeps asking.

``--check`` starts the server from the entry as it is in the file (or the proposed entry
on a dry run), in a temporary directory, with the environment the CLI is known to pass
on, completes the MCP handshake, lists the tools and calls ``a2a_next``. It never posts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NAME = "a2a"
CLIS = ("claude", "codex", "antigravity", "gemini", "hermes")
#: The vendor each CLI speaks for, to catch `--as hermes mcp-setup codex` typos.
VENDORS = {"claude": "anthropic", "codex": "openai", "antigravity": "google",
           "gemini": "google", "hermes": "nous"}
EXPECTED_TOOLS = {"a2a_next", "a2a_post", "a2a_inbox", "a2a_tasks", "a2a_escalations",
                  "a2a_guard"}
#: Environment the server needs that a CLI may not pass on: only paths, never secrets.
PASS_ENV = ("A2A_KEY_DIR", "A2A_LOG_DIR")
#: Pipes on Windows default to the ANSI code page; the server speaks UTF-8.
FIXED_ENV = {"PYTHONUTF8": "1"}
#: Drops the working directory ('' under -c) and makes sure the directory this swarmly
#: package was loaded from is on sys.path (first, unless it is already there, as
#: site-packages is: it must not go ahead of the stdlib), so the server runs the code
#: mcp-setup ran, not a workspace's.
LAUNCHER = ("import sys; sys.path[:] = [p for p in sys.path if p]; "
            "{root!r} in sys.path or sys.path.insert(0, {root!r}); "
            "from swarmly.swarm.cli import main; sys.exit(main())")
#: The directory holding the ``swarmly`` package (a checkout or site-packages).
PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
#: What Codex passes to a stdio server besides its own env/env_vars (codex-rs, 0.16x).
CODEX_ENV = ("HOME", "LOGNAME", "PATH", "SHELL", "USER", "USERNAME", "TMPDIR", "TEMP", "TMP",
             "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
             "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "USERPROFILE",
             "HOMEDRIVE", "HOMEPATH", "COMSPEC", "PATHEXT")
#: Gemini CLI drops variables whose names look like secrets.
GEMINI_SECRET = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL", re.I)


class SetupError(ValueError):
    pass


# -- what to run ---------------------------------------------------------------------

def resolve_python(python: Optional[str]) -> str:
    """An absolute, existing interpreter path."""
    if not python:
        return sys.executable
    found = shutil.which(python) or (python if Path(python).expanduser().exists() else None)
    if not found:
        raise SetupError(f"--python {python}: not found")
    return str(Path(found).expanduser().resolve())


def server_command(swarm_file: Path, agent: str,
                   python: Optional[str] = None) -> Tuple[str, List[str]]:
    return (resolve_python(python),
            ["-c", LAUNCHER.format(root=PACKAGE_ROOT), "--swarm",
             str(Path(swarm_file).resolve()), "--as", agent, "mcp"])


def server_env() -> Dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    env.update(FIXED_ENV)
    return env


def token_env_name(swarm: Any, agent: str) -> Optional[str]:
    """The doorbell token's variable name for ``agent``, when a doorbell is configured."""
    from .doorbell import DEFAULT_TOKEN_ENV
    conf = (getattr(swarm, "doorbell", None) or {}).get("telegram") or {}
    if not isinstance(conf, dict) or not conf.get("chat_id"):
        return None
    return str(conf.get("token_env") or DEFAULT_TOKEN_ENV).replace("{AGENT}", agent.upper())


def config_path(cli: str) -> Optional[Path]:
    home = Path.home()
    if cli == "codex":
        return Path(os.environ.get("CODEX_HOME") or home / ".codex") / "config.toml"
    if cli == "antigravity":
        # Where agy 1.3 reads it (`agy mcp list`); not ~/.gemini/antigravity/.
        return home / ".gemini" / "config" / "mcp_config.json"
    if cli == "gemini":
        return home / ".gemini" / "settings.json"
    if cli == "hermes":
        return Path(os.environ.get("HERMES_HOME") or home / ".hermes") / "config.yaml"
    if cli == "claude":
        return None
    raise SetupError(f"unknown CLI {cli!r}; one of {', '.join(CLIS)}")


def our_entry(cli: str, command: str, args: List[str], env: Dict[str, str], *,
              cwd: Optional[str] = None, token_env: Optional[str] = None) -> Dict[str, Any]:
    """The keys of the a2a entry this tool owns, for ``cli``."""
    entry: Dict[str, Any] = {"command": command, "args": list(args)}
    if env:
        entry["env"] = dict(env)
    if cli == "codex":
        if cwd:
            entry["cwd"] = cwd
        entry["startup_timeout_sec"] = 30
        # Codex passes only an allowlist of the CLI's own environment; name the rest.
        entry["env_vars"] = [n for n in (token_env, "SSH_AUTH_SOCK") if n]
        # No tool approvals here: they would apply to the owner's interactive Codex too.
        # The swarm's `codex exec` runs get them per run from the Telegram adapter
        # (swarmly/telegram/adapters/codex.py), so only those runs skip the prompt.
    return entry


#: Keys an earlier mcp-setup wrote and no longer does, with the value it wrote: removed
#: on a re-run when they still hold that value (a blanket Codex approval).
RETIRED = {"default_tools_approval_mode": "approve"}


def merged(old: Any, ours: Dict[str, Any]) -> Dict[str, Any]:
    """``ours`` over ``old``, keeping the keys we do not own."""
    out: Dict[str, Any] = dict(old) if isinstance(old, dict) else {}
    for key, value in RETIRED.items():
        if out.get(key) == value:
            del out[key]
    for key, value in ours.items():
        if key == "env" and isinstance(out.get("env"), dict):
            out["env"] = {**out["env"], **value}
        elif key == "env_vars" and isinstance(out.get("env_vars"), list):
            out["env_vars"] = list(dict.fromkeys([*out["env_vars"], *value]))
        else:
            out[key] = value
    return out


def _q(text: str) -> str:
    """A double-quoted string that TOML, YAML and JSON all read the same way."""
    return json.dumps(text, ensure_ascii=False)


# -- TOML (Codex) --------------------------------------------------------------------

def _toml_parser() -> Any:
    try:
        import tomllib
        return tomllib
    except ImportError:          # pragma: no cover - Python 3.10
        try:
            import tomli  # type: ignore[import-not-found]
            return tomli
        except ImportError:
            return None


def _toml_load(text: str) -> Dict[str, Any]:
    parser = _toml_parser()
    if parser is None:
        if text.strip():
            raise SetupError("editing an existing config.toml safely needs Python 3.11+ or "
                             'the tomli package (pip install "swarmly[swarm]")')
        return {}
    try:
        return parser.loads(text)
    except Exception as exc:     # noqa: BLE001 - tomllib/tomli decode errors
        raise SetupError(f"not valid TOML: {exc}") from None


def _toml_key(key: str) -> str:
    return key if re.match(r"^[A-Za-z0-9_-]+$", key) else _q(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _q(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}"
                                for k, v in value.items()) + " }"
    raise SetupError(f"cannot write {value!r} as TOML")


def toml_tables(prefix: str, data: Dict[str, Any]) -> str:
    """``data`` as ``[prefix]`` plus sub-tables for dicts of dicts (e.g. ``tools``)."""
    def is_tables(v: Any) -> bool:
        return isinstance(v, dict) and bool(v) and all(isinstance(x, dict) for x in v.values())
    plain = {k: v for k, v in data.items() if not is_tables(v)}
    lines = [f"[{prefix}]"] + [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in plain.items()]
    text = "\n".join(lines) + "\n"
    for key, tables in data.items():
        if is_tables(tables):
            for name, sub in tables.items():
                text += "\n" + toml_tables(f"{prefix}.{_toml_key(key)}.{_toml_key(name)}", sub)
    return text


def merge_toml(text: str, ours: Dict[str, Any]) -> str:
    """Replace ``[mcp_servers.a2a]`` (and its sub-tables), keeping what the owner added.

    Only the table form is edited. An entry written as dotted keys or an inline table is
    refused, and so is any result that would not parse back to the same settings.
    """
    before = _toml_load(text)
    if _toml_parser() is None:              # only an empty file gets here: nothing to keep
        return toml_tables(f"mcp_servers.{NAME}", dict(ours))
    servers = before.get("mcp_servers") or {}
    block = toml_tables(f"mcp_servers.{NAME}", merged(servers.get(NAME), ours))
    header = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
    out: List[str] = []
    pending: List[str] = []          # comments and blank lines inside a table we drop
    skipping = replaced = False
    for line in text.splitlines(keepends=True):
        match = header.match(line)
        if match:
            table = re.sub(r"\s+", "", match.group(1)).replace('"', "").replace("'", "")
            mine = table == f"mcp_servers.{NAME}" or table.startswith(f"mcp_servers.{NAME}.")
            if skipping and not mine:
                out.extend(pending)  # they lead into the next table: keep them
            pending = []
            if mine and not replaced:
                out.append(block)
                replaced = True
            skipping = mine
            if mine:
                continue
        if skipping:
            if not line.strip() or line.lstrip().startswith("#"):
                pending.append(line)
            else:
                pending = []
            continue
        out.append(line)
    if skipping:
        out.extend(pending)
    if NAME in servers and not replaced:
        raise SetupError(f"mcp_servers.{NAME} is written as dotted keys or an inline table; "
                         "delete it and run again")
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        if out and out[-1].strip():
            out.append("\n")
        out.append(block)
    result = "".join(out)
    try:
        after = _toml_load(result)
    except SetupError as exc:
        raise SetupError(f"the edit would break the file ({exc}); add the {NAME} entry "
                         "by hand") from None
    _same_except_ours(before, after, "TOML")
    return result


def _same_except_ours(before: Dict[str, Any], after: Dict[str, Any], kind: str,
                      servers_key: str = "mcp_servers") -> None:
    """Refuse an edit that changed anything but our own entry."""
    def strip(data: Dict[str, Any]) -> Dict[str, Any]:
        copy = dict(data)
        servers = dict(copy.get(servers_key) or {})
        servers.pop(NAME, None)
        copy[servers_key] = servers
        return copy
    if strip(before) != strip(after):
        raise SetupError(f"the {kind} edit would change other settings; add the {NAME} "
                         "entry by hand")


# -- YAML (Hermes) -------------------------------------------------------------------

_YAML_TOP = re.compile(r"""^(["']?)mcp_servers\1\s*:(.*)$""")


def _yaml() -> Any:
    try:
        import yaml
    except ImportError:
        raise SetupError('editing config.yaml needs PyYAML: pip install '
                         '"swarmly[swarm]"') from None
    return yaml


def yaml_block(entry: Dict[str, Any], indent: int = 2) -> str:
    """The entry as YAML; each value in JSON flow style, which YAML reads as-is."""
    pad = " " * indent
    lines = [f"{pad}{NAME}:"] + [f"{pad}{pad}{k}: {json.dumps(v, ensure_ascii=False)}"
                                 for k, v in entry.items()]
    return "\n".join(lines) + "\n"


def merge_yaml(text: str, ours: Dict[str, Any]) -> str:
    """Put the entry under the top-level ``mcp_servers:`` key, replacing an old ``swarmly``."""
    yaml = _yaml()
    try:
        before = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise SetupError(f"not valid YAML: {exc}") from None
    if not isinstance(before, dict):
        raise SetupError("the file is not a YAML mapping")
    entry = merged((before.get("mcp_servers") or {}).get(NAME), ours)
    lines = text.splitlines(keepends=True)
    top = [i for i, line in enumerate(lines) if _YAML_TOP.match(line.rstrip("\r\n"))]
    if len(top) > 1:
        raise SetupError("the file has more than one top-level mcp_servers key")
    if not top:
        tail = "" if not text or text.endswith("\n") else "\n"
        result = (text + tail + ("\n" if text.strip() else "") + "mcp_servers:\n"
                  + yaml_block(entry))
        return _check_yaml(before, result)
    start = top[0]
    rest = _YAML_TOP.match(lines[start].rstrip("\r\n")).group(2)   # type: ignore[union-attr]
    value = rest.split(" #", 1)[0].strip() if not rest.strip().startswith("#") else ""
    if value not in ("", "{}", "null", "~", "Null", "NULL"):
        raise SetupError("mcp_servers is written inline or with an anchor; add the a2a entry "
                         "by hand")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            end = i
            break
    out: List[str] = []
    pending: List[str] = []
    skipping = False
    indent: Optional[int] = None
    for line in lines[start + 1:end]:
        stripped = line.lstrip(" ")
        depth = len(line) - len(stripped)
        if stripped.strip() and not stripped.startswith("#"):
            if indent is None:
                indent = depth
            if depth == indent:
                if skipping:
                    out.extend(pending)   # comments that lead into the next server
                pending = []
                skipping = stripped.split(":", 1)[0].strip().strip("'\"") == NAME
            elif skipping:
                pending = []
        elif skipping:
            pending.append(line)
            continue
        if not skipping:
            out.append(line)
    if skipping:
        out.extend(pending)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    block = yaml_block(entry, indent or 2)
    result = "".join(lines[:start]) + "mcp_servers:\n" + "".join(out) + block \
        + "".join(lines[end:])
    return _check_yaml(before, result)


def _check_yaml(before: Dict[str, Any], result: str) -> str:
    yaml = _yaml()
    try:
        after = yaml.safe_load(result)
    except yaml.YAMLError as exc:
        raise SetupError(f"the edit would break the file: {exc}") from None
    if not isinstance(after, dict) or \
            not isinstance((after.get("mcp_servers") or {}).get(NAME), dict):
        raise SetupError(f"the {NAME} entry did not land under mcp_servers; add it by hand")
    _same_except_ours(before, after, "YAML")
    return result


# -- JSON (Antigravity, Gemini) ------------------------------------------------------

def _json_load(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise SetupError(f"not plain JSON ({exc}); comments are not supported, add the "
                         f"{NAME} entry by hand") from None
    if not isinstance(data, dict):
        raise SetupError("the file is not a JSON object")
    return data


def merge_json(text: str, ours: Dict[str, Any]) -> str:
    data = _json_load(text)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError("mcpServers is not an object")
    servers[NAME] = merged(servers.get(NAME), ours)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# -- dispatch ------------------------------------------------------------------------

def render(cli: str, ours: Dict[str, Any], current: str = "") -> str:
    """The whole new file for ``cli``, given its current text."""
    if cli == "codex":
        return merge_toml(current, ours)
    if cli == "hermes":
        return merge_yaml(current, ours)
    if cli in ("antigravity", "gemini"):
        return merge_json(current, ours)
    raise SetupError(f"{cli} has no config file to render")


def entry_in(cli: str, text: str) -> Optional[Dict[str, Any]]:
    """The a2a entry as the CLI will read it from ``text``, or None."""
    if cli == "codex":
        if _toml_parser() is None:
            return None
        servers = _toml_load(text).get("mcp_servers") or {}
    elif cli == "hermes":
        servers = (_yaml().safe_load(text) or {}).get("mcp_servers") or {}
    else:
        servers = _json_load(text).get("mcpServers") or {}
    entry = servers.get(NAME)
    return entry if isinstance(entry, dict) else None


def read_text(path: Path) -> str:
    """The file's text; a BOM (PowerShell 5.1 writes one) is dropped."""
    return path.read_text(encoding="utf-8-sig") if path.exists() else ""


def claude_command(command: str, args: List[str], env: Dict[str, str],
                   claude: str = "claude") -> List[str]:
    # The name goes first: -e takes every value after it up to the next option or --.
    cmd = [claude, "mcp", "add", NAME, "--scope", "user"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    return cmd + ["--", command, *args]


def write(path: Path, text: str) -> Optional[Path]:
    """Write ``text`` to ``path``, keeping the old file as ``<name>.bak``.

    A symlinked config is written through to its target, and keeps its permissions
    (these files can hold API keys); a new file is private to the user.
    """
    backup = None
    path = path.resolve()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o600
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup)
            mode = path.stat().st_mode & 0o777
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SetupError(f"could not write {path}: {exc}") from None
    return backup


# -- the check -----------------------------------------------------------------------

def cli_env(cli: str, entry_env: Dict[str, str],
            passed: Tuple[str, ...] = ()) -> Dict[str, str]:
    """The environment ``cli`` gives the server: its own filter (plus the names Codex is
    told to pass in ``env_vars``), then the entry's env."""
    if cli == "codex":
        base = {k: v for k, v in os.environ.items()
                if k.upper() in CODEX_ENV or k in passed}
    elif cli == "gemini":
        base = {k: v for k, v in os.environ.items() if not GEMINI_SECRET.search(k)}
    else:
        base = dict(os.environ)
    base.update(entry_env)
    return base


def check(command: str, args: List[str], env: Optional[Dict[str, str]] = None,
          timeout: float = 30.0, *, full_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Start the server from a temporary directory and talk MCP to it. Never posts.

    Returns ``{"ok", "tools", "missing", "agent", "next", "error", "seconds"}``.
    """
    started = time.monotonic()
    if full_env is None:
        full_env = dict(os.environ)
        full_env.update(env or {})
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "a2a-mcp-setup", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "a2a_next", "arguments": {}}},
    ]
    stdin = "".join(json.dumps(r) + "\n" for r in requests)
    result: Dict[str, Any] = {"ok": False, "tools": [], "missing": sorted(EXPECTED_TOOLS),
                              "agent": None, "next": None, "error": None}
    with tempfile.TemporaryDirectory(prefix="a2a-mcp-check-") as cwd:
        try:
            proc = subprocess.run([command, *args], input=stdin.encode("utf-8"),
                                  capture_output=True, timeout=timeout, env=full_env,
                                  cwd=cwd)
        except subprocess.TimeoutExpired as exc:
            err = (exc.stderr or b"").decode("utf-8", "replace")
            result["error"] = f"the server did not answer within {timeout:.0f}s: {err[-600:]}"
            result["seconds"] = round(time.monotonic() - started, 1)
            return result
        except OSError as exc:
            result["error"] = f"could not run the server: {exc}"
            result["seconds"] = round(time.monotonic() - started, 1)
            return result
    stdout = proc.stdout.decode("utf-8", "replace")
    replies: Dict[Any, Dict[str, Any]] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            result["error"] = f"the server wrote non-MCP output to stdout: {line[:200]!r}"
            break
        if isinstance(msg, dict):
            replies[msg.get("id")] = msg
    result["seconds"] = round(time.monotonic() - started, 1)
    stderr = proc.stderr.decode("utf-8", "replace").strip()[-600:]
    if result["error"] is None and 1 not in replies:
        result["error"] = f"no answer to initialize (exit {proc.returncode}): {stderr}"
    if result["error"] is None and "error" in replies[1]:
        result["error"] = f"initialize failed: {replies[1]['error']}"
    if result["error"] is None:
        init = replies[1].get("result") or {}
        result["instructions"] = init.get("instructions")
        tools = {t.get("name") for t in (replies.get(2, {}).get("result") or {})
                 .get("tools", [])}
        result["tools"] = sorted(t for t in tools if t)
        result["missing"] = sorted(EXPECTED_TOOLS - tools)
        call = replies.get(3, {}).get("result") or {}
        text = ((call.get("content") or [{}])[0]).get("text")
        try:
            result["next"] = json.loads(text) if text else None
        except ValueError:
            result["next"] = text
        if call.get("isError") or not isinstance(result["next"], dict):
            result["error"] = f"a2a_next failed: {text or replies.get(3)}"
        else:
            result["agent"] = result["next"].get("agent")
    result["ok"] = result["error"] is None and not result["missing"]
    return result


def check_key(swarm: Any, agent: str, env: Dict[str, str]) -> Optional[str]:
    """A problem with the signing key the server will use, or None."""
    from . import keys
    old = os.environ.get("A2A_KEY_DIR")
    try:
        if env.get("A2A_KEY_DIR"):
            os.environ["A2A_KEY_DIR"] = env["A2A_KEY_DIR"]
        where = keys.key_path(agent)
        try:
            public = keys.public_key_b64(keys.load_private(agent))
        except Exception as exc:  # noqa: BLE001 - SwarmConfigError and crypto errors
            return str(exc)
    finally:
        if old is None:
            os.environ.pop("A2A_KEY_DIR", None)
        else:
            os.environ["A2A_KEY_DIR"] = old
    if public != swarm.public_key(agent):
        return (f"the key in {where} does not match {agent}'s key in swarm.yaml, so every "
                "post would be quarantined")
    return None


def describe_check(result: Dict[str, Any]) -> str:
    if result["ok"]:
        nxt = result.get("next") or {}
        owed = len(nxt.get("obligations") or [])
        return (f"MCP check OK in {result['seconds']}s: {len(result['tools'])} tools, "
                f"a2a_next answered as {result['agent']} ({owed} obligation(s))")
    if result.get("missing") and not result.get("error"):
        return "MCP check FAILED: missing tools " + ", ".join(result["missing"])
    return f"MCP check FAILED: {result.get('error')}"
