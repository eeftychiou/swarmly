"""#495: the ⏳ busy line names the turn the topic's own message waits on.

With ``limits.max_parallel_turns`` above 1 a bot can run a turn in one topic while another
topic queues behind its own turn. The old notice measured both the 120 s threshold and the
"since" it printed from ``min(self._busy_since.values())`` -- the oldest turn anywhere -- so a
topic whose own turn was 16 s old announced a peer topic's turn from 5 minutes before.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from tests.test_telegram_channel import GROUP, event, make_channel, make_config, run

TOPIC_A = 307
TOPIC_B = 308
#: The wall clock the notice turns its elapsed time into (``time.strftime`` of the difference).
REAL_NOW = 1_700_000_000.0


def busy_notices(channel):
    return [message["text"] for message in channel._test_sender.sent if "⏳" in message["text"]]


def expected_since(elapsed: float) -> str:
    return time.strftime("%H:%M", time.gmtime(REAL_NOW - elapsed))


def parallel_channel(now: float, **limits):
    """A channel running parallel topic turns on a clock the test controls."""
    channel = make_channel(make_config(limits={"max_parallel_turns": 2, **limits}))
    channel._clock = lambda: now
    return channel


def notice(channel, key, thread_id):
    """Drive ``_busy_notice`` for one key with ``time.time`` pinned to :data:`REAL_NOW`."""
    with mock.patch.object(time, "time", lambda: REAL_NOW):
        run(channel._busy_notice(event("@HermesBot: another one", thread_id=thread_id), key))


class TestBusyNoticePerTopic(unittest.TestCase):
    def test_a_young_turn_here_posts_nothing_while_another_topic_is_long(self):
        """The #495 live case: A running 300 s, B's own turn 16 s old, message in B."""
        now = 10_000.0
        channel = parallel_channel(now)
        channel._busy_since[(GROUP, TOPIC_A)] = now - 300
        channel._busy_since[(GROUP, TOPIC_B)] = now - 16
        channel._busy_waiting_by[(GROUP, TOPIC_B)] = 1
        notice(channel, (GROUP, TOPIC_B), TOPIC_B)
        self.assertEqual(busy_notices(channel), [], "B's own turn is younger than 120 s")
        self.assertNotIn((GROUP, TOPIC_B), channel._busy_noticed)

    def test_the_time_printed_is_this_topics_turn_not_the_oldest(self):
        now = 10_000.0
        channel = parallel_channel(now)
        channel._busy_since[(GROUP, TOPIC_A)] = now - 300
        channel._busy_since[(GROUP, TOPIC_B)] = now - 200
        channel._busy_waiting_by[(GROUP, TOPIC_B)] = 2
        notice(channel, (GROUP, TOPIC_B), TOPIC_B)
        busy = busy_notices(channel)
        self.assertEqual(len(busy), 1, busy)
        self.assertIn("is busy with a turn running since", busy[0])
        self.assertIn(f"since {expected_since(200)} UTC", busy[0])
        self.assertNotIn(f"{expected_since(300)} UTC", busy[0])

    def test_a_wait_only_for_the_cap_names_the_cap(self):
        """No turn of its own: the message waits for a slot, so no turn's start is claimed."""
        now = 10_000.0
        channel = parallel_channel(now)
        channel._busy_since[(GROUP, TOPIC_A)] = now - 300
        channel._busy_since[(GROUP, TOPIC_B)] = now - 250
        channel._busy_waiting_by[(GROUP, 309)] = 1
        notice(channel, (GROUP, 309), 309)
        busy = busy_notices(channel)
        self.assertEqual(len(busy), 1, busy)
        self.assertIn("busy with 2 turns in other topics", busy[0])
        self.assertNotIn("running since", busy[0])
        self.assertNotIn(expected_since(300), busy[0])
        self.assertNotIn(expected_since(250), busy[0])

    def test_one_turn_for_the_whole_bot_still_names_the_running_turn(self):
        """``max_parallel_turns`` 1: whatever runs is what this topic's message waits for."""
        now = 10_000.0
        channel = make_channel(make_config())
        channel._clock = lambda: now
        channel._busy_since[(GROUP, TOPIC_A)] = now - 200
        notice(channel, (GROUP, TOPIC_B), TOPIC_B)
        busy = busy_notices(channel)
        self.assertEqual(len(busy), 1, busy)
        self.assertIn(f"since {expected_since(200)} UTC", busy[0])
        self.assertNotIn("in other topics", busy[0])

    def test_the_cap_wait_is_still_one_line_per_window(self):
        now = 10_000.0
        channel = parallel_channel(now)
        channel._busy_since[(GROUP, TOPIC_A)] = now - 300
        notice(channel, (GROUP, 309), 309)
        now = channel._clock()
        channel._clock = lambda: now + 60          # inside BUSY_NOTICE_REPEAT_SECONDS
        notice(channel, (GROUP, 309), 309)
        self.assertEqual(len(busy_notices(channel)), 1, busy_notices(channel))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
