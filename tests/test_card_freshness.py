"""A card that shows the bot's real state, without waiting for its next beat (#450).

The admin's report, 27 Oct: Antigravity said ``intent=RESUMED`` at 21:45 and the dashboard
still drew it paused at 21:47, so he pressed Resume again; and an effort pick pressed twice
never reached the card. Four things, one per acceptance criterion:

1. a pause, a resume or a pick asks for a beat at once, so the peers see it in seconds -- the
   beat is still the authority, and a burst of changes is one pulse;
2. a peer's own ``STATUS`` line moves its row before that beat arrives;
3. the card shows the model and effort the bot's *next* turn runs, which on a bot with a
   control topic is the setting stored under that topic (#374), not General's;
4. a pick that changes nothing answers "already set" and leaves no CONTROL line behind.
"""

from __future__ import annotations

import unittest
from typing import Any

from swarmly.telegram import control_press as cp
from swarmly.telegram import snapshot as snapshot_mod
from tests.aio import run
from tests.test_control_press import ADMIN_ID, live, named_bot
from tests.test_presence import hear, live_channel

NOW = 1_800_000_000.0


def ask(channel: Any, **request: Any) -> Any:
    return run(channel.dashboard_action(request))


def press_twice(channel: Any, **request: Any) -> Any:
    """The two taps of a pick: the policy's code first, then the code itself."""
    asked = ask(channel, **request)
    assert asked["verdict"] == cp.CONFIRM, asked
    return ask(channel, code=asked["confirm"])


class TestAChangeAsksForABeat(unittest.TestCase):
    """Acceptance 1: the state change, not the interval, decides when the pulse goes out."""

    def watched(self, channel: Any) -> list:
        asked: list = []
        channel._beat_soon = lambda reason="": (asked.append(reason or ""), True)[1]
        return asked

    def test_a_pause_and_a_resume_each_ask_for_one(self):
        channel = named_bot("DeepSeek")
        asked = self.watched(channel)
        self.assertEqual(channel._pause_self(None), "paused")     # idle: paused now
        self.assertEqual(asked, ["paused"])
        self.assertTrue(channel._resume_self())
        self.assertEqual(asked[-1], "resumed")

    def test_a_pause_that_changes_nothing_asks_for_nothing(self):
        channel = named_bot("DeepSeek")
        channel._pause_self(None)
        asked = self.watched(channel)
        self.assertEqual(channel._pause_self(None), "already")
        self.assertEqual(asked, [])

    def test_a_pick_that_changed_the_effort_asks_for_one(self):
        channel = named_bot("DeepSeek", adapter="claude")
        asked = self.watched(channel)
        answer = press_twice(channel, action="bot.effort", value="high")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(asked, ["a picked effort"])

    def test_a_burst_of_changes_is_one_beat(self):
        """A swarm-wide pause is one change told to ten bots: not ten pulses."""
        channel = named_bot("DeepSeek")
        ticks: list = []

        async def tick(*, now: Any = None) -> str:
            ticks.append(now)
            return "pulse"

        channel._heartbeat_tick = tick

        async def go() -> tuple:
            first = channel._beat_soon("first")
            second = channel._beat_soon("second")
            await channel._beat_soon_task
            return first, second

        first, second = run(go())
        self.assertTrue(first)
        self.assertFalse(second)                   # inside the window: coalesced
        self.assertEqual(len(ticks), 1)

    def test_the_window_reopens(self):
        channel = named_bot("DeepSeek")
        ticks: list = []

        async def tick(*, now: Any = None) -> str:
            ticks.append(now)
            return "pulse"

        channel._heartbeat_tick = tick

        async def go() -> int:
            channel._beat_soon("first")
            await channel._beat_soon_task
            channel._beat_soon_at = 0.0            # the window is over
            channel._beat_soon("second")
            await channel._beat_soon_task
            return len(ticks)

        self.assertEqual(run(go()), 2)

    def test_with_no_loop_there_is_nothing_to_beat_on(self):
        """A test or a boot has no loop: asking must answer False, not raise."""
        channel = named_bot("DeepSeek")
        self.assertFalse(channel._beat_soon("nowhere to run"))


