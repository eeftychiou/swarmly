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
    def test_the_header_carries_the_round_the_items_and_the_lease(self):
        state = round_state(**{
            "124": {"issue": 124, "state": "merged"},
            "125": {"issue": 125, "state": "open"},
        })
        lease = snap("Claude", term=4, lease_ends=NOW + 600)
        text = ct.header(now=NOW, round_state=state, coordinator="Claude",
                         lease=lease, usage=usage())
        self.assertIn("round=5", text)
        self.assertIn("items=1/2", text)
        self.assertIn("coord=Claude", text)
        self.assertIn("term=4", text)
        self.assertIn("lease=10m", text)
        self.assertIn("today=1.2k/340 $0.42", text)

    def test_an_unreadable_round_is_not_reported_as_empty(self):
        state = RoundState(round="5", status="unreadable", reason="fetch failed")
        text = ct.header(now=NOW, round_state=state)
        self.assertIn("state=unreadable", text)
        self.assertNotIn("items=0/0", text)

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
        self.assertIn("today=unknown/unknown unknown", text)
        self.assertNotIn("today=0", text)

    def test_a_partial_total_says_how_partial_it_is(self):
        text = ct.dashboard(now=NOW, snapshots=[snap("Hermes")],
                            usage={"Hermes": usage(tokens_in_known=2)})
        self.assertIn("1.2k (2/4)", text)


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


class TestControls(unittest.TestCase):
    def test_anything_that_stops_or_changes_asks_for_a_second_tap(self):
        for action in ("bot.stop_turn", "bot.restart", "bot.freeze", "swarm.pause_all",
                       "swarm.lease", "swarm.failback", "swarm.role", "swarm.cleanup",
                       "swarm.admit"):
            self.assertTrue(ct.needs_confirm(action), action)
        for action in ("bot.pause", "bot.resume", "bot.model", "bot.wake", "swarm.sync"):
            self.assertFalse(ct.needs_confirm(action), action)

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
        with open(ct.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("adapters", "aiogram", "urllib", "requests", "socket"):
            self.assertNotIn(forbidden, source)           # nothing here can start a turn


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
