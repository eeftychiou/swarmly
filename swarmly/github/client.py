"""Typed wrapper around the GitHub CLI (gh) for durable coordination."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("swarmly.github.client")


class GitHubError(Exception):
    """A gh call that could not answer (missing CLI, not logged in, rate limit, bad JSON).
    Raised where an empty answer would read as "nothing to do"."""


@dataclass
class IssueComment:
    author: str
    body: str
    created_at: str


@dataclass
class Issue:
    number: int
    title: str
    body: str
    author: str
    state: str
    labels: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    comments: List[IssueComment] = field(default_factory=list)


@dataclass
class PR:
    number: int
    title: str
    body: str
    author: str
    state: str
    labels: List[str] = field(default_factory=list)
    reviewers: List[str] = field(default_factory=list)
    head_ref: str = ""
    mergeable: str = "UNKNOWN"


@dataclass
class Notification:
    id: str
    reason: str
    repo: str
    subject_title: str
    subject_type: str
    subject_url: str
    number: Optional[int]
    updated_at: str
    unread: bool = True


@dataclass
class PRChecks:
    """The check runs of one pull request's current head commit (issue #24).

    ``state`` is ``pending`` while any check is still running, and ``success`` or ``failure``
    once all of them are done — the terminal states an agent waiting on CI is woken for.
    """

    number: int
    head_sha: str
    state: str                              # "pending" | "success" | "failure"
    url: str = ""
    title: str = ""
    total: int = 0
    passed: int = 0
    failed: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    #: The pull request's own state: ``OPEN``, ``MERGED`` or ``CLOSED``, empty when gh did not
    #: say. A merged or closed PR is not waiting on anything, so its checks are not a wake.
    pr_state: str = ""

    @property
    def is_open(self) -> bool:
        """Whether the PR still waits on these checks. Unknown counts as open: waking on a
        guess is worse than one wake that the next poll corrects."""
        return self.pr_state.upper() in ("", "OPEN")

    @property
    def terminal(self) -> bool:
        return self.state != "pending"

    def summary(self) -> str:
        """One line for the wake turn: what finished, and what it said."""
        if self.total == 0:
            return "no check runs reported"
        if self.state == "success":
            return f"all {self.total} check(s) passed"
        if self.state == "failure":
            names = ", ".join(self.failed) if self.failed else "unrecorded checks"
            return f"{self.passed} of {self.total} check(s) passed; failing: {names}"
        running = ", ".join(self.pending) if self.pending else "checks"
        return f"{len(self.pending)} of {self.total} check(s) still running ({running})"


#: Conclusions that mean a check did not pass. ``NEUTRAL`` and ``SKIPPED`` do not.
FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
                                "STARTUP_FAILURE", "STALE"})
#: Check statuses/states that mean "not finished yet".
PENDING_STATES = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING",
                            "EXPECTED"})


def checks_from_rollup(number: int, head_sha: str, url: str, title: str, rollup: Any,
                       pr_state: str = "") -> PRChecks:
    """Fold ``gh pr view --json statusCheckRollup`` into one :class:`PRChecks`.

    The rollup mixes ``CheckRun`` entries (``status``/``conclusion``) with ``StatusContext``
    entries (``state``); both are read here so a PR watched through either API answers the
    same. An entry the parser cannot read counts as pending: waking on a guess is worse than
    waiting for the next poll, and the watch's own lifetime bounds the wait. An empty rollup
    is pending too — a PR pushed a moment ago has no checks until GitHub materialises them,
    and a wake saying "no checks" would arrive before the run it is waiting for exists.
    ``pr_state`` is the pull request's own state (``OPEN``/``MERGED``/``CLOSED``): a merged
    PR's checks are not something to wake anyone for.
    """
    checks = PRChecks(number=number, head_sha=head_sha, state="pending", url=url, title=title,
                      pr_state=pr_state)
    for entry in rollup if isinstance(rollup, list) else []:
        if not isinstance(entry, dict):
            checks.pending.append("?")
            continue
        name = str(entry.get("name") or entry.get("context") or entry.get("__typename") or "?")
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        state = str(entry.get("state") or "").upper()
        checks.total += 1
        if conclusion in FAILED_CONCLUSIONS or state in {"FAILURE", "ERROR"}:
            checks.failed.append(name)
        elif status in PENDING_STATES or state in PENDING_STATES or \
                (not conclusion and not state) or (status and status != "COMPLETED"):
            checks.pending.append(name)
        else:
            checks.passed += 1
    if checks.pending or checks.total == 0:
        # No checks at all is not "all good": a PR pushed a moment ago has none until GitHub
        # materialises them, so the run stays pending and the next poll asks again.
        checks.state = "pending"
    elif checks.failed:
        checks.state = "failure"
    else:
        checks.state = "success"
    return checks


class GitHubClient:
    """Thin wrapper around ``gh`` CLI with environment injection and safe fallbacks."""

    def __init__(self, token_env: str = "GH_TOKEN", alias: Optional[str] = None,
                 redact: Optional[Callable[[str], str]] = None) -> None:
        self.token_env = token_env
        self.alias = alias
        # Issue #22: everything this client writes to GitHub goes through the redactor, so a
        # comment cannot carry the maintainer's handle, a chat id or an absolute path by
        # accident. Without one the text is passed through unchanged.
        self.redact = redact or (lambda text: text)

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        token = os.environ.get(self.token_env)
        if token:
            env["GH_TOKEN"] = token
        return env

    def _run(self, args: List[str], timeout: float = 30.0) -> Tuple[int, str, str]:
        """Execute a ``gh`` command safely.

        Returns (returncode, stdout, stderr). Never raises on missing executable or non-zero exit.
        """
        gh_bin = shutil.which("gh")
        if not gh_bin:
            logger.warning("gh CLI not found on PATH; operations will fail cleanly")
            return 127, "", "gh CLI not found on PATH"
        try:
            res = subprocess.run(
                [gh_bin, *args],
                env=self._env(),
                capture_output=True,
                # gh writes UTF-8. Without this, Windows decodes with the ANSI code page, and an
                # issue body with a character outside it (live: byte 0x8f) kills the reader
                # thread: stdout comes back None and /cleanup never answered.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            return res.returncode, res.stdout or "", res.stderr or ""
        except subprocess.TimeoutExpired:
            logger.warning("gh command timed out after %ss: gh %s", timeout, " ".join(args[:4]))
            return 124, "", "command timed out"
        except Exception as exc:
            logger.warning("gh execution failed: %s", exc)
            return 1, "", str(exc)

    def ref_state(self, repo: str, number: int) -> Optional[str]:
        """``open``, ``closed`` or ``merged`` for an issue *or* a PR, in one call; None if unknown.

        GitHub's issues endpoint answers for both, and a PR carries ``pull_request.merged_at``.
        /cleanup asked ``pr view`` first and fell back to ``issue view``: two calls for every
        issue, one after another, and a dry run took most of a minute (live, 2026-10-22).
        """
        code, out, err = self._run(
            ["api", f"repos/{repo}/issues/{int(number)}",
             "--jq", '[.state, (.pull_request.merged_at // "")] | @tsv'], timeout=20.0)
        if code != 0:
            logger.warning("ref_state(%s, %s) failed (code %d): %s", repo, number, code,
                           err.strip())
            return None
        state, _, merged = out.strip().partition("\t")
        state = state.strip().lower()
        if merged.strip():
            return "merged"
        return state if state in ("open", "closed") else None

    def issue_view(self, repo: str, number: int) -> Optional[Issue]:
        """Get details of an issue."""
        fields = "number,title,body,author,state,labels,assignees,comments"
        code, out, err = self._run(["issue", "view", str(number), "--repo", repo, "--json", fields])
        if code != 0:
            logger.warning("issue_view(%s, %s) failed (code %d): %s", repo, number, code, err.strip())
            return None
        try:
            data = json.loads(out)
            if not isinstance(data, dict):
                return None
            author_val = data.get("author")
            author = author_val.get("login", "") if isinstance(author_val, dict) else str(author_val or "")
            labels = [
                lb.get("name", "") if isinstance(lb, dict) else str(lb)
                for lb in (data.get("labels") or [])
            ]
            assignees = [
                a.get("login", "") if isinstance(a, dict) else str(a)
                for a in (data.get("assignees") or [])
            ]
            raw_comments = data.get("comments") or []
            comments: List[IssueComment] = []
            for c in raw_comments:
                if isinstance(c, dict):
                    c_auth = c.get("author")
                    c_login = c_auth.get("login", "") if isinstance(c_auth, dict) else str(c_auth or "")
                    comments.append(
                        IssueComment(
                            author=c_login,
                            body=str(c.get("body") or ""),
                            created_at=str(c.get("createdAt") or c.get("created_at") or ""),
                        )
                    )
            return Issue(
                number=int(data.get("number") or number),
                title=str(data.get("title") or ""),
                body=str(data.get("body") or ""),
                author=author,
                state=str(data.get("state") or ""),
                labels=labels,
                assignees=assignees,
                comments=comments,
            )
        except Exception as exc:
            logger.warning("failed to parse issue_view json: %s", exc)
            return None

    def pr_view(self, repo: str, number: int) -> Optional[PR]:
        """Get details of a pull request."""
        fields = "number,title,body,author,state,labels,reviewRequests,headRefName,mergeable"
        code, out, err = self._run(["pr", "view", str(number), "--repo", repo, "--json", fields])
        if code != 0:
            logger.warning("pr_view(%s, %s) failed (code %d): %s", repo, number, code, err.strip())
            return None
        try:
            data = json.loads(out)
            if not isinstance(data, dict):
                return None
            author_val = data.get("author")
            author = author_val.get("login", "") if isinstance(author_val, dict) else str(author_val or "")
            labels = [
                lb.get("name", "") if isinstance(lb, dict) else str(lb)
                for lb in (data.get("labels") or [])
            ]
            raw_reviewers = data.get("reviewRequests") or []
            reviewers: List[str] = []
            for r in raw_reviewers:
                if isinstance(r, dict):
                    r_login = r.get("login") or r.get("name") or ""
                    if r_login:
                        reviewers.append(str(r_login))
                elif isinstance(r, str):
                    reviewers.append(r)
            return PR(
                number=int(data.get("number") or number),
                title=str(data.get("title") or ""),
                body=str(data.get("body") or ""),
                author=author,
                state=str(data.get("state") or ""),
                labels=labels,
                reviewers=reviewers,
                head_ref=str(data.get("headRefName") or data.get("head_ref") or ""),
                mergeable=str(data.get("mergeable") or "UNKNOWN"),
            )
        except Exception as exc:
            logger.warning("failed to parse pr_view json: %s", exc)
            return None

    def pr_checks(self, repo: str, number: int) -> Optional[PRChecks]:
        """The check runs of a pull request's current head, or ``None`` when gh cannot say.

        One call (``gh pr view --json headRefOid,statusCheckRollup,state``) gives the head sha,
        every check on it, and whether the PR is still open, which is what an agent waiting on
        CI needs to be woken once: the sha says *which* push the result belongs to, so a wake
        is never repeated for it, and a merged or closed PR wakes nobody.
        """
        fields = "number,title,url,headRefOid,statusCheckRollup,state"
        code, out, err = self._run(["pr", "view", str(number), "--repo", repo, "--json", fields])
        if code != 0:
            logger.warning("pr_checks(%s, %s) failed (code %d): %s", repo, number, code, err.strip())
            return None
        try:
            data = json.loads(out)
            if not isinstance(data, dict):
                return None
            return checks_from_rollup(
                number=int(data.get("number") or number),
                head_sha=str(data.get("headRefOid") or ""),
                url=str(data.get("url") or ""),
                title=str(data.get("title") or ""),
                rollup=data.get("statusCheckRollup") or [],
                pr_state=str(data.get("state") or ""),
            )
        except Exception as exc:
            logger.warning("failed to parse pr_checks json: %s", exc)
            return None

    def open_pr_checks(self, repo: str, limit: int = 30) -> Optional[List[PRChecks]]:
        """The checks of every open pull request's head in *repo*, in one call, or ``None``
        when gh cannot say (``gh pr list --json ...statusCheckRollup``)."""
        fields = "number,title,url,headRefOid,statusCheckRollup"
        code, out, err = self._run(["pr", "list", "--repo", repo, "--state", "open",
                                    "--limit", str(int(limit)), "--json", fields])
        if code != 0:
            logger.warning("open_pr_checks(%s) failed (code %d): %s", repo, code, err.strip())
            return None
        try:
            data = json.loads(out)
        except ValueError as exc:
            logger.warning("failed to parse open_pr_checks json: %s", exc)
            return None
        found: List[PRChecks] = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict) or not entry.get("number"):
                continue
            found.append(checks_from_rollup(
                number=int(entry["number"]), head_sha=str(entry.get("headRefOid") or ""),
                url=str(entry.get("url") or ""), title=str(entry.get("title") or ""),
                rollup=entry.get("statusCheckRollup") or [], pr_state="OPEN"))
        return found

    def notifications(self, repo: Optional[str] = None) -> List[Notification]:
        """Fetch the token owner's unread notifications, optionally scoped to a repository.

        Raises :class:`GitHubError` when gh cannot answer: an empty list must mean "no
        notifications", never "gh is missing, logged out or rate-limited"."""
        endpoint = f"repos/{repo}/notifications" if repo else "notifications"
        code, out, err = self._run(["api", endpoint])
        if code != 0:
            logger.warning("notifications(%s) failed (code %d): %s", repo, code, err.strip())
            raise GitHubError(f"gh api {endpoint} failed (exit {code}): "
                              f"{(err.strip() or out.strip())[:300]}")
        try:
            data = json.loads(out)
            if not isinstance(data, list):
                raise GitHubError(f"gh api {endpoint}: expected a JSON list")
            result: List[Notification] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                nid = str(item.get("id") or "")
                reason = str(item.get("reason") or "")
                repo_dict = item.get("repository") or {}
                repo_name = repo_dict.get("full_name") or repo or ""
                subj = item.get("subject") or {}
                stitle = str(subj.get("title") or "")
                stype = str(subj.get("type") or "")
                surl = str(subj.get("url") or "")
                num: Optional[int] = None
                if surl:
                    last_seg = surl.rstrip("/").split("/")[-1]
                    if last_seg.isdigit():
                        num = int(last_seg)
                updated_at = str(item.get("updated_at") or "")
                unread = bool(item.get("unread", True))
                result.append(
                    Notification(
                        id=nid,
                        reason=reason,
                        repo=repo_name,
                        subject_title=stitle,
                        subject_type=stype,
                        subject_url=surl,
                        number=num,
                        updated_at=updated_at,
                        unread=unread,
                    )
                )
            return result
        except GitHubError:
            raise
        except Exception as exc:
            logger.warning("failed to parse notifications json: %s", exc)
            raise GitHubError(f"gh api {endpoint}: unreadable answer ({exc})") from exc

    def issues_with_label(self, repo: str, label: str, state: str = "all") -> List[Issue]:
        """List a repository's issues carrying *label*, in any state.

        Raises :class:`GitHubError` when gh cannot answer, so an empty list always means
        "no issue carries this label" and never "gh is missing or logged out" -- a closed
        tracking issue must not be missed because the poll could not run."""
        fields = "number,title,body,author,state,labels,assignees"
        code, out, err = self._run(
            ["issue", "list", "--repo", repo, "--label", label, "--state", state,
             "--json", fields, "--limit", "200"])
        if code != 0:
            logger.warning("issues_with_label(%s, %s) failed (code %d): %s",
                           repo, label, code, err.strip())
            raise GitHubError(f"gh issue list --repo {repo} --label {label} failed "
                              f"(exit {code}): {(err.strip() or out.strip())[:300]}")
        try:
            data = json.loads(out)
            if not isinstance(data, list):
                raise GitHubError(f"gh issue list --label {label}: expected a JSON list")
            result: List[Issue] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                author_val = item.get("author")
                author = (author_val.get("login", "") if isinstance(author_val, dict)
                          else str(author_val or ""))
                labels = [
                    lb.get("name", "") if isinstance(lb, dict) else str(lb)
                    for lb in (item.get("labels") or [])
                ]
                assignees = [
                    a.get("login", "") if isinstance(a, dict) else str(a)
                    for a in (item.get("assignees") or [])
                ]
                result.append(Issue(
                    number=int(item.get("number") or 0),
                    title=str(item.get("title") or ""),
                    body=str(item.get("body") or ""),
                    author=author,
                    state=str(item.get("state") or ""),
                    labels=labels,
                    assignees=assignees,
                ))
            return result
        except GitHubError:
            raise
        except Exception as exc:
            logger.warning("failed to parse issues json: %s", exc)
            raise GitHubError(f"gh issue list --label {label}: unreadable answer ({exc})") from exc

    def comment(self, repo: str, number: int, body: str) -> bool:
        """Post a comment signed with the agent's identity."""
        body = self.redact(body)
        signed_body = body
        if self.alias:
            signature = f"— {self.alias}"
            if signature not in body:
                signed_body = f"{body.rstrip()}\n\n{signature}"
        code, _out, err = self._run(["issue", "comment", str(number), "--repo", repo, "--body", signed_body])
        if code != 0:
            logger.warning("comment(%s, %s) failed (code %d): %s", repo, number, code, err.strip())
            return False
        return True

    def close_issue(self, repo: str, number: int, comment: str = "") -> bool:
        """Close an issue, with the reason as a comment first (#227 item 2).

        A close is never silent: the comment names what landed, and it goes in before the
        close so a failed close leaves the reason behind rather than a closed issue nobody
        can account for.
        """
        if comment and not self.comment(repo, number, comment):
            return False
        code, _out, err = self._run(["issue", "close", str(number), "--repo", repo])
        if code != 0:
            logger.warning("close_issue(%s, %s) failed (code %d): %s", repo, number, code,
                           err.strip())
            return False
        return True

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        assignees: Optional[List[str]] = None,
        labels: Optional[List[str]] = None,
    ) -> Optional[int]:
        """Create a new issue and return its number."""
        title = self.redact(title)
        body = self.redact(body)
        signed_body = body
        if self.alias:
            signature = f"— {self.alias}"
            if signature not in body:
                signed_body = f"{body.rstrip()}\n\n{signature}"
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", signed_body]
        for a in assignees or []:
            if a:
                args.extend(["--assignee", str(a)])
        for lb in labels or []:
            if lb:
                args.extend(["--label", str(lb)])
        code, out, err = self._run(args)
        if code != 0:
            logger.warning("create_issue(%s, %r) failed (code %d): %s", repo, title, code, err.strip())
            return None
        # gh issue create prints url like https://github.com/owner/repo/issues/123
        match = re.search(r"/issues/(\d+)", out)
        if match:
            return int(match.group(1))
        # fallback: find any digits
        digits = re.findall(r"\b(\d+)\b", out)
        if digits:
            return int(digits[-1])
        return None

    def mark_notification_read(self, thread_id: str) -> bool:
        """Mark a notification thread as read."""
        code, _out, err = self._run(["api", "-X", "PATCH", f"/notifications/threads/{thread_id}"])
        if code != 0:
            logger.warning("mark_notification_read(%s) failed (code %d): %s", thread_id, code, err.strip())
            return False
        return True