class TestAPeersOwnLine(unittest.TestCase):
    """Acceptance 2: the RESUMED/FROZEN line moves the row; the beat stays the authority."""

    def test_a_resumed_line_stops_the_card_saying_paused(self):
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        live(channel, "Antigravity", state=snapshot_mod.PAUSED)
        self.assertEqual(channel.peer_state("Antigravity"), snapshot_mod.PAUSED)
        self.assertTrue(channel.peer_state_line("Antigravity", "RESUMED"))
        self.assertEqual(channel.peer_state("Antigravity"), snapshot_mod.IDLE)

    def test_a_frozen_line_says_paused(self):
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        live(channel, "Antigravity")
        self.assertTrue(channel.peer_state_line("Antigravity", "FROZEN"))
        self.assertEqual(channel.peer_state("Antigravity"), snapshot_mod.PAUSED)

    def test_the_next_beat_still_wins(self):
        """The beat is the authority: a line is only the row, until the peer's own beat says."""
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        live(channel, "Antigravity", state=snapshot_mod.PAUSED, age=30.0)
        channel.peer_state_line("Antigravity", "RESUMED")
        self.assertEqual(channel.peer_state("Antigravity"), snapshot_mod.IDLE)
        live(channel, "Antigravity", state=snapshot_mod.PAUSED)     # a newer beat
        self.assertEqual(channel.peer_state("Antigravity"), snapshot_mod.PAUSED)

    def test_an_early_line_never_overwrites_a_newer_beat(self):
        """The row keeps the peer's own ``at``, so a beat that lands afterwards replaces it."""
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        live(channel, "Antigravity", state=snapshot_mod.PAUSED, age=30.0)
        before = dict(channel.presence().peers["Antigravity"].snapshot or {})
        channel.peer_state_line("Antigravity", "RESUMED")
        after = channel.presence().peers["Antigravity"].snapshot or {}
        self.assertEqual(after.get("at"), before.get("at"))
        self.assertEqual(after.get("state"), snapshot_mod.IDLE)

    def test_a_line_that_changes_nothing_says_so(self):
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        live(channel, "Antigravity", state=snapshot_mod.IDLE)
        self.assertFalse(channel.peer_state_line("Antigravity", "RESUMED"))

    def test_a_bot_nobody_has_heard_from_gains_no_row(self):
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        self.assertFalse(channel.peer_state_line("Antigravity", "RESUMED"))
        self.assertEqual(channel.peer_state("Antigravity"), "")

    def test_a_resumed_line_on_the_wire_moves_the_row(self):
        """The path the admin's report took: the peer's STATUS line, read as it arrives."""
        channel = live_channel()
        live(channel, "Codex", state=snapshot_mod.PAUSED)
        self.assertEqual(channel.peer_state("Codex"), snapshot_mod.PAUSED)
        hear(channel, "[A2A v1.1] STATUS to=* intent=RESUMED ref=pause rev=abc1234\n\n"
                      "\u25b6 Codex resumed.")
        self.assertEqual(channel.peer_state("Codex"), snapshot_mod.IDLE)

    def test_a_pulse_says_nothing_about_state(self):
        """A busy bot and an idle one pulse the same way: PULSE must not move a row."""
        channel = named_bot("Claudebot", peers={"Antigravity": "AntigravityBot"})
        live(channel, "Antigravity", state=snapshot_mod.BUSY)
        self.assertFalse(channel.peer_state_line("Antigravity", "PULSE"))
        self.assertEqual(channel.peer_state("Antigravity"), snapshot_mod.BUSY)


