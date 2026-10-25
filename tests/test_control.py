"""The command centre's dashboard text (#117): alerts first, ids never, unknown stays unknown.

What these pin: the alert set a reader must see before anything else, a message that fits
Telegram's 4096 characters by dropping detail rather than alerts, a record carrying ids or
secrets that renders none of them, the button table's confirm rule, and a render that needs
no adapter at all -- the dashboard is a function of records the heartbeat already took.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict

from swarmly.telegram import control as ct
from swarmly.telegram.rounds import RoundState
from swarmly.telegram.snapshot import (BUSY, DOWN, FROZEN, IDLE, PAUSED, STUCK, Snapshot)

NOW = 1_700_000_000.0


def _imported_modules(path: str) -> set:
    """Every module name this file imports, at any level of nesting (issue #117)."""
    import ast

    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def snap(alias: str, state: str = IDLE, **fields: Any) -> Snapshot:
    base: Dict[str, Any] = dict(alias=alias, state=state, since=NOW - 300, at=NOW - 5,
                                every=60.0, topic="tg-307", model="deepseek", effort="high",
                                rev="b571641")
    base.update(fields)
    return Snapshot(**base)


def usage(**fields: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(turns=4, tokens_in=1200, tokens_out=340, tokens_in_known=4,
                                tokens_out_known=4, cost_usd=0.42, cost_known=4,
                                quota_left=90000, quota_resets_at=NOW + 3600)
    base.update(fields)
    return base


def _row(text: str, alias: str) -> str:
    """The one card row for *alias*: a row is recognised by its alias cell, not by its start."""
    for line in text.splitlines():
        if f" {alias} · " in line:
            return line
    raise AssertionError(f"no row for {alias} in:\n{text}")


def round_state(**items: Dict[str, Any]) -> RoundState:
    state = RoundState(round="5", status="ok", term=4)
    state.items = dict(items)
    state.order = list(items)
    return state


class TestAlerts(unittest.TestCase):
    def test_a_stale_snapshot_is_a_down_alert_not_a_current_picture(self):
        lines = ct.alerts(now=NOW, snapshots=[snap("Hermes", at=NOW - 600, every=60.0)])
        self.assertEqual(len(lines), 1)
        self.assertIn("DOWN Hermes", lines[0])
        self.assertIn("10m", lines[0])                     # how long it has been quiet

    def test_a_peer_that_reported_down_says_down(self):
        lines = ct.alerts(now=NOW, snapshots=[snap("Codex", DOWN)])
        self.assertIn("DOWN Codex", lines[0])

    def test_a_stuck_turn_is_an_alert_with_both_numbers(self):
        lines = ct.alerts(now=NOW, snapshots=[snap("DeepSeek", STUCK, turn_elapsed=700,
                                                   turn_timeout=600)])
        self.assertIn("STUCK DeepSeek", lines[0])
        self.assertIn("12m over 10m", lines[0])

    def test_a_frozen_bot_and_a_dead_quota_are_alerts(self):
        lines = ct.alerts(now=NOW, snapshots=[snap("Claude", FROZEN)],
                          usage={"Claude": usage(quota_left=0)})
        self.assertTrue(any("FROZEN Claude" in line for line in lines))
        self.assertTrue(any("QUOTA Claude" in line for line in lines))

    def test_a_paused_bot_is_reported_but_below_a_stuck_one(self):
        lines = ct.alerts(now=NOW, snapshots=[snap("Claude", PAUSED), snap("Codex", STUCK,
                                                                          turn_elapsed=700,
                                                                          turn_timeout=600)])
        self.assertIn("STUCK Codex", lines[0])
        self.assertTrue(any("PAUSED Claude" in line for line in lines))

    def test_an_unanswered_handoff_is_an_alert(self):
        state = round_state(**({
            "129": {"issue": 129, "owner": "DeepSeek", "waits_on": "Codex", "pr": 129},
        }))
        lines = ct.alerts(now=NOW, round_state=state)
        self.assertTrue(any("HANDOFF #129: waits on Codex" in line for line in lines))

    def test_a_healthy_swarm_has_no_alerts(self):
        self.assertEqual(ct.alerts(now=NOW, snapshots=[snap("Hermes"), snap("Codex")],
                                   usage={"Hermes": usage()}), [])


class TestHeaderAndBoard(unittest.TestCase):
    def test_the_header_carries_the_round_the_items_and_todays_spend(self):
        state = round_state(**{
            "124": {"issue": 124, "state": "merged"},
            "125": {"issue": 125, "state": "open"},
        })
        text = ct.header(now=NOW, round_state=state, usage=ct._sum_usage({"Claude": usage()}))
        self.assertIn("round=5", text)
        self.assertIn("items=1/2", text)
        self.assertIn("today 4 turns", text)
        self.assertIn("1.5k tok", text)                    # both directions, one total
        self.assertIn("$0.42", text)
        # The time of the last refresh, so a stale pin is recognised without a second look.
        self.assertIn("refreshed 22:13Z", text)
        self.assertNotIn("coord=", text)                   # the lease has its own line now

    def test_an_unreadable_round_is_not_reported_as_empty(self):
        state = RoundState(round="5", status="unreadable", reason="fetch failed")
        text = ct.header(now=NOW, round_state=state)
        self.assertIn("state=unreadable", text)
        self.assertNotIn("items=0/0", text)

    def test_the_lease_line_names_the_holder_its_term_and_how_long_it_holds(self):
        line = ct.lease_line(now=NOW, coordinator="Claude",
                             lease=snap("Claude", term=4, lease_ends=NOW + 600))
        self.assertIn("lease: Claude", line)
        self.assertIn("term=4", line)
        self.assertIn("10m left", line)
        self.assertEqual(ct.lease_line(now=NOW), "lease: ?")   # nobody named is not a guess

    def test_the_board_shows_each_item_and_whether_it_can_merge(self):
        state = round_state(**{
            "129": {"issue": 129, "pr": 129, "owner": "DeepSeek", "head": "7c8ae60aaaa",
                    "ci": "7/7", "review": {"verdict": "merge", "sha": "7c8ae60aaaa"},
                    "qa": {"verdict": "pass", "sha": "7c8ae60aaaa"}},
            "131": {"issue": 131, "pr": 132, "owner": "DeepSeek", "head": "01dcec4bbbb",
                    "ci": "7/7", "review": {"verdict": "merge", "sha": "01dcec4bbbb"}},
        })
        lines = ct.board(state)
        self.assertIn("#129 DeepSeek PR#129 7c8ae60", lines[0])
        self.assertIn("ready", lines[0])
        self.assertIn("#131", lines[1])
        self.assertIn("blocked: qa open", lines[1])

    def test_a_verdict_on_another_head_is_not_a_verdict(self):
        state = round_state(**{
            "129": {"issue": 129, "pr": 129, "owner": "DeepSeek", "head": "7c8ae60",
                    "ci": "7/7", "review": {"verdict": "merge", "sha": "e64ea3b"},
                    "qa": {"verdict": "pass", "sha": "7c8ae60"}},
        })
        lines = ct.board(state)
        self.assertIn("review=open", lines[0])
        self.assertIn("blocked: review open", lines[0])


class TestNoLeaks(unittest.TestCase):
    def test_a_record_carrying_ids_or_secrets_renders_none_of_them(self):
        """Fields are picked by name, so a chat id, a token or a path never reaches the text."""
        state = round_state(**{
            "129": {"issue": 129, "pr": 129, "owner": "DeepSeek", "head": "7c8ae60",
                    "ci": "7/7",
                    "chat_id": -1001234567890123, "thread_id": 1336,
                    "token": "123456:AAH-secret", "path": r"C:\Users\eftyc\secret",
                    "note": "chat 1001234567890 said so"},
        })
        rows = [snap("DeepSeek")]
        text = ct.dashboard(now=NOW, snapshots=rows, round_state=state,
                            usage={"DeepSeek": usage(chat_id=-1001234567890)}, coordinator="DeepSeek")
        for secret in ("1001234567890123", "1001234567890", "1336", "123456:AAH",
                       "C:\\Users", "eftyc"):
            self.assertNotIn(secret, text)

    def test_a_snapshot_cannot_carry_an_identifier_in_the_first_place(self):
        """The record itself has no field for a chat id: the leak cannot be introduced."""
        with self.assertRaises(TypeError):
            Snapshot(alias="DeepSeek", state=IDLE, chat_id=-1001234567890)

    def test_a_bot_that_never_reported_a_token_count_says_unknown(self):
        text = ct.dashboard(now=NOW, snapshots=[snap("Hermes")],
                            usage={"Hermes": usage(tokens_in=None, tokens_out=None,
                                                   tokens_in_known=1, tokens_out_known=0,
                                                   cost_usd=None, cost_known=0)})
        row = _row(text, "Hermes")
        self.assertIn("4 turns", row)
        self.assertIn("? tok", row)
        self.assertNotIn("0 tok", row)                    # never a zero it did not report
        self.assertNotIn("$0.00", row)
        self.assertIn("? tok", text.splitlines()[0])      # the swarm total, the same way

    def test_a_partial_total_says_how_partial_it_is(self):
        text = ct.dashboard(now=NOW, snapshots=[snap("Hermes")],
                            usage={"Hermes": usage(tokens_in_known=2)})
        self.assertIn("1.5k (2/4) tok", _row(text, "Hermes"))


class TestOneRowPerBot(unittest.TestCase):
    """#241: one row per bot in a fixed order -- icon, alias, role, doing, model, turns, spend.

    The whole card is pinned as text: "keep the data, change the presentation" is only a
    guarantee if the presentation itself is a fixture that has to be changed on purpose.
    """

    def card(self, state: Any = None) -> str:
        rows = [
            snap("Claude", BUSY, since=NOW - 180, model="opus", effort="high",
                 term=4, lease_ends=NOW + 600),
            snap("Codex", PAUSED, since=NOW - 3660),
            snap("DeepSeek", IDLE, since=NOW - 720),
            snap("Hermes", DOWN),
            snap("Reasonix", STUCK, since=NOW - 700, turn_elapsed=700, turn_timeout=600),
        ]
        spend = {"Claude": usage(turns=9, tokens_in=15_000_000, tokens_out=4_000_000,
                                 tokens_in_known=9, tokens_out_known=9,
                                 cost_usd=22.33, cost_known=9),
                 "DeepSeek": usage(turns=4, tokens_in=900, tokens_out=None,
                                   tokens_in_known=4, tokens_out_known=0,
                                   cost_usd=0.42, cost_known=4)}
        roles = {"Claude": "coordinator", "Codex": "reviewer", "DeepSeek": "developer",
                 "Reasonix": "reviewer"}
        state = state if state is not None else round_state(**{"241": {
            "issue": 241, "pr": 254, "owner": "DeepSeek", "head": "abcdef1", "ci": "7/7",
            "review": {"verdict": "merge", "sha": "abcdef1"},
            "qa": {"verdict": "pass", "sha": "abcdef1"}}})
        return ct.dashboard(now=NOW, snapshots=rows, round_state=state, usage=spend,
                            coordinator="Claude", roles=roles)

    def test_the_whole_card_for_a_five_bot_swarm(self):
        self.assertEqual(self.card(), "\n".join([
            "SWARM · round=5 · items=0/1 · today 13 turns · 19.0M (9/13) tok"
            " · $22.75 · refreshed 22:13Z",
            "",
            "🔵 Claude · coordinator · working 3m · opus/high · 9 turns · 19.0M tok · $22.33",
            "⏸ Codex · reviewer · paused 61m · deepseek/high · ? turns · ? tok · ?",
            "🟢 DeepSeek · developer · idle 12m · deepseek/high · 4 turns · 900/? tok · $0.42",
            "🔴 Hermes · ? · DOWN 5s · deepseek/high · ? turns · ? tok · ?",
            "⚠️ Reasonix · reviewer · stuck 12m · deepseek/high · ? turns · ? tok · ?",
            "",
            "lease: Claude · term=4 · 10m left",
            "",
            "! DOWN Hermes: reported down by a peer",
            "! STUCK Reasonix: 12m over 10m",
            "! PAUSED Codex: 61m",
            "",
            "round board (1 items):",
            "#241 DeepSeek PR#254 abcdef1 ci=7/7 review=merge qa=pass ready",
        ]))

    def test_every_state_has_its_own_icon_and_its_own_word(self):
        for state, icon, said in ((BUSY, "🔵", "working 5m"), (IDLE, "🟢", "idle 5m"),
                                  (PAUSED, "⏸", "paused 5m"), (FROZEN, "🧊", "frozen 5m"),
                                  (STUCK, "⚠️", "stuck 5m")):
            row = ct.bot_row(snap("Bot", state, since=NOW - 300), now=NOW)
            self.assertTrue(row.startswith(f"{icon} Bot · "), row)
            self.assertIn(said, row)
        # A snapshot that stopped being renewed is DOWN, whatever it last said of itself.
        row = ct.bot_row(snap("Bot", IDLE, at=NOW - 600), now=NOW)
        self.assertTrue(row.startswith("🔴 Bot · "), row)
        self.assertIn("DOWN 10m", row)

    def test_a_frozen_bot_reads_as_frozen_under_its_row(self):
        row = ct.bot_row(snap("Bot", FROZEN, since=NOW - 5400), now=NOW)
        self.assertIn("🧊 Bot · ", row)
        self.assertIn("frozen 1.5h", row)

    def test_the_row_order_never_moves(self):
        row = ct.bot_row(snap("Claude", BUSY, since=NOW - 180, model="opus", effort="high"),
                         now=NOW, usage=usage(turns=9, tokens_in=15_000_000,
                                              tokens_out=4_000_000, tokens_in_known=9,
                                              tokens_out_known=9, cost_usd=22.33,
                                              cost_known=9),
                         role="coordinator")
        self.assertEqual(row, "🔵 Claude · coordinator · working 3m · opus/high"
                              " · 9 turns · 19.0M tok · $22.33")

    def test_five_bots_and_ten_open_items_still_fit_one_message(self):
        state = round_state(**{str(241 + index): {
            "issue": 241 + index, "pr": 254 + index, "owner": "DeepSeek", "head": "abcdef1",
            "ci": "5/7", "review": {"verdict": "merge", "sha": "abcdef1"}} for index in range(10)})
        text = self.card(state)
        self.assertLessEqual(len(text), ct.MAX_CHARS)
        self.assertNotIn("hidden", text)                  # nothing had to be dropped
        self.assertEqual(len([line for line in text.splitlines()
                          if line.startswith(tuple(ct._ICONS.values()))]), 5)


class TestTheMessageFits(unittest.TestCase):
    def test_a_full_swarm_fits_in_one_telegram_message(self):
        rows = [snap(f"Bot{index}", STUCK if index % 7 == 0 else BUSY, turn_elapsed=700,
                     turn_timeout=600, queue=index) for index in range(40)]
        state = round_state(**{str(200 + index): {
            "issue": 200 + index, "pr": 200 + index, "owner": "Bot3", "head": "abcdef1",
            "ci": "7/7", "review": {"verdict": "merge", "sha": "abcdef1"},
            "qa": {"verdict": "pass", "sha": "abcdef1"}} for index in range(40)})
        text = ct.dashboard(now=NOW, snapshots=rows, round_state=state, coordinator="Bot0")
        self.assertLessEqual(len(text), ct.MAX_CHARS)
        self.assertIn("hidden", text)                     # it says what it dropped

    def test_detail_is_dropped_before_an_alert(self):
        rows = [snap("Hermes", at=NOW - 900, every=60.0)]
        rows += [snap(f"Bot{index}", BUSY, queue=3) for index in range(80)]
        text = ct.dashboard(now=NOW, snapshots=rows, coordinator="Bot0", limit=400)
        self.assertLessEqual(len(text), 400)
        self.assertIn("DOWN Hermes", text)

    def test_the_hidden_count_includes_what_the_note_itself_displaced(self):
        """The note makes room for itself by dropping lines, and those are hidden too."""
        rows = [snap(f"Bot{index}", BUSY, queue=3) for index in range(30)]
        state = round_state(**{str(200 + index): {
            "issue": 200 + index, "pr": 200 + index, "owner": "Bot3", "head": "abcdef1",
            "ci": "7/7"} for index in range(5)})
        text = ct.dashboard(now=NOW, snapshots=rows, round_state=state, coordinator="Bot0",
                            limit=900)
        drawn = len([line for line in text.splitlines() if line.strip()]) - 1
        # Every line the card could hold: 1 header + 30 rows + 1 lease + title + 5 items.
        self.assertIn(f"{1 + 30 + 1 + 6 - drawn} more line(s) hidden", text)

    def test_a_limit_too_small_for_the_card_still_shows_its_first_lines(self):
        """There is no whole-line cut left that keeps an alert: keep what fits, never the note alone."""
        rows = [snap(f"Bot{index}", at=NOW - 900, every=60.0) for index in range(5)]
        text = ct.dashboard(now=NOW, snapshots=rows, limit=64)
        self.assertLessEqual(len(text), 64)
        self.assertIn("SWARM", text)                       # the head survives the clipping
        self.assertIn("more line(s) hidden", text)


class TestControls(unittest.TestCase):
    def test_anything_that_stops_or_changes_asks_for_a_second_tap(self):
        for action in ("bot.stop_turn", "bot.restart", "swarm.pause_all",
                       "swarm.lease", "swarm.failback", "swarm.role",
                       "bot.memory", "swarm.hold", "swarm.release",
                       "swarm.admit", "swarm.remove"):
            self.assertTrue(ct.needs_confirm(action), action)
        for action in ("bot.pause", "bot.resume", "bot.model", "swarm.sync"):
            self.assertFalse(ct.needs_confirm(action), action)

    def test_a_report_is_never_confirmed_because_it_changes_nothing(self):
        for action in ("bot.model", "bot.effort", "bot.cli", "swarm.cleanup"):
            self.assertIn(action, ct.reports())
            self.assertFalse(ct.needs_confirm(action), action)
        # Cleanup's button is the dry run, and it says so: nothing is deleted by a press.
        self.assertIn("nothing is deleted", ct.control("swarm.cleanup").note)
        self.assertNotIn("swarm.sync", ct.reports())          # a resync does change the swarm

    def test_the_retired_buttons_are_gone(self):
        """WAKE had nothing left to pick (#215) and Freeze was Pause behind a second tap:
        neither is in the table.

        Admit left it too, but for a different reason -- it was *never wired* ("the WAKE and
        Freeze buttons, and the never-wired Admit bot, leave the table", 568f7da, the admin's
        own round). #345 asked for `/admit` as a page control and wired it (the roster change
        and the welcome the typed command makes), so it is back, with the second tap every
        change carries -- see ``test_admit_is_back_because_it_is_wired``.
        """
        for action in ("bot.wake", "bot.freeze"):
            self.assertIsNone(ct.control(action), action)
            self.assertNotIn(action, ct.PICKERS)

    def test_admit_is_back_because_it_is_wired(self):
        found = ct.control("swarm.admit")
        self.assertIsNotNone(found)
        self.assertTrue(found.confirm, "adding a bot to the roster asks twice")
        self.assertEqual(found.typed, "a bot's @username")     # a handle cannot be picked
        self.assertIn("HELLO", found.note)

    def test_the_nudge_says_it_costs_a_turn(self):
        self.assertIn("turn", ct.control("item.nudge").note)

    def test_an_unknown_action_is_never_confirmed_or_run(self):
        self.assertIsNone(ct.control("bot.destroy"))
        self.assertFalse(ct.needs_confirm("bot.destroy"))

    def test_deploy_and_merge_are_not_controls(self):
        actions = [row.action for row in ct.CONTROLS]
        for forbidden in ("deploy", "upgrade", "merge", "secrets", "config.edit"):
            self.assertFalse([action for action in actions if forbidden in action], forbidden)


class TestNoAdapterCall(unittest.TestCase):
    def test_rendering_needs_no_adapter_and_no_network(self):
        """#117 acceptance: the message updates with no model turn. The renderer takes records."""
        state = round_state(**{"129": {"issue": 129, "pr": 129, "owner": "DeepSeek",
                                      "waits_on": "Codex"}})
        rows = [snap("Hermes", BUSY, queue=2)]
        first = ct.dashboard(now=NOW, snapshots=rows, round_state=state, coordinator="Hermes")
        second = ct.dashboard(now=NOW, snapshots=rows, round_state=state, coordinator="Hermes")
        self.assertEqual(first, second)                   # a pure function of the records
        self.assertTrue(first.startswith("SWARM"))
        imported = _imported_modules(ct.__file__)
        for forbidden in ("swarmly.telegram.adapters", "aiogram", "urllib", "requests",
                          "socket", "subprocess"):
            self.assertFalse([name for name in imported if name.startswith(forbidden)],
                             forbidden)                    # nothing here can start a turn


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
