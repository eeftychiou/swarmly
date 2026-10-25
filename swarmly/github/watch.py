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

#: Issue #87: the notification reasons and subject types that are a **bot's own machinery**
#: talking -- a CI run, a workflow. They feed the CI watches (#24) and the round state (#86),
#: and never become a chat reply addressed to "@GitHub": the swarm asking itself, in public,
#: what its own runner just did.
CI_REASONS = frozenset({"ci_activity", "workflow_run"})
CI_SUBJECTS = frozenset({"CheckSuite", "WorkflowRun", "CheckRun"})

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

    This is the **broad** reading, for refusing a close: `#189` mentions eight issues in prose
    and every one of them may be checked. A close nobody asked for is recoverable ("reopen
    it"); :func:`listed_item_refs` is the narrow one that closing needs.
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


#: A line that *states* its slices rather than mentioning them: ``split into #65, #68``,
#: ``slices: #65 #68``, ``sub-issues - #65``. The phrase leads the line, so prose that merely
#: mentions a number ("#117 stayed open after all its slices (#133…) merged", from #189's own
#: body) is a mention, not a listing.
_LISTED_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:"
    r"split\s+(?:in)?to\s+|split\s+as\s+"                       # "split into #65, #68"
    r"|(?:slices?|sub-?issues?|children|parts)\s*(?:[:=\-—]\s*|of\s+)"   # "slices: #65"
    r")", re.I)


