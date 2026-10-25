"""The owed table on the state store (topic registry, phase 3, ``state.owed: store``)."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from swarmly.telegram.config import AgentConfig, ConfigError
from swarmly.telegram.owed import OwedStore, Row
from tests.test_state_link import FakeGroup, link_for
from tests.test_state_store import Clock
from tests.test_telegram_channel import make_channel, make_config


def row(item, debtor, writer, state="open"):
    out = Row(kind="handoff", item=item, debtor=debtor, owes="a review", writer=writer,
              key_extra="tg-7", created_at=1000.0)       # old: before any test clock
    out.state = state
    return out


class SharedRowsTests(unittest.TestCase):
    def test_the_store_rows_fold_with_the_own_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OwedStore("Claudebot", state_dir=Path(tmp), transport=None, root=None,
                              shared=lambda: [row("#13", "claudebot", "deepseek")])
            store.save(row("#12", "deepseek", "claudebot"))
            self.assertEqual(sorted(r.item for r in store.load()), ["#12", "#13"])

    def test_an_unreadable_store_is_never_nothing_owed(self):
        def broken():
            raise RuntimeError("the state store is not ready")
        with tempfile.TemporaryDirectory() as tmp:
            store = OwedStore("Claudebot", state_dir=Path(tmp), shared=broken)
            self.assertIn("cannot read", store.summary())


class FoldTests(unittest.TestCase):
    """#298 review: a debt owed again after its close must not hide behind that close."""

    def rows(self):
        closed = row("#12", "deepseek", "deepseek", state="closed")
        closed.created_at, closed.updated_at = 100.0, 200.0
        return closed

    def test_an_open_row_created_after_the_close_wins(self):
        again = row("#12", "deepseek", "claudebot")
        again.created_at = again.updated_at = 300.0
        from swarmly.telegram.owed import fold
        self.assertEqual([r.state for r in fold([self.rows(), again])], ["open"])
        self.assertEqual([r.state for r in fold([again, self.rows()])], ["open"])

    def test_a_stale_open_copy_still_loses_even_refreshed(self):
        stale = row("#12", "deepseek", "claudebot")
        stale.created_at, stale.updated_at = 50.0, 900.0          # a sweep touched it later
        from swarmly.telegram.owed import fold
        self.assertEqual([r.state for r in fold([stale, self.rows()])], ["closed"])

    def test_a_stored_row_without_a_creation_time_is_old(self):
        data = row("#12", "deepseek", "claudebot").to_dict()
        data.pop("created_at")
        data["updated_at"] = 150.0
        self.assertEqual(Row.from_dict(data).created_at, 150.0)


