"""Review P0-8: on Windows a prompt never reaches cmd.exe through a .cmd/.bat shim."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly.telegram.adapters import antigravity
from swarmly.telegram.adapters import base
from swarmly.telegram.adapters.base import (BatchFileRefused, IncomingMessage, SubprocessAdapter,
                                        format_channel_prompt, windows_argv)
from swarmly.telegram.config import AdapterSettings

NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
)
endLocal & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
'''
PROMPT = 'hi "&calc&"'


class TestWindowsArgv(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)

    def which(self, mapping):
        return lambda name: mapping.get(name)

    def test_an_npm_shim_runs_its_script_with_node(self):
        shim = self.bin / "codex.cmd"
        shim.write_text(NPM_SHIM, encoding="utf-8")
        argv = windows_argv(["codex", "exec", PROMPT],
                            which=self.which({"codex": str(shim), "node": "C:/node/node.exe"}))
        self.assertEqual(argv[0], "C:/node/node.exe")
        self.assertTrue(argv[1].endswith("codex.js"), argv)
        self.assertNotIn(".cmd", " ".join(argv[:2]).lower())
        self.assertEqual(argv[2:], ["exec", PROMPT])

    def test_the_shims_own_node_is_preferred(self):
        shim = self.bin / "claude.cmd"
        shim.write_text(NPM_SHIM, encoding="utf-8")
        (self.bin / "node.exe").write_bytes(b"")
        argv = windows_argv(["claude", "-p"], which=self.which({"claude": str(shim)}))
        self.assertEqual(argv[0], str(self.bin / "node.exe"))

    def test_any_other_batch_file_is_refused(self):
        bat = self.bin / "agent.bat"
        bat.write_text("@echo off\r\nagent-real.exe %*\r\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            windows_argv(["agent", PROMPT], which=self.which({"agent": str(bat)}))

    def test_an_exe_is_used_as_resolved(self):
        argv = windows_argv(["claude", PROMPT], which=self.which({"claude": "C:/c/claude.exe"}))
        self.assertEqual(argv, ["C:/c/claude.exe", PROMPT])

    def test_agentapi_bat_is_never_returned(self):
        bat = self.bin / "agentapi.bat"
        bat.write_text("@echo off\r\nsomething else %*\r\n", encoding="utf-8")
        home = self.bin / "home"
        with mock.patch("swarmly.telegram.adapters.antigravity.shutil.which",
                        side_effect=lambda name: str(bat) if name == "agentapi.bat" else None), \
                mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.bin / "none")}), \
                mock.patch("pathlib.Path.home", return_value=home):
            self.assertIsNone(antigravity.resolve_agentapi())

    def test_a_trailing_dot_is_still_a_batch_file(self):
        shim = self.bin / "codex.cmd"
        shim.write_text(NPM_SHIM, encoding="utf-8")
        argv = windows_argv(["codex", PROMPT], which=self.which(
            {"codex": str(shim) + ".", "node": "C:/node/node.exe"}))
        self.assertEqual(argv[0], "C:/node/node.exe")
        bat = self.bin / "agent.bat"
        bat.write_text("@echo off\r\nagent-real.exe %*\r\n", encoding="utf-8")
        with self.assertRaises(BatchFileRefused):
            windows_argv(["agent", PROMPT], which=self.which({"agent": str(bat) + " ."}))

    def test_a_node_that_is_itself_a_batch_file_is_refused(self):
        shim = self.bin / "codex.cmd"
        shim.write_text(NPM_SHIM, encoding="utf-8")
        with self.assertRaises(BatchFileRefused):
            windows_argv(["codex", PROMPT], which=self.which(
                {"codex": str(shim), "node": "C:/nvm/node.cmd"}))

    def test_an_npm_shim_for_a_native_program_runs_the_exe(self):
        shim = self.bin / "tool.cmd"
        shim.write_text('@ECHO off\r\n"%~dp0\\node_modules\\tool\\bin\\tool.exe"   %*\r\n',
                        encoding="utf-8")
        argv = windows_argv(["tool", PROMPT], which=self.which({"tool": str(shim)}))
        self.assertTrue(argv[0].endswith("tool.exe"), argv)
        self.assertEqual(argv[1:], [PROMPT])

    def test_gemini_agentapi_bat_is_never_returned(self):
        home = self.bin / "home"
        gemini_bin = home / ".gemini" / "antigravity" / "bin"
        gemini_bin.mkdir(parents=True)
        (gemini_bin / "agentapi.bat").write_text("@echo off\r\nsomething %*\r\n",
                                                 encoding="utf-8")
        with mock.patch("swarmly.telegram.adapters.antigravity.shutil.which", return_value=None), \
                mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.bin / "none")}), \
                mock.patch("pathlib.Path.home", return_value=home):
            self.assertIsNone(antigravity.resolve_agentapi())


class TestARefusedShimIsAnError(unittest.TestCase):
    """The refusal reaches the chat as an error naming the file, not a crashed turn."""

    def test_respond_names_the_batch_file(self):
        adapter = SubprocessAdapter("Codex", AdapterSettings(type="custom",
                                                             command="codex {prompt}"))
        refusal = BatchFileRefused("refusing to run C:/npm/codex.cmd: cmd.exe would ...")
        with mock.patch.object(base, "_ON_WINDOWS", True), \
                mock.patch.object(base, "windows_argv", side_effect=refusal) as rewrite:
            reply = asyncio.run(adapter.respond(IncomingMessage(PROMPT, "Hermes")))
        rewrite.assert_called_once()
        self.assertFalse(reply.ok)
        self.assertIn("codex.cmd", reply.error)


class TestNamesInThePromptLine(unittest.TestCase):

    def test_the_framing_line_carries_no_shell_characters(self):
        prompt = format_channel_prompt('x"&calc&"', "hello", author_username='y"&del&"')
        first = prompt.splitlines()[0]
        for char in '"&|<>^%':
            self.assertNotIn(char, first)

    def test_the_agentapi_title_carries_no_shell_characters(self):
        message = IncomingMessage("hi", 'x"&calc&"', author_username='@y"&del&"|z')
        title = antigravity.agentapi_title(message)
        self.assertTrue(title.startswith("Telegram: @y"), title)
        for char in '"&|<>^%':
            self.assertNotIn(char, title)

    def test_the_agentapi_call_site_passes_a_plain_title(self):
        # P0-7/8 review: the title check above, but through the adapter's real call.
        import subprocess
        settings = antigravity.AntigravityThreadAdapter.default_settings(
            cwd=tempfile.gettempdir(), conversation="00000000-0000-4000-8000-000000000000")
        adapter = antigravity.AntigravityThreadAdapter("Antigravity", settings)
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"ok": true}',
                                           stderr="")
        message = IncomingMessage("hi", 'x"&calc&"', author_username='@y"&del&"|z')
        with mock.patch.object(antigravity, "resolve_agentapi", return_value="/bin/agentapi"), \
                mock.patch.object(antigravity.subprocess, "run", return_value=done) as run:
            asyncio.run(adapter.respond(message))
        [title] = [arg for arg in run.call_args.args[0] if str(arg).startswith("--title=")]
        self.assertTrue(title.startswith("--title=Telegram: @y"), title)
        for char in '"&|<>^%\n':
            self.assertNotIn(char, title)

    def test_a_bus_sender_is_made_plain(self):
        from swarmly.telegram.poll import _bus_incoming
        from tests.test_telegram_channel import make_config
        incoming = _bus_incoming("hi", 'codex"&calc&"\nx', make_config(), "tok")
        for field in (incoming.author_alias, incoming.author_username):
            for char in '"&\n':
                self.assertNotIn(char, field)


if __name__ == "__main__":
    unittest.main()
