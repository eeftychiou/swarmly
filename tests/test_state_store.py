"""The shared state store (topic registry, phase 1): records, keys, the fold and backends."""

import asyncio
import itertools
import json
import random
import tempfile
import unittest
from pathlib import Path

from swarmly.state import keys as K
from swarmly.state.log import Op, StateLog
from swarmly.state.records import (Assignment, Binding, Link, RecordError, Topic,
                                   AgentInTopic, Profile)
from swarmly.state.store import DirStore, MemoryLog, MemoryStore, StoreError
from swarmly.state.telegram_store import (Pinned, Posted, TelegramIO, TelegramStore,
                                          MAX_LINE_CHARS)


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def op(pos, key, base, writer, value, term=0, at=None):
    return Op(pos=pos, key=key, base=base, term=term, writer=writer, value=value,
              at=float(pos if at is None else at))


def lease(pos, writer, term, ttl=600, at=None, base=0):
    return op(pos, K.LEASE, base, writer, {"holder": writer, "term": term, "ttl": ttl},
              term=term, at=at)


# -- records ----------------------------------------------------------------------------------
class RecordTests(unittest.TestCase):
    def topic(self):
        return Topic(
            topic_id=10134, chat_id=-1001234567890, title="Stability round", status="open",
            opened_by="admin", opened_at="2026-10-23T23:12:41Z",
            links=[Link(id="L1", provider="github", kind="issue", ref="https://x/issues/237",
                        role="tracking", added_by="admin", added_at="2026-10-23T23:12:41Z"),
                   Link(id="L2", provider="github", kind="pr", ref="https://x/pull/256",
                        role="work", status="open", phase="review", added_by="DeepSeek",
                        added_at="2026-10-24T13:49:00Z",
                        assignees=[Assignment(agent="DeepSeek", role="developer",
                                              assigned_by="Claudebot",
                                              assigned_at="2026-10-24T10:02:00Z")])],
            agents={"Claudebot": Binding(role="coordinator", runtime="claude",
                                         model="claude-opus-5-5", effort="high",
                                         linked_by="transport",
                                         linked_at="2026-10-23T23:12:41Z")})

    def test_a_topic_round_trips_through_json(self):
        topic = self.topic()
        data = json.loads(json.dumps(topic.to_dict()))
        self.assertEqual(data["links"][1]["assignees"][0]["as"], "developer")
        again = Topic.from_dict(data)
        self.assertEqual(again.to_dict(), topic.to_dict())
        self.assertEqual(again.next_link_id(), "L3")

    def test_unknown_fields_are_ignored_and_bad_enums_refused(self):
        data = self.topic().to_dict()
        data["future_field"] = 1
        Topic.from_dict(data)
        data["links"][1]["phase"] = "dreaming"
        with self.assertRaises(RecordError):
            Topic.from_dict(data)

    def test_only_a_work_item_carries_status_and_assignees(self):
        with self.assertRaises(RecordError):
            Link.from_dict({"id": "L1", "provider": "topic", "kind": "task", "ref": "T-1-1",
                            "role": "reference", "added_by": "a", "added_at": "t",
                            "status": "open"})

    def test_a_work_item_needs_status_and_phase(self):
        with self.assertRaises(RecordError):
            Link.from_dict({"id": "L1", "provider": "topic", "kind": "task", "ref": "T-1-1",
                            "role": "work", "added_by": "a", "added_at": "t"})

    def test_a_missing_required_field_is_refused(self):
        with self.assertRaises(RecordError):
            Binding.from_dict({"role": "developer"})

    def test_small_records(self):
        session = AgentInTopic(session_id="s1", turns=3, last_activity={"L2": "t"})
        self.assertEqual(AgentInTopic.from_dict(session.to_dict()), session)
        profile = Profile(alias="DeepSeek", handle="@deepseek_dd_bot", runtime="reasonix",
                          default_model="deepseek-flash", default_effort="low", max_turns=40)
        self.assertEqual(Profile.from_dict(profile.to_dict()), profile)


