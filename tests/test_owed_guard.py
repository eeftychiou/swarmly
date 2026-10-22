"""Invariants, deadlines and the wake budget (#164), with round 5's stalls as the facts.

Three things keep the table honest when nobody is looking: an open item with no live row is an
orphan (the coordinator owes the next step), a row that has not moved climbs a three-rung
ladder once per rung, and a round's coordinator wakes are budgeted for the day.
"""

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import owed as ow
from swarmly.telegram import owed_guard as guard
from tests.test_telegram_channel import GROUP, make_channel, make_config

ROLES = {"review": "Reasonix", "qa": "Antigravity", "developer": "DeepSeek",
         "coordinator": "Claudebot"}


def item(**fields):
    base = {"pr": 146, "head": "c166987", "owner": "", "ci": "green"}
    base.update(fields)
    return base


def row(**fields):
    fields.setdefault("kind", "ci")
    fields.setdefault("item", "#146")
    fields.setdefault("debtor", ow.CI)
    fields.setdefault("owes", "a result on c166987")
    fields.setdefault("head", "c166987")
    fields.setdefault("updated_at", 1000.0)
    return ow.Row(**fields)


class TestTheOrphanItem(unittest.TestCase):
    """#146 at 09:13: an open item, no row, no WAKE -- and nothing said the round was stuck."""

    def test_an_open_item_with_no_live_row_is_the_coordinators(self):
        rows = guard.orphans([("146", item())], [], coordinator="Claudebot", at=1000.0)
        self.assertEqual([r.id for r in rows], ["progress:#146:Claudebot"])
        # The row carries the concrete gap, so an item that moves is news again.
        self.assertEqual(rows[0].observed,
                         "PR #146: CI green, no merge verdict, no QA pass, not merged")
        self.assertTrue(rows[0].is_open)

    def test_the_orphan_names_the_next_actor_not_always_the_coordinator(self):
        """`next_actor()` has a caller: a reviewer with a merge in hand is the one to remind."""
        rows = guard.orphans([("146", item(review={"verdict": "merge"}))], [],
                             coordinator="Claudebot", roles=ROLES, at=1000.0)
        self.assertEqual([r.id for r in rows], ["progress:#146:Antigravity"])   # QA is next
        self.assertEqual(rows[0].waiting_on, "Claudebot")
        # An item waiting on nothing but CI is the coordinator's: no bot owes a waiting.
        rows = guard.orphans([("146", item(ci="pending"))], [],
                             coordinator="Claudebot", roles=ROLES, at=1000.0)
        self.assertEqual([r.id for r in rows], ["progress:#146:Claudebot"])
        # No roles at all: the coordinator, as before.
        rows = guard.orphans([("146", item())], [], coordinator="Claudebot", at=1000.0)
        self.assertEqual([r.id for r in rows], ["progress:#146:Claudebot"])

    def test_an_item_with_no_next_actor_is_flagged_not_silently_the_coordinators(self):
        """#164's second invariant: no derivable actor is a flag on the row, not a quiet debt."""
        rows = guard.orphans([("146", item())], [], coordinator="Claudebot", roles={}, at=1000.0)
        self.assertEqual([r.debtor for r in rows], ["Claudebot"])   # still theirs to assign
        self.assertIn("no next actor", rows[0].flag)
        self.assertIn("⚑ no next actor", rows[0].line(now=1000.0))  # and `/owed` shows it
        # A row whose next actor *is* known carries no flag.
        rows = guard.orphans([("146", item(review={"verdict": "merge"}))], [],
                             coordinator="Claudebot", roles=ROLES, at=1000.0)
        self.assertEqual(rows[0].flag, "")
        self.assertNotIn("⚑", rows[0].line(now=1000.0))

    def test_the_flag_survives_the_round_trip(self):
        row = guard.orphans([("146", item())], [], coordinator="Claudebot", roles={},
                            at=1000.0)[0]
        again = ow.Row.from_dict(row.to_dict())
        self.assertEqual(again.flag, row.flag)
        self.assertEqual(again.id, row.id)

    def test_an_item_a_live_row_answers_is_no_orphan(self):
        live = row(item="#146", kind="progress", debtor="DeepSeek", owes="progress on #146")
        self.assertEqual(guard.orphans([("146", item())], [live], coordinator="Claudebot"), [])

    def test_a_closed_row_does_not_cover_the_item(self):
        closed = row(item="#146", kind="progress", debtor="DeepSeek", owes="progress",
                     state=ow.CLOSED)
        self.assertEqual(len(guard.orphans([("146", item())], [closed],
                                           coordinator="Claudebot")), 1)

    def test_a_finished_item_opens_nothing(self):
        self.assertEqual(guard.orphans([("146", item(state="merged"))], [],
                                       coordinator="Claudebot"), [])


    def test_the_gap_names_the_facts_that_do_hold(self):
        done = guard.gap(item(review={"verdict": "merge"}, qa={"verdict": "pass"}))
        self.assertEqual(done, "PR #146: CI green, merge verdict, QA pass, not merged")


