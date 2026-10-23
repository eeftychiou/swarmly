"""Self-wake timers (issue #81).

The properties the design review asked for: an exact-argv read-only allowlist, per-command
exit codes so a *failing* check starts a turn instead of re-arming, absolute times and an
attempt count that survive a restart, and a fire claimed on the bus so a takeover cannot fire
the same timer twice.
"""

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import wake as wk


class TestDurations(unittest.TestCase):

    def test_units(self):
        self.assertEqual(wk.parse_duration("90"), 90)
        self.assertEqual(wk.parse_duration("90s"), 90)
        self.assertEqual(wk.parse_duration("10m"), 600)
        self.assertEqual(wk.parse_duration("2h"), 7200)
        self.assertEqual(wk.parse_duration("1d"), 86400)

    def test_bad_durations_are_refused(self):
        for text in ("", "soon", "-5m", "0", "10x", "m"):
            with self.subTest(text=text), self.assertRaises(wk.WakeError):
                wk.parse_duration(text)

    def test_read_only_commands_are_allowed(self):
        self.assertEqual(wk.validate_check("gh pr checks 75 --required"),
                         ("gh", "pr", "checks", "75", "--required"))
        self.assertEqual(wk.validate_check("git status --short"),
                         ("git", "status", "--short"))

    def test_write_capable_commands_are_refused(self):
        for command in ("gh pr merge 75", "gh api -X POST repos/x", "git push origin main",
                        "git checkout -b x", "rm -rf /", "python -c 'print(1)'"):
            with self.subTest(command=command), self.assertRaises(wk.WakeError):
                wk.validate_check(command)

    def test_shell_metacharacters_are_refused(self):
        for command in ("gh pr checks 75 | tee out", "gh pr view 75; rm -rf x",
                        "gh pr view $(whoami)", "gh pr view 75 > out",
                        "gh pr view `id`", "gh api repos/x\nrm -rf y"):
            with self.subTest(command=command), self.assertRaises(wk.WakeError):
                wk.validate_check(command)

    def test_a_write_flag_on_a_read_command_is_refused(self):
        with self.assertRaises(wk.WakeError):
            wk.validate_check("gh api repos/x --method=POST")
        with self.assertRaises(wk.WakeError):
            wk.validate_check("gh api repos/x -X PATCH")

    def test_gh_api_get_is_allowed(self):
        self.assertEqual(wk.validate_check("gh api repos/x/pulls/9"),
                         ("gh", "api", "repos/x/pulls/9"))


class TestParseLine(unittest.TestCase):

    def test_a_full_line(self):
        spec = wk.parse_line('WAKE in=10m every=5m until=120m check="gh pr checks 75" '
                             'then="merge it if review holds"')
        self.assertEqual(spec.argv, ("gh", "pr", "checks", "75"))
        self.assertEqual(spec.in_seconds, 600)
        self.assertEqual(spec.every_seconds, 300)
        self.assertEqual(spec.until_seconds, 7200)
        self.assertEqual(spec.then, "merge it if review holds")

    def test_missing_pieces_are_refused_with_a_reason(self):
        for line in ('WAKE in=10m check="gh pr checks 75"',
                     'WAKE check="gh pr checks 75" then="x"',
                     'WAKE in=10m then="x"',
                     'WAKE in=10m check="gh pr checks 75" then="x" nope=1'):
            with self.subTest(line=line), self.assertRaises(wk.WakeError):
                wk.parse_line(line)

    def test_until_shorter_than_in_is_refused(self):
        with self.assertRaises(wk.WakeError):
            wk.parse_line('WAKE in=10m until=1m check="gh pr checks 75" then="x"')


class TestExtract(unittest.TestCase):

    def test_lines_are_stripped_even_when_refused(self):
        text = ('merging once green\n'
                'WAKE in=10m check="gh pr merge 75" then="x"\n'
                'that is all')
        stripped, specs, reasons = wk.extract_wakes(text)
        self.assertEqual(specs, [])
        self.assertEqual(len(reasons), 1)
        self.assertNotIn("WAKE", stripped)
        self.assertIn("merging once green", stripped)
        self.assertIn("that is all", stripped)

    def test_two_valid_lines(self):
        text = ('a\nWAKE in=1m check="gh pr checks 1" then="x"\n'
                'WAKE in=2m check="git status" then="y"\nb')
        stripped, specs, reasons = wk.extract_wakes(text)
        self.assertEqual(len(specs), 2)
        self.assertEqual(reasons, [])
        self.assertEqual(stripped, "a\nb")

    def test_text_without_a_line_is_untouched(self):
        self.assertEqual(wk.extract_wakes("just prose"), ("just prose", [], []))


