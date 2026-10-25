"""Review P1-3: a bus signature covers the whole envelope, old payload-only signatures are
readable but not accepted, and a signed timestamp outside the window is refused."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from swarmly.bus import DurableBus
from swarmly.crypto import HAS_ED25519, CryptoAuth, KeyPair
from swarmly.models import A2AMessage, MessageIntent
from swarmly.transports import FolderTransport

SECRET = "k" * 64


class _Bus(unittest.TestCase):

    registry = {"authorized_agents": {
        "codex": {"public_key_or_secret_name": "A2A_KEY_CODEX"}}}

    def setUp(self):
        env = patch.dict(os.environ, {"A2A_KEY_CODEX": SECRET})
        env.start()
        self.addCleanup(env.stop)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir()
        self.write_registry(self.registry)
        self.folder = self.root / "bus"

    def write_registry(self, registry):
        (self.root / "config" / "a2a_config.json").write_text(json.dumps(registry),
                                                              encoding="utf-8")

    def bus(self):
        return DurableBus(root_dir=self.root,
                          transport=FolderTransport(self.folder, create=True))

    def message(self, text="hello", recipient="hermes"):
        return A2AMessage.create(sender_id="codex", intent="QUERY_REQUEST",
                                 payload={"text": text}, recipient_id=recipient)

    def post(self, message, key=SECRET, key_type="hmac"):
        return self.bus().post_message(message, private_key=key, key_type=key_type)

    def rewrite(self, path, old, new):
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn(old, text)
        Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")

    def read(self):
        [result] = self.bus().read_inbox()
        return result


class TestTheHeaderIsSigned(_Bus):

    def test_a_posted_message_verifies(self):
        self.post(self.message())
        message, valid, status = self.read()
        self.assertTrue(valid, status)
        self.assertTrue(message.signature.startswith("v2:hmac-sha256:"))

    def test_rewriting_any_header_field_breaks_the_signature(self):
        message = self.message()
        new_id = message.message_id[:-4] + "ffff"
        for old, new in (("agent_id: hermes", "agent_id: antigravity"),
                         ("intent: QUERY_REQUEST", "intent: TASK_APPROVE"),
                         (f"message_id: {message.message_id}", f"message_id: {new_id}"),
                         ("priority: ROUTINE", "priority: BLOCKING"),
                         ("correlation_id: ", "correlation_id: msg-x")):
            with self.subTest(field=old):
                for path in self.folder.rglob("*.md"):
                    path.unlink()
                fresh = self.message()
                path = self.post(fresh)
                self.rewrite(path, old.replace(message.message_id, fresh.message_id),
                             new.replace(new_id, fresh.message_id[:-4] + "ffff"))
                _message, valid, status = self.read()
                self.assertFalse(valid, f"{old} -> {new}")
                self.assertIn("Invalid signature", status)

    def test_a_fresh_timestamp_on_an_old_signature_does_not_verify(self):
        message = self.message()
        path = self.post(message)
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        self.rewrite(path, message.timestamp_utc, later)
        self.assertFalse(self.read()[1])

    @unittest.skipUnless(HAS_ED25519, "needs cryptography")
    def test_ed25519_signs_the_envelope_too(self):
        pair = KeyPair.generate_ed25519("codex")
        self.write_registry({"authorized_agents": {"codex": {
            "public_key_or_secret_name": pair.public_key, "key_type": "ed25519"}}})
        path = self.post(self.message(), key=pair.private_key, key_type="ed25519")
        self.assertTrue(self.read()[1])
        self.rewrite(path, "agent_id: hermes", "agent_id: claude")
        self.assertFalse(self.read()[1])


class TestOldSignatures(_Bus):

    def v1(self):
        message = self.message("signed the old way")
        message.signature = CryptoAuth.sign_payload(message.payload, SECRET, "hmac")
        self.post(message)
        return message

    def test_a_v1_signature_is_readable_but_not_accepted(self):
        sent = self.v1()
        message, valid, status = self.read()
        self.assertEqual(message.payload, sent.payload)
        self.assertFalse(valid)
        self.assertIn("v1 signature", status)

    def test_the_registry_can_accept_v1_during_a_rollout(self):
        self.write_registry({**self.registry, "accept_v1_signatures": True})
        self.v1()
        _message, valid, status = self.read()
        self.assertTrue(valid, status)
        self.assertIn("v1", status)

    def test_a_forged_v1_signature_is_still_invalid(self):
        self.write_registry({**self.registry, "accept_v1_signatures": True})
        message = self.message()
        message.signature = CryptoAuth.sign_payload(message.payload, "wrong", "hmac")
        self.post(message)
        self.assertFalse(self.read()[1])

    def test_a_v2_signature_cannot_be_passed_off_as_v1(self):
        message = self.message()
        path = self.post(message)
        self.write_registry({**self.registry, "accept_v1_signatures": True})
        self.rewrite(path, "signature: 'v2:", "signature: '")
        self.assertFalse(self.read()[1])

    def test_a_v2_envelope_moved_into_a_v1_payload_is_refused(self):
        # Review P1-3 m2: the signed fields of a 30-day-old v2 message become the payload
        # of a fresh header for another recipient, and the "v2:" prefix is dropped.
        original = self.message()
        original.timestamp_utc = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        signature = CryptoAuth.sign_envelope(original.signed_fields(), SECRET, "hmac")
        replay = A2AMessage.create(sender_id="codex", intent="TASK_APPROVE",
                                   payload=original.signed_fields(), recipient_id="claude")
        replay.signature = signature[len("v2:"):]
        self.write_registry({**self.registry, "accept_v1_signatures": True})
        self.post(replay)
        _message, valid, status = self.read()
        self.assertFalse(valid)
        self.assertIn("v2 envelope", status)


class TestEmptyHeaderFields(_Bus):
    """Review P1-3 m1: an empty signed field cannot be refilled with something not text."""

    def test_a_nested_section_in_an_empty_field_breaks_the_signature(self):
        path = self.post(self.message())
        for field in ("correlation_id", "timeout_utc"):
            with self.subTest(field=field):
                text = Path(path).read_text(encoding="utf-8")
                Path(path).write_text(text.replace(f"{field}: \n", f"{field}:\n  x: y\n", 1),
                                      encoding="utf-8")
                self.assertFalse(self.read()[1])

    def test_a_number_in_a_header_field_is_not_signed_as_empty(self):
        message = self.message()
        message.correlation_id = 5
        with self.assertRaises(ValueError):
            message.signed_fields()


class TestTheTimeWindow(_Bus):

    def signed_at(self, when):
        message = self.message()
        message.timestamp_utc = when.isoformat()
        self.post(message)
        return self.read()

    def test_a_recent_message_is_accepted(self):
        self.assertTrue(self.signed_at(datetime.now(timezone.utc) - timedelta(days=6))[1])

    def test_an_old_message_is_refused(self):
        _message, valid, status = self.signed_at(
            datetime.now(timezone.utc) - timedelta(days=8))
        self.assertFalse(valid)
        self.assertIn("older than", status)

    def test_a_message_from_the_future_is_refused(self):
        _message, valid, status = self.signed_at(
            datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertFalse(valid)
        self.assertIn("future", status)


class TestTheIntentEnum(unittest.TestCase):

    def test_signed_fields_survive_the_file_round_trip(self):
        message = A2AMessage.create(sender_id="codex", intent=MessageIntent.STATUS_UPDATE,
                                    payload={"text": "a\n---\nb", "n": [1, {"x": None}]},
                                    recipient_id="*", correlation_id="msg-1")
        message.signature = "v2:x"
        parsed = A2AMessage.from_yaml_frontmatter(message.to_yaml_frontmatter())
        self.assertEqual(parsed.signed_fields(), message.signed_fields())


if __name__ == "__main__":
    unittest.main()
