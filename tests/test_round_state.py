"""Durable round state on the bus (issue #86).

The properties these tests pin are the ones the design review asked for: writes are
append-only (a lost push costs a segment, not the state), a segment is guarded by term and
seq, a verdict is cleared when the head moves, a state that cannot be read is never
"nothing is open", and the round's files ride the bus push path like any other.
"""

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.gitenv import git_env
from swarmly.telegram import rounds as rs
from swarmly.transports.base import is_bus_document
from swarmly.transports.git import GitTransport

HEAD_A = "a" * 40
HEAD_B = "b" * 40


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env=git_env()).stdout.strip()


class TestBlock(unittest.TestCase):
    """The ``ROUND_STATE`` block in a reply: stripped always, applied only when usable."""

    def test_a_block_is_removed_from_the_text(self):
        text = ('@Codex: #86 PR is up.\n'
                'ROUND_STATE {"version": 1, "round": 86, "items": {"86": {"owner": "DeepSeek"}}}\n'
                'Ask @agy_dd_bot for QA.')
        stripped, payload, reason = rs.extract_block(text)
        self.assertIsNone(reason)
        self.assertEqual(payload["round"], 86)
        self.assertNotIn("ROUND_STATE", stripped)
        self.assertIn("@Codex: #86 PR is up.", stripped)
        self.assertIn("Ask @agy_dd_bot for QA.", stripped)

    def test_a_reply_without_a_block_is_untouched(self):
        text = "just a reply"
        self.assertEqual(rs.extract_block(text), (text, None, None))

    def test_a_truncated_block_is_stripped_and_refused(self):
        stripped, payload, reason = rs.extract_block('done\nROUND_STATE {"round": 86, "items": {')
        self.assertIsNone(payload)
        self.assertIn("closed", reason)
        self.assertEqual(stripped, "done")

    def test_invalid_json_is_refused(self):
        stripped, payload, reason = rs.extract_block('x\nROUND_STATE {"round": }')
        self.assertIsNone(payload)
        self.assertIn("JSON", reason)
        self.assertEqual(stripped, "x")

    def test_an_oversized_block_is_refused_not_truncated(self):
        body = json.dumps({"round": 86, "note": "n" * (rs.MAX_BLOCK_CHARS + 10)})
        stripped, payload, reason = rs.extract_block("x\nROUND_STATE " + body)
        self.assertIsNone(payload)
        self.assertIn("too large", reason)
        self.assertEqual(stripped, "x")

    def test_braces_inside_a_note_do_not_end_the_object(self):
        _, payload, reason = rs.extract_block('x\nROUND_STATE {"round": 86, "note": "a } b"}')
        self.assertIsNone(reason)
        self.assertEqual(payload["note"], "a } b")


class TestSegment(unittest.TestCase):

    def test_unknown_fields_are_ignored_and_wrong_types_refused(self):
        update = rs.RoundUpdate.from_dict({"round": 86, "future_field": 1})
        self.assertEqual(update.round, "86")
        with self.assertRaises(rs.RoundStateError):
            rs.RoundUpdate.from_dict({"round": 86, "items": []})
        with self.assertRaises(rs.RoundStateError):
            rs.RoundUpdate.from_dict({"items": {}})
        with self.assertRaises(rs.RoundStateError):
            rs.RoundUpdate.from_dict({"round": 86, "version": 99})

    def test_a_verdict_sha_must_identify_a_head(self):
        verdict = rs.Verdict.from_dict({"verdict": "merge", "sha": HEAD_A[:7]}, head=HEAD_A)
        self.assertEqual(verdict.sha, HEAD_A)
        with self.assertRaises(rs.RoundStateError):
            rs.Verdict.from_dict({"verdict": "merge", "sha": HEAD_B[:7]}, head=HEAD_A)
        with self.assertRaises(rs.RoundStateError):
            rs.Verdict.from_dict({"verdict": "merge", "sha": "zzz"})


class TestFold(unittest.TestCase):

    def update(self, **kwargs):
        base = {"round": "repo#86", "writer": "Claude", "at": rs.utc_now(lambda: 1000.0)}
        base.update(kwargs)
        return rs.RoundUpdate(**base)

    def test_a_later_term_wins(self):
        state = rs.fold([
            self.update(term=1, seq=9, items={"86": {"owner": "DeepSeek"}}),
            self.update(term=2, seq=1, items={"86": {"owner": "Antigravity"}}),
        ], round_key="repo#86")
        self.assertEqual(state.term, 2)
        self.assertEqual(state.item("86")["owner"], "Antigravity")

    def test_a_segment_patch_keeps_fields_it_does_not_mention(self):
        state = rs.fold([
            self.update(term=1, seq=1, items={"86": {"owner": "DeepSeek", "pr": 90}}),
            self.update(term=1, seq=2, items={"86": {"owner": "Antigravity"}}),
        ], round_key="repo#86")
        self.assertEqual(state.item("86"), {"owner": "Antigravity", "pr": 90})

    def test_the_same_seq_from_two_writers_folds_the_same_way_everywhere(self):
        one = self.update(term=1, seq=3, writer="Claude", items={"86": {"owner": "Claude"}})
        two = self.update(term=1, seq=3, writer="Antigravity", items={"86": {"owner": "Antigravity"}})
        first = rs.fold([one, two], round_key="repo#86")
        second = rs.fold([two, one], round_key="repo#86")
        self.assertEqual(first.item("86"), second.item("86"))
        self.assertEqual(first.writer, second.writer)


