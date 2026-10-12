"""``swarmly mcp-setup``: the config each agent CLI gets, and the live MCP check."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly.swarm import mcp_setup as ms
from tests.swarm_helpers import SwarmCase, needs_swarm

CMD = "C:\\Python311\\python.exe"
ARGS = ["-m", "swarmly.swarm.cli", "--swarm", "C:\\Users\\alice\\a2a\\swarm.yaml", "--as", "codex",
        "mcp"]


needs_toml = unittest.skipIf(ms._toml_parser() is None, "needs tomllib or tomli")


def toml_loads(text):
    parser = ms._toml_parser()
    if parser is None:
        raise unittest.SkipTest("needs tomllib or tomli")
    return parser.loads(text)


def R(cli, env, before="", **kw):
    return ms.render(cli, ms.our_entry(cli, CMD, ARGS, env, **kw), before)


class RenderTests(unittest.TestCase):
    @needs_toml
    def test_codex_toml_appends_and_replaces_only_its_table(self):
        before = ('model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n\n'
                  '[mcp_servers.a2a]\ncommand = "old"\n\n[mcp_servers.a2a.env]\nX = "1"\n\n'
                  '[profiles.fast]\nmodel = "mini"\n')
        text = R("codex", {"A2A_KEY_DIR": "C:\\keys"}, before)
        data = toml_loads(text)
        self.assertEqual(data["mcp_servers"]["a2a"]["command"], CMD)
        self.assertEqual(data["mcp_servers"]["a2a"]["args"], ARGS)
        self.assertEqual(data["mcp_servers"]["a2a"]["env"],
                         {"X": "1", "A2A_KEY_DIR": "C:\\keys"})       # the owner's X stays
        self.assertEqual(data["mcp_servers"]["other"], {"command": "x"})
        self.assertEqual(data["profiles"]["fast"]["model"], "mini")
        self.assertEqual(data["model"], "o4")
        self.assertEqual(R("codex", {"A2A_KEY_DIR": "C:\\keys"}, text), text)
        fresh = toml_loads(R("codex", {}, ""))
        self.assertEqual(fresh["mcp_servers"]["a2a"]["startup_timeout_sec"], 30)
        self.assertEqual(fresh["mcp_servers"]["a2a"]["env_vars"], ["SSH_AUTH_SOCK"])
        # Approvals ride on the swarm's own `codex exec` command line, not in config.toml.
        self.assertNotIn("tools", fresh["mcp_servers"]["a2a"])
        self.assertNotIn("default_tools_approval_mode", fresh["mcp_servers"]["a2a"])

    @needs_toml
    def test_codex_rerun_removes_the_old_blanket_approval(self):
        old = '[mcp_servers.a2a]\ncommand = "old"\ndefault_tools_approval_mode = "approve"\n'
        self.assertNotIn("default_tools_approval_mode", R("codex", {}, old))
        mine = old.replace('"approve"', '"prompt"')
        self.assertIn('default_tools_approval_mode = "prompt"', R("codex", {}, mine))

    def test_launcher_handles_windows_paths_and_safe_path_mode(self):
        import subprocess
        with mock.patch.object(ms, "PACKAGE_ROOT", "C:\\Users\\Ελένη\\a2a 'x'"):
            _, args = ms.server_command(Path("s.yaml"), "codex")
        compile(args[1], "<launcher>", "exec")
        _, args = ms.server_command(Path("s.yaml"), "codex")
        probe = args[1].split("from swarmly")[0] + \
            "import json, swarmly; print('' in sys.path); print(swarmly.__file__)"
        for flags in ([], ["-P"] if sys.version_info >= (3, 11) else []):
            with tempfile.TemporaryDirectory() as cwd:
                (Path(cwd) / "swarmly").mkdir()
                (Path(cwd) / "swarmly" / "__init__.py").write_text("raise SystemExit('decoy')\n")
                out = subprocess.run([sys.executable, *flags, "-c", probe], cwd=cwd,
                                     capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(out[0], "False")
            self.assertTrue(out[1].startswith(ms.PACKAGE_ROOT), out)

    @needs_toml
    def test_codex_keeps_what_the_owner_added_to_its_entry(self):
        before = ('[mcp_servers.a2a]\ncommand = "old"\ntool_timeout_sec = 120\n\n'
                  '[mcp_servers.a2a.tools.a2a_post]\napproval_mode = "prompt"\n')
        a2a = toml_loads(R("codex", {}, before, cwd="/w", token_env="A2A_DOORBELL_CODEX"))[
            "mcp_servers"]["a2a"]
        self.assertEqual(a2a["command"], CMD)
        self.assertEqual(a2a["tool_timeout_sec"], 120)
        self.assertEqual(a2a["tools"], {"a2a_post": {"approval_mode": "prompt"}})
        self.assertEqual(a2a["cwd"], "/w")
        self.assertEqual(a2a["env_vars"], ["A2A_DOORBELL_CODEX", "SSH_AUTH_SOCK"])

    def test_codex_without_a_toml_parser_refuses_to_edit(self):
        with mock.patch.object(ms, "_toml_parser", return_value=None):
            with self.assertRaises(ms.SetupError) as caught:
                R("codex", {}, 'model = "o4"\n')
            self.assertIn("tomli", str(caught.exception))
            self.assertIn("[mcp_servers.a2a]", R("codex", {}, ""))

    def test_hermes_comment_between_servers_is_kept(self):
        import yaml
        before = ("mcp_servers:\n  a2a:\n    command: old\n  # the github one is mine\n"
                  "  github:\n    command: gh\n")
        text = R("hermes", {}, before)
        self.assertIn("  # the github one is mine\n  github:", text)
        self.assertEqual(yaml.safe_load(text)["mcp_servers"]["github"], {"command": "gh"})

    def test_antigravity_and_gemini_json_keep_other_servers(self):
        before = json.dumps({"theme": "dark", "mcpServers": {"fs": {"command": "fs"}}})
        for cli in ("antigravity", "gemini"):
            data = json.loads(R(cli, {}, before))
            self.assertEqual(data["mcpServers"]["a2a"], {"command": CMD, "args": ARGS})
            self.assertEqual(ms.entry_in(cli, json.dumps(data))["command"], CMD)
            self.assertEqual(data["mcpServers"]["fs"], {"command": "fs"})
            self.assertEqual(data["theme"], "dark")
        self.assertIn("a2a", json.loads(R("antigravity", {}, ""))
                      ["mcpServers"])

    def test_hermes_yaml_replaces_its_entry_and_keeps_the_rest(self):
        import yaml
        before = ("model: hermes-4\nmcp_servers:\n    github:\n        command: gh-mcp\n"
                  "    a2a:\n        command: old\n        args: []\n"
                  "terminal:\n  backend: local\n")
        text = R("hermes", {"A2A_LOG_DIR": "off"}, before)
        data = yaml.safe_load(text)
        self.assertEqual(data["mcp_servers"]["a2a"],
                         {"command": CMD, "args": ARGS, "env": {"A2A_LOG_DIR": "off"}})
        self.assertEqual(data["mcp_servers"]["github"], {"command": "gh-mcp"})
        self.assertEqual(data["terminal"], {"backend": "local"})
        self.assertEqual(data["model"], "hermes-4")
        self.assertEqual(R("hermes", {"A2A_LOG_DIR": "off"}, text), text)
        for start in ("", "model: x\n", "model: x"):
            data = yaml.safe_load(R("hermes", {}, start))
            self.assertEqual(data["mcp_servers"]["a2a"]["command"], CMD)

    @needs_toml
    def test_codex_entry_not_in_table_form_is_refused(self):
        for before in ('mcp_servers.a2a.command = "old"\n',
                       '[mcp_servers]\na2a = { command = "old" }\n',
                       '[mcp_servers]\na2a.command = "old"\n'):
            toml_loads(before)
            with self.assertRaises(ms.SetupError, msg=before):
                R("codex", {}, before)
        spaced = '[ mcp_servers . a2a ]\ncommand = "old"\n'
        self.assertEqual(toml_loads(R("codex", {}, spaced))
                         ["mcp_servers"]["a2a"]["command"], CMD)

    @needs_toml
    def test_codex_comments_after_its_table_are_kept(self):
        before = ('[mcp_servers.a2a]\ncommand = "old"\n\n# --- profiles: keep me ---\n'
                  '[profiles.fast]\nmodel = "mini"\n')
        text = R("codex", {}, before)
        self.assertIn("# --- profiles: keep me ---\n[profiles.fast]", text)
        self.assertNotIn('"old"', text)

    @needs_toml
    def test_codex_broken_file_is_refused(self):
        toml_loads("")
        with self.assertRaises(ms.SetupError):
            R("codex", {}, "model = \n")

    def test_hermes_quoted_null_and_commented_keys(self):
        import yaml
        for before in ('"mcp_servers":\n  fs:\n    command: fs\n',
                       "mcp_servers: # mine\n  fs:\n    command: fs\n"):
            data = yaml.safe_load(R("hermes", {}, before))
            self.assertEqual(set(data["mcp_servers"]), {"fs", "a2a"}, before)
        for before in ("mcp_servers: null\n", "mcp_servers: ~\n", "mcp_servers:\n"):
            data = yaml.safe_load(R("hermes", {}, before))
            self.assertEqual(list(data["mcp_servers"]), ["a2a"], before)
        with self.assertRaises(ms.SetupError):
            R("hermes", {}, "mcp_servers: &base\n  fs: {}\n")

    def test_hermes_inline_mcp_servers_is_refused(self):
        with self.assertRaises(ms.SetupError):
            R("hermes", {}, "mcp_servers: {github: {command: x}}\n")

    def test_claude_command_puts_the_name_before_env(self):
        line = ms.claude_command(CMD, ARGS, {"A2A_KEY_DIR": "/k"})
        self.assertEqual(line[:6], ["claude", "mcp", "add", "a2a", "--scope", "user"])
        self.assertEqual(line[6:9], ["-e", "A2A_KEY_DIR=/k", "--"])
        self.assertEqual(line[9:], [CMD, *ARGS])

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions and symlinks")
    def test_write_follows_a_symlink_and_keeps_the_mode(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "dotfiles" / "config.yaml"
            real.parent.mkdir()
            real.write_text("api_key: SECRET\n")
            os.chmod(real, 0o600)
            link = Path(tmp) / "config.yaml"
            link.symlink_to(real)
            ms.write(link, "api_key: SECRET\nmcp_servers: {}\n")
            self.assertTrue(link.is_symlink())
            self.assertIn("mcp_servers", real.read_text())
            self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)

    def test_antigravity_uses_the_path_agy_reads(self):
        path = ms.config_path("antigravity")
        self.assertEqual(path.parts[-3:], (".gemini", "config", "mcp_config.json"))

    def test_write_keeps_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.toml"
            self.assertIsNone(ms.write(path, "a = 1\n"))
            backup = ms.write(path, "a = 2\n")
            self.assertEqual(backup.read_text(), "a = 1\n")
            self.assertEqual(path.read_text(), "a = 2\n")


class CodexApprovalTests(unittest.TestCase):
    """The swarm's `codex exec` runs approve the a2a tools on their own command line."""

    def argv(self, config, prompt="hello", command=None, project=None):
        from swarmly.telegram.adapters.codex import CodexAdapter
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            if config is not None:
                Path(home, "config.toml").write_text(config, encoding="utf-8")
            if project is not None:
                Path(cwd, ".codex").mkdir()
                Path(cwd, ".codex", "config.toml").write_text(project, encoding="utf-8")
            settings = CodexAdapter.default_settings(cwd=cwd)
            if command:
                settings.command = command
            with mock.patch.dict("os.environ", {"CODEX_HOME": home}):
                return CodexAdapter("codex", settings).build_argv(prompt)

    def test_tools_match_the_server(self):
        from swarmly.telegram.adapters.codex import A2A_TOOLS
        self.assertEqual(set(A2A_TOOLS), ms.EXPECTED_TOOLS)

    def test_approval_is_added_only_when_the_server_is_configured(self):
        plain = ["codex", "exec", "--sandbox", "workspace-write", "hello"]
        self.assertEqual(self.argv(None), plain)
        self.assertEqual(self.argv('[mcp_servers.other]\ncommand = "x"\n'), plain)
        argv = self.argv('[mcp_servers.a2a]\ncommand = "py"\n')
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(argv[-3:], ["--sandbox", "workspace-write", "hello"])
        approvals = argv[3:-3:2]
        self.assertEqual(argv[2:-3:2], ["-c"] * 6)
        self.assertIn("mcp_servers.a2a.tools.a2a_post.approval_mode=approve", approvals)
        self.assertFalse(any("default_tools" in a or "bypass" in a for a in argv))

    def test_the_prompt_does_not_switch_the_approval_off(self):
        argv = self.argv('[mcp_servers.a2a]\ncommand = "py"\n',
                         prompt="why does mcp_servers.a2a fail?")
        self.assertEqual(len(argv), 2 + 12 + 3)

    def test_a_project_config_alone_does_not_count(self):
        # Codex skips an untrusted project's .codex/config.toml, then refuses to start.
        self.assertEqual(len(self.argv(None, project='[mcp_servers.a2a]\ncommand = "p"\n')),
                         5)

    def test_other_templates(self):
        table = '[mcp_servers.a2a]\ncommand = "py"\n'
        argv = self.argv(table, command="codex -m o3 exec {prompt}")
        self.assertEqual(argv[:4], ["codex", "-m", "o3", "exec"])
        self.assertEqual(argv[4], "-c")
        for command in ("codex exec --ignore-user-config {prompt}", "codex queue {prompt}"):
            self.assertNotIn("-c", self.argv(table, command=command), command)


