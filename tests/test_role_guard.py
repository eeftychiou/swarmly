"""guard.py: gh and git wrappers refuse what an agent's role rules out."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import TEST_IDENTITY, git_env
from swarmly.telegram.guard import REFUSED, prepare, refusal


class TestRefusal(unittest.TestCase):

    def test_push_refusal_reads_the_hook_lines(self):
        from swarmly.telegram.guard import push_refusal
        line = "refs/heads/x 1111 refs/heads/{} 0000"
        self.assertIn("push a branch", push_refusal("developer", [line.format("main")]))
        self.assertIsNone(push_refusal("developer", [line.format("issue-7")]))
        self.assertIn("does not push", push_refusal("reviewer", [line.format("fix")]))
        self.assertIsNone(push_refusal("coordinator", [line.format("main")]))

    def test_only_the_coordinator_merges(self):
        self.assertIsNone(refusal("coordinator", "gh", ["pr", "merge", "8", "--squash"]))
        for role in ("developer", "reviewer", "qa-tester", "researcher"):
            self.assertIn("does not merge", refusal(role, "gh", ["pr", "merge", "8"]))
        self.assertIn("does not merge",
                      refusal("developer", "git", ["merge", "feature"], current_branch="main"))
        self.assertIsNone(refusal("developer", "git", ["merge", "main"], current_branch="f"))

    def test_pushes(self):
        self.assertIsNone(refusal("developer", "git", ["push", "-u", "origin", "issue-7"]))
        self.assertIn("push a branch", refusal("developer", "git", ["push", "origin", "main"]))
        self.assertIn("push a branch",
                      refusal("developer", "git", ["push", "origin", "HEAD:refs/heads/main"]))
        self.assertIn("push a branch",
                      refusal("qa-tester", "git", ["push"], current_branch="master"))
        self.assertIn("does not push", refusal("reviewer", "git", ["push", "origin", "fix"]))
        self.assertIsNone(refusal("coordinator", "git", ["push", "origin", "main"]))
        self.assertIn("push a branch",
                      refusal("developer", "git", ["-C", "repo", "push", "origin", "main"]))
        self.assertIsNone(refusal("developer", "git", ["-C", "push", "status"]))

    def test_everything_else_runs(self):
        self.assertIsNone(refusal("reviewer", "gh", ["pr", "review", "8", "--approve"]))
        self.assertIsNone(refusal("developer", "gh", ["pr", "create", "--fill"]))
        self.assertIn("pull requests", refusal("researcher", "gh", ["pr", "create"]))
        self.assertIsNone(refusal("", "gh", ["pr", "merge", "8"]))          # no role, no guard


#: The identity and the isolated global config the guard's own git commands run with.
GIT_ENV = dict(TEST_IDENTITY, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


@unittest.skipIf(os.name == "nt", "the POSIX wrapper is tested here")
class TestWrapper(unittest.TestCase):

    def test_the_gh_wrapper_refuses_and_passes_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "gh"
            fake.write_text("#!/bin/sh\necho real gh \"$@\"\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = prepare(Path(tmp) / "guard", "developer",
                          {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
            self.assertTrue(env["PATH"].startswith(str(Path(tmp) / "guard")))
            self.assertFalse((Path(tmp) / "guard" / "git").exists())
            ok = subprocess.run(["gh", "pr", "list"], env=env, capture_output=True, text=True)
            self.assertEqual(ok.stdout.strip(), "real gh pr list")
            refused = subprocess.run(["gh", "pr", "merge", "8"], env=env, capture_output=True,
                                     text=True)
            self.assertEqual(refused.returncode, REFUSED)
            self.assertIn("coordinator merges it", refused.stderr)

    def _repos(self, tmp, role):
        def git(cwd, *args, env=None):
            # The scrubber runs unconditionally; a caller's pinned guard environment is
            # layered on top of it. ``env or git_env(...)`` would skip the scrub whenever
            # the caller's env is truthy, which is what the CI guard now rejects.
            child_env = {**git_env(**GIT_ENV), **(env or {})}
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                  text=True, env=child_env)
        origin, work = Path(tmp) / "origin.git", Path(tmp) / "work"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        git(tmp, "clone", "-q", str(origin), str(work))
        git(work, "checkout", "-q", "-b", "main")
        (work / "a").write_text("a")
        git(work, "add", "a")
        git(work, "commit", "-qm", "a")
        env = prepare(Path(tmp) / "guard", role, git_env(**GIT_ENV))
        return work, env, git

    def test_the_pre_push_hook_holds_whichever_git_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, env, git = self._repos(tmp, "developer")
            refused = git(work, "push", "origin", "main", env=env)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("swarmly guard: refused the push", refused.stderr)
            git(work, "checkout", "-q", "-b", "issue-7")
            ok = git(work, "push", "-q", "origin", "issue-7", env=env)
            self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_the_repositorys_own_hooks_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, env, git = self._repos(tmp, "coordinator")
            hook = work / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\necho repo hook ran >&2\nexit 1\n")
            hook.chmod(0o755)
            (work / "b").write_text("b")
            git(work, "add", "b")
            done = git(work, "commit", "-qm", "b", env=env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("repo hook ran", done.stderr)
            ok = git(work, "push", "-q", "origin", "main", env=env)
            self.assertEqual(ok.returncode, 0, ok.stderr)


class TestAdapterEnvironment(unittest.TestCase):

    def test_a_role_puts_the_guard_on_path(self):
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        from swarmly.telegram.config import AdapterSettings
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter("Claude", AdapterSettings(type="claude"))
            self.assertNotIn("SWARMLY_ROLE", adapter.environment())
            adapter.role_name, adapter.guard_dir = "reviewer", Path(tmp) / "g"
            env = adapter.environment()
            if env.get("SWARMLY_REAL_GIT") or env.get("SWARMLY_REAL_GH"):
                self.assertEqual(env["SWARMLY_ROLE"], "reviewer")
                self.assertTrue(env["PATH"].startswith(str(Path(tmp) / "g")))

    def test_the_lease_holder_is_guarded_as_the_coordinator(self):
        """#143 drill: a reviewer that took the lease over must be able to merge the round."""
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        from swarmly.telegram.config import AdapterSettings
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter("Claude", AdapterSettings(type="claude"))
            adapter.role_name, adapter.guard_dir = "reviewer", Path(tmp) / "g"
            adapter.acting_coordinator = True
            env = adapter.environment()
            if env.get("SWARMLY_REAL_GIT") or env.get("SWARMLY_REAL_GH"):
                self.assertEqual(env["SWARMLY_ROLE"], "coordinator")
            adapter.acting_coordinator = False          # the lease is gone: the role is back
            env = adapter.environment()
            if env.get("SWARMLY_REAL_GIT") or env.get("SWARMLY_REAL_GH"):
                self.assertEqual(env["SWARMLY_ROLE"], "reviewer")


