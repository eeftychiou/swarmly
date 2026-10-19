"""
Per-agent configuration for the Telegram home channel.

One ``config.yaml`` per agent:

    agent:
      name: example_hermes_bot       # the bot's Telegram username (from @BotFather)
      alias: Hermes                    # the alias used on GitHub, in A2A and in chat
      bot_token: "123456:ABC..."       # or bot_token_env: HERMES_TELEGRAM_BOT_TOKEN
      home_channel_id: -1001234567890  # the group every bot joins and talks in
      admin_user_id: 1234567890        # the human who controls everything
    peers:
      Codex: example_codex_bot              # alias -> Telegram username (no @ needed)
      Antigravity: example_agy_bot
    adapter:
      type: hermes                     # codex | codex_thread | claude | claude_thread | hermes | antigravity | reasonix | echo | custom
      cwd: /home/user/projects/your-project
      timeout_seconds: 900
    fallback:
      gitbus_poll_minutes: 10          # optional: poll the project's durable git bus every N minutes (0 disables)
      # gitbus_root: /path/to/project  # required for polling: the project whose bus is polled
      # gitbus_ref: origin/main        # ref read for the freshest view (refresh is fetch-only)
    bus:                               # optional: durable-plane transport (default: git)
      # transport: folder              # git | folder | gdrive (see docs/TRANSPORTS.md)
      # folder: ~/GoogleDrive/a2a-bus  # transport: folder -- a synced or shared directory
      # gdrive:                        # transport: gdrive -- the Drive API, no sync client
      #   folder_id: 1AbC...
      #   credentials: ~/.config/a2a/gdrive.json

Identity is the bot token: whoever holds the token *is* that bot in Telegram, so there is
no second authentication layer to configure. The alias is the name everyone else uses.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from swarmly.telegram.roles import Role

try:                                    # optional dependency of the telegram extra
    import yaml
    HAS_YAML = True
except ImportError:                     # pragma: no cover - exercised on minimal installs
    HAS_YAML = False


logger = logging.getLogger("swarmly.telegram.config")


def load_env(search_paths: Optional[Sequence[Path | str]] = None) -> None:
    """Load variables from ``.env`` beside the config (*search_paths*), then
    ``~/.a2a-telegram.env`` and ``~/.env``, without overriding os.environ."""
    candidates: List[Path] = []
    if search_paths:
        for p in search_paths:
            if p:
                # A folder means its .env (the config's own folder is passed this way and
                # was silently skipped before, since only files were read).
                candidates.append(Path(p) / ".env" if Path(p).is_dir() else Path(p))
    # Review P0-6: never the working directory or its parent. A .env planted in whatever
    # folder the bot was started from (or an agent's workspace) must not set its secrets
    # or NODE_OPTIONS-style variables; the config's own folder and the home files are the
    # operator's.
    candidates.extend([
        Path.home() / ".a2a-telegram.env",
        Path.home() / ".env",
    ])
    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            try:
                # Issue #113: utf-8-sig -- a BOM (Notepad et al.) would otherwise turn
                # the first line's key into "\ufeffKEY" and silently drop that entry.
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'").strip('"')
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


class ConfigError(ValueError):
    """Raised when config.yaml is missing required fields or is internally inconsistent."""


_BOOL_LIMIT_WORDS: Dict[str, bool] = {
    "true": True, "yes": True, "on": True, "1": True,
    "false": False, "no": False, "off": False, "0": False,
}


def _parse_bool(where: str, value: Any) -> bool:
    """Parse a boolean config value explicitly (issue #109, review P1-7).

    ``bool("false")`` is ``True``, so a quoted value that reads as *off* would silently
    turn a switch ON. Accept real bools and the recognised words (true/false, yes/no,
    on/off, 1/0, case-insensitive); reject everything else with the key named.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
        raise ConfigError(f"{where} must be a boolean — one of true/false, "
                          f"yes/no, on/off, 1/0 (got {value!r})")
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _BOOL_LIMIT_WORDS:
            return _BOOL_LIMIT_WORDS[word]
        raise ConfigError(f"{where} must be a boolean — one of true/false, "
                          f"yes/no, on/off, 1/0 (got {value!r})")
    raise ConfigError(f"{where} must be a boolean (got {value!r})")


def _coerce_bool_limit(key: str, value: Any) -> bool:
    """A bool-typed ``limits`` entry; see :func:`_parse_bool`."""
    return _parse_bool(f"limits.{key}", value)


def _coerce_int_list(key: str, value: Any) -> List[int]:
    """A list-of-integers ``limits`` entry (``limits.cleanup_keep``).

    A bare integer is accepted as a one-item list -- ``cleanup_keep: 42`` is a plausible
    thing to write -- but a string, a float or a bool is not: those are typos, and a silently
    mangled keep list would protect the wrong topic or none.
    """
    if isinstance(value, bool):
        raise ConfigError(f"limits.{key} must be a list of topic ids (got {value!r})")
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"limits.{key} must be a list of topic ids (got {value!r})")
    ids: List[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"limits.{key} must be a list of topic ids (got {item!r})")
        ids.append(int(item))
    return ids


