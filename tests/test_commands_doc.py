"""docs/telegram/COMMANDS.md lists every command the bots answer.

The ``/`` menu (``MENU_COMMANDS``) is what a person sees in Telegram; the page is where they
read what each one does. A command added to the menu without a line on the page is a command
nobody can look up, so the two are kept in step here.
"""

import re
import unittest
from pathlib import Path

from swarmly.telegram.commands import MENU_COMMANDS

PAGE = Path(__file__).resolve().parent.parent / "docs" / "telegram" / "COMMANDS.md"


class TheCommandsPageTests(unittest.TestCase):
    def test_every_menu_command_has_a_line_on_the_page(self):
        text = PAGE.read_text(encoding="utf-8")
        written = set(re.findall(r"`/([a-z_]+)", text))
        menu = {name for name, _ in MENU_COMMANDS}
        self.assertEqual(sorted(menu - written), [])

    def test_the_commands_outside_the_menu_are_on_it_too(self):
        text = PAGE.read_text(encoding="utf-8")
        written = set(re.findall(r"`/([a-z_]+)", text))
        hidden = {"diag", "effort", "bus"}           # answered, but not in the menu
        self.assertEqual(sorted(hidden - written), [])


if __name__ == "__main__":
    unittest.main()