class TestState(unittest.TestCase):

    def state(self, **kwargs):
        base = {"round": "repo#86", "status": "ok", "updated_at": "2026-01-01T00:00:00Z"}
        base.update(kwargs)
        return rs.RoundState(**base)

    def test_a_new_head_clears_the_verdict(self):
        item = {"review": {"verdict": "merge", "sha": HEAD_A, "by": "Codex"}}
        state = self.state(items={"86": item})
        self.assertIsNotNone(state.verdict("86", "review", HEAD_A))
        self.assertIsNone(state.verdict("86", "review", HEAD_B))

    def test_absent_and_unreadable_are_not_empty(self):
        absent = rs.RoundState(round="repo#86", status="absent")
        self.assertIn("absent", absent.summary())
        self.assertIn("not usable", absent.summary())
        self.assertFalse(absent.usable)
        unreadable = rs.RoundState(round="repo#86", status="unreadable", reason="no ref")
        self.assertIn("no ref", unreadable.summary())

    def test_a_damaged_segment_marks_the_state_stale(self):
        state = self.state(items={"86": {"owner": "DeepSeek"}}, stale="segment not readable: x")
        self.assertFalse(state.usable)
        self.assertIn("STALE", state.summary())


class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = rs.RoundStore(None, root=self.root, alias="Claude",
                                   authority=rs.Authority(alias="Claude"))

    def test_writes_are_append_only(self):
        first = self.store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                                 items={"86": {"owner": "A"}}))
        second = self.store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                                  items={"86": {"owner": "B"}}))
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(Path(first).parent.glob("*.json"))), 2)
        state = self.store.load("repo#86")
        self.assertEqual(state.item("86")["owner"], "B")
        self.assertEqual(state.segments, 2)

    def test_an_older_term_is_refused(self):
        self.store.append(rs.RoundUpdate(round="repo#86", writer="A", term=2))
        with self.assertRaises(rs.RoundStaleTerm):
            self.store.append(rs.RoundUpdate(round="repo#86", writer="B", term=1))
        self.assertEqual(self.store.load("repo#86").term, 2)

    def test_a_round_with_no_segments_is_absent_not_empty(self):
        state = self.store.load("repo#88")
        self.assertEqual(state.status, "absent")
        self.assertEqual(state.items, {})
        self.assertEqual(self.store.rounds(), [])

    def test_a_segment_outside_the_rounds_directory_is_refused(self):
        store = rs.RoundStore(None, root=self.root, relpath="../elsewhere")
        with self.assertRaises(rs.RoundStateError):
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude"))


class TestBusDocumentPath(unittest.TestCase):

    def test_only_rounds_documents_are_documents(self):
        self.assertTrue(is_bus_document(".agents/rounds/repo#86/000001-000001-a-b.json"))
        self.assertFalse(is_bus_document(".agents/inbox/msg.md"))
        self.assertFalse(is_bus_document(".agents/rounds/../x/y.json"))
        self.assertFalse(is_bus_document(".agents/rounds/a/b/c.json"))
        self.assertFalse(is_bus_document("/etc/passwd.json"))

    def test_a_rounds_file_kept_local_by_a_lost_push_is_carried_forward(self):
        """The regression #86 review named: only envelopes were carried onto a moved tip."""
        base = Path(self.tmp_dir())
        remote = base / "remote.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(remote))
        one = base / "one"
        two = base / "two"
        for path in (one, two):
            git(base, "clone", "-q", str(remote), str(path))
            git(path, "checkout", "-q", "-B", "main")
        (one / "README").write_text("x\n", encoding="utf-8")
        git(one, "add", "README")
        git(one, "commit", "-q", "-m", "init")
        git(one, "push", "-q", "-u", "origin", "main")
        git(two, "pull", "-q", "origin", "main")

        # one: a round segment committed locally, whose push never happened.
        early = one / ".agents" / "rounds" / "repo#86" / "000000-000001-claude-aaaa.json"
        early.parent.mkdir(parents=True, exist_ok=True)
        early.write_text('{"round": "repo#86", "term": 0, "seq": 1}', encoding="utf-8")
        git(one, "add", str(early.relative_to(one)))
        git(one, "commit", "-q", "-m", "unpushed round segment")

        # two: moves the remote on, so one's next push loses the race and is retried.
        (two / "other.txt").write_text("y\n", encoding="utf-8")
        git(two, "add", "other.txt")
        git(two, "commit", "-q", "-m", "other")
        git(two, "push", "-q", "origin", "main")

        late = one / ".agents" / "rounds" / "repo#86" / "000000-000002-claude-bbbb.json"
        late.write_text('{"round": "repo#86", "term": 0, "seq": 2}', encoding="utf-8")
        # The bus commit is made as the agent's identity; a checkout with none cannot commit
        # (on CI the process environment is scrubbed, so this must be passed explicitly).
        transport = GitTransport(one, ref="origin/main",
                                 identity=("Swarmly Tests", "tests@swarmly.invalid"))
        self.assertTrue(transport.commit_and_push(
            "rounds: repo#86 seq 2",
            paths=[str(late.relative_to(one)).replace("\\", "/")]))

        listed = git(one, "ls-tree", "-r", "--name-only", "origin/main", ".agents/rounds/")
        self.assertIn(".agents/rounds/repo#86/000000-000001-claude-aaaa.json", listed)
        self.assertIn(".agents/rounds/repo#86/000000-000002-claude-bbbb.json", listed)
        both = json.loads(git(one, "show", "origin/main:" +
                             ".agents/rounds/repo#86/000000-000001-claude-aaaa.json"))
        self.assertEqual(both["seq"], 1)

    def tmp_dir(self):
        if not hasattr(self, "_tmp"):
            self._tmp = TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
            saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("GIT_")}
            self.addCleanup(os.environ.update, saved)
        return self._tmp.name


