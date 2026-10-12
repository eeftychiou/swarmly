"""``swarmly``: the swarm governor on the command line.

Every write goes through ``swarmly post`` (or the MCP server, ``swarmly mcp``). There is no
default identity: pass ``--as <agent>`` or set ``A2A_AGENT``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logsetup import configure as configure_logging
from . import keys
from .doc import render_default
from .doorbell import doorbell_from_swarm
from .envelope import EnvelopeError, parse_evidence
from .governor import Governor, Rejected
from .lanes import LaneConflict, store_from_config
from .protocol import SwarmConfigError, load_swarm

DOC_PATH = Path("docs/SWARM-PROTOCOL.md")
log = logging.getLogger("swarmly.swarm.cli")


def _agent(args: argparse.Namespace) -> str:
    agent = args.agent or os.environ.get("A2A_AGENT")
    if not agent:
        raise SwarmConfigError("no agent identity: pass --as <agent> or set A2A_AGENT")
    return agent


def _governor(args: argparse.Namespace) -> Governor:
    swarm = load_swarm(args.swarm)
    store = store_from_config(swarm.bus, swarm.project)
    agent = _agent(args)
    return Governor(swarm, store, agent, doorbell=doorbell_from_swarm(swarm, agent))


def _print(data: Any, as_json: bool, text: Optional[str] = None) -> None:
    if as_json or text is None:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(text)


def _body(args: argparse.Namespace) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if args.body_json:
        loaded = json.loads(args.body_json)
        if not isinstance(loaded, dict):
            raise SwarmConfigError("--body-json must be a JSON object")
        body.update(loaded)
    for item in args.body or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SwarmConfigError(f"--body expects key=value, got {item!r}")
        body[key] = value
    if args.text is not None:
        body.setdefault("text", args.text)
    if args.option:
        body["options"] = list(args.option)
    return body


def _moves_text(moves: List[Dict[str, Any]]) -> str:
    lines = []
    for m in moves:
        where = " ".join(f"{k}={m[k]}" for k in ("task_id", "re", "epoch") if k in m)
        needs = ", ".join(m.get("body") or [])
        ev = f"; evidence x{m['evidence']}" if m.get("evidence") else ""
        lines.append(f"  {m['intent']:<15} {where:<28} needs: {needs or '-'}{ev}")
    return "\n".join(lines)


def cmd_keygen(args: argparse.Namespace) -> int:
    agent = _agent(args)
    path, pub = keys.generate(agent, overwrite=args.force)
    print(f"wrote {path}")
    print("add this to swarm.yaml under agents:")
    print(f"  {agent}:\n    key: {pub}")
    return 0


def cmd_bus_init(args: argparse.Namespace) -> int:
    swarm = load_swarm(args.swarm)
    bus = dict(swarm.bus)
    if args.url:
        bus["url"] = args.url
    store = store_from_config(bus, swarm.project)
    store.sync()
    print(f"bus ready: {getattr(store, 'repo', getattr(store, 'root', ''))}; "
          f"lanes: {', '.join(store.lanes()) or '(none yet)'}")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    gov = _governor(args)
    evidence = [parse_evidence(e, gov.swarm.protocol.evidence_kinds) for e in args.evidence or []]
    to = [t.strip() for t in args.to.split(",") if t.strip()] if args.to else None
    try:
        env = gov.post(args.intent.upper(), to=to, task_id=args.task, body=_body(args),
                       evidence=evidence, re=args.re, context_id=args.context, epoch=args.epoch)
    except Rejected as rej:
        if args.json:
            _print(rej.to_dict(), True)
        else:
            print("rejected:", file=sys.stderr)
            for err in rej.errors:
                print(f"  - {err}", file=sys.stderr)
            print("legal moves now:", file=sys.stderr)
            print(_moves_text(rej.legal), file=sys.stderr)
        return 3
    _print({"ok": True, "envelope": env.to_dict()}, args.json,
           f"posted {env.id} {env.intent}" + (f" task={env.task_id} epoch={env.epoch}"
                                              if env.task_id else ""))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    gov = _governor(args)
    data = gov.next()
    if args.json:
        _print(data, True)
        return 0
    print(f"{data['agent']} ({data['role']})")
    if data.get("my_quarantined"):
        print(f"WARNING: {len(data['my_quarantined'])} of your envelopes are quarantined")
    print("you owe:" if data["obligations"] else "you owe nothing right now.")
    for ob in data["obligations"]:
        ref = ob.get("task_id") or ob.get("re") or ""
        print(f"  {ob['do']:<15} {ref:<28} {ob['why']}")
    if data.get("escalations"):
        print("escalations for the owner:")
        for e in data["escalations"]:
            print(f"  [{e['kind']}] {e['reason']} (deadline {e['deadline']}"
                  f"{', OVERDUE' if e['overdue'] else ''})")
    print("legal moves:")
    print(_moves_text(data["legal_moves"]))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    gov = _governor(args)
    envs = gov.view().inbox(gov.agent, limit=args.limit)
    if args.json:
        _print([e.to_dict() for e in envs], True)
        return 0
    for e in envs:
        body = e.body.get("text") or e.body.get("title") or e.body.get("reason") or ""
        print(f"{e.ts[:19]} {e.id:<16} {e.intent:<14} {e.task_id or '':<20} {body}")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    gov = _governor(args)
    proj = gov.view()
    terminal = gov.swarm.protocol.terminal_states
    tasks = [t for t in proj.tasks.values() if args.all or t.state not in terminal]
    if args.json:
        _print([t.to_dict() for t in tasks], True)
        return 0
    for t in tasks:
        print(f"{t.id:<24} {t.state:<18} {t.claimant or '-':<12} e{t.epoch} "
              f"fail={t.failures} {t.title}")
    return 0


def cmd_escalations(args: argparse.Namespace) -> int:
    gov = _governor(args)
    items = gov.view().escalations()
    if args.json:
        _print(items, True)
        return 0
    if not items:
        print("nothing needs the owner.")
    for e in items:
        print(f"[{e['kind']}] {e['reason']}\n    answer with {e['answer_with']}"
              f"{' re=' + e['id'] if e.get('id') else ''}; deadline {e['deadline']}"
              f"{' (OVERDUE: ' + e['safe_default'] + ')' if e['overdue'] else ''}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    gov = _governor(args)
    proj = gov.view()
    data = {"envelopes": len(proj.log), "violations": [v.__dict__ for v in proj.violations]}
    _print(data, args.json, f"{len(proj.log)} envelopes accepted, "
           f"{len(proj.violations)} quarantined" + "".join(
               f"\n  {v.id}: {'; '.join(v.errors)}" for v in proj.violations))
    return 1 if proj.violations else 0


def cmd_guard(args: argparse.Namespace) -> int:
    """Exit 0 to allow, 2 to block. Every error blocks: a hook treats any other code as allow."""
    try:
        command = " ".join(args.command).strip() or None
        paths = list(args.path or [])
        writes = not args.read
        if args.hook:
            command, paths, writes = _from_hook(sys.stdin.read())
        if not command and not paths:
            return 0
        gov = _governor(args)
        result = gov.guard(command, paths, writes=writes, sync=not args.offline)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        log.exception("guard failed, blocking")
        print(f"swarmly guard: blocked because the check itself failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print(result.to_dict(), True)
    elif not result.allowed:
        print(result.message(), file=sys.stderr)
    return 0 if result.allowed else 2


def _from_hook(raw: str) -> "tuple[Optional[str], List[str], bool]":
    """Read a PreToolUse hook payload (Claude Code style) from stdin.

    Raises ValueError on a payload it cannot read, which the guard turns into a block.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook payload is not a JSON object")
    tool = str(data.get("tool_name") or "")
    tin = data.get("tool_input") or {}
    if not isinstance(tin, dict):
        raise ValueError("hook payload has no tool_input object")
    command = tin.get("command") if isinstance(tin.get("command"), str) else None
    paths = [str(tin[k]) for k in ("file_path", "path", "notebook_path") if tin.get(k)]
    writes = tool not in ("Read", "Grep", "Glob", "LS")
    return command, paths, writes


