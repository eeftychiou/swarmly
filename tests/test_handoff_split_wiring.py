"""The transport half of #181: a multi-recipient hand-off reaches every bot, never a refusal.

The decision is in `handoff_split`; these tests pin where the transport uses it -- the routing
decision that used to refuse, and the brief the turn actually reads.
"""

import unittest

from tests.test_telegram_channel import event, make_channel, make_config, run

#: Every bot in the swarm, under both spellings the live messages use.
PEERS = {"Codex": "codex_dd_bot", "Reasonix": "reasonix_dd_bot",
         "Antigravity": "agy_dd_bot", "Claude": "Claude_obot"}

TWO_ASKS = ("@reasonix_dd_bot: please re-review #191 at e326779.\n"
            "@agy_dd_bot: please QA #191 at e326779, then post on the PR.")

DELEGATION = "@codex_dd_bot please hand @reasonix_dd_bot the task, it is in the topic."


def channel_named(alias, username):
    channel = make_channel(make_config(agent={"alias": alias, "name": username},
                                       peers=dict(PEERS)))
    channel.bot = type("Bot", (), {"id": 999})()
    return channel


class TestTheDecision(unittest.TestCase):
    """`should_respond`: the refusal that dropped a request eight times in round 5."""

    def test_a_message_asking_two_bots_is_not_refused(self):
        channel = channel_named("Reasonix", "reasonix_dd_bot")
        decision, reason = channel.should_respond(
            event(TWO_ASKS, author_id=303, username="Claude_obot", thread_id=307, message_id=55))
        self.assertTrue(decision, reason)

    def test_a_delegation_is_still_refused(self):
        """The case the rule was written for: another bot is asked to hand this bot work."""
        channel = channel_named("Reasonix", "reasonix_dd_bot")
        decision, reason = channel.should_respond(
            event(DELEGATION, author_id=303, username="Claude_obot", thread_id=307, message_id=56))
        self.assertFalse(decision)
        self.assertIn("a peer asks another bot", reason)


class TestTheBriefTheTurnReads(unittest.TestCase):
    """The split itself: this bot runs its own line, plus the context, and no one else's ask."""

    def test_it_reads_its_own_line_and_not_the_other_bots(self):
        channel = channel_named("Antigravity", "agy_dd_bot")
        own = channel._own_brief(TWO_ASKS)
        self.assertIn("QA #191 at e326779", own)
        self.assertNotIn("re-review #191", own)

    def test_the_other_bot_reads_the_other_line(self):
        channel = channel_named("Reasonix", "reasonix_dd_bot")
        own = channel._own_brief(TWO_ASKS)
        self.assertIn("re-review #191", own)
        self.assertNotIn("QA #191", own)

    def test_a_single_recipient_message_is_left_alone(self):
        channel = channel_named("Antigravity", "agy_dd_bot")
        text = "@agy_dd_bot: QA #191 at e326779 please."
        self.assertEqual(channel._own_brief(text), text)

    def test_the_turn_really_runs_on_the_narrowed_text(self):
        """End to end through the echo adapter: the reply carries this bot's line only."""
        channel = channel_named("Antigravity", "agy_dd_bot")
        run(channel.handle_event(event(TWO_ASKS, author_id=303, username="Claude_obot",
                                      thread_id=307, message_id=57)))
        said = " ".join(str(sent) for sent in channel._test_sender.sent)
        self.assertIn("QA #191", said)
        self.assertNotIn("re-review #191", said)


class TestTheHandlesItKnows(unittest.TestCase):

    def test_both_spellings_of_every_bot_are_known(self):
        channel = channel_named("Antigravity", "agy_dd_bot")
        handles = channel._handoff_handles()
        self.assertIn("Reasonix", handles)
        self.assertIn("reasonix_dd_bot", handles)

    def test_it_finds_the_handle_the_message_uses_for_itself(self):
        channel = channel_named("Antigravity", "agy_dd_bot")
        self.assertEqual(channel._own_handle("@agy_dd_bot: QA it."), "agy_dd_bot")
        self.assertEqual(channel._own_handle("@Antigravity: QA it."), "Antigravity")
