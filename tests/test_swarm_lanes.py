import json
import shutil
import subprocess
import unittest

from tests.gitenv import git_env
from tests.swarm_helpers import SwarmCase, needs_swarm

HAVE_GIT = shutil.which("git") is not None


@needs_swarm
class EnvelopeTests(SwarmCase):
    def test_signature_covers_the_whole_envelope(self):
        env = self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        key = self.swarm.public_key("codex")
        self.assertTrue(env.verify(key))
        env.body["title"] = "changed"
        self.assertFalse(env.verify(key))
        env.body["title"] = "x"
        env.to = ["claude"]
        self.assertFalse(env.verify(key))

    def test_round_trip_and_strict_parsing(self):
        from swarmly.swarm.envelope import Envelope, EnvelopeError
        env = self.post("codex", "NOTE", body={"text": "hi"})
        again = Envelope.from_json(env.to_json())
        self.assertEqual(again.to_dict(), env.to_dict())
        data = env.to_dict()
        for mutate in (lambda d: d.pop("sig"),
                       lambda d: d.update(extra=1),
                       lambda d: d.update(protocol="a2a/v1.0"),
                       lambda d: d.update(sig={"alg": "hmac", "value": "x"}),
                       lambda d: d.update(lane="claude")):
            bad = json.loads(json.dumps(data))
            mutate(bad)
            with self.assertRaises(EnvelopeError):
                Envelope.from_dict(bad)

    def test_lane_is_hash_chained(self):
        a = self.post("codex", "NOTE", body={"text": "1"})
        b = self.post("codex", "NOTE", body={"text": "2"})
        self.assertEqual((a.seq, b.seq), (1, 2))
        self.assertEqual(b.prev, a.digest())

    def test_store_refuses_a_duplicate_seq(self):
        from swarmly.swarm.lanes import LaneConflict
        a = self.post("codex", "NOTE", body={"text": "1"})
        with self.assertRaises(LaneConflict):
            self.store.append(a)  # same seq again

    def test_two_writers_drafting_the_same_seq_cannot_both_land(self):
        from swarmly.swarm.governor import Governor
        from swarmly.swarm.lanes import LaneConflict
        self.post("codex", "NOTE", body={"text": "0"})
        cli = Governor(self.swarm, self.store, "codex")
        mcp = Governor(self.swarm, self.store, "codex")
        view = cli.view()
        a = cli.draft(view, "NOTE", body={"text": "a"})
        b = mcp.draft(view, "HEARTBEAT")  # same seq, different intent and file name
        self.assertEqual(a.seq, b.seq)
        a.sign_with(cli.private_key())
        b.sign_with(mcp.private_key())
        self.store.append(a)
        with self.assertRaises(LaneConflict):
            self.store.append(b)
        self.assertEqual(self.view().violations, [])

    def test_unreadable_file_does_not_stop_readers(self):
        self.post("codex", "NOTE", body={"text": "1"})
        self.post("antigravity", "NOTE", body={"text": "ok"})
        junk = self.root / "lanes" / "codex" / "2026" / "01" / "01" / "00000002-NOTE.json"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("{not json")
        proj = self.view()
        self.assertEqual({e.id for e in proj.log}, {"codex:1", "antigravity:1"})
        self.assertTrue(any("invalid" in " ".join(v.errors) for v in proj.violations))

    def test_garbage_on_top_of_own_lane_is_refused(self):
        from swarmly.swarm.lanes import LaneConflict
        self.post("codex", "NOTE", body={"text": "1"})
        files = sorted((self.root / "lanes" / "codex").rglob("*.json"))
        data = json.loads(files[0].read_text())
        data.update(seq=2, id="codex:2")
        files[0].with_name("00000002-NOTE.json").write_text(json.dumps(data))
        with self.assertRaises(LaneConflict) as ctx:
            self.post("codex", "NOTE", body={"text": "2"})
        self.assertIn("swarmly verify", str(ctx.exception))

    def test_ts_needs_six_fraction_digits(self):
        from swarmly.swarm.envelope import Envelope, EnvelopeError
        data = self.post("codex", "NOTE", body={"text": "x"}).to_dict()
        data["ts"] = "2026-10-07T20:00:00.5Z"
        with self.assertRaises(EnvelopeError):
            Envelope.from_dict(data)

    def test_tampered_file_quarantines_rest_of_lane(self):
        self.post("codex", "NOTE", body={"text": "1"})
        self.post("codex", "NOTE", body={"text": "2"})
        self.post("antigravity", "NOTE", body={"text": "ok"})
        files = sorted((self.root / "lanes" / "codex").rglob("*.json"))
        data = json.loads(files[0].read_text())
        data["body"]["text"] = "forged"
        files[0].write_text(json.dumps(data))
        proj = self.view()
        bad = {v.id for v in proj.violations}
        self.assertEqual(bad, {"codex:1", "codex:2"})
        self.assertEqual([e.id for e in proj.log], ["antigravity:1"])

    def test_lane_outside_roster_is_quarantined(self):
        from swarmly.swarm.lanes import DirLaneStore
        env = self.post("codex", "NOTE", body={"text": "1"})
        src = next((self.root / "lanes" / "codex").rglob("*.json"))
        dest = self.root / "lanes" / "mallory" / src.relative_to(self.root / "lanes" / "codex")
        dest.parent.mkdir(parents=True)
        dest.write_text(env.to_json())
        self.assertIn("mallory", DirLaneStore(self.root / "lanes").lanes())
        proj = self.view()
        self.assertTrue(any(v.lane == "mallory" for v in proj.violations))


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=git_env()).stdout.strip()


