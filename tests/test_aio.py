"""tests/aio.py: the shared loop finalizes what a test leaves behind, like asyncio.run."""

import asyncio
import threading
import unittest

from tests import aio


class TestSharedLoop(unittest.TestCase):
    def tearDown(self):
        aio.close_shared_loop()

    def test_one_loop_serves_every_run(self):
        first = aio.run(self._current_loop())
        second = aio.run(self._current_loop())
        self.assertIs(first, second)

    def test_a_task_left_running_is_cancelled(self):
        holder = {}

        async def leave_one():
            holder["task"] = asyncio.ensure_future(asyncio.sleep(3600))

        aio.run(leave_one())
        self.assertTrue(holder["task"].cancelled())

    def test_an_unfinished_async_generator_is_finalized(self):
        closed = []

        async def gen():
            try:
                yield 1
                yield 2
            finally:
                closed.append(True)

        async def take_one():
            agen = gen()
            await agen.__anext__()
            holder.append(agen)          # keep it alive past the coroutine

        holder = []
        aio.run(take_one())
        self.assertEqual(closed, [True])

    def test_close_shuts_down_the_executor_and_the_loop(self):
        async def use_executor():
            await asyncio.get_running_loop().run_in_executor(None, lambda: None)

        aio.run(use_executor())
        loop = aio.shared_loop()
        before = {t.name for t in threading.enumerate()}
        self.assertTrue(any("asyncio" in name for name in before))
        aio.close_shared_loop()
        self.assertTrue(loop.is_closed())
        after = {t.name for t in threading.enumerate() if "asyncio" in t.name}
        self.assertEqual(after, set())
        aio.close_shared_loop()          # a second close is harmless

    @staticmethod
    async def _current_loop():
        return asyncio.get_running_loop()


if __name__ == "__main__":
    unittest.main()
