"""General and Swarm pulse are transport-only (topic registry, phase 4).

With ``limits.general_turns: never`` no model turn runs in General or Swarm pulse: an own
brief goes to the last work topic, a person's request gets a topic of its own, the rest is
dropped. The default ("allow") keeps today's behaviour.
"""

import unittest

from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, make_config, run
from tests.test_transport_enforcement import Recording

PULSE = 3418


def channel_for(general_turns="never", coordinator=True):
    config = make_config(limits={"general_turns": general_turns})
    adapter = Recording("on it")
    channel = make_channel(config, adapter=adapter)
    channel.is_coordinator = lambda now=None: coordinator
    channel._pulse_topic = lambda: PULSE
    opened = []

    async def open_topic(ev, title, brief, moved=False):
        opened.append((title, brief))
        return "opened"
    channel._open_topic = open_topic
    return channel, adapter, opened


def admin(text, thread_id=None):
    return event(text, author_id=ADMIN_ID, username="admin", is_bot=False,
                 thread_id=thread_id)


class GeneralGateTests(unittest.TestCase):
    def test_a_persons_request_in_general_opens_a_topic_instead_of_a_turn(self):
        channel, adapter, opened = channel_for()
        run(channel.handle_event(admin("@HermesBot please check the CI flakes on main")))
        self.assertEqual(adapter.prompts, [])
        self.assertEqual(len(opened), 1)
        title, brief = opened[0]
        self.assertEqual(title, "please check the CI flakes on main")
        self.assertIn("Request from @admin in General", brief)

    def test_only_the_coordinator_opens_it(self):
        channel, adapter, opened = channel_for(coordinator=False)
        run(channel.handle_event(admin("@HermesBot please check the CI flakes")))
        self.assertEqual((adapter.prompts, opened), ([], []))

    def test_a_peers_message_in_general_starts_nothing(self):
        channel, adapter, opened = channel_for()
        run(channel.handle_event(event("@HermesBot can you review #12?")))
        self.assertEqual((adapter.prompts, opened), ([], []))

    def test_nothing_runs_in_swarm_pulse(self):
        channel, adapter, opened = channel_for()
        run(channel.handle_event(admin("@HermesBot hello", thread_id=PULSE)))
        self.assertEqual((adapter.prompts, opened), ([], []))

    def test_an_own_brief_goes_to_the_work_topic(self):
        channel, adapter, _ = channel_for()
        channel._last_turn_place = (GROUP, 9400)
        places = []
        respond = channel._respond

        async def spy(incoming, ev):
            places.append((ev.chat_id, ev.thread_id))
            return await respond(incoming, ev)
        channel._respond = spy
        run(channel.run_transport_turn("[Transport notice] CI went red on #12", GROUP, None))
        self.assertEqual(len(adapter.prompts), 1)
        self.assertEqual(places, [(GROUP, 9400)])

    def test_an_own_brief_with_no_work_topic_is_dropped(self):
        channel, adapter, _ = channel_for()
        run(channel.run_transport_turn("[Transport notice] CI went red on #12", GROUP, None))
        self.assertEqual(adapter.prompts, [])

    def test_work_topics_are_untouched(self):
        channel, adapter, opened = channel_for()
        run(channel.handle_event(admin("@HermesBot please check the CI", thread_id=9400)))
        self.assertEqual(len(adapter.prompts), 1)
        self.assertEqual(opened, [])

    def test_commands_still_work_in_general(self):
        channel, adapter, _ = channel_for()
        run(channel.handle_event(admin("/ping@HermesBot")))
        self.assertEqual(adapter.prompts, [])
        self.assertTrue(channel._test_sender.sent, "the command was answered")

    def test_a_request_naming_another_bot_still_gets_a_topic(self):
        """Review: the coordinator's should_respond dropped "@CodexBot ..." as another peer's,
        and Codex left it to the coordinator -- nobody acted."""
        channel, adapter, opened = channel_for()
        run(channel.handle_event(admin("@CodexBot please check the CI flakes")))
        self.assertEqual(adapter.prompts, [])
        self.assertEqual(len(opened), 1)
        self.assertIn("@CodexBot please check the CI flakes", opened[0][1])

    def test_a_peers_request_in_general_is_still_acknowledged(self):
        channel, adapter, _ = channel_for()
        run(channel.handle_event(event(
            "[A2A v1.1] REQUEST to=Hermes intent=QUERY id=r1\n\n@HermesBot status of #12?")))
        self.assertEqual(adapter.prompts, [])
        self.assertTrue(any("ACK" in m["text"] for m in channel._test_sender.sent),
                        channel._test_sender.sent)

    def test_a_peers_handoff_dropped_by_the_gate_is_said_back(self):
        """#327: the gate used to return "" for a peer's message, so a hand-off that passed
        `should_respond` vanished with only a log line the sender cannot read."""
        channel, adapter, _ = channel_for()
        run(channel.handle_event(event("@HermesBot can you review #12?")))
        texts = [m["text"] for m in channel._test_sender.sent]
        self.assertEqual(adapter.prompts, [])
        self.assertTrue(any("took no turn" in t and "@HermesBot" in t for t in texts), texts)
        self.assertTrue(any("general_turns" in t for t in texts), texts)

    def test_a_peers_notice_is_not_answered_with_a_drop_notice(self):
        """A ⚠️ notice, a command answer and a sign-off never asked for a turn (#27)."""
        for text in ("⚠️ Hermes paused this topic; @HermesBot is told",
                     "STATUS to=* intent=PULSE @HermesBot idle",
                     "@HermesBot: closing here, thanks"):
            with self.subTest(text=text):
                channel, adapter, _ = channel_for()
                run(channel.handle_event(event(text)))
                self.assertEqual(adapter.prompts, [])
                self.assertEqual(channel._test_sender.sent, [])

    def test_a_peers_message_that_names_another_bot_says_nothing(self):
        channel, adapter, _ = channel_for()
        run(channel.handle_event(event("@CodexBot can you review #12?")))
        self.assertEqual((adapter.prompts, channel._test_sender.sent), ([], []))

    def test_a_persons_message_the_gate_drops_says_nothing_back(self):
        """A person is not told about a transport setting it cannot read (#418).

        In Swarm pulse, where a person's message really does reach the gate: General is
        handled above it (`_general_request` opens a topic or leaves it), so a General message
        would never get here and a test written that way proves nothing -- the reviewed test
        did exactly that.
        """
        # The coordinator, so the *only* thing keeping this from becoming a topic is the
        # gate's own pulse branch: with that branch gone this message would open one, which
        # is what the mutation check below shows.
        channel, adapter, opened = channel_for()
        run(channel.handle_event(admin("@HermesBot hello", thread_id=PULSE)))
        self.assertEqual((adapter.prompts, opened, channel._test_sender.sent), ([], [], []))

    def test_the_gate_drop_notice_advises_something_that_works(self):
        """#418: "address one of those handles alone" cannot help when the place takes no
        turns at all -- the sender may already have done that. A work topic can."""
        channel, adapter, _ = channel_for()
        run(channel.handle_event(event("@HermesBot can you review #12?")))
        texts = [m["text"] for m in channel._test_sender.sent]
        self.assertEqual(adapter.prompts, [])
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("work topic", texts[0])
        self.assertNotIn("Address one of those handles alone", texts[0])

    def test_an_ordinary_dropped_mention_keeps_the_handle_advice(self):
        """The gate passes its own advice; every other drop keeps the handle rule (#418 nit).

        Here the relay guard drops a peer's "ask @HermesBot ..." made of another bot, with the
        gate off -- so this is the default tail, on the default path.
        """
        channel, adapter, _ = channel_for(general_turns="allow")
        run(channel.handle_event(event("@AntigravityBot: asking @HermesBot to review #12")))
        texts = [m["text"] for m in channel._test_sender.sent]
        self.assertEqual(adapter.prompts, [])
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("Address one of those handles alone", texts[0])
        self.assertNotIn("work topic", texts[0])

    def test_an_own_brief_in_swarm_pulse_goes_to_the_work_topic(self):
        channel, adapter, _ = channel_for()
        channel._last_turn_place = (GROUP, 9400)
        run(channel.run_transport_turn("[Transport notice] peer back", GROUP, PULSE))
        self.assertEqual(len(adapter.prompts), 1)

    def test_a_dropped_transport_turn_says_it_was_not_run(self):
        channel, _, _ = channel_for()
        result = run(channel.run_transport_turn("[Transport notice] CI red", GROUP, None))
        self.assertFalse(result.ok)
        self.assertIn("no work topic", result.error)

    def test_a_bad_value_is_refused(self):
        from swarmly.telegram.config import ConfigError
        with self.assertRaises(ConfigError):
            make_config(limits={"general_turns": "sometimes"})

    def test_allow_keeps_todays_behaviour(self):
        channel, adapter, opened = channel_for(general_turns="allow")
        run(channel.handle_event(admin("@HermesBot please check the CI flakes")))
        self.assertEqual(len(adapter.prompts), 1)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()