class TestTheCardShowsWhatTheNextTurnRuns(unittest.TestCase):
    """Acceptance 3: the pick's own key (#374), not General's."""

    def test_the_control_topics_effort_is_the_one_on_the_card(self):
        channel = named_bot("DeepSeek", adapter="claude")     # control topic 307
        key = channel._pick_key()
        self.assertNotEqual(key, "general")                   # a control topic is configured
        channel._efforts["general"] = "low"
        channel._efforts[key] = "max"
        snapshot = channel.status_snapshot(NOW)
        self.assertEqual(snapshot.effort, "max")
        self.assertEqual(snapshot.model, "opus" if channel._models.get(key) == "opus" else
                         snapshot.model)

    def test_a_pick_set_on_the_control_topic_reaches_the_card(self):
        """The whole path the admin's report took: a pick, then the card's own record."""
        channel = named_bot("DeepSeek", adapter="claude")
        answer = press_twice(channel, action="bot.effort", value="high")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(channel.status_snapshot(NOW).effort, "high")

    def test_generals_setting_still_shows_when_the_pick_topic_has_none(self):
        channel = named_bot("DeepSeek", adapter="claude")
        channel._efforts["general"] = "medium"
        self.assertEqual(channel.status_snapshot(NOW).effort, "medium")

    def test_a_bot_with_no_control_topic_reads_general_as_before(self):
        # A dashboard in a DM: no topics at all, so a pick is General's, as it always was.
        channel = named_bot("DeepSeek", adapter="claude",
                            control={"chat_id": 4242, "topic_id": 307})
        self.assertEqual(channel._pick_key(), "general")


class TestAPickThatChangesNothing(unittest.TestCase):
    """Acceptance 4: "already high", and no CONTROL line for a press that did nothing."""

    def notes(self, channel: Any) -> list:
        return [entry["text"] for entry in channel._test_sender.sent
                if "CONTROL bot.effort" in entry["text"]]

    def test_the_second_press_of_the_same_effort_says_it_was_already_set(self):
        channel = named_bot("DeepSeek", adapter="claude")
        first = press_twice(channel, action="bot.effort", value="high")
        self.assertTrue(first["ok"], first)
        self.assertEqual(len(self.notes(channel)), 1)
        again = press_twice(channel, action="bot.effort", value="high")
        self.assertIn("already", again["text"])
        self.assertIn("high", again["text"])
        # And no second CONTROL line: the record says nothing changed because nothing did.
        self.assertEqual(len(self.notes(channel)), 1)

    def test_a_refused_value_is_not_called_already_set(self):
        """A model this bot cannot run is a refusal, never "already" -- the page must not lie."""
        channel = named_bot("DeepSeek", adapter="claude")
        answer = press_twice(channel, action="bot.model", value="not-a-model")
        self.assertIn("not a Claude model", answer["text"])
        self.assertNotIn("already", answer["text"])
        self.assertNotIn(channel._pick_key(), channel._models)      # nothing was stored

    def test_a_forwarded_pick_for_the_value_the_peer_reports_is_not_sent(self):
        channel = named_bot("Claudebot", peers={"Hermes": "HermesBot"}, adapter="claude")
        live(channel, "Hermes", effort="high")
        refused = press_twice(channel, action="bot.effort", target="Hermes", value="high")
        self.assertFalse(refused["ok"], refused)
        self.assertIn("already", refused["text"])
        self.assertEqual([line for line in channel._test_sender.sent
                          if "CONTROL bot.effort" in line["text"]], [])

    def test_a_forwarded_pick_that_changes_the_value_is_still_sent(self):
        channel = named_bot("Claudebot", peers={"Hermes": "HermesBot"}, adapter="claude")
        live(channel, "Hermes", effort="low")
        sent = press_twice(channel, action="bot.effort", target="Hermes", value="high")
        self.assertTrue(sent["ok"], sent)
        self.assertIn("sent", sent["text"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
