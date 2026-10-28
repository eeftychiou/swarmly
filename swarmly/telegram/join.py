"""``swarmly --cli <runtime>``: start an agent with one command and let it join the swarm.

    swarmly --cli claude --bot-token 123:ABC --admin-userid 1234567 --home-channel -100987654

That is all an agent needs on its machine. The command

1. asks Telegram who the bot is (``getMe``) and takes the alias from the bot's name
   (``--alias`` to choose another);
2. keeps everything for that bot in ``~/.swarmly/<bot username>/``: a generated
   ``config.yaml`` (the runtime, the group, the maintainer, the working folder), a ``.env``
   with the bot token and the agent's bus signing key (Ed25519, made on first start), and
   what the swarm agreed (``config.swarm.json``, ``config.roster.json``);
3. runs the transport, which joins the swarm in the group (membership.py): the other agents
   tell it the bus, the coordinator gives it a role, and it asks for its first task.

When the swarm names the bus, the launcher clones it into ``~/.swarmly/<bot>/bus`` (or uses a
local checkout when the location is a folder), checks out the bus branch, registers the
signing key in the bus's registry, commits and pushes that, points ``config.yaml`` at the
checkout and starts the transport again on it. A restart finds all of this and rejoins
without asking. The flags only need repeating to change them; ``--bot-token`` can come from
``SWARMLY_BOT_TOKEN`` instead of the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("swarmly.telegram.join")

#: ``--cli`` names and the adapter each one runs. Hermes is left out on purpose: its adapter
#: has never run against a real install (docs/telegram/SETUP-HERMES.md).
CLI_ADAPTERS = {
    "claude": "claude",
    "codex": "codex",
    "agy": "antigravity",
    "antigravity": "antigravity",
    "reasonix": "reasonix",
}
#: Where each bot's files live, one folder per bot username.
HOME = Path("~/.swarmly")


class JoinError(Exception):
    """A refusal the launcher prints as one line (exit 2)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarmly", description="Start an agent and let it join the swarm in the Telegram "
                                    "group: the bus, its role and its first task are agreed "
                                    "there (docs/JOIN.md).")
    parser.add_argument("--cli", required=True, choices=sorted(CLI_ADAPTERS),
                        help="the agent CLI this bot drives")
    parser.add_argument("--bot-token", default=os.environ.get("SWARMLY_BOT_TOKEN"),
                        help="the bot's token from @BotFather (or SWARMLY_BOT_TOKEN); kept in "
                             "the bot's .env, so a restart needs it no more")
    parser.add_argument("--admin-userid", type=int,
                        help="the maintainer's numeric Telegram user id")
    parser.add_argument("--home-channel",
                        help="the group's id (-100...) or a link to any message in it")
    parser.add_argument("--alias", help="the name the swarm calls this agent (default: from "
                                        "the bot's name)")
    parser.add_argument("--workdir", type=Path,
                        help="the folder the agent works in (default: the current folder)")
    parser.add_argument("--home", type=Path,
                        help=f"where this bot's files live (default {HOME}/<bot username>)")
    parser.add_argument("--permissions", choices=("strict", "standard", "full"),
                        help="what the agent may do without asking (default full)")
    parser.add_argument("--model", help="the model the CLI runs (default: the CLI's own)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="debug logging on the console")
    parser.add_argument("--log-dir", help="where the debug log goes (default ~/.a2a/logs)")
    return parser


def derive_alias(first_name: str, username: str) -> str:
    """An alias from the bot's display name ("Claude Bot" or "Claudebot" -> "Claude"), else
    its username without the trailing "bot"."""
    for candidate in (first_name or "").split():
        word = re.sub(r"[^A-Za-z0-9_-]", "", candidate)
        # "Claudebot", "CodexBot", "hermes_bot": the name without "bot" (live test: the
        # swarm called Claude "Claudebot"); short words such as "Robot" stay whole.
        stem = re.sub(r"[_-]?bot$", "", word, flags=re.I)
        if len(stem) >= 3:
            word = stem
        if word and word.lower() != "bot" and re.match(r"[A-Za-z]", word):
            return word[:32]
    base = re.sub(r"_?bot$", "", username or "", flags=re.I)
    base = re.sub(r"[^A-Za-z0-9_-]", "", base)
    return (base[:1].upper() + base[1:])[:32] if base else "Agent"


def env_suffix(alias: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", alias.upper())


def _yaml_scalar(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./~-]+", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def render_config(username: str, alias: str, adapter: str, home_channel: int, admin: int,
                  workdir: Path, permissions: Optional[str] = None,
                  model: Optional[str] = None) -> str:
    """The generated config: who the bot is and what it runs, on the Telegram bus. No peers
    and no role: the swarm agrees those in the group. The swarm's shared state -- the lease,
    the topic registry, routing, the owed table -- lives in the Telegram state store, so a
    first swarm needs no git repository at all (``/bus`` still moves it onto gitbus)."""
    lines = [
        "# Written by `swarmly --cli`. Peers, roles and the bus are agreed in the Telegram",
        "# group (docs/JOIN.md): they live in config.roster.json and config.swarm.json beside",
        "# this file, never here. Edit this file only to change the runtime's settings.",
        "agent:",
        f"  name: {username}",
        f"  alias: {alias}",
        f"  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_{env_suffix(alias)}",
        f"  home_channel_id: {home_channel}",
        f"  admin_user_id: {admin}",
        "adapter:",
        f"  type: {adapter}",
        f"  cwd: {_yaml_scalar(str(workdir))}",
        "  timeout_seconds: 1500",
    ]
    if permissions:
        lines.append(f"  permissions: {permissions}")
    if model:
        lines.append(f"  model: {_yaml_scalar(model)}")
    lines += [
        "",
        "# The swarm's bus is the Telegram state store (docs/TRANSPORTS.md).",
        "bus:",
        "  transport: telegram",
        "state:",
        "  backend: telegram",
    ]
    return "\n".join(lines) + "\n"


async def bot_identity(token: str) -> Tuple[str, str, int]:
    """``(username, first_name, id)`` of the bot that *token* belongs to (getMe)."""
    from aiogram import Bot
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    return str(me.username or ""), str(me.first_name or ""), int(me.id)


def _existing_homes(cli: str) -> List[Path]:
    """Bot folders under ~/.swarmly whose config runs *cli*'s adapter."""
    root = HOME.expanduser()
    adapter = CLI_ADAPTERS[cli]
    found = []
    for config in sorted(root.glob("*/config.yaml")) if root.is_dir() else []:
        text = config.read_text(encoding="utf-8", errors="replace")
        # The adapter or its session-keeping variant (claude_thread for --cli claude).
        if re.search(rf"(?m)^  type: {re.escape(adapter)}(?:_thread)?\s*$", text):
            found.append(config.parent)
    return found


def _ensure_signing_key(env_file: Path, agent: str) -> str:
    """This agent's Ed25519 bus key in its .env (made on first start); its public half."""
    from swarmly import crypto
    from swarmly.bus import key_env_name
    from swarmly.keygen import key_from_private, read_env_key, write_env_key
    name = key_env_name(agent)
    private = read_env_key(env_file, name)
    if private:
        pair = key_from_private(agent, private, "ed25519")
    else:
        if not crypto.HAS_ED25519:
            raise JoinError("Ed25519 keys need the 'cryptography' package: pip install "
                            "cryptography")
        pair = crypto.KeyPair.generate_ed25519(agent)
        write_env_key(env_file, name, pair.private_key)
    os.environ[name] = pair.private_key
    return pair.public_key


def prepare(args: argparse.Namespace) -> Tuple[Path, str]:
    """Write (or update) the bot's folder; return its config path and the key's public half."""
    from swarmly.keygen import read_env_key, write_env_key
    token = (args.bot_token or "").strip()
    home: Optional[Path] = args.home.expanduser() if args.home else None
    if not token and home is None:
        homes = _existing_homes(args.cli)
        if len(homes) == 1:
            home = homes[0]
        elif homes:
            raise JoinError(f"several bots run {args.cli} here ({', '.join(map(str, homes))}); "
                            "pass --home or --bot-token")
    if not token and home is not None:
        for line in (home / ".env").read_text(encoding="utf-8-sig").splitlines() \
                if (home / ".env").is_file() else []:
            if line.startswith("A2A_TELEGRAM_BOT_TOKEN_") and "=" in line:
                token = line.split("=", 1)[1].strip().strip("'\"")
                break
    if not token:
        raise JoinError("--bot-token is required on the first start (the token from "
                        "@BotFather)")
    try:
        username, first_name, _ = asyncio.run(bot_identity(token))
    except Exception as exc:                          # noqa: BLE001 - one line for the human
        raise JoinError(f"Telegram refused the bot token: {exc}") from exc
    home = home or (HOME / username.lower()).expanduser()
    config_path = home / "config.yaml"
    env_file = home / ".env"
    old = _read_config(config_path)
    alias = args.alias or old.get("alias") or derive_alias(first_name, username)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", alias):
        raise JoinError(f"alias {alias!r}: use letters, digits, '-' or '_' (pass --alias)")
    from swarmly.telegram.config import ConfigError, _chat_id
    try:
        group = _chat_id(args.home_channel) if args.home_channel else old.get("home")
    except ConfigError as exc:
        raise JoinError(f"--home-channel: {exc}") from exc
    admin = args.admin_userid or old.get("admin")
    if not group:
        raise JoinError("--home-channel is required on the first start (the group's id or a "
                        "link to a message in it)")
    if not admin:
        raise JoinError("--admin-userid is required on the first start (your numeric Telegram "
                        "id; @userinfobot tells you)")
    workdir = (args.workdir or (Path(old["cwd"]) if old.get("cwd") else Path.cwd()))
    workdir = workdir.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    token_name = old.get("token_env") or f"A2A_TELEGRAM_BOT_TOKEN_{env_suffix(alias)}"
    if read_env_key(env_file, token_name) != token:
        write_env_key(env_file, token_name, token)   # owner-only, never on a command line again
    if config_path.is_file():
        # A restart: change only what the flags say, keep every edit the maintainer made.
        from swarmly.telegram.init import _set_key
        text = config_path.read_text(encoding="utf-8")
        kept = re.search(r"(?m)^  type: (\S+)\s*$", text)
        family = kept is not None and kept.group(1) == f"{CLI_ADAPTERS[args.cli]}_thread"
        updates = [("agent", "alias", args.alias), ("agent", "home_channel_id",
                    group if args.home_channel else None),
                   ("agent", "admin_user_id", args.admin_userid),
                   # A restart keeps the maintainer's session-keeping variant (claude_thread):
                   # rewriting it to the plain adapter undid the switch on every start.
                   ("adapter", "type", None if family else CLI_ADAPTERS[args.cli]),
                   ("adapter", "cwd", args.workdir and workdir),
                   ("adapter", "permissions", args.permissions), ("adapter", "model", args.model)]
        for section, key, value in updates:
            if value is not None:
                text = _set_key(text, section, key, _yaml_scalar(str(value)))
    else:
        text = render_config(username, alias, CLI_ADAPTERS[args.cli], int(group), int(admin),
                             workdir, permissions=args.permissions, model=args.model)
    config_path.write_text(text, encoding="utf-8")
    public = _ensure_signing_key(env_file, alias.lower())
    return config_path, public


def _read_config(path: Path) -> dict:
    """The values a previous start wrote, so a restart needs no flags."""
    if not path.is_file():
        return {}
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    agent, adapter, bus = (data.get(k) or {} for k in ("agent", "adapter", "bus"))
    return {"alias": agent.get("alias"), "home": agent.get("home_channel_id"),
            "token_env": agent.get("bot_token_env"),
            "admin": agent.get("admin_user_id"), "cwd": adapter.get("cwd"),
            "permissions": adapter.get("permissions"), "model": adapter.get("model"),
            "bus": bus.get("repo"), "ref": bus.get("ref")}


def _git(repo: Path, *args: str, timeout: int = 120) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def _origin_key(url: str) -> str:
    """A remote URL compared without credentials or a trailing ``.git`` or slash."""
    from swarmly.telegram.membership import safe_bus_location
    return re.sub(r"(\.git)?/*$", "", safe_bus_location(url))


def _origin(repo: Path) -> str:
    return _origin_key(_git(repo, "remote", "get-url", "origin").stdout.strip())


def setup_bus(config_path: Path, location: str, alias: str,
              public_key: str) -> List[str]:
    """Make a checkout of the bus at *location*, register the signing key there and point
    the config at it. Returns what happened, one line each; raises JoinError."""
    from swarmly.keygen import KeygenError, plan_registry
    from swarmly.telegram.init import (BUS_BRANCH, PUSH_ONCE, _bus_files, _set_key,
                                       bus_branch_checkout)
    home = config_path.parent
    local = Path(location).expanduser()
    if local.is_dir() and _git(local, "rev-parse", "--is-inside-work-tree").stdout.strip() \
            == "true":
        project = local.resolve()                 # a checkout on this machine: used as it is
    else:                                         # a URL (or a bare repository): cloned
        project = home / "bus"
        if (project / ".git").exists() and _origin(project) != _origin_key(location):
            # Another bus than the one cloned before (the admin moved it): a clone of its own.
            digest = hashlib.sha256(_origin_key(location).encode()).hexdigest()[:10]
            project = home / f"bus-{digest}"
        if not (project / ".git").exists():
            clone = subprocess.run(["git", "clone", "--quiet", "--", location, str(project)],
                                   capture_output=True, text=True, timeout=600, check=False)
            if clone.returncode != 0:
                detail = (clone.stderr or clone.stdout).strip().splitlines()
                raise JoinError(f"cannot clone the bus {location}: "
                                f"{detail[-1] if detail else 'git clone failed'}")
    lines: List[str] = []
    try:
        bus_repo, ref, notes = bus_branch_checkout(project, BUS_BRANCH)
    except Exception as exc:                        # InitError: one line for the group
        raise JoinError(str(exc)) from exc
    lines += notes
    lines += _bus_files(bus_repo, project, config_path)
    registry = bus_repo / "config" / "a2a_config.json"
    agent = alias.lower()
    try:
        text, _ = plan_registry(registry, agent, public_key, "ed25519")
    except KeygenError as exc:
        raise JoinError(f"cannot register {agent}'s key: {exc}") from exc
    if registry.read_text(encoding="utf-8-sig") != text:
        registry.write_text(text, encoding="utf-8")
        committed = _commit_registry(bus_repo, alias, public_key)
        if committed.startswith("registered") and "pushed" in committed:
            # The registry push published the bus branch: no first push is left to ask for.
            lines = [line for line in lines if not line.startswith(PUSH_ONCE)]
        lines.append(committed)
    config = config_path.read_text(encoding="utf-8")
    # /bus moves this agent onto gitbus: the transport says so, beside the checkout.
    config = _set_key(config, "bus", "transport", "git")
    config = _set_key(config, "bus", "repo", _yaml_scalar(str(bus_repo)))
    if ref:
        config = _set_key(config, "bus", "ref", _yaml_scalar(ref))
    config_path.write_text(config, encoding="utf-8")
    lines.append(f"bus checkout {bus_repo}")
    return lines


def _commit_registry(repo: Path, alias: str, public_key: str) -> str:
    """Commit the registry (as the agent) and push it. When a peer pushed first (two agents
    joining at once both edit the registry), take its commit and register the key again on
    top of it, instead of leaving a rebase conflict in the checkout."""
    from swarmly.keygen import KeygenError, plan_registry
    name, email = alias, f"{alias.lower()}@a2a.local"
    ident = ("-c", f"user.name={name}", "-c", f"user.email={email}")
    registry = repo / "config" / "a2a_config.json"
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD"
    push = None
    for attempt in range(4):
        if attempt:
            _git(repo, "fetch", "--quiet", "origin", branch, timeout=300)
            # Drop the local commit, keep the working tree, and start from the peer's files.
            _git(repo, "reset", "--quiet", "--mixed", f"origin/{branch}")
            _git(repo, "checkout", f"origin/{branch}", "--", "config/a2a_config.json")
            try:
                text, _ = plan_registry(registry, alias.lower(), public_key, "ed25519")
            except KeygenError as exc:
                return f"could not register the key again: {exc}"
            if registry.read_text(encoding="utf-8-sig") == text:
                return f"{alias}'s key is in the registry already"
            registry.write_text(text, encoding="utf-8")
        _git(repo, "add", "--", *(f for f in ("config/a2a_config.json", ".gitignore")
                                  if (repo / f).exists()))
        commit = _git(repo, *ident, "commit", "--quiet", "-m", f"Register {alias}'s bus key")
        if commit.returncode != 0:
            return f"could not commit the registry: {(commit.stderr or commit.stdout).strip()}"
        push = _git(repo, "push", "--quiet", "-u", "origin", branch, timeout=300)
        if push.returncode == 0:
            return f"registered {alias}'s key and pushed it to {branch}"
    assert push is not None
    detail = (push.stderr or push.stdout).strip().splitlines()
    return ("registered the key locally but the push failed: "
            f"{detail[-1] if detail else 'git push failed'}; the swarm still verifies it "
            "from the JOIN")


async def serve(config_path: Path, cli: str, public_key: str) -> int:
    """Run the transport, starting it again after the swarm agreed on a bus."""
    from swarmly.telegram.channel import AgentChannel
    from swarmly.telegram.config import AgentConfig
    from swarmly.telegram.poll import (RUN_LOCK_EXIT_CODE, acquire_run_lock,
                                       release_run_lock, transport_state_root)
    while True:
        config = AgentConfig.load(config_path)
        root = transport_state_root(config)
        lock = acquire_run_lock(root, config.identity.alias)
        if not lock.acquired:
            print(f"swarmly: {config.identity.alias} is already running "
                  f"(lock {lock.path})", file=sys.stderr)
            return RUN_LOCK_EXIT_CODE
        channel = AgentChannel(config, transport=True)
        membership = channel.membership
        membership.dynamic = True
        membership.cli = cli
        membership.public_key = public_key

        async def on_bus(location: str, channel: AgentChannel = channel) -> None:
            try:
                lines = await asyncio.to_thread(setup_bus, config_path, location,
                                                channel.config.identity.alias, public_key)
            except Exception as exc:            # noqa: BLE001 - the admin is told, no crash
                logger.warning("bus setup failed: %s", exc, exc_info=not isinstance(
                    exc, JoinError))
                channel.membership.state.bus = None
                channel.membership.state.bus_ready = False
                channel.membership.state.save(channel.membership.path)
                await channel.membership.ask_maintainer(
                    f"⚠️ {channel.config.identity.alias} cannot use the bus: {exc}. "
                    f"Fix it and reply /bus{channel.membership._at()} <git URL or folder>.")
                return
            for line in lines:
                logger.info("bus: %s", line)
            channel.membership.state.bus_ready = True
            channel.membership.state.save(channel.membership.path)
            channel.request_restart()

        membership.on_bus = on_bus
        try:
            await channel.start()
        finally:
            try:
                await channel.stop()
            finally:
                release_run_lock(root, config.identity.alias)
        if not channel.restart_requested:
            return 0
        logger.info("starting again on the agreed bus")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from swarmly.logsetup import configure as configure_logging
    configure_logging("swarmly-join", verbose=args.verbose, directory=args.log_dir)
    from swarmly.keygen import KeygenError
    try:
        config_path, public_key = prepare(args)
    except (JoinError, KeygenError, OSError) as exc:
        print(f"swarmly: {exc}", file=sys.stderr)
        return 2
    from swarmly.telegram.config import AgentConfig, ConfigError
    try:
        config = AgentConfig.load(config_path)
    except ConfigError as exc:
        print(f"swarmly: {config_path}: {exc}", file=sys.stderr)
        return 2
    configure_logging(f"swarmly-telegram-{config.identity.alias}-run", verbose=args.verbose,
                      directory=args.log_dir)
    print(f"swarmly: {config.identity.alias} ({config.identity.username}, {args.cli}) joining "
          f"group {config.identity.home_channel_id}; files in {config_path.parent}")
    try:
        return asyncio.run(serve(config_path, args.cli, public_key))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
