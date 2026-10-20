"""Peer liveness (issue #82): a heartbeat, and DOWN as an advisory fact.

What these pin is what the design review asked for: DOWN means silence on *both* planes, a busy
peer is never DOWN, a restart never turns "not heard yet" into "everyone is down", and the admin
notice happens once per episode.
"""

import json
import unittest

from swarmly.telegram import presence as pr
from tests.test_telegram_channel import (ADMIN_ID, GROUP, event, make_channel, make_config,
                                         run)


class TestPulse(unittest.TestCase):

    def test_round_trip(self):
        pulse = pr.Pulse(alias="Claudebot", at=1000.0, rev="abc123", role="coordinator", term=3,
                         busy_since=990.0, frozen=False)
        again = pr.Pulse.from_dict(pulse.to_dict())
        self.assertEqual((again.alias, again.at, again.term, again.busy_since),
                         ("Claudebot", 1000.0, 3, 990.0))

    def test_a_bad_pulse_is_refused(self):
        for data in ({}, {"alias": "x"}, {"alias": "x", "at": "soon"},
                     {"alias": "x", "at": 1, "term": -1}, {"alias": "", "at": 1}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                pr.Pulse.from_dict(data)


class TestDown(unittest.TestCase):

    def presence(self, *, down_after=90.0, grace=None, base=1000.0):
        return pr.Presence(base, down_after=down_after, grace=grace)

    def test_silence_for_the_window_marks_a_peer_down(self):
        table = self.presence(down_after=90.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        self.assertFalse(table.is_down("DeepSeek", 1080.0))
        self.assertTrue(table.is_down("DeepSeek", 1090.0))
        self.assertEqual(table.status("DeepSeek", 1090.0), "down")

    def test_a_pulse_brings_a_peer_back_up(self):
        table = self.presence()
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        self.assertTrue(table.is_down("DeepSeek", 1200.0))
        table.note(pr.Pulse(alias="DeepSeek", at=1200.0))
        self.assertEqual(table.status("DeepSeek", 1200.0), "up")

    def test_any_message_counts_as_liveness(self):
        """DOWN is silence on both planes: a Telegram message keeps a peer up with no pulse."""
        table = self.presence()
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        table.seen("DeepSeek", 1080.0)
        self.assertFalse(table.is_down("DeepSeek", 1080.0))

    def test_a_busy_peer_is_never_down(self):
        table = self.presence(down_after=90.0)
        for stamp in (1000.0, 1090.0, 1180.0):
            table.note(pr.Pulse(alias="DeepSeek", at=stamp, busy_since=stamp - 5))
        self.assertFalse(table.is_down("DeepSeek", 1180.0))
        self.assertEqual(table.status("DeepSeek", 1180.0), "busy")

    def test_a_peer_never_heard_from_is_unknown_not_down(self):
        table = self.presence()
        self.assertFalse(table.is_down("Codex", 5000.0))
        self.assertEqual(table.status("Codex", 5000.0), "unknown")

    def test_a_restart_gets_a_grace_period(self):
        table = self.presence(down_after=90.0, base=10_000.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))     # a stale last-seen from a file
        self.assertFalse(table.is_down("DeepSeek", 10_010.0))
        self.assertTrue(table.is_down("DeepSeek", 10_090.0))


class TestOperatorView(unittest.TestCase):

    def test_the_status_line_says_how_long(self):
        table = pr.Presence(1000.0, down_after=90.0)
        table.note(pr.Pulse(alias="Claudebot", at=1100.0))
        table.note(pr.Pulse(alias="DeepSeek", at=400.0))
        line = table.line(1120.0, order=["Claudebot", "DeepSeek"])
        self.assertIn("Claudebot:up", line)
        self.assertIn("DeepSeek:down(", line)

    def test_busy_duration_is_visible(self):
        table = pr.Presence(1000.0, down_after=90.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1180.0, busy_since=1000.0))
        self.assertIn("DeepSeek:busy(3m)", table.line(1180.0))

    def test_the_admin_notice_fires_once_per_episode(self):
        table = pr.Presence(1000.0, down_after=90.0)
        table.note(pr.Pulse(alias="DeepSeek", at=1000.0))
        self.assertTrue(table.mark_notified("DeepSeek", 1100.0))
        self.assertFalse(table.mark_notified("DeepSeek", 1200.0))
        # ... and a peer that comes back and goes quiet again is a new episode.
        table.note(pr.Pulse(alias="DeepSeek", at=1300.0))
        self.assertTrue(table.mark_notified("DeepSeek", 1500.0))


# --------------------------------------------------------------------------------------
# On the channel: the pulse, /status and /swarm, the hand-off, and the one admin line
# --------------------------------------------------------------------------------------
NOW = 1_800_000_000.0


def live_channel(**limits):
    """A channel whose presence clock a test moves, at the default 2m pulse / 3 misses."""
    channel = make_channel(make_config(limits={"heartbeat_minutes": 2, "peer_down_after": 3,
                                                **limits}))
    clock = {"t": NOW}
    channel.presence_clock = lambda: clock["t"]
    channel._test_presence_clock = clock
    return channel


def pulse_text(alias, at, *, busy_since=None, frozen=False):
    body = json.dumps(pr.Pulse(alias=alias, at=at, rev="abc1234", role="developer", term=2,
                               busy_since=busy_since, frozen=frozen).to_dict())
    return f"[A2A v1.1] STATUS to=* intent=PULSE ref={alias} rev=abc1234\n\n{body}"


def hear(channel, text, message_id=1, username="CodexBot"):
    run(channel.handle_event(event(text, username=username, message_id=message_id)))


def admin_lines(channel, needle):
    return [m["text"] for m in channel._test_sender.sent if needle in m["text"]]


