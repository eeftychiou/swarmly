"""
Unit tests for the A2A Durable Plane File and Git Bus.
"""

import unittest
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import subprocess

from swarmly.models import A2AMessage, MessageIntent, MessagePriority
from swarmly.crypto import KeyPair
from swarmly.bus import DurableBus, BusPushError


class TestDurableBus(unittest.TestCase):

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.bus = DurableBus(root_dir=self.root_path)
        self.kp = KeyPair.generate_hmac("test_sender")

        # Mock public key resolution; only a registered sender verifies.
        self.bus.config.setdefault("authorized_agents", {})["test_sender"] = {
            "public_key_or_secret_name": "A2A_KEY_TEST_SENDER"}
        self.bus.get_agent_key = lambda aid: self.kp.public_key if aid == "test_sender" else None

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_push_failure_is_loud_and_keeps_the_local_commit(self):
        """Drill finding (PR #78): a failed --push must surface, not vanish."""
        # Isolate from other tests' GIT_* overrides: a leaked empty identity would
        # make the *commit* fail and mask the push failure under test.
        git_env = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
        subprocess.run(["git", "init", "-q", "."], cwd=self.root_path,
                       check=True, env=git_env)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=self.root_path, check=True, env=git_env)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root_path,
                       check=True, env=git_env)
        subprocess.run(["git", "remote", "add", "origin",
                        str(self.root_path / "no-such-remote.git")],
                       cwd=self.root_path, check=True, env=git_env)

        msg = A2AMessage.create(sender_id="test_sender",
                                intent=MessageIntent.STATUS_UPDATE,
                                payload={"n": 1}, recipient_id="peer")
        saved_git_env = {key: os.environ.pop(key)
                         for key in list(os.environ) if key.startswith("GIT_")}
        try:
            with self.assertRaises(BusPushError) as caught:
                self.bus.post_message(msg, private_key=self.kp.private_key, git_push=True)
        finally:
            os.environ.update(saved_git_env)

        self.assertIn("git push failed", str(caught.exception))
        self.assertTrue(list(self.bus.inbox_dir.glob("*.md")),
                        "the message file must still be on disk after a push failure")

    def test_post_and_read_verified_message(self):
        payload = {"decision": "MERGE", "target_branch": "main"}
        msg = A2AMessage.create(
            sender_id="test_sender",
            intent=MessageIntent.TASK_APPROVE,
            payload=payload,
            recipient_id="*"
        )

        saved_path = self.bus.post_message(msg, private_key=self.kp.private_key)
        self.assertTrue(saved_path.exists())

        # Read back
        messages = self.bus.read_inbox(verify_signatures=True)
        self.assertEqual(len(messages), 1)
        read_msg, is_valid, status = messages[0]

        self.assertTrue(is_valid)
        self.assertEqual(read_msg.sender.agent_id, "test_sender")
        self.assertEqual(read_msg.payload["decision"], "MERGE")
        self.assertIn("Valid signature", status)

    def test_tampered_inbox_file_detected(self):
        payload = {"score": 100}
        msg = A2AMessage.create(
            sender_id="test_sender",
            intent=MessageIntent.EXPERIMENT_REGISTER,
            payload=payload
        )

        saved_path = self.bus.post_message(msg, private_key=self.kp.private_key)

        # Tamper with file contents on disk
        with open(saved_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        tampered_content = content.replace('"score": 100', '"score": 999')
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(tampered_content)

        # Read inbox
        messages = self.bus.read_inbox(verify_signatures=True)
        self.assertEqual(len(messages), 1)
        _, is_valid, status = messages[0]

        self.assertFalse(is_valid)
        self.assertIn("Invalid signature", status)

    def test_same_second_messages_do_not_overwrite_inbox_files(self):
        fixed_now = datetime(2026, 9, 19, 12, 34, 56, tzinfo=timezone.utc)

        with patch("swarmly.models.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            first = A2AMessage.create(
                sender_id="test_sender",
                intent=MessageIntent.STATUS_UPDATE,
                payload={"sequence": 1},
            )
            second = A2AMessage.create(
                sender_id="test_sender",
                intent=MessageIntent.STATUS_UPDATE,
                payload={"sequence": 2},
            )

        self.assertNotEqual(first.message_id, second.message_id)
        self.assertRegex(first.message_id, r"^msg-20260919T123456Z-[a-z0-9_]{4,16}$")
        self.assertRegex(second.message_id, r"^msg-20260919T123456Z-[a-z0-9_]{4,16}$")
        first_path = self.bus.post_message(first, private_key=self.kp.private_key)
        second_path = self.bus.post_message(second, private_key=self.kp.private_key)

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(len(list(self.bus.inbox_dir.glob("*.md"))), 2)
        messages = self.bus.read_inbox(verify_signatures=True)
        self.assertEqual({message.payload["sequence"] for message, _, _ in messages}, {1, 2})


if __name__ == "__main__":
    unittest.main()


class ShadowVisibilityTests(unittest.TestCase):
    """Issue #105 (pin B of #103): an ambient ``A2A_KEY_*`` that overrides the
    configured value must be VISIBLE -- once per agent per process -- and the key
    material must never reach a log line."""

    def setUp(self):
        import json as _json
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        cfg_path = root / "a2a_config.json"
        cfg_path.write_text(_json.dumps({
            "authorized_agents": {
                "hermes": {"public_key_or_secret_name": "HERMES_CONFIGURED_KEY"},
            },
            "revocation_list": [],
        }), encoding="utf-8")
        self.bus = DurableBus(root_dir=root, config_path=cfg_path)

    def tearDown(self):
        for key in ("A2A_KEY_HERMES", "HERMES_CONFIGURED_KEY"):
            os.environ.pop(key, None)
        self.temp_dir.cleanup()

    def test_differing_env_override_is_visible_once_and_never_logs_values(self):
        os.environ["A2A_KEY_HERMES"] = "ambient-secret-value"
        os.environ["HERMES_CONFIGURED_KEY"] = "configured-secret-value"
        with self.assertLogs("swarmly.bus", level="INFO") as captured:
            first = self.bus.get_agent_key("hermes")
            second = self.bus.get_agent_key("hermes")
        self.assertEqual(first, "ambient-secret-value")
        self.assertEqual(second, "ambient-secret-value")
        lines = [r.getMessage() for r in captured.records
                 if "env override" in r.getMessage()]
        self.assertEqual(len(lines), 1, lines)                 # once per agent/process
        self.assertIn("A2A_KEY_HERMES", lines[0])
        self.assertNotIn("ambient-secret-value", lines[0])     # never the value
        self.assertNotIn("configured-secret-value", lines[0])

    def test_matching_env_override_is_silent(self):
        os.environ["A2A_KEY_HERMES"] = "same-value"
        os.environ["HERMES_CONFIGURED_KEY"] = "same-value"
        with self.assertNoLogs("swarmly.bus", level="INFO"):
            self.assertEqual(self.bus.get_agent_key("hermes"), "same-value")

    def test_unknown_agent_still_returns_none(self):
        os.environ.pop("A2A_KEY_NOBODY", None)
        self.assertIsNone(self.bus.get_agent_key("nobody"))
