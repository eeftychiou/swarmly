"""
Client library for Swarmly protocol.
Connects to the fast-plane gateway (swarmly/server.py, raw TCP) with automatic fallback to the
durable bus.
"""

from __future__ import annotations
import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from swarmly.models import A2AMessage, MessageIntent, MessagePriority
from swarmly.crypto import CryptoAuth
from swarmly.bus import DurableBus

logger = logging.getLogger("a2a_client")


class A2AClient:
    """Universal client for interacting with the A2A cluster."""

    def __init__(
        self,
        agent_id: str,
        private_key: str,
        key_type: str = "hmac",
        model_family: str = "custom",
        framework: str = "custom",
        host: str = "127.0.0.1",
        port: int = 8765,
        bus: Optional[DurableBus] = None,
        auto_fallback_to_bus: bool = True,
        keepalive_seconds: float = 60.0,
    ):
        self.agent_id = agent_id
        self.private_key = private_key
        self.key_type = key_type
        self.model_family = model_family
        self.framework = framework
        self.host = host
        self.port = port
        self.auto_fallback = auto_fallback_to_bus
        # A PING after this long without traffic keeps the gateway's idle timeout away.
        self.keepalive_seconds = keepalive_seconds

        self.crypto = CryptoAuth()
        self.bus = bus or DurableBus()

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.is_connected = False
        self._listener_task: Optional[asyncio.Task] = None
        self._message_callbacks: list[Callable[[A2AMessage], Any]] = []

    def on_message(self, callback: Callable[[A2AMessage], Any]):
        """Register a callback for incoming messages."""
        self._message_callbacks.append(callback)

    async def connect(self, timeout: float = 3.0) -> bool:
        """Establish connection and complete cryptographic challenge-response authentication."""
        # A reconnect must not orphan the previous socket or listener task.
        if self.reader or self.writer or self._listener_task:
            await self.disconnect()

        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout
            )

            # 1. Receive challenge
            line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            challenge = json.loads(line.decode("utf-8").strip())
            nonce_str = challenge.get("nonce")
            if not nonce_str:
                logger.error("Invalid challenge from server: missing nonce")
                await self.disconnect()
                return False

            # 2. Solve challenge by signing the nonce
            sig = self.crypto.sign_payload({"nonce": nonce_str}, self.private_key, self.key_type)
            response = {
                "type": "CHALLENGE_RESPONSE",
                "agent_id": self.agent_id,
                "signature": sig
            }
            self.writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await self.writer.drain()

            # 3. Receive auth result
            line_ack = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            auth_res = json.loads(line_ack.decode("utf-8").strip())
            if auth_res.get("type") != "AUTH_OK":
                logger.error(f"Auth failed: {auth_res.get('error')}")
                await self.disconnect()
                return False

            self.is_connected = True
            logger.info(f"Agent '{self.agent_id}' connected and authenticated with A2A Gateway")
            
            # Start background listener
            self._listener_task = asyncio.create_task(self._listen_loop())
            return True

        except Exception as e:
            logger.warning(f"Could not connect to Fast-Plane Gateway ({e}). Fast-plane disabled.")
            await self.disconnect()
            return False

    async def _listen_loop(self):
        """Background loop reading broadcast messages."""
        while self.is_connected and self.reader:
            try:
                try:
                    line = await asyncio.wait_for(self.reader.readline(),
                                                  timeout=self.keepalive_seconds)
                except asyncio.TimeoutError:
                    if self.writer is None:
                        break
                    self.writer.write(b'{"type": "PING"}\n')
                    await self.writer.drain()
                    continue
                if not line:
                    break
                packet = json.loads(line.decode("utf-8").strip())
                if packet.get("type") == "MESSAGE":
                    msg_dict = packet.get("message", {})
                    msg = A2AMessage.from_dict(msg_dict)
                    for cb in self._message_callbacks:
                        try:
                            if inspect.iscoroutinefunction(cb):
                                await cb(msg)
                            else:
                                cb(msg)
                        except Exception as e:
                            logger.error(f"Callback error processing message {msg.message_id}: {e}")
            except Exception:
                break
        self.is_connected = False

    async def send_message(
        self,
        intent: Union[MessageIntent, str],
        payload: Dict[str, Any],
        recipient_id: str = "*",
        priority: Union[MessagePriority, str] = MessagePriority.ROUTINE,
        correlation_id: Optional[str] = None,
        timeout_utc: Optional[str] = None
    ) -> A2AMessage:
        """Send an A2A message via Fast-Plane or fallback to Durable Bus."""
        # 1. Create the message and sign the whole envelope (review P1-3)
        msg = A2AMessage.create(
            sender_id=self.agent_id,
            intent=intent,
            payload=payload,
            recipient_id=recipient_id,
            model_family=self.model_family,
            framework=self.framework,
            priority=priority,
            correlation_id=correlation_id,
            timeout_utc=timeout_utc,
        )
        msg.signature = self.crypto.sign_envelope(msg.signed_fields(), self.private_key,
                                                  self.key_type)

        # 2. Try fast plane if connected
        if self.is_connected and self.writer:
            try:
                packet = {
                    "type": "MESSAGE",
                    "message": msg.to_dict()
                }
                self.writer.write((json.dumps(packet) + "\n").encode("utf-8"))
                await self.writer.drain()
                logger.info(f"Sent {msg.intent} ({msg.message_id}) via the fast-plane gateway")
                return msg
            except Exception as e:
                logger.warning(f"Fast-plane write failed ({e}); falling back to Durable Bus")
                self.is_connected = False

        # 3. Fallback to Durable Bus
        if self.auto_fallback:
            self.bus.post_message(msg, private_key=self.private_key, key_type=self.key_type)
            logger.info(f"Saved {msg.intent} ({msg.message_id}) to Durable Bus (Git)")
            return msg
        else:
            raise ConnectionError("Gateway not connected and auto_fallback is disabled")

    async def disconnect(self):
        """Cleanly close connection."""
        self.is_connected = False
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task and listener_task is not asyncio.current_task():
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

        writer = self.writer
        self.writer = None
        self.reader = None
        if writer:
            try:
                writer.close()
                # ProactorEventLoop can wait forever when the peer was already stopped.
                # Disconnect is cleanup, not a reason to hang the caller.
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    close = disconnect