if __name__ == "__main__":
    unittest.main()


class TestChannelWiring(unittest.TestCase):
    """The block is stripped from the posted reply, and only a coordinator writes it."""

    def channel(self, role: str):
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # A real bus checkout: the store reads a ref (``main``) and pushes, so a write that
        # cannot be pushed stays visible as local state rather than vanishing.
        git(root, "init", "-q", "-b", "main")
        (root / "README").write_text("bus\n", encoding="utf-8")
        git(root, "add", "README")
        git(root, "commit", "-q", "-m", "bus")
        config = make_config(agent={"role": role},
                             fallback={"gitbus_root": str(root), "gitbus_ref": "main"})
        channel = make_channel(config)
        # #83 moved the authority for a round write to the lease on the bus. These tests are
        # about the block and its write, not about who holds the lease (that is
        # tests/test_lease.py and the channel lease tests), so the authority is supplied here.
        from swarmly.telegram.rounds import Authority
        channel.round_authority = lambda now=None: Authority(
            alias=channel.config.identity.alias if role == "coordinator" else "Claudebot",
            source="lease", term=1)
        channel.lease_fence = lambda now=None: role == "coordinator"
        return channel, root, event

    def test_a_coordinators_block_is_stripped_and_landed_on_the_bus(self):
        channel, root, event = self.channel("coordinator")
        text = ('#86 PR is up, @agy_dd_bot for QA.\n'
                'ROUND_STATE {"version": 1, "round": 88, "items": {"86": {"owner": "DeepSeek"}}}')
        body = channel.apply_round_block(text, event("hi", username="ClaudeBot"))
        self.assertNotIn("ROUND_STATE", body)
        self.assertIn("PR is up", body)
        segments = list((root / ".agents" / "rounds").rglob("*.json"))
        self.assertEqual(len(segments), 1)
        state = channel.round_store().load("88")
        self.assertEqual(state.status, "ok")
        self.assertEqual(state.item("86")["owner"], "DeepSeek")

    def test_a_peers_block_is_stripped_and_not_written(self):
        channel, root, event = self.channel("developer")
        body = channel.apply_round_block(
            'x\nROUND_STATE {"version": 1, "round": 88}', event("hi", username="ClaudeBot"))
        self.assertNotIn("ROUND_STATE", body)
        self.assertFalse(list((root / ".agents" / "rounds").rglob("*.json")) if
                         (root / ".agents" / "rounds").is_dir() else [])

    def test_slash_round_says_absent_rather_than_nothing_is_open(self):
        channel, root, event = self.channel("coordinator")
        reply = channel._round_summary(event("@HermesBot /round"))
        self.assertIn("absent, not empty", reply)

    def test_slash_round_prints_the_folded_state(self):
        channel, root, event = self.channel("coordinator")
        channel.apply_round_block(
            'x\nROUND_STATE {"version": 1, "round": 88, "items": {"86": {"waits_on": "ci"}}}',
            event("hi", username="ClaudeBot"))
        reply = channel._round_summary(event("@HermesBot /round"))
        self.assertIn("round 88", reply)
        self.assertIn("waiting=#86:ci", reply)


class TestOneVerdictPerHead(unittest.TestCase):
    """#185: a verdict is one fact on one head, and the transport keeps it once.

    Live, 22 Oct: every verdict was posted two or three times ("Already posted…"), and a bot
    repeated its own MERGE on the same head. The state already holds the fact -- a repeat is
    suppressed with one log line instead of another segment and another notice.
    """

    VERDICT = {"verdict": "merge", "sha": HEAD_A, "by": "Reasonix", "at": "2026-10-23T09:00:00Z"}

    def _store(self, tmp):
        return rs.RoundStore(None, root=Path(tmp), alias="Claude",
                             authority=rs.Authority(alias="Claude"))

    def _merge(self, store, verdict, *, item="202", round_key="repo#202", **extra):
        return store.append(rs.RoundUpdate(round=round_key, writer="Claude",
                                           items={item: {"review": dict(verdict)}}, **extra))

    def test_a_repeated_verdict_is_suppressed_with_one_log_line(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._merge(store, self.VERDICT)
            again = dict(self.VERDICT, at="2026-10-23T09:05:00Z")   # same fact, later stamp
            with self.assertLogs("swarmly.telegram.rounds", level="INFO") as captured:
                self._merge(store, again)
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 1,
                             "a repeated verdict does not add a segment to the bus")
            said = [r.getMessage() for r in captured.records
                    if "already recorded" in r.getMessage()]
            self.assertEqual(len(said), 1, captured.output)
            self.assertIn("Reasonix", said[0])
            self.assertIn("#202", said[0])
            self.assertIn(HEAD_A, said[0])
            held = store.load("repo#202").verdict("202", "review", head=HEAD_A)
            self.assertEqual(held.verdict, "merge", "the verdict the round holds is unchanged")

    def test_the_same_verdict_on_a_new_head_is_not_a_repeat(self):
        """A head that moved is new information: the verdict is on new code."""
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._merge(store, self.VERDICT)
            self._merge(store, dict(self.VERDICT, sha=HEAD_B))
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 2)
            state = store.load("repo#202")
            self.assertEqual(state.verdict("202", "review", head=HEAD_B).verdict, "merge")
            self.assertIsNone(state.verdict("202", "review", head=HEAD_A),
                              "the old head's verdict does not survive the move")

    def test_another_bots_verdict_is_its_own_fact(self):
        """One reviewer's word does not answer for another's: that is not a repeat."""
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._merge(store, self.VERDICT)
            self._merge(store, dict(self.VERDICT, by="Codex"))
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 2)
            self.assertEqual(store.load("repo#202").verdict("202", "review",
                                                            head=HEAD_A).by, "Codex")

    def test_a_repeat_plus_a_repo_change_is_still_written(self):
        """#185 review, nit 2: `repo` is a field like any other -- a repeat must not swallow it."""
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._merge(store, self.VERDICT)
            store.append(rs.RoundUpdate(round="repo#202", writer="Claude",
                                        repo="owner/project",
                                        items={"202": {"review": dict(self.VERDICT)}}))
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 2,
                             "the repo change is not a repeat, so it gets a segment")
            self.assertEqual(store.load("repo#202").repo, "owner/project")

    def test_a_suppressed_repeat_returns_a_segment_path(self):
        """#185 review, nit 1: `append` promises a segment path, so the no-write path must not
        answer with a bare directory -- the fallback is only for a round with no segments."""
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            first = self._merge(store, self.VERDICT)
            returned = self._merge(store, self.VERDICT)
            self.assertEqual(first, returned, "the repeat answers with the segment already there")
            self.assertTrue(returned.endswith(".json"), returned)
            self.assertTrue(Path(returned).is_file(), returned)
            # The documented fallback, and the only place a directory is the answer.
            self.assertFalse(store._last_segment_path("repo#never-written").endswith(".json"))

    def test_a_repeat_riding_along_a_real_change_does_not_restate_the_verdict(self):
        """The repeat is dropped from the segment; the change alongside it is still written."""
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._merge(store, self.VERDICT)
            store.append(rs.RoundUpdate(
                round="repo#202", writer="Claude",
                items={"202": {"review": dict(self.VERDICT), "ci": "green"}}))
            segments = list(Path(tmp).rglob("*.json"))
            self.assertEqual(len(segments), 2)
            written = json.loads(sorted(segments)[-1].read_text(encoding="utf-8"))
            self.assertEqual(written["items"]["202"].get("ci"), "green")
            self.assertNotIn("review", written["items"]["202"],
                             "the repeat is not re-written into the new segment")