def _parse_int(where: str, value: Any, default: int, *, positive: bool = False) -> int:
    """An integer config value, ``default`` when unset. A YAML ``true`` is not a number,
    and ``0`` stays ``0`` (it used to become the default through ``or``)."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{where} must be an integer (got {value!r})")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be an integer (got {value!r})") from None
    if positive and number <= 0:
        raise ConfigError(f"{where} must be a positive integer (got {value!r})")
    return number


#: Review P1-7: the keys each section of the transport config is read for. Anything else
#: is reported by :func:`unknown_keys` (and ``swarmly-telegram check``) instead of being
#: ignored without a word. ``peers`` maps free-form aliases and ``limits`` is checked
#: against :class:`ChannelLimits`.
KNOWN_KEYS: Dict[str, Tuple[str, ...]] = {
    "": ("agent", "peers", "adapter", "limits", "fallback", "bus", "projects", "github",
         "public_redactions"),
    "agent": ("name", "alias", "bot_token", "bot_token_env", "home_channel_id",
              "admin_user_id", "maintainer_username", "allowed_users", "git_name",
              "git_email", "role"),
    "adapter": ("type", "cwd", "timeout_seconds", "command", "extra_env",
                "max_output_chars", "enabled", "conversation", "conversation_id",
                "reply_state_path", "reply_timeout_seconds", "permissions", "model",
                "allow_commands", "pass_env", "effort", "git_sync", "session_token_limit", "session_turn_limit"),
    "bus": ("repo", "ref", "poll_minutes", "enabled", "verify_settle_seconds", "transport",
            "folder", "gdrive", "swarm", "swarm_agent", "allow_unsigned", "senders",
            "mirror_handoffs"),
    "fallback": ("gitbus_root", "gitbus_ref", "gitbus_poll_minutes"),
    "projects": ("repo", "cwd", "topics", "worktrees", "reply_chars"),
    "github": ("login", "token_env", "poll_minutes", "mark_read", "tracking_label"),
}


def unknown_keys(data: Dict[str, Any]) -> List[str]:
    """One line per config key the loader does not read, with the closest known key."""
    found: List[str] = []

    def report(section: str, keys: Any, known: Tuple[str, ...]) -> None:
        for key in keys:
            if str(key) in known:
                continue
            where = f"{section}.{key}" if section else str(key)
            close = difflib.get_close_matches(str(key), known, n=1)
            hint = f" (did you mean {section + '.' if section else ''}{close[0]}?)" \
                if close else ""
            found.append(f"{where}: unknown key, ignored{hint}")

    report("", data, KNOWN_KEYS[""])
    for section in ("agent", "adapter", "bus", "fallback", "github"):
        raw = data.get(section)
        if isinstance(raw, dict):
            report(section, raw, KNOWN_KEYS[section])
    projects_raw = data.get("projects")
    if isinstance(projects_raw, dict):
        for proj_key, proj_dict in projects_raw.items():
            if isinstance(proj_dict, dict):
                report(f"projects.{proj_key}", proj_dict, KNOWN_KEYS["projects"])
    limits_raw = data.get("limits")
    if isinstance(limits_raw, dict):
        report("limits", limits_raw, tuple(f.name for f in fields(ChannelLimits)))
    return found


DEFAULT_COMMANDS: Dict[str, str] = {
    # {prompt} is substituted with the incoming message text; {permissions} with the
    # adapter.permissions flags below; {model} with adapter.model's flag and {effort} with
    # adapter.effort's flag, when set.
    # --skip-git-repo-check: a bot's working folder need not be a git repository.
    "codex": "codex exec --skip-git-repo-check {model} {effort} {permissions} {prompt}",
    "hermes": "hermes -z {prompt}",
    "claude": "claude -p --output-format json {model} {effort} {permissions} -- {prompt}",
    # claude_thread adds --resume/--session-id itself (swarmly/telegram/adapters/claude.py).
    "claude_thread": "claude -p --output-format json {model} {effort} {permissions} -- {prompt}",
    "antigravity": "agy -p {prompt} {model} {effort} --print-timeout 20m "
                   "--dangerously-skip-permissions",
    # Reasonix (DeepSeek): '--' ends its flags, so a prompt starting with '-' stays the task.
    "reasonix": "reasonix run -p --output-format text {model} {effort} {permissions} -- "
                "{prompt}",
    "echo": "",                          # in-process, never spawns anything
}

#: adapter.permissions: how much the agent may do without asking. Nobody can answer a
#: permission prompt in a headless run, so anything not allowed is silently refused.
#:   strict   - edit files in the working directory only; no shell (Claude), no network (Codex)
#:   standard - also git (including push/pull), Python, pytest, pip/uv, read-only shell
#:              commands and web fetches (Claude); network access in the sandbox (Codex).
#:              Python and pip run arbitrary code: this is code execution, not a boundary
#:   full     - no permission checks and no sandbox. Use on a dedicated machine, VM or
#:              container only: anyone who can message the bot can make it run anything.
#: Reasonix: strict lets edits through and refuses shell commands; standard (its ``auto``)
#: runs any command its own rules do not deny; full is ``bypassPermissions``. A headless
#: ``reasonix run`` refuses whatever would need approval rather than waiting.
#: Antigravity's default command skips approvals (agy -p cannot ask); an explicit
#: adapter.command decides for itself. Hermes has no flags.
PERMISSION_LEVELS = ("strict", "standard", "full")
#: The default is full, the owner's call: agents that can pull, commit, push and run gh out
#: of the box (a sandboxed Codex could neither pull, commit nor push: workspace-write keeps
#: .git read-only and cannot start git's credential helper). Anyone who can reach a bot can
#: then run anything on its host, so the docs require a private group and a private bus
#: repository. Set strict or standard per bot to narrow it.
DEFAULT_PERMISSIONS = "full"
#: Per-type defaults when adapter.permissions is not set (none differ today).
DEFAULT_PERMISSIONS_BY_TYPE: Dict[str, str] = {}


def default_permissions(adapter_type: str) -> str:
    """adapter.permissions when the config leaves it out."""
    return DEFAULT_PERMISSIONS_BY_TYPE.get(adapter_type, DEFAULT_PERMISSIONS)
_CLAUDE_STANDARD_TOOLS = ",".join(
    [f"{shell}({cmd}:*)" for shell in ("Bash", "PowerShell")
     for cmd in ("git", "python", "python3", "py", "pytest", "pip", "uv")]
    + [f"Bash({cmd}:*)" for cmd in ("ls", "cat", "head", "tail", "grep", "rg", "find", "wc",
                                    "diff")]
    + [f"PowerShell({cmd}:*)" for cmd in ("Get-ChildItem", "Get-Content", "Select-String",
                                          "Get-Location")]
    + ["WebFetch", "WebSearch"])
PERMISSION_FLAGS: Dict[str, Dict[str, str]] = {
    "codex": {
        "strict": "--sandbox workspace-write",
        "standard": "--sandbox workspace-write -c sandbox_workspace_write.network_access=true",
        "full": "--dangerously-bypass-approvals-and-sandbox",
    },
    "claude": {
        "strict": "--permission-mode acceptEdits",
        "standard": f"--permission-mode acceptEdits --allowedTools {_CLAUDE_STANDARD_TOOLS}",
        "full": "--permission-mode bypassPermissions",
    },
}
PERMISSION_FLAGS["claude_thread"] = PERMISSION_FLAGS["claude"]
# From Reasonix 2.35's source (internal/frontend/cli/cli.go, session/control/approval.go):
# a headless run never prompts; ``ask`` refuses, acceptEdits is ask plus the file-edit tools,
# auto allows what its rules do not deny, bypassPermissions skips approvals (its help says
# the OS sandbox, where the platform has one, and deny rules still apply).
PERMISSION_FLAGS["reasonix"] = {
    "strict": "--permission-mode acceptEdits",
    "standard": "--permission-mode auto",
    "full": "--permission-mode bypassPermissions",
}

#: adapter.model and ``/model``: one model name, never a flag or a second argument.
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]/-]*")
#: An Antigravity engine name as ``agy models`` prints it -- ``Gemini 3.1 Pro (High)``
#: (docs/telegram/SETUP-ANTIGRAVITY.md): mixed case, digits, dots, parentheses and single
#: spaces between words. No leading dash (a flag), no quote, no shell metacharacter and no
#: control character: it reaches the command line as one argv token (issue #10).
#: One word of an engine name: letters, digits, dots, hyphens, ``+``, with an optional
#: trailing "(Qualifier)" -- never a leading dash (a flag), a quote or a metacharacter.
_AGY_WORD = r"[A-Za-z0-9][A-Za-z0-9._+-]*(?: \([A-Za-z0-9][A-Za-z0-9._+-]*\))?"
_AGY_MODEL = re.compile(rf"{_AGY_WORD}(?: {_AGY_WORD})*")
_AGY_MODEL_TYPES = ("antigravity", "antigravity_thread")
#: Adapter types whose default command takes a model.
MODEL_TYPES = ("codex", "claude", "claude_thread", "reasonix", "antigravity")
#: adapter.effort: the reasoning effort, mapped onto each CLI's own flag. Codex has no max.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_FLAGS: Dict[str, str] = {
    "codex": "-c model_reasoning_effort={effort}",
    "claude": "--effort {effort}",
    "claude_thread": "--effort {effort}",
    "antigravity": "--effort {effort}",
    "reasonix": "--effort {effort}",
}


def effort_problem(adapter_type: str, effort: str) -> Optional[str]:
    """Why ``effort`` cannot be set for this adapter, or None."""
    if adapter_type not in EFFORT_FLAGS:
        return f"the {adapter_type} adapter has no effort setting"
    if effort not in EFFORT_LEVELS:
        return f"effort must be one of {', '.join(EFFORT_LEVELS)} (got {effort!r})"
    if adapter_type.startswith("codex") and effort == "max":
        return "Codex has no max effort: use low, medium, high or xhigh"
    return None
_CLAUDE_MODEL_ALIASES = ("best", "opus", "sonnet", "haiku", "opusplan")


def model_name_ok(adapter_type: str, name: str) -> bool:
    """Whether ``name`` is one model name for this adapter's CLI.

    The generic form (``MODEL_NAME``) covers Claude, Codex and Reasonix ids. Antigravity
    also takes the engine names ``agy models`` prints, which carry spaces and parentheses
    (``Gemini 3.1 Pro (High)``) -- still one name, never a flag or a second argument.
    """
    if adapter_type in _AGY_MODEL_TYPES and _AGY_MODEL.fullmatch(name):
        return True
    return bool(MODEL_NAME.fullmatch(name))


def model_problem(adapter_type: str, name: str) -> Optional[str]:
    """Why ``name`` is not a model this adapter's CLI runs, or None when it is.

    A runtime ``/model`` only switches within the bot's own CLI: Claude Code takes its
    aliases (opus, sonnet, haiku, best, opusplan, optionally with [1m]) and claude-* names;
    Codex takes gpt-*, o<digit>* and codex-* names. A typo is refused here rather than
    failing every later turn.
    """
    if not model_name_ok(adapter_type, name):
        return f"{name!r} is not one model name"
    lower = name.lower()
    if adapter_type.startswith("claude"):
        base = lower[:-4] if lower.endswith("[1m]") else lower
        if base in _CLAUDE_MODEL_ALIASES or base.startswith("claude-"):
            return None
        return (f"{name!r} is not a Claude model: use {', '.join(_CLAUDE_MODEL_ALIASES)} "
                "or a claude-* name")
    if adapter_type.startswith("codex"):
        if re.match(r"(gpt-|o\d|codex-)", lower):
            return None
        return f"{name!r} is not a Codex model: use a gpt-*, o3/o4-* or codex-* name"
    if adapter_type in _AGY_MODEL_TYPES:
        # An id or an engine name from `agy models`: gemini-3.8-flash-high,
        # 'Gemini 3.1 Pro (High)'. Case, spaces and parentheses are part of the name there.
        if model_name_ok(adapter_type, name):
            return None
        return (f"{name!r} is not an Antigravity model: see `agy models` "
                f"(e.g. gemini-3.8-flash-high or 'Gemini 3.1 Pro (High)')")
    if adapter_type == "reasonix":
        # A provider name from reasonix.toml (deepseek-flash, deepseek-pro, ...). Reasonix
        # names are lowercase; this refuses the other CLIs' names and obvious typos of case.
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name) and not re.match(
                r"(claude-|gpt-|o\d|codex-)", name) and name not in _CLAUDE_MODEL_ALIASES:
            return None
        return (f"{name!r} is not a Reasonix provider name: use one from reasonix.toml, "
                "e.g. deepseek-flash or deepseek-pro")
    return f"the {adapter_type} adapter has no model setting"


@dataclass
class AgentIdentity:
    """Who this agent is, on Telegram and everywhere else."""

    alias: str
    name: str                            # Telegram bot username
    bot_token: str = ""
    home_channel_id: Optional[int] = None
    admin_user_id: Optional[int] = None
    maintainer_username: Optional[str] = None  # optional: the human's @handle, used in routing prompts
    allowed_users: List[str] = field(default_factory=list)   # other people who may start turns
    git_name: Optional[str] = None       # commits are authored by the agent
    git_email: Optional[str] = None
    role: Optional[str] = None           # coordinator, developer, ... or a role file (roles/)

    def __post_init__(self) -> None:
        if not self.alias:
            raise ConfigError("agent.alias is required (e.g. 'Hermes')")
        if not self.name:
            raise ConfigError("agent.name is required (the bot username from @BotFather)")

    @property
    def git_identity(self) -> "tuple":
        """``(name, email)`` every commit this agent makes should carry.

        An agent authors its own commits — the human is not the author of work an agent did.
        The default is the alias plus a non-routable address; set ``agent.git_email`` to a
        GitHub machine account's noreply address if the commits should link to that account.
        """
        name = self.git_name or self.alias
        email = self.git_email or f"{self.alias.strip().lower()}@a2a.local"
        return name, email

    @property
    def username(self) -> str:
        return self.name if self.name.startswith("@") else f"@{self.name}"

    def token_is_present(self) -> bool:
        return bool(self.bot_token)


@dataclass
class AdapterSettings:
    """How incoming chat text is handed to the underlying agent runtime."""

    type: str = "echo"
    cwd: Optional[str] = None
    timeout_seconds: int = 900
    command: Optional[str] = None        # overrides the default template for the type
    extra_env: Dict[str, str] = field(default_factory=dict)
    max_output_chars: int = 3500
    enabled: bool = True
    conversation: Optional[str] = None   # optional conversation ID or title for stateful runtimes
    reply_state_path: Optional[str] = None  # codex_thread durable inbox/outbox; default beside config
    reply_timeout_seconds: int = 600
    permissions: Any = None              # strict | standard | full; None: default_permissions
    model: Optional[str] = None          # codex / claude: the model to run (-m / --model)
    effort: Optional[str] = None         # reasoning effort: low | medium | high | xhigh | max
    allow_commands: List[str] = field(default_factory=list)  # claude standard: more commands
    #: Review P0-6: variables the runtime gets although their names look like secrets
    #: (see SECRET_ENV in adapters/base.py), e.g. a doorbell token Codex hands to its MCP server.
    pass_env: List[str] = field(default_factory=list)
    #: Fetch and fast-forward the checkout before each turn, from outside the agent's
    #: sandbox (git_sync.py). None: on for every runtime (agents rarely pull on their own).
    git_sync: Optional[bool] = None
    #: Input tokens a topic's resumed session may reach before the next turn starts a new
    #: one from the topic's recap (task, progress, latest). None: the runtime's default
    #: (Codex: 10M); 0: never.
    session_token_limit: Optional[int] = None
    session_turn_limit: Optional[int] = None

    def __post_init__(self) -> None:
        names = self.pass_env or []
        if isinstance(names, str) or not isinstance(names, (list, tuple)) or not all(
                isinstance(n, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n.strip())
                for n in names):
            raise ConfigError("adapter.pass_env must be a list of environment variable names")
        self.pass_env = [n.strip() for n in names]
        commands = self.allow_commands or []
        if isinstance(commands, str) or not isinstance(commands, (list, tuple)):
            raise ConfigError("adapter.allow_commands must be a list of command names")
        for cmd in commands:
            if not isinstance(cmd, str):   # YAML null / 7 / true: not a command name
                raise ConfigError(f"adapter.allow_commands: {cmd!r} is not one command name")
        self.allow_commands = [cmd.strip() for cmd in commands if cmd.strip()]
        for cmd in self.allow_commands:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cmd):
                raise ConfigError(f"adapter.allow_commands: {cmd!r} is not one command name")
        if self.effort is not None:
            self.effort = str(self.effort).strip().lower() or None
            problem = self.effort and effort_problem(self.type, self.effort)
            if problem:
                raise ConfigError(f"adapter.effort: {problem}")
        if self.model is not None:
            self.model = str(self.model).strip() or None
            if self.model and not model_name_ok(self.type, self.model):
                raise ConfigError(f"adapter.model must be one model name (got {self.model!r})")
        # Only a missing key means the default: YAML reads off/no/false/0 as a bool or number,
        # and a config that reads "turn it off" must not silently get standard.
        if self.permissions is None:
            self.permissions = default_permissions(self.type)
        if not isinstance(self.permissions, str):
            raise ConfigError(f"adapter.permissions must be one of {', '.join(PERMISSION_LEVELS)} "
                              f"(got {self.permissions!r}; quote it in YAML)")
        self.permissions = self.permissions.strip().lower()
        if self.permissions not in PERMISSION_LEVELS:
            raise ConfigError(f"adapter.permissions must be one of {', '.join(PERMISSION_LEVELS)} "
                              f"(got {self.permissions!r})")

    @property
    def syncs_git(self) -> bool:
        """Whether the transport keeps this agent's checkout current (``git_sync``)."""
        if self.git_sync is not None:
            return bool(self.git_sync)
        return self.type != "echo" and not self.command

    @property
    def model_switchable(self) -> bool:
        """Whether the model is set by this config (codex, claude, claude_thread with the
        default command), so ``/model`` can change it; an explicit adapter.command wins."""
        return not self.command and self.type in MODEL_TYPES

    def command_template(self, model: Optional[str] = None,
                         effort: Optional[str] = None) -> str:
        """The command line to run, with ``model`` (a runtime ``/model`` choice) in place of
        adapter.model. An explicit adapter.command wins and ignores permissions and models."""
        if self.command:
            return self.command
        if self.type not in DEFAULT_COMMANDS:
            raise ConfigError(
                f"unknown adapter type '{self.type}'; known types: {sorted(DEFAULT_COMMANDS)}. "
                f"Provide adapter.command to use a custom command line.")
        template = DEFAULT_COMMANDS[self.type]
        if not template:
            raise ConfigError(f"adapter type '{self.type}' has no command template")
        flags = PERMISSION_FLAGS.get(self.type, {}).get(self.permissions, "")
        if self.allow_commands and self.permissions == "standard" \
                and self.type.startswith("claude"):
            extra = [f"{shell}({cmd}:*)" for cmd in self.allow_commands
                     for shell in ("Bash", "PowerShell")]
            flags = flags.replace(_CLAUDE_STANDARD_TOOLS,
                                  ",".join([_CLAUDE_STANDARD_TOOLS, *extra]))
        # A runtime choice comes from a state file the agent may be able to write: one that
        # is not a single model name must never reach the command line.
        if model is not None and not model_name_ok(self.type, model):
            model = None
        model = model or self.model
        model_flag = ""
        if model:
            if not MODEL_NAME.fullmatch(model):
                # Antigravity engine names carry spaces: 'Gemini 3.1 Pro (High)' must reach
                # `agy` as ONE argv token, not five (issue #10).
                model = f"'{model}'"
            model_flag = f"-m {model}" if self.type.startswith("codex") else f"--model {model}"
        # A runtime /reasoning choice comes from a state file too: only a known level.
        if effort is not None and effort_problem(self.type, effort):
            effort = None
        effort = effort or self.effort
        effort_flag = ""
        if effort and self.type in EFFORT_FLAGS:
            effort_flag = EFFORT_FLAGS[self.type].format(effort=effort)
        template = (template.replace("{permissions}", flags).replace("{model}", model_flag)
                    .replace("{effort}", effort_flag))
        return " ".join(template.split())