def listed_item_refs(body: str, own: int) -> Tuple[List[int], bool]:
    """``(refs, listed)`` — the slices a parent names, and whether it names them at all.

    #227 item 2 closes a parent that is **finished**, so it may only act on a body that
    states its slices: a task-list line, or a line that opens with what the list is
    (``split into #a, #b``, ``slices: #a``). A body that only mentions numbers in prose lists
    nothing — closing on that reading is how #189 would have closed with #228 and #226 still
    open, which is the failure #227 exists to prevent, inverted.
    """
    text = body or ""
    own = int(own)

    def refs_in(chunk: str) -> List[int]:
        found: List[int] = []
        for match in _ISSUE_REF.finditer(chunk):
            number = int(match.group(1))
            if number != own and number not in found:
                found.append(number)
        return found

    boxes = _CHECKBOX_LINE.findall(text)
    if boxes:
        refs: List[int] = []
        for chunk in boxes:
            refs.extend(number for number in refs_in(chunk) if number not in refs)
        return refs, bool(refs)
    refs = []
    for line in text.splitlines():
        if _LISTED_LINE.match(line):
            refs.extend(number for number in refs_in(line) if number not in refs)
    return refs, bool(refs)


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
    #: "repo#number" -> when the transport posted its "every listed slice has landed" line on
    #: a parent (#227 review). A close that keeps failing (a locked issue, a token that lost
    #: `issues: write`) must not re-post the same comment on every poll: the note goes once,
    #: the close is retried silently.
    parent_notes: Dict[str, str] = field(default_factory=dict)

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
            parent_notes: Dict[str, str] = {}
            raw_notes = data.get("parent_notes")
            if isinstance(raw_notes, dict):
                parent_notes = {str(k): str(v) for k, v in raw_notes.items()}
            return cls(processed_ids=processed, last_poll=data.get("last_poll"),
                       failures=failures, tracking_open=tracking_open,
                       round_reports=round_reports, pending_reports=pending_reports,
                       blocked_closes=blocked_closes, blocked_reported=blocked_reported,
                       parent_notes=parent_notes)
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
                "parent_notes": self.parent_notes,
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
    #: Issue #87: notifications answered without a turn -- older than ``github.backlog_hours``
    #: (``backlog_skipped``), or a bot's own CI/workflow activity (``quiet_skipped``).
    backlog_skipped: int = 0
    quiet_skipped: int = 0


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

    def _may_poll(self) -> bool:
        """Whether this watcher may read the shared GitHub login right now.

        `is_coordinator()` answers from a fold the channel caches for a few seconds, which is
        fine for a display but not for a gate: a bot displaced a moment ago can still look like
        the coordinator and poll beside its successor. The channel's fence re-reads the lease
        before every side effect, so the poll asks that instead (#93, Codex's blocker).
        """
        if self.channel is None:
            return True                     # standalone `gh-poll --once`: the only reader
        fence = getattr(self.channel, "lease_fence", None)
        if callable(fence):
            try:
                return bool(fence())
            except Exception:                          # noqa: BLE001 - no answer is not authority
                logger.warning("github: the lease fence could not answer; not polling",
                               exc_info=True)
                return False
        return self._is_coordinator()

    def _is_coordinator(self) -> bool:
        """Only the coordinator posts the round report or handles notifications (#83).

        The answer comes from the bus lease, through the channel's one gate, and from nowhere
        else: falling back to the configured role is how a returning coordinator keeps polling
        beside the acting one while both read the same shared GitHub login. No authority is a
        peer, and a peer stays quiet.
        """
        if self.channel is None or not hasattr(self.channel, "is_coordinator"):
            return False
        try:
            return bool(self.channel.is_coordinator())
        except Exception:                              # noqa: BLE001 - no answer is not authority
            logger.warning("github: the coordinator gate could not answer; staying quiet",
                           exc_info=True)
            return False

    async def _deliver(self, project: Optional[ProjectSettings], incoming: IncomingMessage,
                       chat_id: int, thread_id: Optional[int],
                       router: Optional[ProjectRouter]) -> Any:
        """Run one GitHub-driven turn in the project's topic and post its answer."""
        if self.channel is None:                 # the CLI path: no topic to post into
            return await self._standalone_reply(project, incoming, thread_id, router)
        if callable(getattr(type(self.channel), "run_transport_turn", None)):
            # The full turn path (#237): recap, notes, lock, and the reply's WAIT/ROLE lines.
            return await self.channel.run_transport_turn(incoming.text, chat_id, thread_id)
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

    def _item_states(self, repo: str, issue: Any,
                     refs: Optional[List[int]] = None) -> List[Tuple[int, Optional[str]]]:
        """``(number, why it blocks)`` for every item the issue's body lists (#65, #227).

        ``refs`` is the list to read; the default is the broad reading
        (:func:`tracking_item_refs`) the round-end refusal uses. The parent close passes the
        narrow one (:func:`listed_item_refs`), because closing acts on what a body *states*.

        One ``gh api`` call per item answers for an issue *or* a pull request at once, so the
        parent check costs one call per slice rather than the two a ``pr view`` that falls
        back to ``issue view`` costs — it runs once per poll for every open parent (#227).
        ``closed`` is the one ambiguous word (a closed issue is done, a closed pull request
        never landed), and only that word pays for a second call to learn which it is.
        """
        states: List[Tuple[int, Optional[str]]] = []
        if refs is None:
            refs = tracking_item_refs(getattr(issue, "body", "") or "",
                                      int(getattr(issue, "number", 0) or 0))
        for number in refs:
            try:
                state = str(self.client.ref_state(repo, number) or "")
                if state.lower() == "closed":
                    is_pr = self.client.pr_view(repo, number) is not None
                    states.append((number, item_blocker("CLOSED", is_pr=is_pr)))
                    continue
                states.append((number, item_blocker(state.upper())))
            except GitHubError as exc:
                logger.warning("could not read item #%s of %s: %s", number, repo, exc)
                states.append((number, "unchecked"))
        return states

    def _open_items(self, repo: str, issue: Any) -> List[str]:
        """The items of a tracking issue that still block its close (#65), as ``#12 open``."""
        return [f"#{number} {why}" for number, why in self._item_states(repo, issue) if why]

    def _close_finished_parent(self, repo: str, issue: Any,
                               outcome: GitHubPollOutcome) -> bool:
        """Close a parent issue whose every listed slice has landed (#227 item 2).

        A parent states its slices as a checklist or as a "split into #a, #b" line, and it is
        closed once every one of them is merged (a pull request) or closed (an issue). A body
        that only *mentions* numbers states nothing, so it is never closed from here. One line
        says which slices landed, and **the state of each item decides, never a ticked box**: a
        tick says the coordinator believes it landed, and the close is the transport's, so it
        checks. An item GitHub could not answer for stays open, because a wrong close is a
        round ended early and a late close is only late.

        Returns whether it closed the issue, and leaves the caller's ``issue`` marked CLOSED
        so the same poll sees the round end rather than one poll later.
        """
        if str(getattr(issue, "state", "")).upper() == "CLOSED":
            return False
        number = int(getattr(issue, "number", 0) or 0)
        key = f"{repo}#{number}"
        if key in self.state.round_reports or key in self.state.pending_reports:
            # Closed already and its round end queued: a GitHub that still answers OPEN for a
            # moment must not earn a second comment.
            return False
        refs, listed = listed_item_refs(getattr(issue, "body", "") or "", number)
        if not listed:
            # The body mentions numbers; it does not state its slices. Closing on a mention is
            # how #189 would close with two of its own items still open (#227 review).
            logger.debug("parent %s#%s states no slices (a mention is not a listing); left open",
                         repo, number)
            return False
        states = self._item_states(repo, issue, refs)
        if not states:
            return False                              # nothing to read: not a parent
        blockers = [(item, why) for item, why in states if why]
        if blockers:
            logger.debug("parent %s#%s stays open: %s", repo, number,
                         ", ".join(f"#{item} {why}" for item, why in blockers))
            return False
        landed = ", ".join(f"#{item}" for item, _why in states)
        # The note says "this landing was already announced", and it carries **which** landing
        # (#238): a note from an earlier poll whose slices have moved on must not silence the
        # close that finally works. Before this it was a bare timestamp, so the reason for a
        # close was suppressed for ever after one failed attempt, and a parent could end up
        # closed with nobody able to account for why.
        said = self.state.parent_notes.get(key) or ""
        announced = said.split("|", 1)[1] if "|" in said else ""
        comment = "" if announced == landed and announced else (
            f"Every listed slice has landed ({landed}), so this parent closes itself. "
            f"— the transport")
        if not self.client.close_issue(repo, number, comment):
            # The line goes out once per landing, not on every poll: a close that keeps
            # failing for the same reason is retried without re-posting its comment (#227
            # review). A landing that changed earns its own line (#238).
            if comment:
                self.state.parent_notes[key] = f"{_utc()}|{landed}"
            outcome.errors.append(f"{repo}#{number}: could not close a finished parent issue")
            return False
        issue.state = "CLOSED"
        self.state.parent_notes.pop(key, None)
        logger.info("parent %s#%s closed: every listed item landed (%s)", repo, number, landed)
        return True

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

        # #227 item 2: a parent whose every listed slice has landed closes itself, before the
        # open/closed split below -- so the close this poll makes is the round end this poll
        # reports, not one a later poll notices. A parent the transport closed counts as one
        # that was open before this poll: its close is still the round end it earns.
        closed_by_us = set()
        for issue in list(issues):
            try:
                if self._close_finished_parent(repo, issue, outcome):
                    closed_by_us.add(int(getattr(issue, "number", 0) or 0))
            except GitHubError as exc:
                outcome.errors.append(f"{repo}#{getattr(issue, 'number', '?')}: {exc}")

        open_now = sorted({int(i.number) for i in issues if str(i.state).upper() != "CLOSED"})
        closed_now = {int(i.number): i for i in issues
                      if str(i.state).upper() == "CLOSED"}
        previously_open = set(self.state.tracking_open.get(repo) or []) | closed_by_us
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

        if not self._may_poll():
            # #83: the shared GitHub login is read by exactly one bot -- the one the bus lease
            # names. A peer that polled too would handle the same notification twice, and would
            # keep polling after a successor took over. A watcher with no channel is the
            # standalone `gh-poll --once` path, which is the only reader by construction.
            logger.info("github: not the coordinator on the bus; skipping this poll")
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
            # CI on the repo itself, before and apart from the notifications: a failed
            # notification fetch must not also blind the swarm to finished runs.
            await self._poll_repo_ci(project, router, outcome)
            await self._check_new_prs(project, router, outcome)
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
                # Issue #87: a backlog is not news. A fresh start (first enable, a rebuilt
                # state file, a poller that was down) sees every unread notification at once,
                # and answering a three-day-old run in the group told the swarm about the past.
                if self._in_backlog(notif):
                    self.state.mark_processed(notif.id, notif.updated_at)
                    if self.config.github.mark_read:
                        self.client.mark_notification_read(notif.id)
                    outcome.backlog_skipped += 1
                    logger.info("notification %s is older than github.backlog_hours: marked "
                                "read without a turn", notif.id)
                    continue
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
                        # #87 review: the *actor* is whoever spoke last, not the PR's author.
                        # Taking the author quieted a person's comment on a swarm-opened PR --
                        # the author was the bot, so the reply it owed was never written.
                        last = pr.comments[-1] if pr.comments else None
                        actor = (last.author if last and last.author else "") or pr.author
                        user = actor or "a contributor"
                        said = last.body[:200] if last and last.body else ""
                        snippet = said or pr.title or snippet
                elif num is not None:
                    issue = self.client.issue_view(repo, num)
                    if issue:
                        if issue.comments:
                            user = issue.comments[-1].author or issue.author or "a contributor"
                            snippet = issue.comments[-1].body[:200]
                        else:
                            user = issue.author or "a contributor"
                            snippet = issue.body[:200] if issue.body else issue.title

                # Issue #87: a bot's own machinery -- a CI run, a workflow -- and a bot's own
                # activity never open a turn. One short line in the item's topic says what it
                # was, if the topic is there to say it in; the facts themselves land on the CI
                # watches and the round state, which read the repo, not the notification.
                quiet = self._quiet_reason(notif, user)
                if quiet:
                    self.state.mark_processed(notif.id, notif.updated_at)
                    if self.config.github.mark_read:
                        self.client.mark_notification_read(notif.id)
                    outcome.quiet_skipped += 1
                    said = f"GitHub: {repo}#{num} — {quiet}; nothing to answer."
                    await self._quiet_notice(chat_id, thread_id, num, said)
                    logger.info("notification %s: %s; posted one line, no turn", notif.id, quiet)
                    continue

                # Frame turn message
                if notif.reason == "review_requested":
                    turn_text = f"GitHub review requested on {repo}#{num} by {user}"
                elif notif.reason == "assign":
                    turn_text = f"GitHub assignment: {repo}#{num} by {user}"
                elif notif.reason in ("mention", "team_mention"):
                    turn_text = f"GitHub notification: {user} mentioned you on {repo}#{num}: {snippet}"
                else:
                    turn_text = f"GitHub notification ({notif.reason}): {user} on {repo}#{num}: {snippet}"

                # The topic already about this issue or PR gets the turn, not the project's
                # default (General when no topic is bound): live, round 5.5, a CI failure on
                # #230 was answered in General while the PR's work ran in its round topic.
                where = (chat_id, thread_id)
                finder = getattr(self.channel, "topic_for_text", None)
                if callable(finder) and num is not None:
                    try:
                        found = finder(f"#{num}")
                        if isinstance(found, tuple) and len(found) == 2:
                            where = (int(found[0]), found[1])
                    except Exception:                       # noqa: BLE001 - keep the default
                        logger.debug("github: no topic lookup for #%s", num, exc_info=True)
                turn_chat, turn_thread = where
                try:
                    ctx_ok = self._run_turn_precheck(turn_chat, turn_thread, notif, outcome)
                    if not ctx_ok:
                        continue
                    reply = await self._run_turn(project, turn_chat, turn_thread, turn_text)
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
                    outcome.delivered_topics.append((turn_chat, turn_thread))
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
    # -- what is not news (#87) ---------------------------------------------------------
    def _stamp(self, text: str) -> Optional[float]:
        """An ISO-8601 notification stamp as an epoch, or ``None`` when it will not parse."""
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            return datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            return None

    def _in_backlog(self, notif: Notification, *, now: Optional[float] = None) -> bool:
        """Whether this notification is older than ``github.backlog_hours`` (#87).

        A poller that starts fresh -- first enable, a rebuilt state file, a day offline -- sees
        every unread notification at once, and each one used to become a chat reply about
        something that happened days ago. Past the window it is marked read without a turn:
        the item's own state is on GitHub, and the CI watches (#24) read current facts rather
        than notifications. ``0`` turns the rule off. An unparseable stamp is not made young:
        it is left to the old path rather than dropped on a guess.
        """
        hours = float(getattr(self.config.github, "backlog_hours", 0) or 0)
        if hours <= 0:
            return False
        then = self._stamp(notif.updated_at)
        if then is None:
            return False
        moment = time.time() if now is None else float(now)
        return moment - then > hours * 3600.0

    def _swarm_names(self) -> set:
        """Every name this swarm's own bots answer to, lower case, ``@`` stripped (#87)."""
        names = {str(self.config.identity.alias or "").strip().lstrip("@").lower(),
                 str(self.config.identity.name or "").strip().lstrip("@").lower()}
        if self.config.github is not None:
            names.add(str(self.config.github.login or "").strip().lstrip("@").lower())
        for alias, target in (getattr(self.config, "peers", {}) or {}).items():
            names.add(str(alias or "").strip().lstrip("@").lower())
            names.add(str(target or "").strip().lstrip("@").lower())
        return {name for name in names if name}

    def _quiet_reason(self, notif: Notification, actor: str) -> str:
        """Why this notification must not become a turn, or ``""`` when it may (#87).

        Two answers: it is CI or a workflow (the CI watches own those facts, and the run has no
        author to answer), or its actor is one of the swarm's own bots (one bot does not open a
        turn to discuss what another bot just did).
        """
        if str(notif.reason or "").strip().lower() in CI_REASONS:
            return "a CI run"
        if str(notif.subject_type or "").strip() in CI_SUBJECTS:
            return "a CI run"
        who = str(actor or "").strip().lstrip("@").lower()
        if who and who in self._swarm_names():
            return f"{actor} is one of the swarm's bots"
        return ""

    async def _quiet_notice(self, chat_id: int, thread_id: Optional[int],
                            number: Optional[int], text: str) -> None:
        """One short line in the topic that owns the item, never a turn (#64, #87)."""
        if self.channel is None:
            return                           # the CLI path has nowhere to say it
        chat, thread = chat_id, thread_id
        finder = getattr(self.channel, "topic_for_text", None)
        if callable(finder) and number is not None:
            try:
                found = finder(f"#{number}")
                if isinstance(found, tuple) and len(found) == 2:
                    chat, thread = int(found[0]), found[1]
            except Exception:                       # noqa: BLE001 - keep the default topic
                logger.debug("github: no topic lookup for #%s", number, exc_info=True)
        try:
            await self.channel.send(text, chat_id=chat, thread_id=thread, notice=True)
        except Exception:                           # noqa: BLE001 - a notice never fails a poll
            logger.warning("github: could not post the status notice", exc_info=True)

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

    async def _poll_repo_ci(self, project: Any, router: Optional[ProjectRouter],
                            outcome: GitHubPollOutcome) -> None:
        """Every open PR of the project's repo, every poll: a run that finishes wakes the
        coordinator once, in the topic already about that PR (admin, 23 Oct).

        What is watched comes from GitHub, never from a model: live, round 5.5, the last merge
        waited an hour because no reply had named the PR the way the per-PR watch needed. The
        poll runs on the lease holder only (``poll_once`` returns early elsewhere), so the
        bot it wakes *is* the coordinator, and a successor after a failover takes it over.

        A wake needs a run this poll **saw running**: ``reported`` keeps ``<sha>:pending`` for
        a head in flight, and only a later ``<sha>:success``/``failure`` wakes. A PR already
        finished when it is first seen (a restart, the first poll after a deploy) is recorded
        and wakes nobody, so turning this on cannot flood the coordinator with old results.
        """
        github = self.config.github
        if github is None or not getattr(github, "watch_repo_ci", True):
            return
        repo = str(getattr(project, "repo", "") or "")
        if not repo:
            return
        lister = getattr(self.client, "open_pr_checks", None)
        if not callable(lister):
            return                                        # a client that cannot list PRs
        try:
            found = lister(repo)
        except Exception as exc:                          # a poll must never die on CI
            outcome.errors.append(f"repo CI {repo}: {exc}")
            logger.warning("repo CI %s could not be polled: %s", repo, exc)
            return
        if found is None:
            return
        self._router = router
        self.ci_state.merge_from(self.ci_state_path)
        home = int(self.config.identity.home_channel_id or 0)
        topics = router.topics_of(project.key) if router is not None else []
        default = topics[0] if topics else (home, None)
        wakes: List[Tuple[Dict[str, Any], PRChecks]] = []
        changed = False
        for checks in found:
            if not checks.head_sha:
                continue
            key = _ci_key(repo, checks.number)
            revision = f"{checks.head_sha}:{checks.state}"
            before = self.ci_state.reported_result(key)
            if before == revision:
                continue
            self.ci_state.mark_reported(key, revision)
            changed = True
            outcome.ci_watched += 1
            if checks.state == "pending" or before != f"{checks.head_sha}:pending":
                continue                                  # in flight, or never seen running
            where = default
            finder = getattr(self.channel, "topic_for_text", None)
            if callable(finder):
                try:
                    hit = finder(f"#{checks.number}")
                    if isinstance(hit, tuple) and len(hit) == 2:
                        where = (int(hit[0]), hit[1])
                except Exception:                         # noqa: BLE001 - keep the default
                    logger.debug("repo CI: no topic lookup for #%s", checks.number,
                                 exc_info=True)
            self.ci_state.drop(key)                       # one wake: the per-PR watch is spent
            wakes.append(({"repo": repo, "number": checks.number, "chat_id": where[0],
                           "thread_id": where[1], "project": project.key}, checks))
        if changed:
            self._save_ci_state({_ci_key(repo, c.number) for _w, c in wakes})  # before turns
        for watch, checks in wakes:
            await self._wake_on_ci(watch, checks, outcome, router)

    #: A PR's body must name the issue it closes.
    _CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
    #: ...or the larger issue it is one step of: a phase PR must not close the whole roadmap
    #: issue (live, 25 Oct: #298, "Part of #260", was flagged). One issue may have several.
    #: A line of its own, so "as part of #120" in passing does not count.
    _PART_OF = re.compile(r"(?im)^\s*(?:[-*]\s*)?part\s+of\s+#\d+\b")
    #: Test removal and skipping a PR adds (unified diff lines).
    _TEST_REMOVED = re.compile(r"^-\s*(?:async\s+)?def\s+test_", re.M)
    _TEST_ADDED = re.compile(r"^\+\s*(?:async\s+)?def\s+test_", re.M)
    _SKIP_ADDED = re.compile(r"^\+.*(?:@unittest\.skip|@pytest\.mark\.skip|\.skipTest\()", re.M)

    async def _check_new_prs(self, project: Any, router: Optional[ProjectRouter],
                             outcome: GitHubPollOutcome) -> None:
        """The rulebook's PR rules, checked once per PR head on the lease holder's poll
        (admin, 24 Oct): the PR names the issue it closes (or is part of), one open PR per
        issue, and no test is deleted or skipped without a word. A finding is a PR comment and
        a line in the PR's topic, so it reaches the author's and the coordinator's token
        space."""
        lister = getattr(self.client, "open_prs", None)
        repo = str(getattr(project, "repo", "") or "")
        if not callable(lister) or not repo:
            return
        try:
            prs = lister(repo) or []
        except Exception as exc:                          # noqa: BLE001 - a poll must survive
            outcome.errors.append(f"PR checks {repo}: {exc}")
            return
        closes: Dict[str, List[int]] = {}
        for pr in prs:
            for ref in set(self._CLOSES.findall(str(pr.get("body") or ""))):
                closes.setdefault(ref, []).append(int(pr["number"]))
        changed = False
        for pr in prs:
            number, head = int(pr["number"]), str(pr.get("headRefOid") or "")
            key = f"check:{_ci_key(repo, number)}"
            if not head or self.ci_state.reported_result(key) == head:
                continue
            self.ci_state.mark_reported(key, head)
            changed = True
            findings: List[str] = []
            refs = sorted(set(self._CLOSES.findall(str(pr.get("body") or ""))), key=int)
            if not refs and not self._PART_OF.search(str(pr.get("body") or "")):
                findings.append("it names no issue it closes (`Closes #N` in the body, or "
                                "`Part of #N` for one step of a larger issue)")
            for ref in refs:
                others = [n for n in closes.get(ref, []) if n != number]
                if others:
                    findings.append(f"#{ref} already has open PR(s) "
                                    + ", ".join(f"#{n}" for n in others)
                                    + ": one issue, one pull request")
            differ = getattr(self.client, "pr_diff", None)
            diff = differ(repo, number) if callable(differ) else ""
            removed = len(self._TEST_REMOVED.findall(diff or ""))
            added = len(self._TEST_ADDED.findall(diff or ""))
            if removed > added:
                findings.append(f"it removes {removed - added} more test function(s) than it adds")
            if self._SKIP_ADDED.search(diff or ""):
                findings.append("it adds a test skip")
            if not findings:
                continue
            text = (f"Transport check on #{number} at {head[:7]}: " + "; ".join(findings)
                    + ". (The rulebook, checked in transport space.)")
            try:
                self.client.comment(repo, number, text)
            except Exception:                             # noqa: BLE001 - the topic line stays
                logger.warning("PR check: could not comment on %s#%s", repo, number)
            if self.channel is not None:
                where = None
                finder = getattr(self.channel, "topic_for_text", None)
                if callable(finder):
                    try:
                        hit = finder(f"#{number}")
                        where = hit if isinstance(hit, tuple) and len(hit) == 2 else None
                    except Exception:                     # noqa: BLE001
                        where = None
                home = int(self.config.identity.home_channel_id or 0)
                chat, thread = where or (home, None)
                try:
                    await self.channel.send(f"🔎 {text}", chat_id=chat, thread_id=thread,
                                            silent=True, notice=True)
                except Exception:                         # noqa: BLE001
                    logger.warning("PR check: could not post the finding for #%s", number)
        if changed:
            self._save_ci_state()

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
            text += (" The PR is ready for review and QA -- or to merge, if both verdicts are "
                     "already on this head.")
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
