"""The CI guard for issue #7 (brainstorm 2.4/2.5): tests start git through ``gitenv``.

Two things are checked, so the guard cannot go vacuous:

- ``tests/gitenv.py`` scrubs what a guarded host leaks into the environment, and keeps
  whatever the caller passes on top;
- no test module starts ``git`` with an inherited environment: every ``subprocess`` call
  whose argv is a ``git`` command passes ``env=`` built from ``git_env()``.

The scanner is a plain AST walk over ``tests/*.py`` -- it needs no git and no network.  A
call that has to run git in a *deliberately* hostile environment (the guard's own tests)
passes ``env=git_env(...)`` too and then layers the guard on top, so no opt-out is needed.
"""

from __future__ import annotations

import ast
import os
import sys
import subprocess
import unittest
from pathlib import Path

from tests import gitenv

TESTS = Path(__file__).resolve().parent
SPAWNERS = {"run", "call", "check_call", "check_output", "Popen"}
ALLOWED_ENV_WORDS = ("git_env", "gitenv")


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _git_commands(tree: ast.Module) -> list:
    """The ``subprocess``/``os`` calls in ``tree`` whose argv is a git command."""
    constants = {}
    for node in tree.body:                      # module-level ``GIT = "git"`` style aliases
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) not in SPAWNERS:
            continue
        if not node.args:
            continue
        argv = node.args[0]
        if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
            continue
        first = argv.elts[0]
        head = first.value if isinstance(first, ast.Constant) else constants.get(
            getattr(first, "id", None))
        if isinstance(head, str) and Path(head).name in ("git", "git.exe"):
            found.append(node)
    return found


def _env_is_scrubbed(call: ast.Call, source: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "env":
            continue
        text = ast.get_source_segment(source, keyword.value) or ""
        if any(word in text for word in ALLOWED_ENV_WORDS):
            return True
    return False


def violations(source: str, where: str = "<source>") -> list:
    """Human-readable lines for every git spawn in ``source`` that bypasses ``git_env``."""
    tree = ast.parse(source)
    return [f"{where}:{call.lineno}: git starts without env=git_env()"
            for call in _git_commands(tree) if not _env_is_scrubbed(call, source)]


class TestEveryGitSpawnIsScrubbed(unittest.TestCase):

    def test_the_test_modules_spawn_git_through_the_helper(self):
        offenders = []
        scanned = []
        for path in sorted(TESTS.glob("*.py")):
            if path.name in ("gitenv.py", Path(__file__).name):
                continue
            source = path.read_text(encoding="utf-8")
            found = violations(source, path.name)
            offenders.extend(found)
            if _git_commands(ast.parse(source)):
                if "git_env" not in source:
                    offenders.append(f"{path.name}: spawns git but never imports git_env")
                scanned.append(path.name)
        self.assertTrue(scanned, "the scanner found no git-spawning test at all")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_scanner_sees_a_violation(self):
        bad = "import subprocess\nsubprocess.run(['git', 'init', '.'])\n"
        self.assertEqual(len(violations(bad, "bad.py")), 1, violations(bad))
        good = ("import subprocess\nfrom tests.gitenv import git_env\n"
                "subprocess.run(['git', 'init', '.'], env=git_env())\n")
        self.assertEqual(violations(good, "good.py"), [])
        aliased = "import subprocess\nGIT = 'git'\nsubprocess.run([GIT, 'status'])\n"
        self.assertEqual(len(violations(aliased, "aliased.py")), 1)


class TestTheHelperScrubs(unittest.TestCase):

    def test_the_guard_and_the_identity_are_replaced(self):
        hostile = {
            "PATH": os.pathsep.join(["/usr/bin", "/nowhere"]),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/nowhere/hooks",
            "GIT_AUTHOR_NAME": "Guard",
            "GIT_COMMITTER_EMAIL": "guard@a2a.local",
            "SWARMLY_ROLE": "developer",
            "SWARMLY_REAL_GIT": "/usr/bin/git",
            "KEEP": "me",
        }
        env = gitenv.git_env(hostile)
        for gone in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                     "SWARMLY_ROLE", "SWARMLY_REAL_GIT"):
            self.assertNotIn(gone, env)
        self.assertEqual(env["GIT_AUTHOR_NAME"], gitenv.TEST_IDENTITY["GIT_AUTHOR_NAME"])
        self.assertEqual(env["GIT_COMMITTER_EMAIL"],
                         gitenv.TEST_IDENTITY["GIT_COMMITTER_EMAIL"])
        self.assertEqual(env["KEEP"], "me")

    def test_overrides_win(self):
        env = gitenv.git_env(SWARMLY_ROLE="developer", GIT_CONFIG_COUNT="2")
        self.assertEqual(env["SWARMLY_ROLE"], "developer")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")

    def test_a_guard_directory_leaves_path(self):
        with TemporaryDirectoryPath() as folder:
            guard = folder / "guard"
            guard.mkdir()
            (guard / "gh").write_text("#!/bin/sh\n", encoding="utf-8")
            path = os.pathsep.join([str(guard), "/usr/bin"])
            self.assertNotIn(str(guard), gitenv.git_env({"PATH": path})["PATH"])


class TemporaryDirectoryPath:
    """``tempfile.TemporaryDirectory`` as a context manager yielding a ``Path``."""

    def __enter__(self) -> Path:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name)

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class TestSubprocessIsScrubbedEndToEnd(unittest.TestCase):
    """A git child sees the scrubbed environment, not this process's hostile one."""

    def test_git_runs_without_the_guard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            gitenv.git(repo, "init", "-q", "-b", "main")
            gitenv.git(repo, "commit", "-q", "--allow-empty", "-m", "one")
            shown = gitenv.git(repo, "log", "-1", "--format=%an <%ae>")
            self.assertEqual(shown.stdout.strip(),
                             f'{gitenv.TEST_IDENTITY["GIT_AUTHOR_NAME"]} '
                             f'<{gitenv.TEST_IDENTITY["GIT_AUTHOR_EMAIL"]}>')
            # The child's own view of the world: the guard variables are gone.
            probe = ("import os; print(os.environ.get('SWARMLY_ROLE', 'unset'), "
                     "os.environ.get('GIT_CONFIG_COUNT', 'unset'))")
            out = subprocess.run([sys.executable, "-c", probe], env=gitenv.git_env(),
                                 capture_output=True, text=True)
            self.assertEqual(out.stdout.split(), ["unset", "unset"])


if __name__ == "__main__":
    unittest.main()
