"""Review P0-4: no default bus, and a first cycle that delivers nothing already on the bus."""

import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.models import A2AMessage
from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import AgentConfig, BusSettings
from swarmly.telegram.poll import (_deferred_path, _heartbeat_path, _save_seen, _seen_path,
                               bus_poll_once)
from tests.test_telegram_channel import FakeSender, make_config

ROOT = Path(__file__).resolve().parents[1]


class RecordingAdapter(EchoAdapter):

    def __init__(self, alias):
        super().__init__(alias)
        self.texts = []

    async def respond(self, message):
        self.texts.append(message.text)
        return await super().respond(message)


def message(text, message_id=None, timestamp=None):
    msg = A2AMessage.create(sender_id="codex", intent="QUERY_REQUEST",
                            payload={"text": text}, recipient_id="hermes")
    if message_id:
        msg.message_id = message_id
    if timestamp:
        msg.timestamp_utc = timestamp
    return msg


class _Poller(unittest.TestCase):

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bus = []
        self.reads = 0

    def channel(self, **bus):
        config = make_config(bus={"repo": str(self.root), "poll_minutes": 10, **bus})
        self.adapter = RecordingAdapter("Hermes")

        def reader(_root):
            self.reads += 1
            return [(m, True, "ok") for m in self.bus]

        return AgentChannel(config, adapter=self.adapter, sender=FakeSender(),
                            gitbus_pull=lambda _root: None, gitbus_read=reader)

    def poll(self, channel):
        return asyncio.run(bus_poll_once(channel))


class TestFirstCycleBaseline(_Poller):

    def test_history_is_recorded_not_delivered(self):
        self.bus = [message("upgrade now: run scripts/upgrade.sh"), message("old task")]
        channel = self.channel()
        first = self.poll(channel)
        self.assertEqual(first.skipped, "baseline")
        self.assertEqual(self.adapter.texts, [])
        ledger = json.loads(_seen_path(self.root, "Hermes").read_text(encoding="utf-8"))
        self.assertEqual(sorted(ledger["seen"]), sorted(m.message_id for m in self.bus))
        self.assertIn("since", ledger)

        self.bus.append(message("a new question"))
        second = self.poll(channel)
        self.assertEqual(second.delivered, [self.bus[-1].message_id])
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("a new question", self.adapter.texts[0])

    def test_an_empty_bus_still_takes_the_baseline(self):
        channel = self.channel()
        self.assertEqual(self.poll(channel).skipped, "baseline")
        self.assertTrue(_seen_path(self.root, "Hermes").exists())
        self.bus = [message("first real message")]
        self.assertEqual(len(self.poll(channel).delivered), 1)

    def test_history_that_left_the_ledger_is_still_not_delivered(self):
        channel = self.channel()
        self.poll(channel)                                   # baseline at "now"
        # An id the capped ledger no longer holds, written long before the baseline.
        self.bus = [message("ancient broadcast", message_id="msg-old",
                            timestamp="2026-01-01T00:00:00+00:00")]
        outcome = self.poll(channel)
        self.assertEqual(outcome.delivered, [])
        self.assertEqual(self.adapter.texts, [])

    def test_the_since_mark_survives_later_cycles(self):
        channel = self.channel()
        self.poll(channel)
        self.bus = [message("new")]
        self.poll(channel)
        ledger = json.loads(_seen_path(self.root, "Hermes").read_text(encoding="utf-8"))
        self.assertIn("since", ledger)