class TestTheNextActor(unittest.TestCase):
    """Every open PR names a next actor, or the dashboard gets a flag."""

    def test_a_head_still_waiting_on_ci_names_the_result(self):
        self.assertEqual(guard.next_actor(item(ci="pending"), ROLES), "CI")

    def test_a_green_head_names_the_reviewer_then_qa_then_the_coordinator(self):
        self.assertEqual(guard.next_actor(item(), ROLES), "Reasonix")
        self.assertEqual(guard.next_actor(item(review={"verdict": "merge"}), ROLES),
                         "Antigravity")
        self.assertEqual(guard.next_actor(item(review={"verdict": "merge"},
                                               qa={"verdict": "pass"}), ROLES), "Claudebot")

    def test_a_fix_first_verdict_names_the_developer(self):
        self.assertEqual(guard.next_actor(item(ci="green", review={"verdict": "fix-first"}),
                                          ROLES), "DeepSeek")
        self.assertEqual(guard.next_actor(item(ci="fail"), ROLES), "DeepSeek")

    def test_a_role_nobody_holds_is_the_dashboards_flag(self):
        self.assertEqual(guard.next_actor(item(), {}), "")

    def test_a_finished_item_names_nobody(self):
        self.assertEqual(guard.next_actor(item(state="merged"), ROLES), "")


class TestTheStallLadder(unittest.TestCase):

    def rows(self):
        return [row(updated_at=1000.0, topic="tg-1", debtor="DeepSeek", kind="fix",
                    owes="a new head on #146", item="#146")]

    def test_nothing_climbs_before_the_deadline(self):
        self.assertEqual(guard.stalls(self.rows(), now=1000.0 + 60, minutes=30), [])

    def test_a_stalled_row_climbs_one_rung_at_a_time_each_once(self):
        rows = self.rows()
        stale = 1000.0 + 31 * 60
        first = guard.stalls(rows, now=stale, minutes=30)
        self.assertEqual([s.step for s in first], [guard.STALL_REMINDER])
        guard.advance(rows, first, now=stale)
        self.assertEqual(rows[0].stalls, 1)
        # ... the reminder happened once: the same table, a moment later, climbs to the next.
        self.assertEqual(len(guard.stalls(rows, now=stale + 1, minutes=30)), 0)
        rows[0].updated_at = stale - 31 * 60          # still stalled: the row did not move
        second = guard.stalls(rows, now=stale + 1, minutes=30)
        self.assertEqual([s.step for s in second], [guard.STALL_COORDINATOR])
        guard.advance(rows, second, now=stale + 1)
        rows[0].updated_at = stale - 31 * 60
        third = guard.stalls(rows, now=stale + 2, minutes=30)
        self.assertEqual([s.step for s in third], [guard.STALL_ADMIN])
        guard.advance(rows, third, now=stale + 2)
        rows[0].updated_at = stale - 31 * 60
        self.assertEqual(guard.stalls(rows, now=stale + 3, minutes=30), [])   # done, no loop

    def test_a_fact_that_moves_the_row_stops_the_ladder(self):
        rows = self.rows()
        first = guard.stalls(rows, now=1000.0 + 31 * 60, minutes=30)
        guard.advance(rows, first, now=1000.0 + 31 * 60)
        rows[0].updated_at = 1000.0 + 32 * 60          # something happened
        self.assertEqual(guard.stalls(rows, now=1000.0 + 40 * 60, minutes=30), [])

    def test_each_rung_says_something_different(self):
        rows = self.rows()
        texts = {guard.stall_text(rows[0], step, minutes=30) for step in guard.STALL_STEPS}
        self.assertEqual(len(texts), 3)
        self.assertIn("reminder", guard.stall_text(rows[0], guard.STALL_REMINDER, minutes=30))
        self.assertIn("Coordinator", guard.stall_text(rows[0], guard.STALL_COORDINATOR,
                                                      minutes=30))
        self.assertIn("admin", guard.stall_text(rows[0], guard.STALL_ADMIN, minutes=30))


