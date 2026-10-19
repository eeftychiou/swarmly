"""Tests for orphaned child CLIs (issue #68).

A transport that is *killed* leaves its CLI children running. Each child's pid is recorded
with the start time of the child, and of the transport that spawned it; on start the new
transport ends the ones whose parent is dead. Every OS call is faked here -- ``identity``,
``alive`` and ``kill`` -- so these tests pin the decision (which pid may be killed) and never
touch a real process.

Run: python -m unittest tests.test_children -v
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.children import Child, ChildRegistry, reap_orphans
from swarmly.telegram.config import AgentConfig
from tests.test_telegram_channel import GROUP, make_config

DEAD = 4242          # the transport that died with children running
LIVE = 4343          # another transport, still running
CHILD = 5151
OTHER_CHILD = 5252


class FakeProcessTable:
    """The OS, faked: which pids exist and when each started."""

    def __init__(self, processes: dict) -> None:
        self.processes = dict(processes)      # pid -> start time
        self.killed = []

    def identity(self, pid: int):
        return self.processes.get(int(pid))

    def alive(self, pid: int) -> bool:
        return int(pid) in self.processes

    def kill(self, pid: int) -> None:
        self.killed.append(int(pid))
        self.processes.pop(int(pid), None)


def record(path: Path, *, pid: int, transport_pid: int, identity: str,
           transport_identity: str, command: str = "agy -p {prompt}") -> None:
    registry = ChildRegistry(path)
    children = registry.load()
    children.append(Child(pid=pid, transport_pid=transport_pid,
                          transport_identity=transport_identity, identity=identity,
                          command=command))
    registry.save(children)


class ReapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "children-hermes.json"

    def reap(self, table: FakeProcessTable):
        return reap_orphans(self.path, identity=table.identity, alive=table.alive,
                            kill=table.kill)

    def test_an_orphan_of_a_dead_transport_is_ended(self):
        record(self.path, pid=CHILD, transport_pid=DEAD, identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({CHILD: "t100"})        # the old transport is gone
        self.assertEqual(self.reap(table), [CHILD])
        self.assertEqual(table.killed, [CHILD])

    def test_a_live_transports_children_are_left_alone(self):
        record(self.path, pid=CHILD, transport_pid=LIVE, identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({LIVE: "t1", CHILD: "t100"})
        self.assertEqual(self.reap(table), [])
        self.assertEqual(table.killed, [])
        self.assertEqual([c.pid for c in ChildRegistry(self.path).load()], [CHILD])

    def test_a_reused_pid_is_left_alone(self):
        """The pid exists but belongs to a different process now: killing it would be wrong."""
        record(self.path, pid=CHILD, transport_pid=DEAD, identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({CHILD: "t999"})        # same pid, another start time
        self.assertEqual(self.reap(table), [])
        self.assertEqual(table.killed, [])

    def test_a_reused_transport_pid_does_not_protect_an_orphan(self):
        record(self.path, pid=CHILD, transport_pid=DEAD, identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({DEAD: "t999", CHILD: "t100"})
        self.assertEqual(self.reap(table), [CHILD])

    def test_a_child_that_is_already_gone_has_its_record_dropped(self):
        record(self.path, pid=CHILD, transport_pid=DEAD, identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({})
        self.assertEqual(self.reap(table), [])
        self.assertEqual(ChildRegistry(self.path).load(), [])

    def test_an_unreadable_identity_is_never_killed(self):
        record(self.path, pid=CHILD, transport_pid=DEAD, identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({CHILD: None})
        self.assertEqual(self.reap(table), [])
        self.assertEqual(table.killed, [])

    def test_this_transports_own_child_is_left_alone(self):
        """A start against a registry written mid-turn must not kill the turn's CLI."""
        import os
        record(self.path, pid=CHILD, transport_pid=os.getpid(), identity="t100",
               transport_identity="t1")
        table = FakeProcessTable({os.getpid(): "t1", CHILD: "t100"})
        self.assertEqual(self.reap(table), [])
        self.assertEqual(table.killed, [])

    def test_one_dead_record_does_not_stop_the_others(self):
        record(self.path, pid=CHILD, transport_pid=DEAD, identity="t100",
               transport_identity="t1")
        record(self.path, pid=OTHER_CHILD, transport_pid=LIVE, identity="t200",
               transport_identity="t2")
        table = FakeProcessTable({LIVE: "t2", OTHER_CHILD: "t200", CHILD: "t100"})
        self.assertEqual(self.reap(table), [CHILD])
        self.assertEqual([c.pid for c in ChildRegistry(self.path).load()], [OTHER_CHILD])


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "children-hermes.json"

    def test_a_child_is_recorded_with_both_start_times_and_dropped_when_it_exits(self):
        registry = ChildRegistry(self.path)
        registry.add(CHILD, "agy -p hi", identity=lambda pid: f"t{pid}")
        stored = registry.load()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].pid, CHILD)
        self.assertEqual(stored[0].identity, f"t{CHILD}")
        self.assertEqual(stored[0].transport_identity, f"t{__import__('os').getpid()}")
        self.assertEqual(stored[0].command, "agy -p hi")
        registry.remove(CHILD)
        self.assertEqual(registry.load(), [])

    def test_removing_an_unknown_pid_is_quiet(self):
        ChildRegistry(self.path).remove(1234)            # no file yet: no error
        self.assertFalse(self.path.exists())

    def test_a_corrupt_registry_reads_as_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(ChildRegistry(self.path).load(), [])

    def test_a_process_without_a_pid_is_quietly_not_recorded(self):
        """Review of PR #77: an injected runner or a failed spawn has no pid; no traceback."""
        registry = ChildRegistry(self.path)
        for pid in (None, "5151", True, 0, -1):
            registry.add(pid, "agy", identity=lambda value: "t1")
            registry.remove(pid)
        self.assertEqual(registry.load(), [])
        self.assertFalse(self.path.exists())

    def test_a_registry_without_a_path_keeps_nothing(self):
        registry = ChildRegistry(None)
        registry.add(CHILD, "agy", identity=lambda pid: "t1")
        self.assertEqual(registry.load(), [])


class StubProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = 0

    async def communicate(self):
        return b"hello", b""


class AdapterTests(unittest.TestCase):
    """The adapter records the CLI it starts and drops the pid when the run ends."""

    def settings(self, tmp: str):
        from swarmly.telegram.config import AdapterSettings
        return AdapterSettings(type="codex", cwd=tmp, command="codex exec {prompt}")

    def adapter(self, tmp: str, process: StubProcess):
        from swarmly.telegram.adapters.base import SubprocessAdapter

        async def runner(argv, cwd, env, timeout):
            return process

        adapter = SubprocessAdapter("Hermes", self.settings(tmp), runner=runner)
        adapter.use_child_registry(Path(tmp) / "children-hermes.json")
        return adapter

    def test_a_run_is_recorded_and_then_forgotten(self):
        from swarmly.telegram.adapters.base import IncomingMessage
        with TemporaryDirectory() as tmp:
            adapter = self.adapter(tmp, StubProcess(CHILD))
            seen = []
            original = adapter.children.add

            def add(pid, command="", identity=None):
                seen.append((pid, command))
                original(pid, command, identity)

            adapter.children.add = add
            asyncio.run(adapter.respond(IncomingMessage(text="hello", author_alias="Alice")))
            self.assertEqual(seen[0][0], CHILD)
            self.assertIn("codex", seen[0][1])
            self.assertEqual(adapter.children.load(), [])      # the run ended: no record


class ChannelStartTests(unittest.TestCase):
    """The transport reaps what a dead one left before it starts any turn."""

    def test_start_reaps_the_recorded_orphans(self):
        with TemporaryDirectory() as tmp:
            config = make_config(adapter={"type": "echo"})
            config.source_path = Path(tmp) / "config.yaml"
            config.source_path.write_text("agent: {}", encoding="utf-8")
            from swarmly.telegram.channel import AgentChannel
            channel = AgentChannel(config, adapter=None)
            # The path the channel would use, written as a dead transport left it.
            from swarmly.telegram.channel import _state_file
            path = _state_file(config, "children")
            self.assertIsNotNone(path)
            record(path, pid=CHILD, transport_pid=DEAD, identity="t100",
                   transport_identity="t1")
            table = FakeProcessTable({CHILD: "t100"})
            # reap with the fake OS through the same paths the channel uses
            from swarmly.telegram import children as children_module
            original = (children_module.process_identity, children_module.pid_alive,
                        children_module.kill_pid)
            children_module.process_identity = table.identity
            children_module.pid_alive = table.alive
            children_module.kill_pid = table.kill
            try:
                killed = channel._reap_orphaned_children()
            finally:
                (children_module.process_identity, children_module.pid_alive,
                 children_module.kill_pid) = original
            self.assertEqual(killed, [CHILD])
            self.assertEqual(table.killed, [CHILD])


if __name__ == "__main__":
    unittest.main()