def cmd_mcp(args: argparse.Namespace) -> int:
    from .. import __file__ as package_file, __version__
    from .mcp import serve
    # Which copy of swarmly this is: the launcher's argv alone does not say.
    log.info("mcp server %s from %s (python %s)", __version__,
             Path(package_file).resolve().parent, sys.executable)
    return serve(_governor(args))


def _mcp_identity(args: argparse.Namespace, swarm: Any, cli: str) -> Optional[str]:
    """Why ``--as`` is the wrong identity for this CLI, or None.

    The entry pins one identity into a file every session of that CLI reads, so a typo
    here would sign the wrong agent's posts for good.
    """
    from . import mcp_setup as ms
    agent = args.agent
    if not agent:
        return "mcp-setup needs --as <agent> (A2A_AGENT is not used: the entry is permanent)"
    if agent not in swarm.agents:
        return f"{agent!r} is not in the swarm roster ({', '.join(sorted(swarm.agents))})"
    if agent == swarm.owner:
        return f"{agent} is the owner; the owner answers on Telegram, not through an agent CLI"
    vendor = swarm.agents[agent].vendor
    if not args.force and vendor.lower() != ms.VENDORS[cli]:
        return (f"{agent} is a {vendor} agent but {cli} is {ms.VENDORS[cli]}'s CLI; pass "
                "--force if that is really what you want")
    return None