class TestTheWakeBudget(unittest.TestCase):

    def budget(self, cap=3):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return guard.WakeBudget(Path(tmp.name) / "wake-budget.json", cap=cap,
                                clock=lambda: 1000.0)

    def test_it_stops_at_the_cap(self):
        budget = self.budget(cap=3)
        for _ in range(3):
            self.assertTrue(budget.allow("repo#167", now=1000.0))
            budget.spend("repo#167", now=1000.0)
        self.assertFalse(budget.allow("repo#167", now=1000.0))     # paused, and it says so
        self.assertIn("budget of 3", guard.budget_notice("repo#167", budget.cap))

    def test_the_count_survives_a_restart(self):
        budget = self.budget(cap=5)
        budget.spend("repo#167", now=1000.0)
        again = guard.WakeBudget(budget.path, cap=5, clock=lambda: 1000.0)
        self.assertEqual(again.used("repo#167", now=1000.0), 1)

    def test_a_new_day_starts_from_zero(self):
        budget = self.budget(cap=1)
        budget.spend("repo#167", now=1000.0)
        self.assertFalse(budget.allow("repo#167", now=1000.0))
        tomorrow = 1000.0 + 86400
        self.assertTrue(budget.allow("repo#167", now=tomorrow))

    def test_no_cap_is_no_cap(self):
        budget = self.budget(cap=0)
        self.assertTrue(all(budget.allow("repo#167", now=1000.0) for _ in range(50)))