# -- keys -------------------------------------------------------------------------------------
class KeyTests(unittest.TestCase):
    def test_owners(self):
        self.assertEqual(K.owner("lease"), K.ANYONE)
        self.assertEqual(K.owner("topic/10134"), K.HOLDER)
        self.assertEqual(K.owner("owed"), K.HOLDER)
        self.assertEqual(K.owner("topic/10134/agent/DeepSeek"), "deepseek")
        self.assertEqual(K.owner("agent/deepseek/stats"), "deepseek")
        self.assertFalse(K.valid("general/anything"))

    def test_may_write(self):
        self.assertTrue(K.may_write("topic/1", "Claudebot", "claudebot"))
        self.assertFalse(K.may_write("topic/1", "DeepSeek", "claudebot"))
        self.assertFalse(K.may_write("topic/1", "Claudebot", None))
        self.assertTrue(K.may_write(K.topic_agent(1, "DeepSeek"), "deepseek", None))
        self.assertFalse(K.may_write(K.topic_agent(1, "DeepSeek"), "claudebot", "claudebot"))


# -- the fold ---------------------------------------------------------------------------------
class FoldTests(unittest.TestCase):
    def test_the_lower_position_wins_a_compare_and_set(self):
        log = StateLog()
        log.add(op(1, "agent/a/x", 0, "a", {"n": 1}))
        log.add(op(3, "agent/a/x", 1, "a", {"n": 3}))
        log.add(op(2, "agent/a/x", 1, "a", {"n": 2}))     # arrives late, but came first
        self.assertEqual(log.value("agent/a/x"), {"n": 2})
        self.assertIn("lost the compare-and-set", log.rejected[3])

    def test_any_delivery_order_folds_to_the_same_state(self):
        ops = [lease(1, "claudebot", 1, ttl=1000),
               op(2, "topic/7", 0, "claudebot", {"t": 1}, term=1),
               op(3, "topic/7", 0, "claudebot", {"t": "lost"}, term=1),
               op(4, "agent/deepseek/stats", 0, "deepseek", {"s": 1}),
               op(5, "agent/deepseek/stats", 0, "claudebot", {"s": "forged"}),
               op(6, "topic/7", 1, "deepseek", {"t": "not holder"}, term=1),
               op(7, "topic/7", 1, "claudebot", {"t": 2}, term=1)]
        hashes = set()
        rng = random.Random(4)
        for _ in range(40):
            order = ops[:]
            rng.shuffle(order)
            log = StateLog()
            for o in order:
                log.add(o)
            hashes.add(log.state_hash())
        self.assertEqual(len(hashes), 1)
        log = StateLog()
        log.add_all(ops)
        self.assertEqual(log.value("topic/7"), {"t": 2})
        self.assertEqual(log.value("agent/deepseek/stats"), {"s": 1})

    def test_a_write_against_an_unseen_version_is_a_gap(self):
        log = StateLog()
        log.add(op(5, "agent/a/x", 2, "a", {"n": 5}))
        self.assertIn("agent/a/x", log.gaps)
        self.assertIsNone(log.value("agent/a/x"))

    def test_a_bot_cannot_write_in_another_bots_name(self):
        wire = {"k": "agent/deepseek/stats", "b": 0, "t": 0, "w": "deepseek", "v": {"x": 1}}
        forged = Op.from_wire(wire, pos=1, at=1, writer="Claudebot")   # Telegram says Claude
        log = StateLog()
        log.add(forged)
        self.assertIsNone(log.value("agent/deepseek/stats"))

    def test_duplicates_are_ignored(self):
        log = StateLog()
        self.assertTrue(log.add(op(1, "agent/a/x", 0, "a", {"n": 1})))
        self.assertTrue(log.add(op(1, "agent/a/x", 0, "a", {"n": 1})))
        self.assertEqual(log.version("agent/a/x"), 1)

    def test_snapshot_then_later_ops(self):
        log = StateLog()
        log.add_all([op(1, "agent/a/x", 0, "a", {"n": 1}), op(2, "agent/a/x", 1, "a", {"n": 2})])
        partial = StateLog.from_snapshot(log.snapshot(upto=1))
        self.assertEqual(partial.value("agent/a/x"), {"n": 1})
        partial.add(op(2, "agent/a/x", 1, "a", {"n": 2}))
        self.assertEqual(partial.state_hash(), log.state_hash())
        reloaded = StateLog.load(json.loads(json.dumps(log.dump())))
        self.assertEqual(reloaded.state_hash(), log.state_hash())


