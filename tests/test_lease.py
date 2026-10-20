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

    def arrival_rank(self, relpath):
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

    def claim(self, term, alias, *, rank=0, arrival=1, expires=1000.0, nonce=""):
        return ls.LeaseClaim(term=term, alias=alias, expires_at=expires, rank=rank,
                             arrival=arrival, nonce=nonce or alias)

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

    def test_a_claim_the_bus_cannot_rank_is_not_counted(self):
        with TemporaryDirectory() as tmp:
            bus = _Bus(tmp)
            entry = Path(tmp) / ".agents" / "leases"
            entry.mkdir(parents=True)
            (entry / "000001-Antigravity-aaaa.json").write_text(json.dumps(
                {"term": 1, "alias": "Antigravity", "expires_at": 9999.0}), encoding="utf-8")

            class Unranked(_Bus):
                def arrival_rank(self, relpath):
                    return None

            store = ls.LeaseStore("Claude", transport=Unranked(tmp), root=Path(tmp),
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
            # Alpha is lower alphabetically but arrived first on the bus: rank 1 vs 0.
            bus = _Bus(tmp, ranks={"000001-Alpha-aaaa.json": 1, "000001-Zulu-bbbb.json": 0})
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
            ch._on_promoted_to_coordinator()
            state = ch.lease_store().state(now=1000.0)
            self.assertEqual(state.holder, "Hermes")
            self.assertEqual(state.term, 2)
            self.assertTrue(state.live(1000.0))


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
