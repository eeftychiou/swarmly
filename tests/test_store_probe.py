"""/storetest (topic registry, phase 0): the probe that checks the Telegram store's facts."""

import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from swarmly.telegram import store_probe
from swarmly.telegram.store_probe import StoreProbe
from swarmly.telegram.wire import parse_message

CHAT = -100123
ORIGINAL_START = StoreProbe.start
PULSE = 3418


class FakeGroup:
    """One supergroup: global message ids, delivery to every other bot, the newest pin."""

    def __init__(self):
        self.next_id = 1000
        self.channels = []
        self.pins = []                     # message ids, in pin order
        self.files = {}                    # message id -> bytes
        self.blind = set()                 # (reader, sender) pairs that are not delivered
        self.send_fails = set()            # aliases whose sends raise
        self.fail_once = set()             # aliases whose next send raises, once

    def mint(self):
        self.next_id += 1
        return self.next_id

    def deliver(self, sender, mid, text):
        for channel in self.channels:
            if channel.alias == sender or (channel.alias, sender) in self.blind:
                continue
            event = SimpleNamespace(text=text, message_id=mid, author=sender)
            envelope, _ = parse_message(text)
            channel.store_probe.observe(envelope, event)


class FakeBot:
    def __init__(self, group, channel):
        self.group, self.channel = group, channel

    async def send_document(self, chat_id, message_thread_id, document, caption,
                            disable_notification):
        mid = self.group.mint()
        self.group.files[mid] = document.data
        return SimpleNamespace(message_id=mid)

    async def pin_chat_message(self, chat_id, message_id, disable_notification):
        self.group.pins.append(message_id)

    async def unpin_chat_message(self, chat_id, message_id):
        self.group.pins.remove(message_id)

    async def get_chat(self, chat_id):
        if not self.group.pins:
            return SimpleNamespace(pinned_message=None)
        mid = max(self.group.pins)         # Telegram: the most recently *sent* pinned message
        document = (SimpleNamespace(file_id=f"f{mid}") if mid in self.group.files else None)
        return SimpleNamespace(pinned_message=SimpleNamespace(message_id=mid, document=document))

    async def download(self, file_id, destination):
        destination.write(self.group.files[int(file_id[1:])])
        return destination


class FakeChannel:
    def __init__(self, group, alias, everyone):
        self.group, self.alias = group, alias
        self.config = SimpleNamespace(identity=SimpleNamespace(alias=alias,
                                                               home_channel_id=CHAT))
        self.selftest = SimpleNamespace(peers=lambda: [a for a in everyone if a != alias])
        self.bot = FakeBot(group, self)
        self.reports = []
        self.store_probe = StoreProbe(self)
        group.channels.append(self)

    async def _ensure_pulse_topic(self):
        return PULSE

    def _alias_for(self, event):
        return event.author

    async def send(self, text, chat_id=None, thread_id=None, return_message_id=False, **_):
        if self.alias in self.group.send_fails and thread_id == PULSE:
            raise RuntimeError("Flood control exceeded. Retry in 7 seconds")
        if self.alias in self.group.fail_once and thread_id == PULSE                 and "step=line" in text:
            self.group.fail_once.discard(self.alias)
            raise RuntimeError("Flood control exceeded. Retry in 7 seconds")
        await asyncio.sleep(0)
        if thread_id != PULSE:
            self.reports.append(text)
            return None
        mid = self.group.mint()
        self.group.deliver(self.alias, mid, text)
        return mid


def swarm(*aliases):
    group = FakeGroup()
    return group, [FakeChannel(group, alias, aliases) for alias in aliases]


def run_test(group, lead, dashboard=True):
    if dashboard:
        group.pins.append(group.mint())    # the dashboard, pinned long ago

    async def go():
        with mock.patch.object(store_probe, "PIN_AFTER_SECONDS", 0.05), \
                mock.patch.object(store_probe, "PIN_WAIT_SECONDS", 1.0), \
                mock.patch.object(store_probe, "RESULTS_WAIT_SECONDS", 1.0), \
                mock.patch.object(store_probe, "SNAPSHOT_BYTES", 2048), \
                mock.patch.object(StoreProbe, "_write", lambda *a: None):
            run = lead.store_probe.start(origin=(CHAT, 77))
            for _ in range(200):
                await asyncio.sleep(0.02)
                if lead.reports:
                    break
            return run
    run = asyncio.run(go())
    return run, lead.store_probe.runs[run]


