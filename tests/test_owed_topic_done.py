"""A hand-off nobody can answer any more closes (live, 27 Oct).

Live: "Codex owes an answer" stayed open in the topic of a QA on a PR that had merged the night
before. A hand-off closes only on the peer's reply where it was made, Codex never spoke there
again, and the stall ladder ended at "the admin should decide" with nothing the admin could do.
Two facts close it now: every work item the topic links finished after the hand-off, and the
admin's ``/owed close`` in that discussion.
"""

import tempfile
import time
import unittest
from pathlib import Path

from swarmly.state import keys as K
from swarmly.telegram import owed as ow
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, run

NOW = 2_000_000_000.0
TOPIC = "tg-307"


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def handoff(debtor="Codex", topic=TOPIC, created=NOW - 86400):
    return ow.Row(kind="handoff", item="", debtor=debtor, owes="an answer", waiting_on="Claudebot",
                  topic=topic, key_extra=topic, created_at=created, updated_at=created)


def link(status="closed", phase="done", at=NOW - 3600, ident="L1"):
    return {"id": ident, "provider": "github", "kind": "pr", "role": "work",
            "ref": "https://github.com/owner/swarmly/pull/372", "status": status,
            "phase": phase, "updated_at": iso(at)}


class Registry:
    ready = True

    def __init__(self, records):
        self.records = records

    def read(self, key):
        return self.records.get(key)


def channel_with(rows, records, *, coordinating=True, alias_rows_writer="Claudebot"):
    folder = tempfile.TemporaryDirectory()
    channel = make_channel()
    store = ow.OwedStore(alias_rows_writer, state_dir=Path(folder.name), clock=lambda: NOW)
    store.save_all(rows)
    channel.owed_store = lambda: store
    channel.is_coordinator = lambda now=None: coordinating
    channel.state_link.store = Registry(records)
    channel._place_target = lambda token: (GROUP, int(token.split("-")[-1]))
    channel._place_key = lambda chat_id, thread_id: f"tg-{thread_id}" if thread_id else "gen"
    channel._test_folder = folder
    return channel, store


def open_ids(store):
    return sorted(row.id for row in store.open_rows(now=NOW))


class TestAFinishedTopicClosesItsHandOffs(unittest.TestCase):

    def test_a_hand_off_made_before_the_work_finished_closes(self):
        channel, store = channel_with([handoff()], {K.topic(307): {"links": [link()]}})
        done = channel.owed_settle_topics_done(now=NOW)
        self.assertEqual(len(done), 1)
        self.assertEqual(open_ids(store), [])
        self.assertIn("work is done", done[0])

    def test_a_hand_off_made_after_the_work_finished_stays(self):
        channel, store = channel_with([handoff(created=NOW - 60)],
                                      {K.topic(307): {"links": [link(at=NOW - 3600)]}})
        self.assertEqual(channel.owed_settle_topics_done(now=NOW), [])
        self.assertEqual(len(open_ids(store)), 1)

    def test_a_topic_with_open_work_or_none_closes_nothing(self):
        for record in ({"links": [link(), link(status="open", phase="review", ident="L2")]},
                       {"links": []}, None):
            channel, store = channel_with([handoff()],
                                          {K.topic(307): record} if record else {})
            self.assertEqual(channel.owed_settle_topics_done(now=NOW), [])
            self.assertEqual(len(open_ids(store)), 1)

    def test_only_hand_offs_close(self):
        verdict = ow.Row(kind="verdict", item="#372", head="474778a", debtor="Codex",
                         owes="a QA verdict", topic=TOPIC, created_at=NOW - 86400)
        channel, store = channel_with([verdict], {K.topic(307): {"links": [link()]}})
        self.assertEqual(channel.owed_settle_topics_done(now=NOW), [])

    def test_a_peer_that_does_not_coordinate_leaves_others_rows_alone(self):
        channel, store = channel_with([handoff()], {K.topic(307): {"links": [link()]}},
                                      coordinating=False)
        self.assertEqual(channel.owed_settle_topics_done(now=NOW), [])

    def test_no_registry_closes_nothing(self):
        channel, store = channel_with([handoff()], {K.topic(307): {"links": [link()]}})
        channel.state_link.store = None
        self.assertEqual(channel.owed_settle_topics_done(now=NOW), [])


class TestTheAdminClosesARow(unittest.TestCase):

    def admin(self, text, thread_id=307):
        return event(text, author_id=ADMIN_ID, username="admin", is_bot=False,
                     thread_id=thread_id)

    def test_owed_close_closes_the_rows_owed_here(self):
        elsewhere = handoff(debtor="Reasonix", topic="tg-400")
        channel, store = channel_with([handoff(), elsewhere], {})
        reply = channel._handle_command(self.admin("/owed@HermesBot close"))
        self.assertIn("Closed 1", reply)
        self.assertIn("Codex", reply)
        self.assertEqual(open_ids(store), [elsewhere.id])

    def test_owed_close_names_one_debtor(self):
        other = handoff(debtor="Reasonix")
        channel, store = channel_with([handoff(), other], {})
        reply = channel._handle_command(self.admin("/owed@HermesBot close Codex"))
        self.assertIn("Closed 1", reply)
        self.assertEqual(open_ids(store), [other.id])

    def test_a_bare_owed_close_is_the_coordinators(self):
        channel, store = channel_with([handoff()], {}, coordinating=False)
        self.assertIsNone(channel._handle_command(self.admin("/owed close")))
        self.assertEqual(len(open_ids(store)), 1)
        channel.is_coordinator = lambda now=None: True
        self.assertIn("Closed 1", channel._handle_command(self.admin("/owed close")))

    def test_only_the_admin_closes(self):
        channel, store = channel_with([handoff()], {})
        reply = channel._handle_command(event("/owed@HermesBot close", thread_id=307))
        self.assertIn("only the admin", reply)
        self.assertEqual(len(open_ids(store)), 1)

    def test_nothing_owed_here_says_so(self):
        channel, store = channel_with([handoff()], {})
        reply = channel._handle_command(self.admin("/owed@HermesBot close", thread_id=999))
        self.assertIn("Nothing", reply)
        self.assertEqual(len(open_ids(store)), 1)

    def test_plain_owed_still_only_reports(self):
        channel, store = channel_with([handoff()], {})
        reply = channel._handle_command(self.admin("/owed@HermesBot"))
        self.assertIn("Codex", reply)
        self.assertEqual(len(open_ids(store)), 1)


class TestTheFacts(unittest.TestCase):

    def test_finished_needs_a_time_and_the_place(self):
        row = handoff()
        self.assertIsNone(ow.closes(row, ow.Fact(kind="finished", item=TOPIC)))
        self.assertIsNone(ow.closes(row, ow.Fact(kind="finished", item="tg-1", at=NOW)))
        self.assertIsNotNone(ow.closes(row, ow.Fact(kind="finished", item=TOPIC, at=NOW)))

    def test_dismiss_answers_any_row_placed_there(self):
        admin_row = ow.Row(kind="admin", item="", debtor="admin", owes="an answer", topic=TOPIC,
                           key_extra=f"{TOPIC}#qabc")
        self.assertIsNotNone(ow.closes(handoff(), ow.Fact(kind="dismiss", item=TOPIC)))
        self.assertIsNotNone(ow.closes(admin_row, ow.Fact(kind="dismiss", item=TOPIC)))
        self.assertIsNone(ow.closes(handoff(), ow.Fact(kind="dismiss", item=TOPIC,
                                                       who="Reasonix")))


if __name__ == "__main__":
    unittest.main()