BUSY_BEHAVIORS = ("queue", "drop", "notice")
MULTI_MENTIONS = ("first", "all")
#: ``limits.strangers``: whether a person other than the admin, the maintainer and
#: ``agent.allowed_users``, or a bot that is not a peer, can start a turn. A bot runs
#: commands on its machine (adapter.permissions), so by default they cannot.
STRANGERS = ("ignore", "answer")
#: Adapters whose every turn continues one shared conversation: no parallel topics.
SINGLE_SESSION_ADAPTERS = ("antigravity", "antigravity_thread", "codex_thread")
#: Adapters that never touch the working directory: parallel topics are safe without
#: ``limits.allow_shared_cwd``.
NO_FILES_ADAPTERS = ("echo",)
MAX_PARALLEL_TURNS = 8
#: Most parts one reply may be split into, shared by the sender (`split_outgoing`) and the
#: receiver's reassembly (`split_part`), so whatever a transport emits, the other accepts. The
#: default is 4; a larger configured value is clamped here rather than refused at startup.
MAX_REPLY_PARTS = 20


@dataclass
class ChannelLimits:
    """Pacing and size limits — by default sends at least 3 s apart (per topic when topics
    run in parallel, with the whole bot under Telegram's group limit)."""

    max_message_chars: int = 4096
    min_seconds_between_messages: float = 3.0            # at least 3s apart (per topic at N>1)
    max_messages_per_minute: int = 18                   # max 18 msg/min (per topic at N>1)
    max_group_messages_per_minute: int = 1              # Backward-compatible alias
    max_agent_turns: int = 6                            # deprecated no-op: guard removed; loop safety is routing (PROTOCOL.md §8)
    respond_without_mention: bool = False
    require_admin_for_broadcast: bool = True
    min_seconds_between_agent_turns: float = 60.0       # bot-to-bot cooldown, General only (topics: peer cap)
    send_loop_notice: bool = False                      # Silently quiet by default; do not post alert to channel
    ack_ttl_seconds: float = 1800.0                     # v1.1 §5: REQUEST ack deadline before the escalation ladder
    commands_enabled: bool = True                       # v1.1 §6: answer /ping /status /diag without waking the adapter
    auto_freeze_on_quota: bool = True                   # #49: self-freeze after repeated quota-signature adapter failures
    quota_freeze_threshold: int = 3                     # consecutive quota failures before the auto-freeze
    quota_retry_minutes: float = 60.0                   # a quota freeze whose error gives no reset time ends after this
    overdue_alert_cap: int = 2                          # #49: max overdue alerts per ticket
    overdue_digest_threshold: int = 3                   # #49: one digest alert when this many tickets are open for one recipient
    busy_behavior: str = "queue"                        # addressed msg during an in-flight turn: "queue" (run it next), "drop" (silent) or "notice" (drop, one log line/window)
    busy_notice_window_seconds: float = 900.0           # #9: at most one busy-drop notice per window (15 min)
    busy_queue_max: int = 12                            # "queue": messages waiting behind the current turn; more are dropped
    max_parallel_turns: int = 1                         # forum topics running a turn at once
    allow_shared_cwd: bool = False                      # parallel topics share adapter.cwd
    max_peer_turns_per_topic: int = 8                   # peer turns in a row per forum topic
    work_in_topics: bool = True                         # General is for setup; work moves to a topic
    handoff_nudge_minutes: float = 10.0                 # a hand-off with no word from its bot: remind it, then the admin; 0 = off
    max_reply_parts: int = 4                            # a long reply is split into at most this many messages
    #: Issue #31: the channel's reply budget, in characters (the swarm's 500-char convention).
    #: A project may override it per topic (``projects.<key>.reply_chars``); everything else
    #: -- General, unbound topics, the reply tickets -- uses this.
    reply_chars: int = 500
    typing_indicator: bool = True                       # show "typing…" in the chat while a turn runs
    ack_reaction: bool = True                           # 👀 on a person's message when a turn starts
    max_attachment_mb: float = 20.0                     # incoming files larger than this are not downloaded
    selftest_seconds: float = 180.0                     # startup check that bots see each other; 0 = off
    multi_mention: str = "first"                        # a person names several bots: "first" or "all" act
    strangers: str = "ignore"                           # outside the swarm: "ignore" or "answer"
    #: Issue #63: ``/cleanup topics done`` deletes a topic whose linked issue or PR is closed
    #: or merged only once it has been idle this many hours. 0 offers it as soon as the item
    #: is done.
    cleanup_idle_hours: float = 24.0
    #: Topic ids ``/cleanup topics all`` must never delete (General is always kept).
    cleanup_keep: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.busy_behavior not in BUSY_BEHAVIORS:
            raise ConfigError(
                f"limits.busy_behavior must be one of {sorted(BUSY_BEHAVIORS)} "
                f"(got {self.busy_behavior!r}).")
        if self.strangers not in STRANGERS:
            raise ConfigError(
                f"limits.strangers must be one of {list(STRANGERS)} (got {self.strangers!r}).")
        if self.multi_mention not in MULTI_MENTIONS:
            raise ConfigError(
                f"limits.multi_mention must be one of {list(MULTI_MENTIONS)} "
                f"(got {self.multi_mention!r}).")
        attachment_mb = self.max_attachment_mb
        if (isinstance(attachment_mb, bool) or not isinstance(attachment_mb, (int, float))
                or not math.isfinite(attachment_mb) or attachment_mb <= 0):
            raise ConfigError("limits.max_attachment_mb must be a positive number of megabytes "
                              f"(got {attachment_mb!r}).")
        for key in ("min_seconds_between_messages", "min_seconds_between_agent_turns",
                    "handoff_nudge_minutes", "cleanup_idle_hours"):
            value = getattr(self, key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                unit = ("minutes" if key.endswith("_minutes")
                        else "hours" if key.endswith("_hours") else "seconds")
                raise ConfigError(f"limits.{key} must be a number of {unit}, 0 or more "
                                  f"(got {value!r}).")
        rate = self.max_messages_per_minute     # 0: use max_group_messages_per_minute
        if isinstance(rate, bool) or not isinstance(rate, int) or rate < 0:
            raise ConfigError("limits.max_messages_per_minute must be a whole number, 0 for "
                              f"max_group_messages_per_minute (got {rate!r}).")
        for key in ("busy_queue_max", "max_reply_parts", "max_parallel_turns",
                    "reply_chars", "max_group_messages_per_minute"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"limits.{key} must be a whole number of at least 1 "
                                  f"(got {value!r}).")
        # Issue #31: a reply budget above one Telegram message cannot be delivered as one
        # message, so it is refused rather than promised to the agent and then split. The
        # *default* is not an operated setting: a host that lowered max_message_chars below
        # 500 keeps loading, with the budget clamped to what one message can hold (review).
        if self.reply_chars > self.max_message_chars:
            if self.reply_chars == ChannelLimits.__dataclass_fields__["reply_chars"].default:
                logger.warning("limits.reply_chars defaults to 500, above max_message_chars=%s; "
                               "using %s", self.max_message_chars, self.max_message_chars)
                self.reply_chars = self.max_message_chars
            else:
                raise ConfigError(f"limits.reply_chars must be at most max_message_chars "
                                  f"({self.max_message_chars}); got {self.reply_chars}")
        # #29 review: a reply is split into at most MAX_REPLY_PARTS parts, and the receiver
        # refuses a part header above that. Clamping here keeps the two ends agreeing: a larger
        # configured value loses the tail to the existing truncation instead of having every
        # part after the cap ignored by the receiver.
        if self.max_reply_parts > MAX_REPLY_PARTS:
            logger.warning("limits.max_reply_parts=%s is above the %s-part protocol cap; using %s",
                           self.max_reply_parts, MAX_REPLY_PARTS, MAX_REPLY_PARTS)
            self.max_reply_parts = MAX_REPLY_PARTS
        cap = self.max_peer_turns_per_topic
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ConfigError("limits.max_peer_turns_per_topic must be a whole number, 0 for "
                              f"no cap (got {cap!r}).")
        if self.max_parallel_turns > MAX_PARALLEL_TURNS:
            raise ConfigError(f"limits.max_parallel_turns must be at most {MAX_PARALLEL_TURNS} "
                              f"(got {self.max_parallel_turns!r}).")
        # #93(2): validate the notice window at load time. An unusable value used to be accepted
        # here and only fail at drop time, inside the inbound path *after* stats.ignored,
        # stats.busy_dropped and _busy_drops_since_notice had already been bumped -- so the drop
        # was counted, the handler raised instead of returning, and the notice never emitted.
        # A config that refuses to start must not also have half-recorded a drop.
        # The dataclass takes numbers: `from_dict` coerces the YAML value first, so a string
        # reaching here is a programming error, not an operator typo.
        window_raw = self.busy_notice_window_seconds
        if isinstance(window_raw, bool) or not isinstance(window_raw, (int, float)):
            raise ConfigError("limits.busy_notice_window_seconds must be a number of seconds "
                              f"(got {window_raw!r}).")
        window = float(window_raw)
        if not math.isfinite(window):
            # inf makes `elapsed >= window` never true (notice mode silently dead); nan makes
            # every comparison false (same). Both disable the feature from the inside.
            raise ConfigError("limits.busy_notice_window_seconds must be a finite number of "
                              f"seconds (got {window_raw!r}).")
        if window <= 0:
            # Correctness inversion, not a missing bound: with `now - last >= window` true on
            # every drop, N drops fire N notices -- the one-notice-per-drop flood this feature
            # exists to prevent.
            raise ConfigError("limits.busy_notice_window_seconds must be a positive number of "
                              f"seconds (got {window_raw!r}); a window <= 0 would notify on "
                              "every drop.")


@dataclass
class FallbackSettings:
    """Durable-plane fallback: poll the git bus for messages when Telegram delivery fails.

    ``FallbackSettings`` is the legacy name and is preserved so older configs continue to
    load unchanged. The first-class :class:`BusSettings` (``bus:`` config section) supersedes
    it as of issue #59; when both are present the ``bus:`` keys win, and the resolved values
    are mirrored back into ``fallback:`` so any code that still reaches for the legacy fields
    sees the same numbers.
    """

    gitbus_poll_minutes: int = 10        # 0 = disabled; otherwise poll every N minutes
    gitbus_root: Optional[str] = None    # checkout whose .agents/inbox is polled; unset: no polling
    gitbus_ref: str = "origin/main"      # ref read for the freshest pushed view (fetch-only refresh)


#: Issue #80: settle window (seconds) between an injection and the first
#: ``verify_delivery`` probe. Adapter sessions show a just-delivered message
#: asynchronously, so probing immediately after ``respond()`` races the session
#: and fabricates a clean negative. Config-declared under the poller's ``bus:``
#: section; the poller clamps the resolved value to ``MAX_VERIFY_SETTLE_SECONDS``.
DEFAULT_VERIFY_SETTLE_SECONDS = 3.0
MAX_VERIFY_SETTLE_SECONDS = 30.0


@dataclass
class BusSettings:
    """First-class bus poller configuration (issue #59).

    The durable-plane fallback is now expressed as a top-level ``bus:`` section; older
    ``fallback.gitbus_*`` keys remain accepted as aliases. ``enabled`` is the
    canonical disable switch (``false`` here is equivalent to ``poll_minutes=0``);
    the ``repo`` path is resolved at runtime against the configured checkout.
    """

    repo: Optional[str] = None           # checkout whose .agents/inbox is polled; unset: no polling
    ref: str = "origin/main"             # ref read for the freshest pushed view (fetch-only refresh)
    poll_minutes: int = 30               # 0 = disabled; otherwise poll every N minutes
    enabled: bool = True
    #: Settle window before the delivery-verify probe (issue #80); 0 disables it.
    verify_settle_seconds: float = DEFAULT_VERIFY_SETTLE_SECONDS
    #: Where envelope files live: ``git`` (default), ``folder`` or ``gdrive``
    #: (see :mod:`swarmly.transports`). ``repo`` stays the home of local state either way.
    transport: str = "git"
    folder: Optional[str] = None         # transport: folder -- the synced/shared bus folder
    gdrive: Dict[str, Any] = field(default_factory=dict)  # transport: gdrive -- folder_id, credentials
    #: a2a/2 swarm lanes: path to swarm.yaml. When set, each poll cycle also delivers
    #: the signed lane envelopes addressed to ``swarm_agent`` (default: the alias).
    swarm: Optional[str] = None
    #: The transport writes every hand-off (a reply whose line opens with a peer's @handle)
    #: to the bus too, so a peer that missed the Telegram copy still gets the work.
    mirror_handoffs: bool = True
    swarm_agent: Optional[str] = None
    #: Review P0-1: only bus messages whose signature verifies against a registered key
    #: reach the agent. ``true`` turns that off (a local demo bus without keys).
    allow_unsigned: bool = False
    #: Optional allowlist of bus sender ids; empty accepts every verified sender.
    senders: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"repo": self.repo, "ref": self.ref,
                "poll_minutes": self.poll_minutes, "enabled": self.enabled,
                "verify_settle_seconds": self.verify_settle_seconds,
                "transport": self.transport, "folder": self.folder,
                "gdrive": dict(self.gdrive), "swarm": self.swarm,
                "swarm_agent": self.swarm_agent,
                "allow_unsigned": self.allow_unsigned, "senders": list(self.senders)}


