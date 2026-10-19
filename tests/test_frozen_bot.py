"""A frozen bot says so, a quota freeze ends by itself, and peers route work around it."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.quota import quota_resets_at, ttl_for
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run

QUOTA_ERROR = "Quota exceeded for this model. Please retry in 2h."
NOW = 1_800_000_000.0


def frozen_channel(adapter=None, detail=QUOTA_ERROR):
    channel = make_channel(make_config(adapter=adapter or {"type": "claude"}))
    wall = {"t": NOW}
    channel._wall = lambda: wall["t"]
    channel._test_wall = wall
    for _ in range(channel.config.limits.quota_freeze_threshold):
        run(channel._note_adapter_failure(detail))
    return channel


def admin_says(channel, text, message_id=10, thread_id=None):
    run(channel.handle_event(event(text, author_id=ADMIN_ID, username="alice", is_bot=False,
                                   message_id=message_id, thread_id=thread_id)))


class TestQuotaFreeze(unittest.TestCase):

    def test_the_freeze_ends_when_the_provider_says(self):
        channel = frozen_channel()
        self.assertTrue(channel._frozen)
        self.assertEqual(channel._freeze_until, NOW + 7200)
        notice = channel._test_sender.last
        self.assertIn("intent=FROZEN ref=quota", notice)
        self.assertIn("ttl=120m", notice)
        self.assertIn("its provider's quota ran out", notice)
        self.assertIn("/resume@HermesBot", notice)

    def test_a_stated_reset_time_freezes_on_the_first_failure(self):
        # Live run: Codex took a hand-off weeks before its quota reset and only posted ❌;
        # the peers learned nothing until two more turns had failed.
        channel = make_channel(make_config(adapter={"type": "codex"}))
        channel._wall = lambda: NOW
        run(channel._note_adapter_failure("You've hit your usage limit. Please retry in 2h."))
        self.assertTrue(channel._frozen)
        self.assertEqual(channel._freeze_until, NOW + 7200)
        self.assertIn("intent=FROZEN ref=quota", channel._test_sender.last)

    def test_without_a_reset_time_it_takes_the_threshold(self):
        channel = make_channel(make_config(adapter={"type": "codex"}))
        channel._wall = lambda: NOW
        run(channel._note_adapter_failure("429 Too Many Requests"))
        self.assertFalse(channel._frozen)

    def test_without_a_reset_time_it_is_retried_after_the_interval(self):
        channel = frozen_channel(detail="429 Too Many Requests")
        self.assertEqual(channel._freeze_until, NOW + 60 * 60)

    def test_it_lifts_itself_at_that_time(self):
        channel = frozen_channel()
        channel._test_wall["t"] = NOW + 7201
        admin_says(channel, "@HermesBot are you there?")
        self.assertFalse(channel._frozen)
        self.assertTrue(any("intent=RESUMED" in s["text"] for s in channel._test_sender.sent))

    def test_a_person_who_addresses_it_is_told_once_per_topic(self):
        channel = frozen_channel()
        before = len(channel._test_sender.sent)
        admin_says(channel, "@HermesBot status check", message_id=11, thread_id=7)
        admin_says(channel, "@HermesBot hello?", message_id=12, thread_id=7)
        admin_says(channel, "no one named here", message_id=13, thread_id=8)
        told = channel._test_sender.sent[before:]
        self.assertEqual(len(told), 1)
        self.assertIn("Hermes is frozen", told[0]["text"])
        self.assertIn("/model@HermesBot", told[0]["text"])

    def test_switching_model_lifts_a_quota_freeze(self):
        channel = frozen_channel()
        admin_says(channel, "/model@HermesBot sonnet")
        replies = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("quota freeze is lifted to try sonnet" in r for r in replies))
        self.assertTrue(any("intent=RESUMED" in r for r in replies))
        self.assertFalse(channel._frozen)

    def test_switching_model_keeps_a_pause(self):
        channel = make_channel(make_config(adapter={"type": "claude"}))
        admin_says(channel, "/pause@HermesBot")
        admin_says(channel, "/model@HermesBot sonnet", message_id=11)
        self.assertIn("still frozen (the admin paused it)", channel._test_sender.last)
        self.assertTrue(channel._frozen)

    def test_bare_model_and_status_say_it_is_frozen(self):
        channel = frozen_channel()
        admin_says(channel, "/model@HermesBot")
        self.assertIn("Hermes is frozen", channel._test_sender.last)
        admin_says(channel, "/status@HermesBot", message_id=11)
        self.assertIn("blocker=frozen: its provider's quota ran out", channel._test_sender.last)

    def test_resume_tells_the_peers(self):
        channel = frozen_channel()
        admin_says(channel, "/resume@HermesBot")
        self.assertFalse(channel._frozen)
        self.assertTrue(any("intent=RESUMED" in s["text"] for s in channel._test_sender.sent))


class TestPersistedFreeze(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "agent.local.freeze.json"

    def _channel(self):
        config = make_config()
        config.source_path = Path(self.tmp.name) / "agent.local.yaml"
        channel = make_channel(config)
        channel._wall = lambda: NOW
        return channel

    def test_an_ended_quota_freeze_is_not_restored(self):
        self.state.write_text(json.dumps({"frozen": True, "ref": "quota", "until": NOW - 1}))
        channel = self._channel()
        run(channel._restore_freeze_state())
        self.assertFalse(channel._frozen)
        self.assertFalse(self.state.exists())

    def test_an_old_quota_freeze_gets_an_end(self):
        self.state.write_text(json.dumps({"frozen": True, "ref": "quota", "at": 5.0}))
        channel = self._channel()
        run(channel._restore_freeze_state())
        self.assertTrue(channel._frozen)
        self.assertEqual(channel._freeze_until, NOW + 3600)
        self.assertIn("Hermes is frozen", channel._test_sender.last)


class TestFrozenPeers(unittest.TestCase):

    def _peer(self, channel, text, message_id):
        run(channel.handle_event(event(text, username="CodexBot", is_bot=True,
                                       message_id=message_id)))

    def test_turns_and_status_name_a_frozen_peer_until_it_resumes(self):
        channel = make_channel()
        channel._wall = lambda: NOW
        self._peer(channel, "[A2A v1.1] STATUS to=* intent=FROZEN ref=quota ttl=120m", 1)
        self.assertEqual(channel.frozen_peers()["Codex"]["until"], NOW + 7200)
        note = channel._frozen_peers_note()
        self.assertIn("Frozen peers take no work", note)
        self.assertIn("Codex (quota ran out", note)
        admin_says(channel, "/status@HermesBot")
        self.assertIn("frozen_peers=Codex(quota)", channel._test_sender.last)
        self._peer(channel, "[A2A v1.1] STATUS to=* intent=RESUMED ref=quota", 2)
        self.assertEqual(channel.frozen_peers(), {})
        self.assertEqual(channel._frozen_peers_note(), "")

    def test_a_peer_freeze_past_its_end_is_forgotten(self):
        channel = make_channel()
        wall = {"t": NOW}
        channel._wall = lambda: wall["t"]
        self._peer(channel, "[A2A v1.1] STATUS to=* intent=FROZEN ref=quota ttl=10m", 1)
        wall["t"] = NOW + 601
        self.assertEqual(channel.frozen_peers(), {})


class TestResetTimes(unittest.TestCase):

    def test_codex_clock_time(self):
        now = dt.datetime(2026, 10, 19, 9, 0).timestamp()
        text = "You've hit your usage limit. Upgrade or try again at Nov 16th, 2026 10:06 PM."
        self.assertEqual(quota_resets_at(text, now),
                         dt.datetime(2026, 11, 16, 22, 6).timestamp())

    def test_relative_and_header_forms(self):
        self.assertEqual(quota_resets_at("Please retry in 41.2s.", NOW), NOW + 41.2)
        self.assertEqual(quota_resets_at("Quota resets in 3 hours", NOW), NOW + 3 * 3600)
        self.assertEqual(quota_resets_at("HTTP 429, Retry-After: 120", NOW), NOW + 120)

    def test_no_time_or_an_implausible_one_is_none(self):
        self.assertIsNone(quota_resets_at("429 Too Many Requests", NOW))
        self.assertIsNone(quota_resets_at("retry in 400 days", NOW))

    def test_ttl_is_whole_minutes(self):
        self.assertEqual(ttl_for(NOW + 61, NOW), "2m")
        self.assertIsNone(ttl_for(None, NOW))


if __name__ == "__main__":
    unittest.main()
