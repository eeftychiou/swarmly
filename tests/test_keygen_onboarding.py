"""Onboarding an agent: ``swarmly-keygen`` creates its key and registers it, and a message it
then sends verifies for another onboarded agent (the end-to-end path a new install takes)."""

import base64
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly import crypto
from swarmly.keygen import main

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line)


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_")}
    env.update(PYTHONPATH=str(ROOT), A2A_LOG_DIR="off", **extra)
    return env


def run(*args, env=None):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                          env=env or clean_env(), timeout=60)


class _Tmp(unittest.TestCase):

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir()
        self.registry = self.root / "config" / "a2a_config.json"
        self.registry.write_text(json.dumps({"authorized_agents": {}}), encoding="utf-8")

    def keygen(self, *args):
        return run("-m", "swarmly.keygen", *args)

    def entry(self, agent):
        return json.loads(self.registry.read_text(encoding="utf-8"))["authorized_agents"][agent]


@unittest.skipUnless(crypto.HAS_ED25519, "needs the cryptography package")
class TestKeygen(_Tmp):

    def test_onboarding_writes_the_key_and_registers_the_public_half(self):
        env_file = self.root / "alice" / ".env"
        result = self.keygen("--agent", "alice", "--env-file", str(env_file),
                             "--registry", str(self.registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        private = read_env(env_file)["A2A_KEY_ALICE"]
        self.assertNotIn(private, result.stdout)                 # never printed
        entry = self.entry("alice")
        self.assertEqual(entry["key_type"], "ed25519")
        self.assertNotEqual(entry["public_key_or_secret_name"], private)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

    def test_an_existing_key_is_kept_and_registered_and_replaced_only_with_force(self):
        env_file = self.root / ".env"
        args = ["--agent", "alice", "--env-file", str(env_file), "--registry", str(self.registry)]
        self.assertEqual(self.keygen(*args).returncode, 0)
        first = (read_env(env_file)["A2A_KEY_ALICE"], self.entry("alice"))
        again = self.keygen(*args)                          # same key, same entry: a no-op
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((read_env(env_file)["A2A_KEY_ALICE"], self.entry("alice")), first)
        forced = self.keygen(*args, "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn("replacing the key registered", forced.stderr)
        self.assertNotEqual(read_env(env_file)["A2A_KEY_ALICE"], first[0])
        self.assertNotEqual(self.entry("alice"), first[1])

    def test_an_existing_peer_onboards_without_rotating(self):
        # #167 review: every live bot's .env already holds its key while the shipped
        # registry is now empty; registering that key must not replace it.
        pair = crypto.KeyPair.generate_ed25519("alice")
        env_file = self.root / ".env"
        env_file.write_text(f"A2A_KEY_ALICE={pair.private_key}\n", encoding="utf-8")
        result = self.keygen("--agent", "alice", "--env-file", str(env_file),
                             "--registry", str(self.registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_env(env_file)["A2A_KEY_ALICE"], pair.private_key)
        self.assertEqual(self.entry("alice")["public_key_or_secret_name"], pair.public_key)

    def test_a_refused_registry_leaves_no_key_behind_and_registers_nothing_unstored(self):
        # #167 review M1: the registry was written first, so a refused or failed .env
        # write left a registered public key whose private half was thrown away.
        env_file = self.root / ".env"
        self.assertEqual(self.keygen("--agent", "alice", "--env-file", str(env_file),
                                     "--registry", str(self.registry)).returncode, 0)
        entry = self.entry("alice")
        other = self.root / "other.env"                      # a fresh host, no key yet
        refused = self.keygen("--agent", "alice", "--env-file", str(other),
                              "--registry", str(self.registry))
        self.assertEqual(refused.returncode, 2)
        self.assertFalse(other.exists())
        self.assertEqual(self.entry("alice"), entry)
        blocked = self.root / "blocked"
        blocked.write_text("a file, not a folder", encoding="utf-8")
        failed = self.keygen("--agent", "bob", "--env-file", str(blocked / ".env"),
                             "--registry", str(self.registry))
        self.assertEqual(failed.returncode, 2)
        self.assertNotIn("bob", json.loads(self.registry.read_text("utf-8"))["authorized_agents"])

    def test_a_byte_order_mark_does_not_hide_the_existing_key(self):
        pair = crypto.KeyPair.generate_ed25519("alice")
        env_file = self.root / ".env"
        env_file.write_text(f"\ufeffA2A_KEY_ALICE={pair.private_key}\nOTHER=1\n",
                            encoding="utf-8")
        self.assertEqual(self.keygen("--agent", "alice", "--env-file", str(env_file))
                         .returncode, 0)
        text = env_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("A2A_KEY_ALICE="), 1)

    @unittest.skipIf(os.name == "nt", "POSIX modes and symlinks")
    def test_the_registry_keeps_its_mode_and_symlinks_are_written_through(self):
        os.chmod(self.registry, 0o644)
        link = self.root / "registry-link.json"
        link.symlink_to(self.registry)
        env_target = self.root / "real.env"
        env_target.write_text("OTHER=1\n", encoding="utf-8")
        env_link = self.root / "link.env"
        env_link.symlink_to(env_target)
        result = self.keygen("--agent", "alice", "--env-file", str(env_link),
                             "--registry", str(link))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(link.is_symlink() and env_link.is_symlink())
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o644)
        self.assertIn("alice", json.loads(self.registry.read_text("utf-8"))["authorized_agents"])
        self.assertIn("A2A_KEY_ALICE", read_env(env_target))

    def test_an_explicit_type_is_honoured_for_an_existing_key(self):
        # #167 review: --type was ignored when the .env held a key, so a base64 HMAC
        # secret was registered as an Ed25519 key and every message failed to verify.
        secret = base64.b64encode(os.urandom(32)).decode("ascii")
        env_file = self.root / ".env"
        env_file.write_text(f"A2A_KEY_ALICE={secret}\n", encoding="utf-8")
        result = self.keygen("--agent", "alice", "--type", "hmac", "--env-file",
                             str(env_file), "--registry", str(self.registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entry("alice")["key_type"], "hmac")
        self.assertEqual(self.entry("alice")["public_key_or_secret_name"], "A2A_KEY_ALICE")
        self.assertEqual(read_env(env_file)["A2A_KEY_ALICE"], secret)

    def test_an_explicit_type_that_does_not_fit_the_existing_key_is_refused(self):
        env_file = self.root / ".env"
        env_file.write_text("A2A_KEY_ALICE=" + "ab" * 32 + "\n", encoding="utf-8")
        result = self.keygen("--agent", "alice", "--type", "ed25519", "--env-file",
                             str(env_file), "--registry", str(self.registry))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("alice", json.loads(self.registry.read_text("utf-8"))["authorized_agents"])

    def test_other_env_lines_are_kept(self):
        env_file = self.root / ".env"
        env_file.write_text("A2A_TELEGRAM_BOT_TOKEN_ALICE=keep\n", encoding="utf-8")
        self.assertEqual(self.keygen("--agent", "alice", "--env-file", str(env_file))
                         .returncode, 0)
        self.assertEqual(set(read_env(env_file)),
                         {"A2A_TELEGRAM_BOT_TOKEN_ALICE", "A2A_KEY_ALICE"})


class TestKeygenChecks(_Tmp):

    def test_hmac_registers_the_variable_name_not_the_secret(self):
        env_file = self.root / ".env"
        self.assertEqual(main(["--agent", "bob", "--type", "hmac", "--env-file", str(env_file),
                               "--registry", str(self.registry)]), 0)
        entry = self.entry("bob")
        self.assertEqual(entry["public_key_or_secret_name"], "A2A_KEY_BOB")
        self.assertEqual(entry["key_type"], "hmac")
        self.assertNotIn(read_env(env_file)["A2A_KEY_BOB"], self.registry.read_text("utf-8"))

    def test_a_missing_registry_is_refused_and_no_key_is_written(self):
        env_file = self.root / ".env"
        self.assertEqual(main(["--agent", "bob", "--type", "hmac", "--env-file", str(env_file),
                               "--registry", str(self.root / "nope.json")]), 2)
        self.assertFalse(env_file.exists())

    def test_a_home_relative_env_file_is_created_owner_only(self):
        home = self.root / "home"
        result = run("-m", "swarmly.keygen", "--agent", "bob", "--type", "hmac", "--env-file",
                     "~/.a2a-telegram.env", env=clean_env(HOME=str(home),
                                                          USERPROFILE=str(home)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A2A_KEY_BOB", read_env(home / ".a2a-telegram.env"))

    def test_an_id_sharing_a_registered_agents_key_variable_is_refused(self):
        # #167 review: a_b and a-b both sign with A2A_KEY_A_B, so onboarding a_b would
        # reuse a-b's key or rotate it and a-b's messages would stop verifying.
        self.assertEqual(main(["--agent", "a-b", "--type", "hmac", "--env-file",
                               str(self.root / "ab.env"), "--registry", str(self.registry)]), 0)
        before = self.registry.read_text("utf-8")
        env_file = self.root / "a_b.env"
        self.assertEqual(main(["--agent", "a_b", "--type", "hmac", "--env-file", str(env_file),
                               "--registry", str(self.registry)]), 2)
        self.assertFalse(env_file.exists())
        self.assertEqual(self.registry.read_text("utf-8"), before)

    def test_duplicate_key_lines_in_the_env_are_refused(self):
        # #167 review: keygen read the last line and the transports load the first, so
        # the registered key and the signing key could differ.
        env_file = self.root / ".env"
        text = "A2A_KEY_BOB=" + "ab" * 32 + "\nA2A_KEY_BOB=" + "cd" * 32 + "\n"
        env_file.write_text(text, encoding="utf-8")
        self.assertEqual(main(["--agent", "bob", "--env-file", str(env_file),
                               "--registry", str(self.registry)]), 2)
        self.assertEqual(env_file.read_text("utf-8"), text)
        self.assertEqual(json.loads(self.registry.read_text("utf-8"))["authorized_agents"], {})
        self.assertEqual(main(["--agent", "bob", "--type", "hmac", "--force", "--env-file",
                               str(env_file), "--registry", str(self.registry)]), 0)
        self.assertEqual(env_file.read_text("utf-8").count("A2A_KEY_BOB="), 1)

    def test_a_bad_agent_id_is_refused(self):
        self.assertEqual(main(["--agent", "../x", "--type", "hmac"]), 2)

    def test_the_shipped_registry_names_no_agents(self):
        shipped = json.loads((ROOT / "config" / "a2a_config.json").read_text("utf-8"))
        self.assertEqual(shipped["authorized_agents"], {})


@unittest.skipUnless(crypto.HAS_ED25519, "needs the cryptography package")
class TestOnboardingEndToEnd(_Tmp):
    """Two agents onboarded with swarmly-keygen exchange a message over a folder bus."""

    def setUp(self):
        super().setUp()
        (self.root / "bus").mkdir()                         # the shared (synced) folder

    def onboard(self, agent):
        env_file = self.root / agent / ".env"
        result = self.keygen("--agent", agent, "--env-file", str(env_file),
                             "--registry", str(self.registry))
        self.assertEqual(result.returncode, 0, result.stderr)
        return read_env(env_file)

    def bus(self, env, *args):
        return run(str(ROOT / "scripts" / "a2a_bus.py"), "--transport", "folder",
                   "--folder", str(self.root / "bus"), "--config", str(self.registry),
                   *args, env=clean_env(**env))

    def test_status_lists_onboarded_agents_and_says_how_when_none(self):
        empty = self.bus({}, "status")
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        self.assertIn("swarmly-keygen", empty.stdout)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["authorized_agents"]["bare"] = {"public_key_or_secret_name": "A2A_KEY_BARE"}
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        self.onboard("alice")
        listed = self.bus({}, "status")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("@alice", listed.stdout)
        self.assertIn("Key: ed25519", listed.stdout)
        self.assertIn("@bare", listed.stdout)                # no metadata: no crash

    def test_a_message_from_an_onboarded_agent_verifies(self):
        alice, bob = self.onboard("alice"), self.onboard("bob")
        sent = self.bus(alice, "send", "--sender", "alice", "--recipient", "bob",
                        "--text", "hello bob")
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        inbox = self.bus(bob, "inbox")                     # bob holds no key of alice's
        self.assertEqual(inbox.returncode, 0, inbox.stdout + inbox.stderr)
        self.assertIn("Valid signature", inbox.stdout)

    def test_an_agent_that_was_not_registered_does_not_verify(self):
        bob = self.onboard("bob")
        mallory = self.root / "mallory.env"
        self.assertEqual(main(["--agent", "mallory", "--env-file", str(mallory)]), 0)
        sent = self.bus(read_env(mallory), "send", "--sender", "mallory", "--text", "hi")
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        inbox = self.bus(bob, "inbox")
        self.assertNotIn("Valid signature", inbox.stdout)
        self.assertIn("No registered public key for sender 'mallory'", inbox.stdout)


if __name__ == "__main__":
    unittest.main()
