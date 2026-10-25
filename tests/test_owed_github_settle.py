"""A merged PR closes the rows bound to it, whatever the round state says (live, 24 Oct).

Live: "Reasonix owes a merge" rows for #230, #239 and #252 stayed open for days after those PRs
merged -- their rounds had closed, or never marked the item done -- and the stall ladder handed
each one to the coordinator, whose turn could only ask the admin to clear it. GitHub's word on
the PR is the fact: a merge closes the rows before anything wakes, and the sweep that follows
counts the item done.
"""

import tempfile
import unittest
from pathlib import Path

from swarmly.telegram import owed as ow
from swarmly.telegram.channel import OWED_GITHUB_ITEMS_PER_BEAT, OWED_GITHUB_RECHECK_SECONDS
from tests.test_owed_wiring import FakeRounds, round_state
from tests.test_telegram_channel import make_channel, make_config, run

NOW = 2_000_000.0
REPO = "owner/swarmly"


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

    def __call__(self, repo, label):
        self.asked.append((repo, label))
        return self.states.get(label)


def channel_with(rows, states, *, coordinating=True, github=True, repos=(REPO,)):
    folder = tempfile.TemporaryDirectory()
    projects = {f"p{n}": {"repo": repo, "cwd": folder.name, "topics": []}
                for n, repo in enumerate(repos)}
    sections = {"projects": projects}
    if github:
        sections["github"] = {"login": "x"}
    channel = make_channel(make_config(**sections))
    store = ow.OwedStore("Hermes", state_dir=Path(folder.name), clock=lambda: NOW)
    store.save_all(rows)
    channel.owed_store = lambda: store
    channel.is_coordinator = lambda now=None: coordinating
    channel.owed_item_reader = Reader(states)
    channel._owed_merged_path = lambda: Path(folder.name) / "owed-merged.json"
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
        self.assertIn((REPO, "#252"), channel.owed_item_reader.asked)

    def test_the_stall_ladder_has_nothing_left_to_hand_the_coordinator(self):
        channel, _store = channel_with([merge("#239")], {"#239": "merged"})
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_guard_tick(now=NOW + 7 * 86400), [])

    def test_a_close_never_closes_a_merge_and_unknown_closes_nothing(self):
        # A closed PR can be reopened, and a row closed here stays closed: only a merge settles
        # the merge itself.
        channel, store = channel_with([merge("#260"), merge("#261"), progress("#262")],
                                      {"#260": "closed", "#261": None, "#262": "open"})
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(len(open_ids(store)), 3)

    def test_a_row_under_an_issue_asks_about_the_pr_that_carries_it(self):
        channel, store = channel_with([merge("#227")], {"#227": "closed", "#230": "merged"})
        channel._owed_round_prs = {(REPO, "#227"): "#230"}
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [(REPO, "#230")])
        self.assertEqual(open_ids(store), [])

    def test_a_merge_is_never_asked_about_again(self):
        channel, _store = channel_with([merge("#252")], {"#252": "merged"})
        run(channel.owed_settle_from_github(now=NOW))
        channel.owed_store().save_all([merge("#252", debtor="Codex")])
        run(channel.owed_settle_from_github(now=NOW + 2 * OWED_GITHUB_RECHECK_SECONDS))
        self.assertEqual(channel.owed_item_reader.asked, [(REPO, "#252")])
        self.assertEqual(open_ids(channel.owed_store()), [])


class TestAClosedItemClosesItsProgressAndVerdict(unittest.TestCase):
    """#276, folded in: nobody owes progress or a verdict on an issue or PR that was closed."""

    def test_a_closed_issue_closes_its_progress_and_verdict_rows_only(self):
        verdict = ow.Row(kind="verdict", item="#167", head="34a288f", debtor="Antigravity",
                         owes="a review verdict", created_at=NOW - 86400,
                         updated_at=NOW - 86400)
        channel, store = channel_with([progress("#167"), verdict, merge("#167")],
                                      {"#167": "closed"})
        done = run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(len(done), 2, done)
        self.assertEqual(open_ids(store), [merge("#167").id])
        closed = {row.id: row for row in store.load() if not row.is_open}
        self.assertEqual(closed[progress("#167").id].closed_by, "#167 closed on GitHub")

    def test_a_closed_pr_that_carried_an_issue_closes_none_of_the_issues_rows(self):
        # The issue's work may go on in another PR.
        channel, store = channel_with([progress("#227")], {"#230": "closed"})
        channel._owed_round_prs = {(REPO, "#227"): "#230"}
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(open_ids(store), [progress("#227").id])


