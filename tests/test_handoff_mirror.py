"""Hand-offs are written to the bus by the transport (agents never did it), and a receiver
runs a mirror only when it missed the Telegram copy."""

import asyncio
import dataclasses
import datetime
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.telegram.poll import bus_poll_once
from tests.test_bus_live_fixes import channel_for
from tests.test_bus_poll import bus_message
from tests.test_telegram_channel import make_channel


def mirror(message_id, ref):
    message = bus_message(message_id, "codex", "hermes", "@HermesBot please QA #8",
                          "TASK_HANDOFF")
    return dataclasses.replace(
        message, payload={"text": "@HermesBot please QA #8", "telegram": ref},
        timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"))


class TestAssignedPeers(unittest.TestCase):

    def test_line_leading_handles_are_assignments(self):
        channel = make_channel()
        text = ("Thanks all. Merge gate:\n- @AntigravityBot comments a merge verdict\n"
                "- @CodexBot runs pytest\nTeam: @CodexBot (QA)")
        self.assertEqual(sorted(channel.assigned_peers(text)), ["Antigravity", "Codex"])
        self.assertEqual(channel.assigned_peers("FYI the team is @CodexBot and @AntigravityBot"),
                         [])
        self.assertEqual(channel.assigned_peers("@CodexBot please QA #8"), ["Codex"])

    def test_a_reply_that_hands_off_is_mirrored(self):
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.adapters.base import AdapterReply

        class Handoff(EchoAdapter):
            async def respond(self, message):
                return AdapterReply(text="@CodexBot please QA PR #8")

        with TemporaryDirectory() as tmp:
            from tests.test_telegram_channel import make_config
            channel = make_channel(make_config(bus={"repo": tmp}), adapter=Handoff("Hermes"))
            channel.bot = object()
            posted = []

            async def fake_post(_channel, **kw):
                posted.append(kw)
            with mock.patch("swarmly.telegram.relay.post_and_notify", fake_post):
                async def scenario():
                    from tests.test_telegram_channel import event
                    await channel.handle_event(event("@HermesBot get #8 tested", author_id=1234567890,
                                                     username="alice", is_bot=False))
                    await asyncio.gather(*list(channel._mirror_tasks))
                asyncio.run(scenario())
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["to"], "Codex")
        self.assertEqual(posted[0]["intent"], "TASK_HANDOFF")
        self.assertIn("telegram", posted[0]["extra"])


class TestReceiverDedupe(unittest.TestCase):

    def test_a_mirror_of_a_handled_message_is_skipped(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [mirror("m1", {"chat": -100, "thread": 7,
                                                      "message_id": 55})])
            channel._tg_admitted.append((-100, 55))
            outcome = asyncio.run(bus_poll_once(channel))
        self.assertEqual(outcome.delivered, [])
        self.assertIn("m1", outcome.machine_events)

    def test_a_missed_message_runs_from_the_bus_and_answers_in_its_topic(self):
        with TemporaryDirectory() as tmp:
            channel = channel_for(tmp, [mirror("m2", {"chat": -100, "thread": 7,
                                                      "message_id": 56})])
            channel.bot = object()
            channel.send = mock.AsyncMock()
            outcome = asyncio.run(bus_poll_once(channel))
        self.assertEqual(outcome.delivered, ["m2"])
        self.assertEqual(channel.send.await_args_list[-1].kwargs.get("thread_id"), 7)


class TestCommitIdentity(unittest.TestCase):

    def test_bus_commits_carry_the_agent_identity(self):
        from pathlib import Path
        from swarmly.transports.git import GitTransport
        transport = GitTransport(Path("."), identity=("DeepSeek", "deepseek@a2a.local"))
        with mock.patch("swarmly.transports.git.subprocess.run") as run:
            transport._git("status")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "deepseek@a2a.local")
        self.assertEqual(env["GIT_COMMITTER_NAME"], "DeepSeek")


if __name__ == "__main__":
    unittest.main()