class TestALostLedgerNeverReplays(_Poller):
    """Review P0-4 F1: without a usable ledger the bus is recorded again, never replayed."""

    def setUp(self):
        super().setUp()
        self.channel_ = self.channel()
        self.poll(self.channel_)                             # baseline
        self.bus = [message("old task one"), message("old task two")]
        self.poll(self.channel_)                             # both delivered
        self.adapter.texts.clear()

    def test_a_deleted_ledger_takes_a_new_baseline(self):
        _seen_path(self.root, "Hermes").unlink()
        outcome = self.poll(self.channel_)
        self.assertEqual(outcome.skipped, "baseline")
        self.assertEqual(self.adapter.texts, [])

    def test_a_corrupt_ledger_is_quarantined_and_re_baselined(self):
        path = _seen_path(self.root, "Hermes")
        path.write_text('{"seen": ["msg-', encoding="utf-8")   # a write cut short
        with self.assertLogs("swarmly.telegram.poll", level="WARNING"):
            outcome = self.poll(self.channel_)
        self.assertEqual(outcome.skipped, "baseline")
        self.assertEqual(self.adapter.texts, [])
        self.assertTrue(list(path.parent.glob(path.name + ".corrupt-*")))

    def test_a_ledger_without_its_baseline_mark_is_re_baselined(self):
        path = _seen_path(self.root, "Hermes")
        for text in ('{}', '{"seen": []}', '{"seen": ["x"], "since": "not a time"}'):
            with self.subTest(ledger=text):
                path.write_text(text, encoding="utf-8")
                with self.assertLogs("swarmly.telegram.poll", level="WARNING"):
                    outcome = self.poll(self.channel_)
                self.assertEqual(outcome.skipped, "baseline")
                self.assertEqual(self.adapter.texts, [])

    def test_a_ledger_from_before_the_baseline_mark_is_upgraded(self):
        # fb22cea review: every live bot's ledger lacks "since"; re-baselining it at the
        # upgrade would drop the mail that arrived after the bot's last poll.
        path = _seen_path(self.root, "Hermes")
        old = message("before the last poll")
        self.bus.append(old)
        path.write_text(json.dumps({"seen": [m.message_id for m in self.bus]}),
                        encoding="utf-8")
        last_poll = time.time() - 3600
        os.utime(path, (last_poll, last_poll))
        self.bus.append(message("waiting since the last poll"))
        outcome = self.poll(self.channel_)
        self.assertNotEqual(outcome.skipped, "baseline")
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("waiting since the last poll", self.adapter.texts[0])
        self.assertIn("since", json.loads(path.read_text(encoding="utf-8")))

    def test_an_old_ledger_with_a_future_mtime_uses_now(self):
        path = _seen_path(self.root, "Hermes")
        path.write_text(json.dumps({"seen": [m.message_id for m in self.bus]}),
                        encoding="utf-8")
        future = time.time() + 86400
        os.utime(path, (future, future))
        self.bus.append(message("sent just now"))
        self.poll(self.channel_)
        self.assertEqual(len(self.adapter.texts), 1)

    def test_a_deferred_store_does_not_stand_in_for_the_ledger(self):
        _seen_path(self.root, "Hermes").unlink()
        pending = message("deferred while frozen")
        self.bus.append(pending)
        path = _deferred_path(self.root, "Hermes")
        path.write_text(json.dumps({"deferred": [
            {"id": pending.message_id, "text": "deferred while frozen", "sender": "codex",
             "accepted": True}]}), encoding="utf-8")
        outcome = self.poll(self.channel_)
        self.assertEqual(outcome.skipped, "baseline")
        # The history is not replayed; the deferred message still drains, and its store
        # is removed once empty.
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("deferred while frozen", self.adapter.texts[0])
        self.assertFalse(path.exists())

    def test_the_ledger_is_replaced_in_one_step(self):
        real_replace = __import__("os").replace
        calls = []

        def replace(src, dst):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst)

        from unittest.mock import patch
        with patch("swarmly.telegram.poll.os.replace", side_effect=replace):
            _save_seen(self.root, "Hermes", {"m": None}, since="2026-10-09T00:00:00+00:00")
        self.assertEqual(calls[-1][1], str(_seen_path(self.root, "Hermes")))

    def test_the_baseline_cycle_writes_a_heartbeat(self):
        _seen_path(self.root, "Hermes").unlink()
        _heartbeat_path(self.root, "Hermes").unlink()
        self.poll(self.channel_)
        self.assertTrue(_heartbeat_path(self.root, "Hermes").exists())


