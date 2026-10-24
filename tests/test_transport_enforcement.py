"""The rulebook, enforced and checked in transport space (admin, 24 Oct).

Live, 24 Oct 01:11: a CI wake had the coordinator ask a paused bot for QA the round had
given to a stand-in, and the round sat for eight hours. The rules that code can check are
now checked: a message to a frozen peer is not delivered and names the stand-in; the owed
table follows the stand-in; a merge the rulebook rules out is refused; and every violation
counts towards a drift score that first reminds the bot of the rule, then resets its session.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from swarmly.telegram import drift as dr
from swarmly.telegram import merge_gate as mg
from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from tests.test_telegram_channel import GROUP, event, make_channel, make_config, run

HEAD = "0636b79aa1b2c3d4e5f60718293a4b5c6d7e8f90"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class DriftLedgerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.ledger = dr.DriftLedger(Path(self.tmp.name) / "drift-hermes.json", clock=self.clock)

    def test_one_hard_violation_reminds_and_two_reset(self):
        first = self.ledger.record("addressed_frozen_peer", "addressed Antigravity")
        self.assertTrue(first.remind)
        self.assertFalse(first.reset)
        self.assertIn("Never hand work to a peer that is paused", self.ledger.reminder())
        second = self.ledger.record("merge_gate_refused", "no QA pass")
        self.assertTrue(second.reset)
        self.ledger.mark_reset()
        self.assertEqual(self.ledger.decide().score, 0)       # a reset starts the count again
        self.assertEqual(self.ledger.reminder(), "")
        self.assertEqual(self.ledger.decide().resets_today, 1)

    def test_old_violations_age_out_and_the_ledger_survives_a_restart(self):
        self.ledger.record("addressed_frozen_peer")
        self.clock.now += dr.WINDOW_SECONDS + 1
        again = dr.DriftLedger(self.ledger.path, clock=self.clock)
        self.assertEqual(len(again.rows), 1)
        self.assertEqual(again.decide().score, 0)

    def test_another_process_reports_through_the_inbox(self):
        self.clock.now = time.time()                    # the guard stamps real time
        dr.append_inbox(str(self.ledger.path), "merge_gate_refused", "gh pr merge 245")
        self.assertEqual([r.kind for r in self.ledger.fold_inbox()], ["merge_gate_refused"])
        self.assertEqual(self.ledger.decide().score, 3)
        self.assertFalse(self.ledger.inbox.exists())


class MergeGateTests(unittest.TestCase):

    def view(self, *comments, ci="SUCCESS", status="COMPLETED"):
        return {"headRefOid": HEAD,
                "statusCheckRollup": [{"name": "lint", "status": status, "conclusion": ci}],
                "comments": [{"body": body} for body in comments]}

    def test_met_when_merge_and_qa_pass_name_the_head_and_ci_is_green(self):
        body = "Reviewed at 0636b79.\n**Verdict: merge.**\nQA: PASS — 368 passed.\n— Reasonix"
        self.assertEqual(mg.evaluate(self.view(body)), [])

    def test_a_verdict_on_an_older_head_does_not_count(self):
        body = "Reviewed at 3bdc901. Verdict: merge. QA: pass. — Reasonix"
        missing = mg.evaluate(self.view(body))
        self.assertIn("a reviewer's merge verdict", missing)
        self.assertIn("a QA pass", missing)

    def test_the_latest_verdict_on_the_head_wins(self):
        missing = mg.evaluate(self.view("0636b79 Verdict: merge. QA: pass.",
                                        "0636b79 re-check: Verdict: fix-first, Windows quoting."))
        self.assertEqual(missing, ["a reviewer's merge verdict (the latest says fix-first)"])

    def test_pending_or_red_ci_blocks(self):
        body = "0636b79 Verdict: merge. QA: pass."
        self.assertEqual(mg.evaluate(self.view(body, ci="", status="IN_PROGRESS")),
                         ["CI is pending"])
        self.assertEqual(mg.evaluate(self.view(body, ci="FAILURE")), ["CI is red"])

    def test_the_gate_reads_only_pr_merge_with_a_number(self):
        self.assertEqual(mg.pr_target(["pr", "merge", "245", "--merge"]),
                         {"number": "245", "repo_args": []})
        self.assertEqual(mg.pr_target(["-R", "o/r", "pr", "merge", "7"])["repo_args"],
                         ["-R", "o/r"])
        self.assertIsNone(mg.pr_target(["pr", "view", "245"]))
        self.assertIsNone(mg.check("gh", ["pr", "merge", "245"], {"SWARMLY_MERGE_GATE": "off"}))


class Recording(EchoAdapter):
    def __init__(self, reply):
        super().__init__("Hermes")
        self.reply = reply
        self.prompts = []
        self.forgotten = []

    async def respond(self, message):
        self.prompts.append(message.text or "")
        return AdapterReply(text=self.reply)

    def forget_session(self, context_id):
        self.forgotten.append(context_id)
        return True


class ChannelEnforcementTests(unittest.TestCase):

    def channel(self, reply="ok"):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = make_config(peers={"Codex": "CodexBot", "Antigravity": "AntigravityBot",
                                    "Reasonix": "ReasonixBot"})
        adapter = Recording(reply)
        channel = make_channel(config, adapter=adapter)
        channel.bot = object()
        channel.config.identity.role = "coordinator"
        channel.config.peer_roles.update({"Antigravity": "qa-tester", "Reasonix": "reviewer",
                                          "Codex": "developer"})
        channel.drift = dr.DriftLedger(Path(self.tmp.name) / "drift.json", clock=Clock())
        channel._peers_frozen["Antigravity"] = {"ref": "pause", "until": None}
        channel.topics.remember(GROUP, 9400, "Stability round")
        return channel, adapter

    def test_a_frozen_qa_has_its_reviewer_as_stand_in_everywhere(self):
        channel, _ = self.channel()
        self.assertEqual(channel.stand_ins(), {"Antigravity": ("qa-tester", "Reasonix")})
        self.assertEqual(channel.owed_roles()["qa"], "Reasonix")   # the owed table follows
        note = channel._frozen_peers_note()
        self.assertIn("Antigravity's qa-tester work goes to Reasonix", note)

    def test_a_reply_to_a_frozen_peer_is_not_delivered_and_names_the_stand_in(self):
        channel, _ = self.channel("@AntigravityBot please QA #245 at 0636b79.")
        run(channel.handle_event(event("@HermesBot CI on #245 is green", thread_id=9400,
                                       message_id=40)))
        posted = channel._test_sender.sent[-1]["text"]
        self.assertNotIn("@AntigravityBot", posted)                 # not woken
        self.assertIn("⛔ Not delivered to Antigravity: paused by the admin.", posted)
        self.assertIn("@ReasonixBot", posted)                       # the stand-in is
        self.assertEqual([r.kind for r in channel.drift.live()], ["addressed_frozen_peer"])

    def test_the_sender_is_told_in_its_own_token_space_once(self):
        """Admin, 24 Oct: the refusal reaches the sender as a turn, and it takes corrective
        action -- but a sender that repeats itself is not woken in a loop."""
        channel, _ = self.channel("@AntigravityBot please QA #245.")
        briefs = []
        channel._run_own_task = lambda text, chat, thread, name: briefs.append(text)
        run(channel.handle_event(event("@HermesBot one", thread_id=9400, message_id=43)))
        run(channel.handle_event(event("@HermesBot two", thread_id=9400, message_id=44)))
        self.assertEqual(len(briefs), 1)
        self.assertIn("NOT delivered to Antigravity: it is paused by the admin", briefs[0])
        self.assertIn("stand-in Reasonix", briefs[0])

    def test_the_reminder_rides_the_next_turn_and_a_second_hard_violation_resets(self):
        channel, adapter = self.channel("@AntigravityBot please QA #245.")
        channel._run_own_task = lambda *args: None          # the sender notice: own test
        run(channel.handle_event(event("@HermesBot one", thread_id=9400, message_id=41)))
        run(channel.handle_event(event("@HermesBot two", thread_id=9400, message_id=42)))
        self.assertIn("Rule reminder from the transport", adapter.prompts[1])

        async def settle():
            for task in list(channel._self_tasks):
                await task
        run(settle())
        self.assertEqual(adapter.forgotten, [channel.topics.context(GROUP, 9400)])
        sent = " ".join(s["text"] for s in channel._test_sender.sent)
        self.assertIn("context reset after", sent)
        self.assertLess(channel.drift.decide().score, dr.REMIND_AT)   # counting starts again

    def test_drift_command_reports_the_score(self):
        channel, _ = self.channel()
        channel.drift.record("addressed_frozen_peer", "addressed Antigravity")
        from tests.test_telegram_channel import ADMIN_ID
        run(channel.handle_event(event("/drift@HermesBot", author_id=ADMIN_ID,
                                       username="alice", is_bot=False, message_id=50)))
        text = channel._test_sender.sent[-1]["text"]
        self.assertIn("drift score 3", text)
        self.assertIn("addressed_frozen_peer", text)


class TheNightOf24OctTests(unittest.TestCase):
    """The exact path #245 took (24 Oct, 23:11-06:56 UTC): the coordinator's turn ended on
    `WAIT merge #245 ... from Antigravity`, a machine line, not a mention. Nothing was sent to
    the paused bot, so nothing bounced, and the row's first rung was Antigravity's to send."""

    channel = ChannelEnforcementTests.channel

    def test_a_wait_on_a_frozen_peer_is_refused_and_bounced_to_the_sender(self):
        channel, _ = self.channel(f"Waiting on QA.\nWAIT merge #245 head {HEAD[:7]} from "
                                  "Antigravity")
        from swarmly.telegram.owed import OwedStore
        store = OwedStore("Hermes", state_dir=Path(self.tmp.name) / "owed")
        channel.owed_store = lambda: store
        briefs = []
        channel._run_own_task = lambda text, chat, thread, name: briefs.append(text)
        run(channel.handle_event(event("@HermesBot CI on #245 is green", thread_id=9400,
                                       message_id=60)))
        posted = channel._test_sender.sent[-1]["text"]
        self.assertIn("WAIT refused: Antigravity is paused by the admin", posted)
        self.assertFalse([row for row in channel.owed_store().load()
                          if row.debtor == "Antigravity" and row.is_open])
        self.assertEqual(len(briefs), 1)
        self.assertIn("NOT delivered to Antigravity", briefs[0])
        self.assertIn("stand-in Reasonix", briefs[0])

    def test_a_row_already_owed_by_a_frozen_peer_goes_straight_to_the_coordinator(self):
        from swarmly.telegram.owed import Row
        from swarmly.telegram.owed_guard import STALL_COORDINATOR, advance, stalls
        row = Row(kind="merge", item="#245", head=HEAD[:7], debtor="Antigravity",
                  owes="a merge verdict", created_at=1000.0, updated_at=1000.0)
        due = stalls([row], now=1060.0, minutes=30, away={"antigravity": "paused by the admin"})
        self.assertEqual([stall.step for stall in due], [STALL_COORDINATOR])
        self.assertIn("cannot act", due[0].text)
        self.assertEqual(advance([row], due, now=1060.0)[0].stalls, 2)   # next: the admin
        self.assertEqual(stalls([row], now=1060.0, minutes=30), [])     # an active debtor waits

    def test_a_stand_ins_verdict_passes_the_gate_when_it_says_qa(self):
        """The stand-in note asks for `QA: pass`; a merge verdict alone is not a QA pass."""
        view = MergeGateTests.view
        bare = f"Re-review at {HEAD[:7]}: 100 passed.\n**Verdict: merge at {HEAD[:7]}.**"
        self.assertEqual(mg.evaluate(view(None, bare)), ["a QA pass"])
        self.assertEqual(mg.evaluate(view(None, bare + f"\nQA: pass at {HEAD[:7]}")), [])
        channel, _ = self.channel()
        self.assertIn("`QA: pass` or `QA: fail`", channel._frozen_peers_note())

    def test_the_gate_skips_option_values_when_it_looks_for_the_pr(self):
        self.assertEqual(mg.pr_target(["pr", "merge", "--match-head-commit", "1234567", "245"])
                         ["number"], "245")

    def test_a_transport_turn_reports_its_own_failure_only(self):
        channel, adapter = self.channel()

        async def failing(message):
            return AdapterReply(text="", ok=False, error="boom")
        adapter.respond = failing
        reply = run(channel.run_transport_turn("CI finished on #245", GROUP, 9400))
        self.assertFalse(reply.ok)
        self.assertIn("boom", reply.error)


class GuardEnvTests(unittest.TestCase):

    def test_the_guard_is_told_where_the_ledger_is(self):
        from swarmly.telegram.guard import prepare
        with tempfile.TemporaryDirectory() as tmp:
            env = prepare(Path(tmp) / "g", "coordinator", {"PATH": ""}, drift="/x/drift.json")
            if env.get("SWARMLY_ROLE"):
                self.assertEqual(env["SWARMLY_DRIFT_FILE"], "/x/drift.json")


if __name__ == "__main__":
    unittest.main()