class TestTheGuardTick(unittest.TestCase):
    """The transport half: a stalled row's rung becomes one turn, in the row's discussion."""

    def channel(self):
        bus = TemporaryDirectory()
        self.addCleanup(bus.cleanup)
        channel = make_channel(make_config(limits={"stall_minutes": 30},
                                          fallback={"gitbus_root": bus.name,
                                                    "gitbus_ref": ""}))
        channel.bot = object()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ow.OwedStore("Hermes", state_dir=Path(directory.name), clock=lambda: 1000.0)
        channel.owed_store = lambda: store
        channel._test_table = (directory, store)
        channel.topics.remember(GROUP, 5122, "round 5")
        turns = []
        channel._run_own_task = lambda text, chat, thread, name: turns.append(
            (text, chat, thread, name))
        channel._test_turns = turns
        return channel

    def test_a_stalled_row_gets_its_reminder_then_says_the_coordinator_is_next(self):
        channel = self.channel()
        channel._test_table[1].save_all([
            row(kind="fix", debtor="Hermes", owes="a new head on #146", item="#146",
                updated_at=1000.0, topic=channel.topics.context(GROUP, 5122))])
        self.assertEqual(channel.owed_guard_tick(now=1000.0 + 10 * 60), [])   # not yet
        done = channel.owed_guard_tick(now=1000.0 + 31 * 60)
        self.assertEqual(len(channel._test_turns), 1)
        self.assertIn("reminder", channel._test_turns[0][0])
        self.assertEqual((channel._test_turns[0][1], channel._test_turns[0][2]), (GROUP, 5122))
        self.assertEqual([r.stalls for r in channel._test_table[1].load()], [1])
        self.assertIn("reminder", " ".join(done))

    def test_a_busy_topic_holds_the_rung(self):
        channel = self.channel()
        channel._test_table[1].save_all([
            row(kind="fix", debtor="Hermes", owes="a new head on #146", item="#146",
                updated_at=1000.0, topic=channel.topics.context(GROUP, 5122))])
        channel._busy_since[(GROUP, 5122)] = 1000.0 + 30 * 60
        done = channel.owed_guard_tick(now=1000.0 + 31 * 60)
        self.assertEqual(channel._test_turns, [])
        self.assertIn("held: turn running", " ".join(done))
        self.assertEqual([r.stalls for r in channel._test_table[1].load()], [0])  # not spent

    def test_a_peers_row_is_not_climbed_here(self):
        """#177's rule, again: the table folds every writer's documents, so ownership is checked."""
        channel = self.channel()
        channel._test_table[1].save_all([
            row(kind="fix", debtor="DeepSeek", owes="a new head on #146", item="#146",
                updated_at=1000.0, topic=channel.topics.context(GROUP, 5122))])
        done = channel.owed_guard_tick(now=1000.0 + 31 * 60)
        self.assertEqual(channel._test_turns, [])                      # no turn for a peer's debt
        self.assertIn("not this bot's rung", " ".join(done))
        self.assertEqual([r.stalls for r in channel._test_table[1].load()], [0])  # unspent

    def test_the_coordinator_rung_needs_the_coordinator(self):
        channel = self.channel()
        channel._test_table[1].save_all([
            row(kind="fix", debtor="Hermes", owes="a new head on #146", item="#146",
                updated_at=1000.0, stalls=1, topic=channel.topics.context(GROUP, 5122))])
        done = channel.owed_guard_tick(now=1000.0 + 31 * 60)
        self.assertEqual(channel._test_turns, [])                      # not the coordinator here
        self.assertIn("only the coordinator climbs this rung", " ".join(done))
        self.assertEqual([r.stalls for r in channel._test_table[1].load()], [1])
        # ... and the coordinator delivers it.
        channel.is_coordinator = lambda **kwargs: True
        channel.owed_guard_tick(now=1000.0 + 31 * 60)
        self.assertEqual(len(channel._test_turns), 1)
        self.assertEqual([r.stalls for r in channel._test_table[1].load()], [2])

    def test_two_bots_on_one_table_climb_one_rung_between_them(self):
        """Four bots on the beat used to mean four reminders and four rungs for one stall."""
        first = self.channel()
        store = first._test_table[1]
        store.save_all([row(kind="fix", debtor="Hermes", owes="a new head on #146", item="#146",
                            updated_at=1000.0, topic=first.topics.context(GROUP, 5122))])
        second = self.channel()
        second.owed_store = lambda: store                       # the same table, another bot
        second._test_table = (first._test_table[0], store)
        first.owed_guard_tick(now=1000.0 + 31 * 60)
        second.owed_guard_tick(now=1000.0 + 31 * 60)
        self.assertEqual(len(first._test_turns) + len(second._test_turns), 1)   # one turn
        self.assertEqual([r.stalls for r in store.load()], [1])                 # one rung


