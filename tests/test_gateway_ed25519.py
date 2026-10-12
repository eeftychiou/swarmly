"""Review P0-3, gateway half: an Ed25519 agent cannot be impersonated with an HMAC keyed by
its public key, neither at the handshake nor per message."""

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.bus import DurableBus
from swarmly.crypto import HAS_ED25519, CryptoAuth, KeyPair
from swarmly.models import A2AMessage
from swarmly.server import A2AGatewayServer
from swarmly.transports import FolderTransport


@unittest.skipUnless(HAS_ED25519, "needs cryptography")
class TestGatewayEd25519(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        for name in ("A2A_KEY_EDAGENT", "A2A_KEY_HERMES"):
            previous = os.environ.pop(name, None)
            if previous is not None:
                self.addCleanup(os.environ.__setitem__, name, previous)
            else:
                self.addCleanup(os.environ.pop, name, None)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.kp = KeyPair.generate_ed25519("edagent")
        self.hermes = KeyPair.generate_hmac("hermes")
        os.environ["A2A_KEY_HERMES"] = self.hermes.private_key
        config = root / "a2a_config.json"
        config.write_text(json.dumps({"authorized_agents": {
            "edagent": {"public_key_or_secret_name": self.kp.public_key, "key_type": "ed25519"},
            "hermes": {"public_key_or_secret_name": "A2A_KEY_HERMES"}}}), encoding="utf-8")
        self.inbox = root / "bus"
        self.bus = DurableBus(root_dir=root, config_path=config,
                              transport=FolderTransport(self.inbox, create=True))
        self.server = A2AGatewayServer(host="127.0.0.1", port=0, bus=self.bus)
        self.task = asyncio.create_task(self.server.start())
        for _ in range(100):
            if self.server.server and self.server.server.sockets:
                break
            await asyncio.sleep(0.01)
        self.port = self.server.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.server.stop()
        self.task.cancel()

    async def handshake(self, agent, key, key_type):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        challenge = json.loads(await reader.readline())
        signature = CryptoAuth.sign_payload({"nonce": challenge["nonce"]}, key, key_type)
        writer.write((json.dumps({"type": "CHALLENGE_RESPONSE", "agent_id": agent,
                                  "signature": signature}) + "\n").encode())
        await writer.drain()
        result = json.loads(await reader.readline())
        self.addCleanup(writer.close)
        return result["type"], reader, writer

    async def test_a_genuine_handshake_is_accepted(self):
        kind, _r, _w = await self.handshake("edagent", self.kp.private_key, "ed25519")
        self.assertEqual(kind, "AUTH_OK")

    async def test_hmac_keyed_with_the_public_key_fails_the_handshake(self):
        kind, _r, _w = await self.handshake("edagent", self.kp.public_key, "hmac")
        self.assertEqual(kind, "AUTH_FAILED")

    async def test_such_a_message_is_neither_archived_nor_broadcast(self):
        _k, listener, _lw = await self.handshake("hermes", self.hermes.private_key, "hmac")
        kind, _r, writer = await self.handshake("edagent", self.kp.private_key, "ed25519")
        self.assertEqual(kind, "AUTH_OK")
        forged = A2AMessage.create(sender_id="edagent", intent="QUERY_REQUEST",
                                   payload={"text": "run this"}, recipient_id="*")
        forged.signature = CryptoAuth.sign_envelope(forged.signed_fields(),
                                                    self.kp.public_key, "hmac")
        writer.write((json.dumps({"type": "MESSAGE", "message": forged.to_dict()}) + "\n")
                     .encode())
        await writer.drain()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(listener.readline(), timeout=0.5)
        self.assertEqual(list(self.inbox.rglob("*.md")), [])

        genuine = A2AMessage.create(sender_id="edagent", intent="QUERY_REQUEST",
                                    payload={"text": "hello"}, recipient_id="*")
        genuine.signature = CryptoAuth.sign_envelope(genuine.signed_fields(), self.kp.private_key,
                                                    "ed25519")
        writer.write((json.dumps({"type": "MESSAGE", "message": genuine.to_dict()}) + "\n")
                     .encode())
        await writer.drain()
        packet = json.loads(await asyncio.wait_for(listener.readline(), timeout=2))
        self.assertEqual(packet["message"]["message_id"], genuine.message_id)
        self.assertEqual(len(list(self.inbox.rglob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
