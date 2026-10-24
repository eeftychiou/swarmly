"""The transport's link to the shared state store, in shadow (topic registry, phase 1)."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from swarmly.state.telegram_store import TelegramStore
from swarmly.telegram.config import AgentConfig, ConfigError
from swarmly.telegram.state_link import StateLink
from swarmly.telegram.topics import TopicMap
from tests.test_state_store import Clock, FakeGroup, FakeIO


def link_for(group, alias, clock, coordinator=False, backend="telegram"):
    channel = SimpleNamespace(
        config=SimpleNamespace(identity=SimpleNamespace(alias=alias, name=alias,
                                                        home_channel_id=-100, role=None),
                               state=SimpleNamespace(backend=backend, mode="shadow",
                                                     heartbeat_minutes=10),
                               adapter=SimpleNamespace(type="codex", model=None, effort=None),
                               projects={}),
        is_coordinator=lambda: coordinator, _running_revision="abc123",
        topics=TopicMap(None, home_chat_id=-100), adapter=None, _models={}, _efforts={},
        _ref_topics={}, owed_store=lambda: SimpleNamespace(load=lambda: []))
    link = StateLink(channel, clock=clock)
    if link.enabled:
        link.store = TelegramStore(alias, FakeIO(group, alias), clock=clock)
        link.store.CONFIRM_SECONDS = 0.0
        group.stores[alias] = link.store
    return link


class StateLinkTests(unittest.TestCase):
    def test_off_unless_turned_on(self):
        link = link_for(None, "Claudebot", Clock(), backend="off")
        self.assertFalse(link.enabled)
        self.assertIn("off", link.describe())
        self.assertEqual(asyncio.run(link.beat()), "off")

    def test_the_coordinator_claims_the_lease_publishes_and_everyone_beats(self):
        clock = Clock()
        group = FakeGroup(clock)
        claude = link_for(group, "Claudebot", clock, coordinator=True)
        deepseek = link_for(group, "DeepSeek", clock)

        async def go():
            await claude.store.start()
            await deepseek.store.start()
            said = await claude.beat()
            self.assertIn("lease claimed", said)
            self.assertIn("beat", said)
            self.assertIn("beat", await deepseek.beat())
            clock.t += 60
            said = await claude.beat()
            self.assertIn("snapshot", said)
            await deepseek.beat()
        asyncio.run(go())
        text = deepseek.describe()
        self.assertIn("lease: claudebot (term 1)", text)
        self.assertIn("claudebot: agrees", text)
        self.assertIn("deepseek: agrees", text)
        self.assertEqual(claude.store.lease().holder, "claudebot")

    def test_a_registry_error_does_not_stop_the_beat(self):
        clock = Clock()
        group = FakeGroup(clock)
        claude = link_for(group, "Claudebot", clock, coordinator=True)

        async def broken(store):
            raise ValueError("an adapter quirk")
        claude.registry.sync = broken

        async def go():
            await claude.store.start()
            return await claude.beat()
        said = asyncio.run(go())
        self.assertIn("registry failed", said)
        self.assertIn("beat", said.replace("registry failed", ""))

    def test_a_failing_store_never_raises(self):
        clock = Clock()
        group = FakeGroup(clock)
        claude = link_for(group, "Claudebot", clock, coordinator=True)

        async def go():
            await claude.store.start()
            group.down = True
            return await claude.beat()
        self.assertEqual(asyncio.run(go()), "failed")
        self.assertIn("unreachable", claude.describe())


class OwedShadowTests(unittest.TestCase):
    def test_each_bot_writes_its_own_rows_and_the_store_folds_them(self):
        from swarmly.telegram.owed import Row
        clock = Clock()
        group = FakeGroup(clock)
        claude = link_for(group, "Claudebot", clock, coordinator=True)
        deepseek = link_for(group, "DeepSeek", clock)
        mine = Row(kind="handoff", item="#12", debtor="deepseek", owes="a review",
                   writer="claudebot", key_extra="tg-7")
        theirs = Row(kind="handoff", item="#13", debtor="claudebot", owes="a merge",
                     writer="deepseek", key_extra="tg-7")
        both = [mine, theirs]
        for link in (claude, deepseek):
            link.channel.owed_store = lambda: SimpleNamespace(load=lambda: list(both))

        async def go():
            await claude.store.start()
            await deepseek.store.start()
            self.assertTrue(await claude.sync_owed(claude.store, clock()))
            self.assertTrue(await deepseek.sync_owed(deepseek.store, clock()))
            self.assertFalse(await claude.sync_owed(claude.store, clock()), "unchanged")
            await claude.store.refresh()
        asyncio.run(go())
        stored = claude.store.read("agent/claudebot/owed")["rows"]
        self.assertEqual([r["item"] for r in stored], ["#12"])     # only its own rows
        self.assertEqual(sorted(r.id for r in claude.owed_from_store()),
                         sorted([mine.id, theirs.id]))
        self.assertIn("2 open row(s) in the store, 2 here (agree)", claude.describe())


class FloodWaitTests(unittest.TestCase):
    def test_a_flood_wait_is_waited_out(self):
        from unittest import mock
        from swarmly.telegram import state_link

        class Flood(Exception):
            retry_after = 0

        calls = []

        class Bot:
            async def send_message(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise Flood("Too Many Requests: retry after 0")
                return SimpleNamespace(message_id=42, date=1000.0)

        async def topic():
            return 3418
        channel = SimpleNamespace(bot=Bot(), _ensure_pulse_topic=topic,
                                  config=SimpleNamespace(identity=SimpleNamespace(
                                      home_channel_id=-100)))
        with mock.patch.object(state_link.asyncio, "sleep", mock.AsyncMock()):
            posted = asyncio.run(state_link.ChannelIO(channel).post_line("x"))
        self.assertEqual((posted.message_id, len(calls)), (42, 2))


class StateConfigTests(unittest.TestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("agent:\n  name: Bot\n  alias: Bot\n  bot_token: \"1:x\"\n"
                            "  home_channel_id: -100\n  admin_user_id: 1\n" + extra,
                            encoding="utf-8")
            return AgentConfig.load(path)

    def test_defaults_to_off(self):
        self.assertEqual(self.load("").state.backend, "off")

    def test_telegram_shadow(self):
        config = self.load("state:\n  backend: telegram\n  heartbeat_minutes: 5\n")
        self.assertEqual((config.state.backend, config.state.mode,
                          config.state.heartbeat_minutes), ("telegram", "shadow", 5.0))
        self.assertNotIn("state", " ".join(config.unknown_keys))

    def test_bad_values_are_refused(self):
        with self.assertRaises(ConfigError):
            self.load("state:\n  backend: carrier-pigeon\n")
        with self.assertRaises(ConfigError):
            self.load("state:\n  backend: telegram\n  mode: on\n")


if __name__ == "__main__":
    unittest.main()
