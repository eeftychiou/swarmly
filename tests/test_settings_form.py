"""One settings form per bot, one Apply, one press (#451).

The admin's complaint, 27 Oct: setting a model or an effort took two presses, and the second
one only happened because nothing seemed to have happened. The form is one pick per setting,
prefilled with what the bot runs now, and one **Apply** that sends *only* the fields that
changed -- no second tap, one CONTROL line, and an answer that names every field it set and
every field it refused.

The other half of the issue is what the form may offer: a setting is exposed when the adapter
maps it to a flag for a *turn* (`config.EFFORT_FLAGS`, `config.MODEL_TYPES`). Everything else
in `adapter` is read when the CLI is launched, so a dropdown for it would be a lie about when
it takes effect.
"""

from __future__ import annotations

import unittest
from typing import Any

from swarmly.telegram import config as config_mod
from swarmly.telegram import control as ct
from swarmly.telegram import control_press as cp
from swarmly.telegram import webui
from tests.aio import run
from tests.test_control_press import live, named_bot
from tests.test_telegram_channel import make_channel, make_config

NOW = 1_800_000_000.0


def ask(channel: Any, **request: Any) -> Any:
    return run(channel.dashboard_action(request))


def apply_once(channel: Any, **request: Any) -> Any:
    """One press of Apply: settings take no second tap, so the answer is the run's own."""
    answer = ask(channel, **request)
    assert answer.get("verdict") != cp.CONFIRM, answer
    return answer


class TestTheSettingsTable(unittest.TestCase):
    """What a form may offer, per adapter -- and why the rest is not there."""

    def test_the_form_offers_the_settings_a_turn_takes_a_flag_for(self):
        self.assertEqual(ct.SETTING_KEYS, ("model", "effort"))
        for key, action in ct.SETTING_ACTIONS:
            self.assertEqual(ct.control(action).action, action)
            self.assertEqual(ct.setting_action(key), action)

    def test_every_offered_setting_is_one_the_adapters_really_map(self):
        """A dropdown whose value no CLI flag carries would be a setting nothing reads."""
        self.assertIn("effort", ct.SETTING_KEYS)
        for adapter in ("claude", "codex", "antigravity", "reasonix"):
            # Each of these maps an effort onto its own flag; codex spells it differently and
            # refuses max, which is the command's own answer, not the form's business.
            self.assertIn(adapter, config_mod.EFFORT_FLAGS, adapter)
            self.assertIsNone(config_mod.effort_problem(adapter, "high"), adapter)
        # And a model: exactly the adapter types whose command line takes one.
        for adapter in ("claude", "codex", "reasonix", "antigravity"):
            self.assertIn(adapter, config_mod.MODEL_TYPES, adapter)
        # Nothing else in `adapter` is per turn: cwd, command, permissions and timeout are
        # read when the CLI is launched, so a form for them would be lying about effect.
        self.assertNotIn("cwd", ct.SETTING_KEYS)
        self.assertNotIn("permissions", ct.SETTING_KEYS)

    def test_a_value_carries_only_the_pairs_it_was_given(self):
        self.assertEqual(ct.settings_value([("model", "opus"), ("effort", "high")]),
                         "model=opus;effort=high")
        self.assertEqual(ct.settings_value([("model", ""), ("effort", "high")]), "effort=high")
        self.assertEqual(ct.parse_settings("effort=high"), [("effort", "high")])
        # A pair that is not a pair is dropped, never guessed at; an unknown key is kept so
        # the answer can name it back.
        self.assertEqual(ct.parse_settings("effort;=high;model="), [])
        self.assertEqual(ct.parse_settings("nonsense=1"), [("nonsense", "1")])


