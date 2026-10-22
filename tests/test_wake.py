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
                store.add(self.timer(argv=("gh", "pr", "checks", "76")), max_timers=1)

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
            store.add(self.timer(next_at=1500.0, argv=("gh", "pr", "checks", "76")),
                      max_timers=10)
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
            relpath = f".agents/wakes/claude/{name}"
            # One push: the claim goes to the bus as it is, and the bus's history orders it.
            self.assertEqual(transport.pushed, [[relpath]])
            written = json.loads((Path(tmp) / ".agents" / "wakes" / "claude"
                                  / name).read_text(encoding="utf-8"))
            self.assertEqual(written["check"], "gh pr checks 75")
            self.assertGreater(written["written_at"], 0)     # advisory; the bus history orders

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
        self.assertNotIn('check="gh pr checks 75"', body)   # the line itself is gone
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
        channel.lease_fence = lambda now=None: True     # the lease is not what these test
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

    def test_a_bot_that_is_not_the_coordinator_still_fires_its_own_timer(self):
        """Live, round 4: the lease fence returned early for every non-coordinator, so the
        developer's WAKE never fired. The fence is for adopting a dead bot's timers only."""
        import asyncio
        channel = self.channel()
        channel.lease_fence = lambda now=None: False
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        store = channel.wake_store()
        own = wk.WakeTimer(argv=("git", "status"), then="push the next slice", next_at=1.0,
                           expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                           alias=channel.config.identity.alias)
        adopted = wk.WakeTimer(argv=("git", "status"), then="merge it", next_at=1.0,
                               expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                               alias="SomeoneElse")
        store.add(own, max_timers=10)
        store.add(adopted, max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertIn(f"{own.id}: fire", done)
        self.assertIn(f"{adopted.id}: adopted, held while this bot is not the coordinator", done)
        self.assertEqual(len(started), 1)
        self.assertIn("push the next slice", started[0])

    def test_a_frozen_bot_runs_no_check(self):
        import asyncio
        channel = self.channel()
        channel._frozen = True
        self.assertTrue(asyncio.run(channel._wake_tick(now=time.time()))[0].startswith("paused"))


class TestHeldWhileBusy(unittest.TestCase):
    """Round 5.5 item 1 (#160): a due WAKE waits out a turn already running in its topic.

    Replays of the 22 Oct incidents this covers. The coordinator's standing
    ``gh issue view 120`` check came due while a turn was running in that topic, so a second
    turn queued behind it and paid a full model turn to say "nothing changed"; a turn hung on
    a background ``find`` over the whole disk did the same for fifteen minutes. Neither the
    check nor the turn may start while the topic is busy: the timer stays armed, unclaimed,
    and is evaluated -- expiry included -- on the first tick after the turn ends.
    """

    def channel(self):
        """A lone bot whose store, check and claim can all be watched from the test."""
        from unittest import mock
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        channel.lease_fence = lambda now=None: True        # the lease is not what these test
        store = wk.WakeStore(channel.config.identity.alias,
                             state_dir=Path(tmp.name) / "state", transport=None,
                             root=Path(tmp.name), clock=time.time)
        channel.wake_store = lambda: store
        started: list = []
        checks: list = []
        claims: list = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        real_claim = store.claim_state
        store.claim_state = lambda timer, settle_seconds=0.0: (
            claims.append(timer.id) or real_claim(timer, settle_seconds=settle_seconds))

        async def check(argv, *, cwd, timeout):
            checks.append(argv)
            return 0, "state: OPEN\n[ ] #167", wk.output_digest("state: OPEN\n[ ] #167")

        patcher = mock.patch("swarmly.telegram.wake.run_check_async", check)
        patcher.start()
        self.addCleanup(patcher.stop)
        return channel, store, started, checks, claims

    def timer(self, channel, **fields):
        """The 22 Oct standing check: ``gh issue view 120``, always true, every 5 minutes."""
        fields.setdefault("argv", ("gh", "issue", "view", "120"))
        fields.setdefault("then", "check whether the round moved")
        fields.setdefault("next_at", 1.0)
        fields.setdefault("expires_at", time.time() + 3600)
        fields.setdefault("every_seconds", 300)
        fields.setdefault("chat_id", -1)
        fields.setdefault("alias", channel.config.identity.alias)
        return wk.WakeTimer(**fields)

    def test_a_standing_check_due_behind_a_running_turn_is_held(self):
        import asyncio
        channel, store, started, checks, claims = self.channel()
        timer = self.timer(channel, thread_id=307)
        store.add(timer, max_timers=10)
        channel._busy_since[(-1, 307)] = channel._clock()   # the coordinator's own turn
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{timer.id}: held: turn running"])
        self.assertEqual(checks, [])          # no check ran
        self.assertEqual(started, [])         # no second turn queued behind the first
        self.assertEqual(claims, [])          # the firing was not claimed on the bus
        self.assertEqual(store.live_count, 1)  # still armed, still owed
        self.assertEqual(store.load()[0].state, "armed")

    def test_the_held_check_fires_on_the_first_tick_after_the_turn_ends(self):
        import asyncio
        channel, store, started, checks, claims = self.channel()
        timer = self.timer(channel, thread_id=307)
        store.add(timer, max_timers=10)
        channel._busy_since[(-1, 307)] = channel._clock()
        asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(claims, [])
        channel._busy_since.pop((-1, 307))     # the turn ended
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{timer.id}: fire"])
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(started), 1)
        self.assertIn("check whether the round moved", started[0])
        self.assertEqual(store.live_count, 0)

    def test_a_check_due_behind_a_turn_running_for_fifteen_minutes_is_still_held(self):
        """The 22 Oct hang: a turn stuck on a whole-disk ``find`` for 15 minutes."""
        import asyncio
        channel, store, started, checks, _claims = self.channel()
        timer = self.timer(channel, thread_id=307)
        store.add(timer, max_timers=10)
        channel._busy_since[(-1, 307)] = channel._clock() - 900
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{timer.id}: held: turn running"])
        self.assertEqual((started, checks), ([], []))

    def test_a_check_due_in_a_topic_that_is_not_busy_still_fires(self):
        import asyncio
        channel, store, started, _checks, claims = self.channel()
        busy_topic = self.timer(channel, id="busy0001", thread_id=307)
        free_topic = self.timer(channel, id="free0001", thread_id=308)
        store.add(busy_topic, max_timers=10)
        store.add(free_topic, max_timers=10)
        channel._busy_since[(-1, 307)] = channel._clock()
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, ["busy0001: held: turn running", "free0001: fire"])
        self.assertEqual(claims, ["free0001"])
        self.assertEqual(len(started), 1)
        by_id = {row.id: row for row in store.load()}
        self.assertEqual(by_id["free0001"].state, "done")
        self.assertEqual(by_id["busy0001"].state, "armed")   # held, not retired

    def test_a_held_check_past_its_until_expires_once_the_turn_ends(self):
        import asyncio
        channel, store, started, checks, _claims = self.channel()
        now = time.time()
        timer = self.timer(channel, thread_id=307, expires_at=now - 30)
        store.add(timer, max_timers=10)
        channel._busy_since[(-1, 307)] = channel._clock()
        done = asyncio.run(channel._wake_tick(now=now))
        self.assertEqual(done, [f"{timer.id}: held: turn running"])
        self.assertEqual((started, checks), ([], []))
        self.assertEqual(store.live_count, 1)  # held, not silently dropped
        channel._busy_since.pop((-1, 307))
        done = asyncio.run(channel._wake_tick(now=now + 20))
        self.assertEqual(done, [f"{timer.id}: expired"])
        self.assertEqual(len(started), 1)
        self.assertIn(f"WAKE {timer.id} expired", started[0])
        self.assertEqual(store.live_count, 0)


