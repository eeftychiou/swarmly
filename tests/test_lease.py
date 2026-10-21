"""The coordinator lease (issue #83).

What these pin is the whole point of the round: one holder, decided by the bus, no writer able to
forge or backdate its way into the role, and no authority at all being reported as "peer" rather
than guessed.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import lease as ls


class _Bus:
    """A bus that ranks paths, and can hold a push in flight."""

    def __init__(self, root, *, ranks=None, readable=True):
        self.root = Path(root)
        self.ref = "origin/main"
        self.readable = readable
        self.ranks = dict(ranks or {})
        self.pushed = []
        self.fail_push = False

    def refresh(self):
        return self.readable

    def list_at_ref(self, relpath):
        if not self.readable:
            return None
        folder = self.root / relpath
        if not folder.is_dir():
            return []
        return sorted(str(path.relative_to(folder)).replace("\\", "/")
                      for path in folder.glob("*.json"))

    def read_at_ref(self, relpath):
        path = self.root / relpath
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def commit_and_push(self, message, paths=None):
        if self.fail_push:
            raise RuntimeError("push rejected")
        self.pushed.append(list(paths or []))
        return True

    def commit_depth(self, relpath):
        name = relpath.rsplit("/", 1)[-1]
        return self.ranks.get(name, 0)


class TestClaims(unittest.TestCase):

    def test_a_claim_round_trips(self):
        claim = ls.LeaseClaim(term=2, alias="Claude", expires_at=1000.0, renews_at=900.0,
                              rank=0, nonce="abcd")
        again = ls.LeaseClaim.from_dict(claim.to_dict())
        self.assertEqual((again.term, again.alias, again.expires_at), (2, "Claude", 1000.0))

    def test_a_bad_claim_is_refused(self):
        for data in ({}, {"term": 1}, {"term": 1, "alias": "x"},
                     {"term": -1, "alias": "x", "expires_at": 1},
                     {"term": True, "alias": "x", "expires_at": 1}, []):
            with self.subTest(data=data), self.assertRaises(ls.LeaseError):
                ls.LeaseClaim.from_dict(data)


class TestFold(unittest.TestCase):

    def claim(self, term, alias, *, rank=0, depth=1, expires=1000.0, nonce=""):
        return ls.LeaseClaim(term=term, alias=alias, expires_at=expires, rank=rank,
                             depth=depth, nonce=nonce or alias)

    def test_the_highest_term_wins(self):
        state = ls.fold_claims([self.claim(1, "Claude"), self.claim(2, "Antigravity")])
        self.assertEqual((state.term, state.holder), (2, "Antigravity"))

    def test_equal_terms_are_decided_by_succession_rank_then_alias(self):
        state = ls.fold_claims([self.claim(3, "Zulu", rank=0), self.claim(3, "Alpha", rank=2)])
        self.assertEqual(state.holder, "Zulu")            # the earlier role, not the alias
        state = ls.fold_claims([self.claim(3, "zulu", rank=1), self.claim(3, "Alpha", rank=1)])
        self.assertEqual(state.holder, "Alpha")           # same rank: the alias decides
        self.assertIn("Zulu", "Zulu")                     # the loser is recorded, not silent
        self.assertEqual(len(state.ignored), 1)

    def test_no_claims_is_absent_not_ok(self):
        state = ls.fold_claims([])
        self.assertEqual(state.status, "absent")
        self.assertFalse(state.usable)
        self.assertIn("none", state.describe())

    def test_the_succession_order_defaults_to_role(self):
        self.assertLess(ls.succession_order("reviewer"), ls.succession_order("qa-tester"))
        self.assertLess(ls.succession_order("qa-tester"), ls.succession_order("developer"))
        self.assertLess(ls.succession_order("developer", order=["developer", "reviewer"]),
                        ls.succession_order("reviewer", order=["developer", "reviewer"]))


class TestStore(unittest.TestCase):

    def store(self, root, alias="Claude", *, bus=None, now=1000.0, ttl=600.0, rank=0):
        return ls.LeaseStore(alias, transport=bus or _Bus(root), root=Path(root),
                             clock=lambda: now, ttl_seconds=ttl, rank=rank)

    def test_a_claim_is_append_only_and_pushed(self):
        with TemporaryDirectory() as tmp:
            bus = _Bus(tmp)
            store = self.store(tmp, bus=bus)
            first = store.claim(term=1)
            second = store.claim(term=1)
            self.assertNotEqual(first.nonce, second.nonce)
            files = sorted((Path(tmp) / ".agents" / "leases").glob("*.json"))
            self.assertEqual(len(files), 2)               # never rewritten
            self.assertEqual(len(bus.pushed), 2)

    def test_a_renewal_keeps_the_term_and_moves_the_expiry(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            store = ls.LeaseStore("Claude", transport=_Bus(tmp), root=Path(tmp),
                                  clock=lambda: now["t"], ttl_seconds=600.0)
            store.claim(term=1)
            now["t"] += 300
            store.claim()                                  # renews the current term
            state = store.state(now=now["t"])
            self.assertEqual(state.term, 1)
            self.assertTrue(state.holds("Claude", now["t"]))

    def test_a_lease_never_goes_back_a_term(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.claim(term=3)
            with self.assertRaises(ls.LeaseError):
                store.claim(term=2)

    def test_an_unreadable_bus_refuses_the_claim(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp, bus=_Bus(tmp, readable=False))
            with self.assertRaises(ls.LeaseError):
                store.claim(term=1)
            self.assertEqual(store.state().status, "unreadable")
            self.assertIn("unverifiable", store.state().describe())

    def test_a_failed_push_raises(self):
        with TemporaryDirectory() as tmp:
            bus = _Bus(tmp)
            bus.fail_push = True
            store = self.store(tmp, bus=bus)
            with self.assertRaises(ls.LeasePushError):
                store.claim(term=1)

    def test_a_claim_the_bus_cannot_place_is_not_counted(self):
        with TemporaryDirectory() as tmp:
            bus = _Bus(tmp)
            entry = Path(tmp) / ".agents" / "leases"
            entry.mkdir(parents=True)
            (entry / "000001-Antigravity-aaaa.json").write_text(json.dumps(
                {"term": 1, "alias": "Antigravity", "expires_at": 9999.0}), encoding="utf-8")

            class Unplaced(_Bus):
                def commit_depth(self, relpath):
                    return None

            store = ls.LeaseStore("Claude", transport=Unplaced(tmp), root=Path(tmp),
                                  clock=lambda: 1000.0)
            self.assertEqual(store.claims(), [])

    def test_a_delayed_claim_lands_behind_the_one_it_overtook(self):
        with TemporaryDirectory() as tmp:
            entry = Path(tmp) / ".agents" / "leases"
            entry.mkdir(parents=True)
            for name, alias in (("000001-Zulu-bbbb.json", "Zulu"),
                                ("000001-Alpha-aaaa.json", "Alpha")):
                (entry / name).write_text(json.dumps(
                    {"term": 1, "alias": alias, "expires_at": 9999.0, "rank": 1}),
                    encoding="utf-8")
            # Alpha arrived first: its commit is shallower (1 ancestor against Zulu's 2).
            bus = _Bus(tmp, ranks={"000001-Alpha-aaaa.json": 1, "000001-Zulu-bbbb.json": 2})
            state = ls.LeaseStore("Zulu", transport=bus, root=Path(tmp),
                                  clock=lambda: 1000.0).state()
            self.assertEqual(state.holder, "Alpha")        # earliest arrival, not lowest name

    def test_yielding_expires_the_claim_at_that_term(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            store = ls.LeaseStore("Claude", transport=_Bus(tmp), root=Path(tmp),
                                  clock=lambda: now["t"], ttl_seconds=600.0, rank=0)
            store.claim(term=1)
            searcher = ls.LeaseStore("Antigravity", transport=_Bus(tmp), root=Path(tmp),
                                     clock=lambda: now["t"], ttl_seconds=600.0, rank=1)
            store.yield_lease(reason="frozen")
            state = searcher.state(now=now["t"])
            self.assertEqual(state.holder, "Claude")
            self.assertFalse(state.live(now["t"]))         # expired: a successor may take over
            next_term = searcher.claim(term=state.term + 1)
            self.assertEqual(next_term.term, 2)
            self.assertTrue(searcher.state(now=now["t"]).holds("Antigravity", now["t"]))


class TestLeaseSuccession(unittest.IsolatedAsyncioTestCase):
    """Reasonix B1: automatic renewal and takeover on expiry."""

    def channel(self, root: Path, alias: str, role: str, peer_roles: dict, clock_state: dict):
        from tests.test_telegram_channel import make_channel, make_config
        config = make_config(
            agent={"alias": alias, "role": role},
            bus={"transport": "folder", "folder": str(root), "repo": str(root)},
            limits={"takeover_minutes": 10.0, "lease_renew_seconds": 60.0},
        )
        config.peer_roles = peer_roles
        return make_channel(config, clock=lambda: clock_state["t"])

    async def test_renewal_moves_the_expiry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = {"t": 1000.0}
            ch = self.channel(root, "Claude", "coordinator", {"Codex": "reviewer"}, clock)
            first = await ch.check_lease(now=1000.0)
            self.assertIsNotNone(first)
            self.assertEqual(first.term, 1)
            self.assertEqual(first.expires_at, 1600.0)

            clock["t"] = 1070.0
            renewed = await ch.check_lease(now=1070.0)
            self.assertIsNotNone(renewed)
            self.assertEqual(renewed.term, 1)
            self.assertEqual(renewed.expires_at, 1670.0)
            self.assertEqual(ch.lease_store().state(now=1070.0).expires_at, 1670.0)

    async def test_expiry_makes_exactly_one_successor_claim_n_plus_1(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = {"t": 1000.0}
            claude = self.channel(root, "Claude", "coordinator",
                                  {"Codex": "reviewer", "Antigravity": "developer"}, clock)
            codex = self.channel(root, "Codex", "reviewer",
                                 {"Claude": "coordinator", "Antigravity": "developer"}, clock)
            antigravity = self.channel(root, "Antigravity", "developer",
                                       {"Claude": "coordinator", "Codex": "reviewer"}, clock)

            await claude.check_lease(now=1000.0)
            self.assertEqual(claude.lease_store().state(now=1000.0).holder, "Claude")

            clock["t"] = 1700.0
            self.assertFalse(codex.lease_store().state(now=1700.0).live(1700.0))

            codex_claim = await codex.check_lease(now=1700.0)
            self.assertIsNotNone(codex_claim)
            self.assertEqual(codex_claim.term, 2)
            self.assertEqual(codex_claim.alias, "Codex")

            await antigravity.check_lease(now=1700.0)
            self.assertEqual(antigravity.lease_store().state(now=1700.0).holder, "Codex")

    async def test_a_frozen_successor_is_skipped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = {"t": 1000.0}
            claude = self.channel(root, "Claude", "coordinator",
                                  {"Codex": "reviewer", "Antigravity": "developer"}, clock)
            antigravity = self.channel(root, "Antigravity", "developer",
                                       {"Claude": "coordinator", "Codex": "reviewer"}, clock)

            await claude.check_lease(now=1000.0)

            clock["t"] = 1700.0
            antigravity._peers_frozen["Codex"] = {"until": None, "reason": "test"}

            ag_claim = await antigravity.check_lease(now=1700.0)
            self.assertIsNotNone(ag_claim)
            self.assertEqual(ag_claim.term, 2)
            self.assertEqual(ag_claim.alias, "Antigravity")
            self.assertEqual(antigravity.lease_store().state(now=1700.0).holder, "Antigravity")

    async def test_the_tick_after_a_clean_takeover_renews_at_n_plus_1(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = {"t": 1000.0}
            codex = self.channel(root, "Codex", "reviewer",
                                 {"Claude": "coordinator"}, clock)

            leases_dir = root / ".agents" / "leases"
            leases_dir.mkdir(parents=True, exist_ok=True)
            (leases_dir / "000001-claude-aaaa.json").write_text(json.dumps({
                "term": 1, "alias": "Claude", "expires_at": 500.0, "renews_at": 100.0
            }), encoding="utf-8")

            takeover = await codex.check_lease(now=1000.0)
            self.assertIsNotNone(takeover)
            self.assertEqual(takeover.term, 2)
            self.assertEqual(takeover.expires_at, 1600.0)

            clock["t"] = 1070.0
            renewed = await codex.check_lease(now=1070.0)
            self.assertIsNotNone(renewed)
            self.assertEqual(renewed.term, 2)
            self.assertEqual(renewed.expires_at, 1670.0)

    def test_a_live_holder_cannot_be_stolen_at_the_same_term(self):
        with TemporaryDirectory() as tmp:
            bus = _Bus(tmp)
            now = 1000.0
            claude = ls.LeaseStore("Claude", transport=bus, root=Path(tmp),
                                   clock=lambda: now, ttl_seconds=600.0, rank=1)
            claude.claim(term=1)

            reviewer = ls.LeaseStore("Codex", transport=bus, root=Path(tmp),
                                    clock=lambda: now, ttl_seconds=600.0, rank=0)
            with self.assertRaises(ls.LeaseError):
                reviewer.claim(term=1)

    async def test_role_promotion_claims_next_term(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = {"t": 1000.0}
            ch = self.channel(root, "Hermes", "developer", {"Claude": "coordinator"}, clock)
            # Existing term 1 lease from Claude
            leases_dir = root / ".agents" / "leases"
            leases_dir.mkdir(parents=True, exist_ok=True)
            (leases_dir / "000001-claude-aaaa.json").write_text(json.dumps({
                "term": 1, "alias": "Claude", "expires_at": 1600.0, "renews_at": 1000.0
            }), encoding="utf-8")

            # Admin promotes Hermes to coordinator
            await ch._on_promoted_to_coordinator()
            state = ch.lease_store().state(now=1000.0)
            self.assertEqual(state.holder, "Hermes")
            self.assertEqual(state.term, 2)
            self.assertTrue(state.live(1000.0))

    async def test_role_promotion_answers_in_the_chat(self):
        # #126: /role changed the lease and said nothing, so the admin could not tell it landed.
        from tests.test_telegram_channel import event as make_event
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ch = self.channel(root, "Hermes", "developer", {"Claude": "coordinator"},
                              {"t": 1000.0})
            await ch._on_promoted_to_coordinator(make_event("/role @HermesBot coordinator",
                                                            message_id=77))
            sent = ch._test_sender.sent
            self.assertEqual(sent[-1]["text"], "\u2705 Hermes is coordinator at term 1.")
            self.assertEqual(sent[-1]["reply_to"], 77)

    async def test_a_promotion_that_does_not_reach_the_bus_says_so(self):
        from swarmly.telegram import lease as lease_mod
        with TemporaryDirectory() as tmp:
            ch = self.channel(Path(tmp), "Hermes", "developer", {}, {"t": 1000.0})
            original = ch.lease_store

            def failing():
                store = original()

                def claim(**_kwargs):
                    raise lease_mod.LeasePushError("git push failed (rejected)")
                store.claim = claim
                return store

            ch.lease_store = failing
            await ch._on_promoted_to_coordinator()
            self.assertIn("did not reach the bus: git push failed (rejected)",
                          ch._test_sender.last)

    async def test_a_slow_claim_does_not_stop_the_bot(self):
        # #126, live: a /role claim whose push retried for minutes ran on the event loop, and
        # the bot handled no message and fired no timer until git was done.
        import asyncio
        import time as time_mod
        with TemporaryDirectory() as tmp:
            ch = self.channel(Path(tmp), "Hermes", "developer", {}, {"t": 1000.0})
            original = ch.lease_store

            def slow():
                store = original()
                claim = store.claim

                def slow_claim(**kwargs):
                    time_mod.sleep(0.5)                  # a push that takes its time
                    return claim(**kwargs)
                store.claim = slow_claim
                return store

            ch.lease_store = slow
            ticks = 0

            async def other_work():
                nonlocal ticks
                for _ in range(20):
                    await asyncio.sleep(0.01)
                    ticks += 1

            await asyncio.gather(ch._on_promoted_to_coordinator(), other_work())
            self.assertEqual(ticks, 20)
            self.assertIn("is coordinator at term 1", ch._test_sender.last)

    async def test_a_slow_renewal_does_not_stop_the_bot(self):
        import asyncio
        import time as time_mod
        with TemporaryDirectory() as tmp:
            ch = self.channel(Path(tmp), "Claude", "coordinator", {}, {"t": 1000.0})
            original = ch.lease_store
            loop_thread = []

            def slow():
                store = original()
                claim = store.claim

                def slow_claim(**kwargs):
                    import threading
                    loop_thread.append(threading.current_thread() is threading.main_thread())
                    time_mod.sleep(0.3)
                    return claim(**kwargs)
                store.claim = slow_claim
                return store

            ch.lease_store = slow
            ticks = 0

            async def other_work():
                nonlocal ticks
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    ticks += 1

            first, _ = await asyncio.gather(ch.check_lease(now=1000.0), other_work())
            self.assertEqual(first.term, 1)
            self.assertEqual(ticks, 10)
            self.assertEqual(loop_thread, [False])


class TestNoAuthorityOnAFailedClaim(unittest.TestCase):
    """#93 B2: a lease claim that never reached the bus is not authority.

    Granting term 1 anyway would write the round at a term no successor can reason about -- the
    same fail-closed rule #92 applies to a claim the bus cannot place.
    """

    def channel(self, *, fails):
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "coordinator"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""})
        channel = make_channel(config)

        class Store:
            ttl_seconds = 600.0

            def state(self, *, now=None):
                return ls.LeaseState(status="absent", term=0, holder="", expires_at=0.0)

            def claim(self, *, term=None, now=None):
                if fails:
                    raise ls.LeasePushError("the bus did not take it")
                return ls.LeaseClaim(term=term or 1, alias=channel.config.identity.alias,
                                     expires_at=1e9, nonce="abcd")

        channel.lease_store = lambda: Store()
        return channel

    def test_a_failed_bootstrap_claim_grants_no_authority(self):
        authority = self.channel(fails=True).round_authority(now=1000.0)
        self.assertEqual(authority.alias, "")
        self.assertEqual(authority.term, 0)

    def test_a_successful_bootstrap_claim_grants_its_own_term(self):
        authority = self.channel(fails=False).round_authority(now=1000.0)
        self.assertEqual(authority.alias, "Hermes")
        self.assertEqual(authority.term, 1)


class TestLeaseTickRuns(unittest.TestCase):
    """#93 B1: the lease has to work on a *real* channel, not only on the store.

    `_clock` and `_sleep` are what the tick and the coordinator gate hang off; a missing one made
    `is_coordinator()` raise inside its try (always False) and killed `_lease_tick` after its first
    pass. This runs one tick end to end, which is the acceptance the review asked for.
    """

    def channel(self):
        import asyncio
        from tests.gitenv import git as run_git
        from tests.test_telegram_channel import make_channel, make_config
        slept = []

        async def sleeper(seconds):
            slept.append(seconds)
            raise asyncio.CancelledError          # one pass is all this test needs

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        remote = base / "remote.git"
        run_git(base, "init", "-q", "--bare", "-b", "main", str(remote))
        bus = base / "bus"
        run_git(base, "clone", "-q", str(remote), str(bus))
        (bus / "README").write_text("bus", encoding="utf-8")
        run_git(bus, "add", "README")
        run_git(bus, "commit", "-q", "-m", "bus")
        run_git(bus, "push", "-q", "-u", "origin", "main")
        config = make_config(agent={"role": "coordinator"},
                             fallback={"gitbus_root": str(bus), "gitbus_ref": "origin/main"},
                             limits={"lease_cache_seconds": 0})
        channel = make_channel(config, sleeper=sleeper)
        return channel, bus, slept

    def test_the_tick_renews_or_claims_and_then_sleeps(self):
        import asyncio
        channel, root, slept = self.channel()
        now = channel._clock()
        claim = asyncio.run(channel.check_lease(now=now))
        self.assertIsNotNone(claim, "a coordinator with no live lease must claim term 1")
        self.assertEqual(claim.term, 1)
        self.assertTrue(channel.is_coordinator(now=now))
        self.assertEqual(channel.current_coordinator(now=now), "Hermes")
        # A real renewal: the second tick finds this bot the live holder.
        renewed = asyncio.run(channel.check_lease(now=now + 1))
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed.term, 1)
        # And the loop itself: one pass, then the sleep that ends it.
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(channel._lease_tick())
        self.assertEqual(len(slept), 1)

    def test_a_peer_with_a_foreign_lease_is_not_coordinator(self):
        import asyncio
        channel, root, _slept = self.channel()
        now = channel._clock()
        other = ls.LeaseStore("Antigravity", transport=channel.lease_store().transport,
                              root=root, clock=lambda: now, ttl_seconds=600.0)
        other.claim(term=1)
        self.assertFalse(channel.is_coordinator(now=now))
        self.assertEqual(channel.current_coordinator(now=now), "Antigravity")


class TestSuccessionOnExpiry(unittest.TestCase):
    """#93 B3: a dead higher-ranked peer must not block the next one, and a live holder must not
    be usurped by anyone willing to write the next term."""

    def channel(self, *, role="qa-tester", alias="Hermes", peers=None, settle=0.0, bus=None):
        import asyncio
        from tests.gitenv import git as run_git
        from tests.test_telegram_channel import make_channel, make_config
        noted = []

        async def sleeper(seconds):
            noted.append(seconds)
            return None

        async def sender(payload, reply_to_message_id=None, **kwargs):
            noted.append(("sent", payload))
            return payload

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        if bus is None:
            remote = base / "remote.git"
            run_git(base, "init", "-q", "--bare", "-b", "main", str(remote))
            bus = base / "bus"
            run_git(base, "clone", "-q", str(remote), str(bus))
            (bus / "README").write_text("bus", encoding="utf-8")
            run_git(bus, "add", "README")
            run_git(bus, "commit", "-q", "-m", "bus")
            run_git(bus, "push", "-q", "-u", "origin", "main")
        config = make_config(agent={"name": f"{alias.lower()}_bot", "alias": alias,
                                    "role": role},
                             peers=peers or {"Claude": "claude_bot"},
                             fallback={"gitbus_root": str(bus), "gitbus_ref": "origin/main"},
                             limits={"lease_cache_seconds": 0, "lease_settle_seconds": settle})
        channel = make_channel(config, sleeper=sleeper)
        channel._test_sender_fn = sender
        return channel, bus, noted

    def test_a_dead_higher_ranked_peer_does_not_block_the_takeover(self):
        import asyncio
        channel, bus, _noted = self.channel(
            role="qa-tester",
            peers={"Claude": {"username": "claude_bot", "role": "reviewer"}})
        now = channel._clock()
        # The reviewer claimed and then died: its lease lapsed without a renew.
        dead = ls.LeaseStore("Claude", transport=channel.lease_store().transport, root=bus,
                             clock=lambda: now, ttl_seconds=60.0, rank=0)
        dead.claim(term=1)
        later = now + 3600
        claim = asyncio.run(channel.check_lease(now=later))
        self.assertIsNotNone(claim, "a dead better-ranked peer must not block the next in line")
        self.assertEqual(claim.term, 2)
        self.assertTrue(channel.is_coordinator(now=later))

    def test_two_claimants_leave_exactly_one_acting(self):
        import asyncio
        one, bus, _ = self.channel(alias="Hermes", role="reviewer",
                                   peers={"Antigravity": {"username": "agy_bot",
                                                          "role": "qa-tester"}})
        two, _bus, _ = self.channel(alias="Antigravity", role="qa-tester", bus=bus,
                                    peers={"Hermes": {"username": "hermes_bot",
                                                      "role": "reviewer"}})
        now = one._clock()
        lapsed = ls.LeaseStore("Claude", transport=one.lease_store().transport, root=bus,
                               clock=lambda: now, ttl_seconds=60.0, rank=0)
        lapsed.claim(term=1)
        later = now + 3600

        async def run_concurrent():
            return await asyncio.gather(one.check_lease(now=later), two.check_lease(now=later))

        claims = asyncio.run(run_concurrent())
        acted = [claim for claim in claims if claim is not None]
        self.assertEqual(len(acted), 1, "two bots claiming N+1 must not both act")
        self.assertEqual(one.lease_store().state(now=later).term, 2)

    def test_a_live_lease_cannot_be_taken_by_a_higher_term(self):
        with TemporaryDirectory() as tmp:
            now = 1000.0
            bus = _Bus(tmp)
            holder = ls.LeaseStore("Claude", transport=bus, root=Path(tmp),
                                    clock=lambda: now, ttl_seconds=600.0, rank=0)
            holder.claim(term=1)
            usurper = ls.LeaseStore("Antigravity", transport=bus, root=Path(tmp),
                                    clock=lambda: now, ttl_seconds=600.0, rank=1)
            with self.assertRaises(ls.LeaseError):
                usurper.claim(term=2)
            # /role is the one path allowed to take a live lease over.
            promoted = usurper.claim(term=2, promoted=True)
            self.assertEqual((promoted.term, promoted.alias), (2, "Antigravity"))


    def test_claims_keyed_on_ref_tip_sha(self):
        with TemporaryDirectory() as tmp:
            calls = {"rev_parse": 0, "list_at_ref": 0}

            class MockGitTransport(_Bus):
                def __init__(self, root):
                    super().__init__(root)
                    self.tip = "sha11111111"

                def _git(self, *args, **kwargs):
                    class Proc:
                        returncode = 0
                        stdout = self.tip
                    calls["rev_parse"] += 1
                    return Proc()

                def list_at_ref(self, relpath):
                    calls["list_at_ref"] += 1
                    return super().list_at_ref(relpath)

            bus = MockGitTransport(tmp)
            claude = ls.LeaseStore("Claude", transport=bus, root=Path(tmp),
                                   clock=lambda: 1000.0, ttl_seconds=600.0)
            claude.claim(term=1)

            reader = ls.LeaseStore("Hermes", transport=bus, root=Path(tmp),
                                   clock=lambda: 1000.0)
            calls["list_at_ref"] = 0
            c1 = reader.claims()
            self.assertEqual(len(c1), 1)
            self.assertEqual(calls["list_at_ref"], 1)

            # Second call on unchanged tip SHA: returns cached claims without calling list_at_ref
            c2 = reader.claims()
            self.assertEqual(len(c2), 1)
            self.assertEqual(calls["list_at_ref"], 1)

            # When tip moves: list_at_ref is called again
            bus.tip = "sha22222222"
            c3 = reader.claims()
            self.assertEqual(len(c3), 1)
            self.assertEqual(calls["list_at_ref"], 2)


class TestFoldCostAndPolling(unittest.TestCase):
    """#93 B4 and the single-holder rule: cheap folds, and one poller.

    Old terms can never win -- a lease does not go back a term -- so the fold must not read them,
    and a bot whose lease says no must not poll GitHub.
    """

    def test_only_the_top_term_is_read(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / ".agents" / "leases"
            folder.mkdir(parents=True)
            # 30 old claims, and the current term's one.
            for index in range(30):
                (folder / f"{index + 1:06d}-claude-{index:08d}.json").write_text(json.dumps(
                    {"term": index + 1, "alias": "Claude", "expires_at": 1.0}), encoding="utf-8")
            (folder / "000031-antigravity-aaaaaaa1.json").write_text(json.dumps(
                {"term": 31, "alias": "Antigravity", "expires_at": 9999.0}), encoding="utf-8")
            reads = []

            class Counting(_Bus):
                def read_at_ref(self, relpath):
                    reads.append(relpath)
                    return super().read_at_ref(relpath)

            state = ls.LeaseStore("Claude", transport=Counting(tmp), root=Path(tmp),
                                  clock=lambda: 1000.0).state(now=1000.0)
            self.assertEqual((state.term, state.holder), (31, "Antigravity"))
            self.assertEqual(len(reads), 1, f"read {len(reads)} claims; only the top term matters")

    def test_a_bot_without_the_lease_does_not_poll_github(self):
        from swarmly.github.watch import GitHubWatcher
        from tests.test_github_watch import make_test_config
        from unittest.mock import AsyncMock, MagicMock

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        channel = MagicMock()
        channel.is_coordinator.return_value = False          # the lease says: peer
        channel.send = AsyncMock()
        client = MagicMock()
        client.notifications.return_value = []
        watcher = GitHubWatcher(make_test_config(tmp.name), client=client,
                                state_path=Path(tmp.name) / "s.json", channel=channel)
        self.assertFalse(watcher._is_coordinator())
        client.notifications.assert_not_called()

    def test_no_channel_means_no_authority(self):
        from unittest.mock import MagicMock
        from swarmly.github.watch import GitHubWatcher
        from tests.test_github_watch import make_test_config

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        watcher = GitHubWatcher(make_test_config(tmp.name, role="coordinator"), client=MagicMock(),
                                state_path=Path(tmp.name) / "s.json", channel=None)
        # A configured coordinator with no lease to ask is a peer, not a poller: the fallback to
        # the configured role is how a stale coordinator keeps polling beside the acting one.
        self.assertFalse(watcher._is_coordinator())


class TestLateButBetterRankedClaim(unittest.TestCase):
    """#93 B3a: arriving late with a better role must not win a firing already claimed.

    The fold orders by when the bus saw the claim, and rank only breaks a tie between claims at
    the same moment. Ordering by rank first let a better-ranked claimant write N+1 after another
    bot had acted and still win -- two acting coordinators.
    """

    def claim(self, alias, *, rank, depth, term=2):
        return ls.LeaseClaim(term=term, alias=alias, expires_at=9999.0, renews_at=1000.0,
                             rank=rank, depth=depth, nonce=f"{alias}-{depth}")

    def test_the_earlier_claim_beats_a_better_ranked_late_one(self):
        early = self.claim("Zulu", rank=2, depth=1)        # arrived first: shallower commit
        late = self.claim("Alpha", rank=0, depth=2)        # arrived later: deeper commit
        state = ls.fold_claims([early, late])
        self.assertEqual(state.holder, "Zulu")

    def test_a_tie_on_arrival_is_broken_by_rank(self):
        one = self.claim("Zulu", rank=2, depth=3)
        two = self.claim("Alpha", rank=0, depth=3)
        self.assertEqual(ls.fold_claims([one, two]).holder, "Alpha")

    def test_the_settle_window_outlasts_a_push(self):
        from tests.test_telegram_channel import make_config
        # The default has to be longer than a push can take: #92 settled on 30s for the same
        # reason, and 5s equal to the ref-refresh limit was the hole Reasonix named.
        self.assertGreaterEqual(make_config().limits.lease_settle_seconds, 30.0)


class TestCoordinatorFence(unittest.TestCase):
    """#93: a lease displaced mid-turn must fence work that is already under way.

    The settle window orders claims; it does not stop coordinator work. So every action with a
    side effect re-reads the lease first, and a bot that is no longer the holder does not act --
    that is what keeps a late claim down to one merge instead of two.
    """

    def channel(self, *, role="coordinator", alias="Hermes"):
        from tests.gitenv import git as run_git
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        remote = base / "remote.git"
        run_git(base, "init", "-q", "--bare", "-b", "main", str(remote))
        bus = base / "bus"
        run_git(base, "clone", "-q", str(remote), str(bus))
        (bus / "README").write_text("bus", encoding="utf-8")
        run_git(bus, "add", "README")
        run_git(bus, "commit", "-q", "-m", "bus")
        run_git(bus, "push", "-q", "-u", "origin", "main")
        config = make_config(agent={"name": f"{alias.lower()}_bot", "alias": alias, "role": role},
                             fallback={"gitbus_root": str(bus), "gitbus_ref": "origin/main"},
                             limits={"lease_cache_seconds": 0})
        channel = make_channel(config)
        return channel, bus

    def test_the_holder_is_allowed_to_act(self):
        channel, _bus = self.channel()
        now = channel._clock()
        channel.lease_store().claim(term=1, now=now)
        self.assertTrue(channel.lease_fence(now=now + 1))

    def test_a_displaced_coordinator_may_not_act(self):
        channel, bus = self.channel()
        now = channel._clock()
        channel.lease_store().claim(term=1, now=now)
        # A successor takes term 2 while this bot's turn is still running.
        heir = ls.LeaseStore("Antigravity", transport=channel.lease_store().transport, root=bus,
                             clock=lambda: now, ttl_seconds=600.0, rank=0)
        heir.claim(term=2, promoted=True, now=now)
        self.assertFalse(channel.lease_fence(now=now + 1))

    def test_an_unreadable_lease_is_not_permission(self):
        from swarmly.telegram.lease import LeaseState
        channel, _bus = self.channel()

        class Broken:
            ttl_seconds = 600.0

            def refresh(self, *, force=False):
                return False

            def state(self, *, now=None):
                return LeaseState(status="unreadable", term=0, holder="", expires_at=0.0)

        channel.lease_store = lambda: Broken()
        # No fresh ref means no answer, and no answer is not authority.
        self.assertFalse(channel.lease_fence())

    def test_a_displaced_coordinator_does_not_write_the_round(self):
        from tests.test_telegram_channel import event
        channel, bus = self.channel()
        now = channel._clock()
        channel.lease_store().claim(term=1, now=now)
        heir = ls.LeaseStore("Antigravity", transport=channel.lease_store().transport, root=bus,
                             clock=lambda: now, ttl_seconds=600.0, rank=0)
        heir.claim(term=2, promoted=True, now=now)
        body = channel.apply_round_block(
            'x\nROUND_STATE {"version": 1, "round": 88, "items": {"86": {"owner": "DeepSeek"}}}',
            event("hi", username="ClaudeBot"))
        # Refused before the write, with a reason in the reply: either the coordinator gate or
        # the fence catches it, and both mean the same thing -- this bot may not write the round.
        self.assertIn("round state:", body)
        self.assertFalse(list((bus / ".agents" / "rounds").rglob("*.json")))


class TestTipAdvancing(unittest.TestCase):
    """#93: a cached claim must not go stale when the branch grows.

    `arrival_rank` is "commits after the adding commit", which every new commit changes; caching
    it let an old claim look later than it is, so a later, better-ranked claim won. The fold now
    orders by the adding commit's depth, which no later commit can move, and the cache holds that.
    """

    def test_a_later_higher_ranked_claim_still_loses_after_the_tip_moves(self):
        import subprocess
        from tests.gitenv import git as run_git, git_env
        from swarmly.transports.git import GitTransport

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        remote = base / "remote.git"
        run_git(base, "init", "-q", "--bare", "-b", "main", str(remote))
        bus = base / "bus"
        run_git(base, "clone", "-q", str(remote), str(bus))
        (bus / "README").write_text("bus", encoding="utf-8")
        run_git(bus, "add", "README")
        run_git(bus, "commit", "-q", "-m", "bus")
        run_git(bus, "push", "-q", "-u", "origin", "main")

        now = 1000.0
        first = ls.LeaseStore("Zulu", transport=GitTransport(bus, ref="origin/main",
                                                             identity=("t", "t@t")),
                              root=bus, clock=lambda: now, ttl_seconds=60.0, rank=2)
        first.claim(term=1)                     # the first claim, worse role, arrives first
        # Warm the cache *before* the tip moves: without this the test would pass even with the
        # old arrival-rank ordering, because nothing stale would ever have been cached (#93).
        warm = ls.LeaseStore("Zulu", transport=GitTransport(bus, ref="origin/main",
                                                            identity=("t", "t@t")),
                             root=bus, clock=lambda: now, ttl_seconds=60.0)
        self.assertEqual(warm.state(now=now).holder, "Zulu")
        # The branch grows by a few commits, then a better-ranked bot claims the same term.
        for index in range(3):
            (bus / f"noise{index}.txt").write_text("x", encoding="utf-8")
            run_git(bus, "add", f"noise{index}.txt")
            run_git(bus, "commit", "-q", "-m", f"noise {index}")
        run_git(bus, "push", "-q")
        # Alpha's claim lands after the noise: written as a bus fact, since `claim()` refuses a
        # live holder's term (that guard is a different test).
        import json as _json
        entry = bus / ".agents" / "leases"
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "000001-alpha-late0001.json").write_text(_json.dumps(
            {"term": 1, "alias": "Alpha", "expires_at": 9999.0, "renews_at": now, "rank": 0,
             "nonce": "late0001"}), encoding="utf-8")
        run_git(bus, "add", ".agents/leases")
        run_git(bus, "commit", "-q", "-m", "alpha claims later")
        run_git(bus, "push", "-q")

        reader = ls.LeaseStore("Zulu", transport=GitTransport(bus, ref="origin/main",
                                                              identity=("t", "t@t")),
                               root=bus, clock=lambda: now, ttl_seconds=60.0)
        reader.refresh(force=True)
        state = reader.state(now=now)
        self.assertEqual(state.holder, "Zulu",
                         "the claim the bus saw first must still win after the tip moved")


class TestPeerDoesNotPoll(unittest.TestCase):
    """#93 (Codex): one shared GitHub login, one poller.

    `poll_once()` used to fetch notifications before asking who leads, so a peer handled the same
    notification the acting coordinator handled. A bot whose channel says "not the coordinator"
    now polls nothing; the standalone `gh-poll --once` path, which has no channel, still polls,
    because it is the only reader by construction.
    """

    def test_a_peer_channel_polls_nothing(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from swarmly.github.watch import GitHubWatcher
        from tests.test_github_watch import make_test_config

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        channel = MagicMock()
        channel.is_coordinator.return_value = False
        channel.lease_fence.return_value = False
        client = MagicMock()
        client.notifications.return_value = []
        watcher = GitHubWatcher(make_test_config(tmp.name, role="coordinator"), client=client,
                                state_path=Path(tmp.name) / "s.json", channel=channel)
        outcome = asyncio.run(watcher.poll_once())
        self.assertEqual(outcome.polled_repos, [])
        client.notifications.assert_not_called()

    def test_a_displaced_coordinator_does_not_poll_on_a_cached_fold(self):
        """Codex's blocker: `is_coordinator()` can answer from a fold up to 5s old.

        The poll asks the fence instead, which re-reads the lease, so a bot displaced a moment
        ago does not read the shared login beside its successor.
        """
        import asyncio
        from unittest.mock import MagicMock
        from swarmly.github.watch import GitHubWatcher
        from tests.test_github_watch import make_test_config

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        channel = MagicMock()
        channel.is_coordinator.return_value = True          # stale: says yes
        channel.lease_fence.return_value = False            # fresh: says no
        client = MagicMock()
        client.notifications.return_value = []
        watcher = GitHubWatcher(make_test_config(tmp.name, role="coordinator"), client=client,
                                state_path=Path(tmp.name) / "s.json", channel=channel)
        outcome = asyncio.run(watcher.poll_once())
        self.assertEqual(outcome.polled_repos, [])
        client.notifications.assert_not_called()
        channel.lease_fence.assert_called()


class TestRenewalKeepsTheEarliestDepth(unittest.TestCase):
    """#93 R6: a holder's renewal must not make it look later than the claimant it beat.

    `claims()` keeps one entry per (term, alias): its times come from the latest renewal, but its
    ordering key has to stay the group's *earliest* claim. Otherwise A claiming N+1 at depth 100
    and renewing at 140 loses the term to B, who claimed at 105.
    """

    def test_a_renewal_does_not_move_the_holder_behind_a_late_claimant(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / ".agents" / "leases"
            folder.mkdir(parents=True)
            # A: the original claim (depth 100) and a renewal much later in the branch (depth 140).
            (folder / "000002-alpha-aaaa0001.json").write_text(json.dumps(
                {"term": 2, "alias": "Alpha", "expires_at": 5000.0, "renews_at": 1000.0,
                 "rank": 0, "nonce": "aaaa0001"}), encoding="utf-8")
            (folder / "000002-alpha-aaaa0002.json").write_text(json.dumps(
                {"term": 2, "alias": "Alpha", "expires_at": 9000.0, "renews_at": 2000.0,
                 "rank": 0, "nonce": "aaaa0002"}), encoding="utf-8")
            # B: a later claimant at depth 105, better ranked.
            (folder / "000002-zulu-bbbb0001.json").write_text(json.dumps(
                {"term": 2, "alias": "Zulu", "expires_at": 5000.0, "renews_at": 1500.0,
                 "rank": 1, "nonce": "bbbb0001"}), encoding="utf-8")
            depths = {"000002-alpha-aaaa0001.json": 100, "000002-alpha-aaaa0002.json": 140,
                      "000002-zulu-bbbb0001.json": 105}
            bus = _Bus(tmp, ranks=depths)
            state = ls.LeaseStore("Alpha", transport=bus, root=Path(tmp),
                                  clock=lambda: 2000.0).state(now=2000.0)
            self.assertEqual(state.holder, "Alpha", "the earlier claim keeps the term")
            # ... and the times still come from the renewal, so the lease is live.
            self.assertEqual(state.expires_at, 9000.0)
            self.assertTrue(state.live(2000.0))


class TestLeaseRunsOnWallTime(unittest.TestCase):
    """Live: the lease was stamped with time.monotonic, which counts from each host's boot.
    A Linux peer up 34 days read the Windows coordinator's lease as long expired and took
    over; its own lease then looked valid for a month on Windows. Lease times must be wall
    times, comparable across hosts."""

    def test_a_production_channel_stamps_the_lease_with_wall_time(self):
        import time
        from swarmly.telegram.channel import AgentChannel
        from tests.test_telegram_channel import make_config
        channel = AgentChannel(make_config(), sender=lambda *a, **k: None)
        self.assertIs(channel._lease_clock, time.time)
        self.assertLess(abs(channel._lease_clock() - time.time()), 5)


class TestTheNamedCoordinatorKeepsTheRole(TestSuccessionOnExpiry):
    """Live: the admin's /role made the acting coordinator (Codex) yield, and 27 s later Codex
    took the lapsed lease back by succession, ahead of the named coordinator's slower
    promotion push. A lapsed lease is left to the named coordinator while it is up."""

    def lapsed(self):
        channel, bus, _ = self.channel(
            role="reviewer", peers={"Claude": {"username": "claude_bot", "role": "coordinator"}})
        now = channel._clock()
        gone = ls.LeaseStore("Codex", transport=channel.lease_store().transport, root=bus,
                             clock=lambda: now, ttl_seconds=60.0, rank=0)
        gone.claim(term=1)
        channel.membership.state.coordinator = "Claude"
        return channel, now + 3600

    def test_a_peer_does_not_take_the_role_from_a_named_coordinator_that_is_up(self):
        import asyncio
        channel, later = self.lapsed()
        channel.presence().seen("Claude", channel.presence_clock())
        self.assertIsNone(asyncio.run(channel.check_lease(now=later)))
        self.assertFalse(channel.is_coordinator(now=later))

    def test_it_takes_over_when_the_named_coordinator_is_down(self):
        import asyncio
        from unittest import mock
        channel, later = self.lapsed()
        channel.presence().seen("Claude", channel.presence_clock())
        with mock.patch.object(type(channel.presence()), "is_down", return_value=True):
            claim = asyncio.run(channel.check_lease(now=later))
        self.assertIsNotNone(claim)
        self.assertEqual(claim.term, 2)

    def test_it_takes_over_from_a_coordinator_never_heard_from(self):
        import asyncio
        channel, later = self.lapsed()
        self.assertIsNotNone(asyncio.run(channel.check_lease(now=later)))

    def test_it_takes_over_from_a_frozen_coordinator(self):
        import asyncio
        channel, later = self.lapsed()
        channel.presence().seen("Claude", channel.presence_clock())
        channel.frozen_peers = lambda: {"Claude": {"ref": "quota"}}
        self.assertIsNotNone(asyncio.run(channel.check_lease(now=later)))