def cmd_mcp_setup(args: argparse.Namespace) -> int:
    import difflib
    import shlex
    import shutil
    from . import mcp_setup as ms
    from .protocol import find_swarm_file
    cli = args.cli
    swarm_file = find_swarm_file(args.swarm).resolve()
    swarm = load_swarm(str(swarm_file))                 # fail now, not inside the CLI
    problem = _mcp_identity(args, swarm, cli)
    if problem:
        raise ms.SetupError(problem)
    agent = args.agent
    bus_path = swarm.bus.get("path")
    if bus_path and not Path(str(bus_path)).expanduser().is_absolute():
        raise ms.SetupError(f"bus.path {bus_path!r} is relative; the CLIs start the server in "
                            "other directories, so make it absolute in swarm.yaml")
    command, cmd_args = ms.server_command(swarm_file, agent, args.python)
    env = ms.server_env()
    print(f"{cli} will act as {agent} ({swarm.role_of(agent)}, {swarm.agents[agent].vendor}) "
          f"in {swarm.project}")
    key_problem = ms.check_key(swarm, agent, env)
    if key_problem:
        print(f"swarmly: warning: {key_problem}", file=sys.stderr)
    entry: Optional[Dict[str, Any]] = None
    if cli == "claude":
        # shutil.which finds claude.cmd on Windows, which subprocess alone does not.
        claude = shutil.which("claude")
        line = ms.claude_command(command, cmd_args, env, claude or "claude")
        entry = {"command": command, "args": cmd_args, "env": env}
        if args.write:
            if claude is None:
                raise ms.SetupError("the claude command is not on PATH")

            def run(argv: List[str]) -> "subprocess.CompletedProcess[str]":
                return subprocess.run(argv, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=120)
            done = run(line)
            if done.returncode != 0 and "already exists" in (done.stderr + done.stdout):
                # Replace it. If the second add fails the old entry is gone; say so.
                run([claude, "mcp", "remove", "--scope", "user", ms.NAME])
                done = run(line)
            if done.returncode != 0:
                raise ms.SetupError(f"claude mcp add failed (no a2a entry is configured now): "
                                    f"{(done.stderr or done.stdout).strip()}")
            got = run([claude, "mcp", "get", ms.NAME])
            if got.returncode != 0 or str(swarm_file) not in got.stdout:
                raise ms.SetupError(f"claude mcp get {ms.NAME} does not show the new entry: "
                                    f"{(got.stderr or got.stdout).strip()}")
            print(f"added the {ms.NAME} MCP server to Claude Code (user scope)")
        else:
            show = ["claude", *line[1:]]
            print(subprocess.list2cmdline(show) if os.name == "nt" else shlex.join(show))
    else:
        path = Path(args.config).expanduser() if args.config else ms.config_path(cli)
        assert path is not None
        ours = ms.our_entry(cli, command, cmd_args, env, cwd=str(swarm_file.parent),
                            token_env=ms.token_env_name(swarm, agent))
        try:
            current = ms.read_text(path)
            text = ms.render(cli, ours, current)
            entry = ms.entry_in(cli, text) or ours
        except (OSError, ms.SetupError) as exc:
            raise ms.SetupError(f"{path}: {exc}") from None
        if text == current:
            print(f"{path} already has this {ms.NAME} server")
        elif args.write:
            backup = ms.write(path, text)
            print(f"wrote the {ms.NAME} MCP server into {path}"
                  + (f" (old file kept as {backup})" if backup else ""))
        else:
            sys.stdout.writelines(difflib.unified_diff(
                current.splitlines(keepends=True), text.splitlines(keepends=True),
                fromfile=f"{path} (now)", tofile=f"{path} (after --write)"))
            print("# run again with --write to apply")
    if args.check:
        assert entry is not None
        full_env = ms.cli_env(cli, {str(k): str(v) for k, v in
                                    (entry.get("env") or {}).items()},
                              tuple(str(n) for n in entry.get("env_vars") or []))
        result = ms.check(str(entry["command"]), [str(a) for a in entry.get("args") or []],
                          full_env=full_env)
        log.info("mcp-setup check for %s as %s: %s", cli, agent, result)
        print(ms.describe_check(result))
        if not result["ok"]:
            return 1
        if result["agent"] != agent:
            print(f"swarmly: the server answered as {result['agent']}, not {agent}",
                  file=sys.stderr)
            return 1
    return 0


