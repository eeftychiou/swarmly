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

    def test_formatting_round_trips(self):
        self.assertEqual(wk.format_duration(600), "10m")
        self.assertEqual(wk.format_duration(45), "45s")
        self.assertEqual(wk.format_duration(7200), "2h")


class TestCheckAllowlist(unittest.TestCase):
    """Blocker 1: the allowlist is a property of the whole argv, and read-only."""

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


class TestClassify(unittest.TestCase):
    """Blocker 2: a failing check must not look like a pending one."""

    def test_gh_pr_checks(self):
        argv = ("gh", "pr", "checks", "75")
        self.assertEqual(wk.classify(argv, 0), wk.READY)
        self.assertEqual(wk.classify(argv, 8), wk.PENDING)
        self.assertEqual(wk.classify(argv, 1), wk.ERROR)

    def test_other_commands_default_to_ready_or_error(self):
        argv = ("git", "status")
        self.assertEqual(wk.classify(argv, 0), wk.READY)
        self.assertEqual(wk.classify(argv, 128), wk.ERROR)


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


class TestTimer(unittest.TestCase):

    def timer(self, **kwargs):
        base = dict(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                    expires_at=2000.0, every_seconds=300.0, chat_id=-1, alias="Claude",
                    id="abc12345")
        base.update(kwargs)
        return wk.WakeTimer(**base)

    def test_round_trip(self):
        timer = self.timer()
        again = wk.WakeTimer.from_dict(timer.to_dict())
        self.assertEqual(again.argv, timer.argv)
        self.assertEqual(again.next_at, timer.next_at)
        self.assertEqual(again.id, timer.id)

    def test_a_bad_record_is_refused(self):
        for data in ({"argv": []}, {"argv": ["gh"], "next_at": "x"},
                     {"argv": ["gh"], "next_at": 1, "expires_at": 2, "every_seconds": 3}):
            with self.subTest(data=data), self.assertRaises(wk.WakeError):
                wk.WakeTimer.from_dict(data)

    def test_ready_fires_once_and_is_done(self):
        timer = self.timer()
        action = wk.decide(timer, 0, now=1000.0)
        self.assertEqual(action.kind, "fire")
        self.assertEqual(timer.state, "done")
        self.assertEqual(timer.fires, 1)

    def test_pending_rearms_silently(self):
        timer = self.timer()
        action = wk.decide(timer, 8, now=1000.0)
        self.assertEqual(action.kind, "rearm")
        self.assertEqual(timer.next_at, 1300.0)
        self.assertEqual(timer.state, "armed")

    def test_pending_past_until_expires_once(self):
        timer = self.timer(next_at=1950.0, expires_at=2000.0)
        action = wk.decide(timer, 8, now=1950.0)
        self.assertEqual(action.kind, "expire")
        self.assertEqual(timer.state, "expired")

    def test_a_failing_check_starts_a_turn_instead_of_rearming(self):
        timer = self.timer()
        action = wk.decide(timer, 1, now=1000.0)
        self.assertEqual(action.kind, "error")
        self.assertEqual(timer.state, "failed")

    def test_a_timeout_is_an_error(self):
        timer = self.timer()
        action = wk.decide(timer, None, now=1000.0)
        self.assertEqual(action.kind, "error")

    def test_the_attempt_count_bounds_the_chain(self):
        timer = self.timer(attempts=4)
        action = wk.decide(timer, 8, now=1000.0, max_attempts=5)
        self.assertEqual(action.kind, "expire")
        self.assertIn("gave up", action.reason)


class TestRunCheck(unittest.TestCase):
    """A check really runs, without a shell, and a timeout kills the process group."""

    def test_a_real_command_runs_and_reports_its_code(self):
        code, tail = wk.run_check(("git", "--version"), cwd=None, timeout=30)
        self.assertEqual(code, 0)
        self.assertIn("git version", tail)

    def test_a_missing_binary_is_an_error_not_a_crash(self):
        code, tail = wk.run_check(("definitely-not-a-binary-xyz",), cwd=None, timeout=10)
        self.assertIsNone(code)
        self.assertIn("could not run", tail)

    @unittest.skipIf(os.name == "nt", "process-group timing differs on Windows")
    def test_a_hanging_check_is_killed(self):
        started = time.time()
        code, tail = wk.run_check(("sleep", "30"), cwd=None, timeout=0.5)
        self.assertIsNone(code)
        self.assertLess(time.time() - started, 15)
        self.assertIn("timed out", tail)


