"""Review P1-4/P1-5: a gitbus post commits only its own file, pushes to a named branch,
survives a remote that moved on, and never waits on git forever."""

import argparse
import asyncio
import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from tests.gitenv import git_env
from swarmly.transports import git as gitbus
from swarmly.transports.git import BusPushError, GitTransport

ENVELOPE = "msg-20261009T230000Z-p1p4_codex_query_request.md"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env=git_env()).stdout.strip()


class _Repos(unittest.TestCase):
    """A bare remote and a clone of it with an identity, on branch ``main``."""

    def setUp(self):
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("GIT_")}
        self.addCleanup(os.environ.update, saved)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.remote = self.base / "remote.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.clone = self.clone_as("one")
        (self.clone / "README").write_text("x\n", encoding="utf-8")
        git(self.clone, "add", "README")
        git(self.clone, "commit", "-q", "-m", "init")
        git(self.clone, "push", "-q", "-u", "origin", "main")

    def clone_as(self, name):
        path = self.base / name
        git(self.base, "clone", "-q", str(self.remote), str(path))
        git(path, "config", "user.email", f"{name}@example.com")
        git(path, "config", "user.name", name)
        git(path, "checkout", "-q", "-B", "main")
        return path

    def post(self, root, name=ENVELOPE):
        return GitTransport(root).post(name, "---\nid: x\n---\nbody\n")


