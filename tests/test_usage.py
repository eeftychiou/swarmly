"""The `/usage` turn ledger (issue #125).

What these pin: a row per model turn with what the runtime reported, a ledger that survives a
restart and stays bounded, `/usage` answered from the ledger with no adapter call, and unknown
numbers shown as unknown instead of as zero.
"""

import json
from tempfile import TemporaryDirectory
import re
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram import usage as us
from swarmly.telegram.adapters.base import AdapterReply
from tests.test_telegram_channel import (ADMIN_ID, GROUP, event, make_channel, make_config, run)

NOW = 1_800_000_000.0


class TestARow(unittest.TestCase):

    def test_it_round_trips(self):
        row = us.Entry(alias="DeepSeek", started=NOW, ended=NOW + 12, topic="tg-307",
                       outcome=us.OK, tokens_in=1200, tokens_out=340, cost_usd=0.042,
                       model="sonnet", quota_left=10.0, quota_resets_at=NOW + 3600)
        self.assertEqual(us.Entry.from_dict(row.to_dict()), row)

    def test_a_malformed_row_is_refused(self):
        for data in ({}, {"alias": "x"}, {"alias": "x", "started": "soon"},
                     {"started": NOW}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                us.Entry.from_dict(data)

    def test_a_reply_without_numbers_gives_unknowns_not_zeros(self):
        row = us.Entry.from_reply(alias="DeepSeek", started=NOW, ended=NOW + 1,
                                  topic="general", reply=AdapterReply(text="hi"))
        self.assertIsNone(row.tokens_in)
        self.assertIsNone(row.tokens_out)
        self.assertIsNone(row.cost_usd)
        self.assertEqual(row.outcome, us.OK)

    def test_a_reply_reports_what_the_runtime_said(self):
        reply = AdapterReply(text="hi", meta={"input_tokens": 100, "output_tokens": 20,
                                              "total_cost_usd": 0.5, "model": "opus"})
        row = us.Entry.from_reply(alias="DeepSeek", started=NOW, ended=NOW + 1,
                                  topic="tg-1", reply=reply)
        self.assertEqual((row.tokens_in, row.tokens_out, row.cost_usd),
                         (100, 20, 0.5))
        self.assertEqual(row.model, "opus")

    def test_a_failed_reply_is_recorded_as_one(self):
        row = us.Entry.from_reply(alias="DeepSeek", started=NOW, ended=NOW + 1, topic="general",
                                  reply=AdapterReply(ok=False, error="boom"))
        self.assertEqual(row.outcome, us.ERROR)

    def test_a_negative_or_silly_number_is_dropped(self):
        reply = AdapterReply(text="hi", meta={"input_tokens": -5, "total_cost_usd": "free"})
        row = us.Entry.from_reply(alias="DeepSeek", started=NOW, ended=NOW + 1,
                                  topic="general", reply=reply)
        self.assertIsNone(row.tokens_in)
        self.assertIsNone(row.cost_usd)


class TestTheLedger(unittest.TestCase):

    def ledger(self, path=None):
        return us.Ledger(path, alias="DeepSeek", clock=lambda: NOW)

    def test_rows_are_appended_and_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage-deepseek.json"
            ledger = self.ledger(path)
            ledger.append(us.Entry(alias="DeepSeek", started=NOW, ended=NOW + 1,
                                   tokens_in=10, tokens_out=2, cost_usd=0.01))
            ledger.append(us.Entry(alias="DeepSeek", started=NOW + 60, ended=NOW + 61,
                                   tokens_in=20, tokens_out=3, cost_usd=0.02))
            again = self.ledger(path)                     # a restart: read the same file
            self.assertEqual(len(again.entries), 2)
            today = again.totals(NOW - 1)
            self.assertEqual((today["tokens_in"], today["tokens_out"], today["cost_usd"]),
                             (30, 5, 0.03))

    def test_the_file_is_capped_and_the_drop_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            ledger = us.Ledger(path, alias="DeepSeek", max_rows=3, clock=lambda: NOW)
            for index in range(5):
                ledger.append(us.Entry(alias="DeepSeek", started=NOW + index, ended=NOW + index))
            self.assertEqual(len(ledger.entries), 3)
            self.assertEqual(ledger.dropped, 2)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertEqual([row.started for row in self.ledger_with(path, 3).entries],
                             [NOW + 2, NOW + 3, NOW + 4])

    def ledger_with(self, path, max_rows):
        return us.Ledger(path, alias="DeepSeek", max_rows=max_rows, clock=lambda: NOW)

    def test_a_bad_row_does_not_lose_the_good_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            path.write_text("not json\n" + json.dumps(
                us.Entry(alias="DeepSeek", started=NOW).to_dict()) + "\n", encoding="utf-8")
            self.assertEqual(len(self.ledger(path).entries), 1)

    def test_one_silent_runtime_makes_the_total_a_count_not_a_guess(self):
        ledger = self.ledger()
        ledger.append(us.Entry(alias="DeepSeek", started=NOW, ended=NOW + 1, cost_usd=0.10))
        ledger.append(us.Entry(alias="DeepSeek", started=NOW + 1, ended=NOW + 2))   # unknown
        total = ledger.totals(NOW - 1)
        self.assertEqual(total["turns"], 2)
        self.assertEqual(total["cost_known"], 1)          # says how many reported
        self.assertEqual(total["cost_usd"], 0.10)

    def test_no_numbers_at_all_is_unknown_not_zero(self):
        ledger = self.ledger()
        ledger.append(us.Entry(alias="DeepSeek", started=NOW, ended=NOW + 1))
        total = ledger.totals(NOW - 1)
        self.assertIsNone(total["tokens_in"])
        self.assertIsNone(total["tokens_out"])
        self.assertIsNone(total["cost_usd"])


class TestTheRendering(unittest.TestCase):

    def test_unknowns_are_said_out_loud(self):
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20))
        text = ledger.render(now=NOW)
        self.assertIn("in=unknown", text)
        self.assertIn("cost=unknown", text)
        self.assertIn("quota: unknown", text)

    def test_it_says_today_and_since_boot(self):
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 60, ended=NOW - 50,
                               tokens_in=100, tokens_out=10, cost_usd=0.02))
        text = ledger.render(now=NOW, since_boot=NOW - 120)
        self.assertIn("USAGE DeepSeek turns=1", text)
        self.assertIn("today: 1 turns", text)
        self.assertIn("since boot: 1 turns", text)
        self.assertIn("cost=$0.0200", text)
        self.assertIn("last:", text)

    def test_it_says_where_its_history_starts_when_rows_were_dropped(self):
        ledger = us.Ledger(None, alias="DeepSeek", max_rows=2, clock=lambda: NOW)
        for index in range(4):
            ledger.append(us.Entry(alias="DeepSeek", started=NOW - 40 + index, ended=NOW))
        text = ledger.render(now=NOW)
        self.assertIn("dropped=2", text)
        self.assertIn("older rows dropped", text)

    def test_a_partial_token_total_says_how_partial(self):
        """#125 review: `in=7` over ten turns where nine were silent is not a total."""
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20,
                               tokens_in=7, tokens_out=3, cost_usd=0.01))
        for index in range(3):
            ledger.append(us.Entry(alias="DeepSeek", started=NOW - 10 + index, ended=NOW))
        text = ledger.render(now=NOW)
        self.assertIn("in=7 (1/4 reported)", text)
        self.assertIn("out=3 (1/4 reported)", text)
        self.assertIn("cost=$0.0100 (1/4 reported)", text)

    def test_a_complete_total_carries_no_caveat(self):
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20,
                               tokens_in=7, tokens_out=3, cost_usd=0.01))
        text = ledger.render(now=NOW)
        self.assertIn("in=7 out=3 cost=$0.0100", text)
        self.assertNotIn("1/1 reported", text)      # complete: no caveat on the numbers

    def test_each_side_of_the_token_total_is_counted_on_its_own(self):
        """#125 review: a row that reports only input must not cover the output column.

        Two rows — one with both sides, one with input only — are fully reported for `in` and
        half-reported for `out`; sharing one known-count would claim `out` was 2/2 covered.
        """
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20,
                               tokens_in=7, tokens_out=3, cost_usd=0.01))
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 10, ended=NOW,
                               tokens_in=5, cost_usd=0.01))
        text = ledger.render(now=NOW)
        self.assertIn("in=12 ", text)                   # both rows reported input: no caveat
        self.assertIn("out=3 (1/2 reported)", text)     # only one row reported output
        self.assertIn("cost=$0.0200", text)

    def test_silent_rows_are_counted_as_unreported(self):
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20))
        self.assertIn("in=unknown (0/1 reported)", ledger.render(now=NOW))

    def test_no_identifier_reaches_the_rendering(self):
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20,
                               topic="tg-307", tokens_in=5, tokens_out=1, cost_usd=0.01))
        text = ledger.render(now=NOW)
        self.assertNotIn(str(GROUP), text)
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\|/(?:home|Users|tmp)/", text), text)