class TestUnchanged(unittest.TestCase):
    """A turn that repeats the state must not add a commit to the bus's history."""

    def test_the_same_state_twice_writes_one_segment(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            update = {"round": "repo#86", "writer": "Claude", "items": {"86": {"owner": "A"}}}
            first = store.append(rs.RoundUpdate(**update))
            again = store.append(rs.RoundUpdate(**update))
            self.assertEqual(first, again)
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 1)

    def test_a_changed_field_is_written(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "A"}}))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "B"}}))
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 2)


class _RefTransport:
    """A bus transport whose ref can be read, or cannot -- for the read-semantics tests."""

    def __init__(self, files=None, readable=True):
        self.ref = "origin/main"
        self.files = dict(files or {})
        self.readable = readable

    def refresh(self):
        pass

    def list_at_ref(self, relpath):
        if not self.readable:
            return None
        prefix = relpath.rstrip("/") + "/"
        return sorted(path[len(prefix):] for path in self.files if path.startswith(prefix))

    def read_at_ref(self, relpath):
        return self.files.get(relpath)


class TestBusReadSemantics(unittest.TestCase):
    """Blocker 1: a bus that cannot be read is not an empty round, and local bytes are not
    presented as bus state."""

    def test_an_unreadable_ref_is_unreadable_not_absent(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(_RefTransport(readable=False), root=Path(tmp))
            state = store.load("repo#86")
            self.assertEqual(state.status, "unreadable")
            self.assertNotEqual(state.status, "absent")
            self.assertIn("not an empty round", state.reason)

    def test_an_empty_readable_ref_is_absent(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(_RefTransport(), root=Path(tmp))
            self.assertEqual(store.load("repo#86").status, "absent")

    def test_an_unreadable_ref_falls_back_to_the_checkout_and_says_so(self):
        with TemporaryDirectory() as tmp:
            local = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            local.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}))
            store = rs.RoundStore(_RefTransport(readable=False), root=Path(tmp))
            state = store.load("repo#86")
            self.assertEqual(state.status, "ok")
            self.assertEqual(state.source, "local")
            self.assertIn("checkout only", state.stale)
            self.assertFalse(state.usable)

    def test_a_segment_the_ref_does_not_have_yet_is_local_not_bus_state(self):
        with TemporaryDirectory() as tmp:
            local = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            local.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}))
            store = rs.RoundStore(_RefTransport(), root=Path(tmp))
            state = store.load("repo#86")
            self.assertEqual(state.source, "local")
            self.assertEqual(state.pending, 1)
            self.assertFalse(state.usable)

    def test_a_segment_on_the_ref_is_bus_state(self):
        payload = json.dumps({"version": 1, "round": "repo#86", "writer": "Claude", "seq": 1,
                              "items": {"86": {"owner": "DeepSeek"}}})
        files = {".agents/rounds/repo#86/000000-000001-claude-a.json": payload}
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(_RefTransport(files), root=Path(tmp))
            state = store.load("repo#86")
            self.assertEqual(state.source, "ref")
            self.assertTrue(state.usable)
            self.assertEqual(state.item("86")["owner"], "DeepSeek")