class SnapshotTests(unittest.TestCase):
    def truth(self):
        log = StateLog()
        log.add_all([lease(1, "claudebot", 1, ttl=600, at=0),
                     op(2, "topic/7", 0, "claudebot", {"n": 1}, term=1, at=10),
                     lease(3, "deepseek", 2, ttl=600, at=700, base=1),
                     op(4, "topic/7", 1, "deepseek", {"n": 2}, term=2, at=710)])
        return log

    def test_a_snapshot_from_an_older_term_is_refused(self):
        """Review, #263: a deposed holder that missed its successor's claim posts a snapshot
        later in the log; it must not roll every reader back to term 1."""
        reader = self.truth()
        stale = StateLog()
        stale.add_all([lease(1, "claudebot", 1, ttl=10 ** 6, at=0),
                       op(2, "topic/7", 0, "claudebot", {"n": 1}, term=1, at=10)])
        snapshot = stale.snapshot()
        snapshot["last_pos"] = 40
        self.assertEqual(snapshot["term"], 1)
        self.assertFalse(reader.rebase(snapshot))
        self.assertEqual(reader.lease().holder, "deepseek")
        self.assertEqual(reader.value("topic/7"), {"n": 2})

    def test_a_snapshot_never_overwrites_a_newer_entry(self):
        reader = self.truth()
        partial = StateLog()
        partial.add_all([lease(1, "claudebot", 1, ttl=600, at=0),
                         lease(3, "deepseek", 2, ttl=600, at=700, base=1)])
        snapshot = partial.snapshot()
        snapshot["last_pos"] = 50                    # it never saw op 4
        self.assertTrue(reader.rebase(snapshot))
        self.assertEqual(reader.value("topic/7"), {"n": 2})

    def test_compact_only_up_to_what_was_published(self):
        log = self.truth()
        before = log.state_hash()
        log.compact(2)
        self.assertEqual(log.base_pos, 2)
        self.assertEqual([o.pos for o in log.ops], [3, 4])
        self.assertEqual(log.state_hash(), before)
        log.add(op(5, "topic/7", 2, "deepseek", {"n": 3}, term=2, at=720))
        self.assertEqual(log.value("topic/7"), {"n": 3})


class ClassifierTests(unittest.TestCase):
    def test_store_and_probe_lines_are_machine_traffic_for_every_caller(self):
        from swarmly.telegram.poll import MachineEventClassifier
        from swarmly.telegram.wire import Envelope
        for intent in ("STATE", "SNAPSHOT", "PROBE", "CONTROL"):
            self.assertEqual(MachineEventClassifier().classify(
                Envelope(type="NOTE", intent=intent, ref="x")), "note")


