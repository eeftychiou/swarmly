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

    def test_ready_fires_once_and_waits_to_be_marked_done(self):
        timer = self.timer()
        action = wk.decide(timer, 0, now=1000.0)
        self.assertEqual(action.kind, "fire")
        # Not "done" yet: the transport writes the turn down first, then retires the timer,
        # so a crash in between replays it instead of losing it.
        self.assertEqual(timer.state, "firing")
        self.assertEqual(timer.fires, 1)
        self.assertFalse(timer.in_flight)          # no turn written yet
        timer.pending = "the turn"
        self.assertTrue(timer.in_flight)

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
            firing = wk.WakeStore.firing_of(self.timer())
            name = f"abc12345-{firing}-claude.json"
            self.assertEqual(transport.pushed, [[f".agents/wakes/claude/{name}"]])
            written = json.loads((Path(tmp) / ".agents" / "wakes" / "claude"
                                  / name).read_text(encoding="utf-8"))
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
        """A lone bot: no bus ref, so the claim has no other bot to race with."""
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        state_dir = Path(tmp.name) / ".agents" / "state"
        channel.wake_store = lambda: wk.WakeStore(
            channel.config.identity.alias, state_dir=state_dir, transport=None,
            root=Path(tmp.name), clock=time.time)
        return channel

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
        self.assertTrue(asyncio.run(channel._wake_tick(now=time.time()))[0].startswith("paused"))


class TestAttachedFlagBypass(unittest.TestCase):
    """Blocker 1: an attached flag value must not smuggle a write past the allowlist."""

    def test_attached_write_flags_are_refused(self):
        for command in ("gh api repos/x/issues -XPOST",
                        "gh api repos/x/issues --field=body=hi",
                        "gh api repos/x/issues -fbody=hi",
                        "gh api repos/x/issues --method=POST",
                        "gh api graphql -fquery=mutation",
                        "git diff --output=/tmp/x",
                        "git log --output /tmp/x",
                        "git log -o/tmp/x"):
            with self.subTest(command=command), self.assertRaises(wk.WakeError):
                wk.validate_check(command)

    def test_flag_names_are_normalized(self):
        self.assertEqual(wk.flag_name("-XPOST"), "-X")
        self.assertEqual(wk.flag_name("--field=body=x"), "--field")
        self.assertEqual(wk.flag_name("--output=/tmp/x"), "--output")
        self.assertIsNone(wk.flag_name("repos/x"))
        self.assertIsNone(wk.flag_name("--"))

    def test_gh_api_takes_reads_only(self):
        self.assertEqual(wk.validate_check("gh api repos/x/pulls/9"),
                         ("gh", "api", "repos/x/pulls/9"))
        self.assertEqual(wk.validate_check("gh api repos/x/pulls/9 --jq .state"),
                         ("gh", "api", "repos/x/pulls/9", "--jq", ".state"))
        with self.assertRaises(wk.WakeError):
            wk.validate_check("gh api repos/x/pulls/9 --paginatee")

    def test_a_normal_git_read_still_passes(self):
        self.assertEqual(wk.validate_check("git log --oneline -5"),
                         ("git", "log", "--oneline", "-5"))


