"""``bus.transport: telegram``: the Telegram store is the bus, said in one setting (25 Oct)."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from swarmly.telegram.config import AgentConfig, ConfigError
from swarmly.telegram import poll

BASE = {"agent": {"name": "b", "alias": "A", "bot_token": "1:x", "home_channel_id": -1,
                  "admin_user_id": 1},
        "adapter": {"type": "echo"}}


def config(bus, state):
    return AgentConfig.from_dict(dict(BASE, bus=bus, state=state))


class TelegramBusTests(unittest.TestCase):
    def test_no_checkout_no_polling_no_mirror_files(self):
        cfg = config({"transport": "telegram", "repo": "/old/bus", "poll_minutes": 5},
                     {"backend": "telegram"})
        self.assertEqual(cfg.bus.transport, "telegram")
        self.assertIsNone(cfg.bus.repo)
        self.assertIsNone(cfg.fallback.gitbus_root)
        self.assertFalse(cfg.bus.enabled)
        self.assertFalse(cfg.bus.mirror_handoffs)

    def test_it_needs_the_telegram_state_store(self):
        with self.assertRaises(ConfigError):
            config({"transport": "telegram"}, {"backend": "off"})
        with self.assertRaises(ConfigError):
            AgentConfig.from_dict(dict(BASE, bus={"transport": "telegram"}))

    def test_state_dir_beats_any_bus_checkout(self):
        cfg = SimpleNamespace(bus=SimpleNamespace(repo="/srv/bus"), fallback=None,
                              state=SimpleNamespace(dir="/srv/state"), source_path=None)
        self.assertEqual(poll.transport_state_root(cfg), Path("/srv/state"))

    def test_a_member_on_the_telegram_bus_has_joined_with_a_role(self):
        from swarmly.telegram.membership import Membership
        cfg = config({"transport": "telegram"}, {"backend": "telegram"})
        member = Membership.__new__(Membership)
        member.cfg = cfg
        member.state = SimpleNamespace(role="qa-tester", bus=None, bus_ready=False)
        member.dynamic = True
        self.assertTrue(member.bus_ready())
        self.assertTrue(member.joined())


if __name__ == "__main__":
    unittest.main()


class LocalHelpersTests(unittest.TestCase):
    def test_the_round_store_still_builds_on_the_telegram_bus(self):
        """Live, 25 Oct: the owed sweep raised 'unknown bus transport telegram'."""
        import tempfile
        from swarmly.telegram.channel import AgentChannel
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig.from_dict(dict(BASE, bus={"transport": "telegram"},
                                             state={"backend": "telegram", "dir": tmp}))
            channel = AgentChannel(cfg)
            self.assertIsNotNone(channel.round_store())
