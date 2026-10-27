"""#428: a hand-off is settled by the item it names, and never kept in a private chat.

Live, 27 Oct - 18 Nov: the admin kept getting reminders in the private chat with Claudebot,
addressed to DeepSeek, for a #424 hand-off written there. #424 merged long ago. A hand-off was
answered only by the peer posting in the same chat/thread -- which a peer can never do in the
admin's private chat -- and the row did not say which item it was for, so the merge settled
nothing. Both are fixed here: the row names its item, a merge or a close of that item answers
it, and a private chat keeps no hand-off row and gets no reminder.
"""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from swarmly.state import keys as K
from swarmly.telegram.owed import Fact, closes, handoff_rows, settle
from tests.test_telegram_channel import ADMIN_ID, GROUP, make_channel, make_config, run

TOPIC = 307
ITEM = "https://github.com/owner/swarmly/pull/424"


def a_channel(tmp, **overrides):
    limits = {"handoff_nudge_minutes": 10}
    limits.update(overrides.pop("limits", {}))
    channel = make_channel(make_config(limits=limits, state={"dir": tmp}, **overrides))
    channel.bot = object()
    channel._start = channel._clock()
    return channel


def tick(channel, minutes):
    """Check the hand-offs ``minutes`` after they were made."""
    channel._clock = (lambda t=channel._start + minutes * 60: t)
    run(channel.check_stalled_handoffs())
    return channel._test_sender.sent


def a_registry(done, ref=ITEM):
    """A registry store reader: one topic whose work link is #424, done or open."""
    record = {"status": "open", "links": [{
        "id": "L1", "provider": "github", "kind": "pr", "role": "work", "ref": ref,
        "status": "closed" if done else "open", "phase": "done" if done else "development",
        "added_at": "2026-10-27T10:00:00Z"}]}

    class Store:
        ready = True

        def keys(self, prefix=""):
            return [K.topic(9400)]

        def read(self, key):
            return record

    return Store()


