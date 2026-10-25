import asyncio
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly import logsetup


def _drop_a2a_handlers():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_a2a", False):
            root.removeHandler(h)
            h.close()


class _Proc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode, self._out, self._err, self.pid = rc, out, err, 4242

    async def communicate(self):
        return self._out, self._err


class LogSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._reset)

    def _reset(self):
        _drop_a2a_handlers()

    def _read(self, path):
        for h in logging.getLogger().handlers:
            h.flush()
        return Path(path).read_text(encoding="utf-8")

    def test_file_gets_debug_console_gets_info(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            path = logsetup.configure("a2a-telegram-Codex-run", directory=self.tmp.name)
            logging.getLogger("a2a.test").debug("only in the file")
            logging.getLogger("a2a.test").info("in both")
        self.assertEqual(path.name, "a2a-telegram-codex-run.log")
        text = self._read(path)
        self.assertIn("only in the file", text)
        self.assertIn("in both", text)
        self.assertNotIn("only in the file", err.getvalue())

    def test_secrets_are_redacted(self):
        path = logsetup.configure("redact", directory=self.tmp.name, console=False)
        token = "123456789:AAH" + "x" * 32
        logging.getLogger("a2a.test").info("url https://api.telegram.org/bot%s/getMe", token)
        logging.getLogger("a2a.test").info("key sk-%s and api_key=abcdefgh12345", "a" * 24)
        text = self._read(path)
        self.assertNotIn(token, text)
        self.assertNotIn("a" * 24, text)
        self.assertNotIn("abcdefgh12345", text)
        self.assertIn("<telegram-token>", text)

    def test_off_and_unwritable_dir_are_not_fatal(self):
        self.assertIsNone(logsetup.configure("x", directory="off", console=False))
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("")
        self.assertIsNone(logsetup.configure("x", directory=str(blocker / "sub"),
                                             console=False))

    def test_rotation_settings(self):
        with mock.patch.dict(os.environ, {"A2A_LOG_MAX_MB": "1", "A2A_LOG_BACKUPS": "2"}):
            logsetup.configure("rot", directory=self.tmp.name, console=False)
        handler = [h for h in logging.getLogger().handlers if getattr(h, "_a2a", False)][0]
        self.assertEqual(handler.maxBytes, 1024 * 1024)
        self.assertEqual(handler.backupCount, 2)

    def test_adapter_failure_is_logged_with_stderr_tail(self):
        from swarmly.telegram.adapters.base import SubprocessAdapter
        from swarmly.telegram.config import AdapterSettings

        path = logsetup.configure("adapter", directory=self.tmp.name, console=False)

        async def runner(argv, cwd, env, timeout):
            return _Proc(3, b"", b"line one\nboom: model overloaded\n")

        settings = AdapterSettings(type="custom", command="tool {prompt}")
        adapter = SubprocessAdapter("Codex", settings, runner=runner)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(adapter._run(["tool", "x" * 500]))
        finally:
            loop.close()
        text = self._read(path)
        self.assertIn("CLI exited 3", text)
        self.assertIn("boom: model overloaded", text)
        self.assertIn("<500 chars", text)  # the long prompt is shortened

    def test_telegram_cli_logs_config_errors_to_file(self):
        from swarmly.telegram.cli import main
        with mock.patch("sys.stderr", io.StringIO()):
            code = main(["--config", str(Path(self.tmp.name) / "missing.yaml"),
                         "--log-dir", self.tmp.name, "check"])
        self.assertEqual(code, 2)
        text = self._read(Path(self.tmp.name) / "swarmly-telegram-check.log")
        self.assertIn("config error", text)


try:
    from tests.swarm_helpers import SwarmCase, needs_swarm
except ImportError:  # pragma: no cover
    SwarmCase, needs_swarm = unittest.TestCase, unittest.skip("swarm helpers")


@needs_swarm
class SwarmLogTests(SwarmCase):
    def test_rejections_posts_and_guard_blocks_are_logged(self):
        from swarmly.swarm.governor import Rejected
        logdir = self.root / "logs"
        path = logsetup.configure("swarm", directory=str(logdir), console=False)
        self.addCleanup(_drop_a2a_handlers)
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        with self.assertRaises(Rejected):
            self.gov("codex").post("OFFER", task_id="t1")
        self.gov("codex").guard("git push --force origin main", sync=False)
        for h in logging.getLogger().handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        self.assertIn("posted codex:1 PROPOSE", text)
        self.assertIn("REJECTED OFFER from codex", text)
        self.assertIn("guard BLOCKED for codex", text)


if __name__ == "__main__":
    unittest.main()
