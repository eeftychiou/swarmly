"""adapter.effort maps one value onto each CLI's reasoning-effort flag; Antigravity takes
adapter.model too."""

import unittest

from swarmly.telegram.config import AdapterSettings, ConfigError


def line(**kw):
    return AdapterSettings(**kw).command_template()


class TestEffort(unittest.TestCase):

    def test_each_cli_gets_its_own_flag(self):
        self.assertIn("-c model_reasoning_effort=high", line(type="codex", effort="high"))
        self.assertIn("--effort medium", line(type="claude", effort="Medium"))
        self.assertIn("--effort max", line(type="antigravity", effort="max"))
        self.assertIn("--effort low", line(type="reasonix", effort="low"))
        self.assertNotIn("effort", line(type="claude"))

    def test_bad_values_are_refused(self):
        for kw in ({"type": "codex", "effort": "max"}, {"type": "claude", "effort": "huge"},
                   {"type": "hermes", "effort": "high"}):
            with self.assertRaises(ConfigError, msg=str(kw)):
                AdapterSettings(**kw)

    def test_antigravity_model(self):
        cmd = line(type="antigravity", model="gemini-3.8-flash-high")
        self.assertIn("--model gemini-3.8-flash-high", cmd)
        self.assertTrue(cmd.startswith("agy -p {prompt}"))
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("{model}", line(type="antigravity"))

    def test_an_explicit_command_wins(self):
        self.assertEqual(line(type="claude", effort="high", command="my-cli {prompt}"),
                         "my-cli {prompt}")


if __name__ == "__main__":
    unittest.main()
