"""The buttons under the dashboard: who may press, when it asks twice, and the log (issue #117).

What these pin: a non-admin press is refused before the action is even looked up, a control
that stops or changes something runs only on a second tap with its one-use code, a code dies
when it is used or when its window closes, an action this build cannot perform is refused
instead of silently doing nothing, and every action that runs leaves one line saying who
stopped what and when.
"""

from __future__ import annotations

import asyncio
import unittest

from swarmly.telegram import control as ct
from swarmly.telegram import control_press as cp
from tests.test_telegram_channel import (ADMIN_ID, event, make_channel, make_config,
                                        run)

NOW = 1_800_000_000.0


def policy(available=("bot.pause", "bot.resume", "bot.stop_turn"), admin=ADMIN_ID):
    return cp.PressPolicy(admin_id=admin, clock=lambda: NOW,
                          confirm_seconds=cp.CONFIRM_SECONDS,
                          code=lambda: "a1b2", available=available)


def press(data: str, user: int = ADMIN_ID, at: float = NOW) -> cp.Press:
    return cp.Press(user_id=user, data=data, at=at)


class TestParsing(unittest.TestCase):
    def test_callback_data_round_trips(self):
        self.assertEqual(cp.parse(cp.callback_data("bot.pause")), cp.Command("bot.pause"))
        self.assertEqual(cp.parse(cp.callback_data("bot.pause", "Codex")),
                         cp.Command("bot.pause", target="Codex"))

    def test_someone_elses_keyboard_is_not_ours(self):
        self.assertIsNone(cp.parse("v2:something"))
        self.assertIsNone(cp.parse("ctl:"))
        self.assertIsNone(cp.parse(""))


class TestWhoMayPress(unittest.TestCase):
    def test_the_admin_press_runs(self):
        decision = policy().decide(press("ctl:bot.resume"))
        self.assertEqual(decision.verdict, cp.RUN)
        self.assertEqual(decision.action, "bot.resume")
        self.assertIn("CONTROL bot.resume", decision.log)

    def test_a_stranger_is_refused_before_the_action_is_looked_up(self):
        decision = policy().decide(press("ctl:bot.nonexistent", user=42))
        self.assertEqual(decision.verdict, cp.REFUSED)
        self.assertEqual(decision.reason, cp.NOT_ADMIN)      # not "unknown action"
        self.assertNotIn("nonexistent", decision.text)        # and learns nothing about it

    def test_a_missing_user_id_is_not_the_admin(self):
        self.assertEqual(policy().decide(press("ctl:bot.resume", user=0)).reason, cp.NOT_ADMIN)


class TestTheSecondTap(unittest.TestCase):
    def test_a_stopping_control_only_asks_the_first_time(self):
        decision = policy().decide(press("ctl:bot.stop_turn"))
        self.assertEqual(decision.verdict, cp.CONFIRM)
        self.assertIn("Tap again", decision.text)
        self.assertIn("a1b2", decision.text)                  # the code it will accept
        self.assertEqual(decision.log, "")                    # nothing happened yet

    def test_the_code_runs_it_once(self):
        row = policy()
        row.decide(press("ctl:bot.stop_turn"))
        done = row.decide(press("ctl:confirm:a1b2"))
        self.assertEqual(done.verdict, cp.RUN)
        self.assertEqual(done.action, "bot.stop_turn")
        self.assertIn("by=%d" % ADMIN_ID, done.log)
        self.assertIn("at=2027-01-15T08:00:00Z", done.log)
        again = row.decide(press("ctl:confirm:a1b2"))
        self.assertEqual(again.verdict, cp.REFUSED)           # one use only
        self.assertEqual(again.reason, cp.EXPIRED)

    def test_a_code_that_goes_stale_is_refused(self):
        row = policy()
        row.decide(press("ctl:bot.stop_turn"))
        late = row.decide(press("ctl:confirm:a1b2", at=NOW + cp.CONFIRM_SECONDS + 1))
        self.assertEqual(late.verdict, cp.REFUSED)
        self.assertEqual(late.reason, cp.EXPIRED)

    def test_a_press_that_changes_nothing_needs_no_second_tap(self):
        decision = policy().decide(press("ctl:bot.resume"))
        self.assertEqual(decision.verdict, cp.RUN)
        self.assertEqual(policy().pending(), [])


class TestWhatThisBuildCanDo(unittest.TestCase):
    def test_an_unwired_control_is_refused_not_silently_ignored(self):
        decision = policy().decide(press("ctl:bot.restart"))
        self.assertEqual(decision.verdict, cp.REFUSED)
        self.assertEqual(decision.reason, cp.UNKNOWN)

    def test_a_typo_is_refused(self):
        decision = policy().decide(press("ctl:bot.pasue"))
        self.assertEqual(decision.reason, cp.UNKNOWN)


