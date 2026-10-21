"""Peer liveness (issue #82): a heartbeat, and DOWN as an advisory fact.

What these pin is what the design review asked for: DOWN means silence on *both* planes, a busy
peer is never DOWN, a restart never turns "not heard yet" into "everyone is down", and the admin
notice happens once per episode.
"""

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
        self.assertIn("pulse posted rev=", said)
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


    def test_the_coordinator_returning_closes_its_own_episode(self):
        # S4: on the return branch the peer is UP again, so _may_announce_down() cannot see it;
        # the voice is "whoever announced the episode", which is this bot.
        channel = live_channel()
        channel.membership.state.coordinator = "Hermes"          # this bot leads
        hear(channel, pulse_text("Codex", NOW))
        channel._test_presence_clock["t"] = NOW + 400
        self.assertTrue(any(l.startswith("Codex: down") for l in
                            run(channel.check_peer_liveness())))
        hear(channel, pulse_text("Codex", NOW + 401), message_id=2)
        self.assertEqual(run(channel.check_peer_liveness()), ["Codex: back"])
        self.assertEqual(len(admin_lines(channel, "is back")), 1)

    def test_a_busy_peer_busy_past_the_window_goes_back_on_the_ladder(self):
        # S3: the busy exemption is bounded, so a perpetually busy peer still escalates.
        channel = live_channel(handoff_nudge_minutes=10)
        channel.bot = object()
        tasks = []
        channel._run_own_task = lambda text, chat, thread, name: tasks.append(text)
        channel._mirror_handoffs("@CodexBot please run the suite", GROUP, 307, 55)
        key = (int(GROUP), 307, "Codex")
        started = channel._pending_handoffs[key]["at"]
        mono = {"t": started}
        channel._clock = lambda: mono["t"]
        hear(channel, pulse_text("Codex", NOW, busy_since=NOW - 600))
        # 30 minutes of a peer that keeps saying it is busy: past 4 x down_after the ladder runs.
        channel._test_presence_clock["t"] = NOW + 300
        mono["t"] = started + 1800
        run(channel.check_stalled_handoffs())
        self.assertEqual(admin_lines(channel, "reminder: the hand-off above") != [], True)

    def test_a_busy_peer_gets_no_reminder_and_the_clock_restarts(self):
        # Round 3: the reminder landed on a DeepSeek that was 20 minutes into a turn. A pulse
        # carrying `busy_since` says "working", so the hand-off clock starts again instead.
        channel = live_channel(handoff_nudge_minutes=10)
        channel.bot = object()
        tasks = []
        channel._run_own_task = lambda text, chat, thread, name: tasks.append(text)
        channel._mirror_handoffs("@CodexBot please run the suite", GROUP, 307, 55)
        key = (int(GROUP), 307, "Codex")
        channel._test_sender.sent.clear()
        started = channel._pending_handoffs[key]["at"]
        mono = {"t": started}                               # the hand-off ladder's clock
        channel._clock = lambda: mono["t"]
        hear(channel, pulse_text("Codex", NOW, busy_since=NOW - 1200))
        channel._test_presence_clock["t"] = NOW + 60        # still pulsing: not DOWN
        mono["t"] = started + 700.0                         # past the 10 min nudge
        run(channel.check_stalled_handoffs())
        self.assertEqual(admin_lines(channel, "reminder: the hand-off above"), [])
        self.assertEqual(tasks, [])
        self.assertIn(key, channel._pending_handoffs)       # still owed, not handed back
        self.assertEqual(channel._pending_handoffs[key]["at"], started + 700.0)
        self.assertIn("Codex:busy(", channel.presence_line())