class TestStore(unittest.TestCase):

    def store(self, root, alias="Claude"):
        return wk.WakeStore(alias, state_dir=Path(root), clock=lambda: 1000.0)

    def timer(self, **kwargs):
        base = dict(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                    expires_at=2000.0, every_seconds=300.0, chat_id=-1, alias="Claude")
        base.update(kwargs)
        return wk.WakeTimer(**base)

    def test_a_timer_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            self.store(tmp).add(self.timer(), max_timers=10)
            again = self.store(tmp).load()          # a fresh store: a restarted transport
            self.assertEqual(len(again), 1)
            self.assertEqual(again[0].argv, ("gh", "pr", "checks", "75"))
            self.assertEqual(again[0].next_at, 1000.0)

    def test_the_cap_is_enforced(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.add(self.timer(), max_timers=1)
            with self.assertRaises(wk.WakeError):
                store.add(self.timer(), max_timers=1)

    def test_cancel_by_prefix(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            timer = self.timer()
            store.add(timer, max_timers=10)
            self.assertTrue(store.cancel(timer.id[:4]))
            self.assertEqual(store.live_count, 0)

    def test_due_only_returns_live_timers_at_or_after_their_time(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.add(self.timer(next_at=900.0), max_timers=10)
            store.add(self.timer(next_at=1500.0), max_timers=10)
            due = store.due(1000.0)
            self.assertEqual([timer.next_at for timer in due], [900.0])

    def test_a_corrupt_file_does_not_stop_the_bot(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "wake-claude.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(self.store(tmp).load(), [])


class _BusTransport:
    def __init__(self, root, readable=True):
        self.root = Path(root)
        self.ref = "origin/main"
        self.readable = readable
        self.pushed = []

    def refresh(self):
        pass

    def list_at_ref(self, relpath):
        """Names relative to *relpath*, subdirectories included (as GitTransport does)."""
        if not self.readable:
            return None
        folder = self.root / relpath
        if not folder.is_dir():
            return []
        return sorted(str(path.relative_to(folder)).replace("\\", "/")
                      for path in folder.rglob("*.json"))

    def read_at_ref(self, relpath):
        path = self.root / relpath
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def commit_and_push(self, message, paths=None):
        self.pushed.append(list(paths or []))
        return True


class TestClaims(unittest.TestCase):
    """Blocker 3: a firing is claimed before the turn, so a takeover cannot fire it twice."""

    def timer(self, alias="claude", ident="abc12345"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                            expires_at=2000.0, every_seconds=300.0, chat_id=-1, alias=alias,
                            id=ident)

    def test_without_a_bus_the_bot_is_alone(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("Claude", state_dir=Path(tmp), transport=None, root=Path(tmp))
            self.assertTrue(store.claim(self.timer()))

    def test_the_first_claim_wins_and_is_pushed(self):
        with TemporaryDirectory() as tmp:
            transport = _BusTransport(tmp)
            store = wk.WakeStore("claude", state_dir=Path(tmp), transport=transport,
                                 root=Path(tmp), clock=lambda: 1000.0)
            self.assertTrue(store.claim(self.timer()))
            self.assertEqual(transport.pushed, [[".agents/wakes/claude/abc12345-claude.json"]])
            written = json.loads((Path(tmp) / ".agents" / "wakes" / "claude"
                                  / "abc12345-claude.json").read_text(encoding="utf-8"))
            self.assertEqual(written["check"], "gh pr checks 75")

    def test_a_second_bot_loses_to_the_lowest_alias(self):
        with TemporaryDirectory() as tmp:
            winner = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                  transport=_BusTransport(tmp), root=Path(tmp),
                                  clock=lambda: 1000.0)
            self.assertTrue(winner.claim(self.timer(alias="antigravity")))
            later = wk.WakeStore("zulu", state_dir=Path(tmp), transport=_BusTransport(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            self.assertFalse(later.claim(self.timer(alias="zulu")))

    def test_a_claim_outside_the_document_directories_is_refused(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("../../etc", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp))
            self.assertTrue(store.claim(self.timer()))     # the alias is sanitized, not refused
            self.assertTrue((Path(tmp) / ".agents" / "wakes").is_dir())
            self.assertFalse((Path(tmp).parent / "etc").exists())


class TestChannelWiring(unittest.TestCase):
    """A reply arms a timer, the line never reaches the channel, and /wakes lists it."""

    def channel(self):
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        return make_channel(config), event

    def test_a_wake_line_is_armed_and_stripped(self):
        channel, event = self.channel()
        body = channel.apply_wake_lines(
            'merging once green\nWAKE in=10m every=5m until=2h check="gh pr checks 75" '
            'then="merge it"',
            event("hi", username="ClaudeBot"))
        self.assertNotIn("WAKE", body)
        self.assertIn("merging once green", body)
        self.assertIn("⏰ wake", body)
        self.assertEqual(channel.wake_store().live_count, 1)

    def test_a_refused_line_is_stripped_and_explained(self):
        channel, event = self.channel()
        body = channel.apply_wake_lines(
            'x\nWAKE in=1m check="gh pr merge 75" then="go"',
            event("hi", username="ClaudeBot"))
        self.assertNotIn('check="gh pr merge 75"', body)     # the line itself is gone
        self.assertIn("WAKE refused", body)
        self.assertEqual(channel.wake_store().live_count, 0)

    def test_slash_wakes_lists_and_cancels(self):
        from tests.test_telegram_channel import event as make_event
        channel, event = self.channel()
        self.assertIn("none armed", channel.wake_summary())
        channel.apply_wake_lines('x\nWAKE in=1m check="git status" then="go"',
                                 event("hi", username="ClaudeBot"))
        self.assertIn("wakes=1", channel.wake_summary())
        timer_id = channel.wake_store().load()[-1].id
        admin = make_event(f"/wakes cancel {timer_id}", author_id=1234567890,
                           username="admin", is_bot=False)
        self.assertIn("cancelled", channel._wakes_command(admin))
        self.assertEqual(channel.wake_store().live_count, 0)

    def test_only_the_admin_cancels(self):
        from tests.test_telegram_channel import event as make_event
        channel, event = self.channel()
        channel.apply_wake_lines('x\nWAKE in=1m check="git status" then="go"',
                                 event("hi", username="ClaudeBot"))
        timer_id = channel.wake_store().load()[-1].id
        peer = make_event(f"/wakes cancel {timer_id}", author_id=202, username="CodexBot")
        self.assertIn("only the admin", channel._wakes_command(peer))
        self.assertEqual(channel.wake_store().live_count, 1)

    def test_status_shows_the_count(self):
        channel, _event = self.channel()
        self.assertIn("wakes=0", channel._command_reply("status"))


class TestTick(unittest.TestCase):
    """A due timer runs its check off the event loop and starts exactly one turn."""

    def channel(self):
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        return make_channel(config)

    def test_ready_fires_one_turn_and_retires_the_timer(self):
        import asyncio
        channel = self.channel()
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        store = channel.wake_store()
        timer = wk.WakeTimer(argv=("git", "status"), then="merge it", next_at=1.0,
                             expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                             alias=channel.config.identity.alias)
        store.add(timer, max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{timer.id}: fire"])
        self.assertEqual(len(started), 1)
        self.assertIn("merge it", started[0])
        self.assertEqual(channel.wake_store().live_count, 0)

    def test_a_frozen_bot_runs_no_check(self):
        import asyncio
        channel = self.channel()
        channel._frozen = True
        self.assertTrue(asyncio.run(channel._wake_tick(now=time.time()))[0].startswith("frozen"))
