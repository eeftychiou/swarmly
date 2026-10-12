"""
Integration tests for Fast-Plane WebSocket Gateway Server, Authentication, and Client SDK.
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.models import A2AMessage, MessageIntent, MessagePriority
from swarmly.crypto import KeyPair
from swarmly.bus import DurableBus
from swarmly.server import A2AGatewayServer
from swarmly.client import A2AClient


class TestServerClientIntegration(unittest.IsolatedAsyncioTestCase):

    # Agent ids whose ``A2A_KEY_<AGENT>`` env vars must not shadow the keypairs built below.
    # ``DurableBus.get_agent_key()`` returns ``os.environ["A2A_KEY_<AGENT>"]`` *before* it
    # consults the config, so an ambient value (e.g. leaked into ``os.environ`` by another test
    # module) overrides the key this test's own config declares and ``connect()`` fails auth.
    SHADOWING_AGENT_IDS = ("antigravity", "hermes", "rogue_agent", "revoked_bot")

    async def asyncSetUp(self):
        # Removing in setup is what matters: restoring the leaked value in teardown alone would
        # hand the polluted key straight back to the test. The cleanups reinstate the exact
        # pre-test state, so this test never permanently mutates the process environment.
        for agent_id in self.SHADOWING_AGENT_IDS:
            env_key = f"A2A_KEY_{agent_id.upper()}"
            previous = os.environ.pop(env_key, None)
            if previous is None:
                # Never re-introduce a key the process did not already have.
                self.addCleanup(os.environ.pop, env_key, None)
            else:
                self.addCleanup(os.environ.__setitem__, env_key, previous)

        self.temp_dir = TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        
        # Setup keys for test agents
        self.kp_anti = KeyPair.generate_hmac("antigravity")
        self.kp_hermes = KeyPair.generate_hmac("hermes")
        self.kp_rogue = KeyPair.generate_hmac("rogue_agent")
        self.kp_revoked = KeyPair.generate_hmac("revoked_bot")

        # HMAC secrets live in the environment; the config names the variable (P0-2).
        os.environ["A2A_KEY_ANTIGRAVITY"] = self.kp_anti.private_key
        os.environ["A2A_KEY_HERMES"] = self.kp_hermes.private_key
        os.environ["A2A_KEY_REVOKED_BOT"] = self.kp_revoked.private_key

        # Create test config
        self.config_data = {
            "protocol_version": "a2a/v1.0",
            "topology": "designated_master",
            "coordination": {"primary_master": "antigravity", "failover_master": "hermes"},
            "authorized_agents": {
                "antigravity": {
                    "model_family": "gemini",
                    "framework": "antigravity_ide",
                    "public_key_or_secret_name": "A2A_KEY_ANTIGRAVITY",
                    "capabilities": ["master_coordinator"],
                    "role_scope": "master"
                },
                "hermes": {
                    "model_family": "hermes",
                    "framework": "cli_bus",
                    "public_key_or_secret_name": "A2A_KEY_HERMES",
                    "capabilities": ["worker"],
                    "role_scope": "worker"
                },
                "revoked_bot": {
                    "model_family": "custom",
                    "framework": "custom",
                    "public_key_or_secret_name": "A2A_KEY_REVOKED_BOT",
                    "capabilities": ["worker"],
                    "role_scope": "worker"
                }
            },
            "revocation_list": ["revoked_bot"],
            "fast_plane": {"enabled": True, "timeout_ms": 1000}
        }

        self.config_path = self.root_path / "a2a_config.json"
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config_data, f)

        self.bus = DurableBus(root_dir=self.root_path, config_path=self.config_path)

        # Port for test server
        self.port = 18765
        self.server = A2AGatewayServer(host="127.0.0.1", port=self.port, bus=self.bus)
        self.server_task = asyncio.create_task(self.server.start())
        await asyncio.sleep(0.05)  # Allow server to bind

    async def asyncTearDown(self):
        await self.server.stop()
        self.server_task.cancel()
        self.temp_dir.cleanup()

    async def test_successful_client_authentication(self):
        client = A2AClient(
            agent_id="antigravity",
            private_key=self.kp_anti.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )
        connected = await client.connect()
        self.assertTrue(connected)
        self.assertTrue(client.is_connected)
        await client.disconnect()

    async def test_repeated_connect_replaces_previous_session(self):
        client = A2AClient(
            agent_id="antigravity",
            private_key=self.kp_anti.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )

        self.assertTrue(await client.connect())
        first_writer = client.writer
        first_listener = client._listener_task

        self.assertTrue(await client.connect())

        self.assertTrue(first_writer.is_closing())
        self.assertTrue(first_listener.done())
        self.assertTrue(client.is_connected)
        self.assertIsNot(client.writer, first_writer)
        self.assertIsNot(client._listener_task, first_listener)
        await client.disconnect()

    async def test_failed_reconnect_cleans_previous_session(self):
        client = A2AClient(
            agent_id="antigravity",
            private_key=self.kp_anti.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )

        self.assertTrue(await client.connect())
        first_writer = client.writer
        await self.server.stop()

        self.assertFalse(await client.connect(timeout=0.1))

        self.assertTrue(first_writer.is_closing())
        self.assertFalse(client.is_connected)
        self.assertIsNone(client.reader)
        self.assertIsNone(client.writer)
        self.assertIsNone(client._listener_task)

    async def test_rogue_unregistered_agent_rejected(self):
        client = A2AClient(
            agent_id="rogue_agent",
            private_key=self.kp_rogue.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )
        connected = await client.connect()
        self.assertFalse(connected)
        self.assertFalse(client.is_connected)

    async def test_revoked_agent_rejected(self):
        client = A2AClient(
            agent_id="revoked_bot",
            private_key=self.kp_revoked.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )
        connected = await client.connect()
        self.assertFalse(connected)
        self.assertFalse(client.is_connected)

    async def test_realtime_message_broadcast(self):
        client_anti = A2AClient(
            agent_id="antigravity",
            private_key=self.kp_anti.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )
        client_hermes = A2AClient(
            agent_id="hermes",
            private_key=self.kp_hermes.private_key,
            host="127.0.0.1",
            port=self.port,
            bus=self.bus
        )

        await client_anti.connect()
        await client_hermes.connect()

        received_messages = []

        def hermes_callback(msg: A2AMessage):
            received_messages.append(msg)

        client_hermes.on_message(hermes_callback)

        # Send from Antigravity to Hermes
        payload = {"topic": "Review PR #47", "status": "READY"}
        await client_anti.send_message(
            intent=MessageIntent.PR_REVIEW_REQUEST,
            payload=payload,
            recipient_id="hermes",
            priority=MessagePriority.HIGH
        )

        # Allow event loop dispatch
        for _ in range(60):
            if received_messages:
                break
            await asyncio.sleep(0.05)

        self.assertEqual(len(received_messages), 1)
        delivered = received_messages[0]
        self.assertEqual(delivered.sender.agent_id, "antigravity")
        self.assertEqual(delivered.recipient.agent_id, "hermes")
        self.assertEqual(delivered.intent, MessageIntent.PR_REVIEW_REQUEST)
        self.assertEqual(delivered.payload["topic"], "Review PR #47")

        # Verify message was also dual-written to Durable Bus
        inbox_msgs = self.bus.read_inbox(verify_signatures=True)
        self.assertEqual(len(inbox_msgs), 1)
        msg_inbox, is_valid, status = inbox_msgs[0]
        self.assertTrue(is_valid)
        self.assertEqual(msg_inbox.sender.agent_id, "antigravity")

        await client_anti.disconnect()
        await client_hermes.disconnect()


if __name__ == "__main__":
    unittest.main()