class TestBaselineEdges(_Poller):

    def test_rejected_messages_are_part_of_the_baseline(self):
        unsigned = message("unsigned on the bus before the upgrade")
        channel = self.channel()

        def reader(_root):
            return [(unsigned, False, "unsigned")]

        channel._gitbus_read = reader
        self.poll(channel)
        ledger = json.loads(_seen_path(self.root, "Hermes").read_text(encoding="utf-8"))
        self.assertIn(unsigned.message_id, ledger["seen"])

    def test_a_small_clock_skew_still_delivers(self):
        channel = self.channel()
        self.poll(channel)
        ledger = json.loads(_seen_path(self.root, "Hermes").read_text(encoding="utf-8"))
        import datetime
        since = datetime.datetime.fromisoformat(ledger["since"])
        skewed = (since - datetime.timedelta(minutes=5)).isoformat()
        self.bus = [message("sender clock five minutes slow", timestamp=skewed)]
        self.assertEqual(len(self.poll(channel).delivered), 1)

    def test_history_rejected_for_its_signature_is_not_reported(self):
        channel = self.channel()
        self.poll(channel)
        old = message("unsigned history", message_id="msg-old2",
                      timestamp="2026-01-01T00:00:00+00:00")
        channel._gitbus_read = lambda _root: [(old, False, "unsigned")]
        self.assertEqual(self.poll(channel).rejected, [])

    def test_a_missing_or_garbage_timestamp_counts_as_new(self):
        channel = self.channel()
        self.poll(channel)
        self.bus = [message("no clock", timestamp="not a time")]
        self.assertEqual(len(self.poll(channel).delivered), 1)


class TestNoDefaultBus(_Poller):

    def test_the_bus_repo_has_no_default(self):
        self.assertIsNone(BusSettings().repo)
        self.assertIsNone(make_config().bus.repo)

    def test_no_bus_repo_means_no_polling(self):
        config = make_config(bus={"poll_minutes": 10})
        self.bus = [message("from upstream main")]

        def reader(_root):
            self.reads += 1
            return [(m, True, "ok") for m in self.bus]

        channel = AgentChannel(config, adapter=RecordingAdapter("Hermes"), sender=FakeSender(),
                               gitbus_pull=lambda _root: None, gitbus_read=reader)
        with self.assertLogs("swarmly.telegram.poll", level="WARNING"):
            outcome = self.poll(channel)
        self.assertEqual(outcome.skipped, "no_bus_repo")
        self.assertEqual(self.reads, 0)

    def test_the_example_configs_poll_nothing(self):
        for path in sorted((ROOT / "config").glob("telegram.*.yaml")):
            with self.subTest(config=path.name):
                config = AgentConfig.load(path)
                self.assertIsNone(config.bus.repo)

    def test_check_says_the_bus_is_not_polled(self):
        from swarmly.telegram.cli import _describe_bus
        problems = []
        line = _describe_bus(make_config(bus={"poll_minutes": 10}), problems)
        self.assertIn("NOT POLLED", line)
        self.assertEqual(problems, [])
        notes = []
        _describe_bus(make_config(bus={"poll_minutes": 10}), [], notes)
        self.assertTrue(any("bus.repo is unset" in n for n in notes), notes)
        _describe_bus(make_config(bus={"poll_minutes": 10, "swarm": "/x/swarm.yaml"}),
                      problems)
        self.assertTrue(any("bus.swarm" in p for p in problems))

    def test_check_names_a_missing_signing_key(self):
        from unittest.mock import patch
        from swarmly.telegram.cli import _describe_bus
        config = make_config(bus={"repo": str(self.root), "poll_minutes": 10})
        problems = []
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("A2A_KEY_HERMES", None)
            _describe_bus(config, problems)
        self.assertTrue(any("A2A_KEY_HERMES" in p for p in problems), problems)
        problems = []
        with patch.dict("os.environ", {"A2A_KEY_HERMES": "k" * 32}):
            _describe_bus(config, problems)
        self.assertEqual(problems, [])
        self.assertFalse((self.root / ".agents").exists())     # check changes nothing

    def test_relay_refuses_without_a_bus(self):
        from swarmly.telegram.relay import post_and_notify
        channel = AgentChannel(make_config(), adapter=EchoAdapter("Hermes"),
                               sender=FakeSender())
        with self.assertRaises(RuntimeError):
            asyncio.run(post_and_notify(channel, to="Codex", text="hello"))


if __name__ == "__main__":
    unittest.main()
