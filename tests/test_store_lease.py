"""The coordinator lease on the shared state store (topic registry, phase 3)."""

import asyncio
import unittest

from swarmly.state.telegram_store import TelegramStore
from swarmly.telegram.lease import LeaseError
from swarmly.telegram.store_lease import StoreLease
from tests.test_state_store import Clock, FakeGroup, FakeIO, OffsetClock


class StoreLeaseTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.server = Clock()
        self.group = FakeGroup(self.server)

    def lease_for(self, alias, offset=0.0, ttl=600.0):
        store = TelegramStore(alias, FakeIO(self.group, alias),
                              clock=OffsetClock(self.server, offset))
        store.CONFIRM_SECONDS = 0.0
        self.group.stores[alias] = store
        self.loop.run_until_complete(store.start())
        return StoreLease(store, self.loop, alias=alias, ttl_seconds=ttl)

    def test_bootstrap_renew_and_the_live_term_is_kept(self):
        claude, deepseek = self.lease_for("Claudebot"), self.lease_for("DeepSeek")
        self.assertEqual(claude.state().status, "absent")
        claude.claim(term=1)
        state = claude.state()
        self.assertEqual((state.status, state.holder, state.term), ("ok", "Claudebot", 1))
        self.assertTrue(state.holds("Claudebot", self.server()))
        self.server.t += 300
        claude.claim(term=1)                                 # a renewal
        self.assertGreater(claude.state().expires_at, state.expires_at)
        deepseek.refresh()
        with self.assertRaises(LeaseError):
            deepseek.claim(term=2)                           # live: wait for it to lapse
        self.assertEqual(deepseek.state().holder, "claudebot")

    def test_a_lapsed_lease_is_taken_and_a_promotion_takes_a_live_one(self):
        claude, deepseek = self.lease_for("Claudebot"), self.lease_for("DeepSeek")
        claude.claim(term=1)
        deepseek.refresh()
        deepseek.claim(term=2, promoted=True)                # the admin's /role, or DOWN
        claude.refresh()
        self.assertEqual(claude.state().holder, "deepseek")
        self.server.t += 700                                 # deepseek stops renewing
        claude.refresh()
        claude.claim(term=3)
        deepseek.refresh()
        self.assertEqual(deepseek.state().holder, "claudebot")
        self.assertEqual(deepseek.state().term, 3)

    def test_a_yield_ends_the_term_at_once(self):
        claude, deepseek = self.lease_for("Claudebot"), self.lease_for("DeepSeek")
        claude.claim(term=1)
        claude.yield_lease(reason="frozen")
        deepseek.refresh()
        self.assertFalse(deepseek.state().live(self.server()))
        deepseek.claim(term=2)
        self.assertTrue(deepseek.state().holds("DeepSeek", self.server()))

    def test_expiry_is_reported_in_the_bots_own_clock(self):
        behind = self.lease_for("Claudebot", offset=-20)
        behind.claim(term=1)
        local_now = self.server() - 20
        state = behind.state()
        self.assertAlmostEqual(state.expires_at - local_now, 600, delta=2)
        self.server.t += 590
        self.assertTrue(behind.state().live(self.server() - 20))
        self.server.t += 15                                  # past expiry by the log's clock
        self.assertFalse(behind.state().live(self.server() - 20))

    def test_a_peer_sees_a_claim_as_it_arrives(self):
        """Lines are folded on arrival: is_coordinator and succession need no refresh."""
        claude, deepseek = self.lease_for("Claudebot"), self.lease_for("DeepSeek")
        claude.claim(term=1)
        self.assertEqual(deepseek.state().holder, "claudebot")   # no refresh in between

    def test_a_lease_can_carry_on_above_the_gitbus_terms(self):
        claude = self.lease_for("Claudebot")
        claude.claim(term=41)
        self.assertEqual(claude.state().term, 41)
        with self.assertRaises(LeaseError):
            claude.claim(term=40)                            # never back

    def test_a_yield_keeps_the_old_holders_name(self):
        claude = self.lease_for("Claudebot")
        claude.claim(term=1)
        claude.yield_lease(reason="frozen")
        state = claude.state()
        self.assertEqual((state.holder, state.expires_at), ("Claudebot", 0.0))
        self.assertTrue(state.claim.yielded)

    def test_the_holder_is_named_as_configured(self):
        """Live drill, 25 Oct: the store's "claudebot" did not match presence's "Claudebot",
        so a DOWN holder was never seen as down and nobody took over."""
        claude, deepseek = self.lease_for("Claudebot"), self.lease_for("DeepSeek")
        deepseek._names["claudebot"] = "Claudebot"
        claude.claim(term=1)
        self.assertEqual(deepseek.state().holder, "Claudebot")

    def test_an_unready_store_is_unreadable_and_refuses_to_claim(self):
        store = TelegramStore("Claudebot", FakeIO(self.group, "Claudebot"), clock=self.server)
        lease = StoreLease(store, self.loop, alias="Claudebot")
        self.assertEqual(lease.state().status, "unreadable")
        self.assertFalse(lease.refresh())
        with self.assertRaises(LeaseError):
            lease.claim(term=1)


