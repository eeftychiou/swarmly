"""Review P1-10: the fast-plane gateway listens on localhost by default, closes idle
connections, and caps how many are open at once."""

import asyncio
import inspect
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.bus import DurableBus
from swarmly.client import A2AClient
from swarmly.crypto import KeyPair
from swarmly.server import A2AGatewayServer
from swarmly.transports import FolderTransport


class TestGatewayDefaults(unittest.TestCase):

    def test_it_binds_to_localhost_by_default(self):
        default = inspect.signature(A2AGatewayServer.__init__).parameters["host"].default
        self.assertEqual(default, "127.0.0.1")


class TestGatewayLimits(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        previous = os.environ.pop("A2A_KEY_HERMES", None)
        if previous is not None:
            self.addCleanup(os.environ.__setitem__, "A2A_KEY_HERMES", previous)
        else:
            self.addCleanup(os.environ.pop, "A2A_KEY_HERMES", None)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.hermes = KeyPair.generate_hmac("hermes")
        os.environ["A2A_KEY_HERMES"] = self.hermes.private_key
        config = root / "a2a_config.json"
        config.write_text(json.dumps({"authorized_agents": {
            "hermes": {"public_key_or_secret_name": "A2A_KEY_HERMES"}}}), encoding="utf-8")
        self.bus = DurableBus(root_dir=root, config_path=config,
                              transport=FolderTransport(root / "bus", create=True))

    async def start(self, **limits):
        self.server = A2AGatewayServer(port=0, bus=self.bus, **limits)
        task = asyncio.create_task(self.server.start())
        self.addAsyncCleanup(self.stop, task)
        for _ in range(100):
            if self.server.server and self.server.server.sockets:
                break
            await asyncio.sleep(0.01)
        self.port = self.server.server.sockets[0].getsockname()[1]

    async def stop(self, task):
        await self.server.stop()
        task.cancel()

    def client(self, keepalive):
        client = A2AClient("hermes", self.hermes.private_key, port=self.port, bus=self.bus,
                           keepalive_seconds=keepalive)
        self.addAsyncCleanup(client.disconnect)
        return client

    async def test_an_idle_connection_is_closed(self):
        await self.start(idle_timeout=0.3)
        quiet = self.client(keepalive=30)
        self.assertTrue(await quiet.connect())
        await asyncio.sleep(0.8)
        self.assertFalse(quiet.is_connected)

    async def test_the_client_keepalive_holds_the_connection(self):
        await self.start(idle_timeout=0.3)
        client = self.client(keepalive=0.1)
        self.assertTrue(await client.connect())
        await asyncio.sleep(0.8)
        self.assertTrue(client.is_connected)

    async def test_connections_beyond_the_cap_are_refused(self):
        await self.start(max_connections=1)
        _reader, holder = await asyncio.open_connection("127.0.0.1", self.port)
        self.addCleanup(holder.close)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addCleanup(writer.close)
        await asyncio.sleep(0.05)
        answer = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        self.assertEqual(answer, {"type": "AUTH_FAILED", "error": "Server busy"})

    async def test_a_closed_connection_frees_its_slot(self):
        await self.start(max_connections=1)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        challenge = json.loads(await reader.readline())
        self.assertEqual(challenge["type"], "CHALLENGE")
        writer.close()
        await asyncio.sleep(0.1)
        client = self.client(keepalive=30)
        self.assertTrue(await client.connect())


if __name__ == "__main__":
    unittest.main()
