"""
``swarmly-telegram init``: write a starter ``config.yaml`` from the packaged template.

A ``pip install swarmly`` has no checkout to copy ``config/telegram.example.yaml`` from, so
the template ships inside the package and this command fills in what it can: the alias, the
bot-token variable, the adapter and, with ``--bus-repo``, the bus checkout (creating an empty
signing-key registry there when it has none). What only the human knows (the bot username,
the group id, their Telegram user id) stays for them to edit; ``check`` then says what is
still missing.
"""

from __future__ import annotations

import os
import re
import subprocess
from importlib import resources
from pathlib import Path, PurePath
from typing import Callable, List, Optional, Tuple

from swarmly.telegram.config import DEFAULT_COMMANDS

ADAPTER_CHOICES = sorted(set(DEFAULT_COMMANDS) | {"custom", "codex_thread", "antigravity_thread"})
#: init writes these where only the human knows the value; ``check`` names each one.
PLACEHOLDER_HOME_CHANNEL = 0
#: The branch bus mail is committed to, so it never lands on the project's main branch.
BUS_BRANCH = "agents-bus"
#: Where init checks that branch out when the bus checkout is on another branch.
BUS_WORKTREE = Path(".agents") / "bus"


class InitError(Exception):
    """A refusal the CLI prints as one line (exit 2)."""


def _template(name: str) -> str:
    return resources.files("swarmly.telegram").joinpath("templates").joinpath(name).read_text(
        encoding="utf-8")


def _env_suffix(alias: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", alias.upper())


def render_config(alias: str, adapter: str, bus_repo: Optional[Path],
                  cwd: Optional[Path], role: Optional[str] = None,
                  bus_ref: Optional[str] = None) -> str:
    """The template with this agent's values in place. Every substitution must hit exactly
    once, so a template edit that moves a line fails loudly here instead of writing a config
    that silently keeps the example values."""
    text = _template("config.example.yaml")
    workdir = str(cwd or bus_repo or Path.cwd())
    token_env = f"A2A_TELEGRAM_BOT_TOKEN_{_env_suffix(alias)}"
    swaps = [
        (r"(?m)^(  name: )example_hermes_bot()", f"example_{alias.lower()}_bot"),
        (r"(?m)^(  home_channel_id: )-1001234567890()", f"{PLACEHOLDER_HOME_CHANNEL}       "),
        (r"(?m)^(  alias: )Hermes()", alias),
        (r"(?m)^(  bot_token_env: )A2A_TELEGRAM_BOT_TOKEN_HERMES()", token_env),
        (r"(?m)^(  type: )hermes()", adapter),
        (r"(?m)^(  cwd: )/home/user/your-project()$", _yaml_path(workdir)),
    ]
    if role:
        swaps.append((r"(?m)^(  )# role: developer( *)", f"role: {_yaml_path(role)}" + " " * 2))
    if bus_repo is not None:
        ref = f"\n  ref: {_yaml_path(bus_ref)}" if bus_ref else ""
        swaps.append((r"(?m)^()# bus:\n#   repo: /home/user/your-project()$",
                      f"bus:\n  repo: {_yaml_path(str(bus_repo))}{ref}"))
    for pattern, value in swaps:
        # A function, not a replacement string: a Windows path's backslashes are literal.
        text, count = re.subn(pattern, _keeping(value), text)
        if count != 1:
            raise InitError(f"the packaged config template changed shape ({pattern!r}); "
                            "please report this")
    # The agent is not its own peer.
    head, sep, rest = text.partition("\npeers:\n")
    if sep:
        block, blank, tail = rest.partition("\n\n")
        kept = [line for line in block.split("\n")
                if line.split(":", 1)[0].strip().lower() != alias.lower()]
        if alias.lower() != "hermes":         # the template's own agent becomes a peer
            kept.insert(0, "  Hermes: example_hermes_bot")
        text = head + sep + "\n".join(kept) + blank + tail
    hints = {
        "custom": "  # command: 'your-agent-cli {prompt}'   # REQUIRED for custom: "
                  "your CLI, with {prompt}\n",
        "codex_thread": "  # conversation: <Codex session UUID or exact name>   "
                        "# REQUIRED for codex_thread\n",
    }
    if adapter in hints:
        text, count = re.subn(r"(?m)^(  timeout_seconds: )", lambda m: hints[adapter] + m.group(1),
                              text)
        if count != 1:
            raise InitError("the packaged config template changed shape (timeout_seconds); "
                            "please report this")
    return text


def _keeping(value: str) -> Callable[["re.Match[str]"], str]:
    """A substitution that keeps the line's key (group 1) and trailing text (group 2)."""
    return lambda m: m.group(1) + value + m.group(2)


def _yaml_path(path: str) -> str:
    """A path as a YAML scalar: quoted when it has characters YAML would read differently
    (Windows drive letters, spaces, '#')."""
    if re.fullmatch(r"[A-Za-z0-9_./~-]+", path):
        return path
    return "'" + path.replace("'", "''") + "'"


def _ignore_file(repo: Path, local: bool) -> Path:
    """Where ignore entries go: the checkout's tracked ``.gitignore``, or, for ``local``
    entries in a git checkout, its untracked ``info/exclude`` (nothing for the user to
    commit into their project)."""
    if local:
        res = _git(repo, "rev-parse", "--git-path", "info/exclude")
        if res.returncode == 0 and res.stdout.strip():
            path = Path(res.stdout.strip())
            return path if path.is_absolute() else repo / path
    return repo / ".gitignore"


def _gitignore(repo: Path, entries: List[str], local: bool = False) -> List[str]:
    """Append the entries the ignore file lacks; return the ones added."""
    path = _ignore_file(repo, local)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    def variants(entry: str) -> set[str]:
        bare = entry.lstrip("/")
        return {bare, "/" + bare}
    missing = [e for e in entries if not variants(e) & present]
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + prefix + "\n".join(missing) + "\n", encoding="utf-8")
    return missing