class TestTheSweepCountsAMergedPrDone(unittest.TestCase):
    """The round still lists the item: without this, the sweep's orphan invariant opened a
    "nobody owes the next step" row for it and woke the coordinator in the same beat."""

    def test_an_item_whose_pr_merged_opens_nothing(self):
        channel, store = channel_with([merge("#252", debtor="Claudebot")], {"#252": "merged"})
        channel.owed_roles = lambda: {"coordinator": "Claudebot", "developer": "Codex"}
        items = {"252": {"pr": 252, "head": "474778a", "owner": "Codex", "ci": "green"}}
        channel.round_store = lambda: FakeRounds({"repo#167": round_state(items)})
        run(channel.owed_settle_from_github(now=NOW))
        channel.owed_sweep(now=NOW)
        self.assertEqual(open_ids(store), [])

    def test_the_sweep_maps_an_issue_to_its_pr_for_the_next_check(self):
        channel, store = channel_with([merge("#227", debtor="Claudebot")],
                                      {"#227": "closed", "#230": "merged"})
        channel.owed_roles = lambda: {"coordinator": "Claudebot", "developer": "Codex"}
        items = {"227": {"pr": 230, "head": "474778a", "owner": "Codex", "ci": "pending"}}
        channel.round_store = lambda: FakeRounds({"repo#167": round_state(items)})
        channel.owed_sweep(now=NOW)
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [(REPO, "#230")])
        channel.owed_sweep(now=NOW)
        self.assertEqual(open_ids(store), [])


    def test_a_restart_remembers_the_merge(self):
        """Review: forgotten on a restart, the merge let the first sweep open a "next actor"
        row for the item and wake somebody before the next beat closed it again."""
        channel, store = channel_with([merge("#252", debtor="Claudebot")], {"#252": "merged"})
        run(channel.owed_settle_from_github(now=NOW))
        again, _ = channel_with([], {})
        again.owed_store = lambda: store
        again._owed_merged_path = channel._owed_merged_path
        again.owed_roles = lambda: {"coordinator": "Claudebot", "developer": "Codex"}
        items = {"252": {"pr": 252, "head": "474778a", "owner": "Codex", "ci": "green"}}
        again.round_store = lambda: FakeRounds({"repo#167": round_state(items)})
        again.owed_sweep(now=NOW)
        self.assertEqual(open_ids(store), [])
        self.assertEqual(again.owed_item_reader.asked, [])

    def test_two_rounds_that_disagree_on_the_pr_name_none(self):
        channel, store = channel_with([merge("#227", debtor="Claudebot")],
                                      {"#227": "closed", "#230": "merged", "#240": "open"})
        channel.owed_roles = lambda: {"coordinator": "Claudebot", "developer": "Codex"}
        old = {"227": {"pr": 230, "head": "474778a", "owner": "Codex", "ci": "pending"}}
        new = {"227": {"pr": 240, "head": "5b90b50", "owner": "Codex", "ci": "pending"}}
        channel.round_store = lambda: FakeRounds({"repo#1": round_state(old),
                                                  "repo#2": round_state(new)})
        channel.owed_sweep(now=NOW)
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [(REPO, "#227")])
        self.assertIn(merge("#227", debtor="Claudebot").id, open_ids(store))