class LeaseTests(unittest.TestCase):
    def test_one_winner_per_term(self):
        log = StateLog()
        log.add_all([lease(1, "claudebot", 1), lease(2, "deepseek", 1)])
        self.assertEqual(log.lease().holder, "claudebot")
        self.assertIn(2, log.rejected)
        log.add(lease(3, "deepseek", 2, base=1, at=3))              # even on the new version
        self.assertIn("still held", log.rejected[3])

    def test_a_takeover_waits_for_expiry(self):
        log = StateLog()
        log.add(lease(1, "claudebot", 1, ttl=600, at=0))
        log.add(lease(2, "deepseek", 2, at=300, base=1))
        self.assertEqual(log.lease().holder, "claudebot")
        log.add(lease(3, "deepseek", 2, at=601, base=1))
        self.assertEqual(log.lease().holder, "deepseek")
        self.assertEqual(log.lease().term, 2)

    def test_renew_and_release(self):
        log = StateLog()
        log.add(lease(1, "claudebot", 1, ttl=600, at=0))
        renew = op(2, K.LEASE, 1, "claudebot", {"holder": "claudebot", "term": 1, "ttl": 600},
                   term=1, at=500)
        log.add(renew)
        self.assertEqual(log.lease().expires_at, 1100)
        stolen = op(3, K.LEASE, 2, "deepseek", {"holder": "deepseek", "term": 1, "ttl": 600},
                    term=1, at=600)
        log.add(stolen)
        self.assertIn("not this bot's", log.rejected[3])
        release = op(4, K.LEASE, 2, "claudebot", {"holder": None, "term": 1}, term=1, at=700)
        log.add(release)
        self.assertIsNone(log.lease().held_by(701))
        log.add(op(5, K.LEASE, 3, "deepseek", {"holder": "deepseek", "term": 2, "ttl": 600},
                   term=2, at=702))
        self.assertEqual(log.lease().held_by(703), "deepseek")

    def test_a_deposed_coordinators_late_write_is_dropped(self):
        """Split brain: Claudebot was cut off, DeepSeek took term 2, Claudebot's queued write
        (stamped term 1) lands afterwards."""
        log = StateLog()
        log.add_all([lease(1, "claudebot", 1, ttl=600, at=0),
                     op(2, "topic/7", 0, "claudebot", {"by": "claude"}, term=1, at=10),
                     lease(3, "deepseek", 2, ttl=600, at=700, base=1),
                     op(4, "topic/7", 1, "deepseek", {"by": "deepseek"}, term=2, at=710),
                     op(5, "topic/7", 2, "claudebot", {"by": "claude late"}, term=1, at=720)])
        self.assertEqual(log.value("topic/7"), {"by": "deepseek"})
        self.assertIn("not the lease holder", log.rejected[5])

    def test_an_expired_holder_cannot_write(self):
        log = StateLog()
        log.add_all([lease(1, "claudebot", 1, ttl=60, at=0),
                     op(2, "topic/7", 0, "claudebot", {"x": 1}, term=1, at=100)])
        self.assertIn("not the lease holder", log.rejected[2])