def _git(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=60, check=False)


def _worktree_of(repo: Path, branch: str) -> Optional[Path]:
    """The checkout that already has ``branch`` checked out, if any."""
    listing = _git(repo, "worktree", "list", "--porcelain")
    path: Optional[str] = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            return Path(path)
    return None


def bus_branch_checkout(repo: Path, branch: str = BUS_BRANCH,
                        remote: str = "origin") -> Tuple[Path, Optional[str], List[str]]:
    """``(bus checkout, bus.ref, notes)``: a checkout on ``branch``, so agents commit their
    mail there and never to the project's main branch.

    A checkout already on ``branch`` is used as it is; otherwise the branch (made from HEAD,
    or tracking ``<remote>/<branch>`` when that exists) is checked out as a worktree at
    ``.agents/bus`` of the project, leaving the project checkout on its own branch. Not a git
    checkout: returned unchanged, with no ref."""
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return repo, None, []
    ref = f"{remote}/{branch}"
    if _git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() == branch:
        return repo, ref, []
    existing = _worktree_of(repo, branch)
    if existing is not None:
        return existing.resolve(), ref, [f"bus mail goes to branch {branch}, checked out at "
                                         f"{existing}"]
    root = Path(top.stdout.strip())
    worktree = root / BUS_WORKTREE
    if worktree.exists():
        raise InitError(f"{worktree} exists but is not a checkout of {branch}; move it away "
                        "or pass --bus-branch ''")
    # Another agent may have pushed the bus branch already: track it rather than start a
    # second, unrelated one from HEAD (best effort; offline just means no remote branch yet).
    _git(repo, "fetch", "-q", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    if _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
        add = _git(repo, "worktree", "add", str(worktree), branch)
    elif _git(repo, "rev-parse", "--verify", "-q",
              f"refs/remotes/{remote}/{branch}").returncode == 0:
        add = _git(repo, "worktree", "add", "--track", "-b", branch, str(worktree),
                   f"{remote}/{branch}")
    else:
        add = _git(repo, "worktree", "add", "-b", branch, str(worktree), "HEAD")
    if add.returncode != 0:
        detail = (add.stderr or add.stdout).strip().splitlines()
        raise InitError(f"could not check out the bus branch {branch} at {worktree}: "
                        f"{detail[-1] if detail else 'git worktree add failed'} (an empty "
                        "repository needs a first commit; --bus-branch '' keeps the current "
                        "branch)")
    _gitignore(root, ["/" + BUS_WORKTREE.as_posix() + "/"], local=True)
    return worktree, ref, [f"checked out branch {branch} at {worktree} for bus mail, so "
                           "agents never commit to your main branch; push it once with "
                           f"`git -C {worktree} push -u {remote} {branch}`"]


def _set_key(text: str, section: str, key: str, value: str) -> str:
    """Set ``section.key`` in a config's text, keeping every other line and comment."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(rf"{section}:\s*(#.*)?$", line)),
                 None)
    entry = f"  {key}: {value}"
    if start is None:
        return text.rstrip("\n") + f"\n\n{section}:\n{entry}\n"
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "#")):
        end += 1
    for i in range(start + 1, end):
        found = re.match(rf"(  {re.escape(key)}:)\s*([^#\n]*?)(\s+#.*)?$", lines[i])
        if found:
            lines[i] = entry + (found.group(3) or "")
            return "\n".join(lines)
    lines.insert(start + 1, entry)
    return "\n".join(lines)


def retarget_config(text: str, bus_repo: Optional[PurePath], bus_ref: Optional[str],
                    cwd: Optional[PurePath], home_channel: Optional[str]) -> str:
    """An existing agent's config pointed at another project (and group): bus.repo, bus.ref,
    adapter.cwd and agent.home_channel_id change; the bot, peers and settings stay."""
    if bus_repo is not None:
        text = _set_key(text, "bus", "repo", _yaml_path(str(bus_repo)))
        if bus_ref:
            text = _set_key(text, "bus", "ref", _yaml_path(bus_ref))
    if cwd is not None:
        text = _set_key(text, "adapter", "cwd", _yaml_path(str(cwd)))
    if home_channel:
        text = _set_key(text, "agent", "home_channel_id", str(_group_id(home_channel)))
    return text


def _group_id(value: str) -> int:
    from swarmly.telegram.config import ConfigError, _chat_id
    try:
        chat = _chat_id(value)
    except ConfigError:
        chat = None
    if chat is None:
        raise InitError(f"--home-channel {value!r}: give the group id (-100...) or a message "
                        "link copied from the group")
    return chat


def write_from(config_path: Path, source: Path, bus_repo: Path, cwd: Optional[Path] = None,
               home_channel: Optional[str] = None, force: bool = False,
               bus_branch: Optional[str] = BUS_BRANCH) -> List[str]:
    """Copy an existing agent's config to a new project; return what to do next."""
    import yaml
    try:
        text = source.expanduser().read_text(encoding="utf-8-sig")
        raw = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InitError(f"--from {source}: {exc}") from exc
    agent = raw.get("agent") if isinstance(raw, dict) else None
    alias = str((agent or {}).get("alias") or "").strip()
    if not alias:
        raise InitError(f"--from {source}: no agent.alias; is this a swarmly-telegram config?")
    if config_path.expanduser().resolve() == source.expanduser().resolve():
        raise InitError("--from and --config are the same file; write the new config "
                        "elsewhere (e.g. in the new clone)")
    if config_path.exists() and not force:
        raise InitError(f"{config_path} already exists; pass --force to overwrite it")
    if home_channel:
        _group_id(home_channel)                # before anything is checked out
    bus_repo = bus_repo.expanduser().resolve()
    if not bus_repo.is_dir():
        raise InitError(f"--bus-repo {bus_repo} is not a directory (clone the project "
                        "repository first)")
    project = bus_repo
    cwd = cwd.expanduser().resolve() if cwd is not None else project
    bus_ref: Optional[str] = None
    notes: List[str] = []
    if bus_branch:
        bus_repo, bus_ref, notes = bus_branch_checkout(bus_repo, bus_branch)
    text = retarget_config(text, bus_repo, bus_ref, cwd, home_channel)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    lines = [f"wrote {config_path} from {source} (bus, working folder"
             + (", group" if home_channel else "") + " changed; bot, peers and settings kept)",
             *notes, *_bus_files(bus_repo, project, config_path)]
    registry = bus_repo / "config" / "a2a_config.json"
    flag = f" --config {config_path}"
    lines += [
        "",
        "Next:",
        f"  1. Register this agent's existing key in the new project's registry (the key in"
        " your env file is reused, not replaced):",
        f"       swarmly-keygen --agent {alias.lower()} --env-file ~/.a2a-telegram.env "
        f"--registry {registry}",
        "     then commit and push the registry in the bus checkout.",
    ]
    if not home_channel:
        lines.append("  (The group is unchanged; pass --home-channel to move to another one.)")
    lines += [f"  2. swarmly-telegram{flag} check", f"     swarmly-telegram{flag} run"]
    return lines


def _bus_files(bus_repo: Path, project: Path, config_path: Path) -> List[str]:
    """The empty registry and the ignore entries a bus checkout needs."""
    lines: List[str] = []
    registry = bus_repo / "config" / "a2a_config.json"
    if registry.exists():
        lines.append(f"registry {registry} already exists; left as is")
    else:
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(_template("a2a_config.json"), encoding="utf-8")
        lines.append(f"created an empty signing-key registry at {registry}")
    for repo in dict.fromkeys((bus_repo, project)):
        ignore = [".agents/state/"] if repo == bus_repo else []
        try:
            # Anchored: ignore this agent's config, not every config.yaml in the project.
            ignore.append("/" + config_path.resolve().relative_to(repo).as_posix())
        except ValueError:
            pass                      # the config lives outside the checkout
        # The bus checkout's own .gitignore is bus-branch content, shared by every agent;
        # the project checkout gets local-only entries, so its tree stays clean.
        local = repo != bus_repo
        added = _gitignore(repo, ignore, local=local)
        if added:
            lines.append(f"added {', '.join(added)} to {_ignore_file(repo, local)} "
                         "(transport state and per-agent config never belong in git)")
    return lines


def write_starter(config_path: Path, alias: str, adapter: str,
                  bus_repo: Optional[Path] = None, cwd: Optional[Path] = None,
                  force: bool = False, role: Optional[str] = None,
                  bus_branch: Optional[str] = BUS_BRANCH) -> List[str]:
    """Write the config (and, with a bus, its empty registry); return what to do next."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", alias):
        raise InitError(f"alias {alias!r}: use letters, digits, '-' or '_', starting with a "
                        "letter (e.g. Hermes)")
    if adapter not in ADAPTER_CHOICES:
        raise InitError(f"unknown adapter {adapter!r}; choose one of {ADAPTER_CHOICES}")
    if config_path.exists() and not force:
        raise InitError(f"{config_path} already exists; pass --force to overwrite it")
    if bus_repo is not None:
        bus_repo = bus_repo.expanduser().resolve()
        if not bus_repo.is_dir():
            raise InitError(f"--bus-repo {bus_repo} is not a directory (clone the project "
                            "repository first)")
    if cwd is not None:
        cwd = cwd.expanduser().resolve()
    project = bus_repo
    bus_ref: Optional[str] = None
    bus_notes: List[str] = []
    if bus_repo is not None and bus_branch:
        if cwd is None:
            cwd = bus_repo                     # the agent works in the project, not the bus
        bus_repo, bus_ref, bus_notes = bus_branch_checkout(bus_repo, bus_branch)

    if role:
        from swarmly.telegram.roles import RoleError, load_role, role_dirs
        try:
            load_role(role, role_dirs(str(bus_repo) if bus_repo else None),
                      base=config_path.parent)
        except RoleError as exc:
            raise InitError(str(exc)) from exc
    text = render_config(alias, adapter, bus_repo, cwd, role=role, bus_ref=bus_ref)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")

    agent = alias.lower()
    env_file = "~/.a2a-telegram.env"           # swarmly expands ~ on Windows too
    lines = [f"wrote {config_path}", *bus_notes]
    registry = None
    if bus_repo is not None:
        registry = bus_repo / "config" / "a2a_config.json"
        lines += _bus_files(bus_repo, project or bus_repo, config_path)

    flag = "" if config_path.name == "config.yaml" and config_path.parent == Path(".") \
        else f" --config {config_path}"
    lines += [
        "",
        "Next (every swarmly-telegram command reads the env file by itself):",
        f"  1. Put the bot token in {env_file} as one line"
        + (" (%USERPROFILE%\\.a2a-telegram.env)" if os.name == "nt" else "")
        + ", then find the group id:",
        f"       A2A_TELEGRAM_BOT_TOKEN_{_env_suffix(alias)}=123456:ABC...",
        f"       swarmly-telegram{flag} discover",
        "     One env file holds every bot's token and signing key on this machine: each bot"
        " reads",
        "     the token line named after its alias and the key line A2A_KEY_<AGENT>.",
        f"  2. Edit {config_path}: agent.name (your bot's username), agent.home_channel_id,",
        "     agent.admin_user_id (your numeric Telegram id, from @userinfobot), and peers,",
        "     one line per other bot: `Codex: my_codex_bot` or, with a role,",
        "     `Codex: {username: my_codex_bot, role: qa-tester}`.",
        f"  3. Create this agent's signing key"
        + (":" if registry else " (after setting bus.repo):"),
        f"       swarmly-keygen --agent {agent} --env-file {env_file} --registry "
        + (str(registry) if registry else "<bus checkout>/config/a2a_config.json"),
        "     then commit and push the registry in the bus checkout"
        + (f" ({bus_repo})." if bus_repo is not None and bus_repo != project else "."),
        f"  4. swarmly-telegram{flag} check",
        f"     swarmly-telegram{flag} run",
    ]
    return lines
