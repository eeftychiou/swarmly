"""
Command line entry point for the Telegram home channel.

    python -m swarmly.telegram.cli --config agents/hermes/config.yaml check
    python -m swarmly.telegram.cli --config agents/hermes/config.yaml whoami
    python -m swarmly.telegram.cli --config agents/hermes/config.yaml send "hello from Hermes"
    python -m swarmly.telegram.cli --config agents/hermes/config.yaml run

``check`` is the useful one before anything else: it validates the config, resolves the
identity and the adapter's CLI, and prints the exact command line the adapter will run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from swarmly import __version__
from swarmly.logsetup import configure as configure_logging
from swarmly.telegram.channel import AgentChannel, TopicGone
from swarmly.telegram.config import AgentConfig, ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarmly-telegram", description="A2A Telegram home channel")
    parser.add_argument("--config", default="config.yaml",
                        help="path to this agent's config.yaml (default: ./config.yaml)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging on the console")
    parser.add_argument("--log-dir", help="where the rotating debug log goes (default: "
                                          "$A2A_LOG_DIR or ~/.a2a/logs; 'off' disables it)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config.yaml (and, with --bus-repo, "
                                       "an empty signing-key registry in the bus checkout)")
    init.add_argument("--alias", default="Hermes",
                      help="the name the swarm calls this agent (default Hermes)")
    init.add_argument("--adapter", default="hermes",
                      help="agent CLI to drive: hermes, codex, claude, antigravity, reasonix, "
                           "their *_thread variants, echo or custom (default hermes)")
    init.add_argument("--bus-repo", type=Path,
                      help="the git checkout whose .agents/inbox/ is this project's bus")
    init.add_argument("--bus-branch", default="agents-bus",
                      help="the branch bus mail is committed to (default agents-bus; checked "
                           "out at .agents/bus when the bus checkout is on another branch). "
                           "'' keeps the checkout's current branch")
    init.add_argument("--cwd", type=Path,
                      help="the agent's working directory (default: --bus-repo, else here)")
    init.add_argument("--role", help="this agent's role: coordinator, developer, reviewer, "
                                     "qa-tester, researcher, or a role file (docs/ROLES.md)")
    init.add_argument("--from", dest="from_config", type=Path, metavar="OLD_CONFIG",
                      help="copy an existing agent's config to the project at --bus-repo: "
                           "the bus, working folder and (with --home-channel) group change, "
                           "the bot, peers and settings stay")
    init.add_argument("--home-channel", metavar="GROUP",
                      help="with --from: the new group's id or a message link copied from it")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")

    check = sub.add_parser("check", help="validate the config, resolve the adapter CLI, ask "
                                         "Telegram whether the bot is in the group, and print "
                                         "what the agent will do")
    check.add_argument("--offline", action="store_true",
                       help="skip the Telegram calls (token and group membership)")
    sub.add_parser("whoami", help="verify the bot token with getMe")
    sub.add_parser("discover", help="list the chats this bot can see, to find the home channel id")

    send = sub.add_parser("send", help="post one message to the home channel")
    send.add_argument("text", help="message text")
    send.add_argument("--topic", type=int, default=None, metavar="THREAD_ID",
                      help="post in this forum topic of the home group instead of General")

    post = sub.add_parser("post", help="write an envelope to the durable bus, then point "
                                       "Telegram at it (durable first, then instant)")
    post.add_argument("--to", required=True, help="peer alias, or * for everyone")
    post.add_argument("--intent", default="STATUS_UPDATE",
                      help="A2A intent of the durable envelope (default STATUS_UPDATE)")
    post.add_argument("--type", dest="telegram_type", default=None,
                      help="Telegram envelope type (default REQUEST to a peer, NOTE to *)")
    post.add_argument("--no-telegram", action="store_true",
                      help="durable write only; peers see it on their next bus poll")
    post.add_argument("text", help="message text")

    reply = sub.add_parser("reply", help="submit a queued task's explicit Telegram answer")
    reply.add_argument("ticket", help="reply ticket ID from the inbound prompt")
    response = reply.add_mutually_exclusive_group(required=True)
    response.add_argument("--text", help="answer or blocker; under 500 characters")
    response.add_argument("--skip", help="reason no reply is needed (pure ACK/status only)")
    reply.add_argument("--wait", type=float, default=0, help="wait up to this many seconds (maximum 60) for Telegram acceptance")
    status = sub.add_parser("reply-status", help="inspect a reply ticket without posting")
    status.add_argument("ticket")

    sub.add_parser("run", help="start long polling and serve the channel; exits 4 "
                               "when another live transport holds the instance lock")

    poll = sub.add_parser(
        "bus-poll",
        help="run one durable-bus poll cycle (no Telegram token or transport required)",
    )
    poll.add_argument(
        "--once",
        action="store_true",
        help="poll the bus once and exit (the only mode, so it may be left out; kept for "
             "schedulers that pass it)",
    )
    poll.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON summary instead of the human summary",
    )
    poll.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the human summary (exit codes unchanged; for scheduler wrappers)",
    )

    beat = sub.add_parser(
        "heartbeat-check",
        help=("read the canonical liveness beat; exit 3 when stale, or when the beat "
              "is fresh but its transport block is dead/degraded (watchdog entry point)"),
    )
    beat.add_argument(
        "--alert",
        action="store_true",
        help="post a channel alert when the beat is stale (needs the bot token)",
    )
    beat.add_argument(
        "--factor",
        type=float,
        default=3.0,
        help="staleness factor over the poll interval (default 3)",
    )
    beat.add_argument(
        "--json",
        action="store_true",
        help="emit the status as JSON (path, age, interval, threshold, counters)",
    )

    gh_poll = sub.add_parser(
        "gh-poll", aliases=["github-poll"],
        help="run one GitHub notifications backstop cycle (no Telegram token or transport required)",
    )
    gh_poll.add_argument(
        "--once",
        action="store_true",
        help="poll GitHub notifications once and exit",
    )
    gh_poll.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON summary instead of the human summary",
    )
    gh_poll.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the human summary (exit codes unchanged; for scheduler wrappers)",
    )

    # `--config` also after the command (`swarmly-telegram init --config x.yaml`); SUPPRESS
    # keeps the top-level value when it is not repeated there.
    for name, command in sub.choices.items():
        if name != "github-poll":                 # an alias: the same parser as gh-poll
            command.add_argument("--config", default=argparse.SUPPRESS,
                                 help="path to this agent's config.yaml (default: ./config.yaml)")
    return parser


# Adapter types that spawn a local CLI. ``check`` resolves the program so a transport is
# never started against a PATH that cannot run it (the 2026-09-19 ``WinError 2`` incident,
# where a bare environment started a daemon that could not queue a single message). Keep
# this in step with the command-template registry: ``test_cli_adapter_types_cover...``
# fails when a CLI-backed template type is added and left out.
_CLI_ADAPTER_TYPES = ("codex", "codex_thread", "claude", "claude_thread", "hermes",
                      "antigravity", "antigravity_thread", "reasonix")


def _missing_program(command: str, cwd: Optional[str] = None) -> Optional[str]:
    """Return the program from ``command`` that cannot be resolved, or None."""
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return None
    if not argv:
        return None
    program = argv[0].strip("'\"")
    if os.path.dirname(program):
        target = program if os.path.isabs(program) or not cwd else os.path.join(cwd, program)
        found = os.path.exists(target) or (os.name == "nt" and bool(shutil.which(target)))
        return None if found else program
    return None if shutil.which(program) else program


def _bus_on_main_branch(config: AgentConfig) -> str:
    """``main`` or ``master`` when bus mail would be committed to that branch, else ""."""
    if not config.bus.repo:
        return ""
    try:
        head = subprocess.run(["git", "-C", str(config.bus.repo), "symbolic-ref", "--short",
                               "-q", "HEAD"], capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    return head if head in ("main", "master") else ""


async def _check(config: AgentConfig, live: bool = False) -> int:
    try:  # legacy consoles (Windows cp1252) cannot encode the ✗ problem marker
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass
    problems = config.validate()
    identity = config.identity
    print(f"alias           : {identity.alias}")
    print(f"telegram name   : {identity.username}")
    print(f"home channel id : {identity.home_channel_id}")
    print(f"admin user id   : {identity.admin_user_id}")
    print(f"token           : {'present' if config.identity.token_is_present() else 'MISSING'}")
    name, email = config.identity.git_identity
    print(f"commits as      : {name} <{email}>")
    print(f"adapter         : {config.adapter.type}")
    command_line: Optional[str] = None
    if config.adapter.enabled:
        if config.adapter.type == "codex_thread":
            thread = config.adapter.conversation or "<missing conversation>"
            command_line = f"codex queue --thread {thread} --message {{prompt}}"
            print(f"command         : {command_line}")
        elif config.adapter.type == "claude_thread":
            from swarmly.telegram.adapters.claude import ClaudeThreadAdapter
            session = (config.adapter.conversation or ClaudeThreadAdapter.derived_session_id(
                config.identity.alias, config.adapter.cwd))
            command_line = config.adapter.command_template()
            origin = "configured" if config.adapter.conversation else "derived; created on first use"
            print(f"command         : {command_line}  (+ --resume {session})")
            print(f"session         : {session} ({origin})")
            if config.projects:
                for pkey, proj in sorted(config.projects.items()):
                    p_session = ClaudeThreadAdapter.derived_session_id(config.identity.alias, proj.cwd)
                    print(f"session ({pkey:<6}): {p_session} (derived for {proj.cwd})")
        elif config.adapter.type == "antigravity_thread":
            thread = config.adapter.conversation or "auto"
            print(f"command         : agentapi send-message --title='Telegram: @author' {thread} {{prompt}} (fallback: agy -p)")
            # The preflight below only inspects a command that is known, and this
            # branch used to set nothing -- so the ONE adapter type the install
            # doc recommends was the one type `check` never verified. Resolve
            # agentapi the same way dispatch does and let the block below report
            # its absence; the adapter looks it up again at dispatch time, so
            # this is a diagnostic, not the value that gets used.
            try:
                from swarmly.telegram.adapters.antigravity import resolve_agentapi
                resolved = resolve_agentapi()
                if resolved:
                    # Quoted: _missing_program shlex-splits, so an unquoted path
                    # containing a space would be truncated and reported missing.
                    print(f"agentapi        : {resolved[0]}")
                    command_line = f'"{resolved[0]}" agentapi'
                else:
                    print("agentapi        : NOT FOUND (the fast path will be unavailable)")
                    command_line = "agentapi"
            except Exception as exc:                      # noqa: BLE001 - diagnostic only
                print(f"agentapi        : UNRESOLVED — {exc}")
        elif config.adapter.type == "echo" and not config.adapter.command:
            print("command         : none (echo answers in-process; for testing the channel)")
        else:
            try:
                command_line = config.adapter.command_template()
                print(f"command         : {command_line}")
            except ConfigError as exc:
                print(f"command         : INVALID — {exc}")
    if command_line and config.adapter.type in _CLI_ADAPTER_TYPES:
        cwds_to_check = [(config.adapter.cwd, "adapter.cwd")]
        for pkey, proj in config.projects.items():
            cwds_to_check.append((proj.cwd, f"project {pkey}"))
        for cwd_path, cwd_label in cwds_to_check:
            missing = _missing_program(command_line, cwd_path)
            if missing:
                hint = ""
                if config.adapter.type.startswith("codex"):
                    hint = (" or start the transport via scripts/run_codex_telegram.* "
                            "(it resolves the CLI and pins its directory)")
                elif config.adapter.type.startswith("claude"):
                    hint = " (see https://docs.claude.com/en/docs/claude-code/setup)"
                where = "does not exist" if os.path.dirname(missing) else "not found on PATH"
                loc = f" in {cwd_label} ({cwd_path or '.'})" if cwd_label != "adapter.cwd" else ""
                problems.append(
                    f"'{missing}' {where} — the {config.adapter.type} adapter cannot "
                    f"run messages{loc}; install it{hint}"
                )
    print(f"cwd             : {config.adapter.cwd or '(current directory)'}")
    if config.projects:
        for pkey, proj in sorted(config.projects.items()):
            print(f"project {pkey:<7} : {proj.cwd} ({proj.repo})")
    print(f"peers           : {', '.join(config.peers) or '(none configured)'}")
    notes: list = []
    print(f"bus transport   : {_describe_bus(config, problems, notes)}")
    # Review P1-7: a misspelt key used to be ignored without a word.
    if config.identity.role:
        try:
            role = config.role()
            print(f"role            : {role.name} ({role.source}) — {role.description}"
                  if role else "role            : none")
        except Exception as exc:                           # noqa: BLE001 - listed below too
            print(f"role            : UNREADABLE — {exc}")
    adapter = config.adapter
    if adapter.type.startswith("claude") and adapter.permissions == "standard" \
            and not adapter.command and "gh" not in adapter.allow_commands \
            and (config.identity.role or config.github):
        notes.append("this Claude agent has a role but cannot run gh at permissions: standard "
                     "(gh acts with this machine's GitHub login, so it is opt-in): add "
                     "allow_commands: [gh] under adapter: if it should open issues, PRs or "
                     "reviews")
    on_main = _bus_on_main_branch(config)
    if on_main:
        notes.append(f"the bus is on branch {on_main}: every agent commits its mail there. "
                     "Run init again (it checks out an agents-bus branch at .agents/bus) or "
                     "point bus.repo and bus.ref at a dedicated bus branch or repository")
    shared = config.roles_share_a_working_tree()
    if shared:
        notes.append(f"project roles are read from {shared}/.agents/roles, which is also an agent "
                     "working directory: a branch checked out there can change the roles "
                     "(docs/ROLES.md, Trust). Give the agent its own clone or worktree "
                     "(adapter.cwd) to keep them on the bus checkout's reviewed state")
    if config.peer_roles:
        print("peer roles      : " + ", ".join(f"{a}={r}" for a, r in config.peer_roles.items()))
        from swarmly.telegram.roles import known_roles, role_dirs
        known = known_roles(role_dirs(config.bus.repo))
        for alias, peer_role in config.peer_roles.items():
            if peer_role.lower() not in known:
                notes.append(f"peer {alias}'s role {peer_role!r} is not a role this checkout "
                             f"knows ({', '.join(sorted(known))}); it is shown to the agent as is")
    problems.extend(config.unknown_keys)
    if config.identity.name.lstrip("@").lower().startswith("example_"):
        problems.append(f"agent.name is still the example {config.identity.name!r}: set it to "
                        "the bot's username from @BotFather")
    if live:
        print(f"telegram        : {await _check_telegram(config, problems, notes)}")
    print()
    from swarmly.telegram.selftest import describe, read_selftest, selftest_path
    last = read_selftest(config)
    if last is None and selftest_path(config).exists():
        print(f"bot-to-bot test : result file {selftest_path(config)} is unreadable")
        problems.append("the bot-to-bot test result file is unreadable")
    elif last is None:
        print("bot-to-bot test : not run yet (the transport runs it on start)")
    else:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last.get("at") or 0)))
        verdict = "OK" if last.get("ok") else "PROBLEM"
        print(f"bot-to-bot test : {verdict} at {when}: {describe(last)}")
    print()
    print("Telegram requirements for bot-to-bot visibility (check these in the app):")
    print("  1. Bot-to-Bot Communication Mode enabled for EVERY bot (via @BotFather).")
    print("  2. Each bot has admin rights in the group, or Group Privacy Mode disabled.")
    print("  3. All bots are members of the home channel.")
    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    if notes:
        print("\nNOTES:")
        for note in notes:
            print(f"  ! {note}")
    print("\nOK — config is usable.")
    return 0


async def _check_telegram(config: AgentConfig, problems: list, notes: list) -> str:
    """Ask Telegram what only it knows: the token works, it is the configured bot, and the bot
    is in the home channel with what bot-to-bot traffic needs (admin, or privacy off). An
    unreachable Telegram is a note, not a problem: check also runs offline."""
    identity = config.identity
    if not identity.token_is_present() or not identity.home_channel_id:
        return "skipped (no token or home_channel_id yet)"
    from aiogram import Bot
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, \
        TelegramUnauthorizedError
    bot = Bot(token=identity.bot_token)
    try:
        try:
            me = await asyncio.wait_for(bot.get_me(), 20)
        except TelegramUnauthorizedError:
            problems.append("Telegram rejects the bot token (getMe: Unauthorized)")
            return "token rejected"
        configured = identity.username.lstrip("@").lower()
        if me.username and configured and me.username.lower() != configured:
            problems.append(f"the token belongs to @{me.username}, but agent.name is "
                            f"{identity.username!r}")
        try:
            member = await asyncio.wait_for(
                bot.get_chat_member(identity.home_channel_id, me.id), 20)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            problems.append(f"@{me.username} is not in the home channel "
                            f"{identity.home_channel_id} ({exc.message}): add the bot to the "
                            "group and make it an admin")
            return f"@{me.username}: NOT IN THE GROUP"
        status = str(getattr(member.status, "value", member.status))
        if status in ("left", "kicked"):
            problems.append(f"@{me.username} has {status} the home channel: add it back")
            return f"@{me.username}: {status}"
        admin = status in ("administrator", "creator")
        if not admin and not me.can_read_all_group_messages:
            problems.append(f"@{me.username} is a plain member with Group Privacy on, so it "
                            "sees only commands and mentions: make it an admin, or disable "
                            "privacy in @BotFather")
        if status == "administrator" and getattr(member, "can_manage_topics", None) is False:
            notes.append(f"@{me.username} cannot manage topics: give it that admin right so "
                         "it can open a topic for a piece of work")
        if identity.admin_user_id:
            try:
                owner = await asyncio.wait_for(
                    bot.get_chat_member(identity.home_channel_id, identity.admin_user_id), 20)
                if getattr(owner, "is_anonymous", False):
                    problems.append("you post anonymously in the home channel (\"Remain "
                                    "anonymous\" is on for admin_user_id), so the bots can't "
                                    "tell your messages from any other admin's and ignore "
                                    "them: turn it off in the group's Administrators list")
            except Exception:                              # noqa: BLE001 - advisory only
                pass
        try:
            chat = await asyncio.wait_for(bot.get_chat(identity.home_channel_id), 20)
            if getattr(chat, "username", None):
                notes.append(f"the home channel is public (@{chat.username}): anyone can read "
                             "what the agents post, including repository links; make the "
                             "group private unless that is intended")
        except Exception:                                  # noqa: BLE001 - advisory only
            pass
        return f"@{me.username} is {status} of the home channel"
    except Exception as exc:                      # noqa: BLE001 - network, timeouts
        notes.append(f"could not ask Telegram about the bot ({type(exc).__name__}: {exc}); "
                     "run check again online, or pass --offline")
        return "not reachable"
    finally:
        await bot.session.close()


async def _whoami(config: AgentConfig) -> int:
    from aiogram import Bot
    bot = Bot(token=config.identity.bot_token)
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    print(json.dumps({"id": me.id, "username": me.username, "name": me.full_name,
                      "can_join_groups": me.can_join_groups,
                      "can_read_all_group_messages": me.can_read_all_group_messages}, indent=2))
    if not me.can_read_all_group_messages:
        print("\nnote: can_read_all_group_messages is False -> Group Privacy Mode is ON. "
              "That is fine while Bot-to-Bot Communication Mode is enabled and the bot is an "
              "admin of the group; otherwise disable privacy mode in @BotFather.")
    return 0


async def _discover(config: AgentConfig) -> int:
    """
    Print the chats this bot has seen, so the operator can copy the group id into
    ``agent.home_channel_id``. Uses the Bot API directly (no aiogram), and never prints the
    token. Send any message in the group mentioning the bot first, or the list will be empty.
    """
    import urllib.request

    token = config.identity.bot_token
    if not token:
        print("no bot token in the config — set agent.bot_token or agent.bot_token_env first",
              file=sys.stderr)
        return 2

    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=100"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:      # noqa: S310 - fixed https URL
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                                          # noqa: BLE001 - CLI feedback
        print(f"could not reach the Telegram API: {exc}", file=sys.stderr)
        return 2

    if not payload.get("ok"):
        print(f"Telegram refused the request: {payload.get('description')}", file=sys.stderr)
        return 2

    seen: dict = {}
    for update in payload.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            entry = update.get(key)
            if not entry:
                continue
            chat = entry.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            seen[chat_id] = {
                "type": chat.get("type"),
                "title": chat.get("title") or chat.get("username") or "(private)",
                "who": (entry.get("from") or {}).get("username"),
            }

    if not seen:
        print("No chats seen yet. Add the bot to the group (as admin), post anything in the "
              "group mentioning the bot, then run this again.")
        return 1

    print("Chats this bot can see:\n")
    for chat_id, info in seen.items():
        marker = "  ← use this as agent.home_channel_id" if str(chat_id).startswith("-100") else ""
        if not marker and info["type"] == "group" and any(
                str(other).startswith("-100") and seen[other]["title"] == info["title"]
                for other in seen):
            marker = "  (the same group before Telegram upgraded it; ignore)"
        print(f"  {chat_id}  type={info['type']:<10} {info['title']}{marker}")
    print("\nPut the -100… value into agent.home_channel_id in this agent's config.yaml "
          "(a message link copied from the group works too).")
    return 0


async def _send_with_retry(channel: AgentChannel, text: str, topic: Optional[int],
                           attempts: int = 3, first_wait: float = 2.0) -> None:
    """``send`` survives a dropped connection: a network error is retried with backoff
    (live test: one blip ended the command with a traceback). Telegram's own refusals
    are not retried."""
    from aiogram.exceptions import TelegramNetworkError
    for attempt in range(1, attempts + 1):
        try:
            await channel.send(text, thread_id=topic)
            return
        except TelegramNetworkError as exc:
            if attempt == attempts:
                raise
            wait = first_wait * 2 ** (attempt - 1)
            logging.getLogger("swarmly.telegram.cli").warning(
                "send: network error (%s); retrying in %.0fs", exc, wait)
            await asyncio.sleep(wait)


async def _send(config: AgentConfig, text: str, topic: Optional[int] = None) -> int:
    channel = AgentChannel(config)
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    channel.bot = Bot(token=config.identity.bot_token,
                      default=DefaultBotProperties(parse_mode="HTML"))
    # A wrong or deleted topic id fails here; it is not posted in General instead (live
    # test: probes meant for a deleted topic landed in General and the CLI said "sent").
    channel.reroute_gone_topics = False
    try:
        before = channel.last_sent_message_id
        try:
            await _send_with_retry(channel, text, topic)
        except TopicGone:
            partly = channel.last_sent_message_id != before   # a long message, split
            print(json.dumps({"state": "topic_gone", "topic": topic, "partly_posted": partly}))
            print(f"topic {topic} is closed, deleted or does not exist; "
                  + ("the first part was posted, the rest was not" if partly
                     else "nothing was posted"), file=sys.stderr)
            return 1
        print(json.dumps({"state": "sent", "telegram_message_id": channel.last_sent_message_id,
                          "topic": topic}))
    finally:
        await channel.bot.session.close()
    return 0


async def _post(config: AgentConfig, args) -> int:
    from swarmly.telegram.relay import post_and_notify
    channel = AgentChannel(config)
    notify = not args.no_telegram and config.identity.token_is_present()
    if notify:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        channel.bot = Bot(token=config.identity.bot_token,
                          default=DefaultBotProperties(parse_mode="HTML"))
    from swarmly.bus import MissingKeyError
    from swarmly.telegram.relay import NoBusError
    from swarmly.transports.git import BusPushError
    try:
        result = await post_and_notify(channel, to=args.to, text=args.text,
                                       intent=args.intent.upper(),
                                       telegram_type=args.telegram_type, notify=notify)
    except (MissingKeyError, NoBusError) as exc:
        print(f"Not sent: {exc}", file=sys.stderr)
        return 2
    except BusPushError as exc:
        print(f"Not published: {exc}", file=sys.stderr)
        return 2
    finally:
        if notify:
            await channel.bot.session.close()
    print(json.dumps(result.as_dict()))
    return 0


def _reply(config, args) -> int:
    from swarmly.telegram.replies import ReplyStore, reply_state_path
    store = ReplyStore(reply_state_path(config))
    if args.command == "reply":
        store.submit(args.ticket, text=args.text, skip_reason=args.skip)
        deadline = time.monotonic() + max(0, min(args.wait, 60))
        while store.get(args.ticket)['state'] in ('ready', 'claimed', 'sending') and time.monotonic() < deadline:
            time.sleep(0.25)
    result = store.public_status(args.ticket)
    print(json.dumps(result))
    if args.command == "reply-status" or result['state'] in ('sent', 'skipped'):
        return 0
    return 3  # durable submission is NOT proof of Telegram delivery


async def _run(config: AgentConfig) -> int:
    """Start the long-poll transport under the single-instance lock (issue #77).

    A restart task racing the live daemon used to start a SECOND ``getUpdates``
    consumer on the same token, splitting the updates between the two silently. The
    lock (in the bus repo's ``.agents/state``, beside the heartbeat) makes that
    impossible: exit 3 means a live transport already owns it and this start was
    refused. It is released only once the channel has stopped; a killed process
    leaves a stale marker that the next start takes over.
    """
    from swarmly.telegram.poll import (RUN_LOCK_EXIT_CODE, acquire_run_lock,
                                   release_run_lock, transport_state_root)

    root = transport_state_root(config)
    lock = acquire_run_lock(root, config.identity.alias)
    if not lock.acquired:
        if lock.holder_pid is not None:
            print(f"transport already running: pid {lock.holder_pid} "
                  f"started {_lock_started_text(lock.holder_started_at)} "
                  f"(lock {lock.path}); a second poller on the same token is "
                  f"refused (exit {RUN_LOCK_EXIT_CODE})")
        else:
            print(f"transport lock not acquired: {lock.note} (lock {lock.path}); "
                  f"a second poller on the same token is refused "
                  f"(exit {RUN_LOCK_EXIT_CODE})")
        return RUN_LOCK_EXIT_CODE
    if lock.note:
        logging.getLogger("swarmly.telegram.cli").info(
            "run: took over the instance lock (%s)", lock.note)
    # Issue #123: the transport (and only the transport) arms the adapter's fail-fast
    # gate -- one-shot bus-poll and CI keep the documented blocking fallback.
    channel = AgentChannel(config, transport=True)
    try:
        await channel.start()
    finally:
        try:
            await channel.stop()
        finally:
            # Released only after the transport stopped: the next start may begin
            # once this getUpdates consumer is gone, not a moment earlier.
            release_run_lock(root, config.identity.alias)
    return 0


def _lock_started_text(epoch: Optional[float]) -> str:
    """Local ISO 8601 for a lock holder's start time ("unknown" stays honest)."""
    if not epoch:
        return "at an unknown time"
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


async def _bus_poll(config: AgentConfig, args) -> int:
    """One-shot durable-bus poll — no Telegram token or transport required.

    Issue #59 deliverable 1: this subcommand is the per-host scheduler entry
    point. It must run with NO bot token validation and NO network to Telegram
    — the bus is the only thing it touches. Exit 0 on success, 1 on config
    problems that affect the next cycle, 2 only when the cycle could not start.
    """
    from swarmly.telegram.channel import AgentChannel
    from swarmly.telegram.poll import bus_poll_once

    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    problems = config.validate(require_token=False)
    if problems:
        print("config problems:", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 2

    # No Telegram token, no Telegram transport. The poller uses the bus.
    channel = AgentChannel(config)
    try:
        outcome = await bus_poll_once(channel)
    except Exception as exc:                          # noqa: BLE001
        print(f"bus-poll failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Defensive cleanup; the channel never started a transport.
        try:
            await channel.adapter.close()
        except Exception:                              # noqa: BLE001
            pass

    if getattr(args, "json", False):
        print(json.dumps({
            "delivered": outcome.delivered,
            "deferred": outcome.deferred,
            "rejected": outcome.rejected,
            "machine_events": outcome.machine_events,
            "delivery_verified": outcome.delivery_verified,
            "delivery_unknown": outcome.delivery_unknown,
            "escalations": outcome.escalations,
            "bus_write_failures": outcome.bus_write_failures,
            "bus_write_failure_details": outcome.bus_write_failure_details,
            "bus_writes": outcome.bus_writes,
            "replies_unposted": outcome.replies_unposted,
            "skipped": outcome.skipped,
            "telegram_ok": outcome.telegram_ok,
            "telegram_unavailable": outcome.telegram_unavailable,
            "tokens": outcome.tokens,
            "lane_delivered": outcome.lane_delivered,
            "lane_deferred": outcome.lane_deferred,
            "lane_dead_letters": outcome.lane_dead_letters,
            "lane_quarantined": outcome.lane_quarantined,
            "lane_error": outcome.lane_error,
        }))
    elif not getattr(args, "quiet", False):
        print(f"bus-poll --once  delivered={len(outcome.delivered)} "
              f"deferred={len(outcome.deferred)} "
              f"rejected={len(outcome.rejected)} "
              f"machine_events={len(outcome.machine_events)} "
              f"delivery_unknown={len(outcome.delivery_unknown)} "
              f"escalations={len(outcome.escalations)} "
              f"bus_write_failures={outcome.bus_write_failures} "
              f"skipped={outcome.skipped or '-'}")
        if outcome.delivered:
            print(f"  delivered: {', '.join(outcome.delivered)}")
        if outcome.deferred:
            print(f"  deferred:  {', '.join(outcome.deferred)}")
        for rejection in outcome.rejected:
            # Ids and senders come from the bus: no control characters to the terminal.
            line = f"{rejection.get('id')} from {rejection.get('sender')!r}: " \
                   f"{rejection.get('reason')}"
            print("  rejected:  " + re.sub(r"[\x00-\x1f\x7f]", "?", line))
        if outcome.delivery_unknown:
            print(f"  delivery_unknown: {', '.join(outcome.delivery_unknown)}")
        if outcome.escalations:
            for escalation in outcome.escalations:
                print(f"  ESCALATED: {escalation.get('intent')} "
                      f"ticket={escalation.get('ticket_id')} "
                      f"envelope={escalation.get('envelope_id')}")
        for detail in outcome.bus_write_failure_details:
            print(f"  BUS WRITE FAILED: {detail}")
        if outcome.lane_delivered or outcome.lane_deferred or outcome.lane_dead_letters \
                or outcome.lane_quarantined or outcome.lane_error:
            print(f"  lanes: delivered={len(outcome.lane_delivered)} "
                  f"waiting={len(outcome.lane_deferred)} "
                  f"dead_letters={len(outcome.lane_dead_letters)} "
                  f"quarantined={len(outcome.lane_quarantined)}")
            for name, items in (("delivered", outcome.lane_delivered),
                                ("waiting", outcome.lane_deferred),
                                ("DEAD-LETTERED", outcome.lane_dead_letters)):
                if items:
                    print(f"  lane {name}: {', '.join(items)}")
            for q in outcome.lane_quarantined:
                print(f"  lane QUARANTINED {q['id']}: {'; '.join(q['errors'])}")
            if outcome.lane_error:
                print(f"  LANES UNREADABLE: {outcome.lane_error}")
    return 0


async def _gh_poll(config: AgentConfig, args) -> int:
    """One-shot GitHub backstop cycle — no Telegram token or transport required."""
    from swarmly.github.watch import github_poll_once

    if not args.once:
        print("gh-poll requires --once (one-shot mode for per-host schedulers)", file=sys.stderr)
        return 2

    problems = config.validate(require_token=False)
    if problems:
        print("config problems:", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 2

    try:
        outcome = await github_poll_once(config)
    except Exception as exc:
        print(f"gh-poll failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({
            "polled_repos": outcome.polled_repos,
            "notifications_seen": outcome.notifications_seen,
            "turns_queued": outcome.turns_queued,
            "turns_skipped": outcome.turns_skipped,
            "errors": outcome.errors,
            "delivered_topics": [list(t) for t in outcome.delivered_topics],
        }))
    elif not getattr(args, "quiet", False):
        print(f"gh-poll --once  repos={len(outcome.polled_repos)} "
              f"seen={outcome.notifications_seen} "
              f"queued={outcome.turns_queued} "
              f"skipped={outcome.turns_skipped} "
              f"errors={len(outcome.errors)}")
        if outcome.errors:
            for err in outcome.errors:
                print(f"  error: {err}")
    # A poll that could not reach GitHub is a failure, not a quiet "nothing to do".
    return 1 if outcome.errors else 0


def _heartbeat_check(config: AgentConfig, args) -> int:
    """Watchdog entry point for the canonical beat (issue #68).

    Exit 0 = fresh, 3 = stale (and, with ``--alert``, a channel alert was
    attempted). Scheduling this on the host means a dead transport is noticed
    without a human reading logs — the alert names this host's peers so their
    bots actually see it.
    """
    from swarmly.telegram.poll import heartbeat_status

    status = heartbeat_status(config, factor=args.factor)
    if args.json:
        print(json.dumps(status))
    else:
        age = status["age_seconds"]
        age_text = f", age={age:.0f}s" if age is not None else ""
        print(f"heartbeat: {status['detail']}{age_text} "
              f"(threshold={status['threshold_seconds']:.0f}s, {status['path']})")
    transport_state = status.get("transport_state")
    if not status["stale"] and transport_state in ("dead", "degraded"):
        # Issue #123: a fresh beat used to be enough -- and it was, while the
        # scheduled poller kept writing beats for a transport that could not
        # deliver. Now the transport block is checked too.
        detail = status.get("transport_detail") or transport_state
        if not args.json:
            print(f"heartbeat: beat fresh but transport {transport_state} ({detail})",
                  file=sys.stderr)
        if args.alert:
            who = " ".join(f"@{str(u).lstrip('@')}" for u in config.peers.values()
                           if u and not str(u).isdigit())
            text = (f"{who + ' ' if who else ''}[watchdog] {config.identity.alias}: beat is "
                    f"fresh but its transport is {transport_state} -- {detail}. "
                    f"Check the transport daemon on this host.")
            try:
                code = asyncio.run(_send(config, text))
            except Exception as exc:                   # noqa: BLE001 - the alert is best-effort
                print(f"heartbeat alert failed: {exc}", file=sys.stderr)
                return 3
            if code != 0:
                return 3
        return 3
    if not status["stale"]:
        return 0
    if args.alert:
        who = " ".join(f"@{str(u).lstrip('@')}" for u in config.peers.values()
                       if u and not str(u).isdigit())
        counters = ", ".join(f"{key}={value}" for key, value in (status["counters"] or {}).items()
                             if value is not None) or "no counters"
        age = status["age_seconds"]
        age_text = f"{age:.0f}s" if age is not None else "never"
        text = (f"{who + ' ' if who else ''}[watchdog] {config.identity.alias} beat is stale: "
                f"last beat {age_text} ago (> {status['threshold_seconds']:.0f}s); {counters}. "
                f"Check the transport daemon on this host.")
        try:
            code = asyncio.run(_send(config, text))
        except Exception as exc:                       # noqa: BLE001 - the alert is best-effort
            print(f"heartbeat alert failed: {exc}", file=sys.stderr)
            return 3
        if code != 0:
            return 3
    return 3


def _describe_bus(config: AgentConfig, problems: list, notes: Optional[list] = None) -> str:
    """One line naming the durable-plane transport; adds a problem when it cannot work here
    and a note when it works but probably not as meant."""
    from swarmly.transports import TransportError, build_transport
    bus = config.bus
    if not bus.enabled or bus.poll_minutes <= 0:
        return f"{bus.transport} (polling disabled)"
    if not bus.repo:
        if bus.swarm:
            problems.append("bus.swarm is set but bus.repo is not: swarm lanes are not "
                            "polled; set bus.repo to this agent's checkout")
        if notes is not None:
            # P0-4 re-review m3: polling is on but reads nothing; say so, not just "OK".
            notes.append("the bus is not polled: bus.repo is unset (set it to this agent's "
                         "checkout, or bus.enabled: false if that is intended)")
        return f"{bus.transport} (NOT POLLED: no bus.repo; set it, or bus.enabled: false)"
    try:
        transport = build_transport(bus.transport, repo=Path(bus.repo or "."),
                                    ref=config.fallback.gitbus_ref, folder=bus.folder,
                                    gdrive=bus.gdrive)
    except TransportError as exc:
        return f"{bus.transport} (INVALID: {exc})"
    _check_signing_key(config, transport, problems)
    if bus.transport == "gdrive":
        try:
            import googleapiclient  # noqa: F401
        except ImportError:
            problems.append('bus.transport is gdrive but the Google client libraries are '
                            'missing: pip install "swarmly[gdrive]"')
    return transport.describe()


def _check_signing_key(config: AgentConfig, transport: object, problems: list) -> None:
    """Every bus write (``post``, the poller's escalations) is signed with this agent's own
    key, and is refused without it; say so here rather than at the first write."""
    from swarmly.bus import DurableBus, MissingKeyError
    alias = config.identity.alias.lower()
    try:
        DurableBus(root_dir=Path(config.bus.repo or "."), transport=transport,
                   create_dirs=False).signing_key(alias)          # check changes nothing
    except MissingKeyError as exc:
        problems.append(f"{exc.args[0]}; this agent's bus posts and escalations will "
                        "be refused")
    except Exception as exc:  # noqa: BLE001 - a broken registry is reported, not raised
        problems.append(f"cannot read the signing key for {alias!r}: {exc}")


def _init(args: argparse.Namespace) -> int:
    from swarmly.telegram.init import InitError, write_from, write_starter
    try:
        if args.from_config is not None:
            if args.bus_repo is None:
                raise InitError("--from needs --bus-repo, the new project's clone")
            lines = write_from(Path(args.config), args.from_config, args.bus_repo, cwd=args.cwd,
                               home_channel=args.home_channel, force=args.force,
                               bus_branch=args.bus_branch.strip() or None)
        elif args.home_channel:
            raise InitError("--home-channel goes with --from; for a new config set "
                            "agent.home_channel_id in the file")
        else:
            lines = write_starter(Path(args.config), args.alias, args.adapter.strip().lower(),
                                  bus_repo=args.bus_repo, cwd=args.cwd, force=args.force,
                                  role=args.role, bus_branch=args.bus_branch.strip() or None)
    except InitError as exc:
        print(f"init: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _init(args)
    configure_logging(f"swarmly-telegram-{args.command}", verbose=args.verbose,
                      directory=args.log_dir)
    try:
        config = AgentConfig.load(args.config)
    except ConfigError as exc:
        logging.getLogger("swarmly.telegram.cli").error("config error in %s: %s", args.config, exc)
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    # One file per agent and command, so two processes never rotate the same file.
    log_path = configure_logging(f"swarmly-telegram-{config.identity.alias}-{args.command}",
                                 verbose=args.verbose, directory=args.log_dir)
    logging.getLogger("swarmly.telegram.cli").info(
        "swarmly-telegram %s for %s (version %s, config %s, debug log %s)", args.command,
        config.identity.alias, __version__, args.config, log_path or "off")

    handlers = {"check": _check, "whoami": _whoami, "run": _run, "discover": _discover}
    try:
        if args.command in ("reply", "reply-status"):
            return _reply(config, args)
        if args.command == "send":
            return asyncio.run(_send(config, args.text, topic=args.topic))
        if args.command == "post":
            return asyncio.run(_post(config, args))
        if args.command == "bus-poll":
            return asyncio.run(_bus_poll(config, args))
        if args.command in ("gh-poll", "github-poll"):
            return asyncio.run(_gh_poll(config, args))
        if args.command == "heartbeat-check":
            return _heartbeat_check(config, args)
        if args.command == "check":
            return asyncio.run(_check(config, live=not args.offline))
        return asyncio.run(handlers[args.command](config))
    except KeyboardInterrupt:
        logging.getLogger("swarmly.telegram.cli").info("stopped by keyboard interrupt")
        print("\nstopped")
        return 130
    except ValueError as exc:
        logging.getLogger("swarmly.telegram.cli").error("reply error: %s", exc)
        print(f"reply error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Keep the traceback in the debug log; a crashed service often has no console.
        logging.getLogger("swarmly.telegram.cli").exception("swarmly-telegram %s crashed", args.command)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
