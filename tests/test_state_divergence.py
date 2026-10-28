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
from swarmly.state.log import Op
from swarmly.state.store import MemoryLog, MemoryStore
from swarmly.telegram.state_link import StateLink

from tests.test_registry_shadow import Clock
from tests.test_state_link import link_for
from tests.test_state_store import FakeGroup


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


class BeatResyncTests(unittest.TestCase):
    """The wiring the store tests cannot see: a beat that sees a divergence rebuilds (#477).

    `resync()` can be right and still never run -- the repair is only worth anything if the
    beat calls it, and only safe if it cannot stampede: a peer stays disagrees until the pin
    moves past the divergence, so every beat inside the cooldown would rebuild again.
    """

    def setUp(self):
        self.clock = Clock()
        self.group = FakeGroup(self.clock)
        self.holder = link_for(self.group, "Claudebot", self.clock, coordinator=True)
        self.mine = link_for(self.group, "DeepSeek", self.clock)
        asyncio.run(self.holder.store.start())
        asyncio.run(self.mine.store.start())

        async def pin():
            await self.holder.store.claim_lease(600)
            self.clock.t += 60          # a snapshot covers ops that have settled (15 s)
            await self.holder.store.publish_snapshot(force=True)
        asyncio.run(pin())
        self.assertGreater(self.holder.store.snapshot_pos, 0, "the pin is a real fold")

    def peer_disagrees(self):
        """A peer's beat folded at this bot's position with another hash: the live shape."""
        log = self.mine.store.log
        pos = log.last_pos + 1
        log.add(Op(pos=pos, key=K.probe("Codex", "beat"), base=0, term=0, writer="codex",
                   value={"at": round(self.clock()), "rev": "abc123", "pos": pos,
                          "hash": "000000000000"}, at=self.clock()))
        self.assertIn("DIFFERS", "; ".join(self.mine.agreement()))

    def test_a_beat_that_sees_a_divergence_rebuilds_from_the_pin(self):
        """DISCRIMINATING: drop the call from `beat()` and this beat says nothing of it."""
        self.peer_disagrees()
        said = asyncio.run(self.mine.beat())
        self.assertIn("diverged: rebuilt from the pin at p", said)

    def test_the_rebuild_is_shown_in_state_as_well_as_logged(self):
        """The deploy is verified from `/state`: a receipt only in the log cannot be read."""
        self.peer_disagrees()
        asyncio.run(self.mine.beat())
        text = self.mine.describe()
        self.assertIn("last resync at ", text)
        self.assertIn("diverged: rebuilt from the pin at p", text)

    def test_a_second_divergence_inside_the_cooldown_waits_and_then_rebuilds_again(self):
        """DISCRIMINATING: without the 600 s guard the second beat rebuilds too.

        A swarm beat once a minute (`state.heartbeat_minutes: 1`) reaches the check every
        minute, and the peer stays DIFFERS until the pin moves past the divergence -- so
        without the guard every beat would rebuild and re-read the pin.
        """
        self.mine.beat_seconds = 60.0
        self.peer_disagrees()
        self.assertIn("diverged: rebuilt", asyncio.run(self.mine.beat()))

        self.clock.t += 60                       # a minute later: the peer still disagrees
        self.peer_disagrees()
        said = asyncio.run(self.mine.beat())
        self.assertIn("a resync is cooling down", said)
        self.assertNotIn("rebuilt", said)

        self.clock.t += StateLink.RESYNC_SECONDS  # past the cooldown: it heals again
        self.peer_disagrees()
        self.assertIn("diverged: rebuilt from the pin at p", asyncio.run(self.mine.beat()))


if __name__ == "__main__":
    unittest.main()