class TestTheKeyboard(unittest.TestCase):
    def test_only_the_controls_it_is_given_are_offered(self):
        rows = ct.keyboard(["bot.pause", "bot.resume"])
        flat = [button for row in rows for button in row]
        self.assertEqual([button["text"] for button in flat], ["Pause", "Resume"])
        self.assertEqual(flat[0]["callback_data"], "ctl:bot.pause")

    def test_a_deploy_or_merge_button_is_never_offered(self):
        rows = ct.keyboard([row.action for row in ct.CONTROLS])
        labels = " ".join(button["text"].lower() for row in rows for button in row)
        for forbidden in ("deploy", "upgrade", "merge", "secret"):
            self.assertNotIn(forbidden, labels)

    def test_buttons_wrap_three_to_a_row(self):
        rows = ct.keyboard(["bot.pause", "bot.resume", "bot.model", "bot.effort"])
        self.assertEqual([len(row) for row in rows], [3, 1])


class TestTheChannelActs(unittest.TestCase):
    def channel(self):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.control_round = lambda: None
        return channel

    def test_an_admin_pause_press_freezes_the_bot(self):
        channel = self.channel()
        text = run(channel.handle_control_press(press("ctl:bot.pause")))
        self.assertTrue(channel._frozen)
        self.assertIn("Pause done", text)

    def test_a_strangers_press_changes_nothing(self):
        channel = self.channel()
        text = run(channel.handle_control_press(press("ctl:bot.pause", user=42)))
        self.assertFalse(channel._frozen)
        self.assertIn("Only the admin", text)

    def test_the_press_is_logged_as_a_note(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.resume")))
        notes = [entry["text"] for entry in channel._test_sender.sent
                 if "CONTROL bot.resume" in entry["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("by=%d" % ADMIN_ID, notes[0])

    def test_a_restart_press_runs_only_on_its_second_tap(self):
        channel = self.channel()
        asked = run(channel.handle_control_press(press("ctl:bot.restart")))
        self.assertIn("Tap again", asked)
        self.assertFalse(channel.restart_requested)          # the first tap changes nothing
        code = channel.control_policy().pending()[0].code
        done = run(channel.handle_control_press(press("ctl:confirm:" + code)))
        self.assertIn("Restart done", done)
        self.assertTrue(channel.restart_requested)


class TestTheAiogramCallback(unittest.TestCase):
    """The transport's own entry point: a press answered in the chat, then the dashboard."""

    def channel(self):
        from tests.test_control_center import FakeBot
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.control_round = lambda: None
        channel.bot = FakeBot()
        return channel

    @staticmethod
    def callback(data: str, answered: list, user: int = ADMIN_ID):
        from types import SimpleNamespace

        async def answer(text=""):
            answered.append(text)

        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user), answer=answer)

    def test_an_admin_press_is_answered_and_the_dashboard_refreshed(self):
        channel = self.channel()
        answered = []
        run(channel._on_aiogram_callback(self.callback("ctl:bot.pause", answered)))
        self.assertEqual(answered, ["Pause done."])
        self.assertTrue(channel._frozen)
        self.assertTrue(channel.bot.sent[-1]["text"].startswith(ct.MARKER))

    def test_a_strangers_press_is_answered_with_the_refusal(self):
        channel = self.channel()
        answered = []
        run(channel._on_aiogram_callback(self.callback("ctl:bot.pause", answered, user=42)))
        self.assertIn("Only the admin", answered[0])
        self.assertFalse(channel._frozen)

    def test_another_keyboard_is_not_answered(self):
        channel = self.channel()
        answered = []
        run(channel._on_aiogram_callback(self.callback("v2:elsewhere", answered)))
        self.assertEqual(answered, [])
        self.assertEqual(channel.bot.sent, [])


class TestAnotherBotsControl(unittest.TestCase):
    """A press meant for another bot travels as the note the press already posts (#117)."""

    def channel(self):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.control_round = lambda: None
        return channel

    def note(self, action: str, target: str, by: int = ADMIN_ID) -> str:
        return cp.PressPolicy.log_line(action, target, str(by), NOW, done=True)

    def test_a_press_for_another_bot_does_not_act_here(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.pause:Codex")))
        self.assertFalse(channel._frozen)                    # not this bot's pause
        notes = [entry["text"] for entry in channel._test_sender.sent
                 if "CONTROL bot.pause target=Codex" in entry["text"]]
        self.assertEqual(len(notes), 1)                      # but it is on the record

    def test_the_note_is_read_as_a_command_only_in_its_exact_shape(self):
        self.assertEqual(cp.parse_note(self.note("bot.pause", "Codex")),
                         cp.Note("bot.pause", "Codex", str(ADMIN_ID)))
        self.assertIsNone(cp.parse_note("CONTROL is not a command, it is a word"))
        self.assertIsNone(cp.parse_note("the admin pressed CONTROL bot.pause"))

    def test_this_bot_applies_a_note_addressed_to_it(self):
        channel = self.channel()
        self.assertEqual(channel.apply_control_note(self.note("bot.pause", "Hermes")),
                         "applied: bot.pause")
        self.assertTrue(channel._frozen)

    def test_a_note_for_another_bot_is_ignored(self):
        channel = self.channel()
        self.assertIn("not mine", channel.apply_control_note(self.note("bot.pause", "Codex")))
        self.assertFalse(channel._frozen)

    def test_a_note_from_a_stranger_is_refused(self):
        channel = self.channel()
        self.assertIn("not the admin",
                      channel.apply_control_note(self.note("bot.pause", "Hermes", by=42)))
        self.assertFalse(channel._frozen)

    def test_a_note_with_a_malformed_id_authorises_nothing(self):
        channel = self.channel()
        line = "CONTROL bot.pause target=Hermes by=nobody at=2027-01-15T08:00:00Z state=done"
        self.assertIn("not the admin", channel.apply_control_note(line))
        self.assertFalse(channel._frozen)

    def test_a_note_for_a_control_this_build_lacks_is_refused(self):
        channel = self.channel()
        self.assertIn("not a control here",
                      channel.apply_control_note(self.note("swarm.cleanup", "Hermes")))

    def test_the_note_arrives_through_the_machine_path_and_is_applied(self):
        """End to end: a peer posts `NOTE intent=CONTROL`; this bot holds without a turn."""
        from swarmly.telegram.wire import Envelope, MessageType, format_message
        channel = self.channel()
        channel.presence_clock = lambda: NOW
        payload = format_message(
            Envelope(type=MessageType.NOTE, to="*", intent="CONTROL", ref="HERMES"),
            self.note("bot.pause", "Hermes"))
        run(channel.handle_event(event(payload, author_id=303, username="ClaudeBot")))
        self.assertTrue(channel._frozen)

    def test_freeze_holds_the_bot_until_it_is_resumed(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:" + "bot.freeze")))
        asked = channel.control_policy().pending()                 # freeze asks twice
        self.assertTrue(asked)
        run(channel.handle_control_press(press("ctl:confirm:" + asked[0].code)))
        self.assertTrue(channel._frozen)
        run(channel.handle_control_press(press("ctl:bot.resume")))
        self.assertFalse(channel._frozen)


class TestSwarmWideAndNudge(unittest.TestCase):
    """The controls that reach past one bot: pause everything, and nudge an item's owner."""

    def channel(self, items=None):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.presence_clock = lambda: NOW
        state = round_state(items or {})
        channel.control_round = lambda: state
        return channel

    def test_pause_all_holds_this_bot_and_broadcasts_the_hold(self):
        channel = self.channel()
        asked = run(channel.handle_control_press(press("ctl:swarm.pause_all")))
        self.assertIn("Tap again", asked)
        self.assertFalse(channel._frozen)                     # the first tap changes nothing
        code = channel.control_policy().pending()[0].code
        done = run(channel.handle_control_press(press("ctl:confirm:" + code)))
        self.assertIn("Pause all done", done)
        self.assertTrue(channel._frozen)
        lines = [entry["text"] for entry in channel._test_sender.sent
                 if "CONTROL swarm.pause_all" in entry["text"]]
        self.assertTrue(any("target=*" in line for line in lines))   # every bot applies it
        self.assertTrue(any("by=%d" % ADMIN_ID in line for line in lines))

    def test_another_bot_applies_a_broadcast(self):
        channel = self.channel()
        line = "CONTROL bot.pause target=* by=%d at=2027-01-15T08:00:00Z state=done" % ADMIN_ID
        self.assertEqual(channel.apply_control_note(line), "applied: bot.pause")
        self.assertTrue(channel._frozen)

    def test_a_broadcast_from_a_stranger_still_authorises_nothing(self):
        channel = self.channel()
        line = "CONTROL bot.pause target=* by=42 at=2027-01-15T08:00:00Z state=done"
        self.assertIn("not the admin", channel.apply_control_note(line))
        self.assertFalse(channel._frozen)

    def test_a_nudge_names_the_owner_and_the_item(self):
        channel = self.channel({"129": {"issue": 129, "owner": "Codex", "pr": 129}})
        text = run(channel.handle_control_press(press("ctl:item.nudge:129")))
        self.assertIn("Nudge done", text)
        nudges = [entry["text"] for entry in channel._test_sender.sent
                  if "carry on with #129" in entry["text"]]
        self.assertEqual(len(nudges), 1)
        self.assertIn("@CodexBot", nudges[0])                 # the peer's handle, not the alias

    def test_a_nudge_for_an_item_with_no_owner_says_so(self):
        channel = self.channel({"131": {"issue": 131}})
        text = run(channel.handle_control_press(press("ctl:item.nudge:131")))
        self.assertIn("no owner", text)

    def test_a_nudge_for_an_item_the_round_does_not_have_says_so(self):
        channel = self.channel({})
        self.assertIn("no owner", run(channel.handle_control_press(press("ctl:item.nudge:999"))))


def round_state(items):
    from swarmly.telegram.rounds import RoundState
    state = RoundState(round="5", status="ok", term=4)
    state.items = dict(items)
    state.order = list(items)
    return state


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