class TestWriteFencing(unittest.TestCase):
    """Blockers 2-4: concurrent patches, an unverifiable term, and the holder."""

    def write_segment(self, store, name, update):
        path = store.directory(update.round) / name
        path.write_text(json.dumps(update.to_dict()), encoding="utf-8")

    def test_two_concurrent_disjoint_patches_both_survive(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}))
            base = store.load("repo#86").seq
            for writer, patch in (("Claude", {"pr": 89}), ("Antigravity", {"waits_on": "ci"})):
                self.write_segment(store, f"000000-{base + 1:06d}-{writer[-4:]}.json",
                                   rs.RoundUpdate(round="repo#86", writer=writer,
                                                  seq=base + 1, base_seq=base,
                                                  items={"86": patch}))
            state = store.load("repo#86")
            self.assertEqual(state.item("86")["pr"], 89)
            self.assertEqual(state.item("86")["waits_on"], "ci")
            self.assertEqual(state.item("86")["owner"], "DeepSeek")
            self.assertEqual(state.conflicts, [])

    def test_two_concurrent_writes_to_one_field_are_reported_not_silently_resolved(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude"))
            base = store.load("repo#86").seq
            for writer, owner in (("Claude", "DeepSeek"), ("Antigravity", "Reasonix")):
                self.write_segment(store, f"000000-{base + 1:06d}-{writer}.json",
                                   rs.RoundUpdate(round="repo#86", writer=writer,
                                                  seq=base + 1, base_seq=base,
                                                  items={"86": {"owner": owner}}))
            state = store.load("repo#86")
            self.assertEqual(state.conflicts, ["86.owner"])
            self.assertIn("same field", state.stale)
            self.assertFalse(state.usable)

    def test_an_unreadable_bus_refuses_the_write(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(_RefTransport(readable=False), root=Path(tmp))
            with self.assertRaises(rs.RoundStateError):
                store.append(rs.RoundUpdate(round="repo#86", writer="Claude"))
            self.assertFalse(list(Path(tmp).rglob("*.json")))

    def test_a_second_bot_cannot_write_a_held_round_at_the_same_term(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}), holder="Claude")
            with self.assertRaises(rs.RoundFenced):
                store.append(rs.RoundUpdate(round="repo#86", writer="Antigravity",
                                            items={"86": {"owner": "Reasonix"}}),
                             holder="Antigravity")
            self.assertEqual(store.load("repo#86").holder, "Claude")

    def test_a_later_term_may_take_the_round_over(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Antigravity", authority=rs.Authority(alias="Antigravity"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude", term=1),
                         holder="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Antigravity", term=2),
                         holder="Antigravity")
            state = store.load("repo#86")
            self.assertEqual(state.term, 2)
            self.assertEqual(state.holder, "Antigravity")

    def test_a_term_from_the_block_is_not_trusted(self):
        update = rs.RoundUpdate.from_dict({"round": 86, "term": 7, "seq": 9})
        self.assertEqual(update.term, 0)
        self.assertEqual(update.seq, 0)


class TestItemState(unittest.TestCase):
    """Reasonix B1 / Codex B1 / QA: a merged item must leave open_keys()."""

    def test_state_survives_the_parse_and_the_fold(self):
        update = rs.RoundUpdate.from_dict(
            {"round": 86, "items": {"86": {"state": "merged", "owner": "DeepSeek"}}})
        self.assertEqual(update.items["86"]["state"], "merged")
        state = rs.fold([update], round_key="repo#86")
        self.assertEqual(state.item("86")["state"], "merged")
        self.assertEqual(state.open_keys(), [])

    def test_an_open_item_stays_open(self):
        update = rs.RoundUpdate.from_dict(
            {"round": 86, "items": {"86": {"state": "open"}, "81": {"state": "merged"}}})
        state = rs.fold([update], round_key="repo#86")
        self.assertEqual(state.open_keys(), ["86"])

    def test_a_wrongly_typed_state_is_refused(self):
        with self.assertRaises(rs.RoundStateError):
            rs.RoundUpdate.from_dict({"round": 86, "items": {"86": {"state": 3}}})


class TestDamagedSegments(unittest.TestCase):
    """Reasonix B2 / Codex B2 / QA: a round with no readable segment is not `ok`."""

    def test_all_damaged_segments_are_unreadable(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / ".agents" / "rounds" / "repo#86"
            folder.mkdir(parents=True)
            (folder / "000000-000001-claude-a.json").write_text("{not json", encoding="utf-8")
            (folder / "000000-000002-claude-b.json").write_text("[]", encoding="utf-8")
            store = rs.RoundStore(None, root=Path(tmp), authority=rs.Authority(alias="Claude"))
            state = store.load("repo#86")
            self.assertEqual(state.status, "unreadable")
            self.assertIn("could not be read", state.reason)
            self.assertFalse(state.usable)

    def test_one_damaged_segment_among_good_ones_only_marks_the_state_stale(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}))
            folder = store.directory("repo#86")
            (folder / "000000-000002-claude-b.json").write_text("{", encoding="utf-8")
            state = store.load("repo#86")
            self.assertEqual(state.status, "ok")
            self.assertTrue(state.stale)                 # the loss is visible
            self.assertFalse(state.usable)


class TestBlockBudget(unittest.TestCase):
    """Reasonix B3 / Codex B3: a real six-item round must fit."""

    def six_item_block(self, verdict=True):
        items = {}
        for number in (86, 81, 83, 82, 84, 85):
            item = {"issue": number, "pr": 90 + number % 5, "owner": "DeepSeek",
                    "state": "open", "waits_on": "ci",
                    "head": "a" * 40}
            if verdict:
                item["review"] = {"verdict": "merge", "sha": "a" * 40, "by": "Codex",
                                  "at": "2026-10-20T00:00:00Z"}
                item["qa"] = {"verdict": "pass", "sha": "a" * 40, "by": "Antigravity",
                              "at": "2026-10-20T00:00:00Z"}
            items[str(number)] = item
        return json.dumps({"version": 1, "round": 88, "repo": "owner/project", "term": 3,
                           "items": items, "order": ["86", "81", "83", "82", "84", "85"],
                           "blockers": ["#86 waits on CI"],
                           "timers": {"wake-1": {"owner": "Claude", "until": "2026-10-21"}}})

    def test_a_realistic_six_item_round_fits_the_default_cap(self):
        block = self.six_item_block()
        self.assertGreater(len(block), 1200)          # the old cap refused exactly this
        self.assertLess(len(block), rs.MAX_BLOCK_CHARS)
        stripped, payload, reason = rs.extract_block("x\nROUND_STATE " + block)
        self.assertIsNone(reason, reason)
        self.assertEqual(len(payload["items"]), 6)
        self.assertNotIn("ROUND_STATE", stripped)

    def test_the_cap_is_a_setting(self):
        block = self.six_item_block()
        _, payload, reason = rs.extract_block("x\nROUND_STATE " + block,
                                              max_chars=len(block) - 1)
        self.assertIsNone(payload)
        self.assertIn("too large", reason)