class TheRowNamesItsItemTests(unittest.TestCase):

    def test_a_hand_off_row_carries_the_item_the_hand_off_names(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("Thanks.\n@CodexBot please review #424", GROUP, TOPIC, 55)
            rows = channel.owed_store().open_rows()
        self.assertEqual([(row.kind, row.item) for row in rows], [("handoff", "#424")])

    def test_each_peer_gets_the_item_from_its_own_line(self):
        # #429 review: one reply handing two items to two peers opened two rows that both
        # named the first item, so the second peer's debt was settled by the wrong merge.
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs(
                "@CodexBot please review #424\n@AntigravityBot please QA #430",
                GROUP, TOPIC, 55)
            rows = {row.debtor: row.item for row in channel.owed_store().open_rows()}
        self.assertEqual(rows, {"Codex": "#424", "Antigravity": "#430"})

    def test_a_handle_alone_block_gives_its_item(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("@CodexBot:\n\nPlease review #424.", GROUP, TOPIC, 55)
            rows = {row.debtor: row.item for row in channel.owed_store().open_rows()}
        self.assertEqual(rows, {"Codex": "#424"})

    def test_a_merge_or_a_close_of_that_item_answers_it(self):
        def row():
            return handoff_rows(asked=["Codex"], asker="Hermes", item="#424",
                                place="tg-307", topic="tg-307")[0]

        for fact in (Fact(kind="merge", item="#424"), Fact(kind="close", item="#424")):
            with self.subTest(fact=fact.kind):
                self.assertEqual([r.id for r in settle([row()], fact)], [row().id])
        # Another item's merge answers nothing: the row is bound to the item it named.
        self.assertIsNone(closes(row(), Fact(kind="merge", item="#260")))
        self.assertIsNone(closes(row(), Fact(kind="merge")))


class APrivateChatKeepsNoHandOffTests(unittest.TestCase):

    def test_a_row_written_there_before_427_is_cleared_and_nothing_is_posted(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            place = channel._private_chat_place()
            self.assertTrue(place, "the admin's private chat has a place key")
            store = channel.owed_store()
            store.save_all(store.observe(handoff_rows(
                asked=["Codex"], asker="Claudebot", item="#424", place=place,
                opened_by="hand-off in a topic")))
            self.assertEqual(len(store.open_rows()), 1)

            closed = channel.owed_settle_private_chat()

            self.assertEqual(len(closed), 1, closed)
            self.assertIn("private chat", closed[0])
            self.assertEqual(channel.owed_store().open_rows(), [])
        self.assertEqual(channel._test_sender.sent, [])

    def test_a_row_in_a_work_topic_is_left_alone(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            store = channel.owed_store()
            store.save_all(store.observe(handoff_rows(
                asked=["Codex"], asker="Claudebot", item="#424", place="tg-307",
                topic="tg-307", opened_by="hand-off in a topic")))
            self.assertEqual(channel.owed_settle_private_chat(), [])
            self.assertEqual(len(channel.owed_store().open_rows()), 1)

    def test_no_row_is_written_for_a_hand_off_made_there_now(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("@CodexBot please take #424", ADMIN_ID, None, 55)
            self.assertEqual(channel.owed_store().open_rows(), [])

    def test_a_row_from_before_427_is_closed_by_a_beat_with_nothing_posted(self):
        # #429 review: the whole path, not just the sweep method -- a transport that comes up
        # on old rows clears them on its beat, and the private chat gets no reminder.
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            place = channel._private_chat_place()
            store = channel.owed_store()
            store.save_all(store.observe(handoff_rows(
                asked=["Codex"], asker="Claudebot", item="#424", place=place,
                opened_by="hand-off in a topic")))
            run(channel._heartbeat_tick())
            self.assertEqual(channel.owed_store().open_rows(), [])
        posted = [str(sent) for sent in channel._test_sender.sent]
        self.assertFalse([text for text in posted
                          if "reminder" in text or "has not answered" in text], posted)

    def test_no_reminder_is_posted_there(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._pending_handoffs[(ADMIN_ID, None, "Codex")] = {
                "at": channel._clock(), "message_id": 5, "nudged": False, "item": ""}
            self.assertEqual(tick(channel, 11), [])        # the first rung: no reminder
            self.assertEqual(tick(channel, 30), [])        # nor the notice after it
            self.assertEqual(channel._pending_handoffs, {})


class AFinishedHandOffIsNotChasedTests(unittest.TestCase):

    def test_a_merged_item_stops_the_reminder(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("@CodexBot please review #424", GROUP, TOPIC, 55)
            # GitHub has shown this bot the PR merged (kept on disk by owed_settle_from_github).
            channel._owed_merged = {("owner/swarmly", "#424")}
            self.assertEqual(tick(channel, 11), [])
            self.assertEqual(channel._pending_handoffs, {})

    def test_a_registry_item_past_done_stops_it(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("@CodexBot please review #424", GROUP, TOPIC, 55)
            channel.state_link.store = a_registry(done=True)
            self.assertEqual(tick(channel, 11), [])

    def test_another_repos_done_item_does_not_stop_it(self):
        # #429 review, the nit: #424 in another repo is not this repo's #424.
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp, projects={"swarmly": {"repo": "owner/swarmly",
                                                            "cwd": "."}})
            channel._mirror_handoffs("@CodexBot please review #424", GROUP, TOPIC, 55)
            channel.state_link.store = a_registry(
                done=True, ref="https://github.com/other/repo/pull/424")
            sent = tick(channel, 11)
            self.assertEqual(len(sent), 1, sent)
            self.assertIn("@CodexBot reminder", str(sent[-1]))

    def test_an_open_item_still_gets_its_reminder(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("@CodexBot please review #424", GROUP, TOPIC, 55)
            channel.state_link.store = a_registry(done=False)
            sent = tick(channel, 11)
            self.assertEqual(len(sent), 1, sent)
            self.assertIn("@CodexBot reminder", str(sent[-1]))

    def test_a_hand_off_naming_no_item_is_reminded(self):
        with TemporaryDirectory() as tmp:
            channel = a_channel(tmp)
            channel._mirror_handoffs("@CodexBot please run the suite", GROUP, TOPIC, 55)
            sent = tick(channel, 11)
            self.assertEqual(len(sent), 1, sent)
            self.assertIn("@CodexBot reminder", str(sent[-1]))


if __name__ == "__main__":
    unittest.main()
