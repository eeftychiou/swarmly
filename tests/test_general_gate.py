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
