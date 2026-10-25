"""``swarmly-telegram init``: a pip install has no checkout to copy the example config from,
so the template ships in the package and ``init`` writes a config that ``check`` accepts once
the human fills in what only they know."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.config import DEFAULT_COMMANDS, AgentConfig
from swarmly.telegram.init import ADAPTER_CHOICES, InitError, render_config, write_starter

ROOT = Path(__file__).resolve().parents[1]


class TestTemplate(unittest.TestCase):
    def test_packaged_template_matches_the_repository_example(self):
        """The two copies must not drift: docs point at config/, init reads the package."""
        packaged = ROOT / "swarmly" / "telegram" / "templates" / "config.example.yaml"
        self.assertEqual(packaged.read_text(encoding="utf-8"),
                         (ROOT / "config" / "telegram.example.yaml").read_text(encoding="utf-8"))

    def test_packaged_registry_has_no_agents(self):
        registry = ROOT / "swarmly" / "telegram" / "templates" / "a2a_config.json"
        self.assertEqual(json.loads(registry.read_text(encoding="utf-8"))["authorized_agents"],
                         {})


class TestRender(unittest.TestCase):
    def load(self, text, tmp):
        path = Path(tmp, "config.yaml")
        path.write_text(text, encoding="utf-8")
        return AgentConfig.load(path)

    def test_values_land_in_the_config(self):
        with TemporaryDirectory() as tmp:
            config = self.load(render_config("Codex", "codex", Path(tmp), None), tmp)
        self.assertEqual(config.identity.alias, "Codex")
        self.assertEqual(config.adapter.type, "codex")
        self.assertEqual(Path(config.adapter.cwd), Path(tmp))
        self.assertNotIn("Codex", config.peers)            # not its own peer
        self.assertIn("Hermes", config.peers)

    def test_every_adapter_renders_a_loadable_config(self):
        for adapter in ADAPTER_CHOICES:
            with self.subTest(adapter=adapter), TemporaryDirectory() as tmp:
                config = self.load(render_config("Alice", adapter, None, Path(tmp)), tmp)
                self.assertEqual(config.adapter.type, adapter)
                if adapter in DEFAULT_COMMANDS and DEFAULT_COMMANDS[adapter]:
                    config.adapter.command_template()
                if adapter in ("custom", "codex_thread"):
                    # The one value init cannot know stays a commented REQUIRED hint,
                    # and validation names it instead of passing a placeholder.
                    self.assertTrue(any(word in " ".join(config.validate(require_token=False))
                                        for word in ("adapter.command", "conversation")))

    def test_a_windows_path_survives_yaml(self):
        text = render_config("Alice", "codex", None, Path(r"C:\Users\me\my project"))
        with TemporaryDirectory() as tmp:
            config = self.load(text, tmp)
        self.assertEqual(str(config.adapter.cwd), r"C:\Users\me\my project")

    def test_without_a_bus_the_bus_stays_commented(self):
        self.assertIn("# bus:\n", render_config("Alice", "codex", None, None))


class TestWriteStarter(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "proj"
        self.repo.mkdir()

    def test_creates_config_registry_and_gitignore(self):
        config = self.repo / "config.yaml"
        lines = write_starter(config, "Hermes", "hermes", bus_repo=self.repo)
        loaded = AgentConfig.load(config)
        self.assertEqual(Path(loaded.bus.repo), self.repo.resolve())
        registry = self.repo / "config" / "a2a_config.json"
        self.assertEqual(json.loads(registry.read_text())["authorized_agents"], {})
        ignored = (self.repo / ".gitignore").read_text().splitlines()
        self.assertIn(".agents/state/", ignored)
        self.assertIn("/config.yaml", ignored)
        # The hint names the resolved path (on Windows a temp folder may be an 8.3 short name).
        self.assertTrue(any(f"--registry {registry.resolve()}" in line for line in lines))

    def test_keeps_an_existing_registry_and_gitignore_lines(self):
        registry = self.repo / "config" / "a2a_config.json"
        registry.parent.mkdir()
        registry.write_text('{"authorized_agents": {"bob": {}}}')
        (self.repo / ".gitignore").write_text("/.agents/state/\nconfig.yaml")
        write_starter(self.repo / "config.yaml", "Hermes", "hermes", bus_repo=self.repo)
        self.assertIn("bob", registry.read_text())
        self.assertEqual((self.repo / ".gitignore").read_text(), "/.agents/state/\nconfig.yaml")

    def test_refuses_to_overwrite_without_force(self):
        config = self.tmp / "config.yaml"
        config.write_text("mine")
        with self.assertRaises(InitError):
            write_starter(config, "Hermes", "hermes")
        self.assertEqual(config.read_text(), "mine")
        write_starter(config, "Hermes", "hermes", force=True)
        self.assertIn("alias: Hermes", config.read_text())

    def test_rejects_bad_input(self):
        for kwargs in ({"alias": "bad alias", "adapter": "hermes"},
                       {"alias": "Hermes", "adapter": "nope"}):
            with self.subTest(**kwargs), self.assertRaises(InitError):
                write_starter(self.tmp / "c.yaml", **kwargs)
        with self.assertRaises(InitError):
            write_starter(self.tmp / "c.yaml", "Hermes", "hermes", bus_repo=self.tmp / "none")
        self.assertFalse((self.tmp / "c.yaml").exists())


class TestHomeChannelOnAFreshConfig(unittest.TestCase):
    """Issue #11 (brainstorm 2.11): ``init --home-channel`` was refused on a fresh config,
    although ``_group_id`` already parsed the value for ``--from``."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_a_group_id_lands_in_the_new_config(self):
        config = self.tmp / "config.yaml"
        lines = write_starter(config, "Hermes", "hermes", home_channel="-1001234567890")
        loaded = AgentConfig.load(config)
        self.assertEqual(loaded.identity.home_channel_id, -1001234567890)
        self.assertTrue(any("-1001234567890" in line for line in lines), lines)

    def test_a_message_link_lands_in_the_new_config(self):
        config = self.tmp / "config.yaml"
        write_starter(config, "Hermes", "hermes",
                      home_channel="https://t.me/c/1234567890/1/2")
        self.assertEqual(AgentConfig.load(config).identity.home_channel_id, -1001234567890)

    def test_without_the_flag_the_placeholder_stays(self):
        config = self.tmp / "config.yaml"
        write_starter(config, "Hermes", "hermes")
        self.assertEqual(AgentConfig.load(config).identity.home_channel_id, 0)

    def test_an_invalid_value_is_refused_before_anything_is_written(self):
        config = self.tmp / "config.yaml"
        with self.assertRaises(InitError) as caught:
            write_starter(config, "Hermes", "hermes", home_channel="my group")
        self.assertIn("--home-channel", str(caught.exception))
        self.assertFalse(config.exists())

    def test_an_existing_config_needs_force_either_way(self):
        config = self.tmp / "config.yaml"
        config.write_text("mine", encoding="utf-8")
        with self.assertRaises(InitError):
            write_starter(config, "Hermes", "hermes", home_channel="-1001234567890")
        self.assertEqual(config.read_text(encoding="utf-8"), "mine")
        write_starter(config, "Hermes", "hermes", home_channel="-1001234567890", force=True)
        self.assertEqual(AgentConfig.load(config).identity.home_channel_id, -1001234567890)

    def test_the_cli_writes_it_on_a_fresh_directory(self):
        """The reported bug: `init --home-channel` on a fresh directory used to exit 2."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_")}
        env.update(PYTHONPATH=str(ROOT), A2A_LOG_DIR="off", PYTHONIOENCODING="utf-8")
        config = self.tmp / "config.yaml"
        init = subprocess.run(
            [sys.executable, "-m", "swarmly.telegram.cli", "init", "--alias", "Tester",
             "--adapter", "echo", "--config", str(config),
             "--home-channel", "-1001234567890"],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(AgentConfig.load(config).identity.home_channel_id, -1001234567890)

    def test_the_cli_still_refuses_an_invalid_value(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_")}
        env.update(PYTHONPATH=str(ROOT), A2A_LOG_DIR="off", PYTHONIOENCODING="utf-8")
        init = subprocess.run(
            [sys.executable, "-m", "swarmly.telegram.cli", "init", "--alias", "Tester",
             "--adapter", "echo", "--config", str(self.tmp / "config.yaml"),
             "--home-channel", "not-a-group"],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(init.returncode, 2)
        self.assertIn("--home-channel", init.stderr)
        self.assertFalse((self.tmp / "config.yaml").exists())


class TestCommandLine(unittest.TestCase):
    def test_init_then_check_passes_once_the_human_values_are_in(self):
        """The README path: init, fill in the admin id and a token, check exits 0."""
        with TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_")}
            env.update(PYTHONPATH=str(ROOT), A2A_LOG_DIR="off", PYTHONIOENCODING="utf-8",
                       A2A_TELEGRAM_BOT_TOKEN_TESTER="123456789:AA" + "x" * 33)
            cli = [sys.executable, "-m", "swarmly.telegram.cli"]
            init = subprocess.run(cli + ["init", "--alias", "Tester", "--adapter", "echo"],
                                  cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(init.returncode, 0, init.stderr)
            config = Path(tmp, "config.yaml")
            first = subprocess.run(cli + ["check", "--offline"], cwd=tmp, env=env,
                                   capture_output=True, text=True, timeout=120)
            text = (config.read_text().replace("admin_user_id: 0 ", "admin_user_id: 42 ")
                    .replace("home_channel_id: 0 ", "home_channel_id: -1009 ")
                    .replace("example_tester_bot", "tester_bot"))
            config.write_text(text)
            check = subprocess.run(cli + ["check", "--offline"], cwd=tmp, env=env,
                                   capture_output=True, text=True, timeout=120)
        # Fresh from init, check names every value only the human knows ...
        for missing in ("admin_user_id", "home_channel_id", "agent.name"):
            self.assertIn(missing, first.stdout)
        self.assertNotIn("config is usable", first.stdout)
        # ... and passes once they are in.
        self.assertEqual(check.returncode, 0, check.stdout[-2000:] + check.stderr[-2000:])
        self.assertIn("config is usable", check.stdout)



class TestStateRootForAnInstalledWheel(unittest.TestCase):
    def test_no_bus_state_goes_to_home_not_site_packages(self):
        from types import SimpleNamespace
        from unittest import mock
        from swarmly.telegram import poll
        cfg = SimpleNamespace(bus=SimpleNamespace(repo=None), fallback=None)
        no_override = mock.patch.dict(os.environ)       # the suite's own root, set aside
        no_override.start()
        self.addCleanup(no_override.stop)
        os.environ.pop("SWARMLY_STATE_ROOT", None)
        self.assertEqual(poll.transport_state_root(cfg), ROOT)          # a checkout
        with TemporaryDirectory() as tmp:
            fake = Path(tmp, "site-packages", "swarmly", "telegram", "poll.py")
            with mock.patch.object(poll, "__file__", str(fake)):
                self.assertEqual(poll.transport_state_root(cfg), Path.home() / ".a2a")
        cfg.bus.repo = "/srv/bus"
        self.assertEqual(poll.transport_state_root(cfg), Path("/srv/bus"))

    def test_the_suite_keeps_no_bus_state_out_of_the_checkout(self):
        """Parallel workers share the checkout: a no-bus test writing there races the others."""
        from types import SimpleNamespace
        from swarmly.telegram import poll
        cfg = SimpleNamespace(bus=SimpleNamespace(repo=None), fallback=None)
        root = poll.transport_state_root(cfg)
        self.assertNotEqual(root.resolve(), ROOT)
        self.assertEqual(root, Path(os.environ["SWARMLY_STATE_ROOT"]))


class TestConfigFlagPlacement(unittest.TestCase):
    def test_config_works_before_and_after_the_command(self):
        from swarmly.telegram.cli import build_parser
        parser = build_parser()
        self.assertEqual(parser.parse_args(["init", "--config", "x.yaml"]).config, "x.yaml")
        self.assertEqual(parser.parse_args(["--config", "y.yaml", "check"]).config, "y.yaml")
        self.assertEqual(parser.parse_args(["github-poll", "--once", "--config", "z"]).config, "z")
        self.assertEqual(parser.parse_args(["check"]).config, "config.yaml")


class TestGitbusRootMatchesThePoller(unittest.TestCase):
    def test_no_bus_channel_and_poller_share_one_root(self):
        from unittest import mock
        from swarmly.telegram import poll
        from swarmly.telegram.channel import AgentChannel
        with TemporaryDirectory() as tmp:
            config = AgentConfig.from_dict({"agent": {"name": "b", "alias": "A", "bot_token": "1:x",
                                                      "home_channel_id": -1,
                                                      "admin_user_id": 1},
                                            "adapter": {"type": "echo", "cwd": tmp}})
            channel = AgentChannel(config)
            self.assertEqual(channel._gitbus_root(), poll.transport_state_root(config))
            fake = Path(tmp, "site-packages", "swarmly", "telegram", "poll.py")
            with mock.patch.object(poll, "__file__", str(fake)), mock.patch.dict(os.environ):
                os.environ.pop("SWARMLY_STATE_ROOT", None)  # the suite's own root, set aside
                self.assertEqual(channel._gitbus_root(), Path.home() / ".a2a")


class TestInitRole(unittest.TestCase):
    def test_role_is_written_and_checked(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp, "config.yaml")
            write_starter(config, "Codex", "codex", role="qa-tester")
            self.assertEqual(AgentConfig.load(config).role().name, "qa-tester")
            with self.assertRaises(InitError):
                write_starter(config, "Codex", "codex", role="wizard", force=True)
            self.assertIsNone(AgentConfig.from_dict(
                {"agent": {"name": "b", "alias": "A", "bot_token": "1:x"},
                 "adapter": {"type": "echo", "cwd": tmp}}).role())


if __name__ == "__main__":
    unittest.main()


class TestStateRootWithoutABus(unittest.TestCase):
    """Live, 25 Oct: bots whose bus section was removed fell back to one shared folder and
    came up with no state. Without a bus the state stays with the bot."""

    def setUp(self):
        from unittest import mock
        no_override = mock.patch.dict(os.environ)       # the suite's own root, set aside
        no_override.start()
        self.addCleanup(no_override.stop)
        os.environ.pop("SWARMLY_STATE_ROOT", None)

    def cfg(self, home, state_dir=None):
        from types import SimpleNamespace
        return SimpleNamespace(bus=SimpleNamespace(repo=None), fallback=None,
                               state=SimpleNamespace(dir=state_dir),
                               source_path=Path(home) / "config.yaml")

    def test_the_config_folder_is_the_default(self):
        from swarmly.telegram import poll
        with TemporaryDirectory() as tmp:
            self.assertEqual(poll.transport_state_root(self.cfg(tmp)), Path(tmp).resolve())

    def test_a_former_bus_checkout_keeps_its_state(self):
        from swarmly.telegram import poll
        with TemporaryDirectory() as tmp:
            old = Path(tmp) / "bus" / ".agents" / "bus"
            (old / ".agents" / "state").mkdir(parents=True)
            self.assertEqual(poll.transport_state_root(self.cfg(tmp)), old.resolve())

    def test_state_dir_and_the_env_override(self):
        from swarmly.telegram import poll
        with TemporaryDirectory() as tmp:
            cfg = self.cfg(tmp, state_dir=str(Path(tmp) / "here"))
            self.assertEqual(poll.transport_state_root(cfg), Path(tmp) / "here")
            os.environ["SWARMLY_STATE_ROOT"] = str(Path(tmp) / "env")
            self.assertEqual(poll.transport_state_root(cfg), Path(tmp) / "here")   # said outright
            cfg.state.dir = None
            self.assertEqual(poll.transport_state_root(cfg), Path(tmp) / "env")

    def test_state_dir_is_read_from_config(self):
        config = AgentConfig.from_dict({
            "agent": {"name": "b", "alias": "A", "bot_token": "1:x", "home_channel_id": -1,
                      "admin_user_id": 1},
            "adapter": {"type": "echo"},
            "state": {"backend": "telegram", "dir": "C:/bots/a"}})
        self.assertEqual(config.state.dir, "C:/bots/a")
