"""Dynamic membership: agents join, get a role and a first task over Telegram (no peers or
roles in any config)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import git_env

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.membership import (JOIN_WAIT_SECONDS, MEMBERSHIP_MARK,
                                         NOT_ADMIN_RECHECK_SECONDS, Membership,
                                         SwarmState, is_remote_bus, parse_fields,
                                         safe_bus_location,
                                         swarm_state_path)
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, run

PUB = "q83vEjRWeJCrze8SNFZ4kKvN7xI0VniQq83vEjRWeJA="   # 32 bytes of base64


def config_at(folder, peers=None, role=None, bus=None):
    agent = {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
             "home_channel_id": GROUP, "admin_user_id": ADMIN_ID}
    if role:
        agent["role"] = role
    data = {"agent": agent, "peers": peers or {}, "adapter": {"type": "echo"},
            "limits": {"min_seconds_between_messages": 0, "selftest_seconds": 0,
                       "min_seconds_between_agent_turns": 0}}
    if bus:
        data["bus"] = {"repo": bus}
    return AgentConfig.from_dict(data, source_path=Path(folder) / "config.yaml")


class _Scripted(EchoAdapter):
    """Answers every turn with one fixed text, and remembers the prompts."""

    def __init__(self, text):
        super().__init__("Hermes")
        self.text = text
        self.prompts = []

    async def respond(self, message):
        self.prompts.append(message.text)
        return AdapterReply(text=self.text)


def admins(*ids):
    """A lookup in which these user ids are bot administrators of the group."""
    names = {}

    async def lookup(user_id):
        if user_id in ids:
            return ("administrator", names.get(user_id, ""), True)
        return ("member", "", True)
    lookup.names = names
    return lookup


def with_admins(channel, **bots):
    """Make each ``username=user_id`` a bot admin of the group."""
    lookup = admins(*bots.values())
    for username, user_id in bots.items():
        lookup.names[user_id] = username
    channel.membership.lookup = lookup
    return channel


def joined(channel, role="coordinator", bus="https://example.com/bus.git"):
    m = channel.membership
    m.state.role = role
    m.state.bus = bus
    m.state.bus_ready = True
    channel.config.identity.role = role
    if role == "coordinator":
        m.state.coordinator = channel.config.identity.alias
    return channel


def hello(intent, ref, body, username, author_id, to="*", message_id=7):
    return event(f"[A2A v1.1] HELLO to={to} intent={intent} ref={ref}\n\n{body}",
                 username=username, author_id=author_id, message_id=message_id)


def dynamic(channel):
    """An agent the ``swarmly --cli`` launcher started (it joins by itself)."""
    channel.membership.dynamic = True
    return channel


async def drain(channel):
    for _ in range(5):
        tasks = [t for t in channel._self_tasks if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks)


class ParsingTests(unittest.TestCase):
    def test_fields_and_bus_credentials(self):
        fields = parse_fields("hi\ncli: codex\nkey: abc\nrole: none\nbus: https://x/y.git\n")
        self.assertEqual(fields["cli"], "codex")
        self.assertEqual(fields["bus"], "https://x/y.git")
        self.assertEqual(safe_bus_location("https://bot:ghp_secret@github.com/a/b.git"),
                         "https://github.com/a/b.git")
        self.assertEqual(safe_bus_location("git@github.com:a/b.git"), "git@github.com:a/b.git")
        self.assertEqual(safe_bus_location("ssh://git@host/a/b.git"), "ssh://git@host/a/b.git")
        self.assertEqual(safe_bus_location("ssh://u:pw@host/a.git"), "ssh://host/a.git")
        self.assertTrue(is_remote_bus("git@github.com:a/b.git"))
        self.assertTrue(is_remote_bus("https://github.com/a/b.git"))
        self.assertFalse(is_remote_bus("/home/me/bus"))
        self.assertFalse(is_remote_bus("file:///home/me/bus"))
        self.assertFalse(is_remote_bus("C:/work/bus"))

    def test_a_handle_the_transport_appends_never_joins_the_last_field(self):
        # A member that is not the coordinator names the coordinator by alias, so the
        # transport appends its @handle after the last line, the bus (live test).
        from swarmly.telegram.wire import addressed
        body = ("@newbie_x_bot 👋 Antigravity here.\nrole: reviewer\ncoordinator: Claude\n"
                "bus: https://github.com/me/bus.git")
        sent, _ = addressed(body, "claude_x_bot", alias="Claude")
        self.assertTrue(sent.endswith("bus.git @claude_x_bot"))
        self.assertEqual(parse_fields(sent)["bus"], "https://github.com/me/bus.git")
        members = "members: Claude=@claude_x_bot#401:coordinator Codex=@codex_x_bot#402"
        self.assertEqual(parse_fields(members)["members"], members.split(": ", 1)[1])
        self.assertEqual(parse_fields("bus: git@github.com:me/bus.git @a_bot @b_bot")["bus"],
                         "git@github.com:me/bus.git")

    def test_only_options_before_a_subcommand_start_the_launcher(self):
        from swarmly.swarm.cli import _join_command
        self.assertTrue(_join_command(["--cli", "claude"]))
        self.assertTrue(_join_command(["--as", "x", "--cli=codex"]))
        self.assertFalse(_join_command(["post", "--text", "--cli"]))
        self.assertFalse(_join_command(["--", "--cli"]))


class NewcomerTests(unittest.TestCase):
    def test_a_newcomer_asks_to_join_with_its_runtime_and_key(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder))
            channel.membership.cli = "codex"
            channel.membership.public_key = PUB
            run(channel.membership.post_join())
            sent = channel._test_sender.last
            self.assertIn("HELLO to=* intent=JOIN ref=Hermes", sent)
            self.assertIn("cli: codex", sent)
            self.assertIn(f"key: {PUB}", sent)

    def test_the_first_agent_asks_the_maintainer_for_the_bus_and_coordinates(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(make_channel(config_at(folder)))
            m = channel.membership
            run(m._await_swarm())
            ask = channel._test_sender.last
            self.assertIn("first one in this swarm", ask)
            self.assertIn("/bus@HermesBot", ask)
            self.assertEqual(m.state.role, "coordinator")
            self.assertEqual(channel.config.identity.role, "coordinator")
            checkouts = []

            async def on_bus(location):                 # the launcher, then a restart
                checkouts.append(location)
                m.state.bus_ready = True

            m.on_bus = on_bus

            async def bus():
                await channel.handle_event(event("/bus https://github.com/me/swarm-bus.git",
                                                 author_id=ADMIN_ID, username="alice",
                                                 is_bot=False))
                await drain(channel)
                self.assertFalse([r for r in channel._test_sender.sent
                                  if "received:" in r["text"]])    # not before the restart
                await m.on_start()
                await drain(channel)
            run(bus())
            self.assertEqual(checkouts, ["https://github.com/me/swarm-bus.git"])
            self.assertEqual(m.state.bus, "https://github.com/me/swarm-bus.git")
            # The coordinator's first turn carries its role and asks the maintainer the goal.
            onboarding = [row["text"] for row in channel._test_sender.sent
                          if "received:" in row["text"] and MEMBERSHIP_MARK in row["text"]]
            self.assertTrue(onboarding)
            self.assertIn("YOUR ROLE IN THE SWARM: coordinator", onboarding[-1])
            state = json.loads(swarm_state_path(channel.config).read_text())
            self.assertEqual(state["role"], "coordinator")
            self.assertFalse(state["onboard_pending"])

    def test_the_newcomer_learns_peers_bus_and_coordinator_from_the_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(dynamic(make_channel(config_at(folder))),
                                  claude_x_bot=401, codex_x_bot=402, agy_x_bot=403)
            got = []

            async def on_bus(location):
                got.append(location)
            channel.membership.on_bus = on_bus
            present = hello("PRESENT", "Claude",
                            "@HermesBot 👋 Claude here.\nrole: coordinator\ncoordinator: Claude\n"
                            "bus: https://github.com/me/swarm-bus.git\n"
                            "members: Codex=@codex_x_bot#402:reviewer Fake=@fake_x_bot#999:developer",
                            "claude_x_bot", 401, to="Hermes")
            run(channel.handle_event(present))
            peers = channel.config.peers
            self.assertEqual(peers.get("Claude"), "claude_x_bot")
            self.assertEqual(peers.get("Codex"), "codex_x_bot")
            self.assertNotIn("Fake", peers)               # not an admin of the group
            self.assertEqual(channel.config.peer_roles["Codex"], "reviewer")
            self.assertEqual(channel.membership.state.coordinator, "Claude")
            self.assertEqual(got, ["https://github.com/me/swarm-bus.git"])

    def test_a_present_from_a_bot_that_is_not_an_admin_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(make_channel(config_at(folder)))
            run(channel.handle_event(hello("PRESENT", "Mallory", "role: coordinator\n"
                                           "bus: https://evil/bus.git", "mallory_bot", 666,
                                           to="Hermes")))
            self.assertEqual(channel.config.peers, {})
            self.assertIsNone(channel.membership.state.bus)

    def test_the_coordinators_role_is_applied_and_the_role_onboards(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(make_channel(config_at(folder, peers={"Claude": "claude_x_bot"})))
            m = channel.membership
            m.state.bus = "https://github.com/me/swarm-bus.git"
            m.state.bus_ready = True
            m.state.coordinator = "Claude"
            channel.config.peer_roles["Claude"] = "coordinator"

            async def scenario():
                # Someone else's word is not a role.
                await channel.handle_event(hello("ROLE", "coordinator", "", "codex_x_bot", 402,
                                                 to="Hermes"))
                await channel.handle_event(hello("ROLE", "developer",
                                                 "🎭 @HermesBot you are the swarm's developer.",
                                                 "claude_x_bot", 401, to="Hermes"))
                await drain(channel)
            run(scenario())
            self.assertEqual(m.state.role, "developer")
            self.assertEqual(channel.adapter.role_name, "developer")
            self.assertIn("YOUR ROLE IN THE SWARM: developer", channel.adapter.role_text)
            intro = [row["text"] for row in channel._test_sender.sent
                     if MEMBERSHIP_MARK in row["text"]]
            self.assertTrue(intro and "@claude_x_bot" in intro[-1])
            self.assertIn("ask for your first task", intro[-1])
            # The coordinator's answer (the first task) comes within seconds of the
            # introduction: it passes the bot-to-bot cooldown once, never twice.
            channel.config.limits.min_seconds_between_agent_turns = 60.0
            answer = "@HermesBot welcome, take issue #1"
            ok, reason = channel.should_respond(event(answer, author_id=401,
                                                      username="claude_x_bot"))
            self.assertTrue(ok, reason)
            ok, reason = channel.should_respond(event(answer, author_id=401,
                                                      username="claude_x_bot"))
            self.assertFalse(ok)
            self.assertIn("cooldown", reason)

    def test_the_first_agent_greets_the_maintainer_not_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(dynamic(make_channel(config_at(folder))))
            channel.config.identity.maintainer_username = "alice"
            async def onboard():
                channel.membership._onboard()
                await drain(channel)
            run(onboard())
            greeting = self.reassembled_onboarding(channel)
            self.assertIn("Greet the maintainer (@alice)", greeting)
            self.assertIn("never open with @HermesBot", greeting)

    @staticmethod
    def reassembled_onboarding(channel) -> str:
        """The onboarding message as one string, joined from its split parts (#66).

        The greeting carries the whole role text, so a longer role makes it go out as a split
        reply: part 1 holds the membership mark and the last part the "greet the maintainer"
        hint. Joining on ``split_part`` is what the receiving side does, so the test reads the
        message the way a person does, not just the row that happens to carry the mark. A
        message that went out whole (no split) is returned as it is.
        """
        from swarmly.telegram.channel import split_part
        texts = [row["text"] for row in channel._test_sender.sent]
        pieces = [split_part(text) for text in texts]
        starts = [index for index, piece in enumerate(pieces)
                  if piece is not None and piece.index == 1
                  and MEMBERSHIP_MARK in piece.body]
        if not starts:
            whole = [text for text in texts if MEMBERSHIP_MARK in text]
            return whole[-1] if whole else ""
        start = starts[-1]
        first = pieces[start]
        body = first.body
        for piece in pieces[start + 1:]:
            if piece is None or piece.total != first.total or piece.ref != first.ref:
                break
            body += piece.body
        return body

    def test_a_restart_keeps_the_agreed_role(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(f"agent:\n  name: HermesBot\n  alias: Hermes\n  bot_token: x\n"
                            f"  home_channel_id: {GROUP}\n  admin_user_id: {ADMIN_ID}\n"
                            "adapter:\n  type: echo\n", encoding="utf-8")
            SwarmState(bus="https://x/bus.git", role="reviewer", coordinator="Claude",
                       peer_roles={"Claude": "coordinator"}).save(swarm_state_path(
                           AgentConfig.load(path)))
            Path(folder, "config.roster.json").write_text(json.dumps(
                {"peers": {"Claude": "claude_x_bot"}, "via": {"Claude": "admin"}}))
            config = AgentConfig.load(path)
            self.assertEqual(config.identity.role, "reviewer")
            self.assertEqual(config.peers, {"Claude": "claude_x_bot"})
            self.assertEqual(config.peer_roles, {"Claude": "coordinator"})
            channel = make_channel(config)
            run(channel.membership.on_start())
            self.assertEqual(channel._test_sender.sent, [])      # no JOIN, no questions


class MemberTests(unittest.TestCase):
    def join(self, channel, username="newbie_x_bot", user_id=500, alias="Newbie"):
        return hello("JOIN", alias, f"🙋 {alias} wants to join\ncli: reasonix\nkey: {PUB}",
                     username, user_id, message_id=70)

    def test_a_member_welcomes_an_admin_bot_and_tells_it_the_swarm(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder, bus=folder)),
                                         role="reviewer"), newbie_x_bot=500)
            channel.membership.state.coordinator = "Claude"
            run(channel.handle_event(self.join(channel)))
            self.assertEqual(channel.config.peers["Newbie"], "newbie_x_bot")
            sent = channel._test_sender.sent[-1]
            self.assertIn("HELLO to=Newbie intent=PRESENT ref=Hermes", sent["text"])
            self.assertIn("role: reviewer", sent["text"])
            self.assertIn("coordinator: Claude", sent["text"])
            self.assertIn("bus: https://example.com/bus.git", sent["text"])
            self.assertEqual(sent["reply_to"], 70)
            vouched = json.loads(Path(folder, ".agents/state/vouched-keys.json").read_text())
            self.assertEqual(vouched["newbie"], PUB)

    def test_a_bot_that_is_not_an_admin_waits_for_the_admin(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder))))
            run(channel.handle_event(self.join(channel)))
            self.assertNotIn("Newbie", channel.config.peers)
            self.assertIn("newbie_x_bot", channel.roster.pending)     # /admit still works

    def test_the_coordinator_decides_the_role_in_a_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = _Scripted("ROLE: @newbie_x_bot qa-tester\nThe team has no tester yet.")
            channel = make_channel(config_at(folder, peers={"Codex": "codex_x_bot"}),
                                   adapter=adapter)
            with_admins(joined(channel), newbie_x_bot=500)
            channel.membership._add_peer("Codex", "codex_x_bot", 402, "developer")

            async def scenario():
                await channel.handle_event(self.join(channel))
                await drain(channel)
            run(scenario())
            self.assertIn("A new agent joined the swarm: Newbie", adapter.prompts[0])
            self.assertIn("qa-tester:", adapter.prompts[0])           # the catalogue
            texts = [row["text"] for row in channel._test_sender.sent]
            self.assertIn("members: Codex=@codex_x_bot#402:developer", texts[0])
            role = texts[-1]
            self.assertIn("HELLO to=Newbie intent=ROLE ref=qa-tester", role)
            self.assertIn("no tester yet", role)
            self.assertFalse(any(t.startswith("[Hermes] ROLE:") for t in texts))  # consumed
            self.assertEqual(channel.config.peer_roles["Newbie"], "qa-tester")

    def test_a_coordinator_that_decides_nothing_gives_the_gap_role(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder, peers={"Codex": "codex_x_bot"}),
                                   adapter=_Scripted("Hmm, let me think."))
            channel.config.peer_roles["Codex"] = "developer"
            with_admins(joined(channel), newbie_x_bot=500)

            async def scenario():
                await channel.handle_event(self.join(channel))
                await drain(channel)
            run(scenario())
            self.assertIn("intent=ROLE ref=reviewer", channel._test_sender.last)

    def test_the_admin_overrides_a_role_and_asks_for_the_status(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder, peers={"Codex": "codex_x_bot"})))

            async def scenario():
                await channel.handle_event(event("/role @codex_x_bot researcher",
                                                 author_id=ADMIN_ID, username="alice",
                                                 is_bot=False))
                await channel.handle_event(event("/role @codex_x_bot coordinator",
                                                 author_id=77, username="mallory",
                                                 is_bot=False))
                await channel.handle_event(event("/swarm", author_id=ADMIN_ID,
                                                 username="alice", is_bot=False))
            run(scenario())
            self.assertEqual(channel.config.peer_roles["Codex"], "researcher")
            status = channel._test_sender.last
            self.assertIn("SWARM bus: https://example.com/bus.git", status)
            self.assertIn("- Codex: researcher", status)
            self.assertIn("- Hermes: coordinator, online (me)", status)

    def test_plain_role_still_answers_the_diagnostic(self):
        channel = make_channel()
        run(channel.handle_event(event("/role@HermesBot", author_id=ADMIN_ID, username="alice",
                                       is_bot=False)))
        self.assertIn("ROLE Hermes none", channel._test_sender.last)

    def test_a_bot_that_leaves_the_group_leaves_the_swarm(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder)), role="developer")
            m = channel.membership
            m._add_peer("Claude", "claude_x_bot", 401, "coordinator")
            self.assertIsNone(m.state.coordinator)      # its own word does not make it one
            m._set_peer_role("Claude", "coordinator", coordinator=True)
            self.assertEqual(m.state.coordinator, "Claude")
            run(m.member_left(401, "claude_x_bot"))
            self.assertNotIn("Claude", channel.config.peers)
            self.assertIsNone(m.state.coordinator)
            self.assertIn("/role @bot coordinator", channel._test_sender.last)
            # It can come back: leaving is not a /remove.
            self.assertNotIn("claude_x_bot", channel.roster.removed)

    def test_offline_and_online_are_tracked(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder, peers={"Codex": "codex_x_bot"})))

            async def scenario():
                await channel.handle_event(event("[A2A v1.1] BYE to=* intent=OFFLINE ref=Codex",
                                                 username="codex_x_bot", author_id=402))
                return await channel.membership.status_text()
            self.assertIn("- Codex: no role, offline", run(scenario()))
            run(channel.handle_event(hello("ONLINE", "Codex", "🟢 Codex online (adapter: codex, "
                                           "role: reviewer, rev: abc)", "codex_x_bot", 402)))
            # An online notice is not a role: anyone can write "role: coordinator" in one.
            self.assertNotIn("Codex", channel.config.peer_roles)
            self.assertIn("- Codex: no role, online", run(channel.membership.status_text()))

    def test_a_newcomer_still_joining_does_not_answer_swarm(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(make_channel(config_at(folder)))
            swarm = event("/swarm", author_id=ADMIN_ID, username="alice", is_bot=False)
            run(channel.handle_event(swarm))
            self.assertFalse([r for r in channel._test_sender.sent if "SWARM" in r["text"]])
            # Named, it answers; and once it has joined, it answers like any agent.
            run(channel.handle_event(event("/swarm@HermesBot", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertIn("SWARM", channel._test_sender.last)
            joined(channel, role="developer")
            channel._test_sender.sent.clear()
            run(channel.handle_event(swarm))
            self.assertTrue([r for r in channel._test_sender.sent if "SWARM" in r["text"]])

    def test_a_peer_that_answers_our_online_notice_is_online(self):
        # After a restart the peers that were already running only answer our online
        # notice with SEEN; /swarm showed them as "unknown" (live test).
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder, peers={"Codex": "codex_x_bot"})))
            self.assertIn("- Codex: no role, unknown", run(channel.membership.status_text()))
            run(channel.handle_event(hello("SEEN", "Hermes", "👀 Codex sees Hermes",
                                           "codex_x_bot", 402)))
            self.assertIn("- Codex: no role, online", run(channel.membership.status_text()))


class MissedJoinTests(unittest.TestCase):
    """Live run: Codex was offline while Antigravity and DeepSeek joined, and then ignored
    them as strangers until the admin /admitted them."""

    def test_an_admin_bot_that_joined_while_we_were_offline_becomes_a_peer(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder)), role="developer"),
                                  agy_x_bot=600)
            run(channel.handle_event(hello("ONLINE", "Antigravity", "🟢 Antigravity online",
                                           "agy_x_bot", 600)))
            self.assertEqual(channel.config.peers["Antigravity"], "agy_x_bot")
            self.assertEqual(channel.membership.state.peer_ids["Antigravity"], 600)
            self.assertEqual(channel.roster.via["Antigravity"], "admin")
            self.assertNotIn("agy_x_bot", channel.roster.pending)
            self.assertIn("- Antigravity: no role, online",
                          run(channel.membership.status_text()))

    def test_a_seen_answer_from_a_missed_member_adds_it_by_its_banner(self):
        # We restarted while it kept running: its SEEN answer is all we hear from it.
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder)), role="developer"),
                                  deepseek_x_bot=601)
            seen = event("[DeepSeek] [A2A v1.1] HELLO to=* intent=SEEN ref=Hermes\n\n"
                         "👀 DeepSeek sees Hermes", username="deepseek_x_bot", author_id=601)
            run(channel.handle_event(seen))
            self.assertEqual(channel.config.peers["DeepSeek"], "deepseek_x_bot")

    def test_a_bot_that_is_not_an_admin_still_waits_for_the_admin(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder)), role="developer"))
            run(channel.handle_event(hello("ONLINE", "Rogue", "🟢 Rogue online",
                                           "rogue_x_bot", 666)))
            self.assertNotIn("Rogue", channel.config.peers)
            self.assertIn("rogue_x_bot", channel.roster.pending)      # /admit still works

    def test_a_removed_bot_does_not_come_back_by_saying_it_is_online(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder)), role="developer"),
                                  agy_x_bot=600)
            channel.roster.remove(["agy_x_bot"])
            run(channel.handle_event(hello("ONLINE", "Antigravity", "🟢 Antigravity online",
                                           "agy_x_bot", 600)))
            self.assertNotIn("Antigravity", channel.config.peers)

    def test_an_agent_still_joining_adds_nobody_from_an_online_notice(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(dynamic(make_channel(config_at(folder))), agy_x_bot=600)
            run(channel.handle_event(hello("ONLINE", "Antigravity", "🟢 Antigravity online",
                                           "agy_x_bot", 600)))
            self.assertNotIn("Antigravity", channel.config.peers)


class LateAdminTests(unittest.TestCase):
    """Live run: the owner made a newcomer an admin only after it had posted its JOIN; every
    transport kept Telegram's first "not an admin" until it restarted, and the newcomer,
    unanswered, made itself the coordinator of a swarm that already had one."""

    def promoted_later(self, channel):
        """A lookup in which @newbie_x_bot (500) becomes an admin when ``promote()`` is called."""
        admins = set()

        async def lookup(user_id):
            if user_id in admins:
                return ("administrator", "newbie_x_bot", True)
            return ("member", "newbie_x_bot", True)
        channel.membership.lookup = lookup
        return lambda: admins.add(500)

    def test_a_join_from_a_bot_made_admin_later_is_answered_when_it_speaks_again(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder)))
            promote = self.promoted_later(channel)
            join = hello("JOIN", "Newbie", f"🙋 Newbie wants to join\ncli: reasonix\nkey: {PUB}",
                         "newbie_x_bot", 500, message_id=70)
            plea = event("[Newbie] 🙋 Newbie here. No agent answered, so I am the first one in "
                         "this swarm. Where is the bus?", username="newbie_x_bot",
                         author_id=500, message_id=71)
            run(channel.handle_event(join))
            self.assertNotIn("Newbie", channel.config.peers)
            promote()
            run(channel.handle_event(plea))           # Telegram's "no" still stands
            self.assertNotIn("Newbie", channel.config.peers)
            channel._test_clock["t"] += NOT_ADMIN_RECHECK_SECONDS + 1
            run(channel.handle_event(plea))
            self.assertEqual(channel.config.peers["Newbie"], "newbie_x_bot")
            present = [r for r in channel._test_sender.sent if "intent=PRESENT" in r["text"]]
            self.assertEqual(len(present), 1)
            self.assertEqual(present[0]["reply_to"], 70)          # the answer to its JOIN
            self.assertIn("bus: https://example.com/bus.git", present[0]["text"])

    def test_a_join_is_rechecked_even_if_the_newcomer_falls_silent(self):
        # The owner promotes it after its last post: nobody would ever ask Telegram again.
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder)))
            promote = self.promoted_later(channel)
            clock = channel._test_clock

            async def sleeper(seconds):
                clock["t"] += seconds
                promote()                             # made an admin while nobody posts
            channel._sleep = sleeper

            async def scenario():
                await channel.handle_event(hello("JOIN", "Newbie", "🙋 Newbie wants to join",
                                                 "newbie_x_bot", 500, message_id=70))
                await asyncio.gather(*channel.membership._tasks)
            run(scenario())
            self.assertEqual(channel.config.peers["Newbie"], "newbie_x_bot")
            present = [r for r in channel._test_sender.sent if "intent=PRESENT" in r["text"]]
            self.assertEqual([r["reply_to"] for r in present], [70])

    def test_a_bot_that_is_still_not_an_admin_stays_out(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = joined(make_channel(config_at(folder)))
            self.promoted_later(channel)                          # never promoted
            run(channel.handle_event(hello("JOIN", "Newbie", "🙋 Newbie wants to join",
                                           "newbie_x_bot", 500, message_id=70)))
            channel._test_clock["t"] += NOT_ADMIN_RECHECK_SECONDS + 1
            run(channel.handle_event(event("[Newbie] where is the bus?",
                                           username="newbie_x_bot", author_id=500)))
            self.assertNotIn("Newbie", channel.config.peers)
            self.assertIn("newbie_x_bot", channel.roster.pending)     # /admit still works

    def test_a_self_appointed_coordinator_steps_down_for_the_swarms(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(dynamic(make_channel(config_at(folder))), claude_x_bot=401)
            m = channel.membership
            m.set_own_role("coordinator", by="default: the first agent")
            m.state.self_appointed = True
            got = []

            async def on_bus(location):
                got.append(location)
            m.on_bus = on_bus
            run(channel.handle_event(hello(
                "PRESENT", "Claude", "@HermesBot 👋 Claude here.\nrole: coordinator\n"
                "coordinator: Claude\nbus: https://github.com/me/swarm-bus.git",
                "claude_x_bot", 401, to="Hermes")))
            self.assertEqual(m.state.coordinator, "Claude")
            self.assertIsNone(m.state.role)                   # the coordinator decides it
            self.assertFalse(m.state.self_appointed)
            self.assertEqual(got, ["https://github.com/me/swarm-bus.git"])

    def test_a_coordinator_the_admin_named_does_not_step_down(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(dynamic(make_channel(config_at(folder))), claude_x_bot=401)
            m = channel.membership
            m.set_own_role("coordinator", by="admin")
            run(channel.handle_event(hello(
                "PRESENT", "Claude", "@HermesBot 👋 Claude here.\nrole: coordinator\n"
                "coordinator: Claude", "claude_x_bot", 401, to="Hermes")))
            self.assertEqual(m.state.role, "coordinator")
            self.assertEqual(m.state.coordinator, "Hermes")


class TrustTests(unittest.TestCase):
    """Who may make whom what: review findings on PR #3."""

    def coordinator(self, folder, adapter=None):
        channel = make_channel(config_at(folder, peers={"Codex": "codex_x_bot"}),
                               adapter=adapter)
        joined(channel)
        channel.membership._add_peer("Codex", "codex_x_bot", 402, "developer")
        return channel

    def test_a_role_line_outside_the_deciding_turn_assigns_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = self.coordinator(folder, adapter=_Scripted(
                "The protocol says: ROLE: @codex_x_bot researcher"))
            run(channel.handle_event(event("@HermesBot how do roles work?", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertTrue(channel.adapter.prompts)    # the turn ran
            self.assertEqual(channel.config.peer_roles["Codex"], "developer")
            self.assertFalse(any("intent=ROLE" in r["text"] for r in channel._test_sender.sent))

    def test_the_deciding_turn_only_assigns_the_newcomer(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = self.coordinator(folder, adapter=_Scripted(
                "ROLE: @codex_x_bot researcher\nROLE: @newbie_x_bot coordinator"))
            with_admins(channel, newbie_x_bot=500)

            async def scenario():
                await channel.handle_event(MemberTests().join(channel))
                await drain(channel)
            run(scenario())
            self.assertEqual(channel.config.peer_roles["Codex"], "developer")
            # A coordinator cannot hand out its own role: the newcomer gets the gap role.
            self.assertEqual(channel.config.peer_roles["Newbie"], "reviewer")
            self.assertEqual(channel.membership.state.coordinator, "Hermes")

    def test_only_the_coordinator_gives_roles_and_never_the_coordinator_role(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(make_channel(config_at(folder, peers={
                "Claude": "claude_x_bot", "Codex": "codex_x_bot"})))
            m = channel.membership
            m.state.bus, m.state.bus_ready = "https://x/bus.git", True
            m._set_peer_role("Claude", "coordinator", coordinator=True)

            async def scenario():
                # Codex says it coordinates now, then hands out roles: no effect.
                await channel.handle_event(hello("ONLINE", "Codex", "🟢 Codex online (role: "
                                                 "coordinator)", "codex_x_bot", 402))
                await channel.handle_event(hello("ROLE", "researcher", "", "codex_x_bot", 402,
                                                 to="Hermes"))
                # The coordinator cannot make anyone coordinator either.
                await channel.handle_event(hello("ROLE", "coordinator", "", "claude_x_bot",
                                                 401, to="Hermes"))
                # A PRESENT that names another coordinator, once joined: ignored.
                await channel.handle_event(hello("PRESENT", "Codex", "role: coordinator\n"
                                                 "coordinator: Codex", "codex_x_bot", 402,
                                                 to="Hermes"))
            run(scenario())
            self.assertIsNone(m.state.role)
            self.assertEqual(m.state.coordinator, "Claude")
            run(channel.handle_event(hello("ROLE", "developer", "", "claude_x_bot", 401,
                                           to="Hermes")))
            self.assertEqual(m.state.role, "developer")
            # The coordinator switches it later too (#189: the swarm heals itself).
            run(channel.handle_event(hello("ROLE", "reviewer", "", "claude_x_bot", 401,
                                           to="Hermes")))
            self.assertEqual(m.state.role, "reviewer")
            # Never the coordinator's own role.
            run(channel.handle_event(hello("ROLE", "developer", "", "claude_x_bot", 401,
                                           to="Claude")))
            self.assertEqual(channel.config.peer_roles["Claude"], "coordinator")

    def test_the_coordinator_switches_a_role_from_any_reply(self):
        """23 Oct: the reviewer slot was empty after the drill and only the admin could fill
        it. A ROLE line in any coordinator reply now does, and the line is not posted."""
        with tempfile.TemporaryDirectory() as folder:
            channel = self.coordinator(folder, adapter=_Scripted(
                "#209 has no reviewer.\nROLE: @codex_x_bot reviewer: the reviewer is frozen\n"
                "Codex, please review #209."))
            run(channel.handle_event(event("@HermesBot who reviews #209?", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertEqual(channel.config.peer_roles["Codex"], "reviewer")
            texts = [r["text"] for r in channel._test_sender.sent]
            change = next(t for t in texts if "intent=ROLE" in t)
            self.assertIn("HELLO to=Codex intent=ROLE ref=reviewer", change)
            self.assertIn("the reviewer is frozen", change)
            self.assertFalse(any("ROLE: @codex_x_bot" in t for t in texts))
            self.assertTrue(any("please review #209" in t for t in texts))

    def test_the_acting_coordinator_switches_roles_too(self):
        """Review on #212: the named coordinator is down and another bot holds the lease; its
        ROLE must land, or the two bots disagree about the roster."""
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(make_channel(config_at(folder, peers={
                "Claude": "claude_x_bot", "Reasonix": "reasonix_x_bot"})))
            m = channel.membership
            m.state.bus, m.state.bus_ready, m.state.role = "https://x/bus.git", True, "developer"
            m._set_peer_role("Claude", "coordinator", coordinator=True)
            m._set_peer_role("Reasonix", "reviewer")
            channel.current_coordinator = lambda now=None: "Reasonix"
            run(channel.handle_event(hello("ROLE", "reviewer", "", "reasonix_x_bot", 403,
                                           to="Hermes")))
            self.assertEqual(m.state.role, "reviewer")
            channel.current_coordinator = lambda now=None: "Claude"
            run(channel.handle_event(hello("ROLE", "researcher", "", "reasonix_x_bot", 403,
                                           to="Hermes")))
            self.assertEqual(m.state.role, "reviewer")   # no lease, not named: ignored

    def test_one_reply_switches_at_most_two_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder, peers={
                "Codex": "codex_x_bot", "Gem": "gem_x_bot", "Rex": "rex_x_bot"}),
                adapter=_Scripted("ROLE: @codex_x_bot reviewer\nROLE: @gem_x_bot qa-tester\n"
                                  "ROLE: @rex_x_bot researcher"))
            joined(channel)
            for alias, user, uid in (("Codex", "codex_x_bot", 402), ("Gem", "gem_x_bot", 404),
                                     ("Rex", "rex_x_bot", 405)):
                channel.membership._add_peer(alias, user, uid, "developer")
            run(channel.handle_event(event("@HermesBot fill the gaps", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            roles = channel.config.peer_roles
            self.assertEqual((roles["Codex"], roles["Gem"], roles["Rex"]),
                             ("reviewer", "qa-tester", "developer"))

    def test_a_peers_role_line_switches_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder, peers={"Codex": "codex_x_bot"}),
                                   adapter=_Scripted("ROLE: @codex_x_bot researcher"))
            joined(channel, role="developer")
            channel.membership.state.coordinator = "Claude"
            channel.membership._add_peer("Codex", "codex_x_bot", 402, "developer")
            run(channel.handle_event(event("@HermesBot hi", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertEqual(channel.config.peer_roles["Codex"], "developer")
            self.assertFalse(any("intent=ROLE" in r["text"] for r in channel._test_sender.sent))

    def test_the_coordinator_cannot_switch_the_coordinator_role(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = self.coordinator(folder, adapter=_Scripted(
                "ROLE: @codex_x_bot coordinator\nROLE: @HermesBot developer"))
            run(channel.handle_event(event("@HermesBot hi", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertEqual(channel.config.peer_roles["Codex"], "developer")
            self.assertEqual(channel.membership.state.role, "coordinator")
            self.assertFalse(any("intent=ROLE" in r["text"] for r in channel._test_sender.sent))

    def test_a_hand_written_config_never_joins_by_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = make_channel(config_at(folder, bus=folder))
            run(channel.membership.on_start())
            self.assertEqual(channel._test_sender.sent, [])
            self.assertIsNone(channel.membership.state.role)
            run(channel.handle_event(event("/bus https://evil/bus.git", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertIsNone(channel.membership.state.bus)

    def test_the_admin_moves_the_coordinator_and_the_old_one_steps_down(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = self.coordinator(folder)
            m = channel.membership
            run(channel.handle_event(event("/role @codex_x_bot coordinator", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertEqual(channel.config.peer_roles["Codex"], "coordinator")
            self.assertEqual(m.state.coordinator, "Codex")
            self.assertEqual(m.state.role, "developer")
            self.assertFalse(m.is_coordinator())

    def test_a_working_bus_moves_only_with_an_addressed_bus_command(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(joined(make_channel(config_at(folder))))
            m = channel.membership
            moved = []

            async def on_bus(location):
                moved.append(location)
            m.on_bus = on_bus
            run(channel.handle_event(event("/bus https://other/bus.git", author_id=ADMIN_ID,
                                           username="alice", is_bot=False)))
            self.assertEqual(moved, [])
            run(channel.handle_event(event("/bus@HermesBot https://other/bus.git",
                                           author_id=ADMIN_ID, username="alice", is_bot=False)))
            self.assertEqual(moved, ["https://other/bus.git"])

    def test_a_folder_bus_from_a_peer_is_not_taken(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(dynamic(make_channel(config_at(folder))), claude_x_bot=401)
            got = []

            async def on_bus(location):
                got.append(location)
            channel.membership.on_bus = on_bus
            run(channel.handle_event(hello("PRESENT", "Claude", "role: coordinator\n"
                                           "coordinator: Claude\nbus: /home/claude/bus",
                                           "claude_x_bot", 401, to="Hermes")))
            self.assertEqual(got, [])
            self.assertIsNone(channel.membership.state.bus)
            self.assertEqual(channel.membership.state.coordinator, "Claude")

    def test_a_role_given_before_the_bus_is_ready_onboards_after_the_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = dynamic(make_channel(config_at(folder, peers={"Claude": "claude_x_bot"})))
            m = channel.membership
            m.state.bus = "https://x/bus.git"            # adopted, checkout still running
            m._set_peer_role("Claude", "coordinator", coordinator=True)

            async def scenario():
                await channel.handle_event(hello("ROLE", "developer", "", "claude_x_bot", 401,
                                                 to="Hermes"))
                await drain(channel)
            run(scenario())
            self.assertTrue(m.state.onboard_pending)
            self.assertFalse([r for r in channel._test_sender.sent if MEMBERSHIP_MARK in r["text"]])
            m.state.bus_ready = True                    # the launcher's restart

            async def restart():
                await m.on_start()
                await drain(channel)
            run(restart())
            self.assertTrue([r for r in channel._test_sender.sent if MEMBERSHIP_MARK in r["text"]])
            self.assertFalse(m.state.onboard_pending)

    def test_leaving_drops_the_vouched_key(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = with_admins(joined(make_channel(config_at(folder, bus=folder)),
                                         role="reviewer"), newbie_x_bot=500)
            run(channel.handle_event(MemberTests().join(channel)))
            keys = Path(folder, ".agents/state/vouched-keys.json")
            self.assertIn("newbie", json.loads(keys.read_text()))
            run(channel.membership.member_left(500, "newbie_x_bot"))
            self.assertNotIn("newbie", json.loads(keys.read_text()))
            self.assertNotIn(500, channel.membership._verified)


class VouchedKeyTests(unittest.TestCase):
    def test_a_vouched_key_verifies_until_the_registry_has_one(self):
        from swarmly.bus import DurableBus, vouch_key
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            registry = {"authorized_agents": {"codex": {
                "public_key_or_secret_name": PUB.replace("q", "r"), "key_type": "ed25519"}}}
            (root / "config" / "a2a_config.json").write_text(json.dumps(registry))
            self.assertTrue(vouch_key(root, "newbie", PUB))
            self.assertTrue(vouch_key(root, "codex", PUB))
            self.assertFalse(vouch_key(root, "bad", "not a key"))
            bus = DurableBus(root_dir=root, create_dirs=False)
            self.assertEqual(bus.verification_key("newbie"), (PUB, "ed25519"))
            # The registry wins: a JOIN cannot replace a registered key.
            self.assertEqual(bus.verification_key("codex")[0], PUB.replace("q", "r"))


class LauncherTests(unittest.TestCase):
    def test_alias_and_generated_config(self):
        from swarmly.telegram.join import derive_alias, render_config
        self.assertEqual(derive_alias("Claude Bot", "claude_x_bot"), "Claude")
        self.assertEqual(derive_alias("🤖", "deepseek_x_bot"), "Deepseek_x")
        self.assertEqual(derive_alias("Claudebot", "Claude_obot"), "Claude")
        self.assertEqual(derive_alias("CodexBot", "codex_dd_bot"), "Codex")
        self.assertEqual(derive_alias("Robot", "robot_x_bot"), "Robot")
        text = render_config("claude_x_bot", "Claude", "claude", GROUP, ADMIN_ID,
                             Path("/work/swarmly"))
        self.assertNotIn("peers", text.split("#", 3)[-1])
        self.assertNotIn("role:", text)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            config = AgentConfig.load(path)
            self.assertEqual(config.adapter.type, "claude")
            self.assertEqual(config.identity.home_channel_id, GROUP)
            self.assertEqual(config.peers, {})

    def test_swarmly_cli_routes_to_the_launcher(self):
        from swarmly.swarm import cli
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, HOME=folder, USERPROFILE=folder)
            env.pop("SWARMLY_BOT_TOKEN", None)
            out = subprocess.run(
                [os.sys.executable, "-c", "from swarmly.swarm.cli import main; "
                 "raise SystemExit(main(['--cli', 'codex']))"],
                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("--bot-token is required", out.stderr)
        self.assertTrue(callable(cli.main))

    def test_setup_bus_clones_registers_and_pushes(self):
        from swarmly.telegram.join import setup_bus

        def git(*args, cwd=None):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                           env=git_env())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote = root / "remote.git"
            git("init", "--bare", "-q", "-b", "main", str(remote))
            seed = root / "seed"
            git("clone", "-q", str(remote), str(seed))
            (seed / "README.md").write_text("bus\n")
            git("-c", "user.name=t", "-c", "user.email=t@t", "-C", str(seed), "add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@t", "-C", str(seed), "commit", "-qm",
                "init")
            git("-C", str(seed), "push", "-q", "origin", "main")
            home = root / "home"
            home.mkdir()
            config = home / "config.yaml"
            config.write_text("agent:\n  alias: Newbie\nadapter:\n  type: echo\n")
            lines = setup_bus(config, str(remote), "Newbie", PUB)
            self.assertTrue(any("pushed" in line for line in lines), lines)
            # The registry push published the branch: nobody is asked to push it by hand.
            self.assertFalse([line for line in lines if "push the new bus branch" in line])
            text = config.read_text()
            self.assertIn("  repo: ", text.split("bus:", 1)[1])
            self.assertIn("ref: origin/agents-bus", text)
            shown = subprocess.run(["git", "-C", str(remote), "show",
                                    "agents-bus:config/a2a_config.json"],
                                   capture_output=True, text=True, check=True,
                                   env=git_env()).stdout
            self.assertEqual(json.loads(shown)["authorized_agents"]["newbie"]
                             ["public_key_or_secret_name"], PUB)
            author = subprocess.run(["git", "-C", str(remote), "log", "-1", "--format=%an",
                                     "agents-bus"], capture_output=True, text=True,
                                    env=git_env()).stdout
            self.assertEqual(author.strip(), "Newbie")

    def remote_with_main(self, root, name):
        def git(*args):
            subprocess.run(["git", *args], check=True, capture_output=True, env=git_env())
        remote = root / f"{name}.git"
        git("init", "--bare", "-q", "-b", "main", str(remote))
        seed = root / f"{name}-seed"
        git("clone", "-q", str(remote), str(seed))
        (seed / "README.md").write_text("bus\n")
        git("-C", str(seed), "add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "-C", str(seed), "commit", "-qm", "i")
        git("-C", str(seed), "push", "-q", "origin", "main")
        return remote

    def registered(self, remote):
        shown = subprocess.run(["git", "-C", str(remote), "show",
                                "agents-bus:config/a2a_config.json"],
                               capture_output=True, text=True, check=True,
                               env=git_env()).stdout
        return set(json.loads(shown)["authorized_agents"])

    def test_two_agents_registering_at_once_both_land(self):
        from swarmly.telegram.join import setup_bus
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote = self.remote_with_main(root, "remote")
            homes = []
            for alias in ("Alpha", "Beta"):
                home = root / alias
                home.mkdir()
                (home / "config.yaml").write_text(f"agent:\n  alias: {alias}\n")
                homes.append(home)
            setup_bus(homes[0] / "config.yaml", str(remote), "Alpha", PUB)
            # Beta's checkout predates Alpha's push: its own push is refused first.
            from swarmly.telegram import join
            real = join._commit_registry

            def stale(repo, alias, key):
                subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", "HEAD~1"],
                               check=True, capture_output=True, env=git_env())
                registry = repo / "config" / "a2a_config.json"
                data = json.loads(registry.read_text()) if registry.exists() else {}
                data.setdefault("authorized_agents", {}).pop("alpha", None)
                data["authorized_agents"]["beta"] = {"public_key_or_secret_name": key,
                                                     "key_type": "ed25519"}
                registry.parent.mkdir(parents=True, exist_ok=True)
                registry.write_text(json.dumps(data))
                return real(repo, alias, key)
            join._commit_registry = stale
            try:
                lines = setup_bus(homes[1] / "config.yaml", str(remote), "Beta", PUB)
            finally:
                join._commit_registry = real
            self.assertTrue(any("pushed" in line for line in lines), lines)
            self.assertEqual(self.registered(remote) >= {"alpha", "beta"}, True)

    def test_a_new_bus_location_gets_a_clone_of_its_own(self):
        from swarmly.telegram.join import setup_bus
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = (self.remote_with_main(root, n) for n in ("one", "two"))
            home = root / "home"
            home.mkdir()
            config = home / "config.yaml"
            config.write_text("agent:\n  alias: Newbie\n")
            setup_bus(config, str(first), "Newbie", PUB)
            setup_bus(config, str(second), "Newbie", PUB)
            self.assertIn("newbie", self.registered(second))
            self.assertIn("bus-", config.read_text())

    def test_a_restart_keeps_the_maintainers_edits(self):
        import argparse

        from swarmly.telegram import join
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)

            async def identity(token):
                return "claude_x_bot", "Claude", 401
            real, join.bot_identity = join.bot_identity, identity
            try:
                args = join.build_parser().parse_args([
                    "--cli", "claude", "--bot-token", "1:A", "--admin-userid", str(ADMIN_ID),
                    "--home-channel", str(GROUP), "--home", str(home), "--workdir", folder])
                path, _ = join.prepare(args)
                path.write_text(path.read_text() + "limits:\n  max_turns_per_hour: 7\n")
                again = argparse.Namespace(**{**vars(args), "admin_userid": None,
                                              "home_channel": None, "workdir": None,
                                              "model": "opus"})
                join.prepare(again)
            finally:
                join.bot_identity = real
            text = path.read_text()
            self.assertIn("max_turns_per_hour: 7", text)
            self.assertIn("model: opus", text)
            self.assertIn(f"admin_user_id: {ADMIN_ID}", text)


if __name__ == "__main__":
    unittest.main()