class TestOneTickOneTurn(unittest.TestCase):
    """#171: two WAKEs due in one topic in the same tick start one turn, not two.

    #160 holds a due timer for a topic that has a turn *running*, and `_busy_since` only knows
    about a turn once it has begun. Two timers due in the same pass would therefore both fire
    before the first one's turn registered: the same paid "nothing changed", one tick apart.
    """

    channel = TestHeldWhileBusy.channel
    timer = TestHeldWhileBusy.timer

    def test_two_checks_due_in_one_topic_start_one_turn(self):
        import asyncio
        channel, store, started, checks, claims = self.channel()
        first = self.timer(channel, id="first001", thread_id=307,
                           argv=("gh", "pr", "checks", "169"))
        second = self.timer(channel, id="second01", thread_id=307, next_at=2.0,
                            argv=("gh", "issue", "view", "167"))
        store.add(first, max_timers=10)
        store.add(second, max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, ["first001: fire", "second01: held: turn running"])
        self.assertEqual(checks, [("gh", "pr", "checks", "169")])   # one check ran
        self.assertEqual(len(started), 1)                           # one turn started
        self.assertEqual(claims, ["first001"])
        by_id = {row.id: row for row in store.load()}
        self.assertEqual(by_id["first001"].state, "done")
        self.assertEqual(by_id["second01"].state, "armed")           # still owed, not lost

    def test_the_held_second_check_fires_on_the_next_tick(self):
        import asyncio
        channel, store, started, checks, _claims = self.channel()
        first = self.timer(channel, id="first001", thread_id=307)
        second = self.timer(channel, id="second01", thread_id=307, next_at=2.0,
                            argv=("gh", "issue", "view", "167"))
        store.add(first, max_timers=10)
        store.add(second, max_timers=10)
        asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(len(started), 1)
        done = asyncio.run(channel._wake_tick(now=time.time()))     # the turn has ended
        self.assertEqual(done, ["second01: fire"])
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(started), 2)
        self.assertEqual(store.live_count, 0)

    def test_two_checks_due_in_different_topics_still_both_fire(self):
        import asyncio
        channel, store, started, checks, _claims = self.channel()
        store.add(self.timer(channel, id="topic307", thread_id=307), max_timers=10)
        store.add(self.timer(channel, id="topic308", thread_id=308, next_at=2.0), max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, ["topic307: fire", "topic308: fire"])
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(started), 2)

    def test_an_expiry_turn_holds_the_topics_other_timer_too(self):
        import asyncio
        channel, store, started, checks, _claims = self.channel()
        now = time.time()
        expiring = self.timer(channel, id="expiring", thread_id=307, expires_at=now - 30)
        later = self.timer(channel, id="later001", thread_id=307, next_at=2.0,
                           argv=("gh", "issue", "view", "167"))
        store.add(expiring, max_timers=10)
        store.add(later, max_timers=10)
        done = asyncio.run(channel._wake_tick(now=now))
        self.assertEqual(done, ["expiring: expired", "later001: held: turn running"])
        self.assertEqual(checks, [])                 # no check for the held one
        self.assertEqual(len(started), 1)            # the expiry turn only
        self.assertEqual(store.live_count, 1)


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
        store.claim_state = lambda timer, settle_seconds=0: "taken"  # another bot won it
        channel.wake_store = lambda: store
        channel.lease_fence = lambda now=None: True
        store.add(wk.WakeTimer(argv=("git", "status"), then="go", next_at=1.0,
                               expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                               alias=channel.config.identity.alias), max_timers=10)
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{store.load()[0].id}: another bot claimed this firing; "
                                "copy retired"])
        self.assertEqual(started, [])
        # Retired, not retried: the copy no longer counts against wake_max (live, round 5).
        self.assertFalse(store.load()[0].live)


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

    def test_git_ls_remote_is_gone_too(self):
        # It takes a remote argument just like fetch, so `ext::` reaches it as well.
        for command in ("git ls-remote origin", "git ls-remote ext::sh -c id"):
            with self.subTest(command=command), self.assertRaises(wk.WakeError):
                wk.validate_check(command)


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
            dead.add(self.timer(), max_timers=10)        # arming publishes the state
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
            dead.write(finished)
            dead.publish_state(finished)
            heir = wk.WakeStore("claudebot", state_dir=Path(tmp) / "heir",
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            self.assertEqual(heir.adopt(max_timers=10), [])


class TestStaleCopiesDoNotPileUp(unittest.TestCase):
    """Live, round 5: ten armed copies of other bots' timers, days overdue, filled wake_max and
    refused the WAKE that would have merged a green PR. The watchdog took them for promises."""

    def timer(self, alias="antigravity", ident="abc12345"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge it",
                            next_at=1000.0, expires_at=2000.0, every_seconds=300.0,
                            chat_id=-7, thread_id=12, topic="Round 4", alias=alias, id=ident)

    def test_an_adopted_copy_says_whose_it_was_and_leaves_the_cap_alone(self):
        with TemporaryDirectory() as tmp:
            dead = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            dead.add(self.timer(), max_timers=10)
            heir = wk.WakeStore("claudebot", state_dir=Path(tmp) / "heir",
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            [taken] = heir.adopt(max_timers=10)
            self.assertEqual(taken.adopted_from, "antigravity")
            self.assertEqual(wk.WakeTimer.from_dict(taken.to_dict()).adopted_from, "antigravity")
            # The heir's own cap is one: the adopted copy does not take that slot.
            self.assertTrue(heir.add(self.timer(alias="claudebot", ident="own00001"),
                                     max_timers=1) is not None)

    def test_a_peer_that_is_up_keeps_its_own_timer(self):
        with TemporaryDirectory() as tmp:
            owner = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: 1000.0)
            owner.add(self.timer(), max_timers=10)
            heir = wk.WakeStore("claudebot", state_dir=Path(tmp) / "heir",
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            self.assertEqual(heir.adopt(max_timers=10, skip=("Antigravity",)), [])
            self.assertEqual(len(heir.adopt(max_timers=10)), 1)   # DOWN, or nobody can tell

    def tick_channel(self):
        import asyncio  # noqa: F401 - used by the callers
        from tests.test_telegram_channel import make_channel, make_config
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
        channel.wake_store = lambda: store
        channel.lease_fence = lambda now=None: True
        return channel, store, started

    def test_a_timer_long_past_its_until_is_retired_without_a_turn(self):
        import asyncio
        channel, store, started = self.tick_channel()
        now = time.time()
        store.write(wk.WakeTimer(argv=("gh", "issue", "view", "120"), then="go",
                                 next_at=now - 86400, expires_at=now - 80000, every_seconds=300,
                                 chat_id=-1, alias=channel.config.identity.alias, id="old00001"))
        done = asyncio.run(channel._wake_tick(now=now))
        self.assertTrue(done[0].startswith("old00001: retired, expired"), done)
        self.assertEqual(started, [])
        self.assertEqual(store.live_count, 0)

    def test_a_timer_just_past_its_until_still_gets_its_expiry_turn(self):
        import asyncio
        channel, store, started = self.tick_channel()
        now = time.time()
        store.write(wk.WakeTimer(argv=("gh", "pr", "checks", "146"), then="merge it",
                                 next_at=now - 60, expires_at=now - 30, every_seconds=300,
                                 chat_id=-1, alias=channel.config.identity.alias, id="new00001"))
        done = asyncio.run(channel._wake_tick(now=now))
        self.assertEqual(done, ["new00001: expired"])
        self.assertEqual(len(started), 1)
        self.assertIn("WAKE new00001 expired", started[0][0])

    def test_an_adopted_copy_goes_back_when_its_owner_is_up(self):
        import asyncio
        channel, store, started = self.tick_channel()
        now = time.time()
        channel._peers_not_down = lambda moment: ("Codex",)
        store.write(wk.WakeTimer(argv=("gh", "pr", "checks", "90"), then="go",
                                 next_at=now - 60, expires_at=now + 3600, every_seconds=300,
                                 chat_id=-1, alias=channel.config.identity.alias, id="cdx00001",
                                 adopted_from="Codex"))
        done = asyncio.run(channel._wake_tick(now=now))
        self.assertEqual(done, ["cdx00001: handed back to Codex"])
        self.assertEqual(started, [])
        self.assertEqual(store.live_count, 0)

    def test_the_watchdog_and_the_pulse_ignore_a_stale_timer(self):
        channel, store, _started = self.tick_channel()
        now = time.time()
        store.write(wk.WakeTimer(argv=("gh", "issue", "view", "120"), then="go",
                                 next_at=now - 86400, expires_at=now + 86400, every_seconds=300,
                                 chat_id=-1, alias=channel.config.identity.alias, id="old00002"))
        self.assertEqual(channel.status_snapshot(now).wakes, ())


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
        channel.lease_fence = lambda now=None: True     # the lease is not what these test
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
        self.assertEqual(len(started), 1)
        self.assertIn("interrupted by a restart", started[0])   # at-least-once, said plainly
        self.assertIn("the turn that was about to start", started[0])
        self.assertEqual(store.live_count, 0)
        # A second tick replays nothing: the turn is not repeated.
        started.clear()
        self.assertEqual(asyncio.run(channel._wake_tick(now=time.time())), [])
        self.assertEqual(started, [])


class TestStateRecords(unittest.TestCase):
    """B2: a state record per timer, newest wins, terminal states are never adopted."""

    def timer(self, ident="abc12345", alias="antigravity"):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge it",
                            next_at=1000.0, expires_at=2000.0, every_seconds=300.0,
                            chat_id=-7, alias=alias, id=ident)

    def heir(self, root):
        return wk.WakeStore("claudebot", state_dir=Path(root) / "heir",
                            transport=_BusTransport(root), root=Path(root),
                            clock=lambda: 5000.0)

    def test_a_timer_with_several_firings_is_adopted_once(self):
        with TemporaryDirectory() as tmp:
            dead = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            for firing in (1000.0, 1300.0, 1600.0):
                dead.claim(self.timer())
                dead.publish_state(self.timer())
                moved = self.timer()
                moved.next_at = firing
                dead.write(moved)
            self.assertEqual([timer.id for timer in self.heir(tmp).adopt(max_timers=10)],
                             ["abc12345"])

    def test_a_cancelled_timer_is_not_adopted(self):
        with TemporaryDirectory() as tmp:
            dead = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                transport=_BusTransport(tmp), root=Path(tmp),
                                clock=lambda: 1000.0)
            dead.add(self.timer(), max_timers=10)
            dead.cancel("abc12345")
            self.assertEqual(self.heir(tmp).adopt(max_timers=10), [])

    def test_the_newest_state_wins(self):
        with TemporaryDirectory() as tmp:
            old = wk.WakeStore("antigravity", state_dir=Path(tmp),
                               transport=_BusTransport(tmp), root=Path(tmp),
                               clock=lambda: 1000.0)
            old.write(self.timer())
            old.publish_state(self.timer())
            newer = wk.WakeStore("codex", state_dir=Path(tmp) / "c",
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: 9000.0)
            done = self.timer()
            done.state = "done"
            newer.write(done)
            newer.publish_state(done)                    # later stamp, terminal
            self.assertEqual(self.heir(tmp).adopt(max_timers=10), [])


