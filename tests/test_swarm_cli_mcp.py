import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.swarm_helpers import SwarmCase, needs_swarm


@needs_swarm
class CliTests(SwarmCase):
    def run_cli(self, *argv, stdin=None):
        from swarmly.swarm.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = main(["--swarm", str(self.swarm_file), *argv])
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        super().setUp()
        self.swarm_file = self.write_swarm_yaml()

    def test_post_next_tasks(self):
        code, out, _ = self.run_cli("--as", "codex", "post", "propose", "--task", "t1",
                                    "--body", "title=Fix it")
        self.assertEqual(code, 0, out)
        self.assertIn("posted codex:1 PROPOSE", out)
        code, out, _ = self.run_cli("--as", "codex", "post", "CLAIM", "--task", "t1")
        self.assertIn("epoch=1", out)
        code, out, _ = self.run_cli("--as", "codex", "tasks")
        self.assertIn("claimed", out)
        code, out, _ = self.run_cli("--as", "codex", "next", "--json")
        data = json.loads(out)
        self.assertEqual(data["my_tasks"], ["t1"])

    def test_rejection_exits_3_and_lists_moves(self):
        code, _, err = self.run_cli("--as", "codex", "post", "OFFER", "--task", "nope")
        self.assertEqual(code, 3)
        self.assertIn("rejected:", err)
        self.assertIn("legal moves now:", err)
        self.assertIn("PROPOSE", err)

    def test_no_identity(self):
        with mock.patch.dict(os.environ, {"A2A_AGENT": ""}):
            code, _, err = self.run_cli("next")
        self.assertEqual(code, 1)
        self.assertIn("no agent identity", err)

    def test_evidence_and_options(self):
        code, out, err = self.run_cli(
            "--as", "codex", "post", "ESCALATE", "--body", "kind=stuck", "--body",
            "reason=CI is down", "--option", "wait", "--option", "skip", "--json")
        self.assertEqual(code, 0, err)
        env = json.loads(out)["envelope"]
        self.assertEqual(env["body"]["options"], ["wait", "skip"])
        code, out, _ = self.run_cli("--as", "alice", "escalations")
        self.assertIn("CI is down", out)

    def test_guard_exit_codes_and_hook(self):
        code, _, err = self.run_cli("--as", "codex", "guard", "--", "git", "push", "--force")
        self.assertEqual(code, 2)
        self.assertIn("force_push", err)
        code, _, _ = self.run_cli("--as", "codex", "guard", "--", "git", "status")
        self.assertEqual(code, 0)
        hook = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "AGENTS.md"}})
        code, _, err = self.run_cli("--as", "codex", "guard", "--hook", stdin=hook)
        self.assertEqual(code, 2)
        hook = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "AGENTS.md"}})
        code, _, _ = self.run_cli("--as", "codex", "guard", "--hook", stdin=hook)
        self.assertEqual(code, 0)

    def test_guard_fails_closed(self):
        from swarmly.swarm.cli import main
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {"A2A_AGENT": ""}):
            self.assertEqual(main(["--swarm", str(self.root / "missing.yaml"), "--as",
                                   "codex", "guard", "--", "git", "status"]), 2)
            self.assertEqual(main(["--swarm", str(self.swarm_file), "guard", "--", "ls"]), 2)
        code, _, _ = self.run_cli("--as", "codex", "guard", "--hook", stdin="not json")
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli("--as", "codex", "guard", "--hook", stdin="[]")
        self.assertEqual(code, 2)

    def test_verify(self):
        self.run_cli("--as", "codex", "post", "NOTE", "--text", "hi")
        code, out, _ = self.run_cli("--as", "claude", "verify")
        self.assertEqual(code, 0)
        self.assertIn("1 envelopes accepted, 0 quarantined", out)

    def test_protocol_doc_check(self):
        from swarmly.swarm.cli import main
        stale = self.root / "doc.md"
        stale.write_text("old")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["protocol-doc", "--check", str(stale)]), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["protocol-doc", "--out", str(stale)])
        self.assertEqual(main(["protocol-doc", "--check", str(stale)]), 0)


@needs_swarm
class McpTests(SwarmCase):
    def call(self, server, method, params=None, mid=1):
        return server.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                              "params": params or {}})

    def test_initialize_list_and_call(self):
        from swarmly.swarm.mcp import Server
        server = Server(self.gov("codex"))
        init = self.call(server, "initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("codex", init["result"]["instructions"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method":
                                         "notifications/initialized"}))
        names = {t["name"] for t in self.call(server, "tools/list")["result"]["tools"]}
        self.assertIn("a2a_post", names)
        ok = self.call(server, "tools/call", {"name": "a2a_post", "arguments": {
            "intent": "propose", "task_id": "t1", "body": {"title": "x"}}})["result"]
        self.assertFalse(ok["isError"])
        self.assertEqual(json.loads(ok["content"][0]["text"])["envelope"]["id"], "codex:1")
        bad = self.call(server, "tools/call", {"name": "a2a_post", "arguments": {
            "intent": "DONE", "task_id": "t1", "body": {"merge_sha": "abcdef1"}}})["result"]
        self.assertTrue(bad["isError"])
        payload = json.loads(bad["content"][0]["text"])
        self.assertTrue(payload["errors"])
        self.assertTrue(payload["legal_moves"])
        guard = self.call(server, "tools/call", {"name": "a2a_guard", "arguments": {
            "command": "git push --force"}})["result"]
        self.assertFalse(json.loads(guard["content"][0]["text"])["allowed"])
        self.assertIn("error", self.call(server, "nope"))

    def test_serve_reads_lines(self):
        from swarmly.swarm.mcp import serve
        stdin = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\nnot json\n')
        stdout = io.StringIO()
        serve(self.gov("codex"), stdin, stdout)
        lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0], {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertEqual(lines[1]["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
