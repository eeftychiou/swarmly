"""GitHub notifications backstop watcher.

Polls GitHub notifications for configured projects, deduplicates via state file,
skips items already mentioned/handled in the bound topic, and queues turns for missed pings.

It also carries the CI wake (issue #24): a pull request an agent is waiting on has its check
runs polled here, and when they reach a terminal state the waiting agent gets one turn with
the result — once per head commit, across restarts.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from swarmly.github.client import GitHubClient, GitHubError, Notification, PRChecks
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.config import AgentConfig, ProjectSettings
from swarmly.telegram.projects import ProjectRouter
from swarmly.telegram.redact import Redactor

if TYPE_CHECKING:
    from swarmly.telegram.channel import AgentChannel

logger = logging.getLogger("swarmly.github.watch")

#: Reasons addressed to the agent itself, or on its own issue or pull request (author: new
#: review comments on its PR). Only these fire again on new activity; a thread the agent
#: merely commented on or follows (comment, subscribed, ci_activity, ...) fires once, so two
#: agents commenting on one issue cannot wake each other forever.
REFIRE_REASONS = frozenset({"mention", "team_mention", "review_requested", "assign",
                            "author"})

#: A turn that failed leaves its notification unread, so the next poll picks it up again --
#: without turning one broken turn into a hot loop. Between attempts the thread waits
#: ``poll_minutes * 2**(attempt - 1)`` (one poll interval, then two, then four), and after
#: this many failures it is given up **loudly**: an error in the poll outcome, the
#: notification marked read so it stops absorbing the repo's poll, and a log line. Bounded
#: retries were chosen over an unbounded retry because a permanently failing turn (a broken
#: checkout, an expired token) would otherwise never let the poll move past that thread.
MAX_TURN_ATTEMPTS = 3


def _utc(now: Optional[float] = None) -> str:
    moment = (
        datetime.datetime.now(datetime.timezone.utc)
        if now is None
        else datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


#: A task-list line in a tracking issue body: ``- [ ] #65`` / ``- [x] #68``.
_CHECKBOX_LINE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.*)$", re.M)
_ISSUE_REF = re.compile(r"#(\d+)")


def tracking_item_refs(body: str, own: int) -> List[int]:
    """The issues and PRs a tracking issue lists as its items (issue #65).

    The checklist is the list when the body has one -- a ticked box is not proof, the state
    of the item it names decides -- and otherwise every ``#N`` in the body counts. The
    tracking issue's own number never does.
    """
    text = body or ""
    boxes = _CHECKBOX_LINE.findall(text)
    chunks = boxes if boxes else [text]
    refs: List[int] = []
    for chunk in chunks:
        for match in _ISSUE_REF.finditer(chunk):
            number = int(match.group(1))
            if number != int(own) and number not in refs:
                refs.append(number)
    return refs


def item_blocker(state: Optional[str], *, is_pr: bool = False) -> Optional[str]:
    """Why an item's state blocks a round end, or None when it does not.

    Done is ``CLOSED`` for an issue but ``MERGED`` for a pull request: a PR that was closed
    without being merged is work that never landed, and it reads as ``CLOSED`` too -- which
    is why the kind has to be known here (review of PR #76). A state GitHub could not tell us
    is *not done*: refusing a report is recoverable, reporting a half-finished round is not.
    """
    word = (state or "").upper()
    if not word:
        return "unchecked"
    if word == "MERGED":
        return None
    if word == "CLOSED":
        return "closed, unmerged" if is_pr else None
    return "open" if word == "OPEN" else word.lower()


@dataclass
class NotificationState:
    """Tracks processed notification IDs and last poll timestamp."""

    processed_ids: Dict[str, str] = field(default_factory=dict)  # id -> timestamp
    last_poll: Optional[str] = None
    #: notification id -> {"attempts": n, "retry_after": ISO-8601 UTC}: a turn that failed.
    #: The notification stays unread and is retried after ``retry_after``, or given up once
    #: ``attempts`` reaches ``MAX_TURN_ATTEMPTS``.
    failures: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: repo -> the issue numbers last seen OPEN carrying ``github.tracking_label``. A number
    #: that leaves this set as CLOSED is a round that just ended (issue #25): closing is not
    #: a notification, so the poller has to remember what was open to notice it went away.
    tracking_open: Dict[str, List[int]] = field(default_factory=dict)
    #: "repo#number" -> the title of a tracking issue whose closure has not been reported
    #: yet. A failed report turn leaves the entry queued for a later poll instead of losing
    #: the round end.
    pending_reports: Dict[str, str] = field(default_factory=dict)
    #: "repo#number" -> when the round report was posted for that closure. An issue that
    #: stays closed wakes the coordinator once, not once per poll; reopening drops the entry
    #: so a second closure is a second round end.
    round_reports: Dict[str, str] = field(default_factory=dict)
    #: "repo#number" -> the item list of a tracking issue that was closed while some of its
    #: items were still open or unmerged (issue #65). Such a close is **not** a round end:
    #: the report is refused and the coordinator is told what is still open, once.
    blocked_closes: Dict[str, str] = field(default_factory=dict)
    #: "repo#number" -> when a refused close was reported. Stops a still-closed issue from
    #: re-announcing its refusal on every poll; a reopen clears it.
    blocked_reported: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path]) -> "NotificationState":
        if not path or not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cls()
            raw_processed = data.get("processed_ids") or data.get("processed") or {}
            processed: Dict[str, str] = {}
            if isinstance(raw_processed, dict):
                processed = {str(k): str(v) for k, v in raw_processed.items()}
            elif isinstance(raw_processed, list):
                processed = {str(k): _utc() for k in raw_processed}
            failures: Dict[str, Dict[str, Any]] = {}
            raw_failures = data.get("failures")
            if isinstance(raw_failures, dict):
                for key, value in raw_failures.items():
                    if isinstance(value, dict):
                        try:
                            failures[str(key)] = {"attempts": int(value.get("attempts") or 0),
                                                  "retry_after": str(value.get("retry_after") or "")}
                        except (TypeError, ValueError):
                            continue
            tracking_open: Dict[str, List[int]] = {}
            raw_tracking = data.get("tracking_open")
            if isinstance(raw_tracking, dict):
                for repo, numbers in raw_tracking.items():
                    if not isinstance(numbers, list):
                        continue
                    kept: List[int] = []
                    for number in numbers:
                        try:
                            kept.append(int(number))
                        except (TypeError, ValueError):
                            continue
                    tracking_open[str(repo)] = kept
            round_reports: Dict[str, str] = {}
            raw_reports = data.get("round_reports")
            if isinstance(raw_reports, dict):
                round_reports = {str(k): str(v) for k, v in raw_reports.items()}
            pending_reports: Dict[str, str] = {}
            raw_pending = data.get("pending_reports")
            if isinstance(raw_pending, dict):
                pending_reports = {str(k): str(v) for k, v in raw_pending.items()}
            blocked_closes: Dict[str, str] = {}
            raw_blocked = data.get("blocked_closes")
            if isinstance(raw_blocked, dict):
                blocked_closes = {str(k): str(v) for k, v in raw_blocked.items()}
            blocked_reported: Dict[str, str] = {}
            raw_reported = data.get("blocked_reported")
            if isinstance(raw_reported, dict):
                blocked_reported = {str(k): str(v) for k, v in raw_reported.items()}
            return cls(processed_ids=processed, last_poll=data.get("last_poll"),
                       failures=failures, tracking_open=tracking_open,
                       round_reports=round_reports, pending_reports=pending_reports,
                       blocked_closes=blocked_closes, blocked_reported=blocked_reported)
        except Exception as exc:
            logger.warning("could not load notification state from %s: %s", path, exc)
            return cls()

    def save(self, path: Optional[Path]) -> None:
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "processed_ids": self.processed_ids,
                "last_poll": self.last_poll or _utc(),
                "failures": self.failures,
                "tracking_open": self.tracking_open,
                "round_reports": self.round_reports,
                "pending_reports": self.pending_reports,
                "blocked_closes": self.blocked_closes,
                "blocked_reported": self.blocked_reported,
            }
            tmp = path.with_suffix(f".tmp-{os.getpid()}-{int(time.time())}")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("could not save notification state to %s: %s", path, exc)

    def is_processed(self, notif_id: str, updated_at: str = "", refire: bool = True) -> bool:
        """Handled, and not updated since. GitHub keeps one notification thread per issue or
        pull request and bumps its ``updated_at`` on every new event, so a new mention on
        an already-handled thread fires again. Both values are ISO-8601 UTC strings."""
        handled = self.processed_ids.get(str(notif_id))
        if handled is None:
            return False
        return not refire or not updated_at or handled >= updated_at

    def mark_processed(self, notif_id: str, ts: Optional[str] = None) -> None:
        """Record *ts* (the notification's own ``updated_at``, GitHub's clock) so a skewed
        local clock cannot swallow the next event; the processing time without one."""
        self.processed_ids.pop(str(notif_id), None)     # re-insert: pruning drops the oldest
        self.processed_ids[str(notif_id)] = ts or _utc()
        self.failures.pop(str(notif_id), None)          # a handled thread owes no retry
        # Prune if too large
        if len(self.processed_ids) > 2000:
            keys = list(self.processed_ids.keys())[:-1000]
            for k in keys:
                self.processed_ids.pop(k, None)

    def record_failure(self, notif_id: str, poll_minutes: int = 5) -> int:
        """Count a failed turn and set when the thread may be retried; return the count."""
        previous = self.failures.get(str(notif_id)) or {}
        try:
            attempts = int(previous.get("attempts") or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
        # One poll interval, then twice, then four times: a broken runtime costs one turn
        # per window instead of one turn per poll, without hiding the notification forever.
        minutes = max(1, int(poll_minutes or 1)) * (2 ** max(0, attempts - 1))
        self.failures[str(notif_id)] = {"attempts": attempts,
                                        "retry_after": _utc(time.time() + minutes * 60)}
        return attempts

    def attempts(self, notif_id: str) -> int:
        entry = self.failures.get(str(notif_id)) or {}
        try:
            return int(entry.get("attempts") or 0)
        except (TypeError, ValueError):
            return 0

    def backing_off(self, notif_id: str) -> bool:
        """Whether the failed thread is still waiting out its backoff window."""
        entry = self.failures.get(str(notif_id))
        if not entry:
            return False
        retry_after = str(entry.get("retry_after") or "")
        return bool(retry_after) and retry_after > _utc()

    def clear_failure(self, notif_id: str) -> None:
        self.failures.pop(str(notif_id), None)


@dataclass
class GitHubPollOutcome:
    polled_repos: List[str] = field(default_factory=list)
    notifications_seen: int = 0
    turns_queued: int = 0
    turns_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    delivered_topics: List[Tuple[int, Optional[int]]] = field(default_factory=list)
    #: Issue #24: PRs whose checks were polled, and the wake turns run for the ones that
    #: reached a terminal state (``ci_wakes``) or had already been reported (``ci_skipped``).
    ci_watched: int = 0
    ci_wakes: int = 0
    ci_skipped: int = 0


#: How long a PR is watched for its checks before the watch is dropped. A PR whose checks
#: never report (no CI configured, a wedged runner) stops costing polls after this.
CI_WATCH_TTL_SECONDS = 12 * 3600.0
#: Bounds on the persisted numbers: the watches in flight and the reported results kept to
#: stop a duplicate wake.
MAX_CI_WATCHES = 20
MAX_CI_REPORTED = 500


def _ci_key(repo: str, number: int) -> str:
    return f"{repo}#{int(number)}"


@dataclass
class CIWatchState:
    """The PRs this agent waits on for CI, and which results it has already been woken for.

    Persisted so a restart neither forgets a watch nor wakes the agent twice for the same
    run: ``reported`` maps a PR to ``<head sha>:<state>``, and a wake is only run when that
    differs from what the API says now (a new push changes the sha; a re-run the state).
    """

    watches: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # "repo#n" -> watch
    reported: Dict[str, str] = field(default_factory=dict)             # "repo#n" -> "sha:state"

    @classmethod
    def load(cls, path: Optional[Path]) -> "CIWatchState":
        if not path or not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cls()
            raw_watches = data.get("watches")
            watches: Dict[str, Dict[str, Any]] = {}
            if isinstance(raw_watches, dict):
                for key, value in raw_watches.items():
                    if isinstance(value, dict) and value.get("repo") and value.get("number"):
                        watches[str(key)] = dict(value)
            raw_reported = data.get("reported")
            reported = ({str(k): str(v) for k, v in raw_reported.items()}
                        if isinstance(raw_reported, dict) else {})
            return cls(watches=watches, reported=reported)
        except Exception as exc:
            logger.warning("could not load CI watch state from %s: %s", path, exc)
            return cls()

    def save(self, path: Optional[Path]) -> None:
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {"watches": self.watches, "reported": self.reported}
            tmp = path.with_suffix(f".tmp-{os.getpid()}-{int(time.time())}")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("could not save CI watch state to %s: %s", path, exc)

    def watch(self, repo: str, number: int, chat_id: int, thread_id: Optional[int],
              project: Optional[str] = None, now: Optional[float] = None) -> str:
        """Start watching a PR's checks. Watching twice is watching once (a re-registered
        watch keeps its original clock, so the TTL cannot be refreshed forever)."""
        key = _ci_key(repo, number)
        if key not in self.watches:
            if len(self.watches) >= MAX_CI_WATCHES:
                self.watches.pop(next(iter(self.watches)))
            self.watches[key] = {"repo": repo, "number": int(number), "chat_id": int(chat_id),
                                 "thread_id": thread_id, "project": project,
                                 "created_at": float(now if now is not None else time.time())}
        return key

    def drop(self, key: str) -> None:
        self.watches.pop(key, None)

    def merge_from(self, path: Optional[Path], *, keep_out: Any = ()) -> None:
        """Fold in what another writer added to ``path`` since this snapshot was loaded.

        The channel registers a watch whenever a reply names a PR, and a reply can run inside
        this poll's wake turn: that write lands on disk while a stale ``self`` is in memory.
        Saving it back unchanged would erase the new watch, so every save first reads the
        file and keeps the watches and results only it knows about (issue #24 review).
        ``keep_out`` names keys this poll dropped — they must not come back.
        """
        if not path:
            return
        on_disk = CIWatchState.load(path)
        for key, watch in on_disk.watches.items():
            if key not in keep_out and key not in self.watches:
                self.watches[key] = watch
        for key, revision in on_disk.reported.items():
            if key not in keep_out:
                self.reported.setdefault(key, revision)

    def reported_result(self, key: str) -> Optional[str]:
        return self.reported.get(key)

    def mark_reported(self, key: str, revision: str) -> None:
        self.reported.pop(key, None)              # re-insert: pruning drops the oldest
        self.reported[key] = revision
        while len(self.reported) > MAX_CI_REPORTED:
            self.reported.pop(next(iter(self.reported)))


def ci_state_file_for(config: AgentConfig) -> Optional[Path]:
    """Path to gh_ci-<alias>.json in the transport state dir."""
    try:
        from swarmly.telegram.poll import state_dir
        alias = re.sub(r"[^a-z0-9_.-]+", "_", config.identity.alias.lower()).strip("._") or "agent"
        return state_dir(config) / f"gh_ci-{alias}.json"
    except Exception:
        return None


def register_ci_watch(config: AgentConfig, repo: str, number: int, chat_id: int,
                      thread_id: Optional[int], project: Optional[str] = None,
                      state_path: Optional[Path] = None) -> bool:
    """Record that this agent waits on ``repo#number``'s checks (issue #24).

    Called when the agent reports on a pull request: the next polls ask GitHub for its checks
    and wake the agent once when they finish. False when the watcher is off (no ``github:``
    section or ``poll_minutes: 0``), in which case nothing is written.
    """
    if not config.github or config.github.poll_minutes <= 0 or not repo or not number:
        return False
    path = state_path if state_path is not None else ci_state_file_for(config)
    state = CIWatchState.load(path)
    key = state.watch(repo, number, chat_id, thread_id, project=project)
    state.save(path)
    logger.info("watching %s for CI: %s wakes when its checks finish", key,
                config.identity.alias)
    return True


def state_file_for(config: AgentConfig) -> Optional[Path]:
    """Path to gh_notifications-<alias>.json in transport state dir."""
    try:
        from swarmly.telegram.poll import state_dir
        alias = re.sub(r"[^a-z0-9_.-]+", "_", config.identity.alias.lower()).strip("._") or "agent"
        return state_dir(config) / f"gh_notifications-{alias}.json"
    except Exception:
        return None


class GitHubWatcher:
    """Background watcher for GitHub notifications directed at an agent."""

    def __init__(
        self,
        config: AgentConfig,
        client: Optional[GitHubClient] = None,
        state_path: Optional[Path] = None,
        channel: Optional[AgentChannel] = None,
    ) -> None:
        self.config = config
        self.client = client or GitHubClient(
            token_env=config.github.token_env if config.github else "GH_TOKEN",
            alias=config.identity.alias,
            # Issue #22: comments this watcher posts are redacted like the bus payloads.
            redact=Redactor.from_config(config).text,
        )
        self.state_path = state_path if state_path is not None else state_file_for(config)
        # Issue #24: the CI watches live beside the notification state (same directory, own
        # file), derived from the state path a caller gave so a test's temp dir holds both.
        self.ci_state_path = (
            state_path.with_name(f"{state_path.stem}-ci{state_path.suffix}")
            if state_path is not None else ci_state_file_for(config)
        )
        self.channel = channel
        self.state = NotificationState.load(self.state_path)
        self.ci_state = CIWatchState.load(self.ci_state_path)
        self._router: Optional[ProjectRouter] = None

    def _is_coordinator(self) -> bool:
        """Only the coordinator posts the round report; a developer's watcher stays quiet."""
        return (self.config.identity.role or "").strip().lower() == "coordinator"

    async def _deliver(self, project: Optional[ProjectSettings], incoming: IncomingMessage,
                       chat_id: int, thread_id: Optional[int],
                       router: Optional[ProjectRouter]) -> Any:
        """Run one GitHub-driven turn in the project's topic and post its answer."""
        if self.channel is None:                 # the CLI path: no topic to post into
            return await self._standalone_reply(project, incoming, thread_id, router)
        adapter = (
            self.channel._adapter_for(chat_id, thread_id)
            if hasattr(self.channel, "_adapter_for")
            else self.channel.adapter
        )
        admitted = getattr(self.channel, "admitted", None)
        if admitted is not None:
            # The topic's turn lock and a parallel-turn slot, like a chat turn:
            # never two turns at once in one checkout.
            async with admitted(chat_id, thread_id):
                reply = await adapter.respond(incoming)
        else:
            reply = await adapter.respond(incoming)
        if reply and reply.text:
            await self.channel.send(reply.text, chat_id=chat_id, thread_id=thread_id)
        return reply

    async def _standalone_reply(self, project: Optional[ProjectSettings],
                                incoming: IncomingMessage, thread_id: Optional[int],
                                router: Optional[ProjectRouter]) -> Any:
        """The same turn with no channel (the CLI path): an adapter for the checkout."""
        from swarmly.telegram.channel import build_adapter
        # A CI wake may name no project (the topic is not bound to one): the adapter's own
        # checkout is then the workdir, as it is for any turn outside a project topic.
        workdir = (router.workdir_for(project, thread_id or 0)
                   if project is not None and router is not None else self.config.adapter.cwd)
        adapter_cfg = dataclasses.replace(self.config.adapter, cwd=str(workdir))
        cfg = dataclasses.replace(self.config, adapter=adapter_cfg)
        adapter = build_adapter(cfg, transport=False)
        try:
            return await adapter.respond(incoming)
        finally:
            await adapter.close()

    def _open_items(self, repo: str, issue: Any) -> List[str]:
        """The items of a tracking issue that still block its close (#65), as ``#12 open``.

        Every item the body lists has its own state read: a pull request has to be *merged*
        (a closed one never landed), an issue *closed*. Reading it is a `gh` call per item,
        which is why this runs only when a tracking issue actually closes.
        """
        blockers: List[str] = []
        for number in tracking_item_refs(getattr(issue, "body", "") or "",
                                         int(getattr(issue, "number", 0) or 0)):
            state: Optional[str] = None
            is_pr = False
            try:
                pr = self.client.pr_view(repo, number)
                if pr is not None:
                    is_pr = True
                    state = str(getattr(pr, "state", "") or "")
                else:
                    item = self.client.issue_view(repo, number)
                    state = str(getattr(item, "state", "") or "") if item is not None else ""
            except GitHubError as exc:
                logger.warning("could not read item #%s of %s: %s", number, repo, exc)
                state = ""
            reason = item_blocker(state, is_pr=is_pr)
            if reason:
                blockers.append(f"#{number} {reason}")
        return blockers

    async def _wake_on_tracking_close(self, project: ProjectSettings, chat_id: int,
                                      thread_id: Optional[int], outcome: GitHubPollOutcome,
                                      router: ProjectRouter) -> None:
        """Wake the coordinator to post the round report when a tracking issue closes.

        GitHub sends no notification for a closure, so the poller remembers which labelled
        issues were open and reports a number that is now closed. One report per closure:
        an issue that stays closed is quiet on the next poll, a reopened-and-closed one is a
        new round end. A failed turn backs off and is given up loudly, like a notification.
        """
        github = self.config.github
        if not self._is_coordinator() or github is None:
            return
        label = (github.tracking_label or "").strip()
        if not label:
            return
        repo = project.repo
        try:
            issues = self.client.issues_with_label(repo, label)
        except GitHubError as exc:
            # Not "no round ended": a broken gh would silently swallow every round report.
            outcome.errors.append(f"{repo}: {exc}")
            return

        open_now = sorted({int(i.number) for i in issues if str(i.state).upper() != "CLOSED"})
        closed_now = {int(i.number): i for i in issues
                      if str(i.state).upper() == "CLOSED"}
        previously_open = set(self.state.tracking_open.get(repo) or [])
        for number in open_now:
            # Reopened: the round runs again, so its next closure earns its own report.
            report_key = f"{repo}#{number}"
            self.state.round_reports.pop(report_key, None)
            self.state.pending_reports.pop(report_key, None)
            self.state.blocked_closes.pop(report_key, None)
            self.state.blocked_reported.pop(report_key, None)
        self.state.tracking_open[repo] = open_now

        # A closure is queued once and stays queued until a report is actually posted, so a
        # failed turn is retried on a later poll instead of losing the round end.
        for number in sorted(previously_open & set(closed_now)):
            report_key = f"{repo}#{number}"
            if report_key in self.state.round_reports:
                continue
            issue = closed_now[number]
            blockers = self._open_items(repo, issue)
            if blockers:
                # Issue #65: a tracking issue closed with work still open is not a round
                # end. Nothing is reported; the coordinator is told what is missing, once.
                self.state.blocked_closes[report_key] = ", ".join(blockers)
                self.state.pending_reports.setdefault(report_key, issue.title or "(untitled)")
                logger.warning("tracking issue %s closed with items still open: %s",
                               report_key, ", ".join(blockers))
                continue
            self.state.blocked_closes.pop(report_key, None)
            self.state.blocked_reported.pop(report_key, None)
            self.state.pending_reports.setdefault(report_key, issue.title or "(untitled)")

        for report_key in sorted(self.state.pending_reports):
            repo_key, _, number_text = report_key.rpartition("#")
            if repo_key != repo:
                continue
            number = int(number_text)
            failure_key = f"tracking:{report_key}"
            if report_key in self.state.round_reports:
                self.state.pending_reports.pop(report_key, None)
                continue
            if self.state.backing_off(failure_key):
                logger.debug("round report for %s is backing off after a failed turn",
                             report_key)
                outcome.turns_skipped += 1
                continue
            title = self.state.pending_reports[report_key] or "(untitled)"
            still_open = self.state.blocked_closes.get(report_key)
            if still_open:
                turn_text = (
                    f"GitHub tracking issue {repo}#{number} \"{title}\" was CLOSED, but the "
                    f"round is not over: {still_open} -- so there is no round report to post "
                    f"yet. Reopen the issue and keep it open until every item is merged (the "
                    f"report fires on the close that has none left), or say here why the "
                    f"issue should stay closed."
                )
            else:
                turn_text = (
                    f"GitHub tracking issue closed: {repo}#{number} \"{title}\" is CLOSED, so "
                    f"the round is over: post the round report now -- what landed (PRs and "
                    f"merge commits), what is still open, and what the human decides next."
                )
            try:
                from swarmly.telegram.topics import context_block_reason, context_for
                blocked = context_block_reason(
                    chat_id, thread_id, self.config.identity.home_channel_id)
                if blocked:
                    self.state.pending_reports.pop(report_key, None)
                    self.state.round_reports[report_key] = _utc()
                    outcome.errors.append(f"round report for {report_key}: refused: {blocked}")
                    logger.error("round report for %s refused: %s", report_key, blocked)
                    continue
                ctx = context_for(chat_id, thread_id, self.config.identity.home_channel_id)
                incoming = IncomingMessage(
                    text=turn_text,
                    author_alias="GitHub",
                    context_id=ctx,
                    project=project.key,
                    project_repo=repo,
                )
                if self.channel is not None:
                    reply = await self._deliver(project, incoming, chat_id, thread_id, router)
                else:
                    reply = await self._standalone_reply(project, incoming, thread_id, router)
            except Exception as exc:
                outcome.errors.append(f"round report for {report_key}: {exc}")
                logger.exception("failed to post the round report for %s: %s", report_key, exc)
                continue

            if not getattr(reply, "ok", True):
                failed = str(getattr(reply, "error", "") or "the turn did not complete")
                attempts = self.state.record_failure(
                    failure_key, poll_minutes=github.poll_minutes)
                if attempts >= MAX_TURN_ATTEMPTS:
                    # Given up loudly, and recorded so one broken round does not retry forever.
                    self.state.pending_reports.pop(report_key, None)
                    self.state.round_reports[report_key] = _utc()
                    outcome.errors.append(
                        f"round report for {report_key}: gave up after {attempts} failed "
                        f"turns: {failed}")
                    logger.error("gave up on the round report for %s after %s failed turns: %s",
                                 report_key, attempts, failed)
                else:
                    outcome.errors.append(
                        f"round report for {report_key}: turn failed ({failed}); retry "
                        f"{attempts}/{MAX_TURN_ATTEMPTS}")
                    logger.warning("round report for %s: turn failed (%s); retry %s/%s",
                                   report_key, failed, attempts, MAX_TURN_ATTEMPTS)
                continue

            self.state.clear_failure(failure_key)
            self.state.pending_reports.pop(report_key, None)
            if still_open:
                # Announced once; the issue stays closed and quiet until it is reopened.
                self.state.blocked_reported[report_key] = _utc()
                self.state.blocked_closes.pop(report_key, None)
            else:
                self.state.round_reports[report_key] = _utc()
            outcome.turns_queued += 1
            outcome.delivered_topics.append((chat_id, thread_id))
            logger.info("round report queued for %s in %s/%s", report_key, chat_id, thread_id)
        if len(self.state.round_reports) > 500:
            for key in list(self.state.round_reports)[:-250]:
                self.state.round_reports.pop(key, None)

    async def poll_once(self) -> GitHubPollOutcome:
        """Execute one polling pass across all configured projects."""
        outcome = GitHubPollOutcome()
        if not self.config.github or self.config.github.poll_minutes == 0:
            logger.debug("github watcher disabled or poll_minutes=0")
            return outcome
        if not self.config.projects:
            logger.debug("no projects configured; github watcher has no repos to poll")
            return outcome

        login = (self.config.github.login or "").strip().lower()
        router = (
            self.channel.router
            if self.channel is not None and isinstance(getattr(self.channel, "router", None), ProjectRouter)
            else ProjectRouter(list(self.config.projects.values()), self.config.identity.home_channel_id)
        )

        for pkey, project in self.config.projects.items():
            repo = project.repo
            outcome.polled_repos.append(repo)
            try:
                notifs = self.client.notifications(repo=repo)
            except GitHubError as exc:
                # Not "nothing to do": a logged-out or missing gh would otherwise hide every
                # missed ping behind a quiet, successful-looking poll.
                outcome.errors.append(f"{repo}: {exc}")
                continue
            outcome.notifications_seen += len(notifs)

            # Determine topic for this project
            topics = router.topics_of(pkey)
            if topics:
                chat_id, thread_id = topics[0]
            else:
                chat_id = self.config.identity.home_channel_id or 0
                thread_id = None

            # A round ends when its tracking issue closes (issue #25). Closing sends no
            # notification, so the poller watches the labelled open issues and wakes the
            # coordinator in the project's topic when one goes away.
            await self._wake_on_tracking_close(project, chat_id, thread_id, outcome, router)

            for notif in notifs:
                if self.state.is_processed(notif.id, notif.updated_at,
                                           refire=notif.reason in REFIRE_REASONS):
                    continue
                if self.state.backing_off(notif.id):
                    # A failed turn waits out its window: retried later, not every poll.
                    logger.debug("notification %s is backing off after a failed turn", notif.id)
                    outcome.turns_skipped += 1
                    continue

                num = notif.number
                # Check whether any of the project's topics recently handled this issue/PR
                if self.channel is not None and num is not None and hasattr(self.channel, "has_recent_ref"):
                    seen_in = topics or [(chat_id, thread_id)]
                    if any(self.channel.has_recent_ref(c, t, repo, num) for c, t in seen_in):
                        logger.info(
                            "skipping notification %s for %s#%s: already handled in recent topic turns",
                            notif.id, repo, num,
                        )
                        self.state.mark_processed(notif.id, notif.updated_at)
                        if self.config.github.mark_read:
                            self.client.mark_notification_read(notif.id)
                        outcome.turns_skipped += 1
                        continue

                # Resolve user and context snippet
                user = "a contributor"
                snippet = notif.subject_title
                if notif.subject_type == "PullRequest" and num is not None:
                    pr = self.client.pr_view(repo, num)
                    if pr:
                        user = pr.author or "a contributor"
                        snippet = pr.title or snippet
                elif num is not None:
                    issue = self.client.issue_view(repo, num)
                    if issue:
                        if issue.comments:
                            user = issue.comments[-1].author or issue.author or "a contributor"
                            snippet = issue.comments[-1].body[:200]
                        else:
                            user = issue.author or "a contributor"
                            snippet = issue.body[:200] if issue.body else issue.title

                # Frame turn message
                if notif.reason == "review_requested":
                    turn_text = f"GitHub review requested on {repo}#{num} by {user}"
                elif notif.reason == "assign":
                    turn_text = f"GitHub assignment: {repo}#{num} by {user}"
                elif notif.reason in ("mention", "team_mention"):
                    turn_text = f"GitHub notification: {user} mentioned you on {repo}#{num}: {snippet}"
                else:
                    turn_text = f"GitHub notification ({notif.reason}): {user} on {repo}#{num}: {snippet}"

                try:
                    ctx_ok = self._run_turn_precheck(chat_id, thread_id, notif, outcome)
                    if not ctx_ok:
                        continue
                    reply = await self._run_turn(project, chat_id, thread_id, turn_text)
                    if not getattr(reply, "ok", True):
                        # The runtime failed the turn (2.12): the notification stays unread so
                        # the next poll retries it -- not marked read, not marked processed --
                        # and it backs off instead of retrying every poll. After
                        # MAX_TURN_ATTEMPTS it is given up loudly.
                        failed = str(getattr(reply, "error", "") or "the turn did not complete")
                        attempts = self.state.record_failure(
                            notif.id, poll_minutes=self.config.github.poll_minutes)
                        if attempts >= MAX_TURN_ATTEMPTS:
                            self.state.mark_processed(notif.id, notif.updated_at)
                            if self.config.github.mark_read:
                                self.client.mark_notification_read(notif.id)
                            outcome.errors.append(
                                f"notification {notif.id}: gave up after {attempts} failed "
                                f"turns: {failed}")
                            logger.error("gave up on notification %s after %s failed turns: %s",
                                         notif.id, attempts, failed)
                        else:
                            outcome.errors.append(
                                f"notification {notif.id}: turn failed ({failed}); left unread, "
                                f"retry {attempts}/{MAX_TURN_ATTEMPTS}")
                            logger.warning("notification %s: turn failed (%s); retry %s/%s",
                                           notif.id, failed, attempts, MAX_TURN_ATTEMPTS)
                        continue

                    self.state.mark_processed(notif.id, notif.updated_at)
                    if self.config.github.mark_read:
                        self.client.mark_notification_read(notif.id)
                    outcome.turns_queued += 1
                    outcome.delivered_topics.append((chat_id, thread_id))
                except Exception as exc:
                    outcome.errors.append(f"notification {notif.id}: {exc}")
                    logger.exception("failed to process notification %s: %s", notif.id, exc)

        # Issue #24: the same poll carries the CI wakes for the PRs this agent waits on.
        await self._poll_ci_watches(outcome, router)

        self.state.last_poll = _utc()
        self.state.save(self.state_path)
        return outcome

    # ----------------------------------------------------------------------------------
    # Turns, and the CI wake that uses them (issue #24)
    # ----------------------------------------------------------------------------------
    def _refused_topic(self, chat_id: int, thread_id: Optional[int]) -> Optional[str]:
        """Why a turn in this topic has no safe context id, or ``None`` when it has one.

        Both turn sources -- the notification backstop and the CI wake -- ask this, so
        neither can route a bound topic's work into the shared General session.
        """
        from swarmly.telegram.topics import context_block_reason
        return context_block_reason(chat_id, thread_id, self.config.identity.home_channel_id)

    def _run_turn_precheck(self, chat_id: int, thread_id: Optional[int], notif: Notification,
                           outcome: GitHubPollOutcome) -> bool:
        """Refuse a turn that has no context id, loudly and not forever (issue #22 review):
        it would fall into the shared General session and mix another group's work into it."""
        blocked = self._refused_topic(chat_id, thread_id)
        if not blocked:
            return True
        self.state.mark_processed(notif.id, notif.updated_at)
        if self.config.github.mark_read:
            self.client.mark_notification_read(notif.id)
        outcome.errors.append(f"notification {notif.id}: refused: {blocked}")
        logger.error("notification %s refused: %s", notif.id, blocked)
        return False

    async def _run_turn(self, project: Optional[Any], chat_id: int, thread_id: Optional[int],
                        text: str) -> Any:
        """Run one adapter turn and post its reply where the work lives.

        The turn is admitted exactly like a chat turn (the topic's lock and a parallel-turn
        slot), so neither the notification backstop nor a CI wake can put two turns into one
        checkout at once.
        """
        from swarmly.telegram.topics import context_for
        ctx = context_for(chat_id, thread_id, self.config.identity.home_channel_id)
        incoming = IncomingMessage(
            text=text,
            author_alias="GitHub",
            context_id=ctx,
            project=getattr(project, "key", None) if project is not None else None,
            project_repo=getattr(project, "repo", None) if project is not None else None,
        )
        # One delivery path for every GitHub-driven turn (#24, #25): _deliver holds the topic's
        # turn lock and posts the reply, and the CLI path has no topic to post into.
        return await self._deliver(project, incoming, chat_id, thread_id, self._router)

    async def _poll_ci_watches(self, outcome: GitHubPollOutcome,
                               router: Optional[ProjectRouter] = None) -> None:
        """Wake each waiting agent once when its PR's checks reach a terminal state (#24).

        A watch is dropped when it fires, when its PR is gone, or after ``CI_WATCH_TTL_SECONDS``
        — so a PR with no CI stops costing polls instead of waking a bot forever. The result
        it fired for is recorded (``<sha>:<state>``) before the turn runs: a restart in the
        middle of a wake repeats it at most once more, and never for a run already reported.
        """
        self._router = router
        # A channel reply registers watches by writing the file directly (another writer,
        # and possibly another process): fold in anything it added since this snapshot.
        self.ci_state.merge_from(self.ci_state_path)
        if not self.ci_state.watches:
            return
        now = time.time()
        changed = False
        dropped: set = set()
        for key, watch in list(self.ci_state.watches.items()):
            if now - float(watch.get("created_at") or 0) > CI_WATCH_TTL_SECONDS:
                logger.info("stopped watching %s for CI: no result within %d h",
                            key, CI_WATCH_TTL_SECONDS // 3600)
                self.ci_state.drop(key)
                dropped.add(key)
                changed = True
                continue
            try:
                checks = self.client.pr_checks(str(watch["repo"]), int(watch["number"]))
            except Exception as exc:                     # a poll must never die on one PR
                outcome.errors.append(f"CI watch {key}: {exc}")
                logger.warning("CI watch %s could not be polled: %s", key, exc)
                continue
            if checks is None:
                # Not a PR (an issue number the agent named), deleted, or gh cannot say: the
                # watch is not worth keeping.
                logger.info("stopped watching %s for CI: gh has no checks for it", key)
                self.ci_state.drop(key)
                dropped.add(key)
                changed = True
                continue
            outcome.ci_watched += 1
            watch["head_sha"] = checks.head_sha
            if not checks.is_open:
                # The PR was merged or closed after the agent named it (a merge notice, an
                # upgrade call): its checks are done and nothing waits on them, so this is no
                # wake — the agent would be told to "review and QA" work that already landed.
                logger.info("stopped watching %s for CI: the PR is %s", key,
                            checks.pr_state.lower() or "not open")
                self.ci_state.drop(key)
                dropped.add(key)
                outcome.ci_skipped += 1
                changed = True
                continue
            if not checks.terminal:
                continue
            revision = f"{checks.head_sha}:{checks.state}"
            if self.ci_state.reported_result(key) == revision:
                # Already woken for this exact run (a restart, or a re-registered watch).
                self.ci_state.drop(key)
                dropped.add(key)
                outcome.ci_skipped += 1
                changed = True
                continue
            self.ci_state.mark_reported(key, revision)
            self.ci_state.drop(key)
            dropped.add(key)
            self._save_ci_state(dropped)                 # before the turn: no double wake
            changed = True
            await self._wake_on_ci(watch, checks, outcome, router)
        if changed:
            self._save_ci_state(dropped)

    def _save_ci_state(self, keep_out: Any = ()) -> None:
        """Persist the watches, keeping what another writer added during a wake turn.

        The turn a wake runs can post a reply that names another PR, and registering that
        watch writes this same file; a plain save of the snapshot this poll loaded would
        erase it. So the file is re-read and merged in before every write (issue #24 review).
        """
        self.ci_state.merge_from(self.ci_state_path, keep_out=keep_out)
        self.ci_state.save(self.ci_state_path)

    async def _wake_on_ci(self, watch: Dict[str, Any], checks: PRChecks,
                          outcome: GitHubPollOutcome,
                          router: Optional[ProjectRouter] = None) -> None:
        """The one turn the waiting agent gets when its PR's checks finish."""
        chat_id = int(watch.get("chat_id") or self.config.identity.home_channel_id or 0)
        thread_id = watch.get("thread_id")
        # The same guard the notification turns use (issue #24 review): a topic with no
        # context id is refused rather than run into the shared General session. The watch
        # is already spent by now, so a refused wake is reported once and not retried.
        blocked = self._refused_topic(chat_id, thread_id)
        if blocked:
            outcome.errors.append(
                f"CI wake for {watch['repo']}#{checks.number}: refused: {blocked}")
            logger.error("CI wake for %s#%s refused: %s", watch["repo"], checks.number, blocked)
            return
        text = (f"CI finished on {watch['repo']}#{checks.number}"
                f"{f' ({checks.title[:80]})' if checks.title else ''}: {checks.summary()}.")
        if checks.state == "failure":
            text += (" Fix the failing checks — or say on the PR why they cannot be fixed — "
                     "then push again.")
        else:
            text += " The PR is ready for review and QA."
        if checks.url:
            text += f"\n{checks.url}"
        project = None
        if watch.get("project"):
            project = self.config.projects.get(str(watch["project"]))
        if project is None and router is not None:
            project = router.project_for(chat_id, thread_id)
        try:
            reply = await self._run_turn(project, chat_id, thread_id, text)
        except Exception as exc:
            outcome.errors.append(f"CI wake for {watch['repo']}#{checks.number}: {exc}")
            logger.exception("CI wake for %s#%s failed: %s", watch["repo"], checks.number, exc)
            return
        if not getattr(reply, "ok", True):
            outcome.errors.append(
                f"CI wake for {watch['repo']}#{checks.number}: the turn did not complete "
                f"({getattr(reply, 'error', '') or 'no reason given'})")
            return
        outcome.ci_wakes += 1
        outcome.delivered_topics.append((chat_id, thread_id))
        logger.info("woke %s for CI on %s#%s: %s", self.config.identity.alias,
                    watch["repo"], checks.number, checks.state)


async def github_poll_once(
    config: AgentConfig,
    channel: Optional[AgentChannel] = None,
    client: Optional[GitHubClient] = None,
    state_path: Optional[Path] = None,
) -> GitHubPollOutcome:
    """Shared entry point for both in-transport background polling and the CLI command."""
    watcher = GitHubWatcher(config, client=client, state_path=state_path, channel=channel)
    return await watcher.poll_once()