class TestAdoptionIsUntrusted(unittest.TestCase):
    """A state record is bus input: anyone who can push can write one, so validate it."""

    def record(self, argv, ident="evil0001"):
        return wk.WakeTimer(argv=tuple(argv), then="do it", next_at=1000.0,
                            expires_at=2000.0, every_seconds=0, chat_id=-1,
                            alias="antigravity", id=ident)

    def publish(self, root, timer):
        dead = wk.WakeStore(timer.alias, state_dir=Path(root) / "dead",
                            transport=_BusTransport(root), root=Path(root),
                            clock=lambda: 1000.0)
        dead.write(timer)
        dead.publish_state(timer)

    def heir(self, root):
        return wk.WakeStore("claudebot", state_dir=Path(root) / "heir",
                            transport=_BusTransport(root), root=Path(root),
                            clock=lambda: 5000.0)

    def test_a_forged_record_cannot_smuggle_a_command(self):
        with TemporaryDirectory() as tmp:
            self.publish(tmp, self.record(["sh", "-c", "id"]))
            self.assertEqual(self.heir(tmp).adopt(max_timers=10), [])

    def test_a_write_flag_in_a_record_is_refused(self):
        with TemporaryDirectory() as tmp:
            self.publish(tmp, self.record(["gh", "api", "repos/x", "-XPOST"]))
            self.assertEqual(self.heir(tmp).adopt(max_timers=10), [])

    def test_the_bots_own_allowlist_gates_adoption_too(self):
        with TemporaryDirectory() as tmp:
            self.publish(tmp, self.record(["git", "status"]))
            self.assertEqual(self.heir(tmp).adopt(max_timers=10, allow=("gh",)), [])
            self.assertEqual(len(self.heir(tmp).adopt(max_timers=10, allow=("git",))), 1)