class TestTheFormOnThePage(unittest.TestCase):
    """Acceptance: one form per card, prefilled, one Apply, and only what changed is sent."""

    def page(self) -> str:
        return webui.page_html()

    def test_the_view_carries_each_bots_fields_and_choices(self):
        view = webui.controls_view({"ready": True, "self": "DeepSeek", "controls": [
            {"action": "bot.settings", "label": "Apply", "where": "bot",
             "settings": {"DeepSeek": [{"key": "effort", "label": "Effort", "value": "high",
                                        "choices": [["low", "low"], ["high", "high"]]}],
                          "Codex": [{"key": "model", "label": "Model", "value": "opus",
                                     "choices": [["opus", "opus"]]}]}}]})
        row = view["controls"][0]
        self.assertEqual(sorted(row["settings"]), ["Codex", "DeepSeek"])
        self.assertEqual(row["settings"]["DeepSeek"][0]["value"], "high")
        self.assertEqual(row["settings"]["DeepSeek"][0]["choices"],
                         [{"label": "low", "value": "low"},
                          {"label": "high", "value": "high"}])
        # A field with no key, or choices that are not pairs, is dropped rather than drawn.
        view = webui.controls_view({"ready": True, "controls": [
            {"action": "bot.settings", "where": "bot",
             "settings": {"X": [{"label": "no key"}, {"key": "effort", "choices": ["oops"]}]}}]})
        fields = view["controls"][0]["settings"]["X"]
        self.assertEqual([field["key"] for field in fields], ["effort"])
        self.assertEqual(fields[0]["choices"], [])

    def test_the_card_draws_one_form_with_a_dropdown_per_field(self):
        page = self.page()
        self.assertIn("function settingsForm(button, target, fields, box) {", page)
        self.assertIn("(button.settings || {})[target || \"\"]", page)
        self.assertIn('select.dataset.for = button.action + ":" + field.key;', page)
        # Prefilled with what that bot runs now, and never blank when it has said.
        self.assertIn("const chosen = kept === null ? field.value : kept;", page)
        self.assertIn("if (chosen) select.value = chosen;", page)
        # One Apply, and it is the same press path everything else uses.
        self.assertIn('press(button.action, target || "", changed.join(";"));', page)
        self.assertIn('toast("nothing changed: pick a new value first", "bad");', page)

    def test_only_the_changed_field_is_sent(self):
        page = self.page()
        # The value that goes out is built from the fields that differ from what the bot runs.
        self.assertIn("if (value && value !== read.field.value) changed.push("
                      "read.field.key + \"=\" + value);", page)
        # And an untouched form sends nothing at all.
        self.assertIn("if (!changed.length) {", page)

    def test_a_chosen_value_survives_the_redraw_until_the_bot_reports_it(self):
        """#449's machinery, keyed per field: a kept pick goes when the bot really runs it."""
        page = self.page()
        self.assertIn('const key = pendingKey(target, button.action + ":" + field.key);', page)
        self.assertIn("let kept = pendingGet(key);", page)
        self.assertIn("if (kept !== null && kept === field.value) kept = null;", page)


class TestOnePressOneLine(unittest.TestCase):
    """Acceptance: no second tap, one CONTROL line, and every field named in the answer."""

    def notes(self, channel: Any) -> list:
        return [entry["text"] for entry in channel._test_sender.sent
                if "CONTROL bot.settings" in entry["text"]]

    def test_one_apply_sets_every_field_it_sends_with_no_second_tap(self):
        channel = named_bot("DeepSeek", adapter="claude")
        answer = apply_once(channel, action="bot.settings",
                            value="model=opus;effort=high")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(channel._models.get(channel._pick_key()), "opus")
        self.assertEqual(channel._efforts.get(channel._pick_key()), "high")
        self.assertIn("model → opus", answer["text"])
        self.assertIn("effort → high", answer["text"])
        self.assertEqual(len(self.notes(channel)), 1)          # one press, one line

    def test_the_line_names_the_fields_that_were_sent(self):
        channel = named_bot("DeepSeek", adapter="claude")
        apply_once(channel, action="bot.settings", value="effort=high")
        line = self.notes(channel)[0]
        self.assertIn("value=effort=high", line)
        self.assertIn("state=done", line)

    def test_a_field_that_was_already_set_is_named_as_such(self):
        channel = named_bot("DeepSeek", adapter="claude")
        apply_once(channel, action="bot.settings", value="effort=high")
        again = apply_once(channel, action="bot.settings", value="effort=high")
        self.assertIn("already", again["text"])
        # Nothing changed, so nothing is recorded: no second CONTROL line (#450).
        self.assertEqual(len(self.notes(channel)), 1)

    def test_the_answer_names_the_fields_that_changed_and_the_ones_refused(self):
        channel = named_bot("DeepSeek", adapter="claude")
        answer = apply_once(channel, action="bot.settings",
                            value="model=not-a-model;effort=high")
        self.assertTrue(answer["ok"], answer)
        self.assertIn("effort → high", answer["text"])          # the one that worked
        self.assertIn("model:", answer["text"])                 # the one that did not
        self.assertIn("not a Claude model", answer["text"])     # in the command's own words
        self.assertEqual(channel._efforts.get(channel._pick_key()), "high")

    def test_a_setting_this_build_does_not_have_is_named_not_ignored(self):
        channel = named_bot("DeepSeek", adapter="claude")
        answer = apply_once(channel, action="bot.settings", value="temperature=0.2")
        self.assertIn("temperature", answer["text"])
        self.assertIn("not a setting", answer["text"])

    def test_a_form_that_parses_to_nothing_is_refused_not_logged_as_done(self):
        """A value with no field in it is nothing to apply, not an Apply that "ran"."""
        channel = named_bot("DeepSeek", adapter="claude")
        answer = apply_once(channel, action="bot.settings", value=";;=")
        self.assertFalse(answer["ok"], answer)
        self.assertIn("nothing to apply", answer["text"])
        self.assertEqual(self.notes(channel), [])

    def test_a_bare_press_answers_what_the_settings_are(self):
        """A typed ``ctl:bot.settings:DeepSeek`` reads the settings; it sets nothing."""
        channel = named_bot("DeepSeek", adapter="claude")
        channel._efforts[channel._pick_key()] = "high"
        self.assertIn("effort high", channel.settings_report())
        answer = apply_once(channel, action="bot.settings")
        self.assertIn("effort high", answer["text"])
        self.assertEqual(self.notes(channel), [])

    def test_the_form_is_not_on_the_chat_keyboard(self):
        """One form of dropdowns cannot be a keypad: the page draws it, the chat does not."""
        from swarmly.telegram import control as control_mod
        self.assertIn("bot.settings", control_mod.page_actions())
        buttons = [button for row in control_mod.keyboard(("bot.settings", "bot.pause"))
                   for button in row]
        self.assertEqual([button["text"] for button in buttons], ["Pause"])


