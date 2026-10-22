"""Round 5.5 item 2 (#161): a turn that is running but says nothing is reported stuck.

No CLI is spawned here: the adapter's pipe pump is driven with fake streams, and the channel
with a fake adapter and a test clock. What these pin: the transport records when the CLI last
said anything, on stdout or stderr; the snapshot says `stuck (silent Nm)` past
``limits.turn_silence_minutes`` and stops saying it when output resumes; an adapter that
cannot see its CLI's output is "silent since the turn started" and nothing more; and a turn
silent past ``limits.turn_silence_kill_minutes`` is stopped once, through the /stop path,
with one line in its topic. Liveness only: none of this starts a turn or touches a WAKE.
"""

import asyncio
import time
import unittest

from swarmly.telegram import snapshot as sn
from swarmly.telegram.adapters.base import SubprocessAdapter
from swarmly.telegram.config import AdapterSettings
from tests.aio import run
from tests.test_snapshot import GROUP, NOW, live_channel


class Pipes:
    """One pipe a test writes to: ``read`` returns what is queued, then EOF."""

    def __init__(self):
        self._queue = asyncio.Queue()

    async def read(self, n: int = 65536) -> bytes:
        return await self._queue.get()

    def write(self, chunk: bytes) -> None:
        self._queue.put_nowait(chunk)

    def close(self) -> None:
        self.write(b"")


class PipeProcess:
    """A process whose pipes the test drives, so a turn can be watched while it runs."""

    def __init__(self, pid: int = 4242):
        self.stdout, self.stderr = Pipes(), Pipes()
        self.returncode = 0
        self.pid = pid
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code
        self.stdout.close()
        self.stderr.close()
        self._done.set()


class CannedProcess:
    """A process with no readable pipes: what a test runner injects."""

    def __init__(self, out: bytes = b"done\n", err: bytes = b"", returncode: int = 0):
        self._out, self._err, self.returncode = out, err, returncode
        self.pid = 4243
        self.communicated = 0

    async def communicate(self):
        self.communicated += 1
        return self._out, self._err


def adapter_with(process) -> SubprocessAdapter:
    async def runner(argv, cwd, env, timeout):
        return process

    return SubprocessAdapter("TestBot",
                             AdapterSettings(type="claude", command="agent {prompt}",
                                             timeout_seconds=10),
                             runner=runner)


class TestTheOutputClock(unittest.TestCase):
    """The transport records when the CLI last wrote, not only when it exited."""

    def test_no_run_means_no_clock(self):
        self.assertIsNone(adapter_with(CannedProcess()).last_output_at())

    def test_output_mid_turn_moves_the_clock(self):
        async def scenario():
            process = PipeProcess()
            adapter = adapter_with(process)
            task = asyncio.ensure_future(adapter._run(["agent", "hi"]))
            await asyncio.sleep(0.01)
            started = adapter.last_output_at()
            self.assertIsNotNone(started)          # the run itself is progress
            await asyncio.sleep(0.02)
            process.stdout.write(b"reading the repo\n")
            await asyncio.sleep(0.02)
            said = adapter.last_output_at()
            self.assertGreater(said, started)      # bytes moved the clock, mid-turn
            await asyncio.sleep(0.02)
            process.stderr.write(b"a warning\n")
            await asyncio.sleep(0.02)
            self.assertGreater(adapter.last_output_at(), said)
            process.finish()
            _process, stdout, stderr = await task
            return stdout, stderr

        stdout, stderr = run(scenario())
        self.assertEqual(stdout, b"reading the repo\n")
        self.assertEqual(stderr, b"a warning\n")

    def test_a_runner_without_pipes_still_reports_the_run_start(self):
        process = CannedProcess(out=b"all good\n")
        adapter = adapter_with(process)

        async def scenario():
            return await adapter._run(["agent", "hi"])

        _process, stdout, _stderr = run(scenario())
        self.assertEqual(stdout, b"all good\n")
        self.assertEqual(process.communicated, 1)      # the fallback path is the old one
        self.assertIsNotNone(adapter.last_output_at())