class TestSecondBlock(unittest.TestCase):
    """Reasonix B4 / Codex B4: no block may reach the channel, ever."""

    def test_two_blocks_are_both_stripped_and_the_state_is_refused(self):
        text = ('done\nROUND_STATE {"round": 86, "items": {}}\n'
                'again\nROUND_STATE {"round": 86, "items": {"81": {"owner": "X"}}}')
        stripped, payload, reason = rs.extract_block(text)
        self.assertEqual(stripped, "done\nagain")
        self.assertIsNone(payload)
        self.assertIn("2", reason)

    def test_a_second_block_never_reaches_the_channel(self):
        from tests.test_telegram_channel import event as event_of
        from tests.test_telegram_channel import make_channel, make_config
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q", "-b", "main")
            (root / "README").write_text("bus\n", encoding="utf-8")
            git(root, "add", "README")
            git(root, "commit", "-q", "-m", "bus")
            config = make_config(agent={"role": "coordinator"},
                                 fallback={"gitbus_root": str(root), "gitbus_ref": "main"})
            channel = make_channel(config)
            body = channel.apply_round_block(
                'x\nROUND_STATE {"round": 88, "items": {}}\n'
                'y\nROUND_STATE {"round": 88, "items": {}}',
                event_of("hi", username="ClaudeBot"))
            self.assertNotIn("ROUND_STATE", body)
            self.assertIn("round state:", body)          # the refusal is visible
            self.assertFalse(list(root.rglob("*.json")))


class TestLocalAndRefUnion(unittest.TestCase):
    """Reasonix S8: a segment whose push failed must not vanish from its writer's view."""

    def test_a_local_segment_the_ref_lacks_is_unioned_and_counted(self):
        good = json.dumps({"version": 1, "round": "repo#86", "writer": "Claude", "seq": 1,
                           "items": {"86": {"owner": "DeepSeek"}}})
        files = {".agents/rounds/repo#86/000000-000001-claude-a.json": good}
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(_RefTransport(files), root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            folder = store.directory("repo#86")
            folder.mkdir(parents=True)
            (folder / "000000-000002-claude-b.json").write_text(
                json.dumps({"version": 1, "round": "repo#86", "writer": "Claude", "seq": 2,
                            "base_seq": 1, "items": {"86": {"pr": 89}}}), encoding="utf-8")
            state = store.load("repo#86")
            self.assertEqual(state.source, "ref")
            self.assertEqual(state.pending, 1)
            self.assertEqual(state.item("86")["pr"], 89)
            self.assertEqual(state.item("86")["owner"], "DeepSeek")
            self.assertFalse(state.usable)               # not durable until it is pushed

    def test_a_write_is_refused_against_a_checkout_only_state(self):
        with TemporaryDirectory() as tmp:
            local = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            local.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}))
            store = rs.RoundStore(_RefTransport(readable=False), root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            with self.assertRaises(rs.RoundStateError):
                store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                            items={"86": {"pr": 89}}),
                             holder="Claude")


class TestClaimRace(unittest.TestCase):
    """Codex B6: two coordinators, an absent round, both append term 0."""

    def test_two_simultaneous_first_writes_leave_one_deterministic_holder(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            folder = store.directory("repo#86")
            folder.mkdir(parents=True)
            for writer in ("Claude", "Antigravity"):
                update = rs.RoundUpdate(round="repo#86", writer=writer, holder=writer,
                                        seq=1, base_seq=0, items={"86": {"owner": writer}})
                (folder / f"000000-000001-{writer}.json").write_text(
                    json.dumps(update.to_dict()), encoding="utf-8")
            first = store.load("repo#86")
            second = store.load("repo#86")
            self.assertEqual(first.holder, second.holder)
            self.assertEqual(first.holder, "Antigravity")     # lowest alias wins the claim
            self.assertIn("holder", first.conflicts)
            self.assertFalse(first.usable)

    def test_a_no_op_from_another_bot_returns_instead_of_raising(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}), holder="Claude")
            # Same content from another bot: nothing to write, and no fence error.
            path = store.append(rs.RoundUpdate(round="repo#86", writer="Antigravity",
                                               items={"86": {"owner": "DeepSeek"}}),
                                holder="Antigravity")
            self.assertTrue(path.endswith(".json"))
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 1)


