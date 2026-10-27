"""The registry flags PROTOCOL.md §11 documents exist, with the defaults it states (#421).

The docs are where an operator reads what to put in ``config.yaml``, and the registry's phases
3–8 nearly all ship **behind a flag whose default is still the legacy path**. A documented
default that drifts from the code is worse than no line at all: it tells someone a flag is off
when it is on, or names a knob that was renamed. §11.7 is the one table, and this test reads it
back and compares every row with ``swarmly/telegram/config.py``.

The other half of #421's acceptance is that no page still makes gitbus mandatory, which the last
class checks.
"""

import re
import unittest
from pathlib import Path

from swarmly.telegram.config import (
    STATE_REGISTRY_DEFAULTS,
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

#: Rows whose default depends on the backend (#424): the cell is the default with the store off,
#: the dagger says the registry side is the default on it, and the footnote states both.
DAGGERED = {"state.lease", "state.owed", "state.links", "state.routing"}


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
        rows[setting] = cells[1].replace("`", "").replace("†", "").strip()
    return rows


#: "…an omitted `state.lease`/`state.owed` is `store`…": the footnote's own sentence (#424).
OMITTED_IS = re.compile(r"omitted ((?:`state\.\w+`[^`]*?)+?) is `(\w+)`")


def footnote_defaults(text: str = None) -> dict:
    """``{flag: default}`` as the §11.7 footnote states the on-the-store defaults (#424)."""
    text = PAGE.read_text(encoding="utf-8") if text is None else text
    parts = text.split("† **On the store these four default to the registry side**", 1)
    if len(parts) != 2:
        return {}
    first_paragraph = parts[1].split(chr(10) * 2, 1)[0]
    body = re.sub(r"\s+", " ", first_paragraph)
    stated = {}
    for flags, value in OMITTED_IS.findall(body):
        for flag in re.findall(r"`state\.(\w+)`", flags):
            stated[flag] = value
    return stated


def daggered_rows(text: str = None) -> set:
    """The rows whose default column carries the on-the-store dagger (#424)."""
    text = PAGE.read_text(encoding="utf-8") if text is None else text
    marked = set()
    for line in text.split("### 11.7 The flags", 1)[1].splitlines():
        if line.startswith(("### ", "## ")):
            break
        if line.startswith("|") and "†" in line.split("|")[2]:
            marked.add(line.strip("|").split("|")[0].strip().strip("`"))
    return marked


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


#: Statements that were true while every flag was off by default, and are false since #424.
#: The review of #426 found three of them still in the pages: a page that says legacy is the
#: default reads as a lie next to the dagger footnote.
STALE = (
    "the legacy path is still the default today",
    "No flag is on by default yet",
    "new, behind flags",
    "(the default once the store is on)",
)

#: The pages that carry one of them, by name.
STALE_PAGES = {
    "docs/telegram/PROTOCOL.md": PAGE,
    "AGENTS.md": ROOT / "AGENTS.md",
    "README.md": ROOT / "README.md",
    "swarmly/telegram/store_lease.py": ROOT / "swarmly" / "telegram" / "store_lease.py",
}


def stale_statements(text: str) -> list:
    """The superseded statements a page still makes (#424)."""
    return [line for line in STALE if line in text]


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
        mutated = text.replace("| `state.links` | `legacy` † |",
                               "| `state.links` | `registry` † |", 1)
        self.assertNotEqual(text, mutated, "the row this test flips is no longer in the table")
        self.assertEqual(wrong_defaults(mutated),
                         {"state.links": ("registry", "legacy")})

    def test_the_rows_whose_default_depends_on_the_backend_carry_the_dagger(self):
        """#424: the four registry flags say so in the table, and no other row does."""
        self.assertEqual(daggered_rows(), DAGGERED)

    def test_the_footnote_states_the_defaults_config_uses(self):
        """#424: the on-the-store default in the prose is the one in config.py."""
        self.assertEqual(footnote_defaults(), STATE_REGISTRY_DEFAULTS)

    def test_a_wrong_footnote_is_caught(self):
        """DISCRIMINATING: the same check, over a copy whose footnote flips one default."""
        text = PAGE.read_text(encoding="utf-8")
        mutated = text.replace("an omitted `state.lease`/`state.owed` is `store`",
                               "an omitted `state.lease`/`state.owed` is `shadow`", 1)
        self.assertNotEqual(text, mutated, "the footnote no longer reads that way")
        wrong = footnote_defaults(mutated)
        self.assertEqual(wrong.get("lease"), "shadow")
        self.assertNotEqual(wrong, STATE_REGISTRY_DEFAULTS)

    def test_a_later_subsection_is_not_read_as_flag_rows(self):
        """The section stops at the next heading: `### 11.8` cannot smuggle a row in."""
        text = PAGE.read_text(encoding="utf-8") + \
            "\n### 11.8 Later\n\n| `state.made_up` | `on` | x | 9 | no |\n"
        self.assertNotIn("state.made_up", table_rows(text))


class ThePagesThatStatedTheOldDefaultTests(unittest.TestCase):
    """#424: a page that still calls ``legacy`` *the* default is not updated documentation."""

    def test_no_page_still_calls_legacy_the_default(self):
        for name, page in STALE_PAGES.items():
            with self.subTest(page=name):
                self.assertEqual(stale_statements(page.read_text(encoding="utf-8")), [])

    def test_a_page_that_puts_one_back_is_caught(self):
        """DISCRIMINATING: the same check, over a copy of AGENTS.md that still says it."""
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        mutated = text.replace("On that store the registry side is the default",
                               "No flag is on by default yet; On that store the registry side "
                               "is the default", 1)
        self.assertNotEqual(text, mutated, "the sentence this test mutates is gone")
        self.assertEqual(stale_statements(mutated), ["No flag is on by default yet"])

    def test_the_section_intro_names_where_the_default_flips(self):
        """The replacement sentence says which backend the registry default belongs to."""
        intro = PAGE.read_text(encoding="utf-8").split("### 11.1", 1)[0]
        paragraph = intro.split("**Most of what is below ships behind its flag.**", 1)[1]
        paragraph = paragraph.split("### ", 1)[0]
        for needle in ("With the store off", "`state.backend: telegram`", "#424", "§11.7"):
            with self.subTest(needle=needle):
                self.assertIn(needle, paragraph)


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