class TestAcrossARestart(unittest.TestCase):
    """Restart grace is not persistence (#82 review): the facts are saved, not just re-guessed."""

    def test_the_facts_survive_a_restart(self):
        table = pr.Presence(1000.0, down_after=360, grace=120)
        table.note(pr.Pulse(alias="Codex", at=1000.0, busy_since=900.0, rev="abc1234", term=2))
        saved = json.loads(json.dumps(table.to_dict(1100.0)))
        back = pr.Presence.restore(saved, now=1150.0, down_after=360, grace=120)
        self.assertEqual(back.status("Codex", 1150.0), "busy")
        self.assertEqual(back.peers["Codex"].busy_since, 900.0)
        self.assertEqual(back.peers["Codex"].rev, "abc1234")
        self.assertEqual(back.peers["Codex"].term, 2)

    def test_a_down_verdict_and_its_notice_survive(self):
        table = pr.Presence(1000.0, down_after=360, grace=120)
        table.seen("Codex", 1000.0)
        self.assertTrue(table.mark_notified("Codex", 1400.0))
        back = pr.Presence.restore(table.to_dict(1400.0), now=1420.0, down_after=360, grace=120)
        # A stop shorter than one interval missed no pulse: the verdict stands, and so does
        # the notice it already claimed.
        self.assertTrue(back.is_down("Codex", 1420.0))
        self.assertFalse(back.mark_notified("Codex", 1420.0))

    def test_a_long_stop_judges_again_instead_of_declaring_the_swarm_dead(self):
        table = pr.Presence(1000.0, down_after=360, grace=120)
        table.seen("Codex", 1000.0)
        # Four hours later: this bot was not listening, so it cannot call that silence DOWN.
        back = pr.Presence.restore(table.to_dict(1000.0), now=15_400.0, down_after=360,
                                   grace=120)
        self.assertFalse(back.is_down("Codex", 15_400.0))
        self.assertEqual(back.status("Codex", 15_400.0), "up")
        self.assertEqual(len(back.peers), 1)                   # the peer is still known

    def test_unusable_state_starts_fresh(self):
        for data in (None, [], {"peers": 5}, {"peers": {"Codex": "nope"}}, {"saved_at": "x"}):
            table = pr.Presence.restore(data, now=1000.0, down_after=360, grace=120)
            self.assertEqual(table.peers, {})


