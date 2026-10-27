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
        self.assertEqual([(l.ref, l.added_by, l.role) for l in topic.links],
                         [("https://github.com/o/r/issues/237", "legacy", "reference")])
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

    def test_the_first_session_is_not_a_rotation_and_its_turn_counts(self):
        bot = channel_for("DeepSeek", self.clock)
        seen(bot, 7, "t")
        context = bot.topics.context(HOME, 7)
        b = self.store("DeepSeek")
        shadow = RegistryShadow(bot, clock=self.clock)

        async def go():
            await shadow.sync(b)                         # no session yet
            bot.adapter.threads[context] = "T1"
            shadow.turn_done(context, 7)
            await shadow.sync(b)
        asyncio.run(go())
        record = b.read(K.topic_agent(7, "DeepSeek"))
        self.assertEqual((record["session_id"], record.get("rotations", 0), record["turns"]),
                         ("T1", 0, 1))

    def test_a_restart_keeps_the_turn_count_and_sees_a_new_session(self):
        bot = channel_for("DeepSeek", self.clock)
        seen(bot, 7, "t")
        context = bot.topics.context(HOME, 7)
        b = self.store("DeepSeek")

        async def go():
            bot.adapter.threads[context] = "T1"
            first = RegistryShadow(bot, clock=self.clock)
            for _ in range(5):
                first.turn_done(context, 7)
            await first.sync(b)
            again = RegistryShadow(bot, clock=self.clock)    # the transport restarted
            again.turn_done(context, 7)
            await again.sync(b)
            self.assertEqual(b.read(K.topic_agent(7, "DeepSeek"))["turns"], 6)
            bot.adapter.threads[context] = "T2"             # replaced while it was down
            third = RegistryShadow(bot, clock=self.clock)
            await third.sync(b)
        asyncio.run(go())
        record = b.read(K.topic_agent(7, "DeepSeek"))
        self.assertEqual((record["session_id"], record["rotations"]), ("T2", 1))

    def test_topics_never_active_are_not_written(self):
        claude = channel_for("Claudebot", self.clock)
        claude.topics.remember(HOME, 9, "named, never active")
        a = self.store("Claudebot")

        async def go():
            await a.claim_lease(10 ** 6)
            return await RegistryShadow(claude, clock=self.clock).sync(a)
        self.assertNotIn("topic/9", asyncio.run(go()))

    def test_a_derived_session_is_written_only_where_the_bot_worked(self):
        claude = channel_for("Claudebot", self.clock, "claude_thread")
        claude.adapter = SimpleNamespace(KEEPS_TOPIC_SESSION=True, alias="Claudebot",
                                         settings=SimpleNamespace(cwd="."),
                                         derived_session_id=lambda a, c, x: f"d-{x}")
        seen(claude, 7, "worked here")
        seen(claude, 8, "never here")
        a = self.store("Claudebot")
        shadow = RegistryShadow(claude, clock=self.clock)
        shadow.turn_done(claude.topics.context(HOME, 7), 7)
        written = asyncio.run(shadow.sync(a))
        self.assertIn("topic/7/agent/claudebot", written)
        self.assertNotIn("topic/8/agent/claudebot", written)

    def test_a_topic_choice_of_model_is_marked_as_the_topics(self):
        claude = channel_for("Claudebot", self.clock, "claude", "claude-opus-5-5")
        seen(claude, 7, "t")
        claude._models[claude.topics.context(HOME, 7)] = "claude-fable-5-1"
        binding = RegistryShadow(claude, clock=self.clock).binding(
            claude.topics.context(HOME, 7), "coordinator")
        self.assertEqual((binding.model, binding.model_source),
                         ("claude-fable-5-1", "topic"))

    def _holding(self, alias="Claudebot"):
        """A store whose bot holds the lease: only the lease holder may write a topic record."""
        store = self.store(alias)
        asyncio.run(store.claim_lease(10 ** 6))
        return store

    def _closed_topic(self, store, thread=8, title="a topic that is gone"):
        """A record `/cleanup` closed: the registry keeps it, the listing must not show it."""
        record = {"topic_id": thread, "chat_id": HOME, "title": title, "status": "closed",
                  "opened_by": "admin", "opened_at": "2026-10-27T00:00:00Z",
                  "closed_at": "2026-10-27T01:00:00Z",
                  "last_activity": "2026-10-27T01:00:00Z", "links": [], "agents": {}}
        asyncio.run(store.write(K.topic(thread), record))

    def test_topics_lists_open_records_and_counts_the_closed_ones(self):
        """#435: a closed record is left out of /topics and counted, not printed."""
        claude = channel_for("Claudebot", self.clock)
        seen(claude, 10134, "Stability round")
        a = self._holding()
        self._closed_topic(a)
        shadow = RegistryShadow(claude, clock=self.clock)
        asyncio.run(shadow.sync(a))

        text = shadow.describe(a)
        self.assertIn("Stability round (10134)", text)
        self.assertNotIn("a topic that is gone", text)
        self.assertIn("+1 closed (/topics all shows them)", text)

    def test_topics_all_shows_the_closed_records_marked_closed(self):
        claude = channel_for("Claudebot", self.clock)
        seen(claude, 10134, "Stability round")
        a = self._holding()
        self._closed_topic(a)
        shadow = RegistryShadow(claude, clock=self.clock)
        asyncio.run(shadow.sync(a))

        text = shadow.describe(a, show_all=True)
        self.assertIn("a topic that is gone (8) closed", text)
        # The count, not the placeholder -- `/topics all` printed a literal "+N closed".
        self.assertIn("+1 closed (listed above)", text)
        self.assertNotIn("+N", text)

    def test_topics_on_an_all_closed_registry_counts_them_and_says_so(self):
        """A registry whose every topic /cleanup closed is not "empty so far" (#435 review)."""
        claude = channel_for("Claudebot", self.clock)
        a = self._holding()
        self._closed_topic(a, thread=8)
        self._closed_topic(a, thread=9, title="another gone one")
        shadow = RegistryShadow(claude, clock=self.clock)

        text = shadow.describe(a)
        self.assertIn("no open topics", text)
        self.assertIn("+2 closed (/topics all shows them)", text)
        self.assertNotIn("empty so far", text)

    def test_an_agents_handle_and_its_alias_are_one_row(self):
        """`ASSIGN: @claude_obot` records the handle beside the alias: one agent, one row.

        The handle is not prefixed by the alias here (`Claudebot` / `claude_obot`), which is
        the live shape from #423 -- the prefix rule alone printed both rows.
        """
        claude = channel_for("Claudebot", self.clock)
        claude.config.peers = {"Claudebot": "claude_obot"}
        a = self._holding()
        record = {"topic_id": 11, "chat_id": HOME, "title": "quiet", "status": "open",
                  "opened_at": "2026-10-27T00:00:00Z", "last_activity": "2026-10-27T00:00:00Z",
                  "links": [],
                  "agents": {"Claudebot": {"role": "coordinator", "model": "m", "effort": "high"},
                             "claude_obot": {}}}
        asyncio.run(a.write(K.topic(11), record))
        # The session can sit under the handle: merging must keep it, not drop it. Only the
        # agent's own store may write its key, so a second store shares the log here.
        b = self.store("Claude_obot")
        asyncio.run(b.write(K.topic_agent(11, "claude_obot"),
                            {"turns": 3, "session_id": "sess-abcde"}))
        asyncio.run(a.refresh())

        text = RegistryShadow(claude, clock=self.clock).describe(a)
        self.assertIn("Claudebot coordinator m/high s=sess-abcde t=3", text)
        self.assertNotIn("claude_obot", text, "the handle is not a second agent")
        self.assertEqual(text.count("• quiet (11)"), 1)

    def test_the_topics_command_reaches_show_all_in_any_case(self):
        """/topics@bot all (and ALL) must reach describe(show_all=True) (#435 review)."""
        from swarmly.telegram.channel import AgentChannel, ChannelEvent

        seen = []

        class Recorder:
            def describe(self, store, *, show_all=False):
                seen.append(show_all)
                return "Topic registry:\n• x (1)"

        stub = SimpleNamespace(
            _commands_enabled=lambda: True,
            config=SimpleNamespace(identity=SimpleNamespace(name="Claudebot_bot")),
            state_link=SimpleNamespace(store=None, registry=Recorder()))
        for text in ("/topics@Claudebot_bot", "/topics@Claudebot_bot all",
                     "/topics@Claudebot_bot ALL"):
            reply = AgentChannel._handle_command(stub, ChannelEvent(text=text, chat_id=HOME,
                                                                    thread_id=1))
            self.assertIn("Topic registry", reply or "")
        self.assertEqual(seen, [False, True, True])

    def test_no_agent_row_for_a_bot_that_never_spoke(self):
        """An agent with no binding and no turns is noise: no `not linked … t=0` row (#435).

        A peer's own key is the only way such a row appears (`topic/<id>/agent/<alias>` is
        written by that agent), so two stores share the log here, as on the live swarm.
        """
        claude = channel_for("Claudebot", self.clock)
        a = self._holding()
        b = self.store("DeepSeek")
        record = {"topic_id": 11, "chat_id": HOME, "title": "quiet", "status": "open",
                  "opened_at": "2026-10-27T00:00:00Z", "last_activity": "2026-10-27T00:00:00Z",
                  "links": [], "agents": {"Claudebot": {"role": "coordinator"}}}
        asyncio.run(a.write(K.topic(11), record))
        asyncio.run(b.write(K.topic_agent(11, "deepseek"), {"turns": 0}))   # never spoke
        asyncio.run(a.refresh())
        text = RegistryShadow(claude, clock=self.clock).describe(a)
        self.assertIn("quiet (11)", text)
        self.assertIn("claudebot coordinator", text)
        self.assertNotIn("deepseek", text, "a silent, unbound agent gets no row")

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
