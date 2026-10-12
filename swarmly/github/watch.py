"""GitHub notifications backstop watcher.

Polls GitHub notifications for configured projects, deduplicates via state file,
skips items already mentioned/handled in the bound topic, and queues turns for missed pings.
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

from swarmly.github.client import GitHubClient, GitHubError, Notification
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.projects import ProjectRouter

if TYPE_CHECKING:
    from swarmly.telegram.channel import AgentChannel

logger = logging.getLogger("swarmly.github.watch")

#: Reasons addressed to the agent itself, or on its own issue or pull request (author: new
#: review comments on its PR). Only these fire again on new activity; a thread the agent
#: merely commented on or follows (comment, subscribed, ci_activity, ...) fires once, so two
#: agents commenting on one issue cannot wake each other forever.
REFIRE_REASONS = frozenset({"mention", "team_mention", "review_requested", "assign",
                            "author"})


def _utc(now: Optional[float] = None) -> str:
    moment = (
        datetime.datetime.now(datetime.timezone.utc)
        if now is None
        else datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class NotificationState:
    """Tracks processed notification IDs and last poll timestamp."""

    processed_ids: Dict[str, str] = field(default_factory=dict)  # id -> timestamp
    last_poll: Optional[str] = None

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
            return cls(processed_ids=processed, last_poll=data.get("last_poll"))
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
        # Prune if too large
        if len(self.processed_ids) > 2000:
            keys = list(self.processed_ids.keys())[:-1000]
            for k in keys:
                self.processed_ids.pop(k, None)


@dataclass
class GitHubPollOutcome:
    polled_repos: List[str] = field(default_factory=list)
    notifications_seen: int = 0
    turns_queued: int = 0
    turns_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    delivered_topics: List[Tuple[int, Optional[int]]] = field(default_factory=list)


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
        )
        self.state_path = state_path if state_path is not None else state_file_for(config)
        self.channel = channel
        self.state = NotificationState.load(self.state_path)

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

            for notif in notifs:
                if self.state.is_processed(notif.id, notif.updated_at,
                                           refire=notif.reason in REFIRE_REASONS):
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
                    from swarmly.telegram.topics import context_for
                    ctx = context_for(chat_id, thread_id, self.config.identity.home_channel_id)
                    incoming = IncomingMessage(
                        text=turn_text,
                        author_alias="GitHub",
                        context_id=ctx,
                        project=project.key,
                        project_repo=project.repo,
                    )
                    if self.channel is not None:
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
                    else:
                        from swarmly.telegram.channel import build_adapter
                        workdir = router.workdir_for(project, thread_id or 0)
                        adapter_cfg = dataclasses.replace(self.config.adapter, cwd=str(workdir))
                        cfg = dataclasses.replace(self.config, adapter=adapter_cfg)
                        adapter = build_adapter(cfg, transport=False)
                        try:
                            await adapter.respond(incoming)
                        finally:
                            await adapter.close()

                    self.state.mark_processed(notif.id, notif.updated_at)
                    if self.config.github.mark_read:
                        self.client.mark_notification_read(notif.id)
                    outcome.turns_queued += 1
                    outcome.delivered_topics.append((chat_id, thread_id))
                except Exception as exc:
                    outcome.errors.append(f"notification {notif.id}: {exc}")
                    logger.exception("failed to process notification %s: %s", notif.id, exc)

        self.state.last_poll = _utc()
        self.state.save(self.state_path)
        return outcome


async def github_poll_once(
    config: AgentConfig,
    channel: Optional[AgentChannel] = None,
    client: Optional[GitHubClient] = None,
    state_path: Optional[Path] = None,
) -> GitHubPollOutcome:
    """Shared entry point for both in-transport background polling and the CLI command."""
    watcher = GitHubWatcher(config, client=client, state_path=state_path, channel=channel)
    return await watcher.poll_once()
