"""The merge gate the role guard runs before ``gh pr merge`` (admin, 24 Oct).

The rulebook says a PR merges only when a reviewer's **merge** verdict, a QA **pass** and green
CI are all on its current head. That is checkable, so it is checked: the guard reads the PR's
head, its checks and its comments, and refuses the merge with the list of what is missing.
The coordinator still decides to merge; the gate only stops a merge the rulebook rules out.

Verdicts are comments (the bots share one GitHub login, so formal reviews are refused): a
verdict counts only in a comment that **names the head** (7+ characters of its sha) and is
**signed** with a ``— <alias>`` line, as the rulebook asks. A verdict signed by one of the PR's
own commit authors does not count, so a developer cannot pass its own PR. The latest counted
comment wins, so a fix-first after a merge verdict on the same head blocks again.

The gate checks words and signatures, not identity: the shared login means anyone can type
any signature. It stops a merge the rulebook rules out by mistake or drift; it is not a
security boundary.

A ``gh pr merge`` the gate cannot place (no number, a branch, a URL) is looked up with
``gh pr view``; if that fails too, the merge is refused and the bot is asked to name the PR.

Stdlib only: ``guard.py`` runs it with ``python -I -S``. ``SWARMLY_MERGE_GATE=off`` turns it
off for one command; the guard records each such bypass in the drift ledger.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence

_REVIEW_MERGE = re.compile(
    r"(?i)\b(?:verdict|review)\b\W{0,6}(?:\*\*)?\s*merge\b(?!\s+(?:it\s+)?(?:once|when|after))")
_REVIEW_FIX = re.compile(r"(?i)\b(?:verdict|review)\b\W{0,6}(?:\*\*)?\s*fix[\s-]?first\b")
_QA_PASS = re.compile(r"(?i)\bQA\b\W{0,6}(?:\*\*)?\s*pass(?:es|ed)?\b")
_QA_FAIL = re.compile(r"(?i)\bQA\b\W{0,6}(?:\*\*)?\s*fail(?:s|ed)?\b")

_DONE_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}

#: The signature line a verdict ends with: ``— Reasonix``, ``-- Codex (reviewer)``.
_SIGNATURE = re.compile(r"(?m)^\s*(?:—|–|--)\s*([A-Za-z][\w.-]*)")

#: gh pr merge's options that take a separate value, so their value is not read as the PR.
_VALUE_OPTIONS = ("--match-head-commit", "-t", "--subject", "-b", "--body", "-F",
                  "--body-file", "-A", "--author-email")


def signer(body: str) -> Optional[str]:
    """The alias a comment is signed with (its last signature line), or None."""
    found = _SIGNATURE.findall(body or "")
    return found[-1] if found else None


def _authors(view: Dict[str, Any]) -> set:
    names = set()
    for commit in view.get("commits") or []:
        for author in (commit or {}).get("authors") or []:
            name = str((author or {}).get("name") or "").strip().lower()
            if name:
                names.add(name)
    return names


def _names_head(body: str, head: str) -> bool:
    head = (head or "").lower()
    if len(head) < 7:
        return False
    text = body.lower()
    return any(head[:n] in text for n in (40, 12, 10, 8, 7))


def _ci(rollup: Any) -> str:
    """``green``, ``pending`` or ``red`` for a ``statusCheckRollup``."""
    entries = rollup if isinstance(rollup, list) else []
    if not entries:
        return "pending"
    state = "green"
    for entry in entries:
        if not isinstance(entry, dict):
            return "pending"
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        ctx_state = str(entry.get("state") or "").upper()
        if conclusion and conclusion not in _DONE_OK or ctx_state in {"FAILURE", "ERROR"}:
            return "red"
        if (status and status != "COMPLETED") or (not conclusion and ctx_state not in
                                                  ("SUCCESS",)):
            state = "pending"
    return state


def evaluate(view: Dict[str, Any]) -> List[str]:
    """What the PR still lacks on its head; empty when the gate is met."""
    head = str(view.get("headRefOid") or "")
    missing: List[str] = []
    ci = _ci(view.get("statusCheckRollup"))
    if ci != "green":
        missing.append(f"green CI (it is {ci})")
    authors = _authors(view)
    review = qa = None
    unsigned = own = False
    for comment in view.get("comments") or []:
        body = str((comment or {}).get("body") or "")
        if not _names_head(body, head):
            continue
        if not any(rx.search(body) for rx in (_REVIEW_FIX, _REVIEW_MERGE, _QA_FAIL, _QA_PASS)):
            continue
        who = signer(body)
        if who is None:
            unsigned = True
            continue
        if who.lower() in authors:
            own = True
            continue
        if _REVIEW_FIX.search(body):
            review = "fix-first"
        elif _REVIEW_MERGE.search(body):
            review = "merge"
        if _QA_FAIL.search(body):
            qa = "fail"
        elif _QA_PASS.search(body):
            qa = "pass"
    if review != "merge":
        missing.append("a reviewer's merge verdict" + (" (the latest says fix-first)"
                                                        if review == "fix-first" else ""))
    if qa != "pass":
        missing.append("a QA pass" + (" (the latest says fail)" if qa == "fail" else ""))
    if missing and unsigned:
        missing.append("(an unsigned verdict on this head was not counted: sign it `— <alias>`)")
    if missing and own:
        missing.append("(a verdict signed by one of the PR's own authors was not counted)")
    return missing


def pr_target(args: Sequence[str]) -> Optional[Dict[str, Any]]:
    """``gh [opts] pr merge [<n>|<url>|<branch>] [opts]`` -> ``{"number", "repo_args"}``, or
    None when *args* is not a merge. ``number`` is the PR as written (a number, a URL or a
    branch), or None when the merge names none (gh then merges the current branch's PR)."""
    args = list(args)
    repo_args: List[str] = []
    words: List[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word in ("-R", "--repo") and i + 1 < len(args):
            repo_args = [word, args[i + 1]]
            i += 2
            continue
        if word in _VALUE_OPTIONS and i + 1 < len(args):
            i += 2                                         # an option's value, not the PR
            continue
        if word.startswith("--repo="):
            repo_args = [word]
        elif not word.startswith("-"):
            words.append(word)
        i += 1
    if [w.lower() for w in words[:2]] != ["pr", "merge"]:
        return None
    number = words[2] if len(words) > 2 else None
    return {"number": number, "repo_args": repo_args}


def _off(env: Dict[str, str]) -> bool:
    return str(env.get("SWARMLY_MERGE_GATE", "")).lower() in ("off", "0", "false", "no")


def bypassed(args: Sequence[str], env: Any) -> bool:
    """True when *args* is a merge the caller ran with the gate switched off."""
    return _off(dict(env)) and pr_target(args) is not None


def check(real_gh: str, args: Sequence[str], env: Dict[str, str]) -> Optional[str]:
    """Why the merge in *args* is refused, or None to let it run (also when the gate cannot
    read the PR: an unreadable PR is gh's to report, not the gate's to invent)."""
    if _off(env):
        return None
    target = pr_target(args)
    if target is None:
        return None
    number = target["number"]
    try:
        done = subprocess.run(
            [real_gh, "pr", "view", *([number] if number else []), *target["repo_args"],
             "--json", "headRefOid,statusCheckRollup,comments,commits"],
            capture_output=True, text=True, timeout=60, env=env)
        view = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        view = None
    if not isinstance(view, dict):
        if number and number.isdigit():
            return None            # gh reports an unreadable PR itself when the merge runs
        return ("the merge gate cannot tell which pull request this merges; "
                "run `gh pr merge <number>`")
    missing = evaluate(view)
    if not missing:
        return None
    head = str(view.get("headRefOid") or "")[:7]
    return (f"the merge gate is not met on head {head}: missing " + "; ".join(missing)
            + ". A verdict counts only in a comment that names this head.")
