"""Review P0-1: the bus poller delivers only messages whose signature verifies against a
registered key; unsigned, forged or unregistered ones never reach the adapter."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.bus import DurableBus
from swarmly.crypto import HAS_ED25519, CryptoAuth, KeyPair
from swarmly.models import A2AMessage
from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.channel import AgentChannel, read_transport_messages
from swarmly.telegram.config import ConfigError
from swarmly.telegram.poll import (_deferred_path, _heartbeat_path, _load_deferred,
                               _save_seen, _seen_path, bus_poll_once)
from swarmly.transports import FolderTransport
from tests.test_telegram_channel import FakeSender, make_config

SECRET = "codex-shared-secret"


class RecordingAdapter(EchoAdapter):

    def __init__(self, alias, failures=0):
        super().__init__(alias)
        self.texts = []
        self.failures = failures

    async def respond(self, message):
        self.texts.append(message.text)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("adapter timed out")
        return await super().respond(message)


class _Bus(unittest.TestCase):
    """A real folder bus with a key registry; the channel uses its default reader."""

    agents = {"codex": {"public_key_or_secret_name": "A2A_KEY_CODEX"}}

    def setUp(self):
        baseline = patch("swarmly.telegram.poll._needs_baseline", new=lambda *_args: False)
        baseline.start()                         # these tests are about later cycles
        self.addCleanup(baseline.stop)
        for name in ("A2A_KEY_CODEX", "A2A_KEY_MALLORY"):
            previous = os.environ.pop(name, None)
            if previous is not None:
                self.addCleanup(os.environ.__setitem__, name, previous)
            else:
                self.addCleanup(os.environ.pop, name, None)
        os.environ["A2A_KEY_CODEX"] = SECRET
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "a2a_config.json").write_text(
            json.dumps({"authorized_agents": self.agents, "revocation_list": []}),
            encoding="utf-8")
        self.folder = self.root / "bus"
        self.writer = DurableBus(root_dir=self.root,
                                 transport=FolderTransport(self.folder, create=True))

    def channel(self, failures=0, **bus):
        config = make_config(bus={"repo": str(self.root), "transport": "folder",
                                  "folder": str(self.folder), "poll_minutes": 10, **bus})
        self.adapter = RecordingAdapter("Hermes", failures)
        channel = AgentChannel(config, adapter=self.adapter, sender=FakeSender(),
                               gitbus_pull=lambda _root: None)
        return channel

    def post(self, text, sender="codex", key=None, key_type="hmac", recipient="hermes",
             intent="QUERY_REQUEST"):
        msg = A2AMessage.create(sender_id=sender, intent=intent,
                                payload={"text": text}, recipient_id=recipient)
        if key is not None:
            msg.signature = CryptoAuth.sign_envelope(msg.signed_fields(), key, key_type)
        self.writer.post_message(msg)
        return msg.message_id

    def poll(self, channel):
        return asyncio.run(bus_poll_once(channel))


class TestOnlyVerifiedMessagesArrive(_Bus):

    def test_unsigned_forged_and_unknown_senders_never_reach_the_adapter(self):
        self.post("unsigned: rm -rf")
        self.post("forged: rm -rf", key="guessed-secret")
        self.post("stranger: rm -rf", sender="mallory", key="mallory-secret")
        good = self.post("genuine question", key=SECRET)
        channel = self.channel()
        with self.assertLogs("swarmly.telegram.poll", level="WARNING") as logs:
            outcome = self.poll(channel)
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("genuine question", self.adapter.texts[0])
        self.assertEqual(outcome.delivered, [good])
        self.assertEqual(len(outcome.rejected), 3)
        self.assertTrue(any("not delivered" in line for line in logs.output))

    def test_a_tampered_payload_is_rejected(self):
        message_id = self.post("original", key=SECRET)
        [path] = [p for p in self.folder.rglob("*.md") if message_id in p.name]
        path.write_text(path.read_text(encoding="utf-8").replace("original", "tampered"),
                        encoding="utf-8")
        outcome = self.poll(self.channel())
        self.assertEqual(self.adapter.texts, [])
        self.assertEqual([r["id"] for r in outcome.rejected], [message_id])

    def test_rejected_messages_are_not_marked_seen(self):
        message_id = self.post("from a new agent", sender="mallory", key="mallory-secret")
        self.poll(self.channel())
        self.assertEqual(self.adapter.texts, [])
        # Once the agent's key is registered, the same message is delivered.
        (self.root / "config" / "a2a_config.json").write_text(json.dumps({
            "authorized_agents": {**self.agents,
                                  "mallory": {"public_key_or_secret_name": "A2A_KEY_MALLORY"}}}),
            encoding="utf-8")
        os.environ["A2A_KEY_MALLORY"] = "mallory-secret"
        outcome = self.poll(self.channel())
        self.assertEqual(outcome.delivered, [message_id])

    def test_an_unsigned_freeze_does_not_freeze(self):
        msg = A2AMessage.create(sender_id="codex", intent="STATUS_UPDATE",
                                payload={"text": "[A2A v1.1] STATUS to=Hermes intent=FREEZE"},
                                recipient_id="hermes")
        self.writer.post_message(msg)
        channel = self.channel()
        self.poll(channel)
        self.assertFalse(getattr(channel, "_frozen", False))

    def test_senders_allowlist(self):
        self.post("from codex", key=SECRET)
        outcome = self.poll(self.channel(senders=["claude"]))
        self.assertEqual(self.adapter.texts, [])
        self.assertEqual(outcome.rejected[0]["reason"], "sender not in bus.senders")
        self.poll(self.channel(senders=["Codex"]))
        self.assertEqual(len(self.adapter.texts), 1)

    def test_allow_unsigned_is_an_explicit_opt_out(self):
        self.post("demo bus without keys")
        self.poll(self.channel(allow_unsigned=True))
        self.assertEqual(len(self.adapter.texts), 1)

    def test_a_deferred_entry_from_before_verification_is_dropped(self):
        channel = self.channel()
        path = _deferred_path(self.root, "hermes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"deferred": [
            {"id": "old-1", "text": "captured unverified", "sender": "codex"}]}),
            encoding="utf-8")
        outcome = self.poll(channel)
        self.assertEqual(self.adapter.texts, [])
        self.assertEqual(outcome.rejected[0]["id"], "old-1")


class TestReviewFindings(_Bus):
    """The fix-first findings of the P0-1 review (F1, F2, F4, F5, F7, F8)."""

    def freeze(self, to, recipient, intent="FREEZE"):
        return self.post(f"[A2A v1.1] STATUS to={to} intent={intent}", key=SECRET,
                         recipient=recipient, intent="STATUS_UPDATE")

    def test_the_transport_tick_checks_signatures(self):
        self.post("unsigned: rm -rf")
        self.post("forged: rm -rf", key="guessed-secret")
        self.post("genuine question", key=SECRET)
        channel = self.channel()
        asyncio.run(channel.check_gitbus())
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("genuine question", self.adapter.texts[0])

    def test_a_message_deferred_while_frozen_is_delivered_after_resume(self):
        channel = self.channel()
        self.freeze("Hermes", "hermes")
        self.poll(channel)
        self.assertTrue(channel._frozen)
        question = self.post("asked while frozen", key=SECRET)
        outcome = self.poll(channel)
        self.assertEqual(outcome.deferred, [question])
        self.freeze("Hermes", "hermes", intent="RESUME")
        outcome = self.poll(channel)
        self.assertFalse(channel._frozen)
        self.assertEqual(outcome.rejected, [])
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("asked while frozen", self.adapter.texts[0])

    def test_an_adapter_failure_is_retried_not_rejected(self):
        message_id = self.post("first try fails", key=SECRET)
        outcome = self.poll(self.channel(failures=1))
        self.assertEqual(outcome.rejected, [])
        self.assertIn(message_id, outcome.delivered)
        self.assertEqual(len(self.adapter.texts), 2)

    def test_a_failing_adapter_keeps_the_payload_deferred(self):
        message_id = self.post("adapter keeps failing", key=SECRET)
        outcome = self.poll(self.channel(failures=5))
        self.assertEqual(outcome.rejected, [])
        [entry] = _load_deferred(self.root, "Hermes")
        self.assertEqual(entry["id"], message_id)
        self.assertIn("adapter keeps failing", entry["text"])

    def test_a_freeze_signed_for_another_agent_does_not_freeze_this_one(self):
        self.freeze("Antigravity", "hermes")
        channel = self.channel()
        self.poll(channel)
        self.assertFalse(getattr(channel, "_frozen", False))
        self.assertEqual(self.adapter.texts, [])

    def test_a_freeze_re_addressed_to_everyone_freezes_no_one(self):
        self.freeze("Antigravity", "*")
        self.freeze("Hermes", "*")            # a broadcast header still needs the admin
        channel = self.channel()
        self.poll(channel)
        self.assertFalse(getattr(channel, "_frozen", False))
        self.assertEqual(self.adapter.texts, [])

    def test_a_request_signed_for_another_agent_is_not_delivered(self):
        self.post("[A2A v1.1] REQUEST to=Antigravity intent=QUERY\ndelete the branch",
                  key=SECRET)
        outcome = self.poll(self.channel())
        self.assertEqual(self.adapter.texts, [])
        self.assertEqual(outcome.delivered, [])

    def test_a_freeze_addressed_with_an_at_sign_still_freezes(self):
        self.freeze("@Hermes", "hermes")
        channel = self.channel()
        self.poll(channel)
        self.assertTrue(getattr(channel, "_frozen", False))

    def test_a_request_addressed_with_an_at_sign_is_delivered(self):
        message_id = self.post("[A2A v1.1] REQUEST to=@Hermes intent=QUERY\nstatus?",
                               key=SECRET)
        outcome = self.poll(self.channel())
        self.assertEqual(outcome.delivered, [message_id])

    def test_a_broadcast_freeze_written_with_an_at_sign_still_needs_the_admin(self):
        self.freeze("@*", "hermes")
        channel = self.channel()
        self.poll(channel)
        self.assertFalse(getattr(channel, "_frozen", False))

    def test_a_re_addressed_request_warns_once(self):
        message_id = self.post("[A2A v1.1] REQUEST to=Antigravity intent=QUERY\nx",
                               key=SECRET)
        channel = self.channel()
        with self.assertLogs("swarmly.telegram.poll", level="WARNING"):
            self.poll(channel)
        ledger = json.loads(_seen_path(self.root, "Hermes").read_text(encoding="utf-8"))
        self.assertIn(message_id, ledger["seen"])

    def test_an_already_seen_rejection_is_not_counted(self):
        message_id = self.post("unsigned, but already handled")
        _save_seen(self.root, "Hermes", {message_id: None})
        outcome = self.poll(self.channel())
        self.assertEqual(outcome.rejected, [])

    def test_an_unsigned_broadcast_is_rejected(self):
        self.post("unsigned to everyone", recipient="*")
        outcome = self.poll(self.channel())
        self.assertEqual(self.adapter.texts, [])
        self.assertEqual(len(outcome.rejected), 1)

    def test_only_unseen_mail_for_this_agent_counts_as_rejected(self):
        self.post("unsigned for another agent", recipient="antigravity")
        self.post("unsigned from ourselves", sender="hermes")
        mine = self.post("unsigned for us")
        channel = self.channel()
        outcome = self.poll(channel)
        self.assertEqual([r["id"] for r in outcome.rejected], [mine])
        heartbeat = json.loads(_heartbeat_path(self.root, "Hermes").read_text())
        self.assertEqual(heartbeat["rejected"], 1)

    def test_a_revoked_sender_with_a_valid_signature_is_rejected(self):
        (self.root / "config" / "a2a_config.json").write_text(json.dumps(
            {"authorized_agents": self.agents, "revocation_list": ["codex"]}),
            encoding="utf-8")
        self.post("signed but revoked", key=SECRET)
        outcome = self.poll(self.channel())
        self.assertEqual(self.adapter.texts, [])
        self.assertIn("revoked", outcome.rejected[0]["reason"])

    def test_a_bad_key_type_rejects_that_sender_without_stopping_the_cycle(self):
        os.environ["A2A_KEY_MALLORY"] = "mallory-secret"
        (self.root / "config" / "a2a_config.json").write_text(json.dumps({"authorized_agents": {
            **self.agents, "mallory": {"public_key_or_secret_name": "A2A_KEY_MALLORY",
                                       "key_type": "hmac-sha256"}}}), encoding="utf-8")
        self.post("from a typo'd registry entry", sender="mallory", key="mallory-secret")
        good = self.post("genuine question", key=SECRET)
        outcome = self.poll(self.channel())
        self.assertEqual(outcome.delivered, [good])
        self.assertIn("Signature not checked", outcome.rejected[0]["reason"])

    def test_a_reader_without_keys_verifies_nothing(self):
        self.post("genuine question", key=SECRET)
        items = read_transport_messages(FolderTransport(self.folder))
        self.assertEqual([item[1] for item in items], [None])
        channel = self.channel()
        channel._gitbus_read = lambda _root: items
        outcome = self.poll(channel)
        self.assertEqual(self.adapter.texts, [])
        self.assertEqual(len(outcome.rejected), 1)


@unittest.skipUnless(HAS_ED25519, "needs cryptography")
class TestEd25519Bus(_Bus):

    kp = KeyPair.generate_ed25519("codex") if HAS_ED25519 else None
    agents = {"codex": {"public_key_or_secret_name": kp.public_key if kp else "",
                        "key_type": "ed25519"}}

    def test_hmac_keyed_with_the_public_key_is_rejected(self):
        self.post("forged with the public key", key=self.kp.public_key, key_type="hmac")
        good = self.post("signed", key=self.kp.private_key, key_type="ed25519")
        outcome = self.poll(self.channel())
        self.assertEqual(outcome.delivered, [good])
        self.assertEqual(len(self.adapter.texts), 1)


class TestBusConfig(unittest.TestCase):

    def test_defaults_are_closed(self):
        bus = make_config().bus
        self.assertFalse(bus.allow_unsigned)
        self.assertEqual(bus.senders, [])

    def test_values_are_checked(self):
        for bad in ({"allow_unsigned": "yes"}, {"allow_unsigned": 1}, {"senders": "codex"},
                    {"senders": [""]}, {"senders": [3]}):
            with self.assertRaises(ConfigError, msg=bad):
                make_config(bus=bad)


if __name__ == "__main__":
    unittest.main()