class ChannelUsesTheStoreLeaseTests(unittest.TestCase):
    def test_state_lease_store_routes_the_lease_to_the_store(self):
        from tests.test_telegram_channel import make_channel, make_config
        config = make_config(state={"backend": "telegram", "lease": "store"})
        channel = make_channel(config)
        self.assertEqual(channel.state_link.lease_mode, "store")
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        group = FakeGroup(Clock())
        channel.state_link.store = TelegramStore("Hermes", FakeIO(group, "Hermes"),
                                                 clock=group.clock)
        channel.__dict__["_loop"] = loop
        self.assertTrue(channel.store_lease_on())
        self.assertIsInstance(channel.lease_store(), StoreLease)

    def test_check_lease_bootstraps_and_renews_on_the_store_from_worker_threads(self):
        """The real path: check_lease runs the lease calls in worker threads, which hand the
        store's coroutines back to the running loop."""
        from tests.test_telegram_channel import make_channel, make_config
        config = make_config(state={"backend": "telegram", "lease": "store"})
        channel = make_channel(config)
        channel.config.identity.role = "coordinator"
        group = FakeGroup(Clock())
        store = TelegramStore("Hermes", FakeIO(group, "Hermes"), clock=group.clock)
        store.CONFIRM_SECONDS = 0.0
        channel.state_link.store = store
        channel._lease_clock = group.clock

        async def go():
            channel.__dict__["_loop"] = asyncio.get_running_loop()
            await store.start()
            first = await channel.check_lease()
            self.assertIsNotNone(first)
            self.assertTrue(channel.is_coordinator())
            group.clock.t += 400                       # past half the TTL: renew
            channel._forget_lease_cache()
            await channel.check_lease()
            return store.lease()
        lease = asyncio.run(go())
        self.assertEqual((lease.holder, lease.term), ("hermes", 1))
        self.assertGreater(lease.expires_at, group.clock() + 500)

    def test_the_fence_answers_on_the_event_loop(self):
        """Review: round writes and the GitHub poll call lease_fence on the loop; a blocking
        store call there was refused, so the fence always said no."""
        from tests.test_telegram_channel import make_channel, make_config
        channel = make_channel(make_config(state={"backend": "telegram", "lease": "store"}))
        group = FakeGroup(Clock())
        store = TelegramStore("Hermes", FakeIO(group, "Hermes"), clock=group.clock)
        store.CONFIRM_SECONDS = 0.0
        channel.state_link.store = store
        channel._lease_clock = group.clock
        channel.config.identity.role = "coordinator"

        async def go():
            channel.__dict__["_loop"] = asyncio.get_running_loop()
            await store.start()
            self.assertFalse(channel.lease_fence())          # nothing held yet
            await channel.check_lease()
            return channel.lease_fence(), channel._has_lease_bus()
        fence, bus = asyncio.run(go())
        self.assertTrue(fence)
        self.assertTrue(bus, "hand-over and /role yields work without a gitbus checkout")

    def test_check_lease_waits_quietly_for_the_store(self):
        from tests.test_telegram_channel import make_channel, make_config
        channel = make_channel(make_config(state={"backend": "telegram", "lease": "store"}))
        channel.config.identity.role = "coordinator"

        async def go():
            channel.__dict__["_loop"] = asyncio.get_running_loop()
            with self.assertNoLogs("swarmly.telegram", level="WARNING"):
                return await channel.check_lease()
        self.assertIsNone(asyncio.run(go()))

    def test_the_default_keeps_the_gitbus_lease(self):
        from swarmly.telegram.lease import LeaseStore
        from tests.test_telegram_channel import make_channel, make_config
        channel = make_channel(make_config(state={"backend": "telegram"}))
        self.assertEqual(channel.state_link.lease_mode, "shadow")
        self.assertIsInstance(channel.lease_store(), LeaseStore)

    def test_store_lease_needs_the_telegram_backend(self):
        from swarmly.telegram.config import ConfigError
        from tests.test_telegram_channel import make_config
        with self.assertRaises(ConfigError):
            make_config(state={"lease": "store"})


if __name__ == "__main__":
    unittest.main()