class TestRefreshFailure(unittest.TestCase):
    """Codex: a failed refresh must not be a claim on a stale ref."""

    def timer(self):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                            expires_at=2000.0, every_seconds=300.0, chat_id=-1,
                            id="abc12345")

    def test_a_failed_fetch_does_not_fire(self):
        with TemporaryDirectory() as tmp:
            class NoFetch(_BusTransport):
                def refresh(self):
                    return False

            store = wk.WakeStore("claude", state_dir=Path(tmp), transport=NoFetch(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            self.assertFalse(store.claim(self.timer()))

    def test_a_failed_fetch_before_the_post_read_does_not_fire(self):
        with TemporaryDirectory() as tmp:
            class FailsLater(_BusTransport):
                def __init__(self, root):
                    super().__init__(root)
                    self.calls = 0

                def refresh(self):
                    self.calls += 1
                    return self.calls == 1          # the pre-read fetch works, the post does not

            store = wk.WakeStore("claude", state_dir=Path(tmp), transport=FailsLater(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            self.assertFalse(store.claim(self.timer()))


class TestSettleWindow(unittest.TestCase):
    """Codex: two overlapping pushes must not both fire."""

    def timer(self):
        return wk.WakeTimer(argv=("gh", "pr", "checks", "75"), then="merge", next_at=1000.0,
                            expires_at=2000.0, every_seconds=300.0, chat_id=-1,
                            id="abc12345")

    def test_the_first_sighting_writes_the_claim_and_waits(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("claude", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: 1000.0)
            self.assertEqual(store.claim_state(self.timer(), settle_seconds=30), "wait")
            self.assertFalse(store.claim(self.timer(), settle_seconds=30))

    def test_once_the_window_has_passed_the_lowest_alias_fires(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            lower = wk.WakeStore("antigravity", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: now["t"])
            higher = wk.WakeStore("zulu", state_dir=Path(tmp), transport=_BusTransport(tmp),
                                  root=Path(tmp), clock=lambda: now["t"])
            self.assertEqual(lower.claim_state(self.timer(), settle_seconds=30), "wait")
            self.assertEqual(higher.claim_state(self.timer(), settle_seconds=30), "taken")
            now["t"] += 60
            self.assertEqual(higher.claim_state(self.timer(), settle_seconds=30), "taken")
            self.assertEqual(lower.claim_state(self.timer(), settle_seconds=30), "won")

    def test_without_a_settle_window_a_single_bot_fires_immediately(self):
        with TemporaryDirectory() as tmp:
            store = wk.WakeStore("claude", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: 1000.0)
            self.assertTrue(store.claim(self.timer(), settle_seconds=0))


class TestOverlappingPushRace(unittest.TestCase):
    """Codex/QA repro: beta fires before alpha's push lands, then alpha fires too.

    Two things stop it: a claim is not honoured until it has settled (so beta cannot fire on
    the tick it wrote), and the winner is the earliest claim, not the lowest alias (so alpha
    pushing later cannot overtake a claim that is already there).
    """

    def timer(self):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1,
                            alias="beta", id="t1")

    def test_the_repro_does_not_double_fire(self):
        with TemporaryDirectory() as tmp:
            class Scripted(_BusTransport):
                """The bus, seen at the moment each bot asks: beta pushes first, alpha after."""

                def __init__(self, root, alias, script):
                    super().__init__(root)
                    self.alias = alias
                    self.script = list(script)
                    self.calls = 0

                def list_at_ref(self, relpath):
                    self.calls += 1
                    if self.script:
                        return self.script.pop(0)
                    return super().list_at_ref(relpath)

            now = {"t": 1000.0}
            beta = wk.WakeStore("beta", state_dir=Path(tmp),
                                transport=Scripted(tmp, "beta", [[], ["beta/t1-1060-beta.json"]]),
                                root=Path(tmp), clock=lambda: now["t"])
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp),
                                 transport=Scripted(tmp, "alpha",
                                                    [["beta/t1-1060-beta.json"],
                                                     ["alpha/t1-1060-alpha.json",
                                                      "beta/t1-1060-beta.json"]]),
                                 root=Path(tmp), clock=lambda: now["t"])
            self.assertFalse(beta.claim(self.timer(), settle_seconds=30),
                             "beta must not fire on the tick it wrote its claim")
            now["t"] += 60                       # alpha pushed in the meantime
            won = [store.claim(self.timer(), settle_seconds=30) for store in (beta, alpha)]
            self.assertEqual(sum(1 for result in won if result), 1, won)

    def test_a_late_lower_alias_cannot_overtake_the_earlier_claim(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            first = wk.WakeStore("zulu", state_dir=Path(tmp), transport=_BusTransport(tmp),
                                 root=Path(tmp), clock=lambda: now["t"])
            self.assertEqual(first.claim_state(self.timer(), settle_seconds=30), "wait")
            now["t"] += 60
            self.assertEqual(first.claim_state(self.timer(), settle_seconds=30), "won")
            # The lower alias only writes now; the firing is already claimed by an older one.
            later = wk.WakeStore("alpha", state_dir=Path(tmp), transport=_BusTransport(tmp),
                                 root=Path(tmp), clock=lambda: now["t"])
            self.assertEqual(later.claim_state(self.timer(), settle_seconds=30), "taken")


class TestPublicationSurfaces(unittest.TestCase):
    """Codex/QA repro: publish_state() swallowed push failures."""

    def timer(self):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1000.0,
                            expires_at=2000.0, every_seconds=0, chat_id=-1,
                            alias="claude", id="abc12345")

    def test_add_raises_when_the_bus_refuses_the_state(self):
        with TemporaryDirectory() as tmp:
            class Refuses(_BusTransport):
                def commit_and_push(self, message, paths=None):
                    raise RuntimeError("push rejected")

            store = wk.WakeStore("claude", state_dir=Path(tmp), transport=Refuses(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            with self.assertRaises(wk.WakePushError):
                store.add(self.timer(), max_timers=10)
            self.assertEqual(store.live_count, 1)       # it is armed here, and said so

    def test_a_refused_state_is_reported_in_the_wake_ack(self):
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        store = wk.WakeStore(channel.config.identity.alias,
                             state_dir=Path(tmp.name) / "state", transport=None,
                             root=Path(tmp.name), clock=time.time)

        def refuse(timer, *, max_timers):
            store.write(timer)
            raise wk.WakePushError("the bus did not take the state")

        store.add = refuse
        channel.wake_store = lambda: store
        channel.lease_fence = lambda now=None: True
        body = channel.apply_wake_lines('x\nWAKE in=1m check="git status" then="go"',
                                        event("hi", username="ClaudeBot"))
        self.assertIn("armed locally only", body)
        self.assertIn("the bus did not take the state", body)


class TestDelayedPush(unittest.TestCase):
    """Codex: the settle window must start when the *push* returns, not when the file is
    written, so a claim still in flight cannot be overtaken by a peer firing."""

    def timer(self):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1, id="t1")

    def bus(self, root, *, held):
        """A bus where alpha's claims stay invisible until *held* is cleared."""

        class Bus(_BusTransport):
            def refresh(self):
                return True

            def list_at_ref(self, relpath):
                names = []
                for owner in ("alpha", "beta"):
                    if owner == "alpha" and held["alpha"]:
                        continue                    # still in flight, not on the bus yet
                    folder = self.root / relpath / owner
                    if folder.is_dir():
                        names.extend(f"{owner}/{path.name}" for path in folder.glob("*.json"))
                return sorted(names)

            def read_at_ref(self, relpath):
                path = self.root / relpath
                return path.read_text(encoding="utf-8") if path.is_file() else None

        return Bus(root)

    def test_a_lower_alias_whose_push_is_slow_cannot_double_fire(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            held = {"alpha": True}
            beta = wk.WakeStore("beta", state_dir=Path(tmp), transport=self.bus(tmp, held=held),
                                root=Path(tmp), clock=lambda: now["t"])
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp),
                                 transport=self.bus(tmp, held=held), root=Path(tmp),
                                 clock=lambda: now["t"])
            self.assertEqual(beta.claim_state(self.timer(), settle_seconds=30), "wait")
            # alpha's push has not landed, so the bus does not confirm its own claim: it must
            # not fire, and it will not start firing later either.
            self.assertEqual(alpha.claim_state(self.timer(), settle_seconds=30), "taken")
            now["t"] += 31
            self.assertEqual(beta.claim_state(self.timer(), settle_seconds=30), "won")
            # alpha's claim finally lands: it finds beta's claim and loses.
            held["alpha"] = False
            now["t"] += 120
            self.assertEqual(alpha.claim_state(self.timer(), settle_seconds=30), "taken")

    def test_the_window_is_measured_from_the_confirmation(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            store = wk.WakeStore("claude", state_dir=Path(tmp),
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: now["t"])
            self.assertEqual(store.claim_state(self.timer(), settle_seconds=30), "wait")
            now["t"] += 10
            self.assertEqual(store.claim_state(self.timer(), settle_seconds=30), "wait")
            now["t"] += 25
            self.assertEqual(store.claim_state(self.timer(), settle_seconds=30), "won")


class TestTransitionPublication(unittest.TestCase):
    """Codex: replace() must not leave stale armed state on the bus after a failed publish."""

    def timer(self):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1000.0,
                            expires_at=2000.0, every_seconds=0, chat_id=-1, id="abc12345")

    def test_a_failed_publish_on_a_transition_raises(self):
        with TemporaryDirectory() as tmp:
            class Refuses(_BusTransport):
                def commit_and_push(self, message, paths=None):
                    raise RuntimeError("push rejected")

            store = wk.WakeStore("claude", state_dir=Path(tmp), transport=Refuses(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            timer = self.timer()
            store.write(timer)
            timer.state = "done"
            with self.assertRaises(wk.WakePushError):
                store.replace(timer)

    def test_the_tick_reports_a_transition_it_could_not_publish(self):
        import asyncio
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        store = wk.WakeStore(channel.config.identity.alias,
                             state_dir=Path(tmp.name) / "state", transport=None,
                             root=Path(tmp.name), clock=time.time)
        channel.wake_store = lambda: store
        channel.lease_fence = lambda now=None: True
        timer = wk.WakeTimer(argv=("git", "status"), then="go", next_at=1.0,
                             expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                             alias=channel.config.identity.alias)
        store.add(timer, max_timers=10)
        real_replace = store.replace

        def refuse_transitions(updated, **kwargs):
            if updated.state != "armed":
                raise wk.WakePushError("the bus did not take the transition")
            return real_replace(updated, **kwargs)

        store.replace = refuse_transitions
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(len(started), 1)                 # the turn still runs
        self.assertTrue(any("not on the bus" in line for line in done), done)


class TestInFlightClaim(unittest.TestCase):
    """B4 as QA modelled it: the stamp is the time the *push* returned, not the write."""

    def timer(self, alias="alpha"):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1,
                            alias=alias, id="t1")

    def bus(self, root, now, *, slow=0.0):
        class Bus(_BusTransport):
            def refresh(self):
                return True

            def commit_and_push(self, message, paths=None):
                if slow:
                    now["t"] += slow          # the push takes this long to return
                return True

        return Bus(root)

    def test_a_slow_push_holds_the_firing_and_the_other_bot_loses(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp),
                                 transport=self.bus(tmp, now, slow=40.0),
                                 root=Path(tmp), clock=lambda: now["t"])
            zulu = wk.WakeStore("zulu", state_dir=Path(tmp), transport=self.bus(tmp, now),
                                root=Path(tmp), clock=lambda: now["t"])
            self.assertEqual(alpha.claim_state(self.timer(), settle_seconds=30), "wait")
            # alpha's push took 40s and landed; zulu, starting after it, finds the claim.
            self.assertEqual(zulu.claim_state(self.timer("zulu"), settle_seconds=30), "taken")
            now["t"] += 40
            self.assertEqual(alpha.claim_state(self.timer(), settle_seconds=30), "won")

    def test_the_earliest_stamp_wins_when_both_claims_are_visible(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / ".agents" / "wakes"
            for alias, stamp in (("alpha", 1040.0), ("zulu", 1005.0)):
                (folder / alias).mkdir(parents=True)
                timer = self.timer(alias)
                (folder / alias / f"t1-1060-{alias}.json").write_text(json.dumps(
                    {"id": "t1", "alias": alias, "firing": 1060, "written_at": stamp,
                     "timer": timer.to_dict()}), encoding="utf-8")
            now = {"t": 1100.0}
            zulu = wk.WakeStore("zulu", state_dir=Path(tmp) / "z",
                                 transport=self.bus(tmp, now), root=Path(tmp),
                                 clock=lambda: now["t"])
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=self.bus(tmp, now), root=Path(tmp),
                                 clock=lambda: now["t"])
            # zulu's push returned earlier even though alpha's alias is lower.
            self.assertEqual(zulu.claim_state(self.timer("zulu"), settle_seconds=0), "won")
            self.assertEqual(alpha.claim_state(self.timer(), settle_seconds=0), "taken")

    def test_a_claim_on_the_ref_without_a_confirmed_stamp_is_not_a_claim(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / ".agents" / "wakes" / "alpha"
            folder.mkdir(parents=True)
            (folder / "t1-1060-alpha.json").write_text(json.dumps(
                {"id": "t1", "alias": "alpha", "firing": 1060, "written_at": 0,
                 "timer": self.timer().to_dict()}), encoding="utf-8")
            store = wk.WakeStore("alpha", state_dir=Path(tmp), transport=_BusTransport(tmp),
                                 root=Path(tmp), clock=lambda: 1000.0)
            # Placed but never stamped: nobody counts it, including its own writer.
            self.assertEqual(store._claim_records("t1", 1060), [])


class TestReadyTurnIsNotDropped(unittest.TestCase):
    """B6 as QA modelled it: a failed state publish must not lose the ready turn."""

    def test_a_refused_launch_publication_still_starts_the_turn(self):
        import asyncio
        from tests.test_telegram_channel import make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": ""},
                             limits={"wake_tick_seconds": 0})
        channel = make_channel(config)
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        store = wk.WakeStore(channel.config.identity.alias,
                             state_dir=Path(tmp.name) / "state", transport=None,
                             root=Path(tmp.name), clock=time.time)
        channel.wake_store = lambda: store
        channel.lease_fence = lambda now=None: True
        timer = wk.WakeTimer(argv=("git", "status"), then="merge it", next_at=1.0,
                             expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                             alias=channel.config.identity.alias)
        store.add(timer, max_timers=10)
        real_replace = store.replace

        def refuse_bus(updated, **kwargs):
            # The local record is written; only the bus copy of the transition fails.
            store.write(updated)
            if updated.state != "armed":
                raise wk.WakePushError("the bus did not take the transition")
            return real_replace(updated, **kwargs)

        store.replace = refuse_bus
        done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(len(started), 1, "the ready turn must still run")
        self.assertIn("merge it", started[0])
        self.assertTrue(any("not on the bus" in line for line in done), done)
        self.assertEqual(store.live_count, 0)          # and the timer is retired locally


class TestBusHistoryOrdersClaims(unittest.TestCase):
    """B4: the winner is decided by when the bus *saw* a claim, not by a time inside it."""

    def timer(self, alias):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1,
                            alias=alias, id="t1")

    def bus(self, root, rank):
        """A bus that says where each claim sits in its history: higher rank = earlier."""

        class Historic(_BusTransport):
            def arrival_rank(self, relpath):
                return rank.get(relpath.rsplit("/", 1)[-1])

        return Historic(root)

    def claim_file(self, root, alias, written_at):
        folder = Path(root) / ".agents" / "wakes" / alias
        folder.mkdir(parents=True, exist_ok=True)
        name = f"t1-1060-{alias}.json"
        (folder / name).write_text(json.dumps(
            {"id": "t1", "alias": alias, "firing": 1060, "written_at": written_at,
             "timer": self.timer(alias).to_dict()}), encoding="utf-8")

    def test_a_claim_that_lies_about_its_stamp_loses_to_the_one_the_bus_saw_first(self):
        with TemporaryDirectory() as tmp:
            # alpha says 1000 in its own file but only reached the bus at 1050.
            self.claim_file(tmp, "alpha", 1000.0)
            # beta says 1040 and reached the bus at 1040.
            self.claim_file(tmp, "beta", 1040.0)
            # beta's claim is one commit further from the tip: the bus got it first.
            seen = {"t1-1060-alpha.json": 0, "t1-1060-beta.json": 1}
            now = {"t": 1100.0}
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=self.bus(tmp, seen), root=Path(tmp),
                                 clock=lambda: now["t"])
            beta = wk.WakeStore("beta", state_dir=Path(tmp) / "b",
                                transport=self.bus(tmp, seen), root=Path(tmp),
                                clock=lambda: now["t"])
            self.assertEqual(beta.claim_state(self.timer("beta"), settle_seconds=0), "won")
            self.assertEqual(alpha.claim_state(self.timer("alpha"), settle_seconds=0), "taken")

    def test_a_slow_confirmation_push_cannot_win_by_its_earlier_write(self):
        with TemporaryDirectory() as tmp:
            # The exact residual Codex/Reasonix described: the file says 1000, the bus saw it
            # at 1050, after beta's 1040 claim had already settled and fired.
            self.claim_file(tmp, "alpha", 1000.0)
            self.claim_file(tmp, "beta", 1040.0)
            seen = {"t1-1060-alpha.json": 0, "t1-1060-beta.json": 1}
            now = {"t": 1080.0}
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=self.bus(tmp, seen), root=Path(tmp),
                                 clock=lambda: now["t"])
            beta = wk.WakeStore("beta", state_dir=Path(tmp) / "b",
                                transport=self.bus(tmp, seen), root=Path(tmp),
                                clock=lambda: now["t"])
            # beta settled and fired (the window is behind it); alpha's claim lands later.
            self.assertEqual(beta.claim_state(self.timer("beta"), settle_seconds=0), "won")
            now["t"] += 40
            self.assertEqual(alpha.claim_state(self.timer("alpha"), settle_seconds=0), "taken")