class TestConflictRecovery(unittest.TestCase):
    """Codex blocker 1: a conflict is a report, not a permanent mark on the round."""

    def write_segment(self, store, name, update):
        path = store.directory(update.round) / name
        path.write_text(json.dumps(update.to_dict()), encoding="utf-8")

    def test_a_conflict_is_settled_by_a_later_write(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude"), holder="Claude")
            base = store.load("repo#86").seq
            for writer, owner in (("Claude", "DeepSeek"), ("Claude2", "Reasonix")):
                self.write_segment(store, f"000000-{base + 1:06d}-{writer}.json",
                                   rs.RoundUpdate(round="repo#86", writer=writer,
                                                  seq=base + 1, base_seq=base,
                                                  items={"86": {"owner": owner}}))
            contested = store.load("repo#86")
            self.assertIn("86.owner", contested.conflicts)
            self.assertFalse(contested.usable)

            # The holder writes the field again, having seen both values: settled.
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "DeepSeek"}}), holder="Claude")
            settled = store.load("repo#86")
            self.assertEqual(settled.conflicts, [])
            self.assertTrue(settled.usable)
            self.assertEqual(settled.item("86")["owner"], "DeepSeek")

    def test_a_claim_race_is_settled_by_the_holders_next_write(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Antigravity", authority=rs.Authority(alias="Antigravity"))
            folder = store.directory("repo#86")
            folder.mkdir(parents=True)
            for writer in ("Claude", "Antigravity"):
                update = rs.RoundUpdate(round="repo#86", writer=writer, holder=writer,
                                        seq=1, base_seq=0, items={"86": {"owner": writer}})
                (folder / f"000000-000001-{writer}.json").write_text(
                    json.dumps(update.to_dict()), encoding="utf-8")
            raced = store.load("repo#86")
            self.assertIn("holder", raced.conflicts)
            self.assertEqual(raced.holder, "Antigravity")

            store.append(rs.RoundUpdate(round="repo#86", writer="Antigravity",
                                        items={"86": {"owner": "Antigravity",
                                                      "state": "open"}}),
                         holder="Antigravity")
            settled = store.load("repo#86")
            self.assertEqual(settled.conflicts, [])
            self.assertTrue(settled.usable)


class TestClaimIsNotOwnership(unittest.TestCase):
    """Codex blocker 2: a stale coordinator must not end up writing an absent round."""

    def test_a_second_claimant_is_fenced_at_write_time(self):
        with TemporaryDirectory() as tmp:
            winner = rs.RoundStore(None, root=Path(tmp), alias="Antigravity", authority=rs.Authority(alias="Antigravity"))
            winner.append(rs.RoundUpdate(round="repo#86", writer="Antigravity",
                                         items={"86": {"owner": "DeepSeek"}}),
                          holder="Antigravity")
            # A stale, configured coordinator tries the same round: it loses the fold, so its
            # write is refused instead of silently becoming a second writer.
            stale = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            with self.assertRaises(rs.RoundFenced):
                stale.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                            items={"86": {"owner": "Claude"}}),
                             holder="Claude")
            self.assertEqual(winner.load("repo#86").holder, "Antigravity")

    def test_a_lost_claim_is_fenced_even_in_the_same_window(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude", authority=rs.Authority(alias="Claude"))
            folder = store.directory("repo#86")
            folder.mkdir(parents=True)
            # Another bot's claim is already on the bus but not yet folded by this writer.
            (folder / "000000-000001-Anti.json").write_text(json.dumps({
                "version": 1, "round": "repo#86", "writer": "Antigravity", "holder":
                "Antigravity", "seq": 1, "base_seq": 0}), encoding="utf-8")
            with self.assertRaises(rs.RoundFenced):
                store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                            items={"86": {"pr": 89}}), holder="Claude")


class TestRoundListingRefresh(unittest.TestCase):
    """Codex should-fix: `/round` must not list a ref from before the fetch."""

    def test_rounds_refreshes_the_bus_first(self):
        files = {".agents/rounds/repo#86/000000-000001-c-a.json": json.dumps(
            {"version": 1, "round": "repo#86", "writer": "Claude", "seq": 1})}
        transport = _RefTransport(files)
        calls = []
        transport.refresh = lambda: calls.append(1)
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(transport, root=Path(tmp))
            self.assertIn("repo#86", store.rounds())
            self.assertEqual(len(calls), 1)