# -- the memory and folder backends -----------------------------------------------------------
class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.shared = MemoryLog(clock=self.clock)

    def store(self, alias, **kwargs):
        return MemoryStore(alias, self.shared, clock=self.clock, **kwargs)

    def test_two_bots_see_each_others_writes(self):
        async def go():
            a, b = self.store("Claudebot"), self.store("DeepSeek")
            await a.start()
            await b.start()
            result = await b.write(K.agent("DeepSeek", "profile"), {"model": "flash"})
            self.assertTrue(result.applied)
            await a.refresh()
            self.assertEqual(a.read("agent/deepseek/profile"), {"model": "flash"})
            self.assertEqual(a.state_hash(), b.state_hash())
        run(go())

    def test_a_bot_may_not_write_what_it_does_not_own(self):
        async def go():
            a = self.store("DeepSeek")
            await a.start()
            refused = await a.write("topic/7", {"x": 1})
            self.assertFalse(refused.applied)
            self.assertEqual(self.shared.ops, [])           # nothing was even sent
        run(go())

    def test_the_lease_holder_writes_topics_and_a_racing_claim_loses(self):
        async def go():
            a, b = self.store("Claudebot"), self.store("DeepSeek")
            await a.start()
            await b.start()
            first = await a.claim_lease(600)
            second = await b.claim_lease(600)              # b had not seen a's claim
            await a.refresh()
            await b.refresh()
            self.assertTrue(first.applied)
            self.assertFalse(second.applied)
            self.assertTrue(a.holds_lease())
            self.assertFalse(b.holds_lease())
            self.assertTrue((await a.write("topic/7", {"title": "x"})).applied)
        run(go())

    def test_an_unreachable_log_fails_closed(self):
        async def go():
            a = self.store("Claudebot")
            await a.start()
            self.shared.down = True
            with self.assertRaises(StoreError):
                await a.write(K.agent("Claudebot", "profile"), {"x": 1})
            with self.assertRaises(StoreError):
                await a.refresh()
        run(go())

    def test_a_bot_back_after_eight_days_reads_the_snapshot(self):
        async def go():
            a = self.store("Claudebot")
            await a.start()
            await a.claim_lease(10 ** 9)
            for n in range(5):
                await a.write("topic/7", {"n": n})
            await a.publish_snapshot()
            # Telegram keeps a day of updates: the lines themselves are gone for a late bot.
            self.shared.ops = []
            self.clock.t += 8 * 86400
            late = self.store("DeepSeek")
            await late.start()
            self.assertEqual(late.read("topic/7"), {"n": 4})
            self.assertEqual(late.state_hash(), a.state_hash())
        run(go())

    def test_the_local_cache_survives_a_restart(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp) / "state-deepseek.json"
                a = self.store("DeepSeek", cache=cache)
                await a.start()
                await a.write(K.agent("DeepSeek", "profile"), {"m": 1})
                self.shared.down = True                     # the log is unreachable now
                again = self.store("DeepSeek", cache=cache)
                again.log = again.log.load(json.loads(cache.read_text()))
                self.assertEqual(again.read("agent/deepseek/profile"), {"m": 1})
        run(go())