class TestWhoAsksWhereAndHowOften(unittest.TestCase):

    def test_a_bot_that_does_not_coordinate_closes_only_its_own_debts(self):
        mine = merge("#252", debtor="Hermes")
        channel, store = channel_with([mine, merge("#252"), merge("#230")],
                                      {"#252": "merged", "#230": "merged"}, coordinating=False)
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [(REPO, "#252")])
        self.assertEqual(open_ids(store), sorted([merge("#252").id, merge("#230").id]))

    def test_with_several_repos_a_row_that_names_no_place_is_left_alone(self):
        channel, store = channel_with([merge("#252")], {"#252": "merged"},
                                      repos=(REPO, "owner/other"))
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(channel.owed_item_reader.asked, [])

    def test_with_several_repos_the_round_that_lists_the_item_names_it(self):
        channel, store = channel_with([merge("#252")], {"#252": "merged"},
                                      repos=(REPO, "owner/other"))
        channel._owed_round_prs = {("owner/other", "#252"): "#252"}
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [("owner/other", "#252")])
        self.assertEqual(open_ids(store), [])

    def test_a_repo_spelled_in_another_case_is_the_same_repo(self):
        channel, store = channel_with([merge("#252")], {"#252": "merged"},
                                      repos=("Owner/Swarmly", "owner/swarmly"))
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [("owner/swarmly", "#252")])
        self.assertEqual(open_ids(store), [])

    def test_a_wake_row_is_read_in_the_repo_its_check_named(self):
        row = ow.Row(kind="progress", item="#45", debtor="Reasonix", owes="a look",
                     key_extra="wake#abc",
                     opened_by="a WAKE line that checked `gh pr checks 45` in Owner/Other",
                     created_at=NOW - 86400, updated_at=NOW - 86400)
        channel, store = channel_with([row], {"#45": "merged"})
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(channel.owed_item_reader.asked, [("owner/other", "#45")])
        self.assertEqual(open_ids(store), [])

    def test_an_open_item_is_asked_again_only_after_the_recheck_window(self):
        channel, _store = channel_with([merge("#260")], {"#260": "open"})
        run(channel.owed_settle_from_github(now=NOW))
        run(channel.owed_settle_from_github(now=NOW + 60))
        self.assertEqual(len(channel.owed_item_reader.asked), 1)
        run(channel.owed_settle_from_github(now=NOW + OWED_GITHUB_RECHECK_SECONDS + 1))
        self.assertEqual(len(channel.owed_item_reader.asked), 2)

    def test_a_backlog_is_asked_about_a_few_items_a_beat(self):
        rows = [merge(f"#{200 + n}") for n in range(OWED_GITHUB_ITEMS_PER_BEAT + 3)]
        channel, store = channel_with(rows, {row.item: "merged" for row in rows})
        run(channel.owed_settle_from_github(now=NOW))
        self.assertEqual(len(channel.owed_item_reader.asked), OWED_GITHUB_ITEMS_PER_BEAT)
        run(channel.owed_settle_from_github(now=NOW + 60))
        self.assertEqual(open_ids(store), [])

    def test_a_bot_without_github_settings_reads_nothing(self):
        channel, _store = channel_with([merge("#252")], {"#252": "merged"}, github=False)
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(channel.owed_item_reader.asked, [])

    def test_a_held_swarm_reads_nothing(self):
        channel, _store = channel_with([merge("#252")], {"#252": "merged"})
        channel.held = lambda: True
        self.assertEqual(run(channel.owed_settle_from_github(now=NOW)), [])
        self.assertEqual(channel.owed_item_reader.asked, [])


class TestTheRealReader(unittest.TestCase):

    def test_it_asks_github_once_for_issue_or_pr_and_caches_it(self):
        calls = []

        class Client:
            def ref_state(self, repo, number):
                calls.append((repo, number))
                return "merged"

        channel, _store = channel_with([], {})
        channel.owed_item_reader = None
        channel._github_client = lambda: Client()
        self.assertEqual(channel._owed_item_state(REPO, "#252"), "merged")
        self.assertEqual(channel._owed_item_state(REPO, "#252"), "merged")
        self.assertEqual(calls, [(REPO, 252)])


if __name__ == "__main__":
    unittest.main()


class TheRealClientTests(unittest.TestCase):
    """Live, 25 Oct: #308 removed _github_client and the settle check raised every heartbeat;
    the tests had all stubbed it."""

    def test_the_channel_builds_its_own_client(self):
        from tests.test_telegram_channel import make_channel
        channel = make_channel()
        client = channel._github_client()
        self.assertIs(channel._github_client(), client)
        self.assertTrue(hasattr(client, "ref_state"))
