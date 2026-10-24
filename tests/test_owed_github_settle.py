"""A merged PR closes the rows bound to it, whatever the round state says (live, 24 Oct).

Live: "Reasonix owes a merge" rows for #230, #239 and #252 stayed open for days after those PRs
merged -- their rounds had closed, or never marked the item done -- and the stall ladder handed
each one to the coordinator, whose turn could only ask the admin to clear it. What GitHub says
about the item is the fact: it closes the rows before anything wakes.
"""

import tempfile
import unittest
from pathlib import Path

from swarmly.telegram import owed as ow
from swarmly.telegram.channel import OWED_GITHUB_ITEMS_PER_BEAT, OWED_GITHUB_RECHECK_SECONDS
from tests.test_telegram_channel import make_channel, make_config, run

NOW = 2_000_000.0


def merge(item, debtor="Reasonix", head="474778a"):
    return ow.Row(kind="merge", item=item, head=head, debtor=debtor, owes=f"a merge of {item}",
                  created_at=NOW - 86400, updated_at=NOW - 86400)


def progress(item, debtor="Codex"):
    return ow.Row(kind="progress", item=item, debtor=debtor, owes=f"progress on {item}",
                  created_at=NOW - 86400, updated_at=NOW - 86400)


class Reader:
    def __init__(self, states):
        self.states = states
        self.asked = []

    def __call__(self, item, chat_id, thread_id):
        self.asked.append(item)
        return self.states.get(item)


def channel_with(rows, states, *, coordinating=True, github=True):
    config = make_config(github={"login": "x"}) if github else make_config()
    channel = make_channel(config)
    folder = tempfile.TemporaryDirectory()
    store = ow.OwedStore("Hermes", state_dir=Path(folder.name), clock=lambda: NOW)
    store.save_all(rows)
    channel.owed_store = lambda: store
    channel.is_coordinator = lambda now=None: coordinating
    channel.owed_item_reader = Reader(states)
    channel._test_folder = folder
    return channel, store


def open_ids(store):
    return sorted(row.id for row in store.open_rows(now=NOW))


class TestAMergedPrClosesItsRows(unittest.TestCase):

    def test_a_merged_pr_closes_every_row_bound_to_it(self):
        channel, store = channel_with([merge("#252"), progress("#252"), merge("#230")],
                                      {"#252": "merged", "#230": "open"})
        done = run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(len(done), 2, done)
        self.assertEqual(open_ids(store), [merge("#230").id])
        closed = {row.id: row for row in store.load() if not row.is_open}
        self.assertEqual(closed[merge("#252").id].closed_by, "merged on GitHub")

    def test_the_stall_ladder_has_nothing_left_to_hand_the_coordinator(self):
        channel, store = channel_with([merge("#239")], {"#239": "merged"})
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_guard_tick(now=NOW + 7 * 86400), [])

    def test_a_closed_issue_closes_progress_but_not_a_merge(self):
        # The same rule the round's own facts follow (owed.closes): a closed issue answers its
        # progress and verdict rows; a merge is answered by a merge.
        channel, store = channel_with([progress("#227"), merge("#227")], {"#227": "closed"})
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(open_ids(store), [merge("#227").id])

    def test_unknown_or_open_is_never_a_fact(self):
        channel, store = channel_with([merge("#260"), merge("#261")],
                                      {"#260": "open", "#261": None})
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(len(open_ids(store)), 2)


class TestWhoAsksAndHowOften(unittest.TestCase):

    def test_a_bot_that_does_not_coordinate_checks_only_its_own_debts(self):
        mine = merge("#252", debtor="Hermes")
        channel, store = channel_with([mine, merge("#230")],
                                      {"#252": "merged", "#230": "merged"}, coordinating=False)
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, ["#252"])
        self.assertEqual(open_ids(store), [merge("#230").id])

    def test_an_open_item_is_asked_again_only_after_the_recheck_window(self):
        channel, _store = channel_with([merge("#260")], {"#260": "open"})
        run(channel.owed_settle_from_github(now=NOW))
        run(channel.owed_settle_from_github(now=NOW + 60))
        self.assertEqual(channel.owed_item_reader.asked, ["#260"])
        run(channel.owed_settle_from_github(now=NOW + OWED_GITHUB_RECHECK_SECONDS + 1))
        self.assertEqual(channel.owed_item_reader.asked, ["#260", "#260"])

    def test_a_backlog_is_asked_about_a_few_items_a_beat(self):
        rows = [merge(f"#{200 + n}") for n in range(OWED_GITHUB_ITEMS_PER_BEAT + 3)]
        channel, store = channel_with(rows, {row.item: "merged" for row in rows})
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(len(channel.owed_item_reader.asked), OWED_GITHUB_ITEMS_PER_BEAT)
        run(channel.owed_settle_from_github(now=NOW + 60))
        self.assertEqual(open_ids(store), [])

    def test_a_bot_without_github_settings_reads_nothing(self):
        channel, store = channel_with([merge("#252")], {"#252": "merged"}, github=False)
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(channel.owed_item_reader.asked, [])

    def test_a_held_swarm_reads_nothing(self):
        channel, _store = channel_with([merge("#252")], {"#252": "merged"})
        channel.held = lambda: True
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(channel.owed_item_reader.asked, [])


if __name__ == "__main__":
    unittest.main()