class TestPushLandsAfterAPeerFired(unittest.TestCase):
    """B4 exactly as reported: a claim whose push lands *after* another bot fired.

    The stamp is the time the push returned, so the late-landing claim is ordered *after* the
    claim that already fired -- and loses. Ordering by the pre-push write time is what let it
    win, and that was the hole.
    """

    def timer(self, alias):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1,
                            alias=alias, id="t1")

    def claim_file(self, root, alias, stamp):
        folder = Path(root) / ".agents" / "wakes" / alias
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"t1-1060-{alias}.json").write_text(json.dumps(
            {"id": "t1", "alias": alias, "firing": 1060, "written_at": stamp,
             "timer": self.timer(alias).to_dict()}), encoding="utf-8")

    def test_a_claim_landing_after_the_firing_loses_by_its_later_stamp(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1040.0}
            # beta claimed at 1000, settled, and fired; its claim carries stamp 1000.
            self.claim_file(tmp, "beta", 1000.0)
            # alpha wrote at 1000 but its push only landed at 1040, so its stamp is 1040.
            self.claim_file(tmp, "alpha", 1040.0)
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: now["t"])
            beta_store = wk.WakeStore("beta", state_dir=Path(tmp) / "b",
                                      transport=_BusTransport(tmp), root=Path(tmp),
                                      clock=lambda: now["t"])
            now["t"] = 1100.0
            # Its own claim has been on the bus since 1040 (settled), and beta's is earlier: lost.
            self.assertEqual(alpha.claim_state(self.timer("alpha"), settle_seconds=30), "taken")
            # and beta, already fired, does not fire a second time either: its own record says
            # done, so nothing on the claim path can make it fire again.
            fired = self.timer("beta")
            fired.state, fired.replays = "done", 1
            beta_store.write(fired)
            self.assertFalse(beta_store.due(now=1100.0))


