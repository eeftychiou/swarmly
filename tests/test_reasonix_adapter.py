"""The Reasonix (DeepSeek) adapter: a one-shot ``reasonix run`` per message, always with a
permission mode (a headless run cannot answer Reasonix's default ``ask``)."""

import unittest
from tempfile import TemporaryDirectory

from swarmly.telegram.adapters import ReasonixAdapter, build_adapter
from swarmly.telegram.config import AgentConfig, model_problem


def config(tmp, **adapter):
    return AgentConfig.from_dict({
        "agent": {"name": "deepseek_bot", "alias": "DeepSeek", "bot_token": "1:x",
                  "home_channel_id": -1, "admin_user_id": 1, "role": "developer"},
        "adapter": {"type": "reasonix", "cwd": tmp, **adapter}})


class TestReasonixAdapter(unittest.TestCase):
    def test_default_command_is_a_headless_run_with_a_permission_mode(self):
        with TemporaryDirectory() as tmp:
            adapter = build_adapter(config(tmp))
            self.assertIsInstance(adapter, ReasonixAdapter)
            argv = adapter.build_argv("-rm everything? no: say hi")
        self.assertEqual(argv[:5], ["reasonix", "run", "-p", "--output-format", "text"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        # The prompt follows '--', so a leading '-' is never read as a flag.
        self.assertEqual(argv[-2:], ["--", "-rm everything? no: say hi"])

    def test_permission_levels_and_model(self):
        expected = {"strict": "acceptEdits", "standard": "auto", "full": "bypassPermissions"}
        for level, mode in expected.items():
            with self.subTest(level=level), TemporaryDirectory() as tmp:
                argv = build_adapter(config(tmp, permissions=level,
                                            model="deepseek-pro")).build_argv("x")
                self.assertEqual(argv[argv.index("--permission-mode") + 1], mode)
                self.assertEqual(argv[argv.index("--model") + 1], "deepseek-pro")
        for ok in ("deepseek-pro", "deepseek-flash", "mimo-pro"):
            self.assertIsNone(model_problem("reasonix", ok), ok)
        for bad in ("two words", "opus", "gpt-5", "DeepSeek-Pro", "claude-opus-4-1"):
            self.assertIsNotNone(model_problem("reasonix", bad), bad)

    def test_a_topic_model_choice_reaches_the_argv(self):
        from swarmly.telegram.adapters.base import IncomingMessage
        calls = []

        class Done:
            returncode = 0

            async def communicate(self):
                return b"ok", b""

        async def runner(argv, cwd, env, timeout):
            calls.append(list(argv))
            return Done()

        import asyncio
        with TemporaryDirectory() as tmp:
            adapter = build_adapter(config(tmp, model="deepseek-flash"), runner=runner)
            asyncio.run(adapter.respond(IncomingMessage(text="hi", author_alias="a",
                                                        model="deepseek-pro")))
            asyncio.run(adapter.respond(IncomingMessage(text="hi", author_alias="a")))
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "deepseek-pro")
        self.assertEqual(calls[1][calls[1].index("--model") + 1], "deepseek-flash")

    def test_allow_commands_changes_nothing(self):
        with TemporaryDirectory() as tmp:
            plain = build_adapter(config(tmp)).build_argv("x")
            extra = build_adapter(config(tmp, allow_commands=["make"])).build_argv("x")
        self.assertEqual(plain, extra)

    def test_the_prompt_carries_the_channel_rules_and_the_role(self):
        from swarmly.telegram.adapters.base import IncomingMessage
        with TemporaryDirectory() as tmp:
            adapter = build_adapter(config(tmp))
            prompt = adapter.build_prompt(IncomingMessage(
                message_id=1, author_alias="Efty", text="add a test"))
        self.assertIn("add a test", prompt)
        self.assertIn("YOUR ROLE IN THE SWARM: developer", prompt)


if __name__ == "__main__":
    unittest.main()
