"""A turn the transport starts (a CI result, a GitHub event) takes the normal turn path.

Live, 24 Oct 01:11: a CI wake reached the coordinator as a bare 618-character message with
none of the round's context -- no recap, no frozen-peer note -- so a fresh session asked a
paused bot for QA the round had given to a stand-in, and the round sat for eight hours.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import owed as ow
from swarmly.telegram.adapters.base import AdapterReply
from swarmly.telegram.adapters import EchoAdapter
from tests.test_telegram_channel import GROUP, make_channel, make_config, run


class Recording(EchoAdapter):
    def __init__(self, reply):
        super().__init__("Hermes")
        self.reply = reply
        self.prompts = []

    async def respond(self, message):
        self.prompts.append(message.text or "")
        return AdapterReply(text=self.reply)


class TransportTurnTests(unittest.TestCase):

    def channel(self, reply):
        adapter = Recording(reply)
        channel = make_channel(make_config(), adapter=adapter)
        channel.bot = object()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ow.OwedStore("Hermes", state_dir=Path(directory.name), clock=lambda: 1000.0)
        channel.owed_store = lambda: store
        channel._test_store = store
        channel.topics.remember(GROUP, 9400, "Stability round")
        channel._peers_frozen["Antigravity"] = {"ref": "pause", "until": None}
        return channel, adapter

    def test_a_ci_wake_carries_the_frozen_peers_and_its_reply_is_processed(self):
        channel, adapter = self.channel("#245 is ready.\nWAIT ci #245 head 0636b79")
        result = run(channel.run_transport_turn(
            "CI finished on owner/repo#245: all 7 check(s) passed.", GROUP, 9400))
        self.assertTrue(result.ok)
        self.assertEqual(len(adapter.prompts), 1)
        prompt = adapter.prompts[0]
        self.assertIn("CI finished on owner/repo#245", prompt)
        self.assertIn("Frozen peers take no work", prompt)       # the note a chat turn gets
        self.assertIn("Antigravity", prompt)
        posted = " ".join(str(sent.get("text", "")) for sent in channel._test_sender.sent)
        self.assertIn("#245 is ready.", posted)
        self.assertNotIn("WAIT ci #245 head", posted)             # the line became a row
        self.assertIn("recorded", posted)
        self.assertEqual([row.kind for row in channel._test_store.load()], ["ci"])


if __name__ == "__main__":
    unittest.main()
