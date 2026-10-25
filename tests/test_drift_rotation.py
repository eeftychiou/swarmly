"""Drift v2 (topic registry, phase 8): a drift reset is a handover rotation."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram import drift as dr
from swarmly.telegram.adapters.base import AdapterReply, IncomingMessage
from swarmly.telegram.adapters.claude import ClaudeThreadAdapter
from swarmly.telegram.adapters.handover import (HANDOVER_PROMPT, NO_HANDOVER_LEAD, TAKEOVER_LEAD,
                                                 Handovers)
from tests.test_channel_rules import topic_message
from tests.test_claude_adapter import FakeProcess, ScriptedRunner, result_json
from tests.test_telegram_channel import GROUP, make_channel, make_config, run
from tests.test_transport_enforcement import Clock, Recording

HANDOVER = "1. Goal and scope: ship #12.\n2. Decisions: none.\n3. State: PR #40 open.\n" * 3


class HandoverLeadTests(unittest.TestCase):
    def test_a_prefix_goes_ahead_of_the_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            handovers = Handovers("Claude")
            handovers.attach(Path(tmp) / "claude-sessions-claude.json")
            handovers.save("tg-7", HANDOVER, prefix="[RULES you broke]")
            lead = handovers.lead("tg-7")
            self.assertTrue(lead.startswith("[RULES you broke]"))
            self.assertIn(TAKEOVER_LEAD.strip()[:40], lead)
            self.assertIn("ship #12", lead)
            again = Handovers("Claude")                         # survives a restart
            again.attach(Path(tmp) / "claude-sessions-claude.json")
            self.assertEqual(again.lead("tg-7"), lead)

    def test_no_usable_handover_still_carries_the_prefix(self):
        handovers = Handovers("Claude")
        handovers.save("tg-7", "ok", prefix="[RULES]")
        lead = handovers.lead("tg-7")
        self.assertTrue(lead.startswith("[RULES]"))
        self.assertIn(NO_HANDOVER_LEAD.strip()[:40], lead)


class ClaudeRotationTests(unittest.TestCase):
    def test_the_old_session_writes_the_handover_and_the_new_one_starts_from_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ScriptedRunner(FakeProcess(result_json()),
                                    FakeProcess(result_json(HANDOVER)),
                                    FakeProcess(result_json()))
            claude = ClaudeThreadAdapter("Claude", ClaudeThreadAdapter.default_settings(),
                                         runner=runner)
            claude.use_state_file(Path(tmp) / "claude-sessions-claude.json")
            message = topic_message()
            asyncio.run(claude.respond(message))
            old = claude.topic_session(message.context_id)
            self.assertTrue(asyncio.run(claude.rotate(message.context_id, message,
                                                      prefix="[RULES you broke]")))
            handover_call = runner.calls[1]
            self.assertIn("--resume", handover_call)
            self.assertIn(old, handover_call)
            self.assertIn(HANDOVER_PROMPT[:40], handover_call[-1])
            new = claude.topic_session(message.context_id)
            self.assertNotEqual(old, new)
            asyncio.run(claude.respond(message))
            first = runner.calls[2]
            self.assertEqual(first[first.index("--session-id") + 1], new)
            self.assertIn("[RULES you broke]", first[-1])
            self.assertIn("ship #12", first[-1])
            self.assertFalse(claude._handovers.pending(message.context_id), "used once")


class ClaudeTurnLimitTests(unittest.TestCase):
    def test_the_session_hands_over_at_its_turn_limit(self):
        runner = ScriptedRunner(FakeProcess(result_json()), FakeProcess(result_json()),
                                FakeProcess(result_json(HANDOVER)), FakeProcess(result_json()))
        settings = ClaudeThreadAdapter.default_settings()
        settings.session_turn_limit = 2
        claude = ClaudeThreadAdapter("Claude", settings, runner=runner)
        message = topic_message()
        first = claude.topic_session(message.context_id)

        async def go():
            await claude.respond(message)
            reply = await claude.respond(message)
            self.assertTrue(reply.meta.get("rotated"))
            await claude.respond(message)       # queued behind the rotation: the new session
            return reply
        asyncio.run(go())
        self.assertIn(HANDOVER_PROMPT[:40], runner.calls[2][-1])
        self.assertNotEqual(claude.topic_session(message.context_id), first)
        third = runner.calls[3]
        self.assertEqual(third[third.index("--session-id") + 1],
                         claude.topic_session(message.context_id))
        self.assertIn("ship #12", third[-1])


class TopicLessViolationTests(unittest.TestCase):
    def test_a_guard_violation_with_no_topic_ages_out(self):
        """Review: with no window, one guard row reminded every topic for ever."""
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = dr.DriftLedger(Path(tmp) / "drift.json", clock=clock)
            ledger.record("merge_gate_refused")                 # the guard: no topic
            self.assertTrue(ledger.decide("-100:7").remind)
            clock.now += dr.WINDOW_SECONDS + 1
            self.assertFalse(ledger.decide("-100:7").remind)
            ledger.record("duplicate_reply", topic="-100:7")
            clock.now += 30 * 86400
            self.assertEqual(ledger.decide("-100:7").score, 1)   # a topic's row stays


class RotatingAdapter(Recording):
    def __init__(self):
        super().__init__("ok")
        self.rotations = []

    async def rotate(self, context_id, message, prefix=""):
        self.rotations.append((context_id, prefix))
        return True


class ChannelDriftResetTests(unittest.TestCase):
    def test_a_reset_rotates_with_the_broken_rules_first(self):
        adapter = RotatingAdapter()
        channel = make_channel(make_config(), adapter=adapter)
        with tempfile.TemporaryDirectory() as tmp:
            channel.drift = dr.DriftLedger(Path(tmp) / "drift.json", clock=Clock())

            async def go():
                channel.record_violation("addressed_frozen_peer", "to Codex", GROUP, 9400)
                channel.record_violation("merge_gate_refused", "no QA", GROUP, 9400)
                for task in list(channel._self_tasks):
                    await task
            run(go())
        self.assertEqual(len(adapter.rotations), 1)
        context_id, prefix = adapter.rotations[0]
        self.assertEqual(context_id, "tg-9400")
        self.assertIn("drift reset", prefix)
        self.assertIn("Never hand work to a peer that is paused", prefix)
        self.assertIn("handover", channel._test_sender.last)

    def test_an_adapter_without_rotation_just_forgets(self):
        adapter = Recording("ok")
        channel = make_channel(make_config(), adapter=adapter)
        with tempfile.TemporaryDirectory() as tmp:
            channel.drift = dr.DriftLedger(Path(tmp) / "drift.json", clock=Clock())

            async def go():
                channel.record_violation("addressed_frozen_peer", "x", GROUP, 9400)
                channel.record_violation("merge_gate_refused", "y", GROUP, 9400)
                for task in list(channel._self_tasks):
                    await task
            run(go())
        self.assertEqual(adapter.forgotten, ["tg-9400"])


if __name__ == "__main__":
    unittest.main()
