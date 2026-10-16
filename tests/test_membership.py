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

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from swarmly.telegram.config import AgentConfig
from swarmly.telegram.membership import (JOIN_WAIT_SECONDS, MEMBERSHIP_MARK, Membership,
                                         SwarmState, parse_fields, safe_bus_location,
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
    channel.config.identity.role = role
    if role == "coordinator":
        m.state.coordinator = channel.config.identity.alias
    return channel


def hello(intent, ref, body, username, author_id, to="*", message_id=7):
    return event(f"[A2A v1.1] HELLO to={to} intent={intent} ref={ref}\n\n{body}",
                 username=username, author_id=author_id, message_id=message_id)


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
            channel = make_channel(config_at(folder))
            run(channel.membership._await_swarm())
            ask = channel._test_sender.last
            self.assertIn("first one in this swarm", ask)
            self.assertIn("/bus", ask)
            self.assertEqual(channel.membership.state.role, "coordinator")
            self.assertEqual(channel.config.identity.role, "coordinator")

            async def bus():
                await channel.handle_event(event("/bus https://github.com/me/swarm-bus.git",
                                                 author_id=ADMIN_ID, username="alice",
                                                 is_bot=False))
                await drain(channel)
            run(bus())
            self.assertEqual(channel.membership.state.bus, "https://github.com/me/swarm-bus.git")
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
            channel = with_admins(make_channel(config_at(folder)),
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
            channel = make_channel(config_at(folder, peers={"Claude": "claude_x_bot"}))
            m = channel.membership
            m.state.bus = "https://github.com/me/swarm-bus.git"
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
            self.assertEqual(channel.config.peer_roles["Codex"], "reviewer")
            self.assertIn("- Codex: reviewer, online", run(channel.membership.status_text()))


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
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
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
            text = config.read_text()
            self.assertIn("  repo: ", text.split("bus:", 1)[1])
            self.assertIn("ref: origin/agents-bus", text)
            shown = subprocess.run(["git", "-C", str(remote), "show",
                                    "agents-bus:config/a2a_config.json"],
                                   capture_output=True, text=True, check=True).stdout
            self.assertEqual(json.loads(shown)["authorized_agents"]["newbie"]
                             ["public_key_or_secret_name"], PUB)
            author = subprocess.run(["git", "-C", str(remote), "log", "-1", "--format=%an",
                                     "agents-bus"], capture_output=True, text=True).stdout
            self.assertEqual(author.strip(), "Newbie")


if __name__ == "__main__":
    unittest.main()