class TestChannelWiring(unittest.TestCase):
    """A WAKE line becomes a WAIT row and arms nothing; the line never reaches the channel."""

    def channel(self):
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             )
        channel = make_channel(config)
        # A table of its own, so the migration test writes no row to this checkout (#190).
        from swarmly.telegram import owed as ow
        table = TemporaryDirectory()
        self.addCleanup(table.cleanup)
        table_store = ow.OwedStore("Hermes", state_dir=Path(table.name), clock=lambda: 1000.0)
        channel.owed_store = lambda: table_store
        channel._test_table = table_store
        return channel, event

    def test_a_wake_line_becomes_a_row_and_arms_no_timer(self):
        """#165: the promise survives the timer. `WAKE in=10m check=… ` is progress owed."""
        channel, event = self.channel()
        body = channel.apply_wake_lines(
            'merging once green\nWAKE in=10m check="gh pr checks 75" then="merge it"',
            event("hi", username="ClaudeBot"))
        self.assertNotIn('check="gh pr checks 75"', body)   # the line itself is gone
        self.assertIn("merging once green", body)
        self.assertIn("no longer arms a timer", body)       # and the bot is told to use WAIT
        rows = channel._test_table.load()
        self.assertEqual([row.kind for row in rows], ["progress"])
        self.assertEqual(rows[0].item, "#75")
        self.assertIn("gh pr checks 75", rows[0].owes)
        self.assertIn("merge it", rows[0].opened_by)
        # The row is due when the timer would first have fired (in=10m).
        self.assertAlmostEqual(rows[0].deadline - time.time(), 600.0, delta=30.0)

    def test_a_repeating_check_is_refused_with_a_pointer(self):
        """#213 review: `every=` promised a recurrence the rows do not perform."""
        channel, event = self.channel()
        body = channel.apply_wake_lines(
            'x\nWAKE in=1m every=5m check="gh pr checks 209" then="merge it"',
            event("hi", username="ClaudeBot"))
        self.assertIn("`every=` is not carried", body)
        self.assertIn("Write one `WAIT`", body)
        self.assertEqual(channel._test_table.load(), [])

    def test_until_is_refused_beside_every(self):
        """#213 review: `until=` was the same bug one keyword over -- promised, then dropped."""
        channel, event = self.channel()
        body = channel.apply_wake_lines(
            'x\nWAKE in=1m until=2h check="gh pr checks 209" then="merge it"',
            event("hi", username="ClaudeBot"))
        self.assertIn("`until=` is not carried", body)
        self.assertNotIn('check="gh pr checks 209"', body)   # the line is still stripped
        self.assertEqual(channel._test_table.load(), [])     # and no row was opened

    def test_a_refused_line_is_stripped_and_explained(self):
        channel, event = self.channel()
        body = channel.apply_wake_lines(
            'x\nWAKE in=1m check="gh pr merge 75" then="go"',
            event("hi", username="ClaudeBot"))
        self.assertNotIn('check="gh pr merge 75"', body)     # the line itself is gone
        self.assertIn("WAKE refused", body)
        self.assertEqual(channel._test_table.load(), [])

    def test_slash_wakes_points_at_the_rows_instead(self):
        """#215: the command outlives the timers, as a pointer to what replaced them."""
        channel, _event = self.channel()
        said = channel.control_report("bot.wake")     # the picker's report, and /wakes' own text
        self.assertIn("retired", said)
        self.assertIn("/owed", said)

    def test_status_no_longer_counts_timers(self):
        channel, _event = self.channel()
        self.assertNotIn("wakes=", channel._command_reply("status"))


class TestOwnBriefIsNotAddressedToItself(unittest.TestCase):

    def channel(self):
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""})
        return make_channel(config), event

    def test_the_self_address_is_dropped(self):
        channel, _event = self.channel()
        alias = channel.config.identity.alias
        self.assertEqual(
            channel._unaddress_self(f"@{alias} (own brief): Nothing changed on #120."),
            "Nothing changed on #120.")
        self.assertEqual(channel._unaddress_self(f"@{alias}: re-arming."), "re-arming.")

    def test_an_address_to_a_peer_stays(self):
        channel, _event = self.channel()
        self.assertEqual(channel._unaddress_self("@agy_dd_bot: please QA #151."),
                         "@agy_dd_bot: please QA #151.")
