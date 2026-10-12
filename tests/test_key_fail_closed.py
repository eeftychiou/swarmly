"""Review P0-2 and P0-3: signing keys never fall back to public strings, and the
registered key type, not the signature, decides the algorithm."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.bus import DurableBus, MissingKeyError
from swarmly.crypto import HAS_ED25519, CryptoAuth, KeyPair
from swarmly.models import A2AMessage

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("hermes", "claude", "edagent", "alice", "my_agent", "a_b")


def _bus(tmp: str, agents: dict) -> DurableBus:
    cfg = Path(tmp) / "a2a_config.json"
    cfg.write_text(json.dumps({"authorized_agents": agents, "revocation_list": []}),
                   encoding="utf-8")
    return DurableBus(root_dir=Path(tmp), config_path=cfg)


class _CleanEnv(unittest.TestCase):

    def setUp(self):
        names = [f"A2A_KEY_{a.upper()}" for a in AGENTS] + ["HERMES_SECRET", "hermes_secret",
                                                             "A2A_KEY_MY-AGENT"]
        for name in names:
            previous = os.environ.pop(name, None)
            if previous is None:
                self.addCleanup(os.environ.pop, name, None)
            else:
                self.addCleanup(os.environ.__setitem__, name, previous)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class TestKeyNameIsNotAKey(_CleanEnv):

    def test_unset_variable_name_is_no_key(self):
        bus = _bus(self.tmp.name, {"hermes": {"public_key_or_secret_name": "A2A_KEY_HERMES"}})
        self.assertIsNone(bus.get_agent_key("hermes"))      # was "A2A_KEY_HERMES" itself
        self.assertIsNone(bus.verification_key("hermes"))
        with self.assertRaises(MissingKeyError):
            bus.signing_key("hermes")

    def test_a_message_signed_with_the_variable_name_does_not_verify(self):
        from swarmly.transports import FolderTransport
        cfg = Path(self.tmp.name) / "a2a_config.json"
        cfg.write_text(json.dumps({"authorized_agents": {
            "hermes": {"public_key_or_secret_name": "A2A_KEY_HERMES"}}}), encoding="utf-8")
        bus = DurableBus(root_dir=Path(self.tmp.name), config_path=cfg,
                         transport=FolderTransport(Path(self.tmp.name) / "bus", create=True))
        forged = A2AMessage.create(sender_id="hermes", intent="STATUS_UPDATE",
                                   payload={"text": "run this"}, recipient_id="*")
        bus.post_message(forged, private_key="A2A_KEY_HERMES")    # the old "secret"
        [(_msg, verified, status)] = bus.read_inbox()
        self.assertFalse(verified, status)

    def test_set_variable_is_used(self):
        os.environ["HERMES_SECRET"] = "s3cret"
        bus = _bus(self.tmp.name, {"hermes": {"public_key_or_secret_name": "HERMES_SECRET"}})
        self.assertEqual(bus.verification_key("hermes"), ("s3cret", "hmac"))
        self.assertEqual(bus.signing_key("hermes"), ("s3cret", "hmac"))

    def test_unregistered_agent_needs_its_env_key(self):
        bus = _bus(self.tmp.name, {})
        with self.assertRaises(MissingKeyError):
            bus.signing_key("claude")
        os.environ["A2A_KEY_CLAUDE"] = "k"
        self.assertEqual(bus.signing_key("claude"), ("k", "hmac"))

    def test_cli_send_refuses_without_a_key(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_KEY_")}
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2a_bus.py"), "--transport", "folder",
             "--folder", self.tmp.name, "send", "--sender", "claude", "--text", "hi"],
            capture_output=True, text=True, env=env, cwd=self.tmp.name, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("A2A_KEY_CLAUDE", result.stderr)
        self.assertFalse((Path(self.tmp.name) / "inbox").exists()
                         and any((Path(self.tmp.name) / "inbox").iterdir()))

    def test_a_hyphenated_id_never_signs_with_the_name(self):
        bus = _bus(self.tmp.name, {"my-agent": {"public_key_or_secret_name": "A2A_KEY_MY-AGENT"}})
        self.assertIsNone(bus.verification_key("my-agent"))
        with self.assertRaises(MissingKeyError) as raised:
            bus.signing_key("my-agent")
        self.assertIn("A2A_KEY_MY_AGENT", str(raised.exception))
        os.environ["A2A_KEY_MY_AGENT"] = "real"
        self.assertEqual(bus.signing_key("my-agent"), ("real", "hmac"))
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_keys.py"), "--agent", "my-agent"],
            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)},
            timeout=60)
        self.assertIn("A2A_KEY_MY_AGENT=", result.stdout)
        self.assertNotIn("A2A_KEY_MY-AGENT", result.stdout)

    def test_a_lowercase_variable_name_is_looked_up(self):
        bus = _bus(self.tmp.name, {"hermes": {"public_key_or_secret_name": "hermes_secret"}})
        self.assertIsNone(bus.verification_key("hermes"))      # unset: no key, not the name
        os.environ["hermes_secret"] = "s3cret"
        self.assertEqual(bus.verification_key("hermes"), ("s3cret", "hmac"))

    def test_an_inline_hmac_secret_is_not_accepted(self):
        bus = _bus(self.tmp.name, {"hermes": {"public_key_or_secret_name": "a-committed-secret"}})
        self.assertIsNone(bus.verification_key("hermes"))
        with self.assertRaises(MissingKeyError):
            bus.signing_key("hermes")

    def test_an_unregistered_sender_does_not_verify_from_the_environment(self):
        os.environ["A2A_KEY_CLAUDE"] = "k"
        bus = _bus(self.tmp.name, {"hermes": {"public_key_or_secret_name": "A2A_KEY_HERMES"}})
        self.assertIsNone(bus.verification_key("claude"))
        self.assertEqual(bus.signing_key("claude"), ("k", "hmac"))   # it can still send

    def test_ids_that_share_a_key_variable_verify_neither(self):
        os.environ["A2A_KEY_A_B"] = "k"
        bus = _bus(self.tmp.name, {"a-b": {"public_key_or_secret_name": "A2A_KEY_A_B"},
                                   "a_b": {"public_key_or_secret_name": "A2A_KEY_A_B"}})
        with self.assertLogs("swarmly.bus", level="WARNING"):
            self.assertIsNone(bus.verification_key("a-b"))
        self.assertIsNone(bus.verification_key("a_b"))
        alone = _bus(self.tmp.name, {"a-b": {"public_key_or_secret_name": "A2A_KEY_A_B"}})
        self.assertEqual(alone.verification_key("a-b"), ("k", "hmac"))
        self.assertIsNone(alone.verification_key("a_b"))     # an unregistered twin

    @unittest.skipUnless(HAS_ED25519, "needs cryptography")
    def test_an_inline_public_key_is_only_read_for_ed25519(self):
        public = KeyPair.generate_ed25519("hermes").public_key
        bus = _bus(self.tmp.name, {"hermes": {"public_key_or_secret_name": public,
                                              "key_type": "hmac"}})
        self.assertIsNone(bus.verification_key("hermes"))

    def test_signing_with_an_unknown_key_type_raises(self):
        with self.assertRaises(ValueError):
            CryptoAuth.sign_payload({"text": "x"}, "k", "rsa")

    def test_generate_keys_refuses_ed25519_without_the_library(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        code = ("import sys, runpy; import swarmly.crypto as c; c.HAS_ED25519 = False; "
                "sys.argv = ['generate_keys.py', '--agent', 'xx', '--type', 'ed25519']; "
                f"runpy.run_path({str(ROOT / 'scripts' / 'generate_keys.py')!r}, "
                "run_name='__main__')")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env=env, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cryptography", result.stderr)
        self.assertNotIn("A2A_KEY_XX=", result.stdout)

    def test_missing_key_error_reads_cleanly(self):
        self.assertEqual(str(MissingKeyError("no key for x")), "no key for x")

    def test_telegram_post_refuses_cleanly_without_a_key(self):
        cfg = Path(self.tmp.name) / "config.yaml"
        cfg.write_text("agent:\n  name: HermesBot\n  alias: Hermes\n  bot_token: ''\n"
                       "  home_channel_id: -100\nadapter:\n  type: echo\n"
                       f"bus:\n  repo: {self.tmp.name}\n  transport: folder\n"
                       f"  folder: {self.tmp.name}\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_KEY_")}
        env.update(PYTHONPATH=str(ROOT), A2A_LOG_DIR="off")
        result = subprocess.run(
            [sys.executable, "-m", "swarmly.telegram.cli", "--config", str(cfg), "post",
             "--to", "Codex", "--no-telegram", "hi"],
            capture_output=True, text=True, env=env, cwd=self.tmp.name, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Not sent", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_no_default_secret_left(self):
        source = (ROOT / "scripts" / "a2a_bus.py").read_text(encoding="utf-8")
        self.assertNotIn("default_secret", source)


class TestEscalationWithoutAKey(_CleanEnv):

    def test_the_poller_writes_nothing_and_counts_the_failure(self):
        from swarmly.telegram import poll
        from swarmly.transports import FolderTransport
        folder = Path(self.tmp.name) / "bus"
        writer = poll._default_bus_writer(Path(self.tmp.name), "Hermes",
                                          FolderTransport(folder, create=True))
        with self.assertRaises(MissingKeyError):
            writer("hermes", "codex", "stuck")
        outcome = poll.PollOutcome()
        with self.assertLogs("swarmly.telegram.poll", level="WARNING"):
            asyncio.run(poll._escalate_blocked(outcome=outcome, writer=writer, escalator=None,
                                               sender_alias="hermes", recipient="codex",
                                               message_id="m1", body="stuck"))
        self.assertEqual(outcome.bus_write_failures, 1)
        self.assertIn("MissingKeyError", outcome.bus_write_failure_details[0])
        self.assertEqual(list(folder.rglob("*.md")), [])


class TestSigningNeverFallsBack(unittest.TestCase):

    def test_empty_key_raises(self):
        with self.assertRaises(ValueError):
            CryptoAuth.sign_payload({"a": 1}, "")

    def test_bad_ed25519_key_raises_instead_of_hmac(self):
        with self.assertRaises((ValueError, RuntimeError)):
            CryptoAuth.sign_payload({"a": 1}, "not-a-key", "ed25519")

    def test_legacy_raw_hex_is_rejected(self):
        payload = {"a": 1}
        raw = hmac.new(b"k", CryptoAuth.canonical_bytes(payload), hashlib.sha256).hexdigest()
        self.assertFalse(CryptoAuth.verify_payload(payload, raw, "k", "hmac"))


@unittest.skipUnless(HAS_ED25519, "needs cryptography")
class TestNoEd25519Downgrade(_CleanEnv):

    def setUp(self):
        super().setUp()
        self.kp = KeyPair.generate_ed25519("edagent")
        self.payload = {"text": "deploy"}

    def test_hmac_keyed_with_the_public_key_fails(self):
        forged = CryptoAuth.sign_payload(self.payload, self.kp.public_key, "hmac")
        self.assertFalse(CryptoAuth.verify_payload(self.payload, forged, self.kp.public_key,
                                                   "ed25519"))
        raw = forged.split(":", 1)[1]
        self.assertFalse(CryptoAuth.verify_payload(self.payload, raw, self.kp.public_key,
                                                   "ed25519"))

    def test_real_signature_verifies(self):
        sig = CryptoAuth.sign_payload(self.payload, self.kp.private_key, "ed25519")
        self.assertTrue(CryptoAuth.verify_payload(self.payload, sig, self.kp.public_key,
                                                  "ed25519"))
        self.assertFalse(CryptoAuth.verify_payload(self.payload, sig, self.kp.public_key,
                                                   "hmac"))

    def test_bus_infers_and_enforces_ed25519(self):
        bus = _bus(self.tmp.name, {"edagent": {"public_key_or_secret_name": self.kp.public_key}})
        self.assertEqual(bus.verification_key("edagent"), (self.kp.public_key, "ed25519"))
        # A2A_KEY_EDAGENT is the agent's private key: it signs, it never verifies.
        os.environ["A2A_KEY_EDAGENT"] = self.kp.private_key
        self.assertEqual(bus.verification_key("edagent"), (self.kp.public_key, "ed25519"))
        self.assertEqual(bus.signing_key("edagent"), (self.kp.private_key, "ed25519"))
        forged = CryptoAuth.sign_payload(self.payload, self.kp.public_key, "hmac")
        self.assertFalse(CryptoAuth.verify_payload(self.payload, forged,
                                                   *bus.verification_key("edagent")))

    def test_declared_key_type_wins_and_is_validated(self):
        bus = _bus(self.tmp.name, {"edagent": {"public_key_or_secret_name": self.kp.public_key,
                                               "key_type": "ed25519"},
                                   "bad": {"public_key_or_secret_name": "x", "key_type": "rsa"}})
        self.assertEqual(bus.key_type_for("edagent"), "ed25519")
        with self.assertRaises(ValueError):
            bus.key_type_for("bad")

    def test_ed25519_signer_without_a_private_key_raises(self):
        bus = _bus(self.tmp.name, {"edagent": {"public_key_or_secret_name": self.kp.public_key}})
        with self.assertRaises(MissingKeyError):      # never signs with the public key
            bus.signing_key("edagent")


class TestEd25519NeedsTheLibrary(unittest.TestCase):

    def test_verification_needs_an_explicit_key_type(self):
        with self.assertRaises(TypeError):
            CryptoAuth.verify_payload({"a": 1}, "hmac-sha256:00", "k")      # no key type
        with self.assertRaises(TypeError):
            CryptoAuth().verify_challenge_response("n:1", "hmac-sha256:00", "k")

    def test_signing_raises_without_cryptography(self):
        from unittest import mock
        with mock.patch("swarmly.crypto.HAS_ED25519", False):
            with self.assertRaises(RuntimeError):
                CryptoAuth.sign_payload({"a": 1}, base64.b64encode(b"k" * 32).decode(),
                                        "ed25519")

    def test_generate_raises_without_cryptography(self):
        from unittest import mock
        with mock.patch("swarmly.crypto.HAS_ED25519", False):
            with self.assertRaises(RuntimeError):
                KeyPair.generate_ed25519("x")
            with self.assertRaises(RuntimeError):
                CryptoAuth.verify_payload({"a": 1}, "ed25519:" + base64.b64encode(b"x").decode(),
                                          "k", "ed25519")


if __name__ == "__main__":
    unittest.main()