class RegistryRoutingMakesThemTransportOnlyTests(unittest.TestCase):
    """Admin, 25 Oct: with registry routing nothing from token space is posted in General or
    Swarm pulse -- live, bots took turns in both and a reply was moved into the pulse."""

    def channel(self):
        channel, adapter, opened = channel_for(general_turns="allow")
        channel.state_link.routing_mode = "registry"
        return channel, adapter, opened

    def test_no_turn_in_general_or_the_pulse(self):
        channel, adapter, _ = self.channel()
        run(channel.handle_event(event("@HermesBot can you review #12?")))
        run(channel.handle_event(event("@HermesBot can you review #12?", thread_id=PULSE)))
        self.assertEqual(adapter.prompts, [])

    def test_work_is_never_moved_into_the_pulse(self):
        """A link into the pulse is refused like a guess into it (#472)."""
        channel, _, _ = self.channel()
        table = {"#300": (GROUP, PULSE), "#301": (GROUP, 9400)}
        channel.state_link.topic_for_refs = lambda refs: next(
            (table[ref] for ref in refs if ref in table), None)
        self.assertIsNone(channel.topic_for_text("progress on #300"))
        self.assertEqual(channel.topic_for_text("progress on #301"), (GROUP, 9400))


class TheWorkPlaceIsNeverThePulseTests(unittest.TestCase):
    """Live, 25 Oct: own briefs went to Swarm pulse, the last place a turn had run."""

    def test_an_own_brief_never_goes_to_the_pulse(self):
        channel, adapter, _ = channel_for()
        channel._last_turn_place = (GROUP, PULSE)
        channel.topics.remember(GROUP, 9400, "work")
        channel.topics.touch(GROUP, 9400)
        self.assertEqual(channel._work_place(), (GROUP, 9400))
        channel._last_turn_place = (GROUP, 9400)
        channel._remember_work_place(GROUP, PULSE)
        self.assertEqual(channel._last_turn_place, (GROUP, 9400))

    def test_no_work_topic_means_none(self):
        channel, _, _ = channel_for()
        channel._last_turn_place = (GROUP, PULSE)
        self.assertIsNone(channel._work_place())


class RepliesStayInTheirTopicTests(unittest.TestCase):
    """Admin, 25 Oct: a reply that named an item was moved into Swarm pulse. Where a post goes
    is the transport's decision; with registry routing a reply stays in its turn's topic."""

    def test_a_reply_naming_another_topics_item_stays(self):
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.adapters.base import AdapterReply

        class Fixed(EchoAdapter):
            async def respond(self, message):
                return AdapterReply(text="Reviewing #300 now; verdict to follow.")
        channel = make_channel(make_config(), adapter=Fixed("Hermes"))
        channel.state_link.routing_mode = "registry"
        channel._topic_elsewhere = lambda ev, body: (GROUP, 9500)     # what refs would pick
        run(channel.handle_event(event("@HermesBot please review #300", thread_id=9400)))
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertFalse(any("moved this to" in t for t in texts), texts)
        self.assertTrue(any("Reviewing #300" in t for t in texts), texts)
