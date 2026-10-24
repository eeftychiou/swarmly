"""The topic registry filled in shadow from today's logic (topic registry, phase 2)."""

import asyncio
import unittest
from types import SimpleNamespace

from swarmly.state import keys as K
from swarmly.state.records import Topic
from swarmly.state.store import MemoryLog, MemoryStore
from swarmly.telegram.registry_shadow import RegistryShadow, session_of
from swarmly.telegram.topics import TopicMap

HOME = -1001234567890
CLOCK_START = 1_800_000_000.0


class Clock:
    def __init__(self, t=CLOCK_START):
        self.t = t

    def __call__(self):
        return self.t


class ThreadAdapter:
    def __init__(self):
        self.threads = {}

    def thread_for(self, context):
        return self.threads.get(context)


def channel_for(alias, clock, adapter_type="codex", model=None):
    topics = TopicMap(None, home_chat_id=HOME)
    adapter = ThreadAdapter()
    config = SimpleNamespace(
        identity=SimpleNamespace(alias=alias, name=f"{alias}_bot", home_channel_id=HOME,
                                 role=None),
        adapter=SimpleNamespace(type=adapter_type, model=model, effort="high",
                                session_turn_limit=40),
        projects={"swarmly": SimpleNamespace(repo="o/r")})
    return SimpleNamespace(config=config, topics=topics, adapter=adapter, _models={},
                           _efforts={}, _ref_topics={})


def seen(channel, thread, name, when=None):
    channel.topics.remember(HOME, thread, name)
    channel.topics.touch(HOME, thread, when=when if when is not None else CLOCK_START)


class RegistryShadowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.shared = MemoryLog(clock=self.clock)

    def store(self, alias):
        store = MemoryStore(alias, self.shared, clock=self.clock)
        asyncio.run(store.start())
        return store

    def test_the_holder_writes_topics_and_everyone_their_own_keys(self):
        claude, deepseek = channel_for("Claudebot", self.clock, "claude", "claude-opus-5-5"), \
            channel_for("DeepSeek", self.clock, "reasonix", "deepseek-flash")
        for channel in (claude, deepseek):
            seen(channel, 10134, "Stability round")
        claude._ref_topics = {"#237": (HOME, 10134), "#99": (HOME, 1)}
        deepseek.adapter.threads[claude.topics.context(HOME, 10134)] = "sess-1"
        a, b = self.store("Claudebot"), self.store("DeepSeek")

        async def go():
            await a.claim_lease(10 ** 6)
            wrote_a = await RegistryShadow(claude, clock=self.clock).sync(a)
            shadow_b = RegistryShadow(deepseek, clock=self.clock)
            shadow_b.turn_done(deepseek.topics.context(HOME, 10134), 10134)
            wrote_b = await shadow_b.sync(b)
            await a.refresh()
            return wrote_a, wrote_b
        wrote_a, wrote_b = asyncio.run(go())
        self.assertIn("topic/10134", wrote_a)
        self.assertNotIn("topic/10134", wrote_b)
        self.assertIn("topic/10134/agent/deepseek", wrote_b)
        self.assertIn("agent/deepseek/profile", wrote_b)
        topic = Topic.from_dict(a.read("topic/10134"))
        self.assertEqual(topic.title, "Stability round")
        self.assertEqual(topic.agents["Claudebot"].role, "coordinator")
        self.assertEqual(topic.agents["Claudebot"].model, "claude-opus-5-5")
        self.assertEqual([(l.ref, l.added_by) for l in topic.links],
                         [("https://github.com/o/r/issues/237", "legacy")])
        session = a.read("topic/10134/agent/deepseek")
        self.assertEqual((session["session_id"], session["turns"]), ("sess-1", 1))
        text = RegistryShadow(claude, clock=self.clock).describe(a)
        self.assertIn("Stability round (10134) — #237", text)
        self.assertIn("deepseek not linked s=sess-1 t=1", text)

    def test_a_quiet_swarm_writes_nothing(self):
        claude = channel_for("Claudebot", self.clock)
        seen(claude, 7, "t")
        a = self.store("Claudebot")
        shadow = RegistryShadow(claude, clock=self.clock)

        async def go():
            await a.claim_lease(10 ** 6)
            first = await shadow.sync(a)
            self.clock.t += 120
            second = await shadow.sync(a)
            return first, second
        first, second = asyncio.run(go())
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_a_new_session_counts_a_rotation(self):
        bot = channel_for("DeepSeek", self.clock)
        seen(bot, 7, "t")
        context = bot.topics.context(HOME, 7)
        b = self.store("DeepSeek")
        shadow = RegistryShadow(bot, clock=self.clock)

        async def go():
            bot.adapter.threads[context] = "s1"
            shadow.turn_done(context, 7)
            await shadow.sync(b)
            bot.adapter.threads[context] = "s2"
            await shadow.sync(b)
        asyncio.run(go())
        record = b.read(K.topic_agent(7, "DeepSeek"))
        self.assertEqual((record["session_id"], record["rotations"], record["turns"]),
                         ("s2", 1, 0))

    def test_a_topic_choice_of_model_is_marked_as_the_topics(self):
        claude = channel_for("Claudebot", self.clock, "claude", "claude-opus-5-5")
        seen(claude, 7, "t")
        claude._models[claude.topics.context(HOME, 7)] = "claude-fable-5-1"
        binding = RegistryShadow(claude, clock=self.clock).binding(
            claude.topics.context(HOME, 7), "coordinator")
        self.assertEqual((binding.model, binding.model_source),
                         ("claude-fable-5-1", "topic"))

    def test_session_of_each_runtime(self):
        self.assertIsNone(session_of(None, "c"))
        codex = ThreadAdapter()
        codex.threads["c"] = "t1"
        self.assertEqual(session_of(codex, "c"), "t1")
        agy = SimpleNamespace(conversation_for=lambda c: "conv")
        self.assertEqual(session_of(agy, "c"), "conv")
        reasonix = SimpleNamespace(TYPE="reasonix", session_for=lambda c: "r1")
        self.assertEqual(session_of(reasonix, "c"), "r1")
        fresh_claude = SimpleNamespace(KEEPS_TOPIC_SESSION=False)
        self.assertIsNone(session_of(fresh_claude, "c"))


if __name__ == "__main__":
    unittest.main()