class TestPerFiringClaims(unittest.TestCase):
    """Blocker 2: a claim belongs to one firing, so a dead lower alias cannot block the next."""

    def timer(self, next_at=1000.0, ident="abc12345"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge", next_at=next_at,
                            expires_at=next_at + 1000, every_seconds=300.0, chat_id=-1,
                            id=ident)

    def test_a_stale_claim_from_an_earlier_firing_does_not_block_the_next(self):
        with TemporaryDirectory() as tmp:
            first = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: 1000.0)
            self.assertTrue(first.claim(self.timer(next_at=1000.0)))
            # The lowest alias is gone before the next firing; another bot must fire it.
            later = wk.WakeStore("zulu", state_dir=Path(tmp), transport=_BusTransport(tmp),
                                 root=Path(tmp), clock=lambda: 1300.0)
            self.assertTrue(later.claim(self.timer(next_at=1300.0)))

    def test_the_claims_are_per_firing_on_disk(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("claude", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: 1000.0)
            store.claim(self.timer(next_at=1000.0))
            store.claim(self.timer(next_at=1300.0))
            names = sorted(path.name for path in
                           (Path(tmp) / ".agents" / "wakes" / "claude").glob("*.json"))
            self.assertEqual(names, ["abc12345-1000-claude.json", "abc12345-1300-claude.json"])


class TestClaimIsNotFailOpen(unittest.TestCase):
    """Blocker 3: two simultaneous claims must not both fire, and a bad bus must not fire."""

    def timer(self, ident="abc12345"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                            expires_at=2000.0, every_seconds=300.0, chat_id=-1, id=ident)

    def test_two_bots_who_both_saw_nothing_leave_one_winner(self):
        with TemporaryDirectory() as tmp:
            # A genuine race: each bot's *pre*-read sees nothing, both write, and the shared
            # post-read (the fold) decides. Exactly one wins, and it is the lowest alias.
            class Blind(_BusTransport):
                def __init__(self, root):
                    super().__init__(root)
                    self.calls = 0

                def list_at_ref(self, relpath):
                    self.calls += 1
                    if self.calls == 1:            # the pre-read, before either claim exists
                        return []
                    return super().list_at_ref(relpath)

            antigravity = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                       transport=Blind(tmp), root=Path(tmp),
                                       clock=lambda: 1000.0)
            zulu = wk.WakeStore("zulu", state_dir=Path(tmp), transport=Blind(tmp),
                                root=Path(tmp), clock=lambda: 1000.0)
            results = {"antigravity": antigravity.claim(self.timer()),
                       "zulu": zulu.claim(self.timer())}
            self.assertEqual(sum(1 for won in results.values() if won), 1, results)
            self.assertTrue(results["antigravity"], results)

    def test_a_post_read_without_our_own_claim_does_not_fire(self):
        with TemporaryDirectory() as tmp:
            class NeverConfirms(_BusTransport):
                """The bus never shows our claim: unconfirmed is not a win."""

                def list_at_ref(self, relpath):
                    return []

            store = wk.WakeStore("claude", state_dir=Path(tmp),
                                 transport=NeverConfirms(tmp), root=Path(tmp),
                                 clock=lambda: 1000.0)
            self.assertFalse(store.claim(self.timer()))

    def test_an_unreadable_bus_does_not_fire(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("claude", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp, readable=False), root=Path(tmp))
            self.assertFalse(store.claim(self.timer()))

    def test_a_failed_push_does_not_fire(self):
        with TemporaryDirectory() as tmp:
            class Failing(_BusTransport):
                def commit_and_push(self, message, paths=None):
                    raise RuntimeError("push failed")

            store = wk.WakeStore("claude", state_dir=Path(tmp), transport=Failing(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            self.assertFalse(store.claim(self.timer()))

    def test_a_tick_that_loses_the_claim_starts_no_turn(self):
        import asyncio
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        started = []
        channel._run_own_task = lambda *args, **kwargs: started.append(args)
        store = wk.WakeStore(channel.config.identity.alias,
                             state_dir=Path(tmp.name) / "state", transport=None,
                             root=Path(tmp.name), clock=time.time)
        store.claim = lambda timer: False                  # another bot won the firing
        channel.wake_store = lambda: store
        store.add(wk.WakeTimer(argv=("git", "status"), then="go", next_at=1.0,
                               expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                               alias=channel.config.identity.alias), max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{store.load()[0].id}: another bot claimed this firing"])
        self.assertEqual(started, [])


class TestQuotedText(unittest.TestCase):
    """Should-fix: an unquoted free-text field must be refused, not truncated."""

    def test_unquoted_then_is_refused(self):
        with self.assertRaises(wk.WakeError):
            wk.parse_line('WAKE in=10m check="gh pr checks 75" then=merge it if review holds')

    def test_unquoted_check_is_refused(self):
        with self.assertRaises(wk.WakeError):
            wk.parse_line("WAKE in=10m check=gh pr checks 75 then=\"go\"")


class TestStorageIsPerTimer(unittest.TestCase):
    """Should-fix: one file per timer, so parallel turns cannot lose a timer."""

    def test_two_stores_adding_at_once_keep_both(self):
        with TemporaryDirectory() as tmp:
            one = wk.WakeStore("claude", state_dir=Path(tmp), clock=lambda: 1000.0)
            two = wk.WakeStore("claude", state_dir=Path(tmp), clock=lambda: 1000.0)
            one.add(wk.WakeTimer(argv=("git", "status"), then="a", next_at=1.0, expires_at=99,
                                 every_seconds=0, chat_id=-1), max_timers=10)
            two.add(wk.WakeTimer(argv=("git", "log"), then="b", next_at=1.0, expires_at=99,
                                 every_seconds=0, chat_id=-1), max_timers=10)
            self.assertEqual(len(one.load()), 2)

    def test_a_corrupt_timer_file_only_loses_that_timer(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("claude", state_dir=Path(tmp), clock=lambda: 1000.0)
            store.add(wk.WakeTimer(argv=("git", "status"), then="a", next_at=1.0,
                                   expires_at=99, every_seconds=0, chat_id=-1), max_timers=10)
            (Path(tmp) / "wake-claude" / "broken.json").write_text("{", encoding="utf-8")
            self.assertEqual(len(store.load()), 1)


class TestFetchIsGone(unittest.TestCase):
    """Codex blocker 1: `git fetch ext::<cmd>` is arbitrary command execution."""

    def test_git_fetch_is_not_allowed(self):
        with self.assertRaises(wk.WakeError):
            wk.validate_check("git fetch origin")
        with self.assertRaises(wk.WakeError):
            wk.validate_check("git fetch ext::curl -I https://example.invalid")

    def test_git_ls_remote_is_still_allowed(self):
        self.assertEqual(wk.validate_check("git ls-remote origin"),
                         ("git", "ls-remote", "origin"))


class TestRefreshBeforeClaim(unittest.TestCase):
    """R1: a peer's claim from a minute ago must be visible, not wait for the poll."""

    def timer(self, ident="abc12345"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                            expires_at=2000.0, every_seconds=300.0, chat_id=-1, id=ident)

    def test_a_claim_only_after_refresh_is_honoured(self):
        with TemporaryDirectory() as tmp:
            class Lagging(_BusTransport):
                """The ref is stale until refresh(): a peer's claim is invisible before it."""

                def __init__(self, root):
                    super().__init__(root)
                    self.refreshes = 0

                def list_at_ref(self, relpath):
                    if self.refreshes < 2:          # pre-read and its re-read are stale
                        return []
                    return super().list_at_ref(relpath)

                def refresh(self):
                    self.refreshes += 1

            winner = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                  transport=_BusTransport(tmp), root=Path(tmp),
                                  clock=lambda: 1000.0)
            self.assertTrue(winner.claim(self.timer()))

            lagging = Lagging(tmp)
            store = wk.WakeStore("zulu", state_dir=Path(tmp), transport=lagging,
                                 root=Path(tmp), clock=lambda: 1000.0)
            self.assertFalse(store.claim(self.timer()))
            self.assertGreaterEqual(lagging.refreshes, 1)


class TestAdoption(unittest.TestCase):
    """Codex blocker 3: a successor must be able to take a dead bot's timer over."""

    def timer(self, alias="antigravity"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge it",
                            next_at=1000.0, expires_at=2000.0, every_seconds=300.0,
                            chat_id=-7, thread_id=12, topic="Round 4", alias=alias,
                            id="abc12345")

    def test_a_claim_carries_the_whole_timer(self):
        with TemporaryDirectory() as tmp:
            dead = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            self.assertTrue(dead.claim(self.timer()))
            payload = json.loads(next(
                (Path(tmp) / ".agents" / "wakes" / "antigravity").glob("*.json")
            ).read_text(encoding="utf-8"))
            self.assertEqual(payload["timer"]["then"], "merge it")
            self.assertEqual(payload["timer"]["topic"], "Round 4")
            self.assertEqual(payload["timer"]["thread_id"], 12)

    def test_a_successor_adopts_the_armed_timer(self):
        with TemporaryDirectory() as tmp:
            dead = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            dead.claim(self.timer())
            heir = wk.WakeStore("claudebot", state_dir=Path(tmp) / "heir",
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            taken = heir.adopt(max_timers=10)
            self.assertEqual([timer.id for timer in taken], ["abc12345"])
            self.assertEqual(taken[0].alias, "claudebot")
            self.assertEqual(taken[0].then, "merge it")
            self.assertEqual(heir.live_count, 1)

    def test_a_finished_timer_is_not_adopted(self):
        with TemporaryDirectory() as tmp:
            dead = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            finished = self.timer()
            finished.state = "done"
            dead.claim(finished)
            heir = wk.WakeStore("claudebot", state_dir=Path(tmp) / "heir",
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            self.assertEqual(heir.adopt(max_timers=10), [])


class TestCrashWindow(unittest.TestCase):
    """Codex blocker 3b: a ready turn written down is replayed, not lost."""

    def channel(self):
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        state_dir = Path(tmp.name) / "state"
        channel.wake_store = lambda: wk.WakeStore(
            channel.config.identity.alias, state_dir=state_dir, transport=None,
            root=Path(tmp.name), clock=time.time)
        return channel

    def test_a_written_but_unstarted_turn_is_replayed_once(self):
        import asyncio
        channel = self.channel()
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        store = channel.wake_store()
        timer = wk.WakeTimer(argv=("git", "status"), then="merge it", next_at=1.0,
                             expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                             alias=channel.config.identity.alias, state="firing",
                             pending="the turn that was about to start")
        store.add(timer, max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{timer.id}: replayed after a restart"])
        self.assertEqual(started, ["the turn that was about to start"])
        self.assertEqual(store.live_count, 0)
        # A second tick replays nothing: the turn is not repeated.
        started.clear()
        self.assertEqual(asyncio.run(channel._wake_tick(now=time.time())), [])
        self.assertEqual(started, [])