class TestTheBudgetBelongsToTheCurrentRound(unittest.TestCase):
    """A cap counted against the wrong round can pause a round nobody is spending in (#191)."""

    def channel(self, **limits):
        bus = TemporaryDirectory()
        self.addCleanup(bus.cleanup)
        return make_channel(make_config(limits=limits or {"stall_minutes": 30},
                                       fallback={"gitbus_root": bus.name,
                                                 "gitbus_ref": ""}))

    def rounds(self, *updates):
        from swarmly.telegram import rounds as rs
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = rs.RoundStore(None, root=Path(tmp.name), alias="Hermes",
                             authority=rs.Authority(alias="Hermes"))
        for update in updates:
            store.append(rs.RoundUpdate(writer="Hermes", **update))
        return store

    def test_the_newest_round_wins_not_the_last_name(self):
        """Round keys are names: sorted(), "repo#5" comes after "repo#10" and is older."""
        channel = self.channel()
        channel.round_store = lambda: self.rounds(
            {"round": "repo#5", "term": 5, "seq": 2},
            {"round": "repo#10", "term": 10, "seq": 1})
        self.assertEqual(channel.owed_round_key(), "repo#10")

    def test_no_readable_round_falls_back_to_a_name(self):
        channel = self.channel()
        channel.round_store = lambda: self.rounds({"round": "repo#5", "term": 5})
        self.assertEqual(channel.owed_round_key(), "repo#5")
        channel.round_store = lambda: self.rounds()
        self.assertEqual(channel.owed_round_key(), "")


class TestTheBudgetNoticeComesBackTomorrow(unittest.TestCase):
    """A one-shot boolean left the admin told once, ever (#191 review)."""

    def channel(self):
        from swarmly.telegram import owed_guard as guard
        bus = TemporaryDirectory()
        self.addCleanup(bus.cleanup)
        channel = make_channel(make_config(limits={"stall_minutes": 30, "round_wake_budget": 1},
                                          fallback={"gitbus_root": bus.name,
                                                    "gitbus_ref": ""}))
        channel.bot = object()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ow.OwedStore("Hermes", state_dir=Path(directory.name), clock=lambda: 1000.0)
        channel.owed_store = lambda: store
        channel._test_table = (directory, store)
        channel.topics.remember(GROUP, 5122, "round 5")
        turns = []
        channel._run_own_task = lambda text, chat, thread, name: turns.append(
            (text, chat, thread, name))
        channel._test_turns = turns
        channel.is_coordinator = lambda **kwargs: True
        channel.owed_round_key = lambda: "repo#167"
        budget_file = Path(directory.name) / "owed-wake-budget.json"
        channel.owed_budget = lambda: guard.WakeBudget(budget_file, cap=1, clock=time.time)
        return channel

    def stall(self, channel):
        """Two rows owed here, so the cap (1) is spent on the first tick and hit on the second."""
        token = channel.topics.context(GROUP, 5122)
        channel._test_table[1].save_all([
            row(kind="fix", debtor="Hermes", owes="a new head on #146", item="#146",
                observed="green", topic=token),
            row(kind="fix", debtor="Hermes", owes="a new head on #147", item="#147",
                observed="green", topic=token),
        ])

    def notices(self, channel, from_index):
        return [text for text, _c, _t, _n in channel._test_turns[from_index:]
                if "budget" in text]

    def test_the_notice_returns_after_midnight(self):
        channel = self.channel()
        self.stall(channel)
        channel.owed_wake_tick(now=1000.0)              # spends the day's one wake
        channel.owed_wake_tick(now=1060.0)              # hits the cap, tells once
        self.assertEqual(len(self.notices(channel, 0)), 1)
        seen = len(channel._test_turns)
        channel.owed_wake_tick(now=1120.0)              # same day, same round: quiet
        self.assertEqual(self.notices(channel, seen), [])
        channel.owed_wake_tick(now=1000.0 + 86400)      # tomorrow: allowed, then capped again
        channel.owed_wake_tick(now=1000.0 + 86460.0)
        self.assertEqual(len(self.notices(channel, seen)), 1)
