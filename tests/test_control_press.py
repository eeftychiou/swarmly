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
from unittest import mock

from swarmly.telegram import channel as channel_mod
from swarmly.telegram import control as ct
from swarmly.telegram import control_press as cp
from tests.test_control_center import FakeBot
from tests.test_telegram_channel import (ADMIN_ID, event, make_channel, make_config,
                                        run)

NOW = 1_800_000_000.0

#: The bot that posted a CONTROL note in these tests. A press lands on the transport that
#: holds the dashboard, so a note is only obeyed when a bot of the swarm sent it (#133).
SENDER = "Codex"


def buttons_of(markup):
    """``[(text, callback_data)]`` per row, for either aiogram's markup or the plain dict."""
    rows = markup["inline_keyboard"] if isinstance(markup, dict) else markup.inline_keyboard
    return [[((b["text"], b["callback_data"]) if isinstance(b, dict)
              else (b.text, b.callback_data)) for b in row] for row in rows]


def apply(channel, text, sender=SENDER):
    """Apply a peer's note the way the machine-note path does: with its sender named."""
    return channel.apply_control_note(text, sender=sender)


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
        self.assertEqual(apply(channel, self.note("bot.pause", "Hermes")),
                         "applied: bot.pause")
        self.assertTrue(channel._frozen)

    def test_a_note_for_another_bot_is_ignored(self):
        channel = self.channel()
        self.assertIn("not mine", apply(channel, self.note("bot.pause", "Codex")))
        self.assertFalse(channel._frozen)

    def test_a_note_from_a_stranger_is_refused(self):
        channel = self.channel()
        self.assertIn("not the admin",
                      apply(channel, self.note("bot.pause", "Hermes", by=42)))
        self.assertFalse(channel._frozen)

    def test_a_note_with_a_malformed_id_authorises_nothing(self):
        channel = self.channel()
        line = "CONTROL bot.pause target=Hermes by=nobody at=2027-01-15T08:00:00Z state=done"
        self.assertIn("not the admin", apply(channel, line))
        self.assertFalse(channel._frozen)

    def test_a_note_for_a_control_this_build_lacks_is_refused(self):
        channel = self.channel()
        self.assertIn("not a control here",
                      apply(channel, self.note("swarm.lease", "Hermes")))

    def test_the_note_arrives_through_the_machine_path_and_is_applied(self):
        """End to end: a peer posts `NOTE intent=CONTROL`; this bot holds without a turn."""
        from swarmly.telegram.wire import Envelope, MessageType, format_message
        channel = self.channel()
        channel.presence_clock = lambda: NOW
        payload = format_message(
            Envelope(type=MessageType.NOTE, to="*", intent="CONTROL", ref="HERMES"),
            self.note("bot.pause", "Hermes"))
        run(channel.handle_event(event(payload, author_id=303, username="CodexBot")))
        self.assertTrue(channel._frozen)

    def test_a_note_from_a_bot_outside_the_swarm_is_refused(self):
        """The header's author is checked too: `by=` is free text anyone could write."""
        from swarmly.telegram.wire import Envelope, MessageType, format_message
        channel = self.channel()
        channel.presence_clock = lambda: NOW
        payload = format_message(
            Envelope(type=MessageType.NOTE, to="*", intent="CONTROL", ref="HERMES"),
            self.note("bot.pause", "Hermes"))
        run(channel.handle_event(event(payload, author_id=303, username="SomebodyElseBot")))
        self.assertFalse(channel._frozen)

    def test_freeze_holds_the_bot_until_it_is_resumed(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:" + "bot.freeze")))
        asked = channel.control_policy().pending()                 # freeze asks twice
        self.assertTrue(asked)
        run(channel.handle_control_press(press("ctl:confirm:" + asked[0].code)))
        self.assertTrue(channel._frozen)
        run(channel.handle_control_press(press("ctl:bot.resume")))
        self.assertFalse(channel._frozen)


class TestStoppingEverything(unittest.TestCase):
    """`bot.stop_turn` from the dashboard: the `/stop all` form, behind a second tap."""

    def channel(self):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.control_round = lambda: None
        channel.presence_clock = lambda: NOW
        return channel

    def test_a_stop_press_stops_on_the_second_tap(self):
        channel = self.channel()
        asked = run(channel.handle_control_press(press("ctl:bot.stop_turn")))
        self.assertIn("Tap again", asked)
        self.assertEqual(channel._stop_generation, 0)          # the first tap stops nothing
        code = channel.control_policy().pending()[0].code
        done = run(channel.handle_control_press(press("ctl:confirm:" + code)))
        self.assertIn("Stop turn done", done)
        self.assertEqual(channel._stop_generation, 1)

    def test_the_stop_drops_what_queue_behind_the_turn(self):
        channel = self.channel()
        channel._busy_waiting = 3
        channel._cooldown_waiting[(1, None)] = 2
        said = channel._stop_every_turn()
        self.assertIn("5 queued message(s)", said)              # 3 waiting + 2 cooling down

    def test_a_stop_cancels_a_running_turn(self):
        import asyncio

        class Turn:
            done_flag = False

            def done(self):
                return self.done_flag

            def cancel(self):
                self.done_flag = True

        channel = self.channel()
        turn = Turn()
        channel._turns.add(turn)
        said = channel._stop_every_turn()
        self.assertIn("1 turn(s)", said)
        self.assertTrue(turn.done_flag)
        self.assertEqual(channel.stats.stopped, 1)
        asyncio.run(asyncio.sleep(0))

    def test_another_bot_applies_a_stop_note(self):
        channel = self.channel()
        line = "CONTROL bot.stop_turn target=Hermes by=%d at=2027-01-15T08:00:00Z state=done" % ADMIN_ID
        self.assertEqual(apply(channel, line), "applied: bot.stop_turn")
        self.assertEqual(channel._stop_generation, 1)


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
        self.assertEqual(apply(channel, line), "applied: bot.pause")
        self.assertTrue(channel._frozen)

    def test_a_broadcast_from_a_stranger_still_authorises_nothing(self):
        channel = self.channel()
        line = "CONTROL bot.pause target=* by=42 at=2027-01-15T08:00:00Z state=done"
        self.assertIn("not the admin", apply(channel, line))
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


class TestAPeerAppliesWhatItCanAndSaysWhatItCannot(unittest.TestCase):
    """The review's blocker on #133: a note that changed nothing must never read `applied`.

    `swarm.pause_all` had no branch in `_run_control_now`, so a peer answered
    ``applied: swarm.pause_all`` and stayed unfrozen -- a swarm-wide stop that stopped one bot
    and logged success on the rest.
    """

    def channel(self):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.control_round = lambda: None
        return channel

    def note(self, action: str, target: str = "*", by: int = ADMIN_ID) -> str:
        return cp.PressPolicy.log_line(action, target, str(by), NOW, done=True)

    def test_a_broadcast_pause_actually_holds_the_peer(self):
        channel = self.channel()
        self.assertEqual(apply(channel, self.note("swarm.pause_all")),
                         "applied: swarm.pause_all")
        self.assertTrue(channel._frozen)                      # the blocker: it used to stay up
        self.assertEqual(channel._freeze_ref, "pause")

    def test_a_control_this_bot_cannot_carry_out_says_failed(self):
        channel = self.channel()
        # `swarm.sync` is wired on the press path but there is nothing to apply from a note.
        self.assertIn("failed:", apply(channel, self.note("swarm.sync", "Hermes")))

    def test_one_press_posts_one_control_note(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:swarm.pause_all")))
        code = channel.control_policy().pending()[0].code
        run(channel.handle_control_press(press("ctl:confirm:" + code)))
        notes = [entry["text"] for entry in channel._test_sender.sent
                 if "intent=CONTROL" in entry["text"]]
        self.assertEqual(len(notes), 1)                       # the broadcast, not a second copy
        self.assertIn("target=*", notes[0])


class TestTheReportControls(unittest.TestCase):
    """Model, effort, WAKE and /cli: a press answers, and changes nothing (issue #117).

    A one-tap button cannot take the value a change would need, so these four *report*: what
    the setting is now, and the command that changes it. They answer through the same code the
    typed commands use, so a button never drifts from ``/model``, ``/reasoning`` or ``/wakes``.
    """

    def channel(self, **adapter):
        config = make_config(control={"chat_id": -1001234567890, "topic_id": 307},
                             adapter=dict({"type": "claude", "model": "claude-sonnet-4"},
                                          **adapter))
        channel = make_channel(config)
        channel.presence_clock = lambda: NOW
        channel.control_round = lambda: None
        return channel

    def test_a_press_on_model_offers_the_models_and_changes_nothing(self):
        channel = self.channel()
        channel.bot = FakeBot()
        with mock.patch.object(channel_mod, "cached_models",
                               return_value=["claude-sonnet-4", "claude-opus-4"]):
            text = run(channel.handle_control_press(press("ctl:bot.model")))
        self.assertIn("2 choice(s)", text)
        buttons = buttons_of(channel.bot.sent[0]["reply_markup"])[0]
        self.assertEqual([label for label, _ in buttons],
                         ["claude-sonnet-4", "claude-opus-4"])
        self.assertEqual(buttons[0][1], "ctl:bot.model::claude-sonnet-4")
        self.assertEqual(channel._models, {})                 # offering is not setting

    def test_a_press_on_effort_offers_the_levels(self):
        channel = self.channel(effort="high")
        channel.bot = FakeBot()
        run(channel.handle_control_press(press("ctl:bot.effort")))
        values = [data.rsplit(":", 1)[-1]
                  for _, data in buttons_of(channel.bot.sent[0]["reply_markup"])[0]]
        self.assertEqual(values, ["low", "medium", "high"])
        self.assertEqual(channel._efforts, {})                # nothing was changed

    def test_a_picker_with_nothing_to_offer_says_so(self):
        channel = self.channel()
        self.assertIn("nothing to pick",
                      run(channel.handle_control_press(press("ctl:bot.wake"))))

    def test_a_press_on_cli_lists_the_allowlist_and_runs_nothing(self):
        from swarmly.telegram import native as native_cli
        channel = self.channel()
        with mock.patch.object(native_cli, "available", return_value=""):
            text = run(channel.handle_control_press(press("ctl:bot.cli")))
        self.assertIn("/cli", text)
        self.assertIn(native_cli.commands_for("claude")[0], text)
        self.assertIn("/cli!", text)                          # a change is a different word

    def test_a_host_without_a_cli_says_so_instead_of_offering_one(self):
        channel = self.channel(type="echo")
        text = run(channel.handle_control_press(press("ctl:bot.cli")))
        self.assertIn("no /cli here", text)

    def test_a_report_costs_no_turn_and_writes_no_control_note(self):
        channel = self.channel()
        channel.adapter.respond = lambda message: self.fail("a report asked for a turn")
        run(channel.handle_control_press(press("ctl:bot.model")))
        self.assertEqual([entry for entry in channel._test_sender.sent
                          if "CONTROL" in entry["text"]], [])

    def test_a_stranger_cannot_read_them_either(self):
        channel = self.channel()
        self.assertIn("Only the admin",
                      run(channel.handle_control_press(press("ctl:bot.model", user=42))))

    def test_a_report_arriving_as_a_peers_note_is_refused(self):
        channel = self.channel()
        line = ("CONTROL bot.model target=Hermes by=%d at=2027-01-15T08:00:00Z state=done"
                % ADMIN_ID)
        self.assertIn("is a report, not a control", apply(channel, line))


class TestTheValueCarryingKeyboard(unittest.TestCase):
    """One mechanism for every picked value, inside Telegram's callback budget (#153).

    The value rides the button's callback data rather than a conversation state, so it needs
    no disk, survives a restart, and cannot drift from what the button said.
    """

    def channel(self, **adapter):
        config = make_config(control={"chat_id": -1001234567890, "topic_id": 307},
                             adapter=dict({"type": "claude", "model": "claude-sonnet-4"},
                                          **adapter))
        channel = make_channel(config)
        channel.presence_clock = lambda: NOW
        channel.control_round = lambda: None
        return channel

    # -- the wire format ---------------------------------------------------------------
    def test_a_value_written_last_and_read_back(self):
        data = cp.callback_data("bot.model", "", "claude-opus-4")
        self.assertEqual(cp.parse(data), cp.Command("bot.model", "", value="claude-opus-4"))

    def test_a_plain_button_keeps_the_data_it_always_had(self):
        self.assertEqual(cp.callback_data("bot.pause"), "ctl:bot.pause")
        self.assertEqual(cp.parse("ctl:bot.pause"), cp.Command("bot.pause"))
        self.assertEqual(cp.parse("ctl:bot.pause:Codex"), cp.Command("bot.pause", "Codex"))

    def test_the_twelve_longest_values_still_fit_telegrams_budget(self):
        for action in ct.PICKERS:
            for value in ("x" * cp.MAX_VALUE_CHARS, "claude-3-5-sonnet-latest"):
                data = cp.callback_data(action, "", value[:cp.MAX_VALUE_CHARS])
                self.assertFalse(cp.too_long(data), data)
                self.assertLessEqual(len(data.encode()), cp.MAX_CALLBACK_BYTES)

    def test_the_picker_keyboard_carries_values_and_no_identifier(self):
        rows = ct.picker("bot.model", [("opus", "claude-opus-4"), ("sonnet", "claude-sonnet-4")])
        self.assertEqual([row["callback_data"] for row in rows[0]],
                         ["ctl:bot.model::claude-opus-4", "ctl:bot.model::claude-sonnet-4"])

    # -- the press policy --------------------------------------------------------------
    def test_a_picked_value_always_asks_twice_even_for_a_report_control(self):
        row = policy(available=("bot.model",))
        first = row.decide(press("ctl:bot.model::claude-opus-4"))
        self.assertEqual(first.verdict, cp.CONFIRM)
        self.assertEqual(first.value, "claude-opus-4")
        self.assertEqual(first.log, "")                       # nothing has happened yet
        done = row.decide(press("ctl:confirm:" + row.pending()[0].code))
        self.assertEqual(done.verdict, cp.RUN)
        self.assertEqual(done.value, "claude-opus-4")
        self.assertIn("CONTROL bot.model", done.log)          # and it is on the record

    def test_a_stranger_cannot_pick_a_value_either(self):
        decision = policy(available=("bot.model",)).decide(
            press("ctl:bot.model::claude-opus-4", user=42))
        self.assertEqual(decision.reason, cp.NOT_ADMIN)

    def test_an_expired_code_does_not_apply_the_value(self):
        row = policy(available=("bot.model",))
        row.decide(press("ctl:bot.model::claude-opus-4"))
        code = row.pending()[0].code
        late = row.decide(press("ctl:confirm:" + code, at=NOW + cp.CONFIRM_SECONDS + 1))
        self.assertEqual(late.verdict, cp.REFUSED)
        self.assertEqual(late.value, "")

    # -- the transport -----------------------------------------------------------------
    def test_a_picked_model_is_set_by_the_same_code_the_typed_command_runs(self):
        channel = self.channel()
        asked = run(channel.handle_control_press(press("ctl:bot.model::claude-opus-4")))
        self.assertIn("claude-opus-4", asked)
        self.assertEqual(channel._models, {})                 # the first tap changed nothing
        code = channel.control_policy().pending()[0].code
        done = run(channel.handle_control_press(press("ctl:confirm:" + code)))
        self.assertIn("claude-opus-4", done)
        self.assertTrue(channel._models)                      # now it is set
        self.assertIn("claude-opus-4", str(next(iter(channel._models.values()))))

    def test_a_picked_effort_is_set_the_same_way(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.effort::xhigh")))
        code = channel.control_policy().pending()[0].code
        run(channel.handle_control_press(press("ctl:confirm:" + code)))
        self.assertEqual(list(channel._efforts.values()), ["xhigh"])

    def test_a_picked_value_costs_no_model_turn(self):
        channel = self.channel()
        channel.adapter.respond = lambda message: self.fail("a picker asked for a turn")
        run(channel.handle_control_press(press("ctl:bot.effort::high")))
        code = channel.control_policy().pending()[0].code
        run(channel.handle_control_press(press("ctl:confirm:" + code)))


class TestTheRolePick(unittest.TestCase):
    """Two values in one mechanism: which bot, then which role (#153).

    The bot rides the target slot and the role the value slot, so the whole pick still arrives
    in a single callback and the second tap confirms it exactly as a single-value pick does.
    """

    def channel(self):
        config = make_config(control={"chat_id": -1001234567890, "topic_id": 307})
        channel = make_channel(config)
        channel.bot = FakeBot()
        channel.presence_clock = lambda: NOW
        channel.control_round = lambda: None
        asked = []

        async def assign(handle, role, reason=""):
            asked.append((handle, role))
            return True

        channel.membership.assign = assign
        channel.assigned = asked
        return channel

    def test_the_first_press_offers_the_bots_and_names_none_of_them_yet(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:swarm.role")))
        labels = [pair[0] for line in buttons_of(channel.bot.sent[0]["reply_markup"])
                  for pair in line]
        self.assertEqual(labels, ["Antigravity", "Codex", "Hermes"])   # its own alias too

    def test_the_bot_choice_lands_in_the_target_slot(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:swarm.role")))
        data = [pair[1] for line in buttons_of(channel.bot.sent[0]["reply_markup"])
                for pair in line]
        self.assertEqual(data, ["ctl:swarm.role:Antigravity", "ctl:swarm.role:Codex",
                                "ctl:swarm.role:Hermes"])

    def test_the_second_press_offers_the_roles_for_that_bot(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:swarm.role:Codex")))
        data = [pair[1] for line in buttons_of(channel.bot.sent[0]["reply_markup"])
                for pair in line]
        self.assertIn("ctl:swarm.role:Codex:reviewer", data)
        self.assertIn("ctl:swarm.role:Codex:qa-tester", data)
        self.assertEqual(channel.assigned, [])                        # nothing assigned yet

    def test_the_complete_pick_asks_twice_and_then_assigns(self):
        channel = self.channel()
        asked = run(channel.handle_control_press(press("ctl:swarm.role:Codex:reviewer")))
        self.assertIn("Tap again", asked)
        self.assertEqual(channel.assigned, [])
        code = channel.control_policy().pending()[0].code
        done = run(channel.handle_control_press(press("ctl:confirm:" + code)))
        self.assertIn("Codex is now reviewer", done)
        self.assertEqual(channel.assigned, [("Codex", "reviewer")])
        notes = [entry["text"] for entry in channel._test_sender.sent
                 if "CONTROL swarm.role" in entry["text"]]
        self.assertEqual(len(notes), 1)                               # and it is on the record

    def test_a_refused_role_says_so_rather_than_pretending(self):
        channel = self.channel()

        async def refuse(handle, role, reason=""):
            return False

        channel.membership.assign = refuse
        run(channel.handle_control_press(press("ctl:swarm.role:Codex:reviewer")))
        code = channel.control_policy().pending()[0].code
        self.assertIn("refused",
                      run(channel.handle_control_press(press("ctl:confirm:" + code))))


class TestTheSecondTapIsDeliverable(unittest.TestCase):
    """The confirmation has to be *sendable*, not just describable (#153 review).

    The old words said "tap again", but pressing the first button again mints a new code and
    nothing offered ``ctl:confirm:<code>`` as a button, so an action that stops or changes
    something could never actually run. These pin both halves: the button that carries the
    minted code, and the typed form the answer also offers.
    """

    #: A control chat that is not the home channel, so a message sent to the wrong one shows up.
    CONTROL_CHAT = -1009876543210

    def channel(self, bus=None, **adapter):
        config = make_config(control={"chat_id": self.CONTROL_CHAT, "topic_id": 307},
                             adapter=dict({"type": "claude", "model": "claude-sonnet-4"},
                                          **adapter),
                             **({"bus": bus} if bus else {}))
        channel = make_channel(config)
        channel.bot = FakeBot()
        channel.presence_clock = lambda: NOW
        channel.control_round = lambda: None
        return channel

    def typed(self, channel, text, author=ADMIN_ID, is_bot=False):
        """The admin typing *text* into the chat: the real routing path, no callback."""
        return run(channel.handle_event(event(text, author_id=author, is_bot=is_bot,
                                              username="admin", message_id=9)))

    # -- the button --------------------------------------------------------------------
    def test_the_first_tap_posts_a_button_that_finishes_it(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.model::claude-opus-4")))
        code = channel.control_policy().pending()[0].code
        self.assertEqual(len(channel.bot.sent), 1)
        button = buttons_of(channel.bot.sent[0]["reply_markup"])[0][0]
        self.assertEqual(button[1], "ctl:confirm:" + code)
        self.assertIn("Confirm", button[0])
        self.assertEqual(channel._models, {})                 # still nothing changed
        done = run(channel.handle_control_press(press(button[1])))
        self.assertIn("claude-opus-4", done)
        self.assertTrue(channel._models)                      # the second tap ran it

    def test_a_stopping_control_offers_the_same_button(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.freeze")))
        code = channel.control_policy().pending()[0].code
        button = buttons_of(channel.bot.sent[0]["reply_markup"])[0][0]
        self.assertEqual(button[1], "ctl:confirm:" + code)
        self.assertFalse(channel._frozen)                      # one tap holds nothing
        run(channel.handle_control_press(press(button[1])))
        self.assertTrue(channel._frozen)                       # the button froze it

    def test_the_button_goes_where_the_dashboard_is(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.model::claude-opus-4")))
        posted = channel.bot.sent[0]
        self.assertEqual(posted["chat_id"], self.CONTROL_CHAT)
        self.assertEqual(posted.get("message_thread_id"), 307)

    def test_the_picker_goes_there_too_and_names_the_bot_it_changes(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:swarm.role:Codex")))
        posted = channel.bot.sent[0]
        self.assertEqual(posted["chat_id"], self.CONTROL_CHAT)
        self.assertEqual(posted.get("message_thread_id"), 307)
        self.assertIn("for Codex", posted["text"])
        self.assertNotIn("Hermes:", posted["text"])

    def test_the_role_pick_first_step_names_no_bot(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:swarm.role")))
        text = channel.bot.sent[0]["text"]
        self.assertIn("which bot", text)
        self.assertNotIn("for Hermes", text)

    def test_the_role_pick_reads_the_projects_own_role_files(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            roles = Path(root) / ".agents" / "roles"
            roles.mkdir(parents=True)
            (roles / "wrangler.md").write_text("A project role that only exists here.",
                                               encoding="utf-8")
            channel = self.channel(bus={"repo": root})
            run(channel.handle_control_press(press("ctl:swarm.role:Codex")))
        values = [pair[1] for line in buttons_of(channel.bot.sent[0]["reply_markup"])
                  for pair in line]
        self.assertIn("ctl:swarm.role:Codex:wrangler", values)

    def test_the_wake_pick_says_it_cancels(self):
        self.assertIn("cancel", ct.picker_text("bot.wake", "Hermes"))

    # -- the typed form ----------------------------------------------------------------
    def test_a_typed_code_runs_it_through_the_press_handler(self):
        channel = self.channel()
        channel.adapter.respond = lambda message: self.fail("a typed control asked for a turn")
        run(channel.handle_control_press(press("ctl:bot.model::claude-opus-4")))
        code = channel.control_policy().pending()[0].code
        self.assertEqual(channel._models, {})
        self.typed(channel, "ctl:confirm:" + code)
        self.assertTrue(channel._models)                      # the typed code was the tap

    def test_a_typed_code_from_a_stranger_does_nothing(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.model::claude-opus-4")))
        code = channel.control_policy().pending()[0].code
        self.typed(channel, "ctl:confirm:" + code, author=42)
        self.assertEqual(channel._models, {})                 # not run, and not answered

    def test_a_sentence_that_merely_starts_with_the_prefix_is_still_prose(self):
        channel = self.channel()
        for text in ("ctl is the prefix the controls use",
                     "ctl:confirm:ab12 never works, I tried it"):
            self.assertIsNone(run(channel._handle_control_text(
                event(text, author_id=ADMIN_ID, is_bot=False, username="admin",
                      message_id=9))), text)

    # -- the record --------------------------------------------------------------------
    def test_the_control_note_names_the_value_that_was_picked(self):
        channel = self.channel()
        run(channel.handle_control_press(press("ctl:bot.model::claude-opus-4")))
        code = channel.control_policy().pending()[0].code
        run(channel.handle_control_press(press("ctl:confirm:" + code)))
        notes = [entry["text"] for entry in channel._test_sender.sent
                 if "CONTROL bot.model" in entry["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("value=claude-opus-4", notes[0])
        self.assertIn("by=%d" % ADMIN_ID, notes[0])


class TestAStaleDashboardAnswersNothing(unittest.TestCase):
    """A press on a dashboard whose bot no longer coordinates is refused (review, #133).

    A failover retires the old pin, but an unpin can fail (a bot without rights), and the old
    keyboard then still sits in the chat. Its buttons must not answer "done" from a bot that
    is not leading any more.
    """

    def channel(self, leader="Claude"):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.control_round = lambda: None
        channel.current_coordinator = lambda now=None: leader
        return channel

    def test_a_press_on_a_bot_that_no_longer_coordinates_is_refused(self):
        channel = self.channel()
        answer = run(channel.handle_control_press(press("ctl:bot.pause")))
        self.assertIn("coordinates now", answer)
        self.assertFalse(channel._frozen)                          # and it did nothing
        self.assertEqual([entry for entry in channel._test_sender.sent
                          if "intent=CONTROL" in entry["text"]], [])

    def test_the_coordinator_itself_still_answers(self):
        channel = self.channel(leader="Hermes")
        self.assertIn("Pause", run(channel.handle_control_press(press("ctl:bot.pause"))))
        self.assertTrue(channel._frozen)

    def test_a_swarm_that_cannot_read_its_lease_still_answers(self):
        channel = self.channel(leader="")
        run(channel.handle_control_press(press("ctl:bot.pause")))
        self.assertTrue(channel._frozen)


class TestTheSwarmControls(unittest.TestCase):
    """Resync the swarm, and the cleanup dry run: the two that need no value (issue #117)."""

    def channel(self):
        channel = make_channel(make_config(control={"chat_id": -1001234567890, "topic_id": 307}))
        channel.presence_clock = lambda: NOW
        channel.control_round = lambda: None
        return channel

    def test_a_sync_press_resyncs_at_once(self):
        # Like pause and resume, a resync is not confirmed: it changes no bot's state, it only
        # asks the peers to say who they are again.
        channel = self.channel()
        self.assertIn("peers", run(channel.handle_control_press(press("ctl:swarm.sync"))))
        syncs = [entry["text"] for entry in channel._test_sender.sent if "intent=SYNC" in entry["text"]]
        self.assertEqual(len(syncs), 1)                       # one HELLO, from the press

    def test_a_cleanup_press_is_the_dry_run_and_deletes_nothing(self):
        channel = self.channel()
        text = run(channel.handle_control_press(press("ctl:swarm.cleanup")))
        self.assertIn("dry run", text)
        self.assertIn("nothing is deleted", text)

    def test_both_are_offered_because_both_work(self):
        channel = self.channel()
        self.assertIn("swarm.sync", channel.control_actions())
        self.assertIn("swarm.cleanup", channel.control_actions())


def round_state(items):
    from swarmly.telegram.rounds import RoundState
    state = RoundState(round="5", status="ok", term=4)
    state.items = dict(items)
    state.order = list(items)
    return state


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