class DirStoreTests(unittest.TestCase):
    def test_positions_are_unique_and_shared(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                a, b = DirStore("Claudebot", Path(tmp)), DirStore("DeepSeek", Path(tmp))
                await a.start()
                await b.start()
                await asyncio.gather(*[a.write(K.agent("claudebot", f"x{i}"), {"i": i})
                                       for i in range(5)],
                                     *[b.write(K.agent("deepseek", f"x{i}"), {"i": i})
                                       for i in range(5)])
                await a.refresh()
                await b.refresh()
                self.assertEqual(len(a.keys()), 10)
                self.assertEqual(a.state_hash(), b.state_hash())
                await a.claim_lease(10 ** 9)
                self.assertTrue(await a.publish_snapshot())
                c = DirStore("Reasonix", Path(tmp))
                await c.start()
                self.assertEqual(c.state_hash(), a.state_hash())
        run(go())


# -- the Telegram backend ---------------------------------------------------------------------
class FakeGroup:
    """Swarm pulse: global message ids, delivery to every other store, the newest pin."""

    def __init__(self, clock):
        self.clock = clock
        self.ids = itertools.count(5000)
        self.stores = {}
        self.files = {}
        self.pins = []
        self.down = False
        self.held = []                      # (store alias, kwargs) delivered later

    def deliver(self, author, mid, text, data=None, hold_for=None):
        for alias, store in self.stores.items():
            if alias == author:
                continue
            kwargs = dict(message_id=mid, date=self.clock(), author=author, text=text)
            if data is not None:
                async def read(d=data):
                    return d
                kwargs["read_file"] = read
            if alias in (hold_for or ()):
                self.held.append((alias, kwargs))
            else:
                store.ingest(**kwargs)

    def release(self):
        for alias, kwargs in self.held:
            self.stores[alias].ingest(**kwargs)
        self.held = []


class FakeIO(TelegramIO):
    def __init__(self, group, alias):
        self.group, self.alias = group, alias
        self.hold_for = None

    async def post_line(self, text):
        if self.group.down:
            raise RuntimeError("Telegram unreachable")
        mid = next(self.group.ids)
        self.group.deliver(self.alias, mid, text, hold_for=self.hold_for)
        return Posted(mid, self.group.clock())

    async def post_file(self, name, data, caption):
        if self.group.down:
            raise RuntimeError("Telegram unreachable")
        mid = next(self.group.ids)
        self.group.files[mid] = (caption, data)
        if "intent=STATE" in caption:
            self.group.deliver(self.alias, mid, caption, data=data)
        return Posted(mid, self.group.clock())

    async def pin(self, message_id):
        self.group.pins.append(message_id)

    async def unpin(self, message_id):
        self.group.pins.remove(message_id)

    async def pinned(self):
        if self.group.down:
            raise RuntimeError("Telegram unreachable")
        if not self.group.pins:
            return None
        mid = max(self.group.pins)
        caption, data = self.group.files.get(mid, ("dashboard", b""))

        async def read():
            return data
        return Pinned(mid, caption, read)


class TelegramStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.group = FakeGroup(self.clock)

    def store(self, alias):
        store = TelegramStore(alias, FakeIO(self.group, alias), clock=self.clock)
        store.CONFIRM_SECONDS = 0.0
        self.group.stores[alias] = store
        return store

    def test_lines_fold_the_same_for_everyone(self):
        async def go():
            a, b, c = self.store("Claudebot"), self.store("DeepSeek"), self.store("Codex")
            for s in (a, b, c):
                await s.start()
            await a.claim_lease(10 ** 6)
            await b.refresh()
            await b.write(K.agent("DeepSeek", "profile"), {"m": "flash"})
            await a.write("topic/7", {"title": "round"})
            for s in (a, b, c):
                await s.refresh()
            self.assertEqual(len({s.state_hash() for s in (a, b, c)}), 1)
            self.assertEqual(c.read("topic/7"), {"title": "round"})
            self.assertEqual(c.lease().holder, "claudebot")
        run(go())

    def test_a_late_line_with_a_lower_id_wins_the_race(self):
        async def go():
            a, b, c = self.store("Claudebot"), self.store("DeepSeek"), self.store("Codex")
            for s in (a, b, c):
                await s.start()
            a.io.hold_for = {"Codex", "DeepSeek"}   # a's claim reaches them late
            first = await a.claim_lease(600)
            second = await b.claim_lease(600)
            await c.refresh()
            self.assertEqual(c.lease().holder, "deepseek")     # for now
            self.group.release()
            await c.refresh()
            await a.refresh()
            await b.refresh()
            self.assertTrue(first.applied)
            self.assertEqual(c.lease().holder, "claudebot")    # the lower id won after all
            self.assertEqual(len({s.state_hash() for s in (a, b, c)}), 1)
            self.assertFalse(b.holds_lease())
            self.assertIn(second.pos, b.log.rejected)    # b's claim lost, once it knew
        run(go())

    def test_a_big_value_goes_as_a_file(self):
        async def go():
            a, b = self.store("Claudebot"), self.store("DeepSeek")
            await a.start()
            await b.start()
            await a.claim_lease(10 ** 6)
            big = {"title": "x" * (MAX_LINE_CHARS + 100)}
            self.assertTrue((await a.write("topic/7", big)).applied)
            await b.refresh()
            self.assertEqual(b.read("topic/7"), big)
        run(go())

    def test_the_snapshot_is_the_newest_pin_and_a_rejoining_bot_reads_it(self):
        async def go():
            self.group.pins.append(1)                         # the dashboard, pinned long ago
            a = self.store("Claudebot")
            await a.start()
            await a.claim_lease(10 ** 6)
            await a.write("topic/7", {"n": 1})
            self.clock.t += 60
            self.assertTrue(await a.publish_snapshot())
            first = a.snapshot_message
            await a.write("topic/7", {"n": 2})
            self.clock.t += 60
            self.assertTrue(await a.publish_snapshot())
            self.assertNotIn(first, self.group.pins, "the previous snapshot is unpinned")
            self.assertIn(1, self.group.pins, "the dashboard stays pinned")
            late = self.store("DeepSeek")                     # never saw a line
            await late.start()
            self.assertEqual(late.read("topic/7"), {"n": 2})
            self.assertEqual(late.state_hash(), a.state_hash())
        run(go())

    def test_a_snapshot_covers_only_settled_lines(self):
        async def go():
            a = self.store("Claudebot")
            await a.start()
            await a.claim_lease(10 ** 6)
            await a.write("topic/7", {"n": 1})                # just written: not settled
            self.assertFalse(await a.publish_snapshot())
        run(go())

    def test_a_gap_reads_the_pin_again(self):
        async def go():
            a = self.store("Claudebot")
            await a.start()
            await a.claim_lease(10 ** 6)
            await a.write("topic/7", {"n": 1})
            self.clock.t += 60
            await a.publish_snapshot()
            b = self.store("DeepSeek")
            b.log = StateLog()                                # started before the snapshot
            b.ready = True
            await a.write("topic/7", {"n": 2})                # b only sees this line: a gap
            await b.refresh()
            self.assertEqual(b.read("topic/7"), {"n": 2})
        run(go())

    def test_telegram_down_fails_closed(self):
        async def go():
            a = self.store("Claudebot")
            await a.start()
            self.group.down = True
            with self.assertRaises(StoreError):
                await a.write(K.agent("Claudebot", "profile"), {"x": 1})
            b = self.store("DeepSeek")
            with self.assertRaises(StoreError):
                await b.start()
            self.assertFalse(b.ready)
        run(go())

    def test_a_lost_pin_is_restored_by_the_lease_holder(self):
        async def go():
            a = self.store("Claudebot")
            await a.start()
            await a.claim_lease(10 ** 6)
            await a.write("topic/7", {"n": 1})
            self.clock.t += 60
            self.assertTrue(await a.restore_from_cache())
            late = self.store("DeepSeek")
            await late.start()
            self.assertEqual(late.read("topic/7"), {"n": 1})
        run(go())

    def test_writes_made_together_go_in_one_message(self):
        async def go():
            a, b = self.store("Claudebot"), self.store("DeepSeek")
            await a.start()
            await b.start()
            await a.claim_lease(10 ** 6)
            await b.refresh()
            first = next(self.group.ids)
            items = [(K.agent("Claudebot", f"x{i}"), {"i": i}) for i in range(10)]
            items.append(("agent/deepseek/profile", {"forged": True}))
            results = await a.write_many(items)
            used = next(self.group.ids) - first - 1
            self.assertEqual(used, 1, "ten writes, one message")
            self.assertTrue(all(r.applied for r in results[:10]))
            self.assertFalse(results[10].applied)
            await b.refresh()
            self.assertEqual(b.read("agent/claudebot/x9"), {"i": 9})
            self.assertEqual(a.state_hash(), b.state_hash())
            big = [(K.agent("Claudebot", f"y{i}"), {"pad": "z" * 900}) for i in range(8)]
            first = next(self.group.ids)
            self.assertTrue(all(r.applied for r in await a.write_many(big)))
            self.assertGreater(next(self.group.ids) - first - 1, 1, "split under the limit")
            await b.refresh()
            self.assertEqual(a.state_hash(), b.state_hash())
        run(go())

    def test_other_traffic_is_not_the_stores(self):
        a = self.store("Claudebot")
        self.assertFalse(a.ingest(message_id=1, date=1, author="deepseek",
                                  text="[A2A v1.1] STATUS to=* intent=PULSE ref=x\n\n{}"))
        self.assertFalse(a.ingest(message_id=2, date=1, author="deepseek", text="hello"))


if __name__ == "__main__":
    unittest.main()
