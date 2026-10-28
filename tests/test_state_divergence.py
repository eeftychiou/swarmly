"""A bot that missed a STATE line never agreed again -- and the pin is the only repair (#477).

Live, 28 Oct, after the cfe6280 deploy: five bots on one snapshot and one lease folded three
different states, and two of them sat at the same position with different hashes. Positions
come from the Telegram message id, so the same position means the same log *order* -- what
differed was which lines each bot had folded.

A bot polls; it cannot re-read what Telegram stopped handing it. So a line posted while it was
down is gone for that bot, and the fold it keeps is wrong for ever: a write it refused (the
lease holder it never saw, the version it missed) stays refused, and nothing in the log says
so. The snapshot is the lease holder's fold of everything up to a position, which makes
re-reading the pin the one repair there is.

This pins both halves: the divergence itself, and that a resync converges again.
"""

import asyncio
import unittest

from swarmly.state import keys as K
from swarmly.state.store import MemoryLog, MemoryStore

from tests.test_registry_shadow import Clock


class MissedLineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.shared = MemoryLog(clock=self.clock)

    def store(self, alias):
        store = MemoryStore(alias, self.shared, clock=self.clock)
        asyncio.run(store.start())
        return store

    def claim(self, holder="Claudebot"):
        store = self.store(holder)
        asyncio.run(store.claim_lease(600))
        self.claim_pos = store.log.last_pos        # the line a bot down at that moment misses
        return store

    def test_a_missed_lease_line_diverges_the_fold_for_ever(self):
        """DISCRIMINATING: without the missed line the same ops fold differently.

        The holder's topic write is refused by a reader that never saw the claim: it is not
        the holder as far as that reader knows, and `_refusal` says so. Both logs then hold the
        same positions with different states -- the live shape.
        """
        holder = self.claim()
        asyncio.run(holder.write("topic/55", {"topic_id": 55, "status": "open"}))
        asyncio.run(holder.refresh())

        blind = self.store("DeepSeek")
        blind.log.ops.clear()                     # it never received the claim
        blind.log._refold()
        blind._seen = self.claim_pos              # ... and its poll starts after that line
        asyncio.run(blind.refresh())
        self.assertIn("not the lease holder", "; ".join(blind.log.rejected.values()))
        self.assertEqual(blind.log.last_pos, holder.log.last_pos)
        self.assertNotEqual(blind.state_hash(), holder.state_hash())

    def test_a_resync_from_the_pin_agrees_again(self):
        """The repair: load the holder's fold from the pin, then carry on with the ops after it."""
        holder = self.claim()
        asyncio.run(holder.write("topic/55", {"topic_id": 55, "status": "open"}))
        asyncio.run(holder.refresh())

        blind = self.store("DeepSeek")
        blind.log.ops.clear()
        blind.log._refold()
        blind._seen = self.claim_pos
        asyncio.run(blind.refresh())
        self.assertNotEqual(blind.state_hash(), holder.state_hash())

        # The pin is published now, and it is the holder's fold of both lines: the repair.
        asyncio.run(holder.publish_snapshot(force=True))
        self.assertTrue(asyncio.run(blind.resync()))
        self.assertEqual(blind.state_hash(), holder.state_hash())
        self.assertEqual(blind.log.rejected, {}, "the pin is a fold, not a set of refusals")

    def test_a_divergence_above_the_pin_heals_when_the_pin_moves_past_it(self):
        """A Telegram reader cannot re-read a line it never received, so a refusal *above* the
        pin is only repaired by the holder's next snapshot -- which is why the beat republishes
        every 900 s or 25 ops, and why `/state` names the position it rebuilt at."""
        holder = self.claim()
        asyncio.run(holder.publish_snapshot(force=True))      # covers the claim only
        covered = holder.log.last_pos
        asyncio.run(holder.write("topic/55", {"topic_id": 55, "status": "open"}))
        asyncio.run(holder.refresh())
        after = holder.log.last_pos
        self.assertGreater(after, covered)

        blind = self.store("DeepSeek")
        blind.log.ops.clear()
        blind.log._refold()
        blind._seen = after            # it never received the write either: nothing to re-read
        asyncio.run(blind.resync())
        self.assertEqual(blind.log.base_pos, covered)
        self.assertNotEqual(blind.state_hash(), holder.state_hash(),
                            "the pin stops at the claim; the write is not in this bot's fold")

        asyncio.run(holder.publish_snapshot(force=True))      # the pin now covers the write
        asyncio.run(blind.resync())
        self.assertGreaterEqual(blind.log.base_pos, after)
        self.assertEqual(blind.state_hash(), holder.state_hash())


if __name__ == "__main__":
    unittest.main()
