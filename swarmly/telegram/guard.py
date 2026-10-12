"""Role guard: refuse the git and gh commands an agent's role rules out.

Role prompts say "the coordinator merges; you do not", but a prompt fades over a long
session. With ``agent.role`` set, the transport guards the two ways an agent changes what
others see:

- ``gh``: a wrapper first on the agent's PATH checks the command against the role and either
  runs the real ``gh`` or exits with a one-line reason the agent reads back.
- ``git push``: a ``pre-push`` hook, set through ``GIT_CONFIG_*`` in the agent's
  environment, so it holds whichever ``git`` runs (Git Bash puts its own first on PATH).
  The other hooks pass through to the repository's own.

It is a guard against drift, not a security boundary: an agent can still call a program by
its full path or unset the variables. This file imports only the standard library, so the
wrappers run it directly (``python -I -S guard.py``) and stay fast.

    python guard.py gh <args...>
    python guard.py pre-push <remote> <url>   (refs on stdin, as git gives them)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

#: Branches only the coordinator updates directly (others push a branch and open a PR).
PROTECTED = ("main", "master")
#: Exit status of a refused command.
REFUSED = 3
TOOLS = ("gh", "git")


def _words(args: Sequence[str]) -> List[str]:
    return [a for a in args if not a.startswith("-")]


#: git options before the subcommand that take a separate value.
_GIT_VALUE_OPTIONS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")


def split_git(args: Sequence[str]) -> "tuple[List[str], List[str]]":
    """``git`` arguments as (global options, subcommand and its arguments)."""
    i, args = 0, list(args)
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    return args[:i], args[i:]


def _push_targets_protected(args: Sequence[str], current_branch: Optional[str]) -> bool:
    """Whether ``git push <args>`` would update main/master."""
    words = _words(args)
    refspecs = words[1:]                      # words[0] is the remote
    if not refspecs:
        return (current_branch or "") in PROTECTED
    for spec in refspecs:
        target = spec.split(":", 1)[-1].lstrip("+")
        target = target.removeprefix("refs/heads/")
        if target in PROTECTED or (target == "HEAD" and (current_branch or "") in PROTECTED):
            return True
    return False


def refusal(role: str, tool: str, args: Sequence[str],
            current_branch: Optional[str] = None) -> Optional[str]:
    """Why ``role`` may not run ``tool args``, or None when it may."""
    role = (role or "").strip().lower()
    if not role or role == "coordinator":
        return None
    if tool == "git":
        args = split_git(args)[1]
    words = [w.lower() for w in _words(args)]
    if tool == "gh" and words[:2] == ["pr", "merge"]:
        return (f"the {role} does not merge pull requests; say it is ready and the "
                "coordinator merges it")
    if tool == "gh" and role == "researcher" and words[:2] == ["pr", "create"]:
        return ("the researcher does not open pull requests; post the findings on the issue "
                "and hand the build to a developer")
    if tool == "git" and words[:1] == ["push"]:
        if role in ("reviewer", "researcher"):
            return (f"the {role} does not push; leave the change to the author or a "
                    "developer")
        if _push_targets_protected(args[1:], current_branch):
            return (f"the {role} does not push to {'/'.join(PROTECTED)}; push a branch and "
                    "open a pull request")
    if tool == "git" and words[:1] == ["merge"] and current_branch in PROTECTED:
        return (f"the {role} does not merge into {current_branch}; the coordinator merges")
    return None


def _current_branch(real_git: str, global_options: Sequence[str] = ()) -> Optional[str]:
    try:
        done = subprocess.run([real_git, *global_options, "symbolic-ref", "--short", "-q",
                               "HEAD"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def push_refusal(role: str, refs: Sequence[str]) -> Optional[str]:
    """Why ``role`` may not push these pre-push lines
    (``<local ref> <local sha> <remote ref> <remote sha>``), or None."""
    role = (role or "").strip().lower()
    if not role or role == "coordinator":
        return None
    targets = [line.split()[2] for line in refs if len(line.split()) >= 4]
    if not targets:
        return None
    if role in ("reviewer", "researcher"):
        return f"the {role} does not push; leave the change to the author or a developer"
    for ref in targets:
        if ref.removeprefix("refs/heads/") in PROTECTED:
            return (f"the {role} does not push to {'/'.join(PROTECTED)}; push a branch and "
                    "open a pull request")
    return None


def _repository_hook(name: str) -> Optional[str]:
    """The hook the repository itself would run (its own core.hooksPath, else
    ``.git/hooks``), ignoring the guard's override."""
    env = dict(os.environ, GIT_CONFIG_COUNT="0")
    git = os.environ.get("SWARMLY_REAL_GIT") or "git"

    def ask(*args: str) -> str:
        try:
            done = subprocess.run([git, *args], capture_output=True, text=True, timeout=10,
                                  env=env)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout.strip()
    folder = ask("config", "core.hooksPath")
    if folder and not os.path.isabs(folder):
        folder = os.path.join(ask("rev-parse", "--show-toplevel") or ".", folder)
    if not folder:
        common = ask("rev-parse", "--git-common-dir")
        folder = os.path.join(common, "hooks") if common else ""
    hook = os.path.join(folder, name) if folder else ""
    return hook if hook and os.path.isfile(hook) else None