class TestDelayedWriterPush(unittest.TestCase):
    """The case both reviewers asked for: `_write_claim` runs and the push carrying the claim is
    delayed past the moment a peer fires. No claim file is written by hand here."""

    def timer(self, alias):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1,
                            alias=alias, id="t1")

    def bus(self, root, now, *, delay=0.0, book=None):
        """A bus that ranks every pushed path, and whose pushes can take extra time."""
        shared = book if book is not None else {"pushed": [], "paths": {}}

        class Ordered(_BusTransport):
            def refresh(self):
                return True

            def commit_and_push(self, message, paths=None):
                for path in paths or []:
                    shared["paths"][path] = len(shared["pushed"])
                    shared["pushed"].append(path)
                if delay:
                    now["t"] += delay          # the push returns only later
                return True

            def arrival_rank(self, relpath):
                seen = shared["paths"].get(relpath)
                # How many pushes came after it: higher means it got there earlier.
                return None if seen is None else len(shared["pushed"]) - 1 - seen

        return Ordered(root)

    def test_a_claim_whose_push_is_delayed_loses_to_the_one_the_bus_got_first(self):
        """alpha's `_write_claim` runs first, but its push is held until beta has fired.

        The writer is exercised (the point of the test): alpha's claim only reaches the ref
        after beta's, so alpha is ordered behind a firing that already happened.
        """
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            book = {"pushed": [], "paths": {}}
            held: list = []

            class Ranked(_BusTransport):
                """A bus that ranks pushed paths, and can hold one bot's push in flight."""

                def __init__(self, root, hold=False):
                    super().__init__(root)
                    self.hold = hold

                def refresh(self):
                    return True

                def commit_and_push(self, message, paths=None):
                    for path in paths or []:
                        if self.hold:
                            held.append(path)               # accepted here, not on the ref
                        else:
                            book["paths"][path] = len(book["pushed"])
                            book["pushed"].append(path)
                    return True

                def release(self):
                    for path in held:
                        book["paths"][path] = len(book["pushed"])
                        book["pushed"].append(path)
                    held.clear()

                def list_at_ref(self, relpath):
                    names = []
                    for alias in ("alpha", "beta"):
                        folder = self.root / relpath / alias
                        if not folder.is_dir():
                            continue
                        for path in folder.glob("*.json"):
                            full = f"{relpath}/{alias}/{path.name}"
                            if full in book["paths"]:       # only what reached the ref
                                names.append(f"{alias}/{path.name}")
                    return sorted(names)

                def arrival_rank(self, relpath):
                    seen = book["paths"].get(relpath)
                    return None if seen is None else len(book["pushed"]) - 1 - seen

                def read_at_ref(self, relpath):
                    path = self.root / relpath
                    return path.read_text(encoding="utf-8") if path.is_file() else None

            alpha_bus = Ranked(tmp, hold=True)
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp) / "a", transport=alpha_bus,
                                 root=Path(tmp), clock=lambda: now["t"])
            beta = wk.WakeStore("beta", state_dir=Path(tmp) / "b", transport=Ranked(tmp),
                                root=Path(tmp), clock=lambda: now["t"])

            # alpha's writer runs first; its push is held, so the ref does not have it yet.
            self.assertEqual(alpha.claim_state(self.timer("alpha"), settle_seconds=0), "lost")
            self.assertEqual(len(held), 1)             # it really was written and pushed
            # beta claims, settles and fires while alpha's claim is still in flight.
            self.assertEqual(beta.claim_state(self.timer("beta"), settle_seconds=30), "wait")
            now["t"] += 31
            self.assertEqual(beta.claim_state(self.timer("beta"), settle_seconds=30), "won")
            # alpha's claim finally lands -- behind the one that already fired: it loses.
            alpha_bus.release()
            self.assertEqual(alpha.claim_state(self.timer("alpha"), settle_seconds=0), "taken")

    def test_two_writers_are_ordered_by_the_bus_not_by_their_own_stamps(self):
        with TemporaryDirectory() as tmp:
            now = {"t": 1000.0}
            book = {"pushed": [], "paths": {}}
            alpha = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=self.bus(tmp, now, book=book), root=Path(tmp),
                                 clock=lambda: now["t"])
            beta = wk.WakeStore("beta", state_dir=Path(tmp) / "b",
                                transport=self.bus(tmp, now, book=book), root=Path(tmp),
                                clock=lambda: now["t"])
            # Both write at the same clock value: only arrival order differs.
            self.assertEqual(alpha.claim_state(self.timer("alpha"), settle_seconds=0), "won")
            self.assertEqual(beta.claim_state(self.timer("beta"), settle_seconds=0), "taken")


