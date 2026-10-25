"""Windows: a bot with no console of its own starts its child processes with no window.

Admin, 25 Oct: "I keep seeing cmd prompts popping up and then disappearing." The bots run
under scheduled tasks through pythonw, so every console program they started (git, gh, an
agent CLI, each shell command it runs) got a console window of its own.
"""

import subprocess
import sys
import unittest
from unittest import mock

from swarmly import winconsole
from swarmly.winconsole import (CREATE_NEW_CONSOLE, CREATE_NO_WINDOW, DETACHED_PROCESS,
                                hide_child_consoles, no_window)


class NoWindowTests(unittest.TestCase):

    def flags_for(self, **kwargs):
        seen = {}

        def init(self, *args, creationflags=0, **kw):
            seen["flags"] = creationflags
        no_window(init)(object(), ["git", "status"], **kwargs)
        return seen["flags"]

    def test_a_child_gets_no_window(self):
        self.assertEqual(self.flags_for(), CREATE_NO_WINDOW)

    def test_other_flags_are_kept(self):
        self.assertEqual(self.flags_for(creationflags=0x200), 0x200 | CREATE_NO_WINDOW)

    def test_a_console_asked_for_is_left_alone(self):
        for flag in (CREATE_NEW_CONSOLE, DETACHED_PROCESS):
            with self.subTest(flag=flag):
                self.assertEqual(self.flags_for(creationflags=flag), flag)

    def test_flags_passed_by_position_are_honoured(self):
        """`Popen`'s signature puts `creationflags` 14th; a caller passing it by position
        got "multiple values for argument 'creationflags'" (#315 review)."""
        seen = {}

        def init(self, args, bufsize=-1, executable=None, stdin=None, stdout=None,
                 stderr=None, preexec_fn=None, close_fds=True, shell=False, cwd=None, env=None,
                 universal_newlines=None, startupinfo=None, creationflags=0):
            seen["flags"] = creationflags
        # args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds, shell, cwd,
        # env, universal_newlines, startupinfo -- then creationflags.
        leading = (["git"], -1, None, None, None, None, None, True, False, None, None, None, None)
        for flags, expected in ((0x200, 0x200 | CREATE_NO_WINDOW),
                                (CREATE_NEW_CONSOLE, CREATE_NEW_CONSOLE)):
            with self.subTest(flags=flags):
                no_window(init)(object(), *leading, flags)
                self.assertEqual(seen["flags"], expected)


class HideChildConsolesTests(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(subprocess.Popen, "__init__", subprocess.Popen.__init__)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_windows_without_a_console_starts_children_hidden(self):
        self.assertTrue(hide_child_consoles(system="nt", has_console=lambda: False))
        self.assertTrue(getattr(subprocess.Popen.__init__, winconsole._MARK))

    def test_twice_wraps_once(self):
        hide_child_consoles(system="nt", has_console=lambda: False)
        once = subprocess.Popen.__init__
        hide_child_consoles(system="nt", has_console=lambda: False)
        self.assertIs(subprocess.Popen.__init__, once)

    def test_a_process_with_a_console_is_left_alone(self):
        before = subprocess.Popen.__init__
        self.assertFalse(hide_child_consoles(system="nt", has_console=lambda: True))
        self.assertIs(subprocess.Popen.__init__, before)

    def test_other_systems_are_left_alone(self):
        before = subprocess.Popen.__init__
        self.assertFalse(hide_child_consoles(system="posix", has_console=lambda: False))
        self.assertIs(subprocess.Popen.__init__, before)


@unittest.skipUnless(sys.platform == "win32", "the Win32 process call exists only on Windows")
class OnWindowsTests(unittest.TestCase):
    """The flag reaches ``CreateProcess`` itself, for ``subprocess`` and ``asyncio`` alike."""

    def setUp(self):
        patcher = mock.patch.object(subprocess.Popen, "__init__", subprocess.Popen.__init__)
        patcher.start()
        self.addCleanup(patcher.stop)
        hide_child_consoles(system="nt", has_console=lambda: False)

    def flags_of(self, start):
        import _winapi
        with mock.patch.object(_winapi, "CreateProcess", wraps=_winapi.CreateProcess) as spy:
            start()
        return spy.call_args.args[5]                 # (executable, args, ..., creationflags, ...)

    def test_a_child_process_is_created_with_no_window(self):
        def start():
            child = subprocess.Popen([sys.executable, "-c", "print(42)"],
                                     stdout=subprocess.PIPE, text=True)
            self.assertEqual(child.communicate(timeout=60)[0].strip(), "42")
        self.assertTrue(self.flags_of(start) & CREATE_NO_WINDOW)

    def test_an_asyncio_child_too(self):
        import asyncio

        async def child():
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
            await process.wait()
        self.assertTrue(self.flags_of(lambda: asyncio.run(child())) & CREATE_NO_WINDOW)

    def test_a_console_asked_for_reaches_it_unchanged(self):
        def start():
            child = subprocess.Popen([sys.executable, "-c", "pass"],
                                     creationflags=DETACHED_PROCESS)
            child.wait(timeout=60)
        self.assertEqual(self.flags_of(start) & (CREATE_NO_WINDOW | DETACHED_PROCESS),
                         DETACHED_PROCESS)


class TheCiRuleTests(unittest.TestCase):

    def test_agents_are_told_not_to_poll_ci(self):
        from swarmly.telegram.adapters.base import _tail_rules
        rule = next(r for r in _tail_rules() if "CI IN A LOOP" in r)
        self.assertIn("gh pr checks --watch", rule)
        self.assertIn("transport watches its CI", rule)


if __name__ == "__main__":
    unittest.main()
