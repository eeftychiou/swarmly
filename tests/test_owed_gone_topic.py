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


def not_ready():
    raise RuntimeError("the state store is not ready")


class GoneTopicWhileTheStoreIsNotReadyTests(unittest.TestCase):
    """#331: a deletion during a restart closes what it can and is retried until it can."""

    def channel(self, tmp, flags, rows):
        channel = make_channel()
        state = Path(tmp) / "state"
        channel.owed_store = lambda: OwedStore(
            "Hermes", state_dir=state,
            shared=lambda: list(rows) if flags["ready"] else not_ready())
        return channel

    def states(self, channel):
        return {row.topic: row.state for row in channel.owed_store().load()}

    def test_the_next_beat_closes_a_peer_handoff_the_deletion_could_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            flags, peer = {"ready": False}, handoff("tg-77", debtor="Codex", writer="Codex")
            channel = self.channel(tmp, flags, [peer])
            # The deletion lands while the store cannot be read: nothing is closed (the row is
            # not in this bot's own directory at all) and nothing is called done either.
            self.assertEqual(channel.close_owed_for_gone_topic(GROUP, 77, "tg-77"), 0)
            self.assertEqual(channel._pending_gone_closes(), {"tg-77": 77})
            self.assertEqual(channel.retry_gone_topic_closes(), [])   # still not ready
            self.assertEqual(channel._pending_gone_closes(), {"tg-77": 77})
            # The store comes back: the next beat closes the row and forgets the topic.
            flags["ready"] = True
            said = channel.retry_gone_topic_closes()
            self.assertEqual(len(said), 1, said)
            self.assertIn("77", said[0])
            self.assertEqual(channel._pending_gone_closes(), {})
            self.assertEqual(self.states(channel)["tg-77"], "closed")
            self.assertEqual(channel.retry_gone_topic_closes(), [])   # nothing left to retry

    def test_a_close_that_read_the_store_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            flags = {"ready": True}
            channel = self.channel(tmp, flags, [handoff("tg-77")])
            self.assertEqual(channel.close_owed_for_gone_topic(GROUP, 77, "tg-77"), 1)
            self.assertEqual(channel._pending_gone_closes(), {})
            self.assertEqual(channel.retry_gone_topic_closes(), [])
            self.assertEqual(self.states(channel), {"tg-77": "closed"})

    def test_the_memory_survives_a_restart(self):
        """A restart between the deletion and a readable store still retries: the state file."""
        with tempfile.TemporaryDirectory() as tmp:
            flags, peer = {"ready": False}, handoff("tg-77", debtor="Codex", writer="Codex")

            def config():
                from tests.test_telegram_channel import make_config
                made = make_config(state={"dir": tmp})
                made.source_path = Path(tmp) / "config.yaml"     # so its state is kept
                return made

            first = self.channel(tmp, flags, [peer])
            first.config = config()
            self.assertEqual(first.close_owed_for_gone_topic(GROUP, 77, "tg-77"), 0)
            self.assertEqual(first._pending_gone_closes(), {"tg-77": 77})
            kept = list(Path(tmp).rglob("gone-topic-closes-*.json"))   # on disk, not in memory
            self.assertEqual(len(kept), 1, kept)
            # A new process: nothing in memory, and the topic is still remembered.
            flags["ready"] = True
            second = self.channel(tmp, flags, [peer])
            second.config = config()
            self.assertEqual(second._pending_gone_closes(), {"tg-77": 77})
            self.assertEqual(len(second.retry_gone_topic_closes()), 1)
            self.assertEqual(self.states(second)["tg-77"], "closed")

    def test_the_heartbeat_is_what_retries_the_close(self):
        """The retry rides the beat, so nothing has to remember to call it."""
        from tests.test_telegram_channel import run
        with tempfile.TemporaryDirectory() as tmp:
            flags, peer = {"ready": False}, handoff("tg-77", debtor="Codex", writer="Codex")
            channel = self.channel(tmp, flags, [peer])
            self.assertEqual(channel.close_owed_for_gone_topic(GROUP, 77, "tg-77"), 0)
            flags["ready"] = True
            run(channel._heartbeat_tick())
            self.assertEqual(channel._pending_gone_closes(), {})
            self.assertEqual(self.states(channel)["tg-77"], "closed")


if __name__ == "__main__":
    unittest.main()