@dataclass
class ProjectSettings:
    key: str
    repo: str                       # "owner/name"
    cwd: str
    topics: List[Tuple[Optional[int], int]]   # (chat_id or None for home group, thread_id)
    worktrees: bool = False
    #: Issue #31: this project's topics get this reply budget instead of ``limits.reply_chars``.
    #: None (the default) keeps the global one, so a config without it behaves as before.
    reply_chars: Optional[int] = None


@dataclass
class GitHubSettings:
    login: str
    token_env: str = "GH_TOKEN"
    poll_minutes: int = 5
    mark_read: bool = True
    #: The label that marks a tracking issue (round end): closing one wakes the coordinator
    #: to post the round report (issue #25). "" disables the watch.
    tracking_label: str = "tracking"


@dataclass
class AgentConfig:
    """The whole per-agent configuration."""

    identity: AgentIdentity
    peers: Dict[str, str] = field(default_factory=dict)      # alias -> "username" | "user_id"
    peer_roles: Dict[str, str] = field(default_factory=dict)  # alias -> role name, when given
    adapter: AdapterSettings = field(default_factory=AdapterSettings)
    limits: ChannelLimits = field(default_factory=ChannelLimits)
    fallback: FallbackSettings = field(default_factory=FallbackSettings)
    bus: BusSettings = field(default_factory=BusSettings)
    projects: Dict[str, ProjectSettings] = field(default_factory=dict)
    github: Optional[GitHubSettings] = None
    #: Issue #22: literal -> replacement, applied to everything that leaves the host (bus
    #: payloads and GitHub-facing text). Empty by default: nothing is redacted until the
    #: operator names what is sensitive on this machine.
    public_redactions: Dict[str, str] = field(default_factory=dict)
    source_path: Optional[Path] = None
    # Whether ``bus.poll_minutes`` was explicitly present in the raw config.
    # Validation must name the key the operator actually wrote — the resolved
    # value is mirrored into ``fallback`` (review N1, PR #60 round 3).
    _bus_poll_minutes_explicit: bool = field(default=False, repr=False, compare=False)
    #: Review P1-7: keys in the raw config that nothing reads (see :func:`unknown_keys`),
    #: and values that are read but ignored (P1-8: handles in ``allowed_users``).
    unknown_keys: List[str] = field(default_factory=list, repr=False, compare=False)

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_path: Optional[Path] = None) -> "AgentConfig":
        if source_path:
            load_env([source_path.parent])
        else:
            load_env()

        agent = data.get("agent") or {}
        if not isinstance(agent, dict):
            raise ConfigError("'agent' must be a mapping")

        token = str(agent.get("bot_token") or "").strip()
        token_env = agent.get("bot_token_env")
        if not token and token_env:
            token = os.environ.get(str(token_env), "")
        if not token and not token_env:
            # fall back to a per-alias environment variable
            alias = str(agent.get("alias") or "").strip().upper()
            token = os.environ.get(f"A2A_TELEGRAM_BOT_TOKEN_{alias}", "") if alias else ""

        identity = AgentIdentity(
            alias=str(agent.get("alias") or "").strip(),
            name=str(agent.get("name") or "").strip(),
            bot_token=token,
            home_channel_id=_chat_id(agent.get("home_channel_id")),
            admin_user_id=_as_int(agent.get("admin_user_id")),
            maintainer_username=(agent.get("maintainer_username") or None),
            allowed_users=[str(u).strip().lstrip("@").lower()
                           for u in (agent.get("allowed_users") or []) if str(u).strip()],
            git_name=(str(agent.get("git_name")).strip() if agent.get("git_name") else None),
            git_email=(str(agent.get("git_email")).strip() if agent.get("git_email") else None),
            role=(str(agent.get("role")).strip() or None) if agent.get("role") else None,
        )

        adapter_raw = data.get("adapter") or {}
        adapter = AdapterSettings(
            type=str(adapter_raw.get("type") or "echo").strip().lower(),
            cwd=adapter_raw.get("cwd"),
            timeout_seconds=_parse_int("adapter.timeout_seconds",
                                       adapter_raw.get("timeout_seconds"), 900, positive=True),
            command=adapter_raw.get("command"),
            extra_env={str(k): str(v) for k, v in (adapter_raw.get("extra_env") or {}).items()},
            max_output_chars=_parse_int("adapter.max_output_chars",
                                        adapter_raw.get("max_output_chars"), 3500, positive=True),
            enabled=_parse_bool("adapter.enabled", adapter_raw.get("enabled", True)),
            conversation=(str(adapter_raw.get("conversation") or adapter_raw.get("conversation_id") or "").strip() or None),
            reply_state_path=adapter_raw.get("reply_state_path"),
            reply_timeout_seconds=_parse_int("adapter.reply_timeout_seconds",
                                             adapter_raw.get("reply_timeout_seconds"), 600),
            permissions=adapter_raw.get("permissions"),
            model=adapter_raw.get("model"),
            effort=adapter_raw.get("effort"),
            allow_commands=adapter_raw.get("allow_commands") or [],
            pass_env=adapter_raw.get("pass_env") or [],
            git_sync=(None if adapter_raw.get("git_sync") is None
                      else _parse_bool("adapter.git_sync", adapter_raw.get("git_sync"))),
            session_token_limit=(None if adapter_raw.get("session_token_limit") is None
                                 else _parse_int("adapter.session_token_limit",
                                                 adapter_raw.get("session_token_limit"), 0)),
            session_turn_limit=(None if adapter_raw.get("session_turn_limit") is None
                                else _parse_int("adapter.session_turn_limit",
                                                adapter_raw.get("session_turn_limit"), 0)),
        )

        limits_raw = data.get("limits") or {}
        # #93(2): coerce each declared limit to its default's type, but name the offending key on
        # failure. The bare comprehension let `limits.busy_notice_window_seconds: abc` escape as
        # `ValueError: could not convert string to float: 'abc'` -- no key, no section, and only
        # at drop time. `ChannelLimits.__post_init__` then enforces the domain rules.
        _limit_defaults = ChannelLimits()
        _limits_coerced = {}
        for key, value in limits_raw.items():
            if not hasattr(ChannelLimits, key):
                continue
            default = getattr(_limit_defaults, key)
            if isinstance(default, bool):
                # Issue #109: bool fields get an explicit parser; never bool(value).
                _limits_coerced[key] = _coerce_bool_limit(key, value)
                continue
            if isinstance(default, (list, tuple)):
                # Issue #63: limits.cleanup_keep is a list of topic ids, read as one.
                _limits_coerced[key] = _coerce_int_list(key, value)
                continue
            try:
                if isinstance(value, bool):          # review P1-7: YAML true is not 1
                    raise TypeError(key)
                _limits_coerced[key] = type(default)(value)
            except (TypeError, ValueError):
                # #110(nit 5): human words, not type-name jargon ("a int").
                if isinstance(default, int):
                    kind = "an integer"
                elif isinstance(default, float):
                    kind = "a number"
                else:
                    kind = f"a {type(default).__name__}"
                raise ConfigError(f"limits.{key} must be {kind} (got {value!r})")
        limits = ChannelLimits(**_limits_coerced)
        # Issue #31: the clamp above is for the *default* budget meeting a lowered
        # max_message_chars. A budget the operator wrote down is not clamped -- an explicit
        # number that cannot be delivered is a config error, not something to silently move.
        if "reply_chars" in limits_raw and limits.reply_chars != _limits_coerced.get("reply_chars"):
            raise ConfigError(f"limits.reply_chars must be at most max_message_chars "
                              f"({limits.max_message_chars}); got {limits_raw['reply_chars']}")

        # A peer is "username" (or a numeric user id), or {username: ..., role: ...}.
        peers: Dict[str, str] = {}
        peer_roles: Dict[str, str] = {}
        for alias, target in (data.get("peers") or {}).items():
            if isinstance(target, dict):
                extra = sorted(set(target) - {"username", "role"})
                if extra:
                    raise ConfigError(f"peers.{alias}: unknown keys {extra}; a peer takes "
                                      "username and role")
                peers[str(alias)] = str(target.get("username") or "").strip()
                if target.get("role"):
                    peer_roles[str(alias)] = str(target["role"]).strip()
            else:
                peers[str(alias)] = str(target)

        fallback_raw = data.get("fallback") or {}
        bus_raw = data.get("bus") or {}

        # Issue #59: ``bus:`` is the first-class section; ``fallback.gitbus_*`` is accepted as
        # an alias. ``bus:`` always wins when both are present. The resolved values are
        # mirrored back into ``fallback:`` so older code paths that still read it see the
        # same numbers.
        def _as_int_strict(value: Any, where: str) -> Optional[int]:
            if value is None or value == "":
                return None
            text = str(value).strip()
            if text.startswith("<") and text.endswith(">"):
                return None
            try:
                return int(text)
            except (TypeError, ValueError):
                raise ConfigError(f"{where} must be an integer (got {value!r})")

        repo = bus_raw.get("repo")
        if repo is None:
            repo = fallback_raw.get("gitbus_root")
        repo = (str(repo).strip() if repo not in (None, "") else None)

        ref_raw = bus_raw.get("ref")
        if ref_raw is None:
            ref_raw = fallback_raw.get("gitbus_ref")
        ref = (str(ref_raw or "origin/main").strip() or "origin/main")

        bus_poll_explicit = bus_raw.get("poll_minutes") is not None
        bus_poll_raw = bus_raw.get("poll_minutes")
        if bus_poll_raw is None:
            bus_poll_raw = fallback_raw.get("gitbus_poll_minutes")
        poll_minutes = _as_int_strict(
            bus_poll_raw if bus_poll_raw is not None else 30,
            "bus.poll_minutes",
        )
        if poll_minutes is None:
            poll_minutes = 30

        # Issue #80: settle window before the delivery-verify probe. Declared
        # under ``bus:``; the poller clamps it to MAX_VERIFY_SETTLE_SECONDS.
        settle_raw = bus_raw.get("verify_settle_seconds")
        if settle_raw in (None, ""):
            verify_settle_seconds = DEFAULT_VERIFY_SETTLE_SECONDS
        else:
            try:
                verify_settle_seconds = float(settle_raw)
            except (TypeError, ValueError):
                raise ConfigError("bus.verify_settle_seconds must be a number "
                                  f"of seconds (got {settle_raw!r})")

        bus_enabled_raw = bus_raw.get("enabled")
        if bus_enabled_raw is None:
            # No explicit flag in either section: enabled iff poll_minutes != 0.
            bus_enabled_raw = (poll_minutes != 0)

        transport_kind = str(bus_raw.get("transport") or "git").strip().lower()
        folder_raw = bus_raw.get("folder")
        bus_folder = (os.path.expandvars(os.path.expanduser(str(folder_raw).strip()))
                      if folder_raw not in (None, "") else None)
        gdrive_raw = bus_raw.get("gdrive") or {}
        if not isinstance(gdrive_raw, dict):
            raise ConfigError("bus.gdrive must be a mapping (folder_id, credentials)")
        bus_gdrive = {str(k): (str(v).strip() if v is not None else None)
                      for k, v in gdrive_raw.items()}

        swarm_raw = bus_raw.get("swarm")
        bus_swarm = (os.path.expandvars(os.path.expanduser(str(swarm_raw).strip()))
                     if swarm_raw not in (None, "") else None)
        swarm_agent_raw = bus_raw.get("swarm_agent")
        bus_swarm_agent = (str(swarm_agent_raw).strip().lower()
                           if swarm_agent_raw not in (None, "") else None)

        allow_unsigned = bus_raw.get("allow_unsigned", False)
        if not isinstance(allow_unsigned, bool):
            raise ConfigError("bus.allow_unsigned must be true or false "
                              f"(got {allow_unsigned!r})")
        senders_raw = bus_raw.get("senders") or []
        if not isinstance(senders_raw, list) or not all(
                isinstance(item, str) and item.strip() for item in senders_raw):
            raise ConfigError("bus.senders must be a list of agent ids")
        bus_senders = [item.strip().lower() for item in senders_raw]

        bus = BusSettings(
            repo=repo,
            ref=ref,
            poll_minutes=int(poll_minutes),
            enabled=_parse_bool("bus.enabled", bus_enabled_raw),
            verify_settle_seconds=verify_settle_seconds,
            transport=transport_kind,
            folder=bus_folder,
            gdrive=bus_gdrive,
            swarm=bus_swarm,
            swarm_agent=bus_swarm_agent,
            allow_unsigned=allow_unsigned,
            senders=bus_senders,
            mirror_handoffs=_parse_bool("bus.mirror_handoffs",
                                        bus_raw.get("mirror_handoffs", True)),
        )
        fallback = FallbackSettings(
            gitbus_poll_minutes=int(poll_minutes),
            gitbus_root=repo,
            gitbus_ref=ref,
        )

        if limits.max_parallel_turns > 1 and adapter.type in SINGLE_SESSION_ADAPTERS:
            raise ConfigError(
                f"limits.max_parallel_turns > 1 needs an adapter that keeps one session per "
                f"discussion (or none); {adapter.type!r} keeps a single conversation, so "
                "parallel turns would interleave in it. Use 1.")
        if (limits.max_parallel_turns > 1 and adapter.type not in NO_FILES_ADAPTERS
                and not limits.allow_shared_cwd):
            raise ConfigError(
                f"limits.max_parallel_turns > 1 runs several {adapter.type!r} turns in the same "
                f"working directory ({adapter.cwd or '.'}), where they can overwrite each "
                "other's edits. Set limits.allow_shared_cwd: true to accept that, or use 1.")
        projects_dict: Dict[str, ProjectSettings] = {}
        projects_raw = data.get("projects") or {}
        if not isinstance(projects_raw, dict):
            raise ConfigError("projects must be a mapping of project key -> settings "
                              f"(got {type(projects_raw).__name__})")
        if isinstance(projects_raw, dict):
            for pkey, pval in projects_raw.items():
                if not isinstance(pval, dict):
                    raise ConfigError(f"projects.{pkey} must be a mapping with repo, cwd "
                                      "and topics")
                repo = str(pval.get("repo") or "").strip()
                cwd_raw = pval.get("cwd")
                cwd = str(cwd_raw).strip() if cwd_raw is not None else ""
                worktrees = _parse_bool(f"projects.{pkey}.worktrees", pval.get("worktrees", False))
                reply_chars = None
                if pval.get("reply_chars") is not None and pval.get("reply_chars") != "":
                    reply_chars = _parse_int(f"projects.{pkey}.reply_chars",
                                             pval.get("reply_chars"), 0, positive=True)
                    if reply_chars > limits.max_message_chars:
                        raise ConfigError(
                            f"projects.{pkey}.reply_chars must be at most max_message_chars "
                            f"({limits.max_message_chars}); got {reply_chars}")
                raw_topics = pval.get("topics")
                parsed_topics: List[Tuple[Optional[int], Any]] = []
                if isinstance(raw_topics, list):
                    for item in raw_topics:
                        if isinstance(item, int) and not isinstance(item, bool):
                            parsed_topics.append((None, item))
                        elif isinstance(item, str):
                            item_str = item.strip()
                            if "/" in item_str:
                                parts = item_str.split("/", 1)
                                try:
                                    cid = int(parts[0].strip())
                                    tid = int(parts[1].strip())
                                    parsed_topics.append((cid, tid))
                                except ValueError:
                                    parsed_topics.append((None, item))
                            else:
                                try:
                                    tid = int(item_str)
                                    parsed_topics.append((None, tid))
                                except ValueError:
                                    parsed_topics.append((None, item))
                        else:
                            parsed_topics.append((None, item))
                elif raw_topics is not None:
                    parsed_topics.append((None, raw_topics))
                projects_dict[str(pkey)] = ProjectSettings(
                    key=str(pkey),
                    repo=repo,
                    cwd=cwd,
                    topics=parsed_topics,  # type: ignore[arg-type]
                    worktrees=bool(worktrees),
                    reply_chars=reply_chars,
                )

        github_raw = data.get("github")
        github: Optional[GitHubSettings] = None
        if isinstance(github_raw, dict):
            glogin = str(github_raw.get("login") or "").strip()
            gtoken_env = str(github_raw.get("token_env") or "GH_TOKEN").strip()
            gpoll = _parse_int("github.poll_minutes", github_raw.get("poll_minutes"), 5)
            gmark = _parse_bool("github.mark_read", github_raw.get("mark_read", True))
            glabel = str(github_raw.get("tracking_label", "tracking") or "").strip()
            github = GitHubSettings(
                login=glogin,
                token_env=gtoken_env,
                poll_minutes=gpoll,
                mark_read=gmark,
                tracking_label=glabel,
            )
        elif github_raw is not None:
            raise ConfigError("'github' must be a mapping")

        # Issue #22: what must not leave this host. The literal is the sensitive value
        # (a handle, a path); the replacement is what the bus and GitHub text carry instead.
        redactions_raw = data.get("public_redactions") or {}
        if not isinstance(redactions_raw, dict):
            raise ConfigError("'public_redactions' must be a mapping of literal -> replacement")
        public_redactions: Dict[str, str] = {}
        for literal, replacement in redactions_raw.items():
            text = str(literal)
            if not text.strip():
                raise ConfigError("public_redactions: an empty key cannot be redacted")
            public_redactions[text] = str(replacement)

        unknown = unknown_keys(data)
        # Review P1-8: only numeric user ids authorise a person.
        unknown += [f"agent.allowed_users: {name!r} is a handle, ignored: list the person's "
                    "numeric user id instead" for name in identity.allowed_users
                    if not name.lstrip("-").isdigit()]
        for line in unknown:
            logger.warning("config: %s", line)
        return cls(identity=identity, peers=peers, peer_roles=peer_roles, adapter=adapter, limits=limits,
                   fallback=fallback, bus=bus, projects=projects_dict, github=github,
                   public_redactions=public_redactions, source_path=source_path,
                   _bus_poll_minutes_explicit=bool(bus_poll_explicit),
                   unknown_keys=unknown)

    # -- loading -----------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "AgentConfig":
        config_path = Path(path).expanduser()
        if not config_path.exists():
            raise ConfigError(f"config not found: {config_path}")
        text = config_path.read_text(encoding="utf-8")
        if config_path.suffix in (".yaml", ".yml"):
            if not HAS_YAML:
                raise ConfigError("PyYAML is required to read config.yaml (pip install pyyaml)")
            data = yaml.safe_load(text) or {}
        else:
            import json
            data = json.loads(text)
        if not isinstance(data, dict):
            raise ConfigError("config root must be a mapping")
        config = cls.from_dict(data, source_path=config_path)
        from swarmly.telegram.roster import apply_roster
        apply_roster(config)                 # bots the admin admitted at runtime (#152)
        from swarmly.telegram.membership import apply_swarm_state
        apply_swarm_state(config)            # the role and team the swarm agreed on
        return config

    # -- validation --------------------------------------------------------------------
    def validate(self, require_token: bool = True) -> List[str]:
        """Return a list of human-readable problems; empty means the config is usable."""
        problems: List[str] = []
        if not self.identity.home_channel_id:
            problems.append("agent.home_channel_id is required — the group all bots talk in "
                            "(`swarmly-telegram discover` lists it)")
        if not self.identity.admin_user_id:
            problems.append("agent.admin_user_id is required: your Telegram user id, not 0")
        if require_token and not self.identity.token_is_present():
            problems.append(
                f"no bot token: set agent.bot_token, agent.bot_token_env, or "
                f"A2A_TELEGRAM_BOT_TOKEN_{self.identity.alias.upper()} in the environment")
        if self.adapter.enabled and self.adapter.type in ("codex_thread", "antigravity_thread"):
            if self.adapter.type == "codex_thread" and self.adapter.reply_timeout_seconds <= 0:
                problems.append("adapter.reply_timeout_seconds must be positive")
            if self.adapter.type == "codex_thread" and not self.adapter.conversation:
                problems.append(
                    "adapter.type 'codex_thread' requires adapter.conversation containing "
                    "a Codex session UUID or exact session name"
                )
        elif self.adapter.enabled and self.adapter.type != "echo":
            try:
                self.adapter.command_template()
            except ConfigError as exc:
                problems.append(str(exc))
        for alias, target in self.peers.items():
            if not target:
                problems.append(f"peer '{alias}' has an empty target")
        if self.identity.role:
            try:
                self.role()
            except Exception as exc:                        # RoleError, or an unreadable file
                problems.append(f"agent.role: {exc}")
        if self.identity.alias.lower() in {a.lower() for a in self.peers}:
            problems.append(f"peer list contains this agent's own alias '{self.identity.alias}'")
        for alias in self.owner_peers():
            problems.append(f"peer '{alias}' is not a bot (it is the owner, or its username "
                            "does not end in 'bot'); peers are the other bots only, and the "
                            "owner is agent.admin_user_id")
        if self.fallback.gitbus_poll_minutes < 0 or self.fallback.gitbus_poll_minutes > 180:
            # Name the key the operator actually wrote (review N1, round 3):
            # the resolved value is mirrored into ``fallback``, so only when
            # ``bus.poll_minutes`` was NOT the explicit source may the legacy
            # branch claim the legacy name.
            if not self._bus_poll_minutes_explicit:
                problems.append("fallback.gitbus_poll_minutes must be 0 (disabled) or between 1 and 180")
        # Issue #59: validate the new first-class bus: section with the same ranges.
        if self.bus.poll_minutes < 0 or self.bus.poll_minutes > 180:
            if self._bus_poll_minutes_explicit:
                problems.append("bus.poll_minutes must be 0 (disabled) or between 1 and 180")
        if not isinstance(self.bus.enabled, bool):
            problems.append("bus.enabled must be a boolean")
        if self.bus.swarm and (self.bus.poll_minutes <= 0 or not self.bus.enabled):
            problems.append("bus.swarm is set but the bus poller is off (bus.poll_minutes 0 "
                            "or bus.enabled false), so swarm lane mail would never be "
                            "delivered; set bus.poll_minutes to 1-180")
        if not self.bus.ref or not isinstance(self.bus.ref, str):
            problems.append("bus.ref must be a non-empty string")
        if self.bus.repo is not None and not isinstance(self.bus.repo, str):
            problems.append("bus.repo must be a string path")
        from swarmly.transports import TRANSPORT_KINDS
        if self.bus.transport not in TRANSPORT_KINDS:
            problems.append(f"bus.transport must be one of {', '.join(TRANSPORT_KINDS)} "
                            f"(got {self.bus.transport!r})")
        elif self.bus.transport == "folder":
            if not self.bus.folder:
                problems.append("bus.folder is required when bus.transport is 'folder'")
            elif not Path(self.bus.folder).is_dir():
                problems.append(f"bus.folder {self.bus.folder} does not exist "
                                "(is the sync client running and the folder shared?)")
        elif self.bus.transport == "gdrive" and not (self.bus.gdrive or {}).get("folder_id"):
            problems.append("bus.gdrive.folder_id is required when bus.transport is 'gdrive'")

        _REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
        seen_topics: Dict[Tuple[Optional[int], int], str] = {}
        for pkey, proj in self.projects.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", pkey):
                # The key names state files and worktree branches.
                problems.append(f"projects.{pkey!r}: use letters, digits, '.', '-' or '_' "
                                "in a project key")
            if not proj.repo:
                problems.append(f"projects.{pkey}.repo is required")
            elif not _REPO_RE.fullmatch(proj.repo):
                problems.append(f"projects.{pkey}.repo must match 'owner/name' (got {proj.repo!r})")
            if not proj.cwd:
                problems.append(f"projects.{pkey}.cwd is required")
            else:
                p_cwd = Path(proj.cwd).expanduser()
                if not p_cwd.exists():
                    problems.append(f"projects.{pkey}.cwd does not exist: {proj.cwd}")
            for item in proj.topics:
                cid, tid = item
                if not isinstance(tid, int) or isinstance(tid, bool):
                    problems.append(f"projects.{pkey}.topics: topic id must be an integer (got {tid!r})")
                    continue
                if tid <= 0:
                    # Forum topic ids are positive; General has none (it is the default).
                    problems.append(f"projects.{pkey}.topics: topic id must be positive (got {tid})")
                    continue
                topic_key = (cid, tid)
                if topic_key in seen_topics:
                    problems.append(f"topic {tid} is listed under multiple projects: '{seen_topics[topic_key]}' and '{pkey}'")
                else:
                    seen_topics[topic_key] = pkey
                if cid is None and self.identity.home_channel_id is not None:
                    home_topic_key = (self.identity.home_channel_id, tid)
                    if home_topic_key in seen_topics and seen_topics[home_topic_key] != pkey:
                        problems.append(f"topic {tid} is listed under multiple projects: '{seen_topics[home_topic_key]}' and '{pkey}'")
                elif cid is not None and cid == self.identity.home_channel_id:
                    none_topic_key = (None, tid)
                    if none_topic_key in seen_topics and seen_topics[none_topic_key] != pkey:
                        problems.append(f"topic {tid} is listed under multiple projects: '{seen_topics[none_topic_key]}' and '{pkey}'")

        if self.limits.max_parallel_turns > 1 and not self.limits.allow_shared_cwd:
            projs_no_worktrees = [p for p in self.projects.values() if not p.worktrees and p.cwd]
            for i, p1 in enumerate(projs_no_worktrees):
                for p2 in projs_no_worktrees[i + 1:]:
                    try:
                        same = Path(p1.cwd).expanduser().resolve() == Path(p2.cwd).expanduser().resolve()
                    except Exception:
                        same = Path(p1.cwd).expanduser() == Path(p2.cwd).expanduser()
                    if same:
                        problems.append(
                            f"projects '{p1.key}' and '{p2.key}' share the same cwd ({p1.cwd}) with "
                            "worktrees: false while limits.max_parallel_turns > 1 and limits.allow_shared_cwd is false"
                        )
        if self.github is not None:
            # The login is what the backstop polls for; without polling it is not needed.
            if not self.github.login and self.github.poll_minutes != 0:
                problems.append("github.login is required (this agent's GitHub account), "
                                "or set github.poll_minutes: 0")
            if not isinstance(self.github.poll_minutes, int) or isinstance(self.github.poll_minutes, bool) or self.github.poll_minutes < 0:
                problems.append("github.poll_minutes must be an integer >= 0")
            if not self.github.token_env:
                problems.append("github.token_env must not be empty")
            elif re.search(r"^A2A_KEY_|BOT_TOKEN", self.github.token_env, re.I):
                # Its value reaches the agent's runtime as GH_TOKEN.
                problems.append(f"github.token_env {self.github.token_env!r} names a bus key or "
                                "bot token, not a GitHub token")
        return problems

    def is_admin(self, user_id: Optional[int]) -> bool:
        return user_id is not None and self.identity.admin_user_id is not None \
            and int(user_id) == int(self.identity.admin_user_id)

    def is_member(self, user_id: Optional[int], username: Optional[str], is_bot: bool) -> bool:
        """Whether the author belongs to the swarm: the admin, a person whose numeric user
        id is in ``agent.allowed_users``, or a peer bot.

        Review P1-8: people are matched by user id only. A handle can be given up and
        claimed by someone else, so ``maintainer_username`` and handles in
        ``allowed_users`` grant nothing (the maintainer handle is used for display).
        """
        if self.is_admin(user_id):
            return True
        if is_bot:
            return self.peer_alias_for(username, user_id) is not None
        return user_id is not None and str(user_id) in set(self.identity.allowed_users)

    def role(self) -> Optional["Role"]:
        """This agent's role (agent.role), resolved against .agents/roles/ of the bus checkout
        and the built-in roles; None when no role is set. Raises RoleError.

        Only the bus checkout is searched: like the signing-key registry, it is a tree swarmly
        never moves forward on its own, whereas agents edit their working directories and
        check out pull-request branches there, which must not rewrite a reviewer's role."""
        if not self.identity.role:
            return None
        from swarmly.telegram.roles import load_role, role_dirs
        base = self.source_path.parent if self.source_path else None
        return load_role(self.identity.role, role_dirs(self.bus.repo), base=base)

    def roles_share_a_working_tree(self) -> Optional[str]:
        """The bus checkout, when it has project roles and is also the adapter's or a
        project's working directory (so agents edit the tree the roles come from)."""
        if not self.bus.repo:
            return None
        bus = Path(self.bus.repo).expanduser().resolve()
        if not (bus / ".agents" / "roles").is_dir():
            return None
        cwds = [self.adapter.cwd] + [p.cwd for p in self.projects.values()]
        for cwd in cwds:
            if cwd and Path(cwd).expanduser().resolve() == bus:
                return str(bus)
        return None

    def owner_peers(self) -> List[str]:
        """Peer aliases that are, or may be, a human rather than a bot.

        That is the owner (admin id or maintainer handle), or any username that does not end
        in "bot": Telegram requires every bot username to.
        """
        owner = {str(self.identity.admin_user_id)} if self.identity.admin_user_id else set()
        if self.identity.maintainer_username:
            owner.add(self.identity.maintainer_username.lstrip("@").lower())
        found = []
        for alias, target in self.peers.items():
            bare = str(target).strip().lstrip("@").lower()
            if bare in owner or (bare and not bare.isdigit() and not bare.endswith("bot")):
                found.append(alias)
        return found

    def peer_alias_for(self, username: Optional[str], user_id: Optional[int] = None) -> Optional[str]:
        """Resolve an incoming author to a peer alias, by username or by numeric id."""
        if username:
            bare = username.lstrip("@").lower()
            for alias, target in self.peers.items():
                if target.lstrip("@").lower() == bare or alias.lower() == bare:
                    return alias
        if user_id is not None:
            for alias, target in self.peers.items():
                if str(target).strip() == str(user_id):
                    return alias
        return None

    def peer_username_for(self, alias: Optional[str]) -> Optional[str]:
        """Resolve a peer alias to its Telegram username with leading @."""
        if not alias:
            return None
        for a, target in self.peers.items():
            if a.lower() == alias.lower():
                target_str = str(target).strip()
                return target_str if target_str.startswith("@") else f"@{target_str}"
        return None


#: A private group's message link, ``https://t.me/c/<id>/<topic>/<message>``: the group's
#: chat id is ``-100`` followed by ``<id>``.
_CHAT_LINK = re.compile(r"(?:https?://)?t\.me/c/(\d+)(?:/\d+)*/?")


def _chat_id(value: Any) -> Optional[int]:
    """A chat id, also accepted as a message link copied from the group."""
    match = _CHAT_LINK.fullmatch(str(value or "").strip())
    if match:
        return int("-100" + match.group(1))
    return _as_int(value)


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    text = str(value).strip()
    # An unfilled template placeholder ("<FILL IN>") is "not set yet", not a malformed id:
    # validate() then reports the field as required, which is the useful message.
    if text.startswith("<") and text.endswith(">"):
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        raise ConfigError(f"expected an integer id, got {value!r}")
