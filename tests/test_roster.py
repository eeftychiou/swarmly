"""#152: bots join the swarm at runtime with the admin's approval."""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from swarmly.telegram.config import AgentConfig
from swarmly.telegram.roster import ADMIT_WINDOW_SECONDS, SYNC_WINDOW_SECONDS, Roster
from tests.test_telegram_channel import (
    ADMIN_ID,
    GROUP,
    event,
    make_channel,
    make_config,
    run,
)


def hello(alias, username, author_id=303):
    return event(f"[A2A v1.1] HELLO to=* intent=ONLINE ref={alias} rev=abc\n\n🟢 {alias} online",
                 author_id=author_id, username=username)


def admin(text, message_id=50):
    return event(text, author_id=ADMIN_ID, username="alice", is_bot=False, message_id=message_id)


def config_at(folder, **peers):
    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": peers,
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0, "selftest_seconds": 0},
    }
    return AgentConfig.from_dict(data, source_path=Path(folder) / "telegram.yaml")


class SwarmBotsAdmitTests(unittest.TestCase):
    def test_the_admin_admits_a_bot_that_said_hello(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            return await channel.handle_event(admin("/admit @grok_x_bot"))
        self.assertIsNone(run(scenario()))
        self.assertEqual(channel.config.peers["Grok"], "grok_x_bot")
        welcome = channel._test_sender.last
        self.assertIn("HELLO to=Grok intent=WELCOME ref=Hermes", welcome)
        self.assertIn("@grok_x_bot", welcome)

    def test_only_the_admin_admits(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(event("/admit @grok_x_bot", author_id=77,
                                             username="mallory", is_bot=False))
            await channel.handle_event(event("/admit @grok_x_bot", message_id=2))   # a bot
        run(scenario())
        self.assertNotIn("Grok", channel.config.peers)

    def test_a_bot_needs_a_hello_before_it_is_admitted(self):
        channel = make_channel()
        run(channel.handle_event(admin("/admit @grok_x_bot")))
        self.assertNotIn("Grok", channel.config.peers)

    def test_a_hello_cannot_take_a_peers_alias_or_come_from_a_person(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Codex", "fake_codex_bot"))
            await channel.handle_event(event("[A2A v1.1] HELLO to=* intent=ONLINE ref=Eve",
                                             username="eve", is_bot=False, author_id=88))
            await channel.handle_event(admin("/admit @fake_codex_bot @eve"))
        run(scenario())
        self.assertEqual(channel.config.peers["Codex"], "CodexBot")
        self.assertEqual(set(channel.config.peers), {"Codex", "Antigravity"})

    def test_an_admit_aimed_at_another_bot_is_not_applied_here(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(admin("/admit@CodexBot @grok_x_bot"))
        run(scenario())
        self.assertNotIn("Grok", channel.config.peers)

    def test_the_newcomer_learns_the_swarm_from_the_welcomes(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder))

            def welcome(alias, username, message_id):
                return event(f"[A2A v1.1] HELLO to=Hermes intent=WELCOME ref={alias}\n\n"
                             f"👋 @HermesBot {alias} has added Hermes as a peer",
                             username=username, author_id=400 + message_id,
                             message_id=message_id)

            async def scenario():
                await channel.handle_event(welcome("Early", "early_bot", 1))   # not admitted
                await channel.handle_event(admin("/admit @HermesBot", message_id=2))
                await channel.handle_event(welcome("Codex", "CodexBot", 3))
                await channel.handle_event(welcome("Claude", "example_claude_bot", 4))
                channel._test_clock["t"] += ADMIT_WINDOW_SECONDS + 1
                await channel.handle_event(welcome("Late", "late_bot", 5))
            run(scenario())
            self.assertEqual(channel.config.peers, {"Codex": "CodexBot", "Claude": "example_claude_bot"})

    def test_the_admin_removes_a_runtime_peer_but_not_a_configured_one(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot"))
            await channel.handle_event(admin("/remove @grok_x_bot @CodexBot", message_id=51))
        run(scenario())
        self.assertEqual(set(channel.config.peers), {"Codex", "Antigravity"})

    def test_the_roster_survives_a_restart_and_config_peers_win(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder, Codex="CodexBot"))

            async def scenario():
                await channel.handle_event(hello("Grok", "grok_x_bot"))
                await channel.handle_event(admin("/admit @grok_x_bot"))
            run(scenario())
            saved = json.loads((Path(folder) / "telegram.roster.json").read_text())
            self.assertEqual(saved["peers"], {"Grok": "grok_x_bot"})

            again = config_at(folder, Codex="CodexBot")
            roster = Roster(again)
            self.assertEqual(again.peers, {"Codex": "CodexBot", "Grok": "grok_x_bot"})
            self.assertEqual(roster.joined, {"Grok": "grok_x_bot"})

            renamed = config_at(folder, Grok="other_grok_bot")      # the config wins
            Roster(renamed)
            self.assertEqual(renamed.peers, {"Grok": "other_grok_bot"})

    def test_sync_catches_up_a_bot_that_missed_an_admit(self):
        channel = make_channel()

        def sync(alias, username, peers, message_id):
            return event(f"[A2A v1.1] HELLO to=* intent=SYNC ref={alias}\n\n"
                         f"🔄 {alias} peers: {peers}", username=username,
                         author_id=500 + message_id, message_id=message_id)

        async def scenario():
            await channel.handle_event(sync("Codex", "CodexBot", "Grok=@grok_x_bot", 1))  # early
            await channel.handle_event(admin("/sync", message_id=2))
            await channel.handle_event(sync("Codex", "CodexBot",
                                            "Hermes=@HermesBot Grok=@grok_x_bot", 3))
            await channel.handle_event(sync("Rogue", "rogue_x_bot", "Evil=@evil_x_bot", 4))
            await channel.handle_event(sync("Codex", "CodexBot", "Antigravity=@other_bot", 5))
            channel._test_clock["t"] += SYNC_WINDOW_SECONDS + 1
            await channel.handle_event(sync("Codex", "CodexBot", "Late=@late_x_bot", 6))
        run(scenario())
        self.assertEqual(channel.config.peers, {"Codex": "CodexBot",
                                                "Antigravity": "AntigravityBot",
                                                "Grok": "grok_x_bot"})
        answer = channel._test_sender.sent[0]["text"]
        self.assertIn("HELLO to=* intent=SYNC ref=Hermes", answer)
        self.assertIn("Codex=@CodexBot Antigravity=@AntigravityBot", answer)

    def test_sync_lists_the_peers_telegram_confirmed_at_join(self):
        # Live run: every bot had joined with `swarmly --cli`, so every answer said
        # "peers: none" and /sync could not catch anyone up.
        channel = make_channel()
        channel.roster._add("Grok", "grok_x_bot", via="admin")
        channel.roster._add("Echo", "echo_x_bot", via="welcome")
        run(channel.handle_event(admin("/sync")))
        answer = channel._test_sender.sent[0]["text"]
        self.assertIn("Grok=@grok_x_bot", answer)
        self.assertNotIn("Echo=", answer)                  # second hand: not passed on

    def test_only_the_admin_starts_a_sync(self):
        channel = make_channel()
        run(channel.handle_event(event("/sync", author_id=77, username="mallory",
                                       is_bot=False)))
        self.assertEqual(channel._test_sender.sent, [])

    def test_status_lists_the_peers(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot"))
            return await channel.handle_event(admin("/status@HermesBot", message_id=52))
        self.assertIn("peers=Codex,Antigravity,Grok", run(scenario()))

    def test_the_online_notice_names_the_alias(self):
        self.assertIn("intent=ONLINE ref=Hermes", make_channel().online_notice())

    def test_the_online_notice_names_the_model_and_effort(self):
        # Admin request: the startup line says what each bot runs, so a costly default
        # model is visible the moment a bot comes online, without /model and /reasoning.
        channel = make_channel(make_config(adapter={"type": "codex", "model": "gpt-6-luna",
                                                    "effort": "medium"}))
        self.assertIn("model: gpt-6-luna, effort: medium", channel.online_notice())
        channel = make_channel(make_config(adapter={"type": "claude"}))
        self.assertIn("model: CLI default, effort: CLI default", channel.online_notice())

    def test_a_runtime_choice_for_general_shows_in_the_online_notice(self):
        channel = make_channel(make_config(adapter={"type": "claude", "model": "opus"}))
        channel._models["general"], channel._efforts["general"] = "sonnet", "low"
        self.assertIn("model: sonnet, effort: low", channel.online_notice())

    def test_an_adapter_without_effort_names_only_the_model(self):
        notice = make_channel().online_notice()            # the echo adapter
        self.assertIn("model: CLI default", notice)
        self.assertNotIn("effort:", notice)


class SwarmBotsReviewTests(unittest.TestCase):
    """Review of #153: what may reach a roster, and what keeps it right."""

    def test_a_post_made_on_behalf_of_a_chat_is_not_a_bot(self):
        channel = make_channel()
        anonymous = hello("Anon", "GroupAnonymousBot", author_id=1087968824)
        anonymous.raw = types.SimpleNamespace(sender_chat=types.SimpleNamespace(id=GROUP))

        async def scenario():
            await channel.handle_event(anonymous)
            await channel.handle_event(admin("/admit @GroupAnonymousBot"))
        run(scenario())
        self.assertNotIn("Anon", channel.config.peers)

    def test_a_forwarded_or_relayed_hello_is_not_a_bots_own_post(self):
        async def relay_then_admit(channel, relayed):
            await channel.handle_event(relayed)
            await channel.handle_event(admin("/admit @grok_x_bot"))

        shapes = {"forward_origin": types.SimpleNamespace(type="user"),   # Bot API >= 7.0
                  "forward_date": 1760029200,                            # before 7.0
                  "is_automatic_forward": True,
                  "via_bot": types.SimpleNamespace(id=77)}
        for field, value in shapes.items():
            with self.subTest(field=field):
                channel = make_channel()
                relayed = hello("Grok", "grok_x_bot")
                relayed.raw = types.SimpleNamespace(**{field: value})
                run(relay_then_admit(channel, relayed))
                self.assertNotIn("Grok", channel.config.peers)

    def test_a_welcome_must_name_this_bot_by_alias_and_username(self):
        channel = make_channel()

        def welcome(to, body, alias, username, message_id):
            return event(f"[A2A v1.1] HELLO to={to} intent=WELCOME ref={alias}\n\n{body}",
                         username=username, author_id=600 + message_id, message_id=message_id)

        async def scenario():
            await channel.handle_event(admin("/admit @HermesBot", message_id=2))
            await channel.handle_event(welcome("*", "@all hi", "Evil", "evil_x_bot", 3))
            await channel.handle_event(welcome("Grok", "@HermesBot hi", "Other", "other_x_bot", 4))
            await channel.handle_event(welcome("Hermes", "hi all", "Bare", "bare_x_bot", 5))
            await channel.handle_event(welcome("Hermes", "@HermesBot hi", "Good", "good_x_bot", 6))
        run(scenario())
        self.assertEqual(set(channel.config.peers), {"Codex", "Antigravity", "Good"})

    def test_peers_learned_second_hand_are_not_passed_on(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(admin("/admit @HermesBot", message_id=2))
            await channel.handle_event(event(
                "[A2A v1.1] HELLO to=Hermes intent=WELCOME ref=Rogue\n\n@HermesBot hi",
                username="rogue_x_bot", author_id=666, message_id=3))
            await channel.handle_event(admin("/sync", message_id=4))
        run(scenario())
        self.assertEqual(channel.config.peers.get("Rogue"), "rogue_x_bot")
        answer = channel._test_sender.sent[-1]["text"]
        self.assertIn("intent=SYNC", answer)
        self.assertNotIn("rogue_x_bot", answer)

    def test_a_removed_bot_comes_back_only_with_a_new_admit(self):
        channel = make_channel()

        def sync(peers, message_id):
            return event(f"[A2A v1.1] HELLO to=* intent=SYNC ref=Codex\n\nCodex peers: {peers}",
                         username="CodexBot", author_id=202, message_id=message_id)

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot"))
            await channel.handle_event(admin("/remove @grok_x_bot", message_id=51))
            await channel.handle_event(admin("/sync", message_id=52))
            await channel.handle_event(sync("Grok=@grok_x_bot", 53))   # a bot that missed it
            self.assertNotIn("Grok", channel.config.peers)
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot", message_id=54))
        run(scenario())
        self.assertEqual(channel.config.peers.get("Grok"), "grok_x_bot")

    def test_a_second_admit_welcomes_a_bot_that_is_already_a_peer(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("Grok", "grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot @CodexBot", message_id=51))
        run(scenario())
        welcomes = [row["text"] for row in channel._test_sender.sent
                    if "intent=WELCOME" in row["text"]]
        self.assertEqual(len(welcomes), 3)
        self.assertIn("HELLO to=Codex intent=WELCOME", welcomes[-1])

    def test_a_malformed_roster_file_does_not_stop_the_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "telegram.roster.json").write_text(json.dumps({"peers": ["Grok"]}))
            channel = make_channel(config_at(folder, Codex="CodexBot"))
            self.assertEqual(channel.config.peers, {"Codex": "CodexBot"})

    def test_a_configured_peer_that_is_also_in_the_roster_file_is_not_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "telegram.roster.json").write_text(
                json.dumps({"peers": {"Grok": "grok_x_bot"}}))
            channel = make_channel(config_at(folder, Grok="grok_x_bot"))
            run(channel.handle_event(admin("/remove @grok_x_bot")))
            self.assertEqual(channel.config.peers, {"Grok": "grok_x_bot"})

    def test_an_alias_cannot_be_another_bots_username(self):
        channel = make_channel()

        async def scenario():
            await channel.handle_event(hello("CodexBot", "grok_x_bot"))
            await channel.handle_event(admin("/admit @grok_x_bot"))
        run(scenario())
        self.assertNotIn("CodexBot", channel.config.peers)

    def test_an_alias_that_is_not_one_token_is_left_out_of_ref(self):
        notice = make_channel(make_config(agent={"alias": "Claude Code"})).online_notice()
        self.assertTrue(notice.startswith("[A2A v1.1] HELLO to=* intent=ONLINE rev="), notice)

    def test_a_peer_list_leaves_out_an_alias_that_is_not_one_token(self):
        channel = make_channel(make_config(peers={"Claude Code": "claude_code_bot"}))
        run(channel.handle_event(admin("/sync")))
        answer = channel._test_sender.sent[-1]["text"]
        self.assertIn("Codex=@CodexBot", answer)
        self.assertNotIn("claude_code_bot", answer)       # a reader would take it as "Code"


if __name__ == "__main__":
    unittest.main()