def cmd_protocol_doc(args: argparse.Namespace) -> int:
    text = render_default()
    if args.check:
        path = Path(args.check)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            print(f"{path} is out of date: run `swarmly protocol-doc --out {path}`", file=sys.stderr)
            return 1
        return 0
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarmly", description="The Swarmly swarm governor.")
    p.add_argument("--swarm", help="path to swarm.yaml (default: $A2A_SWARM or ./swarm.yaml)")
    p.add_argument("--as", dest="agent", help="agent identity (default: $A2A_AGENT)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    p.add_argument("--log-dir", help="where the rotating debug log goes (default: "
                                     "$A2A_LOG_DIR or ~/.a2a/logs; 'off' disables it)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("keygen", help="create this host's key for --as")
    s.add_argument("--force", action="store_true", help="replace an existing key")
    s.set_defaults(func=cmd_keygen)

    s = sub.add_parser("bus-init", help="create the bus clone named in swarm.yaml")
    s.add_argument("--url", help="git URL of the repository that holds the lanes")
    s.set_defaults(func=cmd_bus_init)

    s = sub.add_parser("post", help="validate, sign and append one envelope")
    s.add_argument("intent")
    s.add_argument("--task", help="task_id")
    s.add_argument("--to", help="comma-separated recipients (default: *)")
    s.add_argument("--re", help="the envelope id this answers")
    s.add_argument("--text", help="shortcut for --body text=...")
    s.add_argument("--body", action="append", metavar="KEY=VALUE")
    s.add_argument("--body-json", help="body fields as a JSON object")
    s.add_argument("--option", action="append", help="ESCALATE option (repeatable)")
    s.add_argument("--evidence", action="append", metavar="KIND:REF")
    s.add_argument("--epoch", type=int, help="override the stamped epoch")
    s.add_argument("--context", help="context_id (default: the task's)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_post)

    for name, func, helptext in (("next", cmd_next, "what you owe and what you may do"),
                                 ("escalations", cmd_escalations, "what needs the owner"),
                                 ("verify", cmd_verify, "check every lane; exit 1 on violations")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=func)

    s = sub.add_parser("inbox", help="recent envelopes addressed to you")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("tasks", help="open tasks")
    s.add_argument("--all", action="store_true", help="include done and rejected")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_tasks)

    s = sub.add_parser("guard", help="exit 2 when a command or path needs owner approval")
    s.add_argument("--path", action="append", help="a file about to be written (repeatable)")
    s.add_argument("--read", action="store_true", help="the paths are only read")
    s.add_argument("--hook", action="store_true",
                   help="read a PreToolUse hook payload from stdin")
    s.add_argument("--offline", action="store_true", help="do not fetch lanes first")
    s.add_argument("--json", action="store_true")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- the command to check")
    s.set_defaults(func=cmd_guard)

    s = sub.add_parser("mcp", help="serve the governor as an MCP server on stdio")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("mcp-setup", help="connect an agent CLI to this swarm's MCP server")
    s.add_argument("cli", choices=["claude", "codex", "antigravity", "gemini", "hermes"])
    s.add_argument("--write", action="store_true",
                   help="apply it (default: print what would change)")
    s.add_argument("--check", action="store_true",
                   help="start the server as the CLI will and check the MCP handshake")
    s.add_argument("--config", help="the CLI's config file (default: its usual place)")
    s.add_argument("--python", help="interpreter that runs the server (default: this one)")
    s.add_argument("--force", action="store_true",
                   help="allow an agent whose vendor does not match the CLI")
    s.set_defaults(func=cmd_mcp_setup)

    s = sub.add_parser("protocol-doc", help="render docs/SWARM-PROTOCOL.md from protocol.yaml")
    s.add_argument("--out", help="write to this file")
    s.add_argument("--check", help="exit 1 when this file differs from the rendered doc")
    s.set_defaults(func=cmd_protocol_doc)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    agent = args.agent or os.environ.get("A2A_AGENT") or "anonymous"
    # The console stays quiet unless -v: stdout carries MCP and JSON, and a hook's
    # stderr is shown to the agent. The file always gets DEBUG.
    configure_logging(f"a2a-{agent}-{args.cmd}", verbose=args.verbose,
                      directory=args.log_dir, console=args.verbose)
    log.debug("swarmly %s as %s", args.cmd, agent)
    try:
        return int(args.func(args) or 0)
    except (SwarmConfigError, EnvelopeError, LaneConflict, ValueError) as exc:
        log.error("swarmly %s failed: %s", args.cmd, exc)
        print(f"swarmly: {exc}", file=sys.stderr)
        return 1
    except Exception:
        log.exception("swarmly %s crashed", args.cmd)
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
