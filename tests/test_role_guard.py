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


class TestMergeAttribution(unittest.TestCase):
    """#228: the transport, not the model, says who merged, at which term and at which commit.

    The bots share one GitHub login, so GitHub shows the same ``mergedBy`` for every one of
    them and refuses their formal reviews (403). The comment the guard posts after a merge is
    the attribution: a merge without one is a merge no bot made through the guard.
    """

    def test_the_comment_names_who_the_term_and_the_commit(self):
        from swarmly.telegram.guard import attribution
        self.assertEqual(attribution("DeepSeek", 4, "1a2b3c4"),
                         "Merged by DeepSeek (lease term 4) at 1a2b3c4")

    def test_what_it_could_not_read_it_leaves_out(self):
        from swarmly.telegram.guard import attribution
        self.assertEqual(attribution("DeepSeek", 0, "1a2b3c4"),
                         "Merged by DeepSeek at 1a2b3c4")
        self.assertEqual(attribution("DeepSeek", 4, ""), "Merged by DeepSeek (lease term 4)")
        self.assertEqual(attribution("", 0, ""), "Merged by an unnamed bot")

    def test_the_pull_request_is_read_from_the_merge_arguments(self):
        from swarmly.telegram.guard import _merge_pr
        self.assertEqual(_merge_pr(["pr", "merge", "8", "--squash"]), "8")
        self.assertEqual(_merge_pr(["pr", "merge", "--merge", "8"]), "8")
        self.assertEqual(_merge_pr(["pr", "merge", "8", "--body", "landed"]), "8")
        self.assertEqual(_merge_pr(["pr", "merge", "8", "-t", "a subject"]), "8")
        self.assertEqual(_merge_pr(["pr", "merge", "--auto"]), "")      # the branch's own PR

    def test_a_merge_is_followed_by_one_comment(self):
        from swarmly.telegram.guard import comment_merge
        calls = []

        def run(argv):
            calls.append(list(argv))
            return (0, "9f8e7d6\n") if "view" in argv else (0, "")

        body = comment_merge("/bin/gh", ["pr", "merge", "8", "--squash"],
                             {"SWARMLY_ALIAS": "DeepSeek", "SWARMLY_LEASE_TERM": "4"}, run)
        self.assertEqual(body, "Merged by DeepSeek (lease term 4) at 9f8e7d6")
        self.assertEqual([call[1:3] for call in calls], [["pr", "view"], ["pr", "comment"]])
        self.assertEqual(calls[-1][3:5], ["8", "--body"])
        self.assertEqual(calls[-1][5], body)               # alias, term and SHA, as posted

    def test_an_unguarded_merge_leaves_no_comment(self):
        from swarmly.telegram.guard import comment_merge
        calls = []
        self.assertIsNone(comment_merge("/bin/gh", ["pr", "merge", "8"], {}, calls.append))
        self.assertIsNone(comment_merge("/bin/gh", ["pr", "merge", "--merge"],
                                        {"SWARMLY_ALIAS": "DeepSeek"}, calls.append))
        self.assertEqual(calls, [])                        # the branch's PR: nothing to name

    def test_a_queued_auto_merge_is_not_a_merge(self):
        """`--auto` waits for the checks: it has not landed, so nothing is claimed."""
        from swarmly.telegram.guard import comment_merge
        calls = []
        self.assertIsNone(comment_merge("/bin/gh", ["pr", "merge", "8", "--auto"],
                                        {"SWARMLY_ALIAS": "DeepSeek"}, calls.append))
        self.assertEqual(calls, [])

    def test_a_failed_comment_never_fails_the_merge(self):
        from swarmly.telegram.guard import comment_merge

        def run(argv):
            return (0, "abc123\n") if "view" in argv else (1, "could not comment")

        self.assertIsNone(comment_merge("/bin/gh", ["pr", "merge", "8"],
                                        {"SWARMLY_ALIAS": "DeepSeek"}, run))

    def test_the_environment_carries_who_and_which_term(self):
        from swarmly.telegram.guard import ALIAS_ENV, TERM_ENV, prepare
        with tempfile.TemporaryDirectory() as tmp:
            env = prepare(Path(tmp) / "guard", "coordinator",
                          {"PATH": os.environ.get("PATH", "")}, alias="DeepSeek", term=4)
            if not (env.get("SWARMLY_REAL_GIT") or env.get("SWARMLY_REAL_GH")):
                self.skipTest("neither git nor gh is on PATH: the guard is not installed")
            self.assertEqual(env[ALIAS_ENV], "DeepSeek")
            self.assertEqual(env[TERM_ENV], "4")
            # The adapter hands the turn's own lease term through, not a constant.
            from swarmly.telegram.adapters.claude import ClaudeAdapter
            from swarmly.telegram.config import AdapterSettings
            adapter = ClaudeAdapter("DeepSeek", AdapterSettings(type="claude"))
            adapter.role_name, adapter.guard_dir = "developer", Path(tmp) / "g2"
            adapter.acting_coordinator, adapter.lease_term = True, 7
            seen = adapter.environment()
            if seen.get("SWARMLY_REAL_GH"):
                self.assertEqual(seen[ALIAS_ENV], "DeepSeek")
                self.assertEqual(seen[TERM_ENV], "7")