@needs_swarm
@unittest.skipUnless(HAVE_GIT, "needs git")
class GitLaneStoreTests(SwarmCase):
    def setUp(self):
        super().setUp()
        from swarmly.swarm.lanes import GitLaneStore
        self.remote = self.root / "remote.git"
        _git("init", "--bare", "--quiet", str(self.remote))
        # A code repo with a main branch, to show lanes never touch it.
        work = self.root / "work"
        _git("init", "--quiet", "-b", "main", str(work))
        (work / "README").write_text("x")
        _git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=work)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=work)
        _git("push", "-q", str(self.remote), "main", cwd=work)
        self.main_sha = _git("rev-parse", "main", cwd=work)
        self.bus_a = GitLaneStore.init(self.root / "bus-a", str(self.remote))
        self.bus_b = GitLaneStore.init(self.root / "bus-b", str(self.remote))

    def gov_on(self, agent, store):
        from swarmly.swarm.governor import Governor
        return Governor(self.swarm, store, agent)

    def test_two_hosts_share_lanes_through_the_remote(self):
        codex = self.gov_on("codex", self.bus_a)
        claude = self.gov_on("claude", self.bus_b)
        codex.post("PROPOSE", task_id="t1", body={"title": "fix it"})
        claude.post("OFFER", task_id="t1")
        codex.post("CLAIM", task_id="t1")
        proj = claude.view()
        self.assertEqual(proj.tasks["t1"].state, "claimed")
        self.assertEqual(proj.tasks["t1"].claimant, "codex")
        self.assertEqual(proj.violations, [])
        refs = _git("for-each-ref", "--format=%(refname)", cwd=self.remote).splitlines()
        self.assertIn("refs/heads/a2a/lane/codex", refs)
        self.assertIn("refs/heads/a2a/lane/claude", refs)
        self.assertEqual(_git("rev-parse", "main", cwd=self.remote), self.main_sha)
        tree = _git("ls-tree", "-r", "--name-only", "refs/heads/a2a/lane/codex",
                    cwd=self.remote).splitlines()
        self.assertEqual(len(tree), 2)
        self.assertTrue(all(not p.startswith(".agents") for p in tree))
        self.assertRegex(tree[0], r"^\d{4}/\d{2}/\d{2}/00000001-PROPOSE\.json$")
        author = _git("log", "-1", "--format=%an <%ae>", "refs/heads/a2a/lane/codex",
                      cwd=self.remote)
        self.assertEqual(author, "codex <codex@a2a.local>")

    def test_same_repo_race_is_a_compare_and_swap(self):
        from swarmly.swarm.governor import Governor
        from swarmly.swarm.lanes import GitLaneStore, LaneConflict
        local = GitLaneStore.init(self.root / "solo")  # no remote
        g1 = Governor(self.swarm, local, "codex")
        g2 = Governor(self.swarm, local, "codex")
        view = g1.view()
        a = g1.draft(view, "NOTE", body={"text": "a"})
        b = g2.draft(view, "NOTE", body={"text": "b"})
        a.sign_with(g1.private_key())
        b.sign_with(g2.private_key())
        local.append(a)
        with self.assertRaises(LaneConflict):
            local.append(b)
        self.assertEqual([e.body["text"] for e in local.read("codex")], ["a"])

    def test_forked_lane_is_reported_not_fatal(self):
        codex_a = self.gov_on("codex", self.bus_a)
        codex_a.post("NOTE", body={"text": "a"})
        self.gov_on("hermes", self.bus_a).post("NOTE", body={"text": "h"})
        # Host b grows a private codex lane, then sees a different one on the remote.
        repo = self.bus_b.repo
        empty = _git("hash-object", "-t", "tree", "-w", "/dev/null", cwd=repo)
        fork = _git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "-m", "fork",
                    empty, cwd=repo)
        _git("update-ref", "refs/heads/a2a/lane/codex", fork, cwd=repo)
        proj = self.gov_on("claude", self.bus_b).view()
        self.assertTrue(any("forked" in " ".join(v.errors) for v in proj.violations))
        self.assertIn("hermes:1", {e.id for e in proj.log})

    def test_lane_commits_are_fast_forward_only(self):
        from swarmly.swarm.lanes import LaneConflict
        codex_a = self.gov_on("codex", self.bus_a)
        codex_b = self.gov_on("codex", self.bus_b)
        codex_a.post("NOTE", body={"text": "from a"})
        # Host b has not synced; forging a seq-1 envelope there must not overwrite a.
        env = codex_b.draft(codex_b.view(sync=False), "NOTE", body={"text": "from b"})
        env.sign_with(codex_b.private_key())
        with self.assertRaises(LaneConflict):
            self.bus_b.append(env)
        # The governor retries on a fresh view and lands at seq 2.
        out = codex_b.post("NOTE", body={"text": "from b"})
        self.assertEqual(out.seq, 2)
        texts = [e.body["text"] for e in self.gov_on("claude", self.bus_a).view().log]
        self.assertEqual(texts, ["from a", "from b"])


if __name__ == "__main__":
    unittest.main()