class TestTheStuckReport(unittest.TestCase):
    """`stuck (silent Nm)` past limits.turn_silence_minutes, and only while it holds."""

    def busy(self, channel, seconds):
        channel._busy_since[(GROUP, 307)] = channel._clock() - seconds
        return channel

    def test_a_silent_turn_is_stuck_and_says_for_how_long(self):
        channel = self.busy(live_channel(), 600)
        channel.adapter.last_output_at = lambda: time.monotonic() - 420
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.state, sn.STUCK)
        self.assertAlmostEqual(snap.silent, 420, delta=2)
        self.assertIn("stuck (silent 7m)", snap.line(NOW))

    def test_output_resuming_clears_stuck(self):
        channel = self.busy(live_channel(), 600)
        channel.adapter.last_output_at = lambda: time.monotonic()
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.state, sn.BUSY)
        self.assertLess(snap.silent, 2)
        self.assertNotIn("silent", snap.line(NOW))

    def test_an_adapter_that_cannot_say_is_silent_since_the_turn_started(self):
        channel = self.busy(live_channel(), 900)      # the echo adapter has no output clock
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.state, sn.STUCK)
        self.assertEqual(snap.silent, 900)
        self.assertIn("stuck (silent 15m)", snap.line(NOW))

    def test_a_turn_under_the_limit_is_busy(self):
        channel = self.busy(live_channel(), 120)
        self.assertEqual(channel.status_snapshot(NOW).state, sn.BUSY)

    def test_a_bot_with_the_report_off_never_says_stuck(self):
        channel = self.busy(live_channel(turn_silence_minutes=0), 1800)
        channel.config.adapter.timeout_seconds = 7200      # the hard timeout is not this test
        snap = channel.status_snapshot(NOW)
        self.assertEqual(snap.state, sn.BUSY)
        self.assertEqual(snap.silent, 1800)           # still measured, just not judged

    def test_the_record_round_trips_with_the_silence(self):
        snap = sn.Snapshot(alias="Hermes", state=sn.STUCK, at=NOW, silent=90.0)
        self.assertEqual(sn.Snapshot.from_dict(snap.to_dict()).silent, 90.0)

    def test_the_pulse_carries_it(self):
        channel = self.busy(live_channel(), 600)
        channel.adapter.last_output_at = lambda: time.monotonic() - 420
        status = channel.own_pulse(NOW).status
        self.assertEqual(status["state"], sn.STUCK)
        self.assertAlmostEqual(status["silent"], 420, delta=2)


class TestTheKillPath(unittest.TestCase):
    """A turn silent past the kill limit stops through /stop, and the topic is told once."""

    def channel(self, kill_minutes, silent_for):
        channel = live_channel(turn_silence_kill_minutes=kill_minutes)
        channel.adapter.last_output_at = lambda: time.monotonic() - silent_for
        return channel

    async def kill(self, channel, silent_for=400, stopping=False):
        task = asyncio.ensure_future(asyncio.sleep(3600))
        channel._turns.add(task)
        channel._turn_keys[task] = (GROUP, 307)
        channel._busy_since[(GROUP, 307)] = channel._clock() - silent_for
        if stopping:
            channel._stopping_turns.add(task)
        done = await channel.check_stuck_turns()
        # Read the cancellation here: once this coroutine returns, the test harness may
        # cancel whatever the scenario left pending, which would hide the answer.
        cancelled = task.cancelled() or task.cancelling()
        channel._turns.discard(task)
        task.cancel()
        return cancelled, done

    def test_the_default_never_kills(self):
        channel = self.channel(0.0, 400)
        cancelled, done = run(self.kill(channel))
        self.assertEqual(done, [])
        self.assertFalse(cancelled)
        self.assertEqual(channel._test_sender.sent, [])

    def test_a_turn_silent_past_the_limit_is_stopped_and_the_topic_told_once(self):
        channel = self.channel(1.0, 400)
        cancelled, done = run(self.kill(channel))
        self.assertEqual(done, ["killed a turn silent for 6m"])
        self.assertTrue(cancelled)
        self.assertEqual(channel.stats.stopped, 1)
        self.assertEqual(len(channel._test_sender.sent), 1)
        self.assertIn("silent for 6m", channel._test_sender.last)
        self.assertIn("stopped it", channel._test_sender.last)

    def test_a_turn_that_is_not_yet_silent_enough_is_left_alone(self):
        channel = self.channel(10.0, 400)
        cancelled, done = run(self.kill(channel))
        self.assertEqual(done, [])
        self.assertFalse(cancelled)
        self.assertEqual(channel._test_sender.sent, [])

    def test_a_turn_already_being_stopped_is_not_stopped_twice(self):
        channel = self.channel(1.0, 400)
        cancelled, done = run(self.kill(channel, stopping=True))
        self.assertEqual(done, [])
        self.assertFalse(channel._test_sender.sent)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