def _pre_push(args: Sequence[str]) -> int:
    lines = sys.stdin.read().splitlines()
    why = push_refusal(os.environ.get("SWARMLY_ROLE", ""), lines)
    if why:
        print(f"swarmly guard: refused the push: {why}.", file=sys.stderr)
        return REFUSED
    hook = _repository_hook("pre-push")
    if hook is None:
        return 0
    shell = shutil.which("sh") or "sh"
    return subprocess.run([shell, hook, *args], input="\n".join(lines) + "\n",
                          text=True).returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "pre-push":
        return _pre_push(argv[1:])
    if not argv or argv[0] not in TOOLS:
        print("usage: guard.py <gh|git> <args...> | guard.py pre-push <remote> <url>",
              file=sys.stderr)
        return 2
    tool, args = argv[0], argv[1:]
    real = os.environ.get(f"SWARMLY_REAL_{tool.upper()}") or ""
    if not real or not Path(real).exists():
        print(f"swarmly guard: the real {tool} was not found", file=sys.stderr)
        return 127
    git = os.environ.get("SWARMLY_REAL_GIT") or "git"
    branch = _current_branch(git, split_git(args)[0]) if tool == "git" else None
    why = refusal(os.environ.get("SWARMLY_ROLE", ""), tool, args, branch)
    if why:
        print(f"swarmly guard: refused `{tool} {' '.join(args)}`: {why}.", file=sys.stderr)
        return REFUSED
    return subprocess.call([real, *args])


#: Hooks that pass through to the repository's own when the guard sets core.hooksPath.
PASS_THROUGH_HOOKS = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
                      "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit",
                      "pre-rebase", "post-checkout", "post-merge", "post-rewrite",
                      "pre-auto-gc", "push-to-checkout")


def _pass_through(name: str) -> str:
    # The repository's own hooks folder, read with the guard's override switched off.
    return ("#!/bin/sh\n"
            'top="$(GIT_CONFIG_COUNT=0 git rev-parse --show-toplevel 2>/dev/null)"\n'
            'dir="$(GIT_CONFIG_COUNT=0 git config core.hooksPath 2>/dev/null)"\n'
            'if [ -n "$dir" ]; then case "$dir" in /*|?:*) ;; *) dir="$top/$dir" ;; esac\n'
            'else dir="$(GIT_CONFIG_COUNT=0 git rev-parse --git-common-dir)/hooks"; fi\n'
            f'[ -x "$dir/{name}" ] && exec "$dir/{name}" "$@"\n'
            "exit 0\n")


def prepare(folder: Path, role: str, env: Dict[str, str]) -> Dict[str, str]:
    """Write the gh wrapper and the git hooks into ``folder`` and return ``env`` with them in
    force, or ``env`` unchanged when they cannot be written."""
    path = env.get("PATH", os.environ.get("PATH", ""))
    real = {tool: shutil.which(tool, path=path) for tool in TOOLS}
    if not any(real.values()):
        return env
    run = f'"{sys.executable}" -I -S "{Path(__file__).resolve()}"'
    hooks = folder / "hooks"
    try:
        hooks.mkdir(parents=True, exist_ok=True)
        if real["gh"]:
            sh = folder / "gh"
            sh.write_text(f'#!/bin/sh\nexec {run} gh "$@"\n', encoding="utf-8")
            sh.chmod(0o755)
            (folder / "gh.cmd").write_text(f"@{run} gh %*\r\n", encoding="utf-8")
        for stale in ("git", "git.cmd"):           # earlier builds wrapped git on PATH
            (folder / stale).unlink(missing_ok=True)
        pre_push = hooks / "pre-push"
        pre_push.write_text(f'#!/bin/sh\nexec {run} pre-push "$@"\n', encoding="utf-8")
        pre_push.chmod(0o755)
        for name in PASS_THROUGH_HOOKS:
            hook = hooks / name
            hook.write_text(_pass_through(name), encoding="utf-8")
            hook.chmod(0o755)
    except OSError:
        return env
    out = dict(env)
    if real["gh"]:
        out["PATH"] = os.pathsep.join([str(folder), path]) if path else str(folder)
    out["SWARMLY_ROLE"] = role
    for tool, found in real.items():
        if found:
            out[f"SWARMLY_REAL_{tool.upper()}"] = found
    try:
        count = int(out.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        count = 0
    out[f"GIT_CONFIG_KEY_{count}"] = "core.hooksPath"
    out[f"GIT_CONFIG_VALUE_{count}"] = str(hooks)
    out["GIT_CONFIG_COUNT"] = str(count + 1)
    return out


if __name__ == "__main__":
    sys.exit(main())