class TestFirstClaimAuthority(unittest.TestCase):
    """Codex review 4: a stale coordinator must not make the first claim on an absent round.

    The write that creates a round cannot be fenced against a holder, because there is none.
    It needs an authority: the alias the swarm names as its coordinator.
    """

    def test_a_first_claim_without_an_authority_is_refused(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude")   # no authority
            with self.assertRaises(rs.RoundUnauthorised):
                store.append(rs.RoundUpdate(round="repo#86", writer="Claude"))
            self.assertFalse(list(Path(tmp).rglob("*.json")))

    def test_a_stale_coordinator_cannot_claim_a_round_the_swarm_gives_someone_else(self):
        with TemporaryDirectory() as tmp:
            # The swarm says Claudebot is the coordinator; this bot is configured as one too,
            # but it is stale. It must not create the round.
            store = rs.RoundStore(None, root=Path(tmp), alias="Antigravity",
                                  authority=rs.Authority(alias="Claudebot", source="membership"))
            with self.assertRaises(rs.RoundUnauthorised):
                store.append(rs.RoundUpdate(round="repo#86", writer="Antigravity"),
                             holder="Antigravity")
            self.assertEqual(store.load("repo#86").status, "absent")
            self.assertFalse(list(Path(tmp).rglob("*.json")))

    def test_the_named_coordinator_can_make_the_first_claim(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claudebot",
                                  authority=rs.Authority(alias="Claudebot"))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claudebot"),
                         holder="Claudebot")
            state = store.load("repo#86")
            self.assertEqual(state.status, "ok")
            self.assertEqual(state.holder, "Claudebot")

    def test_an_existing_round_needs_no_authority_to_append_to(self):
        with TemporaryDirectory() as tmp:
            first = rs.RoundStore(None, root=Path(tmp), alias="Claudebot",
                                  authority=rs.Authority(alias="Claudebot"))
            first.append(rs.RoundUpdate(round="repo#86", writer="Claudebot"), holder="Claudebot")
            # No authority at all, but the round exists and this bot holds it.
            second = rs.RoundStore(None, root=Path(tmp), alias="Claudebot")
            second.append(rs.RoundUpdate(round="repo#86", writer="Claudebot",
                                         items={"86": {"pr": 89}}), holder="Claudebot")
            self.assertEqual(second.load("repo#86").item("86")["pr"], 89)

    def test_a_first_claim_at_term_m_writes_term_m(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claudebot",
                                  authority=rs.Authority(alias="Claudebot", source="lease", term=3))
            path = store.append(rs.RoundUpdate(round="repo#86", writer="Claudebot"),
                                holder="Claudebot")
            self.assertIn("000003-", Path(path).name)
            state = store.load("repo#86")
            self.assertEqual(state.status, "ok")
            self.assertEqual(state.holder, "Claudebot")
            self.assertEqual(state.term, 3)

    def test_a_lease_term_below_the_folded_round_term_is_refused(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claudebot",
                                  authority=rs.Authority(alias="Claudebot", source="lease", term=3))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claudebot"),
                         holder="Claudebot")
            self.assertEqual(store.load("repo#86").term, 3)
            stale_auth = rs.Authority(alias="Claudebot", source="lease", term=2)
            with self.assertRaises(rs.RoundStaleTerm):
                store.append(rs.RoundUpdate(round="repo#86", writer="Claudebot",
                                            items={"86": {"owner": "Someone"}}),
                             holder="Claudebot", authority=stale_auth)

    def test_a_stale_coordinator_comes_back_and_is_refused(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Reasonix",
                                  authority=rs.Authority(alias="Reasonix", source="lease", term=2))
            store.append(rs.RoundUpdate(round="repo#86", writer="Reasonix",
                                        items={"86": {"owner": "DeepSeek"}}),
                         holder="Reasonix")
            self.assertEqual(store.load("repo#86").holder, "Reasonix")
            self.assertEqual(store.load("repo#86").term, 2)

            claude_store = rs.RoundStore(None, root=Path(tmp), alias="Claude",
                                         authority=rs.Authority(alias="Claude", source="lease", term=1))
            with self.assertRaises(rs.RoundStaleTerm):
                claude_store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                                   items={"86": {"owner": "Antigravity"}}),
                                    holder="Claude")
            self.assertEqual(store.load("repo#86").holder, "Reasonix")
            self.assertEqual(store.load("repo#86").term, 2)


class TestChannelAuthority(unittest.TestCase):
    """The channel passes the alias the swarm names, and says so when it is refused."""

    def channel(self, role, coordinator):
        from tests.test_telegram_channel import event, make_channel, make_config
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        git(root, "init", "-q", "-b", "main")
        (root / "README").write_text("bus\n", encoding="utf-8")
        git(root, "add", "README")
        git(root, "commit", "-q", "-m", "bus")
        config = make_config(agent={"role": role},
                             fallback={"gitbus_root": str(root), "gitbus_ref": "main"})
        channel = make_channel(config)
        channel.membership.state.coordinator = coordinator
        # #83: the authority is the lease, so these two cases stub it directly — one names the
        # channel's own alias (a write), one names a peer (a refusal).
        from swarmly.telegram.rounds import Authority
        channel.round_authority = lambda now=None: (
            Authority(alias=coordinator, source="lease", term=1) if coordinator == "Hermes"
            else Authority(alias="", source="lease", term=0))
        channel.lease_fence = lambda now=None: coordinator == "Hermes"
        return channel, root, event

    def test_a_stale_coordinator_writes_nothing_and_says_why(self):
        channel, root, event = self.channel("coordinator", "Claudebot")
        body = channel.apply_round_block(
            'x\nROUND_STATE {"version": 1, "round": 88, "items": {"86": {"owner": "DeepSeek"}}}',
            event("hi", username="ClaudeBot"))
        self.assertNotIn("ROUND_STATE", body)
        self.assertIn("round state:", body)
        self.assertIn("Claudebot", body)
        self.assertFalse(list(root.rglob("*.json")))

    def test_the_named_coordinator_writes_the_round(self):
        channel, root, event = self.channel("coordinator", "Hermes")
        body = channel.apply_round_block(
            'x\nROUND_STATE {"version": 1, "round": 88, "items": {"86": {"owner": "DeepSeek"}}}',
            event("hi", username="ClaudeBot"))
        # The write lands; a push with no remote may still note that it is not on the bus yet,
        # but nothing may say the bot was not allowed to claim the round.
        self.assertNotIn("may not make the first claim", body)
        self.assertTrue(list((root / ".agents" / "rounds").rglob("*.json")))

    def test_a_stale_configured_coordinator_cannot_create_round_while_another_holds_lease(self):
        channel, root, event = self.channel("coordinator", "Reasonix")
        leases_dir = root / ".agents" / "leases"
        leases_dir.mkdir(parents=True, exist_ok=True)
        (leases_dir / "000002-reasonix-12345678.json").write_text(json.dumps({
            "term": 2, "alias": "Reasonix", "expires_at": time.time() + 3600.0,
            "renews_at": time.time(), "rank": 10, "nonce": "12345678"
        }), encoding="utf-8")
        body = channel.apply_round_block(
            'x\nROUND_STATE {"version": 1, "round": 88, "items": {"86": {"owner": "DeepSeek"}}}',
            event("hi", username="ClaudeBot"))
        self.assertNotIn("ROUND_STATE", body)
        self.assertIn("round state:", body)
        self.assertIn("Reasonix", body)
        self.assertFalse(list((root / ".agents" / "rounds").rglob("*.json")))
