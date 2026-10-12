"""
Fast-plane gateway for Swarmly (experimental).

A raw TCP server speaking newline-delimited JSON; it is not WebSocket and not HTTP. Each
connection proves its agent identity by signing a challenge nonce with its registered key,
then messages whose payload signature verifies are relayed to their recipients and archived
on the durable bus.

The connection is plaintext and the client does not authenticate the server, so bind it to
127.0.0.1 (the default) and reach it from elsewhere only through a tunnel or a TLS proxy.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from swarmly.models import A2AMessage, bus_copy_problem, header_problem
from swarmly.crypto import CryptoAuth
from swarmly.bus import DurableBus

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [A2A-Server] %(message)s")
logger = logging.getLogger("a2a_server")


#: An authenticated connection that sends nothing for this long is closed. Clients send a
#: PING well within it (``A2AClient.keepalive_seconds``).
IDLE_TIMEOUT_SECONDS = 300.0

#: Connections open at once, authenticated or not; further ones are refused.
MAX_CONNECTIONS = 64


class A2AGatewayServer:
    """Async gateway server: challenge-response login, then relayed JSON lines over TCP."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        config_path: Optional[Path] = None,
        bus: Optional[DurableBus] = None,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        max_connections: int = MAX_CONNECTIONS,
    ):
        self.host = host
        self.port = port
        self.idle_timeout = idle_timeout
        self.max_connections = max_connections
        self._open_connections = 0
        self.crypto = CryptoAuth(nonce_ttl_seconds=15)
        self.bus = bus or DurableBus(config_path=config_path)
        self.config = self.bus.config

        # Active connected authenticated WebSocket clients: {agent_id: [(writer, lock), ...]}
        self.active_clients: Dict[str, List[Tuple[asyncio.StreamWriter, asyncio.Lock]]] = {}
        self.server: Optional[asyncio.Server] = None
        self._running = False

    def get_public_key(self, agent_id: str) -> Optional[str]:
        return self.bus.get_agent_key(agent_id)

    def get_verification_key(self, agent_id: str) -> Optional[Tuple[str, str]]:
        """``(key, key_type)`` for ``agent_id``: the algorithm is the registered one."""
        return self.bus.verification_key(agent_id)

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle one TCP connection: challenge-response handshake, then its messages."""
        peer = writer.get_extra_info("peername")
        authenticated_agent_id = None
        if self._open_connections >= self.max_connections:
            logger.warning("Connection from %s refused: %d connections already open",
                           peer, self._open_connections)
            try:
                writer.write(json.dumps({"type": "AUTH_FAILED", "error": "Server busy"})
                             .encode("utf-8") + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return
        self._open_connections += 1
        logger.info(f"New incoming connection from {peer}")

        try:
            # 1. Generate challenge nonce
            token, nonce_str = self.crypto.create_challenge_nonce()
            challenge_msg = {
                "type": "CHALLENGE",
                "nonce": nonce_str,
                "timestamp": time.time(),
                "prompt": "Solve challenge by signing nonce with your private key"
            }
            writer.write((json.dumps(challenge_msg) + "\n").encode("utf-8"))
            await writer.drain()

            # 2. Await challenge response with timeout (5s)
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not line:
                logger.warning(f"Connection from {peer} closed before solving challenge")
                writer.close()
                await writer.wait_closed()
                return

            response = json.loads(line.decode("utf-8").strip())
            agent_id = response.get("agent_id")
            signature = response.get("signature")

            # 3. Check revocation
            revocation = self.config.get("revocation_list", [])
            if not agent_id or self.crypto.is_revoked(agent_id, revocation):
                logger.warning(f"Connection rejected: Agent '{agent_id}' is revoked or missing")
                writer.write(json.dumps({"type": "AUTH_FAILED", "error": "Agent revoked or invalid"}).encode("utf-8") + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # 4. Verify signature
            registered = self.get_verification_key(agent_id)
            if not registered:
                logger.warning(f"Connection rejected: Agent '{agent_id}' has no registered public key")
                writer.write(json.dumps({"type": "AUTH_FAILED", "error": "Unregistered agent"}).encode("utf-8") + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            is_valid = self.crypto.verify_challenge_response(nonce_str, signature, *registered)
            if not is_valid:
                logger.warning(f"Connection rejected: Invalid signature from agent '{agent_id}'")
                writer.write(json.dumps({"type": "AUTH_FAILED", "error": "Signature verification failed"}).encode("utf-8") + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # 5. Authenticated successfully
            authenticated_agent_id = agent_id
            conn_entry = (writer, asyncio.Lock())
            self.active_clients.setdefault(agent_id, []).append(conn_entry)
            logger.info(f"Agent '{agent_id}' authenticated successfully from {peer} (active connections: {len(self.active_clients[agent_id])})")
            
            ack_msg = {
                "type": "AUTH_OK",
                "agent_id": agent_id,
                "cluster_master": self.config.get("coordination", {}).get("primary_master", "antigravity"),
                "protocol": "a2a/v1.0"
            }
            writer.write((json.dumps(ack_msg) + "\n").encode("utf-8"))
            await writer.drain()

            # 6. Message receive loop
            while self._running:
                try:
                    data = await asyncio.wait_for(reader.readline(), timeout=self.idle_timeout)
                except asyncio.TimeoutError:
                    logger.info("Agent '%s' idle for %.0fs; closing %s",
                                authenticated_agent_id, self.idle_timeout, peer)
                    break
                if not data:
                    break
                line_str = data.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    payload = json.loads(line_str)
                    await self.process_incoming_packet(authenticated_agent_id, payload, writer)
                except Exception as e:
                    logger.error(f"Error processing packet from {authenticated_agent_id}: {e}")

        except asyncio.TimeoutError:
            logger.warning(f"Connection from {peer} timed out during auth handshake")
        except Exception as e:
            logger.error(f"Connection error with {peer}: {e}")
        finally:
            self._open_connections -= 1
            if authenticated_agent_id and authenticated_agent_id in self.active_clients:
                self.active_clients[authenticated_agent_id] = [
                    c for c in self.active_clients[authenticated_agent_id] if c[0] != writer
                ]
                remaining = len(self.active_clients[authenticated_agent_id])
                if remaining == 0:
                    del self.active_clients[authenticated_agent_id]
                logger.info(f"Agent '{authenticated_agent_id}' disconnected connection {peer} (remaining active: {remaining})")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def process_incoming_packet(self, sender_agent_id: str, packet: Dict, sender_writer: asyncio.StreamWriter):
        """Process an authenticated incoming message packet."""
        pkt_type = packet.get("type", "MESSAGE")
        
        if pkt_type == "PING":
            sender_writer.write(json.dumps({"type": "PONG", "timestamp": time.time()}).encode("utf-8") + b"\n")
            await sender_writer.drain()
            return

        if pkt_type == "MESSAGE":
            msg_dict = packet.get("message")
            if not msg_dict:
                return
            
            msg = A2AMessage.from_dict(msg_dict)
            
            # Verify that sender_id matches authenticated session
            if msg.sender.agent_id != sender_agent_id:
                logger.warning(f"Spoof attempt: {sender_agent_id} tried to send message as {msg.sender.agent_id}")
                return

            # Review P0-7: ids become file names and header lines; refuse odd ones outright
            # rather than archiving (or relaying) them.
            problem = header_problem(msg) or bus_copy_problem(msg)
            if problem:
                logger.warning("Rejected message from %s: %s", sender_agent_id, problem)
                return

            # Verify the signature over the whole envelope (review P1-3), with the same rules
            # as the bus: registered key, no v1 unless the registry allows it, time window.
            valid, status = self.bus.verify_message(msg)
            if not valid:
                logger.warning("Rejected message %s from %s: %s", msg.message_id,
                               sender_agent_id, status)
                return

            # Replay check
            if self.crypto.is_message_replay(msg.message_id):
                logger.warning(f"Rejected replayed message {msg.message_id}")
                return

            # Dual-Write: Archive to Durable Bus (Git) asynchronously
            try:
                self.bus.post_message(msg, git_push=False)
            except Exception as e:
                logger.error(f"Failed to dual-write message {msg.message_id} to durable bus: {e}")

            # Real-time Broadcast: dispatch to recipient or broadcast
            await self.broadcast_message(msg, exclude_agent=sender_agent_id)

    async def broadcast_message(self, message: A2AMessage, exclude_agent: Optional[str] = None):
        """Broadcast an A2A message to authorized subscribers in sub-50ms."""
        packet = {
            "type": "MESSAGE",
            "message": message.to_dict(),
            "timestamp": time.time()
        }
        data_bytes = (json.dumps(packet) + "\n").encode("utf-8")
        target = message.recipient.agent_id
        delivered_count = 0

        for aid, conns in list(self.active_clients.items()):
            if aid == exclude_agent:
                continue
            if target == "*" or target == aid:
                for writer, lock in list(conns):
                    try:
                        async with lock:
                            writer.write(data_bytes)
                            await writer.drain()
                        delivered_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to deliver message {message.message_id} to {aid}: {e}")

        if delivered_count > 0:
            logger.info(f"Broadcasted {message.intent} ({message.message_id}) to {delivered_count} connection(s)")
        else:
            logger.warning(f"Message {message.message_id} (target: {target}) had 0 active listeners")

    async def start(self):
        """Start the gateway server."""
        self._running = True
        self.server = await asyncio.start_server(self.handle_connection, self.host, self.port)
        logger.info(f"Swarmly Gateway running on {self.host}:{self.port}")
        async with self.server:
            await self.server.serve_forever()

    async def stop(self):
        """Stop the gateway server."""
        self._running = False
        for aid, conns in list(self.active_clients.items()):
            for writer, lock in conns:
                try:
                    writer.close()
                except Exception:
                    pass
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=1.0)
            except Exception:
                pass
        logger.info("Swarmly Gateway stopped.")
        
A2AServer = A2AGatewayServer