class StoreProbeTests(unittest.TestCase):
    def test_a_healthy_swarm_passes(self):
        group, (claude, deepseek, reasonix) = swarm("Claudebot", "DeepSeek", "Reasonix")
        run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertTrue(report["ok"], report["summary"])
        self.assertEqual(sorted(state.results), ["Claudebot", "DeepSeek", "Reasonix"])
        self.assertIn("PASS: 3/3 bots reported", claude.reports[0])
        self.assertEqual(len(group.pins), 1, "the probe file is unpinned again")

    def test_a_bot_that_cannot_see_a_peer_fails_the_run(self):
        group, (claude, deepseek, reasonix) = swarm("Claudebot", "DeepSeek", "Reasonix")
        group.blind.add(("Reasonix", "DeepSeek"))
        _run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertFalse(report["ok"])
        self.assertIn("Reasonix saw 0/4 of DeepSeek's lines", report["summary"])

    def test_a_newer_pin_hides_the_file(self):
        """getChat returns only the newest pin: a dashboard re-sent after the file wins."""
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        original = FakeBot.pin_chat_message

        async def pin_then_dashboard(self, chat_id, message_id, disable_notification):
            await original(self, chat_id, message_id, disable_notification)
            self.group.pins.append(self.group.mint())       # a dashboard re-sent and pinned

        with mock.patch.object(FakeBot, "pin_chat_message", pin_then_dashboard):
            _run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertFalse(report["ok"])
        self.assertIn("newest pin is", report["summary"])

    def test_a_silent_peer_is_named(self):
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        group.channels.remove(deepseek)    # an old release: it never answers
        _run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing"], ["DeepSeek"])

    def test_a_bot_left_out_is_not_expected(self):  # noqa: D102
        group, (claude, deepseek, reasonix) = swarm("Claudebot", "DeepSeek", "Reasonix")
        group.channels.remove(reasonix)    # it does not take part
        claude.store_probe.skipped = set()
        original = StoreProbe.start

        def start(self, origin=None, skip=()):
            return original(self, origin, skip=("Reasonix",))

        with mock.patch.object(StoreProbe, "start", start):
            _run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertTrue(report["ok"], report["summary"])
        self.assertIn("left out: reasonix", report["summary"])

    def test_send_errors_are_reported(self):
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        group.fail_once.add("DeepSeek")    # one burst line hits a flood wait, the rest go
        _run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing"], [])
        self.assertTrue(any("DeepSeek: send error" in p and "Flood control" in p
                            for p in report["problems"]), report["problems"])

    def test_a_bot_left_out_takes_no_part(self):
        group, (claude, deepseek, reasonix) = swarm("Claudebot", "DeepSeek", "Reasonix")
        sent = []
        original = FakeChannel.send

        async def counting(self, text, **kwargs):
            if self.alias == "Reasonix":
                sent.append(text)
            return await original(self, text, **kwargs)

        with mock.patch.object(FakeChannel, "send", counting),                 mock.patch.object(StoreProbe, "start",
                                  lambda self, origin=None, skip=(): ORIGINAL_START(
                                      self, origin, skip=("reasonix",))):
            _run, state = run_test(group, claude)
        self.assertEqual(sent, [], "a bot left out (any case) posts nothing")
        self.assertTrue(claude.store_probe.verdict(state)["ok"])

    def test_a_line_with_two_ids_and_ids_out_of_send_order_fail(self):
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        _run, state = run_test(group, claude)
        state.results["DeepSeek"]["saw"]["Claudebot"]["1"] += 1
        report = claude.store_probe.verdict(state)
        self.assertTrue(any("line 1 has different ids" in p for p in report["problems"]))
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        _run, state = run_test(group, claude)
        saw = state.results["DeepSeek"]["saw"]["Claudebot"]
        saw["1"], saw["2"] = saw["2"], saw["1"]
        for reader in state.results.values():
            reader["saw"]["Claudebot"] = dict(saw)
        report = claude.store_probe.verdict(state)
        self.assertTrue(any("do not rise in send order" in p for p in report["problems"]))

    def test_a_corrupt_download_fails(self):
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        original = FakeBot.download

        async def corrupt(self, file_id, destination):
            await original(self, file_id, destination)
            destination.write(b"garbage")
            return destination

        with mock.patch.object(FakeBot, "download", corrupt):
            _run, state = run_test(group, claude)
        report = claude.store_probe.verdict(state)
        self.assertTrue(any("download sha" in p for p in report["problems"]), report)

    def test_late_arrivals_are_reported_but_allowed(self):
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")
        _run, state = run_test(group, claude)
        arrival = state.results["DeepSeek"]["arrival"]
        state.results["DeepSeek"]["arrival"] = list(reversed(arrival))
        report = claude.store_probe.verdict(state)
        self.assertTrue(report["ok"], report["summary"])
        self.assertIn("arrived behind a later id", report["summary"])

    def test_a_lead_that_fails_still_reports(self):
        group, (claude, deepseek) = swarm("Claudebot", "DeepSeek")

        async def broken(self, *args, **kwargs):
            raise RuntimeError("not enough rights to pin")

        with mock.patch.object(FakeBot, "pin_chat_message", broken):
            run_test(group, claude)
        self.assertTrue(claude.reports and "FAIL: the lead could not finish" in claude.reports[0],
                        claude.reports)

    def test_lines_without_a_known_run_are_ignored(self):
        group, (claude,) = swarm("Claudebot")
        event = SimpleNamespace(text="[A2A v1.1] NOTE to=* intent=PROBE ref=x\n\nstep=line n=1",
                                message_id=5, author="DeepSeek")
        claude.store_probe.observe(parse_message(event.text)[0], event)
        self.assertEqual(claude.store_probe.runs, {})


if __name__ == "__main__":
    unittest.main()