class StoreModeLinkTests(unittest.TestCase):
    def links(self):
        clock = Clock()
        group = FakeGroup(clock)
        claude = link_for(group, "Claudebot", clock, coordinator=True)
        deepseek = link_for(group, "DeepSeek", clock)
        for link in (claude, deepseek):
            link.owed_mode = "store"
        return clock, claude, deepseek

    def holding(self, link, tmp, name):
        """Give a link a real owed store in its own state directory."""
        state = Path(tmp) / name
        link.channel.owed_store = lambda: OwedStore(link.store.alias, state_dir=state,
                                                    root=Path(tmp) / f"bus-{name}",
                                                    clock=link.clock,
                                                    shared=link.shared_owed_rows,
                                                    published=link.published_owed_rows)
        return link.channel.owed_store

    def test_a_re_opened_id_is_seen_again(self):
        """Review: the closed copy in the bot's own key (or bus document) hid a new hand-off
        with the same id -- the second hand-off to a peer in one topic."""
        import json
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            claude_store = self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")
            old = Path(tmp) / "bus-c" / ".agents" / "owed" / "claudebot"
            old.mkdir(parents=True)                   # closed on gitbus before the switch
            (old / "x.json").write_text(json.dumps(
                row("#12", "deepseek", "claudebot", state="closed").to_dict()))

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                claude_store().save(row("#12", "deepseek", "claudebot", state="closed"))
                await claude.sync_owed(claude.store, clock())
                clock.t += 60
                claude_store().save(row("#12", "deepseek", "claudebot"))    # handed again
                here = [r.state for r in claude_store().load() if r.item == "#12"]
                await claude.sync_owed(claude.store, clock())
                await deepseek.store.refresh()
                there = [r.state for r in deepseek_store().load() if r.item == "#12"]
                return here, there
            here, there = asyncio.run(go())
        self.assertEqual((here, there), (["open"], ["open"]))

    def test_a_re_opened_id_wins_over_a_peers_old_close(self):
        """#298 review repro: the coordinator closes, the asker hands off again."""
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            claude_store = self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                first = row("#12", "deepseek", "claudebot")
                first.created_at = first.updated_at = clock()
                claude_store().save(first)
                await claude.sync_owed(claude.store, clock())
                await deepseek.store.refresh()
                clock.t += 60
                deepseek_store().save(row("#12", "deepseek", "claudebot", state="closed"))
                await deepseek.sync_owed(deepseek.store, clock())
                await claude.store.refresh()
                clock.t += 60
                again = row("#12", "deepseek", "claudebot")
                again.created_at = again.updated_at = clock()
                claude_store().save(again)
                await claude.sync_owed(claude.store, clock())
                await deepseek.store.refresh()
                mine = [r.state for r in claude_store().load() if r.item == "#12"]
                theirs = [r.state for r in deepseek_store().load() if r.item == "#12"]
                return mine, theirs
            mine, theirs = asyncio.run(go())
        self.assertEqual((mine, theirs), (["open"], ["open"]))

    def test_a_malformed_own_key_is_replaced(self):
        clock, claude, _ = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.holding(claude, tmp, "c")

            async def go():
                await claude.store.start()
                await claude.store.write("agent/claudebot/owed", ["not", "a", "table"])
                store().save(row("#12", "deepseek", "claudebot"))
                self.assertTrue(await claude.sync_owed(claude.store, clock()))
                return claude.store.read("agent/claudebot/owed")
            value = asyncio.run(go())
        self.assertEqual([r["item"] for r in value["rows"]], ["#12"])

    def test_one_malformed_key_does_not_hide_the_table(self):
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            claude_store = self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                await deepseek.store.write("agent/deepseek/owed", ["not", "a", "table"])
                claude_store().save(row("#12", "deepseek", "claudebot"))
                self.assertTrue(await claude.sync_owed(claude.store, clock()))
                await deepseek.store.refresh()
                return deepseek_store().load(), deepseek_store().unreadable
            rows, unreadable = asyncio.run(go())
        self.assertEqual(([r.item for r in rows], unreadable), (["#12"], []))

    def test_an_off_loop_reader_gets_its_own_rows(self):
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                deepseek_store().save(row("#13", "claudebot", "deepseek"))
                await deepseek.sync_owed(deepseek.store, clock())
                await claude.store.refresh()
                await claude.sync_owed(claude.store, clock())
                first = await asyncio.to_thread(claude.shared_owed_rows)
                first[0].state = "closed"             # a reader settling its copy
                return await asyncio.to_thread(claude.shared_owed_rows)
            again = asyncio.run(go())
        self.assertEqual([r.state for r in again], ["open"])

    def test_a_close_on_a_peers_row_reaches_every_reader(self):
        """Review: only rows the bot wrote were published, so its closes never left it."""
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            claude_store = self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                deepseek_store().save(row("#13", "deepseek", "deepseek"))
                await deepseek.sync_owed(deepseek.store, clock())
                await claude.store.refresh()
                claude_store().save(row("#13", "deepseek", "deepseek", state="closed"))
                await claude.sync_owed(claude.store, clock())
                await deepseek.store.refresh()
                return [r for r in deepseek_store().load() if r.item == "#13"]
            seen = asyncio.run(go())
        self.assertEqual([r.state for r in seen], ["closed"])

    def test_a_close_outlives_the_day_while_another_key_holds_the_row_open(self):
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            claude_store = self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                deepseek_store().save(row("#13", "deepseek", "deepseek"))
                await deepseek.sync_owed(deepseek.store, clock())
                await claude.store.refresh()
                claude_store().save(row("#13", "deepseek", "deepseek", state="closed"))
                await claude.sync_owed(claude.store, clock())
                clock.t += 3 * 86400
                await claude.sync_owed(claude.store, clock())
                return claude.store.read("agent/claudebot/owed")["rows"]
            kept = asyncio.run(go())
        self.assertEqual([(r["item"], r["state"]) for r in kept], [("#13", "closed")])

    def test_a_lost_state_directory_never_empties_the_table(self):
        clock, claude, _ = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.holding(claude, tmp, "c")

            async def go():
                await claude.store.start()
                store().save(row("#12", "deepseek", "claudebot"))
                await claude.sync_owed(claude.store, clock())
                for path in (Path(tmp) / "c" / "owed").glob("*.json"):
                    path.unlink()                     # a redeploy on a clean host
                await claude.sync_owed(claude.store, clock())
                return claude.store.read("agent/claudebot/owed")["rows"], store().load()
            published, seen = asyncio.run(go())
        self.assertEqual([r["item"] for r in published], ["#12"])
        self.assertEqual([r.item for r in seen], ["#12"])  # read back from its own key

    def test_a_reader_off_the_loop_gets_the_heartbeats_copy(self):
        clock, claude, deepseek = self.links()
        with tempfile.TemporaryDirectory() as tmp:
            self.holding(claude, tmp, "c")
            deepseek_store = self.holding(deepseek, tmp, "d")

            async def go():
                await claude.store.start()
                await deepseek.store.start()
                deepseek_store().save(row("#13", "claudebot", "deepseek"))
                await deepseek.sync_owed(deepseek.store, clock())
                await claude.store.refresh()
                await claude.sync_owed(claude.store, clock())
                return await asyncio.to_thread(claude.shared_owed_rows)
            rows = asyncio.run(go())
        self.assertEqual([r.item for r in rows], ["#13"])

    def test_off_the_loop_before_any_heartbeat_is_cannot_read(self):
        _clock, claude, _ = self.links()

        async def go():
            await claude.store.start()
            await asyncio.to_thread(claude.shared_owed_rows)
        with self.assertRaises(RuntimeError):
            asyncio.run(go())

    def test_not_ready_raises(self):
        _clock, claude, _ = self.links()
        with self.assertRaises(RuntimeError):
            claude.shared_owed_rows()

    def test_the_table_is_published_every_heartbeat(self):
        clock, claude, _ = self.links()
        calls = []

        async def sync(store, now):
            calls.append(now)
            return False
        claude.sync_owed = sync

        async def go():
            await claude.store.start()
            await claude.beat(now=clock())
            clock.t += 60                  # well inside the state cadence (10 minutes)
            await claude.beat(now=clock())
        asyncio.run(go())
        self.assertEqual(len(calls), 2)

    def test_in_shadow_the_table_waits_for_the_state_cadence(self):
        clock, claude, _ = self.links()
        claude.owed_mode = "shadow"
        calls = []

        async def sync(store, now):
            calls.append(now)
            return False
        claude.sync_owed = sync

        async def go():
            await claude.store.start()
            await claude.beat(now=clock())
            clock.t += 60
            await claude.beat(now=clock())
        asyncio.run(go())
        self.assertEqual(len(calls), 1)


class ChannelTests(unittest.TestCase):
    def test_store_mode_reads_no_gitbus(self):
        channel = make_channel(make_config(state={"backend": "telegram", "owed": "store"}))
        store = channel.owed_store()
        self.assertIsNone(store.transport)
        self.assertEqual(store.shared, channel.state_link.shared_owed_rows)

    def test_shadow_mode_keeps_gitbus(self):
        channel = make_channel(make_config(state={"backend": "telegram"}))
        self.assertIsNone(channel.owed_store().shared)


class ConfigTests(unittest.TestCase):
    def test_store_needs_the_telegram_backend(self):
        with self.assertRaises(ConfigError):
            make_config(state={"backend": "off", "owed": "store"})
        with self.assertRaises(ConfigError):
            make_config(state={"backend": "telegram", "owed": "gitbus"})
        self.assertEqual(make_config(state={"backend": "telegram", "owed": "store"})
                         .state.owed, "store")
        self.assertIsInstance(make_config(), AgentConfig)


if __name__ == "__main__":
    unittest.main()
