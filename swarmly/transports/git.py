"""gitbus: envelope files committed to ``.agents/inbox/`` of a git repository.

This is the original durable plane, moved behind :class:`~swarmly.transports.base.BusTransport`
unchanged: reads prefer a fetched ref (``git ls-tree`` + ``git show``, never touching the
working tree) and fall back to the local checkout; writes land in the working tree and,
when published, are committed and pushed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from swarmly.transports.base import (ROUNDS_RELPATH, is_bus_document,
                                     is_envelope_name, write_envelope)

logger = logging.getLogger("swarmly.transports.git")

INBOX_RELPATH = ".agents/inbox"

#: Review P1-5: no git call may hang a poll cycle or a sender forever.
GIT_TIMEOUT_SECONDS = 60.0
PUSH_TIMEOUT_SECONDS = 120.0
#: Another git command holding .git/index.lock: how often and how long to retry.
INDEX_LOCK_RETRIES = 10
INDEX_LOCK_WAIT_SECONDS = 0.5
#: Agents with their own bus checkouts push to one branch: a push can lose the race to
#: another agent's, so the retry on the remote's tip runs a few times, with a short pause.
TIP_RETRIES = 4
TIP_RETRY_WAIT_SECONDS = 1.0


class BusPushError(RuntimeError):
    """The bus commit succeeded but ``git push`` failed (drill finding, PR #78)."""


class GitTransport:
    """Envelope files in ``<root>/.agents/inbox`` of a git checkout."""

    kind = "git"

    def __init__(self, root: Path, ref: Optional[str] = None, remote: str = "origin",
                 identity: Optional[Tuple[str, str]] = None):
        self.identity = identity        # (name, email) bus commits are made as
        self.root = Path(root)
        self.ref = ref
        self.remote = remote
        self.inbox_dir = self.root / ".agents" / "inbox"

    def describe(self) -> str:
        where = f"ref {self.ref}" if self.ref else "working tree"
        return f"git: {self.inbox_dir} ({where})"

    # -- reading ---------------------------------------------------------------------
    def refresh(self) -> bool:
        """Fetch only, and say whether the ref is now trustworthy.

        The working tree is never touched, so a dirty or diverged checkout cannot block
        delivery. ``False`` means the fetch did not succeed: a caller that arbitrates on the
        bus (a wake claim) must not treat a stale ref as an empty one.
        """
        argv = ["git", "-C", str(self.root), "fetch", "--quiet", self.remote]
        prefix = f"{self.remote}/"
        if self.ref and self.ref.startswith(prefix):
            # Only the bus branch: other branches (a PR, a dist branch) are none of the bus's
            # business, and fetching them races other agents sharing this checkout.
            branch = self.ref[len(prefix):]
            argv.append(f"+refs/heads/{branch}:refs/remotes/{self.remote}/{branch}")
        try:
            for attempt in range(2):
                result = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                        check=False)
                if result.returncode == 0:
                    return True
                # Two agents fetching one checkout at once: one loses the ref lock. A
                # second try a moment later finds the ref already updated.
                if attempt == 0 and "cannot lock ref" in (result.stderr or ""):
                    time.sleep(1.0)
                    continue
                logger.warning("gitbus: git fetch exited %s: %s", result.returncode,
                               (result.stderr or "").strip()[:200])
                return False
        except Exception:  # noqa: BLE001 - refresh is best-effort
            logger.warning("gitbus: git fetch could not run", exc_info=True)
            return False
        return False

    def list_messages(self) -> List[Tuple[str, str]]:
        if self.ref:
            try:
                return self._list_at_ref(self.ref)
            except Exception:  # noqa: BLE001 - fall back to the local checkout
                logger.info("gitbus: ref %r unavailable; reading the local checkout instead",
                            self.ref)
        return self._list_local()

    def _list_at_ref(self, ref: str) -> List[Tuple[str, str]]:
        listing = subprocess.run(
            ["git", "-C", str(self.root), "ls-tree", "-r", "--name-only", ref,
             INBOX_RELPATH + "/"],
            capture_output=True, text=True, timeout=30, check=True).stdout
        items: List[Tuple[str, str]] = []
        for name in sorted(listing.splitlines()):
            if not is_envelope_name(name):
                continue
            data = subprocess.run(["git", "-C", str(self.root), "show", f"{ref}:{name}"],
                                  capture_output=True, text=True, timeout=30,
                                  check=True).stdout
            items.append((name, data))
        return items

    def list_at_ref(self, relpath: str) -> Optional[List[str]]:
        """File names directly under *relpath* at the bus ref (a round's segments).

        Issue #86 review: the round state is a bus *document*, not an envelope, so it has
        its own read path. ``[]`` means the ref was read and holds nothing there; ``None``
        means it could not be read at all. The two are never the same answer: a caller that
        conflates them reports a broken bus as an empty round (PR #89 review, blocker 1).
        """
        if not self.ref:
            return None
        prefix = relpath.rstrip("/") + "/"
        result = self._git("ls-tree", "-r", "--name-only", self.ref, prefix, timeout=30)
        if result.returncode != 0:
            logger.warning("gitbus: could not list %s at %s: %s", relpath, self.ref,
                           (result.stderr or "").strip()[:200])
            return None
        return sorted(path[len(prefix):] for path in (result.stdout or "").splitlines()
                      if path.startswith(prefix))

    def read_at_ref(self, relpath: str) -> Optional[str]:
        """The bytes of one bus document at the ref, or ``None`` when it is not there."""
        if not self.ref:
            return None
        result = self._git("show", f"{self.ref}:{relpath}", timeout=30)
        return result.stdout if result.returncode == 0 else None

    def _list_local(self) -> List[Tuple[str, str]]:
        if not self.inbox_dir.is_dir():
            return []
        items: List[Tuple[str, str]] = []
        for path in sorted(self.inbox_dir.glob("*.md")):
            if not is_envelope_name(path.name):
                continue
            try:
                items.append((str(path), path.read_text(encoding="utf-8")))
            except OSError:
                logger.warning("gitbus: unreadable message file %s", path)
        return items

    # -- writing ---------------------------------------------------------------------
    def post(self, filename: str, content: str, *, publish: bool = True,
             summary: Optional[str] = None) -> str:
        path = write_envelope(self.root, self.inbox_dir, filename, content)
        name = path.name
        if publish:
            self.commit_and_push(summary or f"bus(a2a): {filename}",
                                 paths=[f"{INBOX_RELPATH}/{name}"])
        return str(path)

    def _git(self, *args: str, timeout: float = GIT_TIMEOUT_SECONDS,
             extra_env: Optional[dict] = None) -> "subprocess.CompletedProcess[str]":
        # Always the bus root, never the process cwd (PR #78 drill); never a prompt.
        # LC_ALL=C: callers match git's English messages ("index.lock", "nothing to commit").
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", **(extra_env or {}))
        if self.identity:
            # The agent's own identity, never the machine's global git user (live test: bus
            # commits were authored as the machine owner).
            name, email = self.identity
            env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                       GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        return subprocess.run(["git", "-C", str(self.root), *args], check=False,
                              capture_output=True, text=True, timeout=timeout, env=env)

    def _push_target(self) -> Tuple[str, str]:
        """``(remote, branch)`` to push the bus commit to: the current branch's upstream,
        else the same branch name on :attr:`remote`."""
        upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream.returncode == 0 and "/" in upstream.stdout.strip():
            remote, branch = upstream.stdout.strip().split("/", 1)
            return remote, branch
        head = self._git("rev-parse", "--abbrev-ref", "HEAD")
        branch = head.stdout.strip()
        if head.returncode != 0 or not branch or branch == "HEAD":
            raise BusPushError("the bus checkout is not on a branch (detached HEAD); the "
                               "commit is local - push it once a branch is checked out")
        return self.remote, branch

    def _locked_retry(self, *args: str) -> "subprocess.CompletedProcess[str]":
        """Run a git command that takes the index lock, retrying while another holds it."""
        result = self._git(*args)
        for _ in range(INDEX_LOCK_RETRIES):
            if result.returncode == 0 or "index.lock" not in (result.stderr or ""):
                break
            time.sleep(INDEX_LOCK_WAIT_SECONDS)
            result = self._git(*args)
        return result

    def _push_onto_remote_tip(self, remote: str, branch: str, files: List[str],
                              commit_msg: str) -> "subprocess.CompletedProcess[str]":
        """Push the files of the bus commit just made as a new commit on the remote's tip.

        P1-4 re-review M2: the retry used ``git pull --rebase``, which also moved the
        working tree of ``bus.repo`` forward, including ``config/a2a_config.json``, the
        registry bus messages are verified against, so a key change pushed from elsewhere
        went live without anyone pulling it. This builds the commit in a scratch index
        instead; the working tree and the local branch stay where they are.
        """
        # A private ref per call, not FETCH_HEAD: the poller's refresh() rewrites
        # FETCH_HEAD, and two posts retrying at once must not share one ref either.
        # --refmap= keeps the fetch off refs/remotes/<remote>/<branch>, whose lock
        # refresh() holds while it fetches.
        tip_ref = f"refs/a2a/retry-tip-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            return self._retry_on_tip(remote, branch, files, commit_msg, tip_ref)
        finally:
            self._git("update-ref", "-d", tip_ref)

    def _retry_on_tip(self, remote: str, branch: str, files: List[str], commit_msg: str,
                      tip_ref: str) -> "subprocess.CompletedProcess[str]":
        fetch = self._git("fetch", "--quiet", "--refmap=", remote,
                          f"+refs/heads/{branch}:{tip_ref}", timeout=PUSH_TIMEOUT_SECONDS)
        if fetch.returncode != 0:
            return fetch
        resolved = self._git("rev-parse", "--verify", "--quiet", f"{tip_ref}^{{commit}}")
        if resolved.returncode != 0:
            return subprocess.CompletedProcess(args=["git", "push"], returncode=1, stdout="",
                                               stderr="the remote's new tip could not be read")
        tip = resolved.stdout.strip()
        # Re-review B1: HEAD may already be another post's commit, so the retry carries the
        # files this post staged (commit_and_push lists them), never "whatever HEAD has".
        # It also carries bus files committed here earlier whose own push failed (a
        # checkout that fell behind kept them only locally, and they were never delivered).
        # Issue #86: round state is a bus document under .agents/rounds, so a lost push
        # that kept it local is carried forward too -- an envelope-only filter here would
        # drop a state rewrite silently and leave the bus a sequence behind.
        unpushed = self._git("diff", "--name-only", "--diff-filter=A", f"{tip}...HEAD", "--",
                             INBOX_RELPATH, ROUNDS_RELPATH)
        earlier = [path for path in unpushed.stdout.splitlines()
                   if unpushed.returncode == 0
                   and (is_envelope_name(Path(path).name) or is_bus_document(path))]
        changed = list(dict.fromkeys([*files, *earlier]))
        if not changed:
            return subprocess.CompletedProcess(
                args=["git", "push"], returncode=1, stdout="",
                stderr="no bus file found to put on the remote's new tip")
        with tempfile.TemporaryDirectory() as scratch:
            index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
            if self._git("read-tree", tip, extra_env=index).returncode != 0:
                return subprocess.CompletedProcess(
                    args=["git", "push"], returncode=1, stdout="",
                    stderr="the remote's new tip could not be read")
            for path in changed:
                blob = self._git("rev-parse", "--verify", "--quiet", f"HEAD:{path}")
                if blob.returncode != 0 and (self.root / path).is_file():
                    blob = self._git("hash-object", "-w", "--", path)
                staged = blob.returncode == 0 and self._git(
                    "update-index", "--add", "--cacheinfo",
                    f"100644,{blob.stdout.strip()},{path}", extra_env=index).returncode == 0
                if not staged:
                    return subprocess.CompletedProcess(
                        args=["git", "push"], returncode=1, stdout="",
                        stderr=f"could not put {path} on the remote's new tip")
            tree = self._git("write-tree", extra_env=index).stdout.strip()
        if not tree:
            return subprocess.CompletedProcess(
                args=["git", "push"], returncode=1, stdout="",
                stderr="git write-tree failed; nothing was pushed")
        if tree == self._git("rev-parse", f"{tip}^{{tree}}").stdout.strip():
            # Every file of this post is already on the remote, byte for byte (another
            # retry pushed it): the message is delivered, and an empty commit is not pushed.
            return subprocess.CompletedProcess(args=["git", "push"], returncode=0,
                                               stdout="", stderr="")
        made = self._git("commit-tree", tree, "-p", tip, "-m", commit_msg)
        commit = made.stdout.strip()
        if made.returncode != 0 or not commit:
            return subprocess.CompletedProcess(args=["git", "push"],
                                               returncode=made.returncode or 1, stdout="",
                                               stderr=made.stderr or "git commit-tree failed")
        return self._git("push", remote, f"{commit}:{branch}", timeout=PUSH_TIMEOUT_SECONDS)

    def commit_and_push(self, commit_msg: str, paths: Optional[List[str]] = None) -> bool:
        """Commit the bus files and push them to the current branch's upstream.

        Review P1-4: only ``paths`` (default: ``.agents/inbox``) are committed, never
        whatever else happens to be staged, and the push names its remote and branch.
        Every git call has a timeout. A push rejected because the remote moved on is
        retried once as a commit on the remote's tip, without touching the working tree.

        A failed push must never be silent (drill finding, PR #78): the local
        commit remains so the message is not lost, but the caller gets a
        :class:`BusPushError` carrying git's stderr and a recovery hint.
        """
        pathspec = list(paths or [INBOX_RELPATH])
        try:
            # P1-4 re-review B1: another git command (a pull, a concurrent post) holding
            # .git/index.lock made the add fail and the post still report success.
            add = self._locked_retry("add", "--", *pathspec)
            if add.returncode != 0:
                lines = (add.stderr or add.stdout or "").strip().splitlines()
                raise BusPushError(
                    f"git add failed ({lines[-1] if lines else add.returncode}); the message "
                    "file is written but not committed - commit it by hand")
            # What this post commits, for a retry on the remote's tip: the named files, or
            # for the legacy whole-inbox call what the add staged (added or changed files).
            carry = [path for path in pathspec if (self.root / path).is_file()]
            if len(carry) != len(pathspec):
                staged = self._git("diff", "--cached", "--name-only", "--diff-filter=AM", "--",
                                   *pathspec)
                carry = staged.stdout.splitlines() if staged.returncode == 0 else []
            res_commit = self._locked_retry("commit", "--only", "-m", commit_msg, "--",
                                            *pathspec)
            for _ in range(INDEX_LOCK_RETRIES):
                # A concurrent `commit --only` in the same checkout can drop this post's
                # staged entry from the index between the add and the commit; stage it again.
                if res_commit.returncode == 0 or "did not match any file" not in (
                        res_commit.stderr or ""):
                    break
                self._locked_retry("add", "--", *pathspec)
                res_commit = self._locked_retry("commit", "--only", "-m", commit_msg, "--",
                                                *pathspec)
            committed = res_commit.returncode == 0
            if not committed:
                detail = (res_commit.stderr or res_commit.stdout or "").strip()
                nothing_new = "nothing to commit" in detail or "no changes added" in detail
                if nothing_new and not carry:
                    return False
            if not committed and not nothing_new:
                # Review #163 n15: e.g. no git identity on this host. The file is written but
                # not committed, so nobody else will see it; say so instead of exiting 0.
                lines = detail.splitlines()
                raise BusPushError(
                    f"git commit failed ({lines[-1] if lines else 'exit code '}"
                    f"{'' if lines else res_commit.returncode}); the message file is written "
                    "but not committed - fix that (for example set user.name and user.email "
                    "in the bus checkout) and commit it")
            # Nothing new to commit for a named file means it was committed before: an
            # earlier post of the same envelope whose push failed. Push it now rather than
            # report success for a message that is only local (re-review E4).
            remote, branch = self._push_target()
            push = self._git("push", remote, f"HEAD:{branch}", timeout=PUSH_TIMEOUT_SECONDS)
            retried = push.returncode != 0 and _remote_moved(push)
            for attempt in range(TIP_RETRIES if retried else 0):
                if attempt:
                    logger.info("gitbus: push lost a race to another agent (%s); retrying",
                                ((push.stderr or push.stdout or "").strip().splitlines()
                                 or ["rejected"])[0])
                    time.sleep(TIP_RETRY_WAIT_SECONDS * attempt)
                push = self._push_onto_remote_tip(remote, branch, carry, commit_msg)
                if push.returncode == 0 or not _remote_moved(push):
                    break
            if push.returncode != 0:
                detail = (push.stderr or push.stdout or "").strip().splitlines()
                summary = detail[-1] if detail else f"exit code {push.returncode}"
                # After a retry the local branch has diverged from the remote, so a plain
                # `git push` would be rejected again.
                how = ("`git pull --rebase` then `git push`" if retried else
                       "`git push` once the remote is reachable")
                raise BusPushError(f"git push failed ({summary}); the commit is local - run "
                                   f"{how}")
            return True
        except BusPushError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise BusPushError(f"git {' '.join(str(a) for a in exc.cmd[3:4])} timed out after "
                               f"{exc.timeout:.0f}s; the commit may be local - check "
                               "`git status` and push by hand") from None
        except Exception as exc:                       # noqa: BLE001 - never silent
            raise BusPushError(f"git failed ({exc}); the message file is written but may not "
                               "be committed or pushed - check `git status`") from exc


def _remote_moved(push: "subprocess.CompletedProcess[str]") -> bool:
    text = (push.stderr or "") + (push.stdout or "")
    return any(sign in text for sign in ("non-fast-forward", "fetch first", "[rejected]"))