class TestTheFormForAPeer(unittest.TestCase):
    """A card's form is that bot's: its own list, its own value, and the press is forwarded."""

    def notes(self, channel: Any) -> list:
        return [entry["text"] for entry in channel._test_sender.sent
                if "CONTROL bot.settings" in entry["text"]]

    def test_a_peers_form_offers_that_bots_list_and_value(self):
        channel = named_bot("Claudebot", peers={"Hermes": "HermesBot"}, adapter="claude")
        live(channel, "Hermes", effort="high", efforts=("low", "high"))
        fields = {row["key"]: row for row in channel.settings_fields("Hermes")}
        self.assertEqual(fields["effort"]["value"], "high")
        self.assertEqual([choice[1] for choice in fields["effort"]["choices"]],
                         ["low", "high"])
        # This bot's own form is its own: never the peer's list shown for it.
        mine = {row["key"]: row for row in channel.settings_fields()}
        self.assertNotEqual(mine["effort"]["value"], "high")

    def test_a_peers_apply_is_forwarded_not_set_here(self):
        channel = named_bot("Claudebot", peers={"Hermes": "HermesBot"}, adapter="claude")
        live(channel, "Hermes", effort="low")
        answer = apply_once(channel, action="bot.settings", target="Hermes",
                            value="effort=high")
        self.assertTrue(answer["ok"], answer)
        self.assertIn("sent", answer["text"])
        self.assertEqual(channel._efforts, {})                 # nothing was set here
        self.assertEqual(len(self.notes(channel)), 1)

    def test_a_repeated_form_for_a_peer_is_not_sent(self):
        channel = named_bot("Claudebot", peers={"Hermes": "HermesBot"}, adapter="claude")
        live(channel, "Hermes", effort="high")
        answer = apply_once(channel, action="bot.settings", target="Hermes",
                            value="effort=high")
        self.assertFalse(answer["ok"], answer)
        self.assertIn("already", answer["text"])
        self.assertEqual(self.notes(channel), [])

    def test_the_peer_applies_every_field_it_is_sent(self):
        peer = named_bot("Hermes", peers={"Claudebot": "ClaudebotBot"}, adapter="claude")
        said = peer._run_control_now("bot.settings", "model=opus;effort=high")
        self.assertTrue(said)
        self.assertEqual(peer._models.get(peer._pick_key()), "opus")
        self.assertEqual(peer._efforts.get(peer._pick_key()), "high")

    def test_a_peer_that_already_runs_it_says_so_rather_than_claiming_applied(self):
        peer = named_bot("Hermes", peers={"Claudebot": "ClaudebotBot"}, adapter="claude")
        peer._run_control_now("bot.settings", "effort=high")
        again = peer._run_control_now("bot.settings", "effort=high")
        self.assertIsInstance(again, str)
        self.assertIn("already", again)


class TestTheSettingsViewOnThePage(unittest.TestCase):
    """The row reaches the page from the channel's own table, by name."""

    def test_the_page_row_carries_the_form_of_every_card(self):
        channel = make_channel(make_config(agent={"alias": "DeepSeek", "name": "DeepSeekBot"},
                                           control={"chat_id": -100, "topic_id": 307}))
        view = channel.web_controls()
        row = next(item for item in view["controls"] if item["action"] == "bot.settings")
        self.assertTrue(row["settings"])
        self.assertIn("DeepSeek", row["settings"])
        fields = {field["key"]: field for field in row["settings"]["DeepSeek"]}
        self.assertEqual(sorted(fields), ["effort", "model"])
        self.assertIn("choices", fields["model"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