class TestGitbusPush(_Repos):

    def test_only_the_envelope_is_committed(self):
        (self.clone / "notes.txt").write_text("private\n", encoding="utf-8")
        git(self.clone, "add", "notes.txt")
        self.post(self.clone)
        files = git(self.clone, "show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertEqual(files, [f".agents/inbox/{ENVELOPE}"])
        self.assertIn("A  notes.txt", git(self.clone, "status", "--porcelain"))

    def test_a_remote_that_moved_on_is_rebased_onto_once(self):
        other = self.clone_as("two")
        (other / "other.txt").write_text("y\n", encoding="utf-8")
        git(other, "add", "other.txt")
        git(other, "commit", "-q", "-m", "other")
        git(other, "push", "-q", "origin", "main")
        self.post(self.clone)
        log = git(self.base, "--git-dir", str(self.remote), "log", "--format=%s", "main")
        self.assertEqual(log.splitlines()[:2], [f"bus(a2a): {ENVELOPE}", "other"])

    def test_the_retry_carries_this_post_even_after_another_local_commit(self):
        # Re-review B1: the retry took HEAD's files, so a commit landing between this post's
        # commit and the retry left it pushing an empty commit and reporting success.
        other = self.clone_as("two")
        (other / "other.txt").write_text("y\n", encoding="utf-8")
        git(other, "add", "other.txt")
        git(other, "commit", "-q", "-m", "other")
        git(other, "push", "-q", "origin", "main")
        original = GitTransport._push_onto_remote_tip

        def after_another_commit(transport, *args):
            (self.clone / "later.txt").write_text("z\n", encoding="utf-8")
            git(self.clone, "add", "later.txt")
            git(self.clone, "commit", "-q", "-m", "later")
            return original(transport, *args)

        with patch.object(GitTransport, "_push_onto_remote_tip", after_another_commit):
            self.post(self.clone)
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        self.assertIn(f".agents/inbox/{ENVELOPE}", files)

    def test_a_retry_that_loses_a_race_tries_again(self):
        # Four agents with their own bus checkouts push to one branch: another agent's push
        # can land between this retry's fetch and its push.
        self._moved_remote()
        racer = self.base / "two"
        original = GitTransport._git
        pushes = []

        def racing(transport, *args, **kwargs):
            if args and args[0] == "push" and not args[-1].startswith("HEAD:") and not pushes:
                pushes.append(1)                 # the first push onto the fetched tip
                (racer / "race.txt").write_text("r\n", encoding="utf-8")
                git(racer, "add", ".")
                git(racer, "commit", "-q", "-m", "race")
                git(racer, "push", "-q", "origin", "main")
            return original(transport, *args, **kwargs)

        with patch.object(GitTransport, "_git", racing), \
                patch.object(gitbus, "TIP_RETRY_WAIT_SECONDS", 0):
            self.post(self.clone)
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        self.assertIn(f".agents/inbox/{ENVELOPE}", files)
        self.assertIn("race.txt", files)

    def test_configured_bus_ref_retries_when_remote_moves_during_push(self):
        racer = self.clone_as("two")
        original = GitTransport._git
        raced = []

        def racing(transport, *args, **kwargs):
            if args and args[0] == "push" and not raced:
                raced.append(True)
                (racer / "race.txt").write_text("r\n", encoding="utf-8")
                git(racer, "add", ".")
                git(racer, "commit", "-q", "-m", "race")
                git(racer, "push", "-q", "origin", "main")
            return original(transport, *args, **kwargs)

        with patch.object(GitTransport, "_git", racing), \
                patch.object(gitbus, "TIP_RETRY_WAIT_SECONDS", 0):
            GitTransport(self.clone, ref="origin/main").post(
                ENVELOPE, "---\nid: x\n---\nbody\n")
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        self.assertIn(f".agents/inbox/{ENVELOPE}", files)
        self.assertIn("race.txt", files)
        self.assertEqual(git(self.clone, "log", "-1", "--format=%s"), "init")

    def test_an_earlier_post_stuck_locally_rides_the_next_retry(self):
        self._moved_remote()
        failed = subprocess.CompletedProcess(args=["git", "push"], returncode=1, stdout="",
                                             stderr="fatal: unable to access remote")
        with patch.object(GitTransport, "_push_onto_remote_tip", return_value=failed):
            with self.assertRaises(gitbus.BusPushError):
                self.post(self.clone, "20261010T000000Z-a-b-first.md")
        self.post(self.clone, "20261010T000001Z-a-b-second.md")
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        self.assertIn(".agents/inbox/20261010T000000Z-a-b-first.md", files)
        self.assertIn(".agents/inbox/20261010T000001Z-a-b-second.md", files)

    def test_the_retry_does_not_carry_files_the_remote_already_has(self):
        # #126: the local branch is never fast-forwarded, so every bus file this checkout ever
        # pushed through a retry looked "added since the merge base" and was staged again on
        # each later retry -- two git calls per file, and one lease push took about 40 s.
        other = self.clone_as("two")

        def move_remote(n):
            git(other, "pull", "-q", "--no-rebase", "origin", "main")
            (other / f"other{n}.txt").write_text("y\n", encoding="utf-8")
            git(other, "add", f"other{n}.txt")
            git(other, "commit", "-q", "-m", f"other {n}")
            git(other, "push", "-q", "origin", "main")

        names = [f"20261010T00000{n}Z-a-b-post{n}.md" for n in range(3)]
        for n, name in enumerate(names):
            move_remote(n)
            self.post(self.clone, name)
        move_remote(9)
        staged = []
        original = GitTransport._git

        def recording(transport, *args, **kwargs):
            if args and args[0] == "update-index":
                staged.append(args[-1].split(",", 2)[-1])
            return original(transport, *args, **kwargs)

        last = "20261010T000009Z-a-b-last.md"
        with patch.object(GitTransport, "_git", recording):
            self.post(self.clone, last)
        self.assertEqual(staged, [f".agents/inbox/{last}"])
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        for name in [*names, last]:
            self.assertIn(f".agents/inbox/{name}", files)

    def test_a_retry_with_nothing_to_carry_is_an_error(self):
        transport = GitTransport(self.clone)
        result = transport._push_onto_remote_tip("origin", "main",
                                                 [".agents/inbox/missing.md"], "x")
        self.assertNotEqual(result.returncode, 0)

    def _moved_remote(self):
        other = self.clone_as("two")
        (other / "other.txt").write_text("y\n", encoding="utf-8")
        git(other, "add", "other.txt")
        git(other, "commit", "-q", "-m", "other")
        git(other, "push", "-q", "origin", "main")

    def test_a_failing_commit_tree_in_the_retry_is_an_error(self):
        # Re-review B1 (third round): a failed commit-tree was run again and its result
        # returned as if it were the push, so nothing was pushed and the post succeeded.
        self._moved_remote()
        original = GitTransport._git

        failures = []

        def failing(transport, *args, **kwargs):
            if args and args[0] == "commit-tree" and not failures:
                failures.append(args)                  # fails once, then works
                return subprocess.CompletedProcess(args, 128, "", "fatal: transient")
            return original(transport, *args, **kwargs)

        with patch.object(GitTransport, "_git", failing), \
                self.assertRaises(BusPushError) as raised:
            self.post(self.clone)
        self.assertIn("git pull --rebase", str(raised.exception))      # m3: diverged
        self.assertNotIn("refs/a2a/", git(self.clone, "for-each-ref"))

    def test_the_retry_reads_the_tip_from_its_own_ref_and_removes_it(self):
        # The poller's refresh() rewrites FETCH_HEAD; here it does so after every fetch.
        self._moved_remote()
        original = GitTransport._git

        def refresh_races(transport, *args, **kwargs):
            result = original(transport, *args, **kwargs)
            if args and args[0] == "fetch":
                (self.clone / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
            return result

        with patch.object(GitTransport, "_git", refresh_races):
            self.post(self.clone)
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        self.assertIn(f".agents/inbox/{ENVELOPE}", files)
        self.assertEqual(git(self.clone, "for-each-ref", "refs/a2a/"), "")

    def test_the_retry_fetch_leaves_the_remote_tracking_ref_alone(self):
        # 8168aa8 nit: without --refmap= the fetch also moves origin/main, and fails while
        # the poller's refresh() holds that ref's lock.
        self._moved_remote()
        before = git(self.clone, "rev-parse", "refs/remotes/origin/main")
        original = GitTransport._git
        seen = []

        def watch(transport, *args, **kwargs):
            result = original(transport, *args, **kwargs)
            if args and args[0] == "fetch":
                seen.append(git(self.clone, "rev-parse", "refs/remotes/origin/main"))
            return result

        with patch.object(GitTransport, "_git", watch):
            self.post(self.clone)
        self.assertEqual(seen, [before])

    def test_reposting_after_a_failed_push_pushes_it(self):
        # Re-review E4: the second post found nothing new to commit and reported success
        # for a message that was only in a local commit.
        with patch.object(GitTransport, "_push_target", return_value=("nowhere", "main")):
            with self.assertRaises(BusPushError):
                self.post(self.clone)
        self._moved_remote()
        self.post(self.clone)
        files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                    "main").splitlines()
        self.assertIn(f".agents/inbox/{ENVELOPE}", files)

    def test_the_retry_does_not_move_the_working_tree(self):
        other = self.clone_as("two")
        (other / "config").mkdir()
        (other / "config" / "a2a_config.json").write_text('{"new": "registry"}\n',
                                                         encoding="utf-8")
        git(other, "add", "config")
        git(other, "commit", "-q", "-m", "registry change")
        git(other, "push", "-q", "origin", "main")
        self.post(self.clone)
        self.assertFalse((self.clone / "config" / "a2a_config.json").exists())
        remote_files = git(self.base, "--git-dir", str(self.remote), "ls-tree", "-r",
                           "--name-only", "main").splitlines()
        self.assertIn("config/a2a_config.json", remote_files)
        self.assertIn(f".agents/inbox/{ENVELOPE}", remote_files)
        self.assertEqual(git(self.clone, "status", "--porcelain"), "")

    def test_a_held_index_lock_is_waited_for(self):
        import threading
        lock = self.clone / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        threading.Timer(1.0, lock.unlink).start()
        self.post(self.clone)
        log = git(self.base, "--git-dir", str(self.remote), "log", "--format=%s", "main")
        self.assertEqual(log.splitlines()[0], f"bus(a2a): {ENVELOPE}")

    def test_an_index_lock_that_stays_is_an_error(self):
        (self.clone / ".git" / "index.lock").write_text("", encoding="utf-8")
        with patch.object(gitbus, "INDEX_LOCK_RETRIES", 1), \
                patch.object(gitbus, "INDEX_LOCK_WAIT_SECONDS", 0.01):
            with self.assertRaises(BusPushError) as caught:
                self.post(self.clone)
        self.assertIn("not committed", str(caught.exception))

    def test_the_push_goes_to_the_upstream_branch(self):
        git(self.clone, "checkout", "-q", "-b", "work")
        git(self.clone, "push", "-q", "-u", "origin", "work:bus")
        self.post(self.clone)
        log = git(self.base, "--git-dir", str(self.remote), "log", "--format=%s", "bus")
        self.assertEqual(log.splitlines()[0], f"bus(a2a): {ENVELOPE}")

    def test_a_hung_push_is_an_error_not_a_hang(self):
        real = subprocess.run

        def run(argv, *args, **kwargs):
            self.assertIsNotNone(kwargs.get("timeout"), argv)
            if "push" in argv:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return real(argv, *args, **kwargs)

        with patch.object(gitbus.subprocess, "run", side_effect=run):
            with self.assertRaises(BusPushError) as caught:
                self.post(self.clone)
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue((self.clone / ".agents" / "inbox" / ENVELOPE).exists())


    def test_a_commit_that_fails_is_an_error(self):
        # Review #163 n15: a host without a git identity wrote the file and exited 0.
        git(self.clone, "config", "--unset", "user.email")
        git(self.clone, "config", "--unset", "user.name")
        git(self.clone, "config", "user.useConfigOnly", "true")
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with patch.dict(os.environ, env):
            with self.assertRaises(BusPushError) as caught:
                self.post(self.clone)
        self.assertIn("not committed", str(caught.exception))

if __name__ == "__main__":
    unittest.main()


class TestFollowUps(unittest.TestCase):
    """Review P1-3 follow-ups n5, n1 and n4."""

    def test_telegram_post_reports_a_failed_push_cleanly(self):
        from swarmly.telegram import cli
        args = argparse.Namespace(no_telegram=True, to="Codex", text="hi", intent="query",
                                  telegram_type=None)
        config = Mock()
        failing = AsyncMock(side_effect=BusPushError("push rejected"))
        stderr = io.StringIO()
        with patch.object(cli, "AgentChannel"), \
                patch("swarmly.telegram.relay.post_and_notify", failing), \
                contextlib.redirect_stderr(stderr):
            code = asyncio.run(cli._post(config, args))
        self.assertEqual(code, 2)
        self.assertIn("Not published: push rejected", stderr.getvalue())

    def test_the_seen_cap_drops_the_first_recorded_ids_whatever_their_names(self):
        # n1 re-review M1: sorting let planted ids such as msg-9999... push real ids out.
        from swarmly.telegram import poll
        ids = ["msg-20261010T000001Z-real", "msg-99991231T235959Z-planted",
               "zzz-planted", "msg-20261010T000002Z-newest"]
        with TemporaryDirectory() as tmp, patch.object(poll, "_SAVE_SEEN_CAP", 2):
            poll._save_seen(Path(tmp), "alice", dict.fromkeys(ids))
            data = json.loads(poll._seen_path(Path(tmp), "alice").read_text(encoding="utf-8"))
        self.assertEqual(data["seen"], ids[2:])

    def test_git_runs_in_the_c_locale(self):
        # n4: the retry and "nothing to commit" checks match git's English messages.
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8"}), \
                patch.object(gitbus.subprocess, "run") as fake:
            GitTransport(Path(tmp))._git("status")
        self.assertEqual(fake.call_args.kwargs["env"]["LC_ALL"], "C")
