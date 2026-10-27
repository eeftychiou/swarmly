"""The registry flags PROTOCOL.md §11 documents exist, with the defaults it states (#421).

The docs are where an operator reads what to put in ``config.yaml``, and the registry's phases
3–8 nearly all ship **behind a flag whose default is still the legacy path**. A documented
default that drifts from the code is worse than no line at all: it tells someone a flag is off
when it is on, or names a knob that was renamed. §11.7 is the one table, and this test reads it
back and compares every row with ``swarmly/telegram/config.py``.

The other half of #421's acceptance is that no page still makes gitbus mandatory, which the last
class checks.
"""

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


def table_rows(text: str = None) -> dict:
    """``{setting: stated default}`` from the §11.7 table, in order.

    The section ends at the next heading of any level, so a later subsection's table is never
    read as a flag row.
    """
    text = PAGE.read_text(encoding="utf-8") if text is None else text
    section = text.split("### 11.7 The flags", 1)[1]
    lines = []
    for line in section.splitlines():
        if line.startswith(("### ", "## ")):
            break
        lines.append(line)
    rows = {}
    for line in lines:
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


def wrong_defaults(text: str = None) -> dict:
    """``{setting: (documented, actual)}`` for every row the code disagrees with."""
    wrong = {}
    for setting, documented in table_rows(text).items():
        section, _, attribute = setting.partition(".")
        if section not in SECTIONS:
            wrong[setting] = (documented, f"no section {section!r} in config.py")
            continue
        settings = SECTIONS[section]()
        if not hasattr(settings, attribute):
            wrong[setting] = (documented, f"{type(settings).__name__} has no {attribute}")
            continue
        actual = stated(getattr(settings, attribute))
        if actual != documented:
            wrong[setting] = (documented, actual)
    return wrong


class TheFlagsTableTests(unittest.TestCase):
    def test_the_table_lists_every_flag_of_the_registry(self):
        rows = table_rows()
        self.assertTrue(rows, "the §11.7 table parsed as empty")
        self.assertEqual(sorted(EXPECTED - set(rows)), [])
        self.assertEqual(sorted(set(rows) - EXPECTED), [])

    def test_every_stated_default_is_the_one_in_config(self):
        self.assertEqual(wrong_defaults(), {})

    def test_a_wrong_default_in_the_table_is_caught(self):
        """DISCRIMINATING: the same check, over a copy of §11.7 with one default flipped."""
        text = PAGE.read_text(encoding="utf-8")
        mutated = text.replace("| `state.links` | `legacy` |", "| `state.links` | `registry` |", 1)
        self.assertNotEqual(text, mutated, "the row this test flips is no longer in the table")
        self.assertEqual(wrong_defaults(mutated),
                         {"state.links": ("registry", "legacy")})

    def test_a_later_subsection_is_not_read_as_flag_rows(self):
        """The section stops at the next heading: `### 11.8` cannot smuggle a row in."""
        text = PAGE.read_text(encoding="utf-8") + \
            "\n### 11.8 Later\n\n| `state.made_up` | `on` | x | 9 | no |\n"
        self.assertNotIn("state.made_up", table_rows(text))


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

    def test_no_page_still_makes_bus_repo_a_must(self):
        """The sentence that required `bus.repo` is now conditional on using a gitbus."""
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Where a gitbus is used, every transport must set", agents)
        self.assertNotIn("Every transport must set `bus.repo`", agents)

    def test_the_quickstart_that_needs_no_git_is_reachable_from_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/QUICKSTART.md", readme)
        quickstart = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
        self.assertIn("transport: telegram", quickstart)


if __name__ == "__main__":
    unittest.main()
