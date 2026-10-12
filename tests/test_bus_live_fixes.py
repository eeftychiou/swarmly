"""Durable-bus fixes from the first live test on Windows: the running transport polled the
bus only after its first interval (so the seen-ledger baseline swallowed mail sent since
start), and an agent's answer to bus mail was never posted to Telegram by a live transport."""

import asyncio
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.poll import bus_poll_once
from tests.test_bus_poll import bus_message as _bus_message, make_config


def bus_message(*args):
    """A message sent now, after the baseline (older ones count as history)."""
    import datetime
    import dataclasses
    message = _bus_message(*args)
    return dataclasses.replace(message, timestamp_utc=datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds"))


def channel_for(tmp, messages, sender=None):
    """A channel whose first (baseline) cycle has already run on an empty bus, so
    *messages* arrive as new mail."""
    cfg = make_config(bus={"poll_minutes": 10, "repo": tmp})
    bus: list = []
    channel = AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=sender,
                           gitbus_pull=lambda _root: None,
                           gitbus_read=lambda _root: [(m, True, "ok") for m in bus])
    asyncio.run(bus_poll_once(channel))
    bus.extend(messages)
    return channel


class TestReplyEcho(unittest.TestCase):
    def test_a_live_transport_posts_the_agents_reply(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [bus_message("m1", "codex", "hermes", "ping")])
            channel.bot = object()                       # the transport is up
            channel.send = mock.AsyncMock()
            outcome = asyncio.run(bus_poll_once(channel))
        self.assertEqual(outcome.delivered, ["m1"])
        echoed = [c.args[0] for c in channel.send.await_args_list]
        self.assertTrue(any(text.startswith("[gitbus ← codex]") for text in echoed), echoed)
        self.assertEqual(outcome.replies_unposted, 0)
        self.assertEqual(outcome.replies_posted, 1)

    def test_no_extra_receipt_when_the_reply_was_posted(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [bus_message("m1", "codex", "hermes", "ping")])
            channel.bot = object()
            channel.send = mock.AsyncMock()
            delivered = asyncio.run(channel.check_gitbus())
        self.assertEqual(delivered, ["m1"])
        texts = [c.args[0] for c in channel.send.await_args_list]
        self.assertFalse(any("fallback delivered" in t for t in texts), texts)

    def test_an_unposted_delivery_is_logged_not_posted(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [bus_message("m1", "codex", "hermes", "ping")])
            channel.send = mock.AsyncMock()
            delivered = asyncio.run(channel.check_gitbus())
        self.assertEqual(delivered, ["m1"])
        texts = [c.args[0] for c in channel.send.await_args_list]
        self.assertFalse(any("fallback delivered" in t for t in texts), texts)

    def test_without_a_transport_the_reply_is_counted_and_logged(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [bus_message("m1", "codex", "hermes", "ping")])
            with self.assertLogs("swarmly.telegram.poll", "WARNING") as logs:
                outcome = asyncio.run(bus_poll_once(channel))
        self.assertEqual(outcome.delivered, ["m1"])
        self.assertEqual(outcome.replies_unposted, 1)
        self.assertTrue(any("was not posted" in line for line in logs.output))


class TestBusTickPollsFirst(unittest.TestCase):
    def test_the_first_poll_runs_before_the_first_wait(self):
        events = []

        async def sleeper(_seconds):
            events.append("sleep")
            raise asyncio.CancelledError

        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [])
            channel._sleep = sleeper

            async def check():
                events.append("poll")
                return []
            channel.check_gitbus = check
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(channel._bus_tick())
        self.assertEqual(events, ["poll", "sleep"])


if __name__ == "__main__":
    unittest.main()
