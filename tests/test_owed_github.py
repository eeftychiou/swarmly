"""Owed rows for items GitHub says are merged or closed are closed (not only round items)."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from swarmly.github.client import GitHubClient
from swarmly.telegram.owed import Row
from tests.test_telegram_channel import make_channel, make_config


class OwedGitHubTests(unittest.TestCase):
    def test_merged_and_closed_items_close_their_rows(self):
        channel = make_channel(make_config(projects={"p": {"repo": "o/r"}}))
        rows = [Row(kind="merge", item="#230", head="474778a", debtor="Reasonix", owes="a merge",
                    writer="Hermes"),
                Row(kind="progress", item="#167", debtor="DeepSeek", owes="progress", writer="Hermes"),
                Row(kind="merge", item="#300", head="abcdef1", debtor="Reasonix", owes="a merge",
                    writer="Hermes"),
                Row(kind="merge", item="#231", head="34a288f", debtor="Reasonix", owes="a merge",
                    writer="Codex")]
        saved = []
        channel.owed_store = lambda: SimpleNamespace(load=lambda: rows,
                                                     save_all=lambda r: saved.extend(r))
        states = {230: "merged", 167: "closed", 300: "open", 231: "merged"}
        with mock.patch.object(GitHubClient, "ref_state",
                               lambda self, repo, number: states[number]):
            closed = asyncio.run(channel.owed_github_settle())
        self.assertEqual(closed, 2)
        self.assertEqual(sorted(r.item for r in saved), ["#167", "#230"])   # its own rows
        self.assertTrue(rows[2].is_open)

    def test_an_unknown_state_closes_nothing(self):
        channel = make_channel(make_config(projects={"p": {"repo": "o/r"}}))
        rows = [Row(kind="merge", item="#230", head="474778a", debtor="Reasonix", owes="a merge",
                    writer="Hermes")]
        channel.owed_store = lambda: SimpleNamespace(load=lambda: rows, save_all=lambda r: None)
        with mock.patch.object(GitHubClient, "ref_state", lambda self, repo, number: None):
            self.assertEqual(asyncio.run(channel.owed_github_settle()), 0)
        self.assertTrue(rows[0].is_open)


if __name__ == "__main__":
    unittest.main()
