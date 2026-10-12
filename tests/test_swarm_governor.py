import os
import unittest
from datetime import datetime, timedelta, timezone

from tests.swarm_helpers import EV, SwarmCase, needs_swarm

SHA = "a1b2c3d4e5f6"


@needs_swarm
class LifecycleTests(SwarmCase):
    def _to_ready(self, worker="codex", task="t1"):
        self.post(worker, "PROPOSE", task_id=task, body={"title": "Fix the poller"})
        self.post("claude", "OFFER", task_id=task)
        self.post(worker, "CLAIM", task_id=task)
        self.post(worker, "PROGRESS", task_id=task, body={"text": "half way"})
        self.post(worker, "READY", task_id=task, body={"head_sha": SHA, "branch": "fix"},
                  evidence=EV)

    def test_happy_path(self):
        self._to_ready()
        self.post("antigravity", "REVIEW", task_id="t1",
                  body={"verdict": "approve", "head_sha": SHA}, evidence=EV)
        self.post("claude", "DONE", task_id="t1", body={"merge_sha": "f00dbabe"})
        task = self.view().tasks["t1"]
        self.assertEqual(task.state, "done")
        self.assertEqual(task.epoch, 1)
        self.assertEqual(task.merge_sha, "f00dbabe")
        self.assertEqual(self.view().violations, [])

    def test_unknown_intent_is_rejected_with_legal_moves(self):
        rej = self.rejected("codex", "TASK_DONE", task_id="t1")
        self.assertIn("unknown intent", rej.errors[0])
        intents = {m["intent"] for m in rej.legal}
        self.assertIn("PROPOSE", intents)
        self.assertNotIn("OFFER", intents)  # workers cannot offer

    def test_worker_cannot_offer(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        rej = self.rejected("codex", "OFFER", task_id="t1")
        self.assertIn("planner/owner", rej.errors[0])

    def test_second_claim_is_rejected(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        self.post("codex", "CLAIM", task_id="t1")
        rej = self.rejected("antigravity", "CLAIM", task_id="t1")
        self.assertTrue(any("is claimed" in e for e in rej.errors), rej.errors)

    def test_offer_to_one_agent_reserves_it(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        self.post("claude", "OFFER", task_id="t1", body={"assignee": "hermes"})
        rej = self.rejected("codex", "CLAIM", task_id="t1")
        self.assertIn("offered to hermes", rej.errors[0])
        self.post("hermes", "CLAIM", task_id="t1")

    def test_ready_needs_evidence_and_a_sha(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        self.post("codex", "CLAIM", task_id="t1")
        rej = self.rejected("codex", "READY", task_id="t1",
                            body={"head_sha": "main", "branch": "fix"})
        joined = " ".join(rej.errors)
        self.assertIn("evidence", joined)
        self.assertIn("hex commit SHA", joined)

    def test_review_rules(self):
        self._to_ready()
        rej = self.rejected("codex", "REVIEW", task_id="t1",
                            body={"verdict": "approve", "head_sha": SHA}, evidence=EV)
        self.assertTrue(any("own work" in e for e in rej.errors))
        # Same vendor as the claimant is not an independent review.
        self._to_ready(worker="claude2", task="t2")
        rej = self.rejected("claude", "REVIEW", task_id="t2",
                            body={"verdict": "approve", "head_sha": SHA}, evidence=EV)
        self.assertTrue(any("different vendor" in e for e in rej.errors), rej.errors)
        # A review of another head does not count.
        rej = self.rejected("codex", "REVIEW", task_id="t2",
                            body={"verdict": "approve", "head_sha": "deadbeef"}, evidence=EV)
        self.assertTrue(any("READY was" in e for e in rej.errors))
        self.post("codex", "REVIEW", task_id="t2",
                  body={"verdict": "changes_requested", "head_sha": SHA}, evidence=EV)
        self.assertEqual(self.view().tasks["t2"].state, "changes_requested")

    def test_done_needs_approval(self):
        self._to_ready()
        rej = self.rejected("claude", "DONE", task_id="t1", body={"merge_sha": "abcdef1"})
        self.assertIn("needs approved", rej.errors[0])

    def test_lost_claim_fences_the_old_claimant(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        self.post("codex", "CLAIM", task_id="t1")
        self.post("codex", "RELEASE", task_id="t1")
        self.post("antigravity", "CLAIM", task_id="t1")
        self.assertEqual(self.view().tasks["t1"].epoch, 2)
        rej = self.rejected("codex", "PROGRESS", task_id="t1", body={"text": "still here"})
        self.assertTrue(any("only the claimant (antigravity)" in e for e in rej.errors))
        rej = self.rejected("antigravity", "PROGRESS", task_id="t1", epoch=1,
                            body={"text": "stale"})
        self.assertTrue(any("stale epoch" in e for e in rej.errors))

    def test_racing_claims_only_first_wins_on_replay(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        view = self.view()
        # Both agents drafted against the same view, so both look legal locally.
        a = self.gov("codex").draft(view, "CLAIM", task_id="t1")
        b = self.gov("antigravity").draft(view, "CLAIM", task_id="t1")
        # A coarse clock (Windows: about 15.6 ms) can give both drafts the same ts, and the
        # (ts, lane, seq) tie-break would then pick "antigravity". Make codex's strictly first.
        from swarmly.swarm.envelope import parse_ts
        b.ts = (parse_ts(a.ts) + timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertEqual((a.epoch, b.epoch), (1, 1))
        for gov, env in (("codex", a), ("antigravity", b)):
            env.sign_with(self.gov(gov).private_key())
            self.store.append(env)
        proj = self.view()
        self.assertEqual(proj.tasks["t1"].claimant, "codex")
        self.assertEqual([v.id for v in proj.violations], [b.id])

    def test_backdating_cannot_win_a_race_it_had_seen(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        honest = self.post("codex", "CLAIM", task_id="t1")
        gov = self.gov("antigravity")
        view = gov.view()
        env = gov.draft(view, "CLAIM", task_id="t1", epoch=1)
        self.assertEqual(env.seen, {"codex": 2})
        env.ts = "2000-01-01T00:00:00.000000Z"  # forged to sort before codex's claim
        env.sign_with(gov.private_key())
        self.store.append(env)
        proj = self.view()
        self.assertEqual(proj.tasks["t1"].claimant, "codex")
        bad = {v.id: v.errors for v in proj.violations}
        self.assertIn(env.id, bad)
        self.assertIn(honest.id, bad[env.id][0])

    def test_slow_clock_still_orders_after_what_it_saw(self):
        from swarmly.swarm.governor import Governor
        self.post("codex", "NOTE", body={"text": "now"})
        slow = Governor(self.swarm, self.store, "hermes",
                        clock=lambda: "2000-01-01T00:00:00.000000Z")
        env = slow.post("NOTE", body={"text": "late clock"})
        self.assertGreater(env.ts, "2000-01-01T00:00:00.000000Z")
        self.assertEqual(self.view().violations, [])

    def test_failures_page_the_owner(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        for _ in range(3):
            self.post("codex", "CLAIM", task_id="t1")
            self.post("codex", "FAILED", task_id="t1", body={"reason": "tests red"})
        esc = self.view().escalations()
        self.assertEqual([e["kind"] for e in esc], ["stuck"])
        self.assertIn("failed 3 times", esc[0]["reason"])
        self.assertEqual(esc[0]["safe_default"], "park")
        owner = self.gov("alice").next()
        self.assertTrue(any(o["do"] == "REJECT" for o in owner["obligations"]))

    def test_idle_swarm_pages_the_owner(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        later = datetime.now(timezone.utc) + timedelta(hours=13)
        kinds = [e["reason"] for e in self.view().escalations(now=later)]
        self.assertTrue(any("no progress" in r for r in kinds), kinds)
        self.assertEqual(self.view().escalations(), [])

    def test_escalate_is_only_for_safety_or_stuck(self):
        rej = self.rejected("codex", "ESCALATE",
                            body={"kind": "opinion", "reason": "x", "options": ["a"]})
        self.assertIn("safety or stuck", rej.errors[0])
        env = self.post("codex", "ESCALATE",
                        body={"kind": "stuck", "reason": "CI broken", "options": ["a", "b"]})
        esc = self.view().escalations()
        self.assertEqual(esc[0]["id"], env.id)
        self.post("alice", "DECISION", re=env.id, body={"decision": "a"})
        self.assertEqual(self.view().escalations(), [])
        rej = self.rejected("codex", "DECISION", re=env.id, body={"decision": "b"})
        self.assertIn("owner", rej.errors[0])

    def test_query_creates_an_obligation(self):
        q = self.post("codex", "QUERY", to=["hermes"], body={"text": "which port?"})
        obs = self.gov("hermes").next()["obligations"]
        self.assertEqual(obs[0]["do"], "ANSWER")
        self.post("hermes", "ANSWER", re=q.id, body={"text": "8080"})
        self.assertEqual(self.gov("hermes").next()["obligations"], [])
        rej = self.rejected("codex", "QUERY", body={"text": "anyone?"})
        self.assertIn("name its recipient", rej.errors[0])

    def test_next_lists_obligations_and_moves(self):
        self._to_ready()
        nxt = self.gov("antigravity").next()
        self.assertEqual(nxt["obligations"][0]["do"], "REVIEW")
        moves = {(m["intent"], m.get("task_id")) for m in nxt["legal_moves"]}
        self.assertIn(("REVIEW", "t1"), moves)
        self.assertNotIn(("PROGRESS", "t1"), moves)
        planner = self.gov("claude").next()
        self.assertIn("escalations", planner)


@needs_swarm
class DisputeTests(SwarmCase):
    def test_rounds_ruling_and_appeal(self):
        note = self.post("codex", "NOTE", body={"text": "the poller is fine"})
        rej = self.rejected("antigravity", "OBJECTION", re=note.id, body={"claim": "no"})
        self.assertIn("evidence", " ".join(rej.errors))
        obj = self.post("antigravity", "OBJECTION", re=note.id, body={"claim": "it drops"},
                        evidence=[{"kind": "log", "ref": "drops 60%"}])
        obs = self.gov("codex").next()["obligations"]
        self.assertEqual(obs[0]["do"], "RESPONSE")
        self.post("codex", "RESPONSE", re=obj.id, body={"text": "fixed in #134"})
        self.post("antigravity", "RESPONSE", re=obj.id, body={"text": "not on my host"})
        rej = self.rejected("codex", "RESPONSE", re=obj.id, body={"text": "yes it is"})
        self.assertIn("only a RULING", rej.errors[0])
        planner = self.gov("claude").next()["obligations"]
        self.assertEqual(planner[0]["do"], "RULING")
        rej = self.rejected("codex", "RULING", re=obj.id, body={"decision": "mine"})
        self.assertIn("planner/owner", rej.errors[0])
        self.post("claude", "RULING", re=obj.id, body={"decision": "upgrade the host"})
        self.assertEqual(self.view().escalations(), [])
        rej = self.rejected("claude", "RULING", re=obj.id, body={"decision": "again"})
        self.assertIn("already has a ruling", rej.errors[0])
        self.post("antigravity", "OBJECTION", re=obj.id, body={"claim": "wrong"},
                  evidence=[{"kind": "text", "ref": "see log"}])
        esc = self.view().escalations()
        self.assertEqual([e["kind"] for e in esc], ["stuck"])
        rej = self.rejected("claude", "RULING", re=obj.id, body={"decision": "still"})
        self.assertIn("only the owner", rej.errors[0])
        self.post("alice", "RULING", re=obj.id, body={"decision": "final"})
        self.assertEqual(self.view().escalations(), [])
        rej = self.rejected("antigravity", "OBJECTION", re=obj.id, body={"claim": "x"},
                            evidence=EV)
        self.assertIn("closed", rej.errors[0])
        # Objecting to the original target again does not reopen it.
        rej = self.rejected("antigravity", "OBJECTION", re=note.id, body={"claim": "x"},
                            evidence=EV)
        self.assertIn("closed", rej.errors[0])

    def test_objecting_to_a_ruling_is_an_appeal(self):
        note = self.post("codex", "NOTE", body={"text": "x"})
        obj = self.post("antigravity", "OBJECTION", re=note.id, body={"claim": "no"},
                        evidence=EV)
        ruling = self.post("claude", "RULING", re=obj.id, body={"decision": "codex is right"})
        self.post("antigravity", "OBJECTION", re=ruling.id, body={"claim": "bad ruling"},
                  evidence=EV)
        self.assertEqual([e["kind"] for e in self.view().escalations()], ["stuck"])
        self.assertEqual(len(self.view().disputes), 1)


@needs_swarm
class SafetyTests(SwarmCase):
    def test_guard_blocks_until_the_owner_approves_that_exact_command(self):
        codex = self.gov("codex")
        cmd = "git push --force origin fix"
        res = codex.guard(cmd)
        self.assertFalse(res.allowed)
        self.assertEqual(res.hits[0]["category"], "force_push")
        self.assertIn("REQUEST_ACTION", res.message())
        req = self.post("codex", "REQUEST_ACTION",
                        body={"category": "force_push", "action": cmd, "reason": "rebased"})
        self.assertEqual(self.view().escalations()[0]["kind"], "safety")
        rej = self.rejected("codex", "APPROVE_ACTION", re=req.id, body={"decision": "approve"})
        self.assertIn("owner", rej.errors[0])
        self.post("alice", "APPROVE_ACTION", re=req.id, body={"decision": "approve"})
        self.assertTrue(codex.guard(cmd).allowed)
        self.assertFalse(codex.guard("git push --force origin main").allowed)
        self.assertFalse(self.gov("hermes").guard(cmd).allowed)

    def test_approval_is_single_use(self):
        codex = self.gov("codex")
        cmd = "git push --force origin fix"
        req = self.post("codex", "REQUEST_ACTION",
                        body={"category": "force_push", "action": cmd, "reason": "rebased"})
        self.post("alice", "APPROVE_ACTION", re=req.id, body={"decision": "approve"})
        rej = self.rejected("hermes", "ACTION_TAKEN", re=req.id, body={"action": cmd})
        self.assertIn("only the agent that asked", rej.errors[0])
        self.assertTrue(codex.guard(cmd).allowed)
        self.assertFalse(codex.guard(cmd).allowed)
        self.assertIn(req.id, self.view().used)

    def test_denied_request_stays_blocked(self):
        cmd = "gh release create v1.0"
        req = self.post("codex", "REQUEST_ACTION",
                        body={"category": "release", "action": cmd, "reason": "ship"})
        self.post("alice", "APPROVE_ACTION", re=req.id, body={"decision": "deny"})
        self.assertFalse(self.gov("codex").guard(cmd).allowed)

    def test_command_patterns(self):
        g = self.gov("codex")
        blocked = ["git push -f origin x", "git push origin +x", "git push origin --delete x",
                   "git push origin :old", "rm -rf build", "git tag v1", "git push --tags",
                   "git filter-repo --path x", "cat .env", "echo x >> AGENTS.md",
                   "sed -i s/a/b/ swarmly/swarm/protocol.yaml", "cp x .github/workflows/t.yml",
                   # Spellings that a plain regex on the raw string would miss.
                   "git -C . push --force origin main", "git -c a=b push -f x",
                   "git --git-dir=.git push origin :old", "sudo env X=1 git push --force",
                   "bash -c 'git -C . push -f'", "make && git -C x push -f",
                   "rm --force --recursive build", "rm -v -r build", "git rm -rf src",
                   "find . -delete", "find . -name x -exec rm {} +"]
        allowed = ["git push origin fix", "git push -u origin fix", "rm build/x.o",
                   "git tag -l", "cat AGENTS.md", "pytest -q", "git status"]
        for cmd in blocked:
            self.assertFalse(g.guard(cmd, sync=False).allowed, cmd)
        for cmd in allowed:
            self.assertTrue(g.guard(cmd, sync=False).allowed, cmd)

    def test_paths(self):
        g = self.gov("codex")
        self.assertFalse(g.guard(paths=["swarm.yaml"]).allowed)
        self.assertTrue(g.guard(paths=["swarm.yaml"], writes=False).allowed)
        self.assertFalse(g.guard(paths=["/home/x/.ssh/id_ed25519"], writes=False).allowed)
        self.assertTrue(g.guard(paths=["swarmly/bus.py"]).allowed)

    def test_rules_change_needs_approval_category(self):
        rej = self.rejected("codex", "REQUEST_ACTION",
                            body={"category": "vibes", "action": "x", "reason": "y"})
        self.assertIn("not on the safety list", rej.errors[0])


@needs_swarm
class BudgetTests(SwarmCase):
    policy = {"token_budget_per_task": 100}

    def test_budget_request_must_name_a_task(self):
        rej = self.rejected("codex", "REQUEST_ACTION",
                            body={"category": "budget", "action": "more", "reason": "big"})
        self.assertIn("task_id", rej.errors[0])

    def test_budget_needs_owner_approval(self):
        self.post("codex", "PROPOSE", task_id="t1", body={"title": "x"})
        self.post("codex", "CLAIM", task_id="t1")
        self.post("codex", "PROGRESS", task_id="t1", body={"text": "a", "tokens": 80})
        rej = self.rejected("codex", "PROGRESS", task_id="t1", body={"text": "b", "tokens": 30})
        self.assertIn("over the budget", rej.errors[0])
        req = self.post("codex", "REQUEST_ACTION", task_id="t1",
                        body={"category": "budget", "action": "more tokens", "reason": "big"})
        self.post("alice", "APPROVE_ACTION", re=req.id, task_id="t1",
                  body={"decision": "approve"})
        self.post("codex", "PROGRESS", task_id="t1", body={"text": "b", "tokens": 30})
        self.assertEqual(self.view().tasks["t1"].tokens, 110)


@needs_swarm
class IdentityTests(SwarmCase):
    def test_no_default_identity(self):
        from swarmly.swarm.governor import Governor
        from swarmly.swarm.protocol import SwarmConfigError
        with self.assertRaises(SwarmConfigError):
            Governor(self.swarm, self.store, "")
        with self.assertRaises(SwarmConfigError):
            Governor(self.swarm, self.store, "mallory")

    def test_key_must_match_roster(self):
        from swarmly.swarm import keys
        from swarmly.swarm.protocol import SwarmConfigError
        keys.generate("codex", overwrite=True)
        with self.assertRaises(SwarmConfigError):
            self.post("codex", "NOTE", body={"text": "x"})

    def test_key_file_is_private(self):
        from swarmly.swarm import keys
        if os.name == "posix":
            self.assertEqual(keys.key_path("codex").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