@unittest.skipIf(os.name == "nt", "the POSIX wrapper is tested here")
class TestTheMergeThroughTheGuardedPath(unittest.TestCase):
    """End to end: `gh pr merge` run through the guard's wrapper is followed by the comment."""

    def test_merge_then_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            log = Path(tmp) / "calls.log"
            fake = bin_dir / "gh"
            fake.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$GH_LOG"\n'
                'case "$1 $2" in "pr view") echo 1a2b3c4 ;; esac\n'
                "exit 0\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = prepare(Path(tmp) / "guard", "coordinator",
                          {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                          alias="DeepSeek", term=4)
            # The merge gate (#247) reads the PR first; this test is about the attribution.
            env = {**env, "GH_LOG": str(log), "SWARMLY_MERGE_GATE": "off"}
            done = subprocess.run(["gh", "pr", "merge", "8", "--squash"], env=env,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls[0], "pr merge 8 --squash")
            self.assertTrue(calls[1].startswith("pr view 8 "),
                            calls[1])                            # the commit it landed at
            self.assertEqual(calls[2], "pr comment 8 --body Merged by DeepSeek "
                                       "(lease term 4) at 1a2b3c4")


@unittest.skipIf(os.name == "nt", "the POSIX wrapper is tested here")
class TestTheMergeGateThroughTheGuard(unittest.TestCase):
    """#247 review: the guard really runs the gate before a merge, gate on."""

    HEAD = "0636b79aa1b2c3d4e5f60718293a4b5c6d7e8f90"

    def merge(self, *args, comments=()):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            view = Path(tmp) / "view.json"
            view.write_text(json.dumps({
                "headRefOid": self.HEAD, "comments": [{"body": c} for c in comments],
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
                "commits": [{"authors": [{"name": "Codex"}]}]}))
            fake = bin_dir / "gh"
            fake.write_text(
                "#!/bin/sh\n"
                'case "$*" in *statusCheckRollup*) cat "$GATE_VIEW"; exit 0 ;; esac\n'
                'case "$1 $2" in "pr merge") echo merged ;; "pr view") echo 1a2b3c4 ;; esac\n'
                "exit 0\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = prepare(Path(tmp) / "guard", "coordinator",
                          {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                          alias="Claudebot", term=4)
            env = {**env, "GATE_VIEW": str(view)}
            return subprocess.run(["gh", "pr", "merge", *args], env=env,
                                  capture_output=True, text=True)

    def test_no_verdict_is_refused(self):
        done = self.merge("8")
        self.assertEqual(done.returncode, REFUSED, done.stdout)
        self.assertIn("a reviewer's merge verdict", done.stderr)

    def test_a_merge_without_a_number_is_still_gated(self):
        done = self.merge("--squash")
        self.assertEqual(done.returncode, REFUSED, done.stdout)
        self.assertNotIn("merged", done.stdout)

    def test_the_developers_own_verdict_is_refused(self):
        done = self.merge("8", comments=["0636b79 Verdict: merge. QA: pass.\n— Codex"])
        self.assertEqual(done.returncode, REFUSED)
        self.assertIn("own authors", done.stderr)

    def test_a_signed_reviewer_verdict_merges(self):
        done = self.merge("8", comments=["0636b79 Verdict: merge. QA: pass.\n— Reasonix"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("merged", done.stdout)


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
            if not (env.get("SWARMLY_REAL_GIT") or env.get("SWARMLY_REAL_GH")):
                self.skipTest("neither git nor gh is on PATH: the guard is not installed")
            self.assertEqual(env["SWARMLY_ROLE"], "coordinator")
            adapter.acting_coordinator = False          # the lease is gone: the role is back
            self.assertEqual(adapter.environment()["SWARMLY_ROLE"], "reviewer")


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

    def test_the_read_keeps_the_term_for_the_merge_comment(self):
        """#228: the term comes out of the one read the guard already takes, not a second one."""
        from swarmly.telegram.lease import LeaseState
        channel = self.channel()
        own = channel.config.identity.alias
        channel.lease_state = lambda now=None: LeaseState(
            status="ok", term=11, holder=own, expires_at=channel._lease_clock() + 600.0)
        self.assertTrue(channel._holds_lease())
        self.assertEqual(channel._turn_term(), 11)
        self.assertEqual(self.channel()._turn_term(), 0)     # nothing read yet: no term


class TestTheTermReachesTheAdapter(unittest.TestCase):
    """#228: who merged is only accountable if the merge says at which term it happened."""

    def test_each_turn_hands_its_term_to_the_adapter(self):
        import asyncio
        from types import SimpleNamespace
        from swarmly.telegram.adapters.base import AdapterReply
        from tests.test_telegram_channel import event, make_channel, make_config

        channel = make_channel(make_config())
        seen = []

        class Adapter:
            acting_coordinator = False
            lease_term = 0

            async def respond(self, incoming):
                seen.append((self.acting_coordinator, self.lease_term))
                return AdapterReply(text="ok")

        adapter = Adapter()
        channel._adapter_for = lambda chat, thread: adapter

        async def holding() -> bool:
            return True
        channel._acting_coordinator = holding
        channel._turn_term = lambda: 11
        incoming = SimpleNamespace(author_alias="admin", context_id="c", text="hi")
        asyncio.run(channel._respond(incoming, event("hi")))
        self.assertEqual(seen, [(True, 11)])         # the term the merge will report
        self.assertEqual(adapter.lease_term, 0)      # and it does not outlive the turn


class TestTheFlagLastsOneTurn(unittest.TestCase):
    """#208 review (DeepSeek): the flag was set at turn start and never cleared, so between
    turns the environment still said coordinator after the lease had moved."""

    def test_the_flag_is_cleared_when_the_turn_ends(self):
        import asyncio
        from types import SimpleNamespace
        from swarmly.telegram.adapters.base import AdapterReply
        from tests.test_telegram_channel import event, make_channel, make_config

        channel = make_channel(make_config())
        seen = []

        class Adapter:
            acting_coordinator = False

            async def respond(self, incoming):
                seen.append(self.acting_coordinator)
                return AdapterReply(text="ok")

        adapter = Adapter()
        channel._adapter_for = lambda chat, thread: adapter

        async def holding() -> bool:            # the turn runs with the coordinator's rights
            return True
        channel._acting_coordinator = holding
        incoming = SimpleNamespace(author_alias="admin", context_id="c", text="hi")
        asyncio.run(channel._respond(incoming, event("hi")))
        self.assertEqual(seen, [True])
        self.assertFalse(adapter.acting_coordinator)


class TestTheReviewOnTwoThreeTwo(unittest.TestCase):
    """#232 (review of #229): the merge attribution has to hold where the command shape varies.

    Three things a reviewer found: the identity came from the caller's environment (so
    ``SWARMLY_ALIAS=someone-else gh pr merge`` posted a comment naming another bot), a
    ``--match-head-commit`` SHA was read as the PR number, and ``gh -R owner/repo pr merge 5``
    was not recognised as a merge at all.
    """

    def test_who_it_speaks_for_is_baked_into_the_wrapper(self):
        from swarmly.telegram.guard import ALIAS_ENV, TERM_ENV
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "guard"
            env = prepare(folder, "coordinator", {"PATH": os.environ.get("PATH", "")},
                          alias="DeepSeek", term=4)
            if not env.get("SWARMLY_REAL_GH"):
                self.skipTest("gh is not on PATH: the wrapper is not installed")
            sh = (folder / "gh").read_text(encoding="utf-8")
            self.assertIn(f"{ALIAS_ENV}=DeepSeek", sh)
            self.assertIn(f"{TERM_ENV}=4", sh)
            # Windows gets the same, or the fix would hold on one OS only.
            cmd = (folder / "gh.cmd").read_text(encoding="utf-8")
            self.assertIn(f'set "{ALIAS_ENV}=DeepSeek"', cmd)
            self.assertIn(f'set "{TERM_ENV}=4"', cmd)

    def test_a_later_prepare_does_not_erase_the_baked_term(self):
        """#245 review: a /model-time `prepare()` rewrote the wrapper at term 0.

        That took the term away from a merge already in flight, and its attribution comment
        lost the term it had earned. The term is only ever raised, never reset by a caller who
        does not know it.
        """
        from swarmly.telegram.guard import TERM_ENV
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "guard"
            env = prepare(folder, "developer", {"PATH": os.environ.get("PATH", "")},
                          alias="DeepSeek", term=4)
            if not env.get("SWARMLY_REAL_GH"):
                self.skipTest("gh is not on PATH: the wrapper is not installed")
            again = prepare(folder, "developer", {"PATH": os.environ.get("PATH", "")},
                            alias="DeepSeek", term=0)
            self.assertIn(f"{TERM_ENV}=4", (folder / "gh").read_text(encoding="utf-8"))
            self.assertIn(f'set "{TERM_ENV}=4"',
                          (folder / "gh.cmd").read_text(encoding="utf-8"))
            self.assertEqual(again[TERM_ENV], "4")      # the environment too, not just the file
            # ... and a real term still wins over the baked one.
            newer = prepare(folder, "developer", {"PATH": os.environ.get("PATH", "")},
                            alias="DeepSeek", term=5)
            self.assertIn(f"{TERM_ENV}=5", (folder / "gh").read_text(encoding="utf-8"))
            self.assertEqual(newer[TERM_ENV], "5")

    def test_the_cmd_wrapper_quotes_the_assignment_and_doubles_percent(self):
        """#245 review: `set VAR="x"` keeps the quotes in the value; `set "VAR=x"` does not.

        And a `.cmd` line is expanded before `set` runs, so `%` doubles.
        """
        from swarmly.telegram.guard import _cmd_quote, _gh_wrapper_cmd
        self.assertEqual(_cmd_quote("a%b"), "a%%b")
        line = _gh_wrapper_cmd("run", "a%b", 2)
        self.assertIn('set "SWARMLY_ALIAS=a%%b"', line)
        self.assertNotIn('="a%%b"', line)
        self.assertIn('set "SWARMLY_LEASE_TERM=2"', line)

    def test_an_alias_with_a_space_stays_one_shell_word(self):
        from swarmly.telegram.guard import _gh_wrapper, _sh_quote
        self.assertEqual(_sh_quote("DeepSeek"), "DeepSeek")
        self.assertEqual(_sh_quote("two words"), "'two words'")
        self.assertIn("SWARMLY_ALIAS='two words'",
                      _gh_wrapper('"/usr/bin/python" -I -S "guard.py"', "two words", 3))

    def test_a_match_head_commit_sha_is_not_the_pull_request(self):
        from swarmly.telegram.guard import _merge_pr
        self.assertEqual(_merge_pr(["pr", "merge", "--squash",
                                    "--match-head-commit", "1a2b3c4", "8"]), "8")
        self.assertEqual(_merge_pr(["pr", "merge", "8", "--match-head-commit", "1a2b3c4"]), "8")

    def test_a_merge_behind_global_options_is_still_a_merge(self):
        from swarmly.telegram.guard import refusal, split_gh
        self.assertEqual(split_gh(["-R", "owner/repo", "pr", "merge", "5"])[1],
                         ["pr", "merge", "5"])
        self.assertIn("does not merge",
                      refusal("developer", "gh", ["-R", "owner/repo", "pr", "merge", "5"]))
        self.assertIn("does not merge", refusal("developer", "gh", ["--repo=o/r", "pr", "merge"]))
        self.assertIsNone(refusal("coordinator", "gh", ["-R", "owner/repo", "pr", "merge", "5"]))

    def test_the_comment_follows_a_merge_behind_global_options(self):
        """#245 review (blocker): the `-R owner/repo` prefix must reach the calls the comment
        makes, or the SHA and the comment name this checkout's repository."""
        from swarmly.telegram.guard import comment_merge, split_gh
        calls = []

        def run(argv):
            calls.append(list(argv))
            return (0, "9f8e7d6\n") if "view" in argv else (0, "")

        prefix, tail = split_gh(["-R", "other/repo", "pr", "merge", "5"])
        self.assertEqual(prefix, ["-R", "other/repo"])
        # #245 review: drive the split honestly -- assert what it *left* as well as what it
        # took, or a split that kept its prefix in the tail would still pass this test.
        self.assertEqual(tail, ["pr", "merge", "5"])
        body = comment_merge("/bin/gh", tail, {"SWARMLY_ALIAS": "DeepSeek",
                                               "SWARMLY_LEASE_TERM": "4"}, run,
                             global_options=prefix)
        self.assertEqual(body, "Merged by DeepSeek (lease term 4) at 9f8e7d6")
        self.assertEqual(calls[0][1:5], ["-R", "other/repo", "pr", "view"])
        self.assertEqual(calls[1][1:7], ["-R", "other/repo", "pr", "comment", "5", "--body"])


if __name__ == "__main__":
    unittest.main()
