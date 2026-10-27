"""Issue #1, item 4: ``audit-public`` finds Telegram identifiers in a public repository.

The scanner is the redactor read backwards: the transport knows which strings must not leave
the host (:mod:`swarmly.telegram.redact`), and this asks whether any of them is already out
there -- in every ref of a mirror clone (``refs/pull/*`` included) and in the issue/PR text.

Every handle, chat id and path in this file is **fake**: this repository is public, and a test
for a leak must not be one.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from swarmly.telegram import audit as au
from swarmly.telegram.cli import _audit_public
from swarmly.telegram.redact import CHAT, HANDLE, PATH, Redactor
from tests.gitenv import git_env, git_out
from tests.test_redact import ADMIN_ID, GROUP, SecretSandbox, config

#: A bot handle that is not anybody's (fake, like every other value here).
HANDLE_TEXT = "@example_dd_bot"
#: A path that must never be public.
LOCAL_PATH = r"C:\work\example-client"


def cfg(**overrides):
    return config(public_redactions={HANDLE_TEXT: "<bot>"}, **overrides)


def git(root, *args):
    """One scrubbed ``git`` call (``tests.gitenv``): no runner's guard or identity leaks in."""
    return git_out(root, *args)


def commit(root, message, files):
    """One commit in *root*: *files* is ``{path: text}``."""
    for name, text in files.items():
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)      # gitenv supplies the identity
    return git(root, "rev-parse", "HEAD")


class Repo:
    """A scratch repository, and a mirror clone of it the way the audit makes one."""

    def __init__(self, tmp):
        self.root = Path(tmp) / "src"
        self.mirror = Path(tmp) / "mirror.git"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")

    def commit(self, message, files):
        return commit(self.root, message, files)

    def publish(self):
        """The mirror clone: every ref, ``refs/pull/*`` included (as ``--mirror`` fetches)."""
        done = subprocess.run(["git", "clone", "--mirror", "--quiet", str(self.root),
                               str(self.mirror)], env=git_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
        return self.mirror


class NeedleTests(SecretSandbox):
    """Which strings the audit looks for: the redactor's own set, plus the two backstops."""

    def labels(self, needles, label):
        return [needle.value for needle in needles if needle.label == label]

    def test_the_maintainer_handle_chat_ids_and_paths_are_needles(self):
        from swarmly.telegram.config import AgentConfig
        made = AgentConfig.from_dict({
            "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "1:T",
                      "home_channel_id": GROUP, "admin_user_id": ADMIN_ID,
                      "maintainer_username": "@example_admin"},
            "adapter": {"type": "echo", "cwd": LOCAL_PATH},
        })
        needles = au.needles_from(made)
        self.assertIn("@example_admin", self.labels(needles, HANDLE))
        self.assertIn(str(GROUP), self.labels(needles, CHAT))
        self.assertIn(str(abs(GROUP)), self.labels(needles, CHAT))
        self.assertIn(LOCAL_PATH, self.labels(needles, PATH))
        self.assertIn(str(ADMIN_ID), [n.value for n in needles])

    def test_a_public_redaction_is_a_needle_under_its_placeholder(self):
        needles = au.needles_from(cfg())
        self.assertIn(HANDLE_TEXT, self.labels(needles, "<bot>"))

    def test_scan_text_names_the_label_and_redacts_the_excerpt(self):
        redactor = Redactor.from_config(cfg())
        found = au.scan_text(f"the bot is {HANDLE_TEXT} today", au.needles_from(cfg()), redactor)
        self.assertEqual([label for label, _ in found], ["<bot>"])
        self.assertNotIn(HANDLE_TEXT, found[0][1])       # the report is not a copy of the leak
        self.assertIn("<bot>", found[0][1])

    def test_the_backstop_patterns_find_an_id_no_config_names(self):
        found = au.scan_text("chat -1009998887770 and C:\\Users\\someone\\work", [], None)
        self.assertEqual(sorted(label for label, _ in found), [CHAT, PATH])