class TestPersistenceOnTheChannel(unittest.TestCase):

    def restart(self, path, *, at):
        channel = live_channel()
        channel._presence_state_path = lambda: path
        channel._test_presence_clock["t"] = at
        return channel

    def test_a_peer_heard_before_a_restart_is_known_after_it(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "presence.json"
            first = self.restart(path, at=NOW)
            hear(first, pulse_text("Codex", NOW))
            self.assertTrue(path.exists())
            second = self.restart(path, at=NOW + 60)
            self.assertEqual(second.presence().status("Codex", NOW + 60), "up")
            self.assertIn("Codex:up", second.presence_line())

    def test_a_down_episode_is_not_announced_twice_after_a_restart(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "presence.json"
            first = self.restart(path, at=NOW)
            hear(first, pulse_text("Codex", NOW))
            first._test_presence_clock["t"] = NOW + 400
            self.assertEqual(run(first.check_peer_liveness()), ["Codex: down since "
                                                               + time.strftime(
                                                                   "%H:%M", time.localtime(NOW))])
            second = self.restart(path, at=NOW + 420)           # a stop shorter than a pulse
            second._test_sender.sent.clear()
            self.assertEqual(run(second.check_peer_liveness()), [])   # still DOWN, already said
            self.assertEqual(admin_lines(second, "Codex has been silent since"), [])
            self.assertIn("Codex:down(", second.presence_line())

class TestTheBusyWindowAndTheClock(unittest.TestCase):
    """Review B2 and S2 on #94: busy_since is liveness, and last_seen is what *we* saw."""

    def test_a_busy_peer_that_stops_pulsing_is_busy_until_the_window_expires(self):
        # A blocked event loop (a synchronous git/gh call, a GC pause) stops the pulse without
        # stopping the work: six minutes of silence must not hand its work to somebody else.
        table = pr.Presence(1000.0, down_after=360, grace=120)
        table.note(pr.Pulse(alias="Codex", at=1000.0, busy_since=900.0))
        self.assertFalse(table.is_down("Codex", 1000.0 + 20 * 60))
        self.assertEqual(table.status("Codex", 1000.0 + 20 * 60), "busy")
        # ...but the claim expires, so a bot that wedged while busy still surfaces as DOWN.
        self.assertTrue(table.is_down("Codex", 1000.0 + 25 * 60))
        self.assertEqual(table.status("Codex", 1000.0 + 25 * 60), "down")

    def test_a_pulse_stamped_in_the_future_does_not_keep_a_peer_up(self):
        table = pr.Presence(1000.0, down_after=360, grace=120)
        table.note(pr.Pulse(alias="Codex", at=1000.0 + 3600), seen_at=1000.0)
        self.assertEqual(table.peers["Codex"].last_seen, 1000.0)   # the receiver's clock wins
        self.assertTrue(table.is_down("Codex", 1000.0 + 400))

    def test_a_future_stamp_on_the_channel_is_judged_by_our_clock(self):
        channel = live_channel()
        hear(channel, pulse_text("Codex", NOW + 3600))
        channel._test_presence_clock["t"] = NOW + 400
        self.assertTrue(channel.presence().is_down("Codex", NOW + 400))

class FakeEditingBot:
    """The two Bot API calls the status message uses: edit it, and delete a replaced one."""

    def __init__(self, *, editable=True):
        self.editable = editable
        self.edits = []
        self.deleted = []

    async def edit_message_text(self, *, text, chat_id, message_id, **_kwargs):
        if not self.editable:
            raise RuntimeError("Bad Request: message to edit not found")
        self.edits.append((chat_id, message_id, text))

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class TestThePulseOnTelegram(unittest.TestCase):
    """A heartbeat does not belong on GitHub: by default each bot edits one status message."""

    def channel(self, bot):
        from types import SimpleNamespace
        channel = live_channel()
        channel.bot = bot
        recorder = channel._sender
        ids = iter(range(500, 600))

        async def sender(payload, reply_to_message_id=None):
            await recorder(payload, reply_to_message_id)
            return SimpleNamespace(message_id=next(ids))   # Telegram's id for the new message

        channel._sender = sender
        return channel

    def test_the_bus_is_not_used_by_default(self):
        channel = live_channel()
        channel._pulsebus = FakePulseBus()
        channel._pulsebus_checked = True
        run(channel._heartbeat_tick())
        self.assertEqual(channel._pulsebus.published, [])
        self.assertIn("STATUS to=* intent=PULSE", channel._test_sender.last)

    def test_the_first_beat_posts_and_the_next_ones_edit_it(self):
        bot = FakeEditingBot()
        channel = self.channel(bot)
        self.assertIn("pulse posted", run(channel._heartbeat_tick()))
        channel._test_presence_clock["t"] = NOW + 120
        self.assertIn("pulse edited", run(channel._heartbeat_tick()))
        self.assertIn("pulse edited", run(channel._heartbeat_tick()))
        self.assertEqual(len(channel._test_sender.sent), 1)      # one line in the group, ever
        self.assertEqual([edit[1] for edit in bot.edits], [500, 500])
        self.assertIn("STATUS to=* intent=PULSE", bot.edits[-1][2])

    def test_a_status_message_that_is_gone_is_posted_again(self):
        bot = FakeEditingBot()
        channel = self.channel(bot)
        run(channel._heartbeat_tick())
        bot.editable = False                                    # /cleanup deleted it
        self.assertIn("pulse posted", run(channel._heartbeat_tick()))
        self.assertEqual(len(channel._test_sender.sent), 2)
        self.assertEqual(bot.deleted, [(channel.config.identity.home_channel_id, 500)])
        bot.editable = True
        run(channel._heartbeat_tick())
        self.assertEqual(bot.edits[-1][1], 501)

    def test_a_peers_edited_pulse_is_its_heartbeat(self):
        from types import SimpleNamespace
        channel = live_channel()
        message = SimpleNamespace(
            text=pulse_text("Codex", NOW), caption=None, message_id=9, reply_to_message=None,
            chat=SimpleNamespace(id=channel.config.identity.home_channel_id),
            from_user=SimpleNamespace(id=202, username="CodexBot", is_bot=True),
            is_topic_message=False, message_thread_id=None)
        run(channel._on_aiogram_edited(message))
        self.assertEqual(channel.presence().status("Codex", NOW), "up")

    def test_an_edited_message_that_is_not_a_pulse_does_nothing(self):
        from types import SimpleNamespace
        channel = live_channel()
        message = SimpleNamespace(
            text="@HermesBot please merge #12", caption=None, message_id=9,
            reply_to_message=None,
            chat=SimpleNamespace(id=channel.config.identity.home_channel_id),
            from_user=SimpleNamespace(id=202, username="CodexBot", is_bot=True),
            is_topic_message=False, message_thread_id=None)
        run(channel._on_aiogram_edited(message))
        self.assertNotIn("Codex", channel.presence().peers)
        self.assertEqual(channel._test_sender.sent, [])


class TestThePulseCarriesUsage(unittest.TestCase):
    """The pulse says what the bot spent, read from its ledger: the heartbeat costs no tokens."""

    def test_usage_since_the_last_beat_and_today(self):
        from swarmly.telegram import usage as usage_mod
        channel = live_channel()
        ledger = usage_mod.Ledger(None, alias="Hermes", clock=lambda: NOW)
        ledger.append(usage_mod.Entry(alias="Hermes", started=NOW - 60, ended=NOW - 30,
                                      tokens_in=1200, tokens_out=300, cost_usd=0.02))
        ledger.append(usage_mod.Entry(alias="Hermes", started=NOW - 3 * 3600, ended=NOW - 3 * 3600,
                                      tokens_in=5000, tokens_out=1000, cost_usd=0.1))
        channel._usage_ledger = ledger
        status = channel.own_pulse(NOW).status
        self.assertEqual(status["usage"]["beat"]["tokens_in"], 1200)
        self.assertEqual(status["usage"]["beat"]["turns"], 1)
        # The next beat counts only what came after this one.
        later = channel.own_pulse(NOW + 120).status
        self.assertEqual(later["usage"]["beat"]["turns"], 0)
        self.assertIsNone(later["usage"]["beat"]["tokens_in"])

    def test_a_beat_never_runs_a_model_turn(self):
        class CountingAdapter:
            model, effort = "m", "e"
            calls = 0

            async def respond(self, message):
                CountingAdapter.calls += 1

        channel = live_channel()
        channel.adapter = CountingAdapter()
        run(channel._heartbeat_tick())
        from types import SimpleNamespace
        message = SimpleNamespace(
            text=pulse_text("Codex", NOW), caption=None, message_id=9, reply_to_message=None,
            chat=SimpleNamespace(id=channel.config.identity.home_channel_id),
            from_user=SimpleNamespace(id=202, username="CodexBot", is_bot=True),
            is_topic_message=False, message_thread_id=None)
        run(channel._on_aiogram_edited(message))
        hear(channel, pulse_text("Codex", NOW), message_id=10)
        self.assertEqual(CountingAdapter.calls, 0)


class FakePulseBus:
    """A pulse ref, without a git repo: publish() records, read() returns the others."""

    def __init__(self, *, push_ok=True, pulses=None):
        self.push_ok = push_ok
        self.pulses = dict(pulses or {})
        self.published = []

    def publish(self, pulse):
        self.published.append(pulse)
        if self.push_ok:
            self.pulses[pulse.alias] = pulse.to_dict()
        return self.push_ok

    def read(self):
        return {alias: record for alias, record in self.pulses.items() if alias != "Hermes"}


class TestThePulseOnTheBus(unittest.TestCase):
    """Review B1 on #94: when the bus is on, the pulse is written there, not to Telegram."""

    def channel_with(self, bus):
        channel = live_channel(heartbeat_plane="bus")
        channel._pulsebus = bus
        channel._pulsebus_checked = True
        return channel

    def test_the_pulse_goes_to_the_ref_and_not_to_telegram(self):
        bus = FakePulseBus(pulses={"Codex": pr.Pulse(alias="Codex", at=NOW).to_dict()})
        channel = self.channel_with(bus)
        said = run(channel._heartbeat_tick())
        self.assertIn("pulse ref", said)
        self.assertIn("peers=1", said)                     # Codex was read back from its ref
        self.assertEqual(channel._test_sender.sent, [])     # nothing on Telegram while the bus is on
        self.assertEqual([p.alias for p in bus.published], ["Hermes"])
        self.assertEqual(channel.presence().status("Codex", NOW), "up")

    def test_a_failed_push_falls_back_to_the_telegram_line(self):
        bus = FakePulseBus(push_ok=False)
        channel = self.channel_with(bus)
        said = run(channel._heartbeat_tick())
        self.assertIn("pulse posted rev=", said)
        self.assertIn("STATUS to=* intent=PULSE", channel._test_sender.last)

    def test_a_transport_with_no_config_has_no_bus(self):
        # A unit test or an ad-hoc run has no checkout to write a ref into: Telegram carries it.
        channel = live_channel()
        self.assertIsNone(channel.pulse_bus())
        self.assertIn("pulse posted rev=", run(channel._heartbeat_tick()))

class BlockingPulseBus:
    """A pulse bus whose git calls block, as a slow git subprocess does (review B4)."""

    def __init__(self, gate):
        self.gate = gate

    def publish(self, pulse):
        self.gate.wait(5.0)
        return True

    def read(self):
        self.gate.wait(5.0)
        return {}


class TestThePulseDoesNotBlockTheTransport(unittest.TestCase):

    def test_a_beat_does_not_stall_the_event_loop(self):
        # B4 on #94: publish()/read() are git subprocesses. Inline they would freeze this
        # pulse, every turn and Telegram delivery for as long as git takes, so they run in a
        # worker thread -- and another coroutine must keep making progress while they block.
        gate = threading.Event()
        channel = live_channel()
        channel._pulsebus = BlockingPulseBus(gate)
        channel._pulsebus_checked = True
        progress = {"ticks": 0}

        async def scenario():
            async def other_work():
                while not gate.is_set():
                    progress["ticks"] += 1
                    await asyncio.sleep(0)
            helper = asyncio.ensure_future(other_work())
            beat = asyncio.ensure_future(channel._heartbeat_tick())
            await asyncio.sleep(0.05)
            seen = progress["ticks"]
            gate.set()
            await asyncio.wait_for(beat, 5)
            await asyncio.wait_for(helper, 5)
            return seen

        self.assertGreater(run(scenario()), 0)      # the loop ran while the bus was blocked
