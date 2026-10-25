"""Windows: a bot with no console of its own starts its child processes with no window.

Admin, 25 Oct: "I keep seeing cmd prompts popping up and then disappearing." The bots run
under scheduled tasks through pythonw, so every console program they started (git, gh, an
agent CLI, each shell command it runs) got a console window of its own.
"""

import subprocess
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


class TheCiRuleTests(unittest.TestCase):

    def test_agents_are_told_not_to_poll_ci(self):
        from swarmly.telegram.adapters.base import _tail_rules
        rule = next(r for r in _tail_rules() if "CI IN A LOOP" in r)
        self.assertIn("gh pr checks --watch", rule)
        self.assertIn("transport watches its CI", rule)


if __name__ == "__main__":
    unittest.main()
