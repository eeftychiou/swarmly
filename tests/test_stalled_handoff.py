"""limits.handoff_nudge_minutes: a hand-off nobody answers gets a reminder, then a notice."""

import unittest

from tests.test_telegram_channel import GROUP, event, make_channel, make_config, run


def handed_off(**limits):
    channel = make_channel(make_config(limits=limits))
    channel.bot = object()
    channel._start = channel._clock()
    channel._mirror_handoffs("@CodexBot please run the suite on main", GROUP, 307, 55)
    return channel


def tick(channel, minutes):
    """Check the hand-offs ``minutes`` after they were made."""
    channel._clock = (lambda t=channel._start + minutes * 60: t)
    run(channel.check_stalled_handoffs())
    return channel._test_sender.sent


class TestStalledHandoff(unittest.TestCase):

    def test_a_reminder_then_a_notice(self):
        channel = handed_off()
        self.assertEqual(tick(channel, 5), [])
        sent = tick(channel, 11)
        self.assertEqual(len(sent), 1)
        self.assertIn("@CodexBot reminder", str(sent[-1]))
        self.assertEqual(len(tick(channel, 15)), 1)          # one reminder only
        sent = tick(channel, 21)
        self.assertEqual(len(sent), 2)
        self.assertIn("has not answered the hand-off", str(sent[-1]))
        self.assertEqual(channel._pending_handoffs, {})

    def test_a_post_by_the_peer_in_that_topic_answers_it(self):
        channel = handed_off()
        run(channel.handle_event(event("on it", username="CodexBot", thread_id=307,
                                       message_id=56)))
        self.assertEqual(channel._pending_handoffs, {})
        self.assertEqual(tick(channel, 30), [])

    def test_a_busy_line_restarts_the_clock_instead_of_answering(self):
        channel = handed_off()
        channel._clock = (lambda t=channel._start + 8 * 60: t)
        run(channel.handle_event(event("[Codex] ⏳ Codex is busy with a turn running since "
                                       "14:58 UTC; 1 message(s) here wait", username="CodexBot",
                                       thread_id=307, message_id=56)))
        self.assertIn((GROUP, 307, "Codex"), channel._pending_handoffs)
        self.assertEqual(tick(channel, 11), [])              # 3 minutes after the busy line
        self.assertIn("@CodexBot reminder", str(tick(channel, 19)[-1]))

    def test_a_peers_reminder_counts_as_this_bots_reminder(self):
        channel = handed_off()
        run(channel.handle_event(event("[Antigravity] @CodexBot reminder: the hand-off above "
                                       "is still waiting for you.", author_id=303,
                                       username="AntigravityBot", thread_id=307,
                                       message_id=57)))
        self.assertEqual(tick(channel, 11), [])              # no second reminder
        self.assertIn("has not answered", str(tick(channel, 21)[-1]))

    def test_a_peers_notice_counts_as_this_bots_notice(self):
        channel = handed_off()
        run(channel.handle_event(event("[Antigravity] ⚠️ Codex has not answered the hand-off "
                                       "above for 20 minutes.", author_id=303,
                                       username="AntigravityBot", thread_id=307,
                                       message_id=57)))
        self.assertEqual(channel._pending_handoffs, {})

    def test_the_notice_hands_the_work_back_to_its_sender(self):
        channel = handed_off()
        tasks = []
        channel._run_own_task = lambda text, chat, thread, name: tasks.append((text, thread))
        tick(channel, 11)
        self.assertEqual(tasks, [])
        tick(channel, 21)
        self.assertEqual(len(tasks), 1)
        self.assertIn("Codex has not answered your hand-off", tasks[0][0])
        self.assertIn("another peer whose role fits", tasks[0][0])
        self.assertEqual(tasks[0][1], 307)

    def test_off_with_zero(self):
        channel = handed_off(handoff_nudge_minutes=0)
        self.assertEqual(tick(channel, 60), [])

    def test_only_the_coordinator_nudges(self):
        # Round 2: every bot reminded every other one; the rule is one nudge, by the
        # coordinator. Another bot decides about its own hand-off, silently.
        channel = handed_off()
        channel.membership.state.coordinator = "Claude"           # someone else
        tasks = []
        channel._run_own_task = lambda text, chat, thread, name: tasks.append(text)
        self.assertEqual(tick(channel, 11), [])
        self.assertEqual(tick(channel, 21), [])
        self.assertEqual(len(tasks), 1)
        self.assertIn("Codex has not answered your hand-off", tasks[0])
        self.assertEqual(channel._pending_handoffs, {})

    def test_the_coordinator_still_nudges(self):
        channel = handed_off()
        channel.membership.state.role = "coordinator"
        channel.membership.state.coordinator = "Hermes"
        self.assertIn("@CodexBot reminder", str(tick(channel, 11)[-1]))

    def test_a_frozen_peer_is_not_nudged_and_its_work_goes_back_at_once(self):
        channel = handed_off()
        channel._peers_frozen["Codex"] = {"ref": "quota", "until": None}
        tasks = []
        channel._run_own_task = lambda text, chat, thread, name: tasks.append(text)
        self.assertEqual(tick(channel, 1), [])                     # no reminder, no notice
        self.assertEqual(len(tasks), 1)
        self.assertIn("Codex is frozen", tasks[0])
        self.assertEqual(channel._pending_handoffs, {})

    def test_an_answer_to_the_asker_is_not_tracked(self):
        channel = make_channel()
        channel.bot = object()
        channel._mirror_handoffs("@CodexBot yes, merged", -100, 307, 55, asker="CodexBot")
        self.assertEqual(channel._pending_handoffs, {})


if __name__ == "__main__":
    unittest.main()
