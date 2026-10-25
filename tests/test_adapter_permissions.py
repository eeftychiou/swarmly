"""adapter.permissions: the strict / standard / full flags in the default command templates."""

import os
import shlex
import tempfile
import textwrap
import unittest
from pathlib import Path

from swarmly.telegram.config import (
    DEFAULT_PERMISSIONS,
    AdapterSettings,
    ConfigError,
    AgentConfig,
)


def _argv(settings: AdapterSettings):
    return shlex.split(settings.command_template(), posix=(os.name != "nt"))


class TestPermissionLevels(unittest.TestCase):

    def test_default_is_full(self):
        # The owner's call: as usable as possible out of the box. The docs say to keep the
        # group and the bus repository private, since anyone who reaches a bot gets a shell.
        self.assertEqual(DEFAULT_PERMISSIONS, "full")
        for kind in ("claude", "claude_thread", "codex", "reasonix"):
            self.assertEqual(AdapterSettings(type=kind).permissions, "full")

    def test_claude_levels(self):
        strict = _argv(AdapterSettings(type="claude", permissions="strict"))
        self.assertEqual(strict, ["claude", "-p", "--output-format", "json",
                                  "--permission-mode", "acceptEdits", "--", "{prompt}"])
        standard = _argv(AdapterSettings(type="claude_thread", permissions="standard"))
        self.assertIn("--allowedTools", standard)
        tools = standard[standard.index("--allowedTools") + 1].split(",")
        self.assertIn("Bash(git:*)", tools)
        self.assertIn("PowerShell(git:*)", tools)
        self.assertIn("Bash(pytest:*)", tools)
        self.assertEqual(standard[-2:], ["--", "{prompt}"])   # the prompt is never a tool name
        full = _argv(AdapterSettings(type="claude", permissions="full"))
        self.assertEqual(full[full.index("--permission-mode") + 1], "bypassPermissions")

    def test_codex_levels(self):
        self.assertEqual(AdapterSettings(type="codex", permissions="strict").command_template(),
                         "codex exec --skip-git-repo-check --sandbox workspace-write {prompt}")
        self.assertEqual(AdapterSettings(type="codex", permissions="standard").command_template(),
                         "codex exec --skip-git-repo-check --sandbox workspace-write "
                         "-c sandbox_workspace_write.network_access=true {prompt}")
        # Unset, Codex runs without its sandbox: inside it git can neither pull nor commit.
        self.assertEqual(AdapterSettings(type="codex").permissions, "full")
        self.assertEqual(AdapterSettings(type="codex", permissions="full").command_template(),
                         "codex exec --skip-git-repo-check "
                         "--dangerously-bypass-approvals-and-sandbox {prompt}")

    def test_types_without_flags_are_unchanged(self):
        for level in ("strict", "standard", "full"):
            self.assertEqual(AdapterSettings(type="hermes", permissions=level).command_template(),
                             "hermes -z {prompt}")
            self.assertIn("--dangerously-skip-permissions",
                          AdapterSettings(type="antigravity", permissions=level).command_template())

    def test_explicit_command_wins(self):
        settings = AdapterSettings(type="claude", permissions="full",
                                   command="claude -p --output-format json -- {prompt}")
        self.assertEqual(settings.command_template(), "claude -p --output-format json -- {prompt}")

    def test_level_is_case_insensitive_and_validated(self):
        self.assertEqual(AdapterSettings(type="codex", permissions=" Full ").permissions, "full")
        with self.assertRaises(ConfigError):
            AdapterSettings(type="codex", permissions="everything")

    def test_allow_commands_extends_claude_standard(self):
        argv = _argv(AdapterSettings(type="claude_thread", permissions="standard",
                                     allow_commands=["gh"]))
        tools = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertIn("Bash(gh:*)", tools)
        self.assertIn("PowerShell(gh:*)", tools)
        self.assertIn("Bash(git:*)", tools)
        self.assertEqual(argv[-2:], ["--", "{prompt}"])
        for level in ("strict", "full"):
            self.assertNotIn("gh:", AdapterSettings(type="claude", permissions=level,
                                                    allow_commands=["gh"]).command_template())
        self.assertNotIn("gh", AdapterSettings(type="codex", allow_commands=["gh"])
                         .command_template())
        custom = "claude -p --output-format json -- {prompt}"
        self.assertEqual(AdapterSettings(type="claude", command=custom, allow_commands=["gh"])
                         .command_template(), custom)

    def test_allow_commands_are_single_names(self):
        for bad in (["gh pr merge"], ["-x"], "gh", ["a;b"], ["*"], ["Bash(curl:*)"],
                    ["gh,curl"], [None], ["gh", 7], [True]):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                AdapterSettings(type="claude", allow_commands=bad)

    def test_read_from_yaml(self):
        body = textwrap.dedent("""\
            agent:
              name: test_bot
              alias: Test
              bot_token: "123:abc"
            adapter:
              type: codex
              permissions: full
            """)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram.yaml"
            path.write_text(body, encoding="utf-8")
            config = AgentConfig.load(path)
        self.assertEqual(config.adapter.permissions, "full")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                      config.adapter.command_template())


if __name__ == "__main__":
    unittest.main()
