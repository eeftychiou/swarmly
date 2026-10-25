"""The model and effort each CLI picks by itself, read from its own settings (cli_defaults.py)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib  # noqa: F401
    HAS_TOMLLIB = True
except ImportError:                                     # Python 3.10
    HAS_TOMLLIB = False

from swarmly.telegram.adapters.claude import ClaudeAdapter
from swarmly.telegram.cli_defaults import CliDefault, cli_default, reported_model


class CliDefaultsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.work = Path(self.tmp.name) / "work"
        self.home.mkdir()
        self.work.mkdir()

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_codex_reads_its_config_toml(self):
        self.write(self.home / ".codex" / "config.toml",
                   'model = "gpt-6-sol"\nmodel_reasoning_effort = "xhigh"\n\n[mcp_servers.a2a]\n')
        self.assertEqual(cli_default("codex", env={}, home=self.home),
                         CliDefault("gpt-6-sol", "xhigh"))

    @unittest.skipUnless(HAS_TOMLLIB, "tables need tomllib; 3.10 reads top-level keys only")
    def test_codex_home_and_the_selected_profile_win(self):
        codex_home = Path(self.tmp.name) / "codex-home"
        self.write(codex_home / "config.toml",
                   'model = "gpt-6-sol"\nprofile = "cheap"\n\n'
                   '[profiles.cheap]\nmodel = "gpt-6-luna"\nmodel_reasoning_effort = "low"\n')
        found = cli_default("codex", env={"CODEX_HOME": str(codex_home)}, home=self.home)
        self.assertEqual(found, CliDefault("gpt-6-luna", "low"))

    def test_reasonix_reads_default_model_and_its_default_effort(self):
        self.write(self.home / ".config" / "reasonix" / "config.toml",
                   'default_model = "deepseek-flash"\nmodel_overrides = { "deepseek-flash" = '
                   '{ default_effort = "low" }, "deepseek-v4-pro" = { default_effort = "high" } }\n')
        env = {"XDG_CONFIG_HOME": str(self.home / ".config")}
        if os.name == "nt":
            env["APPDATA"] = str(self.home / ".config")
        found = cli_default("reasonix", str(self.work), env=env, home=self.home)
        self.assertEqual(found.model, "deepseek-flash")
        if found.effort is not None:                       # inline tables need tomllib (3.11+)
            self.assertEqual(found.effort, "low")

    def test_reasonix_finds_the_default_effort_in_its_provider(self):
        # The shipped config.toml keeps model_overrides inside [[providers]].
        self.write(self.home / ".config" / "reasonix" / "config.toml",
                   'default_model = "deepseek-flash"\n\n[[providers]]\nname = "deepseek"\n'
                   'model_overrides = { "deepseek-flash" = { default_effort = "low" } }\n')
        env = {"XDG_CONFIG_HOME": str(self.home / ".config"),
               "APPDATA": str(self.home / ".config")}
        found = cli_default("reasonix", str(self.work), env=env, home=self.home)
        self.assertEqual(found.model, "deepseek-flash")
        if HAS_TOMLLIB:                                     # 3.10 reads top-level keys only
            self.assertEqual(found.effort, "low")

    def test_a_project_reasonix_toml_wins(self):
        self.write(self.home / ".config" / "reasonix" / "config.toml",
                   'default_model = "deepseek-flash"\n')
        self.write(self.work / "reasonix.toml", 'default_model = "deepseek-v4-pro"\n')
        env = {"XDG_CONFIG_HOME": str(self.home / ".config"),
               "APPDATA": str(self.home / ".config")}
        self.assertEqual(cli_default("reasonix", str(self.work), env=env, home=self.home).model,
                         "deepseek-v4-pro")

    def test_claude_reads_the_environment_then_its_settings(self):
        self.write(self.home / ".claude" / "settings.json",
                   json.dumps({"model": "sonnet", "effortLevel": "medium"}))
        self.write(self.work / ".claude" / "settings.json", json.dumps({"model": "opus"}))
        found = cli_default("claude", str(self.work), env={}, home=self.home)
        self.assertEqual(found, CliDefault("opus", "medium"))
        found = cli_default("claude", str(self.work), home=self.home,
                            env={"ANTHROPIC_MODEL": "haiku", "CLAUDE_CODE_EFFORT_LEVEL": "low"})
        self.assertEqual(found, CliDefault("haiku", "low"))

    def test_antigravity_reads_env_and_infers_effort(self):
        """Issue #166: Antigravity defaults from env and tiered model names."""
        found = cli_default("antigravity", env={"ANTIGRAVITY_MODEL": "gemini-3.8-flash-high"}, home=self.home)
        self.assertEqual(found, CliDefault("gemini-3.8-flash-high", "high"))

        found_engine = cli_default("antigravity", env={"AGY_MODEL": "Gemini 3.1 Pro (Medium)"}, home=self.home)
        self.assertEqual(found_engine, CliDefault("Gemini 3.1 Pro (Medium)", "medium"))

        found_explicit = cli_default("antigravity", env={"ANTIGRAVITY_MODEL": "gemini-2.5-pro", "ANTIGRAVITY_EFFORT": "low"}, home=self.home)
        self.assertEqual(found_explicit, CliDefault("gemini-2.5-pro", "low"))

    def test_reasonix_infers_effort_from_model_name(self):
        """Issue #166: Reasonix infers effort when model name carries effort suffix."""
        found = cli_default("reasonix", model="deepseek-v4-pro-high", env={}, home=self.home)
        self.assertEqual(found.effort, "high")

    def test_nothing_found_or_a_broken_file_is_none(self):
        self.write(self.home / ".claude" / "settings.json", "{not json")
        self.assertEqual(cli_default("claude", None, env={}, home=self.home), CliDefault())
        self.assertEqual(cli_default("antigravity", None, env={}, home=self.home), CliDefault())
        self.assertEqual(cli_default("codex", None, env={}, home=self.home), CliDefault())


class ReportedModelTests(unittest.TestCase):

    def test_the_main_model_is_the_one_that_wrote_most(self):
        usage = {"claude-haiku-5-5": {"outputTokens": 40},
                 "claude-opus-5-5": {"outputTokens": 900}}
        self.assertEqual(reported_model(usage), "claude-opus-5-5")
        self.assertIsNone(reported_model({}))
        self.assertIsNone(reported_model(None))

    def test_the_claude_adapter_puts_it_in_the_turn_meta(self):
        out = json.dumps({"type": "result", "subtype": "success", "result": "hi",
                          "session_id": "s", "modelUsage": {"claude-opus-5-5":
                                                            {"outputTokens": 12}}})
        adapter = ClaudeAdapter("Claude", ClaudeAdapter.default_settings())
        reply = adapter.parse_output(out, 1.0)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.meta.get("model"), "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
