"""The pulse on the bus: one parentless commit on the bot's own ref (issue #82).

The round-4 review settled the storage: a pulse commit on the bus *branch* every two minutes is
~180 commits an hour of history growth, forever, each a push race. So each bot force-updates
``refs/pulse/<alias>`` instead — a mailbox, not a history — and these tests hold that promise:
no parents, no growth, nothing on the branch, and a restart reads the same facts back.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram import presence as pr
from swarmly.telegram.pulsebus import PULSE_PATH, PulseBus, notes_from
from tests.gitenv import git_env

GIT = shutil.which("git")
NOW = 1_800_000_000.0


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=check, env=git_env())


def make_clone(base: Path) -> Path:
    """A bare origin plus one clone, the shape a transport's bus checkout has."""
    origin = base / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True,
                   env=git_env())
    work = base / "work"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True,
                   env=git_env())
    git(work, "config", "user.name", "DeepSeek")
    git(work, "config", "user.email", "deepseek@a2a.local")
    git(work, "checkout", "--quiet", "-b", "bus")
    (work / "README.md").write_text("bus\n")
    git(work, "add", "README.md")
    git(work, "commit", "--quiet", "-m", "bus begins")
    # The swarm's own guard refuses a push to main from a developer checkout, and rightly:
    # these tests write a bus branch, never main.
    git(work, "push", "--quiet", "origin", "HEAD:bus")
    return work


def pulse(alias, at, **kwargs):
    return pr.Pulse(alias=alias, at=at, **kwargs)


@unittest.skipIf(GIT is None, "git is not installed")
class TestThePulseRef(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.work = make_clone(self.base)
        self.bus = PulseBus(self.work, "Hermes")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_pulse_is_one_parentless_commit_on_the_bots_own_ref(self):
        self.assertTrue(self.bus.publish(pulse("Hermes", NOW, rev="abc1234", term=2)))
        ref = "refs/pulse/Hermes"
        # A mailbox, not a history: one commit, and it has no parent.
        self.assertEqual(git(self.work, "rev-list", "--count", ref).stdout.strip(), "1")
        body = git(self.work, "cat-file", "-p", ref).stdout
        self.assertNotIn("parent ", body)
        content = json.loads(git(self.work, "show", f"{ref}:{PULSE_PATH}").stdout)
        self.assertEqual(content["alias"], "Hermes")
        self.assertEqual(content["rev"], "abc1234")
        self.assertEqual(content["term"], 2)

    def test_the_second_beat_replaces_the_first_and_grows_no_history(self):
        self.bus.publish(pulse("Hermes", NOW, rev="one"))
        self.bus.publish(pulse("Hermes", NOW + 120, rev="two"))
        ref = "refs/pulse/Hermes"
        self.assertEqual(git(self.work, "rev-list", "--count", ref).stdout.strip(), "1")
        content = json.loads(git(self.work, "show", f"{ref}:{PULSE_PATH}").stdout)
        self.assertEqual(content["rev"], "two")

    def test_the_bus_branch_and_the_working_tree_are_untouched(self):
        before = git(self.work, "rev-parse", "HEAD").stdout.strip()
        self.bus.publish(pulse("Hermes", NOW))
        self.assertEqual(git(self.work, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(git(self.work, "status", "--porcelain").stdout.strip(), "")
        # ...and nothing landed on the bus branch, locally or upstream.
        self.assertEqual(git(self.work, "rev-parse", "origin/bus").stdout.strip(), before)

    def test_a_peers_pulse_is_read_from_its_own_ref(self):
        self.bus.publish(pulse("Hermes", NOW, rev="mine"))
        # Another bot publishes from its own checkout, and we read it from the ref it pushed.
        other = self.base / "other"
        subprocess.run(["git", "clone", "--quiet", str(self.base / "origin.git"), str(other)],
                       check=True, env=git_env())
        PulseBus(other, "Codex").publish(pulse("Codex", NOW + 30, rev="theirs", busy_since=NOW))
        pulses = self.bus.read()
        self.assertIn("Codex", pulses)
        self.assertEqual(pulses["Codex"]["rev"], "theirs")
        self.assertEqual(pulses["Codex"]["busy_since"], NOW)
        self.assertIn("Hermes", pulses)

    def test_a_restart_reads_the_same_facts_back(self):
        self.bus.publish(pulse("Hermes", NOW, rev="abc"))
        # Whatever the process's memory was, the ref is what a restart finds.
        fresh = PulseBus(self.work, "Hermes")
        self.assertEqual(fresh.read()["Hermes"]["rev"], "abc")

    def test_no_git_checkout_means_no_bus(self):
        plain = self.base / "plain"
        plain.mkdir()
        bus = PulseBus(plain, "Hermes")
        self.assertFalse(bus.available())
        self.assertFalse(bus.publish(pulse("Hermes", NOW)))      # Telegram carries it instead
        self.assertEqual(bus.read(), {})

    def test_a_push_that_fails_falls_back_instead_of_claiming_success(self):
        bus = PulseBus(self.work, "Hermes", remote="nowhere")
        self.assertFalse(bus.publish(pulse("Hermes", NOW)))      # no such remote
        # The local ref still holds the pulse, so a later beat is not the first one ever.
        self.assertEqual(bus.aliases(), ["Hermes"])

    def test_an_unreadable_pulse_on_a_ref_is_skipped_not_fatal(self):
        self.bus.publish(pulse("Hermes", NOW, rev="mine"))
        git(self.work, "update-ref", "refs/pulse/Broken", "HEAD")   # a ref with no pulse.json
        self.assertEqual(sorted(self.bus.read()), ["Hermes"])


@unittest.skipIf(GIT is None, "git is not installed")
class TestFeedingTheTable(unittest.TestCase):

    def test_read_pulses_are_stamped_with_our_clock_and_counted(self):
        table = pr.Presence(NOW, down_after=360, grace=120)
        pulses = {"Codex": pulse("Codex", NOW + 3600, rev="ahead").to_dict()}
        self.assertEqual(notes_from(pulses, table, now=NOW, skip=["Hermes"]), 1)
        self.assertEqual(table.status("Codex", NOW), "up")
        # A stamp an hour ahead does not keep the peer up: we saw it now, not then.
        self.assertTrue(table.is_down("Codex", NOW + 400))

    def test_our_own_alias_is_not_a_peer(self):
        table = pr.Presence(NOW, down_after=360, grace=120)
        pulses = {"Hermes": pulse("Hermes", NOW).to_dict()}
        self.assertEqual(notes_from(pulses, table, now=NOW, skip=["hermes"]), 0)
        self.assertEqual(table.peers, {})

    def test_a_malformed_record_is_skipped(self):
        table = pr.Presence(NOW, down_after=360, grace=120)
        self.assertEqual(notes_from({"Codex": {"nope": 1}}, table, now=NOW), 0)