class RefTests(SecretSandbox):
    """Every ref, not only the default branch: a leak on a pull-request head is public too."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(tmp.name)

    def audit(self, **kwargs):
        return au.audit("example-org/example", au.needles_from(cfg()),
                        clone=self.repo.publish(), github=False,
                        redactor=Redactor.from_config(cfg()), **kwargs)

    def test_a_clean_repository_is_clean(self):
        self.repo.commit("first", {"README.md": "# example\nnothing to see\n"})
        report = self.audit()
        self.assertEqual(report.hits, [])
        self.assertEqual(report.status, au.EXIT_CLEAN)
        self.assertEqual(report.problems, [])

    def test_a_file_on_main_names_the_handle_it_is_not_supposed_to_carry(self):
        self.repo.commit("first", {"notes.md": f"ask {HANDLE_TEXT} about it\n"})
        report = self.audit()
        self.assertEqual(report.status, au.EXIT_HITS)
        self.assertEqual([hit.needle for hit in report.hits], ["<bot>"])
        self.assertEqual(report.hits[0].where, "notes.md")
        self.assertNotIn(HANDLE_TEXT, report.hits[0].excerpt)

    def test_a_file_only_a_pull_request_head_carries_is_scanned(self):
        base = self.repo.commit("first", {"README.md": "# example\n"})
        git(self.repo.root, "checkout", "-q", "-b", "pr-head")
        self.repo.commit("a pr", {"bus/envelope.json": f'{{"chat": "{GROUP}"}}\n'})
        git(self.repo.root, "update-ref", "refs/pull/7/head", "HEAD")
        git(self.repo.root, "checkout", "-q", "main")
        git(self.repo.root, "update-ref", "-d", "refs/heads/pr-head")
        self.assertEqual(git(self.repo.root, "rev-parse", "HEAD").strip(), base)
        mirror = self.repo.publish()
        self.assertIn("refs/pull/7/head", au.refs_of(mirror))
        report = au.audit("example-org/example", au.needles_from(cfg()), clone=mirror,
                          github=False, redactor=Redactor.from_config(cfg()))
        self.assertEqual(report.status, au.EXIT_HITS)
        self.assertEqual([hit.where for hit in report.hits], ["bus/envelope.json"])
        self.assertEqual([hit.needle for hit in report.hits], [CHAT])

    def test_a_commit_message_is_public_too(self):
        self.repo.commit(f"ping {HANDLE_TEXT} for the key", {"README.md": "# example\n"})
        report = self.audit()
        self.assertEqual(report.status, au.EXIT_HITS)
        self.assertEqual(len(report.hits), 1)
        self.assertIn("commit ", report.hits[0].where)
        self.assertIn("message", report.hits[0].where)

    def test_the_report_says_how_much_was_read(self):
        self.repo.commit("first", {"README.md": "# example\n"})
        report = self.audit()
        self.assertEqual(report.commits, 1)
        self.assertEqual(report.blobs, 1)
        self.assertIn("refs/heads/main", report.refs)
        self.assertIn("no hit", report.summary())


class GithubTests(SecretSandbox):
    """Bodies, comments and review comments: the three places issue text lives."""

    def reader(self, pages):
        def fetch(path):
            self.assertIn(path, pages, path)
            return pages[path]
        return fetch

    def test_an_issue_body_a_comment_and_a_review_comment_are_all_scanned(self):
        pages = {
            "repos/example-org/example/issues?state=all": [
                {"number": 3, "title": "a title", "body": f"from {HANDLE_TEXT}"}],
            "repos/example-org/example/issues/comments": [
                {"id": 91, "body": "nothing here"}],
            "repos/example-org/example/pulls/comments": [
                {"id": 92, "path": "swarmly/x.py", "body": f"see {LOCAL_PATH}"}],
        }
        made = cfg(adapter={"type": "echo", "cwd": LOCAL_PATH})
        items, hits, problems = au.scan_github("example-org/example", au.needles_from(made),
                                              Redactor.from_config(made),
                                              fetch=self.reader(pages))
        self.assertEqual(items, 3)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(hit.needle for hit in hits), ["<bot>", PATH])
        self.assertIn("issue #3", hits[0].where + hits[1].where)
        self.assertNotIn(HANDLE_TEXT, " ".join(hit.excerpt for hit in hits))

    def test_a_github_that_cannot_be_read_is_not_a_clean_result(self):
        def broken(path):
            raise au.AuditError(f"gh api {path} failed: not logged in")
        _, hits, problems = au.scan_github("example-org/example", au.needles_from(cfg()),
                                          None, fetch=broken)
        self.assertEqual(hits, [])
        self.assertEqual(len(problems), 3)                 # one per source, not one total
        report = au.Report(repo="example-org/example", problems=problems)
        self.assertEqual(report.status, au.EXIT_BROKEN)


class CommandTests(SecretSandbox):
    """The command itself: what an operator's exit status means."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(tmp.name)

    def run_command(self, **kwargs):
        args = Namespace(repo="example-org/example", clone=self.repo.publish(), no_github=True,
                         no_fetch=True, keep=False, json=False)
        for key, value in kwargs.items():
            setattr(args, key, value)
        return _audit_public(cfg(), args)

    def test_a_clean_repository_exits_zero(self):
        self.repo.commit("first", {"README.md": "# example\n"})
        self.assertEqual(self.run_command(), 0)

    def test_a_hit_exits_one_and_says_not_to_publish(self):
        self.repo.commit("first", {"notes.md": f"{HANDLE_TEXT}\n"})
        with self.assertNoLogs("swarmly.telegram.audit", "WARNING"):
            self.assertEqual(self.run_command(), 1)

    def test_a_scan_that_could_not_happen_exits_two(self):
        """A clone that cannot be made is "not scanned", never "clean"."""
        self.repo.commit("first", {"README.md": "# example"})
        blocked = Path(self.repo.root).parent / "not-a-clone"
        blocked.mkdir()
        (blocked / "a-file").write_text("in the way", encoding="utf-8")
        args = Namespace(repo="example-org/example", clone=blocked, no_github=True,
                         no_fetch=True, keep=False, json=False)
        self.assertEqual(_audit_public(cfg(), args), 2)

    def test_the_json_report_carries_no_raw_value(self):
        import contextlib
        import io
        self.repo.commit("first", {"notes.md": f"{HANDLE_TEXT}\n"})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.run_command(json=True)
        printed = buffer.getvalue()
        data = json.loads(printed)
        self.assertFalse(data["clean"])
        self.assertEqual(data["hits"][0]["needle"], "<bot>")
        self.assertNotIn(HANDLE_TEXT, printed)