@needs_swarm
class CheckTests(SwarmCase):
    def run_cli(self, *argv):
        from swarmly.swarm.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--swarm", str(self.swarm_file), *argv])
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        super().setUp()
        self.swarm_file = self.write_swarm_yaml()

    def test_check_talks_to_the_real_server(self):
        command, args = ms.server_command(self.swarm_file, "codex")
        result = ms.check(command, args, ms.server_env())
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["tools"]), ms.EXPECTED_TOOLS)
        self.assertEqual(result["agent"], "codex")
        self.assertIn("You are codex", result["instructions"])
        self.assertEqual(list((self.root / "lanes").glob("**/*.json")), [])   # posted nothing

    def test_check_reports_a_server_that_cannot_start(self):
        result = ms.check(sys.executable, ["-c", "import sys; sys.exit(3)"])
        self.assertFalse(result["ok"])
        self.assertIn("no answer to initialize", result["error"])
        noisy = ms.check(sys.executable, ["-c", "print('hello')"])
        self.assertIn("non-MCP output", noisy["error"])

    @needs_toml
    def test_cli_writes_codex_config_and_checks_it(self):
        config = self.root / "codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "o4"\n', encoding="utf-8")
        code, out, _ = self.run_cli("--as", "codex", "mcp-setup", "codex",
                                    "--config", str(config))
        self.assertEqual(code, 0)
        self.assertIn("[mcp_servers.a2a]", out)
        self.assertEqual(config.read_text(), 'model = "o4"\n')           # dry run
        code, out, err = self.run_cli("--as", "codex", "mcp-setup", "codex", "--write",
                                      "--check", "--config", str(config))
        self.assertEqual(code, 0, err)
        self.assertIn("MCP check OK", out)
        # Parsed, not searched: TOML escapes the backslashes of a Windows path.
        args = ms._toml_load(config.read_text())["mcp_servers"]["a2a"]["args"]
        self.assertIn(str(self.swarm_file.resolve()), args)
        self.assertTrue((config.parent / "config.toml.bak").exists())
        code, out, _ = self.run_cli("--as", "codex", "mcp-setup", "codex", "--write",
                                    "--config", str(config))
        self.assertIn("already has", out)

    def test_cli_claude_dry_run_and_write(self):
        code, out, _ = self.run_cli("--as", "claude", "mcp-setup", "claude")
        self.assertEqual(code, 0)
        self.assertIn("claude mcp add a2a --scope user", out)

        def run_with(replies):
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                if cmd[1:3] == ["mcp", "get"]:
                    return mock.Mock(returncode=0, stderr="",
                                     stdout=f"a2a: ... --swarm {self.swarm_file.resolve()}")
                code, err = replies.pop(0)
                return mock.Mock(returncode=code, stderr=err, stdout="")
            with mock.patch("shutil.which", return_value="/bin/claude"), \
                    mock.patch("swarmly.swarm.cli.subprocess.run", fake_run):
                code, out, err = self.run_cli("--as", "claude", "mcp-setup", "claude",
                                              "--write")
            return code, [c[1:3] for c in calls], err
        code, calls, _ = run_with([(0, "")])
        self.assertEqual((code, calls), (0, [["mcp", "add"], ["mcp", "get"]]))
        code, calls, _ = run_with([(1, "MCP server a2a already exists in user config"),
                                   (0, ""), (0, "")])
        self.assertEqual((code, calls), (0, [["mcp", "add"], ["mcp", "remove"],
                                             ["mcp", "add"], ["mcp", "get"]]))
        code, calls, err = run_with([(1, "invalid scope")])     # the old entry is kept
        self.assertEqual((code, calls), (1, [["mcp", "add"]]))
        self.assertIn("invalid scope", err)
        with mock.patch("shutil.which", return_value=None):
            code, _, err = self.run_cli("--as", "claude", "mcp-setup", "claude", "--write")
        self.assertEqual(code, 1)
        self.assertIn("not on PATH", err)

    def test_cli_dry_run_shows_a_diff_and_names_a_bad_file(self):
        config = self.root / "settings.json"
        config.write_text('{"theme": "dark"}', encoding="utf-8")
        code, out, _ = self.run_cli("--as", "antigravity", "mcp-setup", "gemini",
                                    "--config", str(config))
        self.assertEqual(code, 0)
        self.assertIn('+    "a2a": {', out)
        self.assertIn('-{"theme": "dark"}', out)
        config.write_text('{\n  // my servers\n  "mcpServers": {}\n}\n', encoding="utf-8")
        code, _, err = self.run_cli("--as", "antigravity", "mcp-setup", "gemini",
                                    "--config", str(config))
        self.assertEqual(code, 1)
        self.assertIn(str(config), err)
        self.assertIn("comments are not supported", err)

    def test_check_skips_blank_lines_and_reports_an_initialize_error(self):
        script = ("import sys, json\n"
                  "for line in sys.stdin:\n"
                  "    m = json.loads(line)\n"
                  "    if m.get('id') == 1:\n"
                  "        print()\n"
                  "        print(json.dumps({'jsonrpc': '2.0', 'id': 1,"
                  " 'error': {'code': -32602, 'message': 'bad version'}}))\n")
        result = ms.check(sys.executable, ["-c", script])
        self.assertIn("initialize failed", result["error"])
        self.assertIn("bad version", result["error"])
        slow = ms.check(sys.executable, ["-c", "import sys, time; sys.stderr.write('stuck');"
                                         " sys.stderr.flush(); time.sleep(30)"], timeout=1)
        self.assertIn("did not answer within 1s", slow["error"])

    def test_identity_is_explicit_and_matches_the_cli(self):
        cases = [((), "needs --as"),
                 (("--as", "nobody"), "not in the swarm roster"),
                 (("--as", "alice"), "is the owner"),
                 (("--as", "hermes"), "pass --force")]
        with mock.patch.dict("os.environ", {"A2A_AGENT": "codex"}):
            for who, why in cases:
                code, _, err = self.run_cli(*who, "mcp-setup", "codex")
                self.assertEqual(code, 1, who)
                self.assertIn(why, err, who)
        code, out, err = self.run_cli("--as", "hermes", "mcp-setup", "codex", "--force",
                                      "--config", str(self.root / "c.toml"))
        self.assertEqual(code, 0, err)
        self.assertIn("codex will act as hermes (worker, nous) in demo", out)

    def test_relative_bus_path_is_refused(self):
        self.config["bus"]["path"] = "lanes"
        self.write_swarm_yaml()
        code, _, err = self.run_cli("--as", "codex", "mcp-setup", "codex",
                                    "--config", str(self.root / "c.toml"))
        self.assertEqual(code, 1)
        self.assertIn("is relative", err)

    def test_a_key_that_does_not_match_the_roster_is_reported(self):
        swarm = self.swarm
        self.assertIsNone(ms.check_key(swarm, "codex", ms.server_env()))
        swarm.agents["codex"].key = swarm.agents["claude"].key
        self.assertIn("does not match", ms.check_key(swarm, "codex", ms.server_env()))

    def test_server_ignores_a_swarmly_package_in_its_working_directory(self):
        import subprocess
        command, args = ms.server_command(self.swarm_file, "codex")
        decoy = self.root / "workspace" / "swarmly"
        decoy.mkdir(parents=True)
        (decoy / "__init__.py").write_text("raise SystemExit('the workspace swarmly ran')\n")
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}}) + "\n"
        proc = subprocess.run([command, *args], input=init.encode(), capture_output=True,
                              cwd=str(decoy.parent), timeout=60)
        self.assertNotIn(b"workspace swarmly ran", proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["id"], 1)

    def test_server_speaks_utf8_under_a_cp1252_locale(self):
        import os
        import subprocess
        command, args = ms.server_command(self.swarm_file, "codex")
        text = "→ Καλημέρα ✓"
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": f"a2a_{text}", "arguments": {}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": [1]},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "a2a_next", "arguments": "x"}}]
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "LC_ALL")}
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0", LANG="C")
        stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in lines)
        proc = subprocess.run([command, *args], input=stdin.encode("utf-8"),
                              capture_output=True, env=env, timeout=60)
        replies = {r["id"]: r for r in map(json.loads, proc.stdout.decode("utf-8").splitlines())}
        self.assertEqual(set(replies), {1, 2, 3, 4}, proc.stderr)
        self.assertIn(text, replies[2]["result"]["content"][0]["text"])
        self.assertEqual(replies[3]["error"]["code"], -32602)          # the server lives on
        self.assertEqual(replies[4]["error"]["code"], -32602)

    def test_only_reading_tools_are_marked_read_only(self):
        from swarmly.swarm.mcp import TOOLS
        marked = {t["name"] for t in TOOLS if (t.get("annotations") or {}).get("readOnlyHint")}
        # a2a_guard records ACTION_TAKEN and spends a one-shot approval.
        self.assertEqual(marked, {"a2a_next", "a2a_inbox", "a2a_tasks", "a2a_escalations"})

    def test_unknown_agent_fails_the_check(self):
        command, args = ms.server_command(self.swarm_file, "nobody")
        self.assertFalse(ms.check(command, args, ms.server_env())["ok"])


if __name__ == "__main__":
    unittest.main()
