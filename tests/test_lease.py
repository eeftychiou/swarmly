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