class TestUnrankedClaimsFailClosed(unittest.TestCase):
    """Codex/Reasonix: a bus that can rank claims must not fall back to the sender's stamp.

    If `arrival_rank` exists but cannot place a claim, that claim is not a claim. Demoting it to
    `written_at` would put the sender-set key back in charge -- exactly the double fire this
    round was about.
    """

    def timer(self, alias):
        return wk.WakeTimer(argv=("git", "status"), then="go", next_at=1060.0,
                            expires_at=1300.0, every_seconds=60.0, chat_id=1,
                            alias=alias, id="t1")

    def claim_file(self, root, alias, stamp):
        folder = Path(root) / ".agents" / "wakes" / alias
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"t1-1060-{alias}.json").write_text(json.dumps(
            {"id": "t1", "alias": alias, "firing": 1060, "written_at": stamp,
             "timer": self.timer(alias).to_dict()}), encoding="utf-8")

    def bus(self, root, behaviour):
        class Ranked(_BusTransport):
            def refresh(self):
                return True

            def arrival_rank(self, relpath):
                if behaviour == "raises":
                    raise RuntimeError("git failed")
                return None                          # cannot place it

        return Ranked(root)

    def test_a_claim_the_bus_cannot_rank_is_ignored_not_trusted(self):
        with TemporaryDirectory() as tmp:
            # A backdated stamp: under the old fallback this claim would look earliest.
            self.claim_file(tmp, "alpha", 1.0)
            self.claim_file(tmp, "beta", 1040.0)
            now = {"t": 1100.0}
            store = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=self.bus(tmp, "none"), root=Path(tmp),
                                 clock=lambda: now["t"])
            self.assertEqual(store._claim_records("t1", 1060), [])
            # ... and it cannot fire on it either.
            self.assertEqual(store.claim_state(self.timer("alpha"), settle_seconds=0), "lost")

    def test_a_rank_error_also_skips_the_claim(self):
        with TemporaryDirectory() as tmp:
            self.claim_file(tmp, "alpha", 1.0)
            now = {"t": 1100.0}
            store = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=self.bus(tmp, "raises"), root=Path(tmp),
                                 clock=lambda: now["t"])
            self.assertEqual(store._claim_records("t1", 1060), [])
            self.assertEqual(store.claim_state(self.timer("alpha"), settle_seconds=0), "lost")

    def test_a_bus_with_no_arrival_order_still_uses_the_stamp(self):
        with TemporaryDirectory() as tmp:
            self.claim_file(tmp, "alpha", 1000.0)
            now = {"t": 1100.0}
            store = wk.WakeStore("alpha", state_dir=Path(tmp) / "a",
                                 transport=_BusTransport(tmp), root=Path(tmp),
                                 clock=lambda: now["t"])
            records = store._claim_records("t1", 1060)
            self.assertEqual([claim.alias for claim in records], ["alpha"])
            self.assertEqual(records[0].seen_at, 1000.0)


class TestGitBusTimerFires(unittest.TestCase):
    """Live, round 4: on a git bus a claim's settle window is timed by the store's own memory
    (the bus ranks a claim; it gives no time). A store built afresh every tick forgot it and
    said "wait" on every tick, so no WAKE ever fired. The channel keeps one store."""

    def test_a_claimed_timer_fires_once_the_settle_window_passes(self):
        import asyncio
        from tests.test_telegram_channel import make_channel, make_config

        class Ranked(_BusTransport):
            def arrival_rank(self, relpath):
                return 0 if (self.root / relpath).is_file() else None

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_config(agent={"role": "developer"},
                             fallback={"gitbus_root": tmp.name, "gitbus_ref": "origin/main"},
                             limits={"wake_tick_seconds": 0, "wake_claim_settle_seconds": 0.2})
        channel = make_channel(config)
        bus = Ranked(tmp.name)
        channel.bus_transport = lambda root: bus
        channel.lease_fence = lambda now=None: False          # a developer, not the coordinator
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        timer = wk.WakeTimer(argv=("git", "status"), then="continue #82", next_at=1.0,
                             expires_at=time.time() + 3600, every_seconds=0, chat_id=-1,
                             alias=channel.config.identity.alias)
        channel.wake_store().add(timer, max_timers=10)
        first = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertIn("settle window", first[0])
        self.assertEqual(started, [])
        time.sleep(0.3)
        second = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(len(started), 1, second)
        self.assertIn("continue #82", started[0])

    def test_the_channel_keeps_one_store(self):
        from tests.test_telegram_channel import make_channel, make_config
        with TemporaryDirectory() as tmp:
            channel = make_channel(make_config(fallback={"gitbus_root": tmp}))
            self.assertIs(channel.wake_store(), channel.wake_store())


