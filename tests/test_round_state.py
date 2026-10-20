"""Durable round state on the bus (issue #86).

The properties these tests pin are the ones the design review asked for: writes are
append-only (a lost push costs a segment, not the state), a segment is guarded by term and
seq, a verdict is cleared when the head moves, a state that cannot be read is never
"nothing is open", and the round's files ride the bus push path like any other.
"""

import json
import os
import subprocess
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
        self.store = rs.RoundStore(None, root=self.root, alias="Claude")

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
        config = make_config(agent={"role": role},
                             fallback={"gitbus_root": tmp.name})
        return make_channel(config), Path(tmp.name), event

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


class TestUnchanged(unittest.TestCase):
    """A turn that repeats the state must not add a commit to the bus's history."""

    def test_the_same_state_twice_writes_one_segment(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude")
            update = {"round": "repo#86", "writer": "Claude", "items": {"86": {"owner": "A"}}}
            first = store.append(rs.RoundUpdate(**update))
            again = store.append(rs.RoundUpdate(**update))
            self.assertEqual(first, again)
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 1)

    def test_a_changed_field_is_written(self):
        with TemporaryDirectory() as tmp:
            store = rs.RoundStore(None, root=Path(tmp), alias="Claude")
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "A"}}))
            store.append(rs.RoundUpdate(round="repo#86", writer="Claude",
                                        items={"86": {"owner": "B"}}))
            self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 2)
