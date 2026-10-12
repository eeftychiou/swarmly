"""``check`` asks Telegram what only it knows: the token works, it is the configured bot, and
the bot is in the home channel as an admin (or with privacy off). Found in a live test: a bot
missing from the group passed ``check`` and then crashed ``run`` on its online notice."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from swarmly.telegram import cli
from swarmly.telegram.config import AgentConfig


def config():
    return AgentConfig.from_dict({
        "agent": {"name": "deepseek_bot", "alias": "DeepSeek", "bot_token": "1:x",
                  "home_channel_id": -1001234567890, "admin_user_id": 1},
        "adapter": {"type": "echo", "cwd": "."}})


class FakeBot:
    def __init__(self, me, member=None, member_error=None, me_error=None, chat_username=None):
        self._me, self._member = me, member
        self._chat_username = chat_username
        self._member_error, self._me_error = member_error, me_error
        self.session = SimpleNamespace(close=mock.AsyncMock())

    async def get_me(self):
        if self._me_error:
            raise self._me_error
        return self._me

    async def get_chat_member(self, chat_id, user_id):
        if self._member_error:
            raise self._member_error
        return self._member

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, username=self._chat_username)


def me(username="deepseek_bot", reads_all=False):
    return SimpleNamespace(id=7, username=username, can_read_all_group_messages=reads_all)


def run_check(bot):
    problems, notes = [], []
    with mock.patch("aiogram.Bot", return_value=bot):
        line = asyncio.run(cli._check_telegram(config(), problems, notes))
    return line, problems, notes


class TestCheckTelegram(unittest.TestCase):
    def test_an_admin_passes(self):
        line, problems, notes = run_check(FakeBot(me(), SimpleNamespace(status="administrator")))
        self.assertEqual((problems, notes), ([], []))
        self.assertIn("administrator", line)

    def test_a_public_group_and_no_topic_right_are_notes(self):
        member = SimpleNamespace(status="administrator", can_manage_topics=False)
        _, problems, notes = run_check(FakeBot(me(), member, chat_username="examplegroup"))
        self.assertEqual(problems, [])
        self.assertTrue(any("cannot manage topics" in n for n in notes), notes)
        self.assertTrue(any("public (@examplegroup)" in n for n in notes), notes)

    def test_a_bot_missing_from_the_group_is_a_problem(self):
        from aiogram.exceptions import TelegramBadRequest
        error = TelegramBadRequest(method=mock.Mock(), message="Bad Request: chat not found")
        line, problems, _ = run_check(FakeBot(me(), member_error=error))
        self.assertIn("NOT IN THE GROUP", line)
        self.assertIn("not in the home channel", problems[0])

    def test_a_plain_member_with_privacy_on_is_a_problem(self):
        _, problems, _ = run_check(FakeBot(me(reads_all=False), SimpleNamespace(status="member")))
        self.assertIn("make it an admin", problems[0])
        _, problems, _ = run_check(FakeBot(me(reads_all=True), SimpleNamespace(status="member")))
        self.assertEqual(problems, [])

    def test_a_token_for_another_bot_and_a_rejected_token(self):
        from aiogram.exceptions import TelegramUnauthorizedError
        _, problems, _ = run_check(FakeBot(me("other_bot"), SimpleNamespace(status="creator")))
        self.assertIn("@other_bot", problems[0])
        error = TelegramUnauthorizedError(method=mock.Mock(), message="Unauthorized")
        _, problems, _ = run_check(FakeBot(me(), me_error=error))
        self.assertIn("rejects the bot token", problems[0])

    def test_an_anonymous_owner_is_a_problem(self):
        bot = FakeBot(me(), SimpleNamespace(status="administrator"))
        owner = SimpleNamespace(status="creator", is_anonymous=True)
        real = bot.get_chat_member

        async def member(chat_id, user_id):
            return owner if user_id == 1 else await real(chat_id, user_id)
        bot.get_chat_member = member
        _, problems, _ = run_check(bot)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Remain anonymous", problems[0])

    def test_no_network_is_a_note_not_a_problem(self):
        line, problems, notes = run_check(FakeBot(me(), me_error=OSError("no route")))
        self.assertEqual(problems, [])
        self.assertIn("could not ask Telegram", notes[0])
        self.assertEqual(line, "not reachable")

    def test_offline_flag_parses(self):
        self.assertTrue(cli.build_parser().parse_args(["check", "--offline"]).offline)
        self.assertFalse(cli.build_parser().parse_args(["check"]).offline)


class TestStopAfterAFailedStart(unittest.TestCase):
    def test_stop_does_not_mask_the_start_error(self):
        from swarmly.telegram.channel import AgentChannel
        channel = AgentChannel(config())
        channel.dispatcher = SimpleNamespace(
            stop_polling=mock.AsyncMock(side_effect=RuntimeError("Polling is not started")))
        channel.bot = SimpleNamespace(session=SimpleNamespace(close=mock.AsyncMock()))
        asyncio.run(channel.stop())                 # no RuntimeError


if __name__ == "__main__":
    unittest.main()


class TestGhNote(unittest.TestCase):
    """A Claude agent with a role is blocked on gh at standard (live test): check says how
    to opt in, and stays quiet once it has."""

    def output(self, allow):
        import contextlib
        import io
        cfg = AgentConfig.from_dict({
            "agent": {"name": "claude_bot", "alias": "Claude", "bot_token": "1:x",
                      "home_channel_id": -1001234567890, "admin_user_id": 1,
                      "role": "coordinator"},
            "adapter": {"type": "claude", "cwd": ".", "permissions": "standard",
                        "allow_commands": allow}})
        out = io.StringIO()
        # The note sits under NOTES, which a missing CLI would replace (CI has no claude).
        with contextlib.redirect_stdout(out), \
                mock.patch.object(cli, "_missing_program", return_value=None):
            asyncio.run(cli._check(cfg))
        return out.getvalue()

    def test_note_until_gh_is_allowed(self):
        self.assertIn("allow_commands: [gh]", self.output([]))
        self.assertNotIn("allow_commands: [gh]", self.output(["gh"]))