class TestThePulse(unittest.TestCase):

    def test_the_interval_and_the_down_rule_come_from_the_limits(self):
        channel = live_channel()
        self.assertEqual(channel.heartbeat_seconds(), 120.0)
        self.assertEqual(channel.down_after_seconds(), 360.0)      # 2m x 3 missed pulses
        self.assertEqual(live_channel(heartbeat_minutes=0).down_after_seconds(), float("inf"))
        self.assertEqual(live_channel(peer_down_after=0).down_after_seconds(), float("inf"))

    def test_the_pulse_is_one_status_line_and_not_a_bus_commit(self):
        channel = live_channel()
        said = run(channel._heartbeat_tick())
        sent = channel._test_sender.last
        self.assertIn("STATUS to=* intent=PULSE", sent)
        payload = pr.Pulse.from_dict(json.loads(sent.split("\n\n", 1)[1]))
        self.assertEqual(payload.alias, "Hermes")
        self.assertEqual(payload.rev, channel._running_revision)
        self.assertIn("pulse rev=", said)
        # The bus is what this must never become: exactly one Telegram line, no bus write.
        self.assertEqual(len(channel._test_sender.sent), 1)

    def test_a_long_turn_keeps_pulsing_and_says_since_when(self):
        # The pulse is its own task, not the turn lock: a turn 25 minutes in still goes out and
        # carries `busy_since`, so a peer reads work, not a die-off.
        channel = live_channel()
        channel._busy_since[(GROUP, 7)] = channel._clock() - 1500
        pulse = channel.own_pulse(NOW)
        self.assertEqual(pulse.busy_since, NOW - 1500)
        run(channel._heartbeat_tick())
        self.assertIn('"busy_since"', channel._test_sender.last)

    def test_another_bots_pulse_is_liveness_and_wakes_no_model(self):
        channel = live_channel()
        hear(channel, pulse_text("Codex", NOW))
        self.assertEqual(channel.presence().status("Codex", NOW), "up")
        self.assertEqual(channel._test_sender.sent, [])            # no turn, no answer
        self.assertGreaterEqual(channel.stats.machine_events, 1)

    def test_an_unreadable_pulse_still_counts_as_liveness(self):
        channel = live_channel()
        hear(channel, "[A2A v1.1] STATUS to=* intent=PULSE ref=Codex rev=abc1234\n\n{not json")
        self.assertEqual(channel.presence().status("Codex", NOW), "up")
        self.assertEqual(channel._test_sender.sent, [])


class TestDownOnTheChannel(unittest.TestCase):

    def test_a_peer_that_stops_is_down_once_and_back_once(self):
        channel = live_channel()
        hear(channel, pulse_text("Codex", NOW))
        channel._test_presence_clock["t"] = NOW + 400               # past 6 minutes of silence
        done = run(channel.check_peer_liveness())
        self.assertTrue(any(line.startswith("Codex: down") for line in done), done)
        self.assertEqual(len(admin_lines(channel, "Codex has been silent since")), 1)
        run(channel.check_peer_liveness())                          # still down, nothing more
        self.assertEqual(len(admin_lines(channel, "Codex has been silent since")), 1)
        hear(channel, pulse_text("Codex", NOW + 401), message_id=2)  # it pulses again
        done = run(channel.check_peer_liveness())
        self.assertEqual(done, ["Codex: back"])
        self.assertEqual(len(admin_lines(channel, "is back")), 1)
        run(channel.check_peer_liveness())                          # one return line, not a loop
        self.assertEqual(len(admin_lines(channel, "is back")), 1)

    def test_a_restart_does_not_declare_everyone_down(self):
        channel = live_channel()
        channel._test_presence_clock["t"] = NOW + 60                # one interval of grace
        self.assertEqual(channel.presence().status("Codex", NOW + 60), "unknown")
        self.assertEqual(run(channel.check_peer_liveness()), [])

    def test_status_and_swarm_show_liveness(self):
        channel = live_channel()
        hear(channel, pulse_text("Codex", NOW))
        channel._test_presence_clock["t"] = NOW + 400
        line = channel.presence_line()
        self.assertIn("peers=", line)
        self.assertIn("Codex:down(", line)
        self.assertIn("Antigravity:unknown", line)
        run(channel.handle_event(event("/status@HermesBot", author_id=ADMIN_ID, username="alice",
                                       is_bot=False, message_id=9)))
        self.assertIn("Codex:down(", channel._test_sender.last)
        self.assertIn("peers=", run(channel.membership.status_text()))

    def test_only_the_coordinator_announces(self):
        channel = live_channel()
        channel.membership.state.coordinator = "Claude"             # somebody else is leading
        hear(channel, pulse_text("Codex", NOW))
        channel._test_presence_clock["t"] = NOW + 400
        self.assertEqual(run(channel.check_peer_liveness()), [])
        self.assertEqual(admin_lines(channel, "Codex has been silent since"), [])

    def test_a_hand_off_to_a_down_peer_comes_straight_back(self):
        channel = live_channel()
        channel.bot = object()
        tasks = []
        channel._run_own_task = lambda text, chat, thread, name: tasks.append(text)
        channel._mirror_handoffs("@CodexBot please run the suite", GROUP, 307, 55)
        hear(channel, pulse_text("Codex", NOW))
        channel._test_presence_clock["t"] = NOW + 400
        run(channel.check_stalled_handoffs())
        self.assertEqual(channel._pending_handoffs, {})
        self.assertEqual(len(tasks), 1)
        self.assertIn("Codex is DOWN", tasks[0])
        self.assertEqual(admin_lines(channel, "has not answered the hand-off"), [])
