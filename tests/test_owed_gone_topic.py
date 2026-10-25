"""A deleted topic answers its hand-offs; own bus closures still count on the store (25 Oct)."""

import json
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.owed import OwedStore, Row
from tests.test_telegram_channel import GROUP, make_channel


def handoff(topic, debtor="Codex", writer="Hermes"):
    return Row(kind="handoff", item="-", debtor=debtor, owes="a reply", writer=writer,
               topic=topic, key_extra=topic, created_at=1000.0)


class GoneTopicTests(unittest.TestCase):
    def channel(self, tmp):
        channel = make_channel()
        state = Path(tmp) / "state"
        channel.owed_store = lambda: OwedStore("Hermes", state_dir=state)
        return channel

    def test_its_open_handoffs_close_and_others_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            channel.owed_store().save_all([handoff("tg-77"), handoff("tg-78")])
            self.assertEqual(channel.close_owed_for_gone_topic(GROUP, 77, "tg-77"), 1)
            states = {r.topic: r.state for r in channel.owed_store().load()}
            self.assertEqual(states, {"tg-77": "closed", "tg-78": "open"})

    def test_forgetting_a_deleted_topic_closes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            channel.topics.remember(GROUP, 77, "old round")
            token = channel.topics.context(GROUP, 77)
            channel.owed_store().save_all([handoff(token)])
            channel.forget_topic_state(GROUP, 77)
            self.assertEqual([r.state for r in channel.owed_store().load()], ["closed"])


class OwnBusClosuresTests(unittest.TestCase):
    def test_a_closure_only_in_the_bots_own_bus_document_still_counts(self):
        """The state directory wins only for the ids it holds; for the rest, the bot's own
        bus document is the record (a closure written there must not be lost)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bus"
            own = root / ".agents" / "owed" / "hermes"
            peer = root / ".agents" / "owed" / "codex"
            own.mkdir(parents=True)
            peer.mkdir(parents=True)
            stale = handoff("tg-5", writer="Codex")
            closed = handoff("tg-5", writer="Codex")
            closed.state, closed.updated_at = "closed", 2000.0
            (peer / "a.json").write_text(json.dumps(stale.to_dict()))
            (own / "a.json").write_text(json.dumps(closed.to_dict()))
            store = OwedStore("Hermes", state_dir=Path(tmp) / "state", root=root,
                              shared=lambda: [stale], published=lambda: [])
            self.assertEqual([r.state for r in store.load()], ["closed"])

    def test_clearing_a_missing_marker_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OwedStore("Hermes", state_dir=Path(tmp))
            with self.assertNoLogs("swarmly.telegram.owed", "WARNING"):
                store._mark_undelivered([])


if __name__ == "__main__":
    unittest.main()