class TestOneStandingCheck(unittest.TestCase):
    """Live, round 5: a standing ``gh issue view 120`` check exits 0 every time, so each
    firing started a turn that said nothing had changed, and each re-arm added a timer until
    the cap refused the WAKE that mattered."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self):
        return wk.WakeStore("Claude", state_dir=Path(self.tmp.name), clock=lambda: 1000.0)

    @staticmethod
    def timer(**kwargs):
        base = dict(argv=("gh", "issue", "view", "120"), then="check the round",
                    next_at=1000.0, expires_at=9000.0, every_seconds=300.0, chat_id=-1,
                    thread_id=7, alias="Claude")
        base.update(kwargs)
        return wk.WakeTimer(**base)

    def test_arming_the_same_check_replaces_the_armed_one(self):
        store = self.store()
        first, second = self.timer(), self.timer(then="check it again")
        store.add(first, max_timers=10)
        self.assertEqual(store.add(second, max_timers=10), [first.id])
        live = [row for row in store.load() if row.live]
        self.assertEqual([row.id for row in live], [second.id])

    def test_re_arms_never_fill_the_cap(self):
        store = self.store()
        for _ in range(5):
            store.add(self.timer(), max_timers=2)
        self.assertEqual(store.live_count, 1)

    def test_the_same_check_in_another_topic_is_another_timer(self):
        store = self.store()
        store.add(self.timer(), max_timers=10)
        self.assertEqual(store.add(self.timer(thread_id=8), max_timers=10), [])
        self.assertEqual(store.live_count, 2)

    def test_a_ready_check_with_the_output_already_acted_on_re_arms(self):
        timer = self.timer(seen_digest=wk.output_digest("open\n[ ] 117"))
        action = wk.decide(timer, 0, now=1000.0, digest=wk.output_digest("open\n[ ] 117\n"))
        self.assertEqual(action.kind, "rearm")
        self.assertEqual(timer.next_at, 1300.0)

    def test_new_output_fires_and_becomes_what_was_acted_on(self):
        timer = self.timer(seen_digest=wk.output_digest("open\n[ ] 117"))
        new = wk.output_digest("open\n[x] 117")
        self.assertEqual(wk.decide(timer, 0, now=1000.0, digest=new).kind, "fire")
        self.assertEqual(timer.seen_digest, new)

    def test_unchanged_until_the_deadline_retires_without_a_turn(self):
        # Live, 22 Oct: an expiry with nothing new still cost a "re-arming" turn (#178).
        same = wk.output_digest("open")
        timer = self.timer(seen_digest=same, expires_at=1100.0)
        action = wk.decide(timer, 0, now=1000.0, digest=same)
        self.assertEqual(action.kind, "retire")
        self.assertEqual(timer.state, "expired")

    def test_a_re_armed_check_inherits_what_the_fired_one_acted_on(self):
        store = self.store()
        fired = self.timer(seen_digest="abc", state="done")
        store.write(fired)
        again = self.timer()
        store.add(again, max_timers=10)
        self.assertEqual(again.seen_digest, "abc")

    def test_the_digest_covers_the_whole_output_not_the_tail(self):
        head = "comments: 3\n" + "\n".join(f"line {n}" for n in range(40))
        moved = "comments: 4\n" + "\n".join(f"line {n}" for n in range(40))
        self.assertEqual(wk.tail_of(head), wk.tail_of(moved))
        self.assertNotEqual(wk.output_digest(head), wk.output_digest(moved))

    def test_the_tick_re_arms_an_unchanged_check_without_a_turn(self):
        import asyncio
        from unittest import mock
        channel = TestTick.channel(self)
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        store = channel.wake_store()
        output = "state: OPEN\n[ ] #117"
        timer = wk.WakeTimer(argv=("gh", "issue", "view", "120"), then="check", next_at=1.0,
                             expires_at=time.time() + 3600, every_seconds=300, chat_id=-1,
                             alias=channel.config.identity.alias,
                             seen_digest=wk.output_digest(output))
        store.add(timer, max_timers=10)

        async def check(argv, *, cwd, timeout):
            return 0, output, wk.output_digest(output)

        with mock.patch("swarmly.telegram.wake.run_check_async", check):
            done = asyncio.run(channel._wake_tick(now=time.time()))
        self.assertEqual(done, [f"{timer.id}: rearm"])
        self.assertEqual(started, [])
        self.assertEqual(channel.wake_store().live_count, 1)


class TestOwnBriefIsNotAddressedToItself(unittest.TestCase):

    def channel(self):
        return TestTick.channel(self)

    def test_the_self_address_is_dropped(self):
        channel = self.channel()
        alias = channel.config.identity.alias
        self.assertEqual(
            channel._unaddress_self(f"@{alias} (own brief): Nothing changed on #120."),
            "Nothing changed on #120.")
        self.assertEqual(channel._unaddress_self(f"@{alias}: re-arming."), "re-arming.")

    def test_an_address_to_a_peer_stays(self):
        channel = self.channel()
        self.assertEqual(channel._unaddress_self("@agy_dd_bot: please QA #151."),
                         "@agy_dd_bot: please QA #151.")

    def test_the_wake_turn_may_stay_silent(self):
        timer = wk.WakeTimer(argv=("git", "status"), then="go", next_at=1.0, expires_at=2.0,
                             every_seconds=0, chat_id=-1)
        from swarmly.telegram.channel import AgentChannel
        prompt = AgentChannel._wake_prompt(wk.WakeAction("fire", timer))
        self.assertIn("NO_REPLY", prompt)


class TestStaleProofWake(unittest.TestCase):
    """#178, live 22 Oct: one wait on #173 worded three ways became three timers, `gh pr
    checks` output never matched because of its timings, and timers kept acting on heads
    that had moved on or merged."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self):
        return wk.WakeStore("Claude", state_dir=Path(self.tmp.name), clock=lambda: 1000.0)

    @staticmethod
    def timer(argv, **kwargs):
        base = dict(argv=argv, then="merge it", next_at=1000.0, expires_at=9000.0,
                    every_seconds=300.0, chat_id=-1, thread_id=7, alias="Claude")
        base.update(kwargs)
        return wk.WakeTimer(**base)

    def test_the_subject_of_a_check(self):
        cases = {
            ("gh", "pr", "view", "173"): ("pr", "173", ""),
            ("gh", "pr", "view", "173", "--json", "headRefOid,mergeable"): ("pr", "173", ""),
            ("gh", "pr", "checks", "#169", "--required"): ("pr", "169", ""),
            ("gh", "issue", "view", "120", "-R", "o/r"): ("issue", "120", "o/r"),
            ("gh", "api", "repos/o/r/pulls/177/reviews"): ("pr", "177", "o/r"),
            ("gh", "api", "repos/o/r/issues/167/comments"): ("issue", "167", "o/r"),
        }
        for argv, expected in cases.items():
            self.assertEqual(wk.subject_of(argv), expected, argv)
        for argv in (("git", "status"), ("gh", "pr", "list"), ("gh", "run", "view", "5")):
            self.assertIsNone(wk.subject_of(argv), argv)

    def test_three_wordings_of_one_wait_are_one_timer(self):
        store = self.store()
        for argv in (("gh", "pr", "view", "173"),
                     ("gh", "pr", "view", "173", "--json", "headRefOid"),
                     ("gh", "pr", "checks", "173")):
            store.add(self.timer(argv), max_timers=2)
        self.assertEqual(store.live_count, 1)

    def test_timings_and_timestamps_are_not_a_change(self):
        before = ("lint\tpass\t21s\thttps://x/1\nunittest\tpass\t2m3s\thttps://x/2\n"
                  "updated 2026-10-22T18:04:11Z, about 1 hour ago")
        after = ("lint\tpass\t19s\thttps://x/1\nunittest\tpass\t2m41s\thttps://x/2\n"
                 "updated 2026-10-22T18:09:52Z, 5 minutes ago")
        moved = before.replace("unittest\tpass", "unittest\tfail")
        self.assertEqual(wk.output_digest(before), wk.output_digest(after))
        self.assertNotEqual(wk.output_digest(before), wk.output_digest(moved))
        self.assertNotEqual(wk.output_digest("head 8356114"), wk.output_digest("head 1149b3b"))

    def tick(self, timer, state, *, output="same", digest_of=None):
        import asyncio
        from unittest import mock
        channel = TestTick.channel(self)
        started = []
        channel._run_own_task = lambda text, chat_id, thread_id, name: started.append(text)
        timer.alias = channel.config.identity.alias
        channel.wake_store().add(timer, max_timers=10)

        async def check(argv, *, cwd, timeout):
            return 0, output, wk.output_digest(digest_of or output)

        async def subject(subject, *, cwd, timeout=30.0):
            return state

        with mock.patch("swarmly.telegram.wake.run_check_async", check), \
                mock.patch("swarmly.telegram.wake.subject_state_async", subject):
            done = asyncio.run(channel._wake_tick(now=time.time()))
        return channel, done, started

    def test_a_merged_pr_ends_its_wake_without_a_turn(self):
        timer = self.timer(("gh", "pr", "checks", "175"), next_at=1.0,
                           expires_at=time.time() + 3600)
        channel, done, started = self.tick(timer, ("MERGED", "eebf80e0"))
        self.assertEqual(done, [f"{timer.id}: retired: pr #175 is merged"])
        self.assertEqual(started, [])
        self.assertEqual(channel.wake_store().live_count, 0)

    def test_the_wake_turn_names_the_head_as_it_is_now(self):
        timer = self.timer(("gh", "pr", "view", "173"), next_at=1.0,
                           expires_at=time.time() + 3600, then="review 4f523cb")
        _, done, started = self.tick(timer, ("OPEN", "8356114abcdef"))
        self.assertEqual(done, [f"{timer.id}: fire"])
        self.assertIn("PR #173 is open at 8356114 now", started[0])

    def test_an_expiry_with_nothing_new_retires_quietly(self):
        timer = self.timer(("gh", "issue", "view", "120"), next_at=1.0,
                           expires_at=time.time() - 60,
                           seen_digest=wk.output_digest("state: OPEN"))
        channel, done, started = self.tick(timer, ("OPEN", ""), output="state: OPEN")
        self.assertEqual(done, [f"{timer.id}: retired at its until=, nothing changed"])
        self.assertEqual(started, [])

    def test_an_expiry_with_news_still_gets_its_turn(self):
        timer = self.timer(("gh", "issue", "view", "120"), next_at=1.0,
                           expires_at=time.time() - 60,
                           seen_digest=wk.output_digest("state: OPEN"))
        _, done, started = self.tick(timer, ("OPEN", ""), output="state: CLOSED")
        self.assertEqual(done, [f"{timer.id}: expired"])
        self.assertEqual(len(started), 1)

    def test_an_unreadable_subject_changes_nothing(self):
        timer = self.timer(("gh", "pr", "checks", "175"), next_at=1.0,
                           expires_at=time.time() + 3600)
        _, done, started = self.tick(timer, None)
        self.assertEqual(done, [f"{timer.id}: fire"])
        self.assertNotIn("is open at", started[0])
