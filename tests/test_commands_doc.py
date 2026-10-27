"""docs/telegram/COMMANDS.md lists every command the bots answer.

The ``/`` menu (``MENU_COMMANDS``) is what a person sees in Telegram; the page is where they
read what each one does. A command added to the menu without a line on the page is a command
nobody can look up, so the two are kept in step here.
"""

import re
import unittest
from pathlib import Path

from swarmly.telegram.commands import MENU_COMMANDS
from swarmly.telegram.control import CONTROLS
from tests.test_telegram_channel import make_channel

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
        hidden = {"diag", "effort", "bus", "where", "storetest", "link", "unlink", "phase",
                  "wakes", "rotate"}                    # answered, but not in the menu
        self.assertEqual(sorted(hidden - written), [])

    def test_every_dashboard_button_is_in_the_table(self):
        """Every button the dashboard offers (a control this build has wired; the rest of
        ``CONTROLS`` is never shown) has a row."""
        text = PAGE.read_text(encoding="utf-8")
        section = text.split("## Dashboard buttons", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("|")]
        buttons = set()
        for row in rows[2:]:                    # skip the header and its rule
            cell = row.strip("|").split("|")[0]
            buttons |= {part.strip() for part in cell.split(",")}
        offered = set(make_channel().control_actions())
        listed = {control.label for control in CONTROLS if control.action in offered}
        self.assertEqual(sorted(listed - buttons), [])


if __name__ == "__main__":
    unittest.main()
