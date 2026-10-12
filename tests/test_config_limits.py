"""Issue #109 (bool-typed limit coercion) and #113 (BOM in .env) pins.

DISCRIMINATING by construction: on the pre-fix tree, ``bool("false")`` made
``respond_without_mention`` load as ``True`` (routing silently on), and a BOM'd
first key in .env was dropped because the key read as ``"\\ufeffKEY"``.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.config import AgentConfig, ConfigError, load_env


def cfg_with_limits(limits):
    return AgentConfig.from_dict({
        "agent": {"alias": "Hermes", "name": "HermesBot"},
        "adapter": {"type": "echo"},
        "limits": limits,
    })


class BoolLimitCoercionTests(unittest.TestCase):
    def test_quoted_false_strings_stay_off(self):
        """#109: bool('false') is True -- the inversion that silently enabled routing."""
        for word in ("false", "False", "FALSE", "no", "NO", "off", "0", " false "):
            cfg = cfg_with_limits({"respond_without_mention": word})
            self.assertFalse(cfg.limits.respond_without_mention, word)
            cfg2 = cfg_with_limits({"commands_enabled": word})
            self.assertFalse(cfg2.limits.commands_enabled, word)

    def test_quoted_true_strings_stay_on(self):
        for word in ("true", "TRUE", "yes", "on", "1"):
            cfg = cfg_with_limits({"respond_without_mention": word})
            self.assertTrue(cfg.limits.respond_without_mention, word)

    def test_real_bools_and_numbers(self):
        self.assertFalse(cfg_with_limits({"respond_without_mention": False})
                         .limits.respond_without_mention)
        self.assertTrue(cfg_with_limits({"respond_without_mention": True})
                        .limits.respond_without_mention)
        self.assertTrue(cfg_with_limits({"respond_without_mention": 1})
                        .limits.respond_without_mention)
        self.assertFalse(cfg_with_limits({"respond_without_mention": 0})
                         .limits.respond_without_mention)

    def test_unrecognised_values_are_rejected_with_the_key_named(self):
        for bad in ("maybe", "", "2", 2, 0.5, None, []):
            with self.assertRaises(ConfigError) as caught:
                cfg_with_limits({"respond_without_mention": bad})
            self.assertIn("limits.respond_without_mention", str(caught.exception), bad)

    def test_non_bool_limits_keep_the_generic_coercion(self):
        cfg = cfg_with_limits({"ack_ttl_seconds": "120"})
        self.assertEqual(cfg.limits.ack_ttl_seconds, 120.0)
        with self.assertRaises(ConfigError) as caught:
            cfg_with_limits({"ack_ttl_seconds": "abc"})
        self.assertIn("limits.ack_ttl_seconds", str(caught.exception))
        with self.assertRaises(ConfigError) as caught2:
            cfg_with_limits({"max_message_chars": "big"})
        self.assertIn("limits.max_message_chars", str(caught2.exception))


class LoadEnvBomTests(unittest.TestCase):
    def test_bom_in_env_does_not_drop_the_first_key(self):
        """#113: a Notepad-style BOM turned the first key into '\\ufeffKEY'."""
        with TemporaryDirectory() as tmp:
            envp = Path(tmp) / ".env"
            envp.write_bytes(b"\xef\xbb\xbfBOM_PROBE_KEY=value-1\nSECOND_KEY=value-2\n")
            os.environ.pop("BOM_PROBE_KEY", None)
            os.environ.pop("SECOND_KEY", None)
            try:
                load_env([envp])
                self.assertEqual(os.environ.get("BOM_PROBE_KEY"), "value-1")
                self.assertEqual(os.environ.get("SECOND_KEY"), "value-2")
            finally:
                os.environ.pop("BOM_PROBE_KEY", None)
                os.environ.pop("SECOND_KEY", None)