class CountingAdapter:
    def __init__(self, inner):
        self.inner = inner
        self.turns = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def respond(self, message):
        self.turns.append(message.text)
        reply = await self.inner.respond(message)
        if reply is not None:
            reply.meta.update({"input_tokens": 7, "output_tokens": 3, "total_cost_usd": 0.05})
        return reply


class TestOnTheChannel(unittest.TestCase):

    def channel(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config()
        config.source_path = Path(tmp.name) / "config.yaml"
        # The state directory follows the bus root, so the ledger lives in the temp dir and not
        # in the checkout: one test's rows must not become the next test's history.
        config.bus.repo = str(tmp.name)
        channel = make_channel(config)
        return channel

    def test_a_turn_is_recorded_with_what_the_runtime_reported(self):
        channel = self.channel()
        channel.adapter = CountingAdapter(channel.adapter)
        run(channel.handle_event(event("@HermesBot hello", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        rows = channel.usage_ledger().entries
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].tokens_in, rows[0].tokens_out, rows[0].cost_usd),
                         (7, 3, 0.05))
        self.assertEqual(rows[0].outcome, us.OK)

    def test_usage_answers_without_a_model_turn(self):
        channel = self.channel()
        channel.adapter = CountingAdapter(channel.adapter)
        run(channel.handle_event(event("@HermesBot hello", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        before = list(channel.adapter.turns)
        run(channel.handle_event(event("/usage@HermesBot", author_id=ADMIN_ID,
                                       username="alice", is_bot=False, message_id=9)))
        self.assertEqual(channel.adapter.turns, before)          # no new turn
        self.assertIn("USAGE Hermes", channel._test_sender.last)
        self.assertIn("turns=1", channel._test_sender.last)

    def test_the_ledger_survives_a_restart(self):
        channel = self.channel()
        channel.adapter = CountingAdapter(channel.adapter)
        run(channel.handle_event(event("@HermesBot hello", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        again = make_channel(channel.config)                     # the same state file
        self.assertEqual(len(again.usage_ledger().entries), 1)
        self.assertIn("turns=1", again.usage_text())


class RaisingAdapter:
    """A runtime that raises after the model may well have run: the spend still happened."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def respond(self, message):
        raise RuntimeError("the CLI died mid-turn")


class TestARaisedTurn(unittest.TestCase):

    def test_a_turn_that_raises_still_gets_a_row(self):
        """#125 review: `record_turn` sat only on the success path, losing that turn's spend."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config()
        config.source_path = Path(tmp.name) / "config.yaml"
        config.bus.repo = str(tmp.name)
        channel = make_channel(config)
        channel.adapter = RaisingAdapter(channel.adapter)
        run(channel.handle_event(event("@HermesBot hello", author_id=ADMIN_ID,
                                       username="alice", is_bot=False)))
        rows = channel.usage_ledger().entries
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, us.ERROR)
        self.assertIn("error", channel.usage_text())

    def test_a_recorded_error_row_keeps_the_turn_visible_in_usage(self):
        ledger = us.Ledger(None, alias="DeepSeek", clock=lambda: NOW)
        ledger.append(us.Entry(alias="DeepSeek", started=NOW - 30, ended=NOW - 20,
                               outcome=us.ERROR))
        text = ledger.render(now=NOW)
        self.assertIn("general:error", text)
        self.assertIn("today: 1 turns", text)


class TestTheAdapterReports(unittest.TestCase):
    """Claude's own JSON carries the tokens; the ledger only knows what the adapter passes on."""

    def test_claude_tokens_land_in_the_meta(self):
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        adapter = ClaudeAdapter("Hermes")
        payload = json.dumps({"result": "done", "subtype": "success",
                              "total_cost_usd": 0.2, "num_turns": 1,
                              "usage": {"input_tokens": 11, "output_tokens": 4}})
        reply = adapter.parse_output(payload, 1.0)
        self.assertEqual((reply.meta["tokens_in"], reply.meta["tokens_out"]), (11, 4))
        self.assertEqual(reply.meta["total_cost_usd"], 0.2)

    def test_claude_input_counts_the_prompt_cache(self):
        # Live: 192 input tokens for 17 turns that cost $4.60, because the cache was left out.
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        payload = json.dumps({"result": "done", "subtype": "success", "total_cost_usd": 0.3,
                              "usage": {"input_tokens": 12, "output_tokens": 900,
                                        "cache_creation_input_tokens": 3000,
                                        "cache_read_input_tokens": 40000}})
        reply = ClaudeAdapter("Hermes").parse_output(payload, 1.0)
        self.assertEqual((reply.meta["tokens_in"], reply.meta["tokens_out"]), (43012, 900))

    def test_reasonix_tokens_land_in_the_meta(self):
        # Live, round 5: DeepSeek and Reasonix reported their cost with null tokens.
        with TemporaryDirectory() as tmp:
            from tests.test_reasonix_adapter import config
            from swarmly.telegram.adapters import build_adapter
            adapter = build_adapter(config(tmp))
            adapter._turn_session.set("s1")
            for usage, expected in (
                    ({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90},
                     (100, 5)),
                    ({"prompt_tokens": 120, "completion_tokens": 7,
                      "prompt_cache_hit_tokens": 100}, (120, 7))):
                payload = json.dumps({"result": "done", "session_id": "s1",
                                      "total_cost_usd": 0.01, "usage": usage})
                reply = adapter.parse_output(payload, 1.0)
                self.assertEqual((reply.meta["tokens_in"], reply.meta["tokens_out"]), expected)
            reply = adapter.parse_output(json.dumps({"result": "done", "total_cost_usd": 0.01}),
                                         1.0)
            self.assertNotIn("tokens_in", reply.meta)       # not reported is unknown, not 0

    def test_codex_output_tokens_land_in_the_meta(self):
        from swarmly.telegram.adapters.codex import codex_output_tokens
        out = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 5000, "output_tokens": 321}})])
        self.assertEqual(codex_output_tokens(out), 321)
        self.assertIsNone(codex_output_tokens("no events"))

    def test_nothing_ran_is_zero_not_unknown(self):
        ledger = us.Ledger(None, alias="Hermes", clock=lambda: NOW)
        totals = ledger.totals(NOW)
        self.assertEqual((totals["tokens_in"], totals["tokens_out"], totals["cost_usd"]),
                         (0, 0, 0.0))


class TestNoBackgroundTasksInATurn(unittest.TestCase):
    """Live, round 5: a backgrounded `find` kept the turn alive, and Claude's NO_REPLY to its
    notice replaced the real answer, so the admin's request got silence."""

    def test_the_turn_runs_with_background_tasks_off(self):
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        env = ClaudeAdapter("Hermes").environment({"PATH": "/bin"})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_a_no_reply_to_a_task_notice_is_not_silence(self):
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        payload = json.dumps({"result": "NO_REPLY", "subtype": "success",
                              "origin": {"kind": "task-notification"}})
        reply = ClaudeAdapter("Hermes").parse_output(payload, 1.0)
        self.assertFalse(reply.ok)
        self.assertIn("answer was lost", reply.error)

    def test_a_chosen_no_reply_stays_silent(self):
        from swarmly.telegram.adapters.claude import ClaudeAdapter
        payload = json.dumps({"result": "NO_REPLY", "subtype": "success"})
        reply = ClaudeAdapter("Hermes").parse_output(payload, 1.0)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "NO_REPLY")


if __name__ == "__main__":
    unittest.main()
