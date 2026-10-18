"""Tests for durable GitHub client (swarmly/github/client.py)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from swarmly.github.client import GitHubError, GitHubClient, Issue, IssueComment, Notification, PR


class TestGitHubClient(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GitHubClient(token_env="TEST_GH_TOKEN", alias="Antigravity")

    def test_client_issue_view_parses_json(self):
        fake_data = {
            "number": 42,
            "title": "Bug in parser",
            "body": "Detailed description",
            "author": {"login": "octocat"},
            "state": "OPEN",
            "labels": [{"name": "bug"}, {"name": "priority"}],
            "assignees": [{"login": "alice"}],
            "comments": [
                {
                    "author": {"login": "bob"},
                    "body": "Looking into this",
                    "createdAt": "2026-10-10T12:00:00Z",
                }
            ],
        }
        with patch.object(self.client, "_run", return_value=(0, json.dumps(fake_data), "")):
            issue = self.client.issue_view("example-org/webapp", 42)
            self.assertIsNotNone(issue)
            assert issue is not None
            self.assertEqual(issue.number, 42)
            self.assertEqual(issue.title, "Bug in parser")
            self.assertEqual(issue.body, "Detailed description")
            self.assertEqual(issue.author, "octocat")
            self.assertEqual(issue.state, "OPEN")
            self.assertEqual(issue.labels, ["bug", "priority"])
            self.assertEqual(issue.assignees, ["alice"])
            self.assertEqual(len(issue.comments), 1)
            self.assertEqual(issue.comments[0].author, "bob")
            self.assertEqual(issue.comments[0].body, "Looking into this")
            self.assertEqual(issue.comments[0].created_at, "2026-10-10T12:00:00Z")

    def test_client_pr_view_parses_json(self):
        fake_data = {
            "number": 174,
            "title": "feat: per-topic routing",
            "body": "PR description",
            "author": {"login": "antigravity"},
            "state": "MERGED",
            "labels": [{"name": "enhancement"}],
            "reviewRequests": [{"login": "codex"}],
            "headRefName": "agents/antigravity/routing",
            "mergeable": "MERGEABLE",
        }
        with patch.object(self.client, "_run", return_value=(0, json.dumps(fake_data), "")):
            pr = self.client.pr_view("eeftychiou/swarmly", 174)
            self.assertIsNotNone(pr)
            assert pr is not None
            self.assertEqual(pr.number, 174)
            self.assertEqual(pr.title, "feat: per-topic routing")
            self.assertEqual(pr.body, "PR description")
            self.assertEqual(pr.author, "antigravity")
            self.assertEqual(pr.state, "MERGED")
            self.assertEqual(pr.labels, ["enhancement"])
            self.assertEqual(pr.reviewers, ["codex"])
            self.assertEqual(pr.head_ref, "agents/antigravity/routing")
            self.assertEqual(pr.mergeable, "MERGEABLE")

    def test_client_notifications_parses_json(self):
        fake_data = [
            {
                "id": "notif-101",
                "reason": "mention",
                "repository": {"full_name": "example-org/webapp"},
                "subject": {
                    "title": "Please check line 50",
                    "type": "Issue",
                    "url": "https://api.github.com/repos/example-org/webapp/issues/12",
                },
                "updated_at": "2026-10-10T14:30:00Z",
                "unread": True,
            },
            {
                "id": "notif-102",
                "reason": "review_requested",
                "repository": {"full_name": "example-org/shop"},
                "subject": {
                    "title": "New shipping widget",
                    "type": "PullRequest",
                    "url": "https://api.github.com/repos/example-org/shop/pulls/21",
                },
                "updated_at": "2026-10-10T14:35:00Z",
                "unread": False,
            },
        ]
        with patch.object(self.client, "_run", return_value=(0, json.dumps(fake_data), "")):
            notifs = self.client.notifications(repo="example-org/webapp")
            self.assertEqual(len(notifs), 2)
            n1 = notifs[0]
            self.assertEqual(n1.id, "notif-101")
            self.assertEqual(n1.reason, "mention")
            self.assertEqual(n1.repo, "example-org/webapp")
            self.assertEqual(n1.subject_title, "Please check line 50")
            self.assertEqual(n1.subject_type, "Issue")
            self.assertEqual(n1.number, 12)
            self.assertEqual(n1.updated_at, "2026-10-10T14:30:00Z")
            self.assertTrue(n1.unread)

            n2 = notifs[1]
            self.assertEqual(n2.id, "notif-102")
            self.assertEqual(n2.reason, "review_requested")
            self.assertEqual(n2.repo, "example-org/shop")
            self.assertEqual(n2.subject_title, "New shipping widget")
            self.assertEqual(n2.subject_type, "PullRequest")
            self.assertEqual(n2.number, 21)
            self.assertEqual(n2.updated_at, "2026-10-10T14:35:00Z")
            self.assertFalse(n2.unread)

    def test_client_comment_appends_alias_signature(self):
        with patch.object(self.client, "_run", return_value=(0, "", "")) as mock_run:
            ok = self.client.comment("example-org/webapp", 12, "LGTM")
            self.assertTrue(ok)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[:4], ["issue", "comment", "12", "--repo"])
            self.assertEqual(args[4], "example-org/webapp")
            self.assertIn("LGTM\n\n— Antigravity", args[6])

    def test_client_comment_does_not_duplicate_existing_signature(self):
        with patch.object(self.client, "_run", return_value=(0, "", "")) as mock_run:
            ok = self.client.comment("example-org/webapp", 12, "Done.\n\n— Antigravity")
            self.assertTrue(ok)
            args = mock_run.call_args[0][0]
            self.assertEqual(args[6], "Done.\n\n— Antigravity")

    def test_client_create_issue_extracts_number(self):
        with patch.object(
            self.client,
            "_run",
            return_value=(0, "https://github.com/example-org/webapp/issues/55\n", ""),
        ) as mock_run:
            num = self.client.create_issue(
                repo="example-org/webapp",
                title="Add caching",
                body="Details",
                assignees=["alice"],
                labels=["enhancement"],
            )
            self.assertEqual(num, 55)
            args = mock_run.call_args[0][0]
            self.assertIn("--assignee", args)
            self.assertIn("alice", args)
            self.assertIn("--label", args)
            self.assertIn("enhancement", args)
            self.assertEqual(args[args.index("--body") + 1], "Details\n\n— Antigravity")

    def test_client_issues_with_label_lists_open_and_closed(self):
        fake_data = [
            {"number": 34, "title": "Round 2 tracking", "body": "", "author": {"login": "claude"},
             "state": "CLOSED", "labels": [{"name": "tracking"}], "assignees": []},
            {"number": 35, "title": "Next round", "body": "", "author": {"login": "claude"},
             "state": "OPEN", "labels": [{"name": "tracking"}],
             "assignees": [{"login": "codex"}]},
        ]
        with patch.object(self.client, "_run", return_value=(0, json.dumps(fake_data), "")) as run:
            issues = self.client.issues_with_label("example-org/webapp", "tracking")
            args = run.call_args[0][0]
        self.assertEqual([(i.number, i.state) for i in issues], [(34, "CLOSED"), (35, "OPEN")])
        self.assertEqual(issues[1].assignees, ["codex"])
        self.assertEqual(args[:3], ["issue", "list", "--repo"])
        self.assertEqual(args[args.index("--label") + 1], "tracking")

    def test_an_unanswerable_issue_list_is_an_error_not_an_empty_round(self):
        with patch.object(self.client, "_run", return_value=(1, "", "HTTP 401: Bad credentials")):
            with self.assertRaises(GitHubError):
                self.client.issues_with_label("repo", "tracking")

    def test_client_handles_gh_missing(self):
        with patch("shutil.which", return_value=None):
            code, out, err = self.client._run(["issue", "view", "1"])
            self.assertEqual(code, 127)
            self.assertIn("not found on PATH", err)
            self.assertIsNone(self.client.issue_view("repo", 1))
            self.assertIsNone(self.client.pr_view("repo", 1))
            with self.assertRaises(GitHubError):     # failure is not "no notifications"
                self.client.notifications()
            with self.assertRaises(GitHubError):     # nor "no tracking issues"
                self.client.issues_with_label("repo", "tracking")
            self.assertFalse(self.client.comment("repo", 1, "body"))
            self.assertIsNone(self.client.create_issue("repo", "t", "b"))
            self.assertFalse(self.client.mark_notification_read("1"))

    def test_client_handles_nonzero_exit(self):
        with patch.object(self.client, "_run", return_value=(1, "", "Could not resolve to an issue")):
            self.assertIsNone(self.client.issue_view("repo", 999))
            self.assertIsNone(self.client.pr_view("repo", 999))
            with self.assertRaises(GitHubError):     # failure is not "no notifications"
                self.client.notifications()
            with self.assertRaises(GitHubError):
                self.client.issues_with_label("repo", "tracking")
            self.assertFalse(self.client.comment("repo", 999, "hi"))
            self.assertIsNone(self.client.create_issue("repo", "t", "b"))
            self.assertFalse(self.client.mark_notification_read("999"))

    def test_client_handles_invalid_json(self):
        with patch.object(self.client, "_run", return_value=(0, "invalid-json-content", "")):
            self.assertIsNone(self.client.issue_view("repo", 1))
            self.assertIsNone(self.client.pr_view("repo", 1))
            with self.assertRaises(GitHubError):     # failure is not "no notifications"
                self.client.notifications()
            with self.assertRaises(GitHubError):
                self.client.issues_with_label("repo", "tracking")

    def test_client_injects_env_token(self):
        with patch.dict(os.environ, {"TEST_GH_TOKEN": "ghp_secrettoken123"}):
            env = self.client._env()
            self.assertEqual(env.get("GH_TOKEN"), "ghp_secrettoken123")
            self.assertEqual(env.get("NO_COLOR"), "1")

    def test_client_mark_notification_read(self):
        with patch.object(self.client, "_run", return_value=(0, "", "")) as mock_run:
            ok = self.client.mark_notification_read("thread-456")
            self.assertTrue(ok)
            mock_run.assert_called_once_with(["api", "-X", "PATCH", "/notifications/threads/thread-456"])


if __name__ == "__main__":
    unittest.main()