class TestHoldsLease(unittest.TestCase):
    """The channel tells the adapter, each turn, whether this bot holds the lease."""

    def channel(self):
        from tests.test_telegram_channel import make_channel, make_config
        return make_channel(make_config())

    def test_only_a_live_lease_of_its_own_counts(self):
        from swarmly.telegram.lease import LeaseState
        channel = self.channel()
        own = channel.config.identity.alias
        far = channel._lease_clock() + 600.0
        channel.lease_state = lambda now=None: LeaseState(
            status="ok", term=11, holder=own, expires_at=far)
        self.assertTrue(channel._holds_lease())
        channel.lease_state = lambda now=None: LeaseState(
            status="ok", term=11, holder="Claudebot", expires_at=far)
        self.assertFalse(channel._holds_lease())
        channel.lease_state = lambda now=None: LeaseState(
            status="ok", term=11, holder=own, expires_at=channel._lease_clock() - 1.0)
        self.assertFalse(channel._holds_lease())         # lapsed: no coordinator rights

    def test_an_unreadable_lease_grants_nothing(self):
        channel = self.channel()

        def boom(*args, **kwargs):
            raise RuntimeError("the bus is gone")

        channel.lease_state = boom
        self.assertFalse(channel._holds_lease())


if __name__ == "__main__":
    unittest.main()
