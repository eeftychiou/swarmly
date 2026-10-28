"""The four registry flags are gone (phase 9c of #260, ``#472``).

``state.lease``, ``state.owed``, ``state.links`` and ``state.routing`` used to pick between a
shadow/legacy side and the registry side. Phase 9c made the registry side the only behaviour,
so the names are no longer settings: a config that still spells one out loads, ignores it, and
logs exactly one warning naming the flag. Never a ``ConfigError`` -- an upgrade and a downgrade
must both get by without editing a config.
"""

import unittest

from swarmly.telegram.config import REMOVED_STATE_FLAGS, AgentConfig, ConfigError
from tests.test_telegram_channel import make_config

#: What each flag used to accept: the shadow side and the registry side.
VALUES = {"lease": ("shadow", "store"), "owed": ("shadow", "store"),
          "links": ("legacy", "registry"), "routing": ("legacy", "registry")}


class TheRemovedFlagTests(unittest.TestCase):
    def test_the_removal_names_all_four(self):
        self.assertEqual(sorted(REMOVED_STATE_FLAGS),
                         sorted(["lease", "owed", "links", "routing"]))

    def test_the_settings_no_longer_carry_them(self):
        config = make_config(state={"backend": "telegram"})
        for flag in REMOVED_STATE_FLAGS:
            self.assertFalse(hasattr(config.state, flag), flag)

    def test_any_value_loads_with_one_warning_each(self):
        """DISCRIMINATING: the warning names the flag, and there is exactly one per flag."""
        for flag in REMOVED_STATE_FLAGS:
            for value in VALUES[flag]:
                with self.subTest(flag=flag, value=value):
                    with self.assertLogs("swarmly.telegram.config", level="WARNING") as said:
                        config = make_config(state={"backend": "telegram", flag: value})
                    self.assertIsInstance(config, AgentConfig)
                    named = [line for line in said.output if f"state.{flag}" in line]
                    self.assertEqual(len(named), 1, said.output)

    def test_a_value_no_build_ever_had_is_ignored_too(self):
        """A typo or a newer build's value is still not an error -- one warning, and on."""
        with self.assertLogs("swarmly.telegram.config", level="WARNING") as said:
            config = make_config(state={"backend": "telegram", "routing": "guesswork"})
        self.assertIsInstance(config, AgentConfig)
        self.assertEqual(sum("state.routing" in line for line in said.output), 1, said.output)

    def test_the_backend_is_still_a_flag(self):
        with self.assertRaises(ConfigError):
            make_config(state={"backend": "gitbus"})

    def test_the_other_state_settings_are_untouched(self):
        with self.assertLogs("swarmly.telegram.config", level="WARNING"):
            config = make_config(state={"backend": "telegram", "heartbeat_minutes": 5,
                                        "dir": "/tmp/bot-state", "lease": "shadow"})
        self.assertEqual(config.state.backend, "telegram")
        self.assertEqual(config.state.mode, "shadow")
        self.assertEqual(config.state.heartbeat_minutes, 5.0)
        self.assertEqual(config.state.dir, "/tmp/bot-state")
        self.assertEqual(config.bus.transport, "git")

    def test_no_state_section_at_all_still_loads(self):
        config = make_config()
        self.assertEqual(config.state.backend, "off")
        for flag in REMOVED_STATE_FLAGS:
            self.assertFalse(hasattr(config.state, flag), flag)


if __name__ == "__main__":
    unittest.main()
