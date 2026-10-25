"""Review P0-6: agent runtimes do not inherit the channel's tokens and keys, and no .env is
read from the working directory."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly.telegram.adapters.base import SubprocessAdapter
from swarmly.telegram.config import AdapterSettings, ConfigError, load_env

PARENT = {
    "PATH": "/usr/bin", "HOME": "/home/agent", "OPENAI_API_KEY": "sk-cli-needs-this",
    "A2A_TELEGRAM_BOT_TOKEN_CODEX": "123:bot", "TELEGRAM_BOT_TOKEN": "456:bot",
    "A2A_KEY_CODEX": "bus-signing-key", "a2a_key_claude": "lower-case-too",
    "GITHUB_TOKEN": "ghp_x",
    "GOOGLE_APPLICATION_CREDENTIALS": "/keys/drive.json", "DB_PASSWORD": "pw",
    "AWS_SECRET_ACCESS_KEY": "aws", "GH_PAT": "pat", "SMTP_PASS": "p", "DB_PWD": "p",
    "A2A_KEY_DIR": "/home/agent/.a2a/keys", "PWD": "/work", "OLDPWD": "/",
}


class Adapter(SubprocessAdapter):
    TYPE = "custom"

    async def respond(self, message):              # pragma: no cover - not run here
        return None


def environment(**settings):
    adapter = Adapter("Codex", AdapterSettings(type="codex", **settings))
    with mock.patch.dict(os.environ, PARENT, clear=True):
        return adapter.environment()


class TestRuntimeEnvironment(unittest.TestCase):

    def test_tokens_and_keys_are_not_inherited(self):
        env = environment()
        for name in ("A2A_TELEGRAM_BOT_TOKEN_CODEX", "TELEGRAM_BOT_TOKEN", "A2A_KEY_CODEX",
                     "a2a_key_claude", "GITHUB_TOKEN",
                     "GOOGLE_APPLICATION_CREDENTIALS", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(name, env)
        self.assertFalse([v for v in env.values() if "bot" in v or "bus-signing" in v])

    def test_what_the_cli_needs_still_passes(self):
        env = environment(extra_env={"CODEX_HOME": "/srv/codex"})
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["OPENAI_API_KEY"], "sk-cli-needs-this")
        self.assertEqual(env["CODEX_HOME"], "/srv/codex")
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Codex")

    def test_pass_env_lets_a_named_variable_through(self):
        env = environment(pass_env=["A2A_TELEGRAM_BOT_TOKEN_CODEX"])   # the doorbell's
        self.assertEqual(env["A2A_TELEGRAM_BOT_TOKEN_CODEX"], "123:bot")
        self.assertNotIn("A2A_KEY_CODEX", env)

    def test_more_password_shapes_are_kept_back(self):
        env = environment()
        for name in ("GH_PAT", "SMTP_PASS", "DB_PWD"):
            self.assertNotIn(name, env)
        # Paths that only look like secrets pass.
        self.assertEqual(env["A2A_KEY_DIR"], "/home/agent/.a2a/keys")
        self.assertEqual(env["PWD"], "/work")
        self.assertEqual(env["OLDPWD"], "/")

    def test_pass_env_from_the_yaml_reaches_the_runtime(self):
        from swarmly.telegram.adapters import build_adapter
        from tests.test_telegram_channel import make_config
        config = make_config(adapter={"type": "codex",
                                      "pass_env": ["A2A_TELEGRAM_BOT_TOKEN_CODEX"]})
        adapter = build_adapter(config)
        with mock.patch.dict(os.environ, PARENT, clear=True):
            env = adapter.environment()
        self.assertEqual(env["A2A_TELEGRAM_BOT_TOKEN_CODEX"], "123:bot")

    def test_handing_over_another_identity_is_logged(self):
        with self.assertLogs("swarmly.telegram.adapter", level="WARNING") as logs:
            Adapter("Codex", AdapterSettings(type="codex",
                                             pass_env=["A2A_KEY_CLAUDE", "A2A_KEY_CODEX"]))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("A2A_KEY_CLAUDE", logs.output[0])

    def test_only_the_exact_own_names_pass_silently(self):
        risky = ["A2A_KEY_REVIEW_CODEX", "A2A_TELEGRAM_BOT_TOKEN_OLD_CODEX",
                 "a2a_key_claude", "telegram_bot_token", "GH_PAT"]
        with self.assertLogs("swarmly.telegram.adapter", level="WARNING") as logs:
            Adapter("Codex", AdapterSettings(type="codex", pass_env=risky + [
                "A2A_KEY_CODEX", "a2a_telegram_bot_token_codex", "OPENAI_BASE_URL"]))
        self.assertEqual(len(logs.output), len(risky), logs.output)
        for name in risky:
            self.assertTrue(any(name in line for line in logs.output), name)

    def test_pass_env_is_validated(self):
        for bad in ("GH_TOKEN", ["GH TOKEN"], ["$(x)"], [3]):
            with self.assertRaises(ConfigError, msg=bad):
                AdapterSettings(type="codex", pass_env=bad)


class TestNoImplicitDotEnv(unittest.TestCase):

    def test_cwd_and_its_parent_are_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            work = parent / "work"
            work.mkdir()
            (work / ".env").write_text("A2A_TEST_FROM_CWD=1\n", encoding="utf-8")
            (parent / ".env").write_text("A2A_TEST_FROM_PARENT=1\n", encoding="utf-8")
            home = parent / "home"
            home.mkdir()
            previous = Path.cwd()
            os.chdir(work)
            try:
                with mock.patch.dict(os.environ, {}, clear=False), \
                        mock.patch("pathlib.Path.home", return_value=home):
                    os.environ.pop("A2A_TEST_FROM_CWD", None)
                    os.environ.pop("A2A_TEST_FROM_PARENT", None)
                    load_env()
                    self.assertNotIn("A2A_TEST_FROM_CWD", os.environ)
                    self.assertNotIn("A2A_TEST_FROM_PARENT", os.environ)
            finally:
                os.chdir(previous)

    def test_the_config_folder_is_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("A2A_TEST_FROM_CONFIG=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch("pathlib.Path.home", return_value=Path(tmp) / "nohome"):
                os.environ.pop("A2A_TEST_FROM_CONFIG", None)
                load_env([tmp])
                self.assertEqual(os.environ.get("A2A_TEST_FROM_CONFIG"), "1")


if __name__ == "__main__":
    unittest.main()
