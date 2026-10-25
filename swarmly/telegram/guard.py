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
its full path or unset the variables. What the *wrapper* says about who it speaks for is the
exception -- that is baked into it, not read from the calling environment (#232). This file
imports only the standard library, so the wrappers run it directly (``python -I -S
guard.py``) and stay fast.

    python guard.py gh <args...>
    python guard.py pre-push <remote> <url>   (refs on stdin, as git gives them)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: Branches only the coordinator updates directly (others push a branch and open a PR).
PROTECTED = ("main", "master")
#: Exit status of a refused command.
REFUSED = 3
TOOLS = ("gh", "git")

#: Who merged, and at which lease term, ride in the environment ``prepare`` writes (#228):
#: the bots share one GitHub login, so a merge shows the same ``mergedBy`` for all of them and
#: only the transport can say which bot did it.
ALIAS_ENV = "SWARMLY_ALIAS"
TERM_ENV = "SWARMLY_LEASE_TERM"

#: ``gh`` options that take a separate value, so the PR after ``gh pr merge`` is not confused
#: with a flag's argument. ``--match-head-commit`` was missing and its SHA read as the PR
#: number, so the attribution comment went to a pull request nobody merged (#232).
_GH_VALUE_OPTIONS = ("-b", "--body", "-t", "--subject", "-R", "--repo", "-A", "--author-email",
                     "--match-head-commit")

#: ``gh`` options that come **before** the subcommand and take a separate value.
_GH_GLOBAL_VALUE_OPTIONS = ("-R", "--repo", "--hostname", "--config-dir", "-c")

#: How long one ``gh`` call the guard makes on its own behalf may take.
_GH_TIMEOUT = 20

#: A ``gh`` runner: the argv to run, and its ``(exit code, output)``. Injected so a test
#: never spawns a process to prove what the guard posts.
Runner = Callable[[Sequence[str]], Tuple[int, str]]


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


def split_gh(args: Sequence[str]) -> "tuple[List[str], List[str]]":
    """``gh`` arguments as (global options, subcommand and its arguments).

    ``gh -R owner/repo pr merge 5`` is the same merge as ``gh pr merge 5``. Reading the
    subcommand off the first two words missed it, so a merge that named its repository first
    was neither refused for a role that may not merge nor followed by its comment (#232).
    """
    i, args = 0, list(args)
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GH_GLOBAL_VALUE_OPTIONS else 1
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
    elif tool == "gh":
        args = split_gh(args)[1]             # `gh -R owner/repo pr merge 5` is a merge (#232)
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


def _merge_pr(args: Sequence[str]) -> str:
    """The pull request ``gh pr merge <pr>`` names: its first argument that is not a flag or
    a flag's value. Empty when there is none (``gh pr merge`` can take the branch's PR)."""
    rest = list(args)[2:]                       # drop ``pr merge``
    i = 0
    while i < len(rest):
        word = rest[i]
        if word.startswith("-"):
            i += 2 if word in _GH_VALUE_OPTIONS else 1
            continue
        return word
    return ""


def attribution(alias: str, term: int, sha: str) -> str:
    """The one comment a merge leaves behind: who merged, at which lease term, at which
    commit (#228). A term or SHA the transport could not read is left out, never guessed."""
    who = alias.strip() or "an unnamed bot"
    lease = f" (lease term {int(term)})" if int(term) > 0 else ""
    at = f" at {sha.strip()}" if sha.strip() else ""
    return f"Merged by {who}{lease}{at}"


def merge_sha(real: str, pr: str, run: "Runner",
              global_options: Sequence[str] = ()) -> str:
    """The commit the merge landed at: its merge commit, else the head it merged.

    *global_options* is what came before the subcommand (``-R owner/repo``): dropping it made
    the lookup run against the cwd repository, so a merge of another repository's PR would
    read this one's commit (#245 review).
    """
    code, out = run([real, *global_options, "pr", "view", pr, "--json", "mergeCommit,headRefOid",
                     "--jq", ".mergeCommit.oid // .headRefOid"])
    return out.strip() if code == 0 else ""


def comment_merge(real: str, args: Sequence[str], env: Mapping[str, str],
                  run: "Runner", *, global_options: Sequence[str] = ()) -> Optional[str]:
    """Post the attribution comment after a merge this guard ran, or None when there is
    nobody to attribute it to (an unguarded run) or no PR to comment on.

    The transport does this, not the model: a merge by the admin by hand leaves no comment,
    so a missing comment means no bot merged through the guard. A failure here is logged and
    swallowed -- the merge already happened, and it stays merged.

    *args* is the merge's own arguments (after any global options); *global_options* is the
    prefix they came behind, and it is repeated on both `gh` calls the attribution makes, so
    `gh -R other/repo pr merge 5` comments on **that** repository's PR 5 (#245 review).
    """
    alias = str(env.get(ALIAS_ENV) or "").strip()
    pr = _merge_pr(args)
    if not alias or not pr:
        return None
    if any(word in ("--auto",) for word in args):
        # ``--auto`` only *queues* the merge: it has not landed, so claiming it did would be
        # the very "I merged it" that cannot be checked (#228).
        return None
    try:
        term = int(str(env.get(TERM_ENV) or "0").strip() or 0)
    except ValueError:
        term = 0
    body = attribution(alias, term, merge_sha(real, pr, run, global_options))
    code, out = run([real, *global_options, "pr", "comment", pr, "--body", body])
    if code != 0:
        print(f"swarmly guard: merged, but the attribution comment on #{pr} failed: "
              f"{out.strip()[:200]}", file=sys.stderr)
        return None
    return body


def _gh_run(argv: Sequence[str]) -> "tuple[int, str]":
    """Run one ``gh`` argv the guard keeps for itself; ``(exit code, output)``."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=_GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        return 1, str(err)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


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
    if not why:
        # The rulebook's push rules (admin, 24 Oct): no secrets, no force over another
        # agent's branch, and a coordinator pushes merges only.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from push_checks import check as push_check, default_git
            found = push_check(lines, os.environ.get("SWARMLY_ROLE", ""),
                               os.environ.get("GIT_AUTHOR_NAME", ""),
                               default_git(os.environ.get("SWARMLY_REAL_GIT") or "git"))
        except Exception:                              # noqa: BLE001 - a partial install
            found = None                               # must not block every push
            print("swarmly guard: the push checks could not run", file=sys.stderr)
        if found:
            kind, why = found
            _drift(os.environ.get("SWARMLY_DRIFT_FILE", ""), kind, why)
    if why:
        print(f"swarmly guard: refused the push: {why}.", file=sys.stderr)
        return REFUSED
    hook = _repository_hook("pre-push")
    if hook is None:
        return 0
    shell = shutil.which("sh") or "sh"
    return subprocess.run([shell, hook, *args], input="\n".join(lines) + "\n",
                          text=True).returncode


def _drift(ledger: str, kind: str, evidence: str) -> None:
    """One violation into the transport's drift ledger (drift.py), from this process."""
    if not ledger:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from drift import append_inbox
        append_inbox(ledger, kind, evidence)
    except Exception:                                  # noqa: BLE001 - never block on it
        pass


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
    if not why and tool == "gh":
        # The merge gate (admin, 24 Oct): review merge + QA pass + green CI on the head.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import merge_gate
        except Exception:                              # noqa: BLE001 - a partial install
            merge_gate = None                          # must not break every gh call
            print("swarmly guard: the merge gate could not load", file=sys.stderr)
        if merge_gate is not None:
            why = merge_gate.check(real, args, dict(os.environ))
            ledger = os.environ.get("SWARMLY_DRIFT_FILE", "")
            if why:
                _drift(ledger, "merge_gate_refused", f"gh {' '.join(args)}: {why}")
            elif merge_gate.bypassed(args, os.environ):
                _drift(ledger, "merge_gate_bypassed", f"gh {' '.join(args)}")
    if why:
        print(f"swarmly guard: refused `{tool} {' '.join(args)}`: {why}.", file=sys.stderr)
        return REFUSED
    code = subprocess.call([real, *args])
    # `gh -R owner/repo pr merge 5` merges just as `gh pr merge 5` does, so the comment goes
    # by the subcommand found after the global options -- and repeats that prefix on its own
    # calls, or the SHA and the comment would name this checkout's repository instead (#232).
    prefix, tail = split_gh(args) if tool == "gh" else ([], args)
    if code == 0 and tool == "gh" and [w.lower() for w in _words(tail)][:2] == ["pr", "merge"]:
        # A merge this guard ran leaves one comment saying who merged it, at which term and
        # commit (#228). After the merge, and never in place of its exit code.
        try:
            comment_merge(real, tail, os.environ, _gh_run, global_options=prefix)
        except Exception:                          # noqa: BLE001 - the merge stands
            print("swarmly guard: the attribution comment failed", file=sys.stderr)
    return code


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


def _sh_quote(value: str) -> str:
    """*value* as one POSIX shell word, for the wrapper that bakes the alias in (#232)."""
    text = str(value or "")
    if text and all(ch.isalnum() or ch in "-_@.:/," for ch in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def _baked_term(folder: Path) -> int:
    """The lease term already baked into this folder's wrapper, or 0 when there is none.

    #245 review: ``prepare()`` is called again for reasons that have nothing to do with the
    lease -- a ``/model`` change rewrites the environment, and the adapter it builds may not
    know the term yet. Rewriting the wrapper with ``SWARMLY_LEASE_TERM=0`` then took the term
    away from a merge already in flight, and its attribution comment lost the term it had
    earned. The term is only ever raised here, never lowered back to 0 by a caller who does not
    know it.
    """
    for name in ("gh", "gh.cmd"):
        try:
            text = (folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(rf"{re.escape(TERM_ENV)}=(\d+)", text)
        if match:
            return int(match.group(1))
    return 0


def _gh_wrapper(run: str, alias: str, term: int) -> str:
    """The POSIX ``gh`` wrapper, with **who it speaks for baked in** (#232).

    The alias and the term ride the environment otherwise, and the environment is the
    caller's: ``SWARMLY_ALIAS=someone-else gh pr merge 5`` used to post a comment naming
    another bot. Baked into the wrapper they cannot be changed from outside it, so the
    comment always names the bot whose turn ran the merge.
    """
    return (f"#!/bin/sh\nexec env {ALIAS_ENV}={_sh_quote(alias)} {TERM_ENV}={int(term or 0)} "
            f"{run} gh \"$@\"\n")


def _cmd_quote(value: str) -> str:
    """*value* as the right-hand side of a **quoted** ``set`` assignment (#245 review).

    The quotes belong around ``NAME=value`` -- ``set "VAR=x"`` is the form cmd strips, while
    ``set VAR="x"`` stores the quotes and the comment would read ``Merged by "DeepSeek"``.
    ``%`` is doubled because a batch file expands it before ``set`` ever runs.
    """
    return str(value or "").replace("%", "%%")


def _gh_wrapper_cmd(run: str, alias: str, term: int) -> str:
    """The ``.cmd`` twin of :func:`_gh_wrapper`, for a Windows turn (#232)."""
    return (f'@set "{ALIAS_ENV}={_cmd_quote(alias)}"\r\n'
            f'@set "{TERM_ENV}={int(term or 0)}"\r\n'
            f"@{run} gh %*\r\n")


def prepare(folder: Path, role: str, env: Dict[str, str], *, alias: str = "",
            term: int = 0, drift: str = "") -> Dict[str, str]:
    """Write the gh wrapper and the git hooks into ``folder`` and return ``env`` with them in
    force, or ``env`` unchanged when they cannot be written.

    ``alias`` and ``term`` say who this agent is and at which lease term it runs, so a merge
    the guard runs can post its attribution comment (#228)."""
    path = env.get("PATH", os.environ.get("PATH", ""))
    # #245 review: a caller that does not know the term (a /model rewrite, a restart before
    # the lease is read) must not erase the one the wrapper already carries.
    term = int(term or 0) or _baked_term(folder)
    real = {tool: shutil.which(tool, path=path) for tool in TOOLS}
    if not any(real.values()):
        return env
    run = f'"{sys.executable}" -I -S "{Path(__file__).resolve()}"'
    hooks = folder / "hooks"
    try:
        hooks.mkdir(parents=True, exist_ok=True)
        if real["gh"]:
            sh = folder / "gh"
            sh.write_text(_gh_wrapper(run, alias, term), encoding="utf-8")
            sh.chmod(0o755)
            (folder / "gh.cmd").write_text(_gh_wrapper_cmd(run, alias, term),
                                           encoding="utf-8")
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
    if drift:
        out["SWARMLY_DRIFT_FILE"] = drift
    # #228: who the guard speaks for, so a merge it runs can say so on the PR. Written even
    # when empty: an empty alias is what keeps an unguarded merge from being attributed.
    # #232: and written into the wrapper too (`_gh_wrapper`), because this half is only the
    # caller's environment and a caller can overwrite it.
    out[ALIAS_ENV] = alias
    out[TERM_ENV] = str(int(term or 0))
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
