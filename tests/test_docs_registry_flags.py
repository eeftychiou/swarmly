"""The registry flags PROTOCOL.md §11 documents exist, with the defaults it states (#421).

The docs are where an operator reads what to put in ``config.yaml``, and the registry's phases
3–8 all ship **behind a flag whose default is still the legacy path**. A documented default that
drifts from the code is worse than no line at all: it tells someone a flag is off when it is on,
or names a knob that was renamed. §11.7 is the one table, and this test reads it back and
compares every row with ``swarmly/telegram/config.py``.

The second test is the other half of #421's acceptance: no page still makes gitbus mandatory.
"""

import re
import unittest
from pathlib import Path

from swarmly.telegram.config import (
    AdapterSettings,
    BusSettings,
    ChannelLimits,
    GitHubSettings,
    StateSettings,
)

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "telegram" / "PROTOCOL.md"

#: Every section the table may name a flag from, and the dataclass that holds its default.
SECTIONS = {
    "state": StateSettings,
    "bus": BusSettings,
    "limits": ChannelLimits,
    "adapter": AdapterSettings,
    "github": lambda: GitHubSettings(login="a-bot"),
}

#: The flags the phases of #260 ship behind. A row that disappears from the table is a flag the
#: docs no longer explain, so the set is asserted, not just each row.
EXPECTED = {
    "state.backend", "state.mode", "state.heartbeat_minutes", "state.dir", "state.lease",
    "state.owed", "state.links", "state.routing", "bus.transport", "limits.general_turns",
    "adapter.type", "adapter.session_turn_limit", "adapter.session_token_limit",
}


def table_rows() -> dict:
    """``{setting: stated default}`` from the §11.7 table, in order."""
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("### 11.7 The flags", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        setting = cells[0].strip("`")
        if setting == "Setting" or not setting.strip("- "):
            continue                                  # the header and its rule
        rows[setting] = cells[1].strip("`")
    return rows


def stated(value) -> str:
    """How the table writes a default: ``unset`` for None, ``true``/``false``, plain numbers."""
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


class TheFlagsTableTests(unittest.TestCase):
    def test_the_table_lists_every_flag_of_the_registry(self):
        self.assertEqual(sorted(EXPECTED - set(table_rows())), [])
        self.assertEqual(sorted(set(table_rows()) - EXPECTED), [])

    def test_every_stated_default_is_the_one_in_config(self):
        rows = table_rows()
        self.assertTrue(rows, "the §11.7 table parsed as empty")
        for setting, documented in rows.items():
            with self.subTest(setting=setting):
                section, _, attribute = setting.partition(".")
                self.assertIn(section, SECTIONS, f"{setting}: no section to check it against")
                settings = SECTIONS[section]()
                self.assertTrue(hasattr(settings, attribute),
                                f"{setting} is documented but {type(settings).__name__} "
                                f"has no {attribute}")
                self.assertEqual(documented, stated(getattr(settings, attribute)),
                                 f"{setting}: the docs say {documented}")

    def test_a_wrong_default_in_the_table_is_caught(self):
        """DISCRIMINATING: the check above fails on a default the code does not hold."""
        rows = table_rows()
        self.assertEqual(stated(StateSettings().backend), "off")
        self.assertNotEqual(stated(StateSettings().backend), "telegram")
        self.assertEqual(rows["state.backend"], "off")


class GitbusIsOptionalTests(unittest.TestCase):
    """#421: gitbus is optional when the store is the bus, and the pages say so."""

    def test_the_pages_that_used_to_make_gitbus_mandatory_say_it_is_optional(self):
        for path, needle in (
            (ROOT / "AGENTS.md", "gitbus is optional"),
            (ROOT / "docs" / "PROJECTS.md", "gitbus is **optional**"),
            (PAGE, "`bus.transport: telegram`"),
        ):
            with self.subTest(page=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(needle, text)

    def test_the_flag_that_replaces_bus_repo_really_does(self):
        """`bus.transport: telegram` needs no ``bus.repo``: ``bus.repo`` is unset by default."""
        self.assertIsNone(BusSettings().repo)
        self.assertEqual(BusSettings().transport, "git")
        self.assertTrue(re.search(r"`bus\.transport`\s*\|\s*`git`", PAGE.read_text("utf-8")))


if __name__ == "__main__":
    unittest.main()
