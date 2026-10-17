"""The CI guard for issue #7 (brainstorm 2.4/2.5): tests start git through ``gitenv``.

Two things are checked, so the guard cannot go vacuous:

- ``tests/gitenv.py`` scrubs what a guarded host leaks into the environment, and keeps
  whatever the caller passes on top;
- no test module starts ``git`` with an inherited environment: every ``subprocess`` call
  whose argv is a ``git`` command passes ``env=`` built from an **unconditional** call to
  the helper (``env=git_env()``, ``env={**git_env(), ...}``, or a name assigned only from
  one of those), and that helper is the one imported from ``tests.gitenv``.

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
#: ``os`` launches a shell too, and takes a command line -- it can carry no ``env=``.
OS_SPAWNERS = {"system", "popen"}
SYSTEM_SPAWNERS = set(OS_SPAWNERS)


def _spawner_context(tree: ast.Module) -> dict:
    """The names that start a subprocess in this module: ``subprocess`` and its aliases.

    ``from subprocess import run as launch`` and ``launch = subprocess.run`` both hand the
    spawning function a new name, which the plain name set cannot see -- the reviewer's
    round-6 finding on PR #14.
    """
    spawners = set(SPAWNERS)
    modules = {"subprocess"}
    os_modules = {"os"}
    shadowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "subprocess":
            for alias in node.names:
                if alias.name in SPAWNERS:
                    spawners.add(alias.asname or alias.name)
                elif alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "os":
            for alias in node.names:
                if alias.name in OS_SPAWNERS:            # ``from os import system``
                    spawners.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "subprocess":
                    modules.add(alias.asname or alias.name.split(".")[0])
                elif alias.name == "os":
                    os_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
            owner = node.value.value
            if (node.value.attr in SPAWNERS and isinstance(owner, ast.Name)
                    and owner.id in modules):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        spawners.add(target.id)      # ``launch = subprocess.run``
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
            owner = node.value.value
            if (node.value.attr in OS_SPAWNERS and isinstance(owner, ast.Name)
                    and owner.id in os_modules):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        spawners.add(target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed.add(node.name)
    return {"spawners": (spawners - shadowed) | SYSTEM_SPAWNERS,
            "modules": (modules - shadowed) | (os_modules - shadowed)}


def _is_spawner_call(node: ast.Call, context: dict) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value
        if not isinstance(owner, ast.Name) or owner.id not in context["modules"]:
            return False
        return func.attr in SPAWNERS or func.attr in SYSTEM_SPAWNERS
    if isinstance(func, ast.Name):
        return func.id in context["spawners"]
    return False


def _argv_candidates(tree: ast.Module) -> dict:
    """Every list/tuple a name may hold as argv: assignments, and **loop targets**.

    ``for cmd in (["git", "status"], ["git", "push"]): subprocess.run(cmd)`` hands argv to a
    name that no assignment binds -- the form the reviewer found the scanner missing on
    PR #14 after the variable case was fixed. A loop over another name
    (``for cmd in commands:``) is resolved through that name's own candidate lists.
    """
    candidates: dict = {}

    def add(name: str, value) -> None:
        candidates.setdefault(name, []).append(value)

    for node in ast.walk(tree):                  # assignments first: loops may iterate them
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    add(target.id, node.value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        iterable = node.iter
        if isinstance(iterable, (ast.List, ast.Tuple)):
            for element in iterable.elts:
                if isinstance(element, (ast.List, ast.Tuple)):
                    add(node.target.id, element)
        elif isinstance(iterable, ast.Name):
            for holder in candidates.get(iterable.id, []):
                for element in getattr(holder, "elts", []):
                    if isinstance(element, (ast.List, ast.Tuple)):
                        add(node.target.id, element)
    return candidates


def _head_is_git(argv, constants: dict) -> bool:
    """Whether an argv literal starts with ``git`` (directly or through a string alias)."""
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return False
    first = argv.elts[0]
    head = first.value if isinstance(first, ast.Constant) else constants.get(
        getattr(first, "id", None))
    return isinstance(head, str) and Path(head).name in ("git", "git.exe")


def _string_is_git(node, constants: dict) -> bool:
    """Whether a *string* command (``shell=True``, or a program name) starts with ``git``.

    ``subprocess.run("git push origin main", shell=True)`` hands the shell a command line,
    not an argv list, so the list-shaped scan missed it -- the reviewer's round-6 finding.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words = node.value.split()
        return bool(words) and Path(words[0]).name in ("git", "git.exe")
    if isinstance(node, ast.JoinedStr) and node.values:      # f"git {args}"
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            words = first.value.split()
            return bool(words) and Path(words[0]).name in ("git", "git.exe")
        return False
    if isinstance(node, ast.Name):
        head = constants.get(node.id)
        if isinstance(head, str):
            words = head.split()
            return bool(words) and Path(words[0]).name in ("git", "git.exe")
    return False


def _git_commands(tree: ast.Module) -> list:
    """The ``subprocess``/``os`` calls in ``tree`` whose argv is a git command.

    argv is read from a literal list/tuple, from a module-level constant (``GIT = "git"``),
    from a variable that holds an argv list (``cmd = ["git", "push"]``), and from a loop
    target (``for cmd in (...): subprocess.run(cmd)``) -- the forms the review of PR #14
    found the scanner missing.
    """
    constants = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:          # module-level ``GIT = "git"`` style aliases
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    candidates = _argv_candidates(tree)
    spawner_context = _spawner_context(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_spawner_call(node, spawner_context):
            continue
        if not node.args:
            continue
        argv = node.args[0]
        if _string_is_git(argv, constants):
            found.append(node)
            continue
        options = candidates.get(argv.id, []) if isinstance(argv, ast.Name) else [argv]
        # Any candidate starting with git makes the call a git spawn: a name that may hold a
        # git argv must carry env=git_env(), even when another candidate is harmless.
        if any(_head_is_git(option, constants) for option in options):
            found.append(node)
    return found


def _helper_context(tree: ast.Module) -> dict:
    """What ``git_env`` can mean in this module, and whether it is used unconditionally.

    ``helpers`` are names imported from a ``gitenv`` module, ``modules`` are the imported
    ``gitenv`` module names, ``values`` maps a name to the expressions assigned to it, and
    ``shadowed`` holds every name the module binds some other way (a ``def``, a loop target,
    a ``with`` target, a tuple unpacking). A module may define its own ``git_env`` -- the
    reviewer's second bypass on PR #14 -- and a local name is not the helper.
    """
    helpers, modules, values, shadowed = set(), set(), {}, set()
    os_names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")
            for alias in node.names:
                if alias.name == "git_env" and module and module[-1] == "gitenv":
                    helpers.add(alias.asname or alias.name)
                elif alias.name == "gitenv":
                    modules.add(alias.asname or alias.name)
                elif alias.name in ("environ", "environb"):
                    os_names.add(alias.asname or alias.name)   # ``from os import environ``
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "gitenv":
                    modules.add(alias.asname or alias.name.split(".")[0])
                elif alias.name == "os":
                    os_names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values.setdefault(target.id, []).append(node.value)
                else:
                    shadowed.update(leaf.id for leaf in ast.walk(target)
                                    if isinstance(leaf, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                values.setdefault(node.target.id, []).append(node.value)
            else:
                shadowed.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            shadowed.update(leaf.id for leaf in ast.walk(node.target)
                            if isinstance(leaf, ast.Name))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    shadowed.update(leaf.id for leaf in ast.walk(item.optional_vars)
                                    if isinstance(leaf, ast.Name))
    return {"helpers": helpers, "modules": modules, "values": values, "shadowed": shadowed,
            "os_names": os_names}


def _is_helper_call(node: ast.AST, context: dict) -> bool:
    """A call to the *real* scrub helper, by its imported name or through its module."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        # Reassigning the imported name (``git_env = lambda: ...``) is a bypass too.
        return (func.id in context["helpers"] and func.id not in context["shadowed"]
                and func.id not in context["values"])
    if isinstance(func, ast.Attribute) and func.attr in ("git_env", "strip"):
        owner = func.value
        if isinstance(owner, ast.Name):
            return owner.id in context["modules"] and owner.id not in context["shadowed"]
        if isinstance(owner, ast.Attribute) and owner.attr == "gitenv":
            return True
    return False


def _carries_host_environment(node: ast.AST, context: dict, seen: frozenset = frozenset()) -> bool:
    """Whether ``node`` can bring the process environment (or a copy of it) back.

    ``env={**git_env(), **os.environ}`` and ``env=git_env() | os.environ`` splat the real
    environment over the scrubbed one, so ``SWARMLY_ROLE``, the ``GIT_CONFIG_*`` hooks and the
    guard ``PATH`` all return -- the reviewer's round-5 finding on PR #14, verified by
    reading ``SWARMLY_ROLE`` out of the child. An explicit single key
    (``{"PATH": os.environ["PATH"]}``) is *not* this: that is a key a test chose on purpose.
    """
    if isinstance(node, ast.Subscript):
        return False                       # one named key, chosen deliberately
    if isinstance(node, ast.Attribute):
        if node.attr in ("environ", "environb") and isinstance(node.value, ast.Name):
            return node.value.id in context["os_names"]
        return _carries_host_environment(node.value, context, seen)
    if isinstance(node, ast.Call):
        if _carries_host_environment(node.func, context, seen):
            return True
        return any(_carries_host_environment(arg, context, seen) for arg in node.args)
    if isinstance(node, ast.Name) and node.id not in seen:
        if node.id in ("environ", "environb"):
            return True
        return any(_carries_host_environment(value, context, seen | {node.id})
                   for value in context["values"].get(node.id, []))
    if isinstance(node, ast.Dict):
        return any(key is None and _carries_host_environment(value, context, seen)
                   for key, value in zip(node.keys, node.values))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_carries_host_environment(element, context, seen) for element in node.elts)
    if isinstance(node, ast.BinOp):
        return (_carries_host_environment(node.left, context, seen)
                or _carries_host_environment(node.right, context, seen))
    if isinstance(node, ast.BoolOp):
        return any(_carries_host_environment(value, context, seen) for value in node.values)
    if isinstance(node, ast.IfExp):
        return (_carries_host_environment(node.body, context, seen)
                or _carries_host_environment(node.orelse, context, seen))
    return False


def _is_scrubbed(node: ast.AST, context: dict, seen: frozenset = frozenset()) -> bool:
    """Whether ``node`` *unconditionally* produces a scrubbed environment.

    ``env=env or git_env()`` and ``env=pinned if flag else git_env()`` do not: the helper is
    only reached on one path, which is the reviewer's first finding of round 4. Accepted:
    the helper call itself, a dict that splats the helper first (``{**git_env(), "X": "1"}``),
    a ``|`` merge with the helper on either side, and a name whose every assignment in the
    module is one of those -- provided nothing splats or merges the host environment back in
    (``{**git_env(), **os.environ}`` is rejected, see ``_carries_host_environment``).
    """
    if _is_helper_call(node, context):
        return True
    if isinstance(node, ast.Dict) and node.keys and node.keys[0] is None:
        if not _is_scrubbed(node.values[0], context, seen):
            return False
        later = [value for key, value in zip(node.keys[1:], node.values[1:]) if key is None]
        return not any(_carries_host_environment(value, context, seen) for value in later)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        helper = _is_helper_call(node.left, context) or _is_helper_call(node.right, context)
        return helper and not (_carries_host_environment(node.left, context, seen)
                               or _carries_host_environment(node.right, context, seen))
    if isinstance(node, ast.Name) and node.id not in seen:
        if node.id in context["shadowed"] or not context["values"].get(node.id):
            return False
        return all(_is_scrubbed(value, context, seen | {node.id})
                   for value in context["values"][node.id])
    return False


def _env_is_scrubbed(call: ast.Call, context: dict) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "env" and _is_scrubbed(keyword.value, context):
            return True
    return False


def violations(source: str, where: str = "<source>") -> list:
    """Human-readable lines for every git spawn in ``source`` that bypasses ``git_env``."""
    tree = ast.parse(source)
    context = _helper_context(tree)
    return [f"{where}:{call.lineno}: git starts without env=git_env()"
            for call in _git_commands(tree) if not _env_is_scrubbed(call, context)]


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

    def test_the_scanner_sees_an_argv_variable(self):
        """The reviewer's finding on PR #14: ``cmd = ["git", ...]`` then ``run(cmd)``."""
        bad = ("import subprocess\n"
               "def go(repo):\n"
               "    cmd = ['git', '-C', repo, 'push', 'origin', 'main']\n"
               "    return subprocess.run(cmd, check=True, capture_output=True)\n")
        self.assertEqual(len(violations(bad, "varargv.py")), 1, violations(bad))
        good = ("import subprocess\nfrom tests.gitenv import git_env\n"
                "def go(repo):\n"
                "    cmd = ['git', '-C', repo, 'push', 'origin', 'main']\n"
                "    return subprocess.run(cmd, env=git_env())\n")
        self.assertEqual(violations(good, "varargv_good.py"), [])
        # A variable that is not an argv list is not a git spawn either way.
        other = ("import subprocess\n"
                 "cmd = ['python', '-V']\n"
                 "subprocess.run(cmd)\n")
        self.assertEqual(violations(other, "other.py"), [])

    def test_the_scanner_sees_a_loop_target(self):
        """The reviewer's second finding on PR #14: argv bound by a ``for`` statement."""
        bad = ("import subprocess\n"
               "for cmd in (['git', 'status'], ['git', 'push']):\n"
               "    subprocess.run(cmd, check=True)\n")
        self.assertEqual(len(violations(bad, "loop.py")), 1, violations(bad))
        good = ("import subprocess\nfrom tests.gitenv import git_env\n"
                "for cmd in (['git', 'status'], ['git', 'push']):\n"
                "    subprocess.run(cmd, env=git_env())\n")
        self.assertEqual(violations(good, "loop_good.py"), [])
        # ... and through a name that holds the argv lists.
        via_name = ("import subprocess\n"
                    "commands = [['git', 'log'], ['python', '-V']]\n"
                    "for cmd in commands:\n"
                    "    subprocess.run(cmd)\n")
        self.assertEqual(len(violations(via_name, "loop_name.py")), 1,
                         violations(via_name))
        # A loop over harmless argvs is not a git spawn: no false positive.
        harmless = ("import subprocess\n"
                    "for cmd in (['python', '-V'], ['ls']):\n"
                    "    subprocess.run(cmd)\n")
        self.assertEqual(violations(harmless, "harmless.py"), [])

    def test_mentioning_the_helper_is_not_calling_it(self):
        """The reviewer's third finding on PR #14: the name in a string is not a scrub."""
        bypass = ("import os\nimport subprocess\n"
                  "subprocess.run(['git', 'push'], env={**os.environ, 'NOTE': 'git_env'})\n")
        self.assertEqual(len(violations(bypass, "bypass.py")), 1, violations(bypass))
        # A local variable merely named like the helper is not a call either.
        shadowed = ("import os\nimport subprocess\n"
                    "git_env = dict(os.environ)\n"
                    "subprocess.run(['git', 'push'], env=git_env)\n")
        self.assertEqual(len(violations(shadowed, "shadowed.py")), 1, violations(shadowed))

    def test_a_real_helper_call_in_any_shape_counts(self):
        shapes = {
            "plain": "subprocess.run(['git', 'push'], env=git_env())\n",
            "module": "subprocess.run(['git', 'push'], env=gitenv.git_env())\n",
            "strip": "subprocess.run(['git', 'push'], env=gitenv.strip())\n",
            "unpacked": "subprocess.run(['git', 'push'], env={**git_env(), 'X': '1'})\n",
            "merged": "subprocess.run(['git', 'push'], env=git_env() | {'X': '1'})\n",
            "named": "cmd_env = git_env()\nsubprocess.run(['git', 'push'], env=cmd_env)\n",
        }
        for name, line in shapes.items():
            with self.subTest(shape=name):
                source = ("import os\nimport subprocess\n"
                          "from tests import gitenv\n"
                          "from tests.gitenv import git_env\n"
                          "env = None\nEXTRA = {}\n" + line)
                self.assertEqual(violations(source, f"{name}.py"), [], name)

    def test_a_conditional_helper_call_does_not_count(self):
        """Round 4: ``env=env or git_env()`` skips the scrub whenever ``env`` is truthy."""
        conditional = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                       "env = os.environ\n"
                       "subprocess.run(['git', 'push'], env=env or git_env())\n")
        self.assertEqual(len(violations(conditional, "or_env.py")), 1,
                         violations(conditional))
        ternary = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                   "flag = True\nenv = os.environ\n"
                   "subprocess.run(['git', 'push'], env=git_env() if flag else env)\n")
        self.assertEqual(len(violations(ternary, "ternary.py")), 1, violations(ternary))
        # Layering onto the scrubbed base with the helper splatted first stays correct:
        # the helper always runs, the overlay only adds keys.
        layered = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                   "extra = {'X': '1'}\n"
                   "subprocess.run(['git', 'push'], env={**git_env(), **extra})\n")
        self.assertEqual(violations(layered, "layered.py"), [])

    def test_a_local_helper_of_the_same_name_does_not_count(self):
        """Round 4: a module may define ``git_env`` itself and hand back ``os.environ``."""
        local = ("import os\nimport subprocess\n"
                 "from tests.gitenv import git_env\n"
                 "def git_env():\n"
                 "    return dict(os.environ)\n"
                 "subprocess.run(['git', 'push'], env=git_env())\n")
        self.assertEqual(len(violations(local, "local.py")), 1, violations(local))
        # Rebinding the imported name to something else is the same bypass.
        rebound = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                   "git_env = lambda: dict(os.environ)\n"
                   "subprocess.run(['git', 'push'], env=git_env())\n")
        self.assertEqual(len(violations(rebound, "rebound.py")), 1, violations(rebound))
        # A name assigned both ways is not reliably scrubbed either.
        mixed = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                 "env = git_env()\n"
                 "if os.name:\n"
                 "    env = dict(os.environ)\n"
                 "subprocess.run(['git', 'push'], env=env)\n")
        self.assertEqual(len(violations(mixed, "mixed.py")), 1, violations(mixed))

    def test_the_host_environment_cannot_be_splatted_back_in(self):
        """Round 5: an overlay that restores ``os.environ`` undoes the whole scrub."""
        splat = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                 "subprocess.run(['git', 'push'], env={**git_env(), **os.environ})\n")
        self.assertEqual(len(violations(splat, "splat.py")), 1, violations(splat))
        merged = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                  "subprocess.run(['git', 'push'], env=git_env() | os.environ)\n")
        self.assertEqual(len(violations(merged, "merged.py")), 1, violations(merged))
        copied = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                  "subprocess.run(['git', 'push'], env={**git_env(), **dict(os.environ)})\n")
        self.assertEqual(len(violations(copied, "copied.py")), 1, violations(copied))
        via_name = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                    "host = os.environ.copy()\n"
                    "subprocess.run(['git', 'push'], env={**git_env(), **host})\n")
        self.assertEqual(len(violations(via_name, "via_name.py")), 1, violations(via_name))
        named = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                 "child = {**git_env(), **os.environ}\n"
                 "subprocess.run(['git', 'push'], env=child)\n")
        self.assertEqual(len(violations(named, "named_env.py")), 1, violations(named))

    def test_a_chosen_overlay_is_still_allowed(self):
        """A literal overlay, or one explicit host key, is a decision a test made."""
        literal = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                   "extra = {'X': '1'}\n"
                   "subprocess.run(['git', 'push'], env={**git_env(), **extra})\n")
        self.assertEqual(violations(literal, "literal.py"), [])
        one_key = ("import os\nimport subprocess\nfrom tests.gitenv import git_env\n"
                   "subprocess.run(['git', 'push'],\n"
                   "               env={**git_env(), 'PATH': os.environ['PATH']})\n")
        self.assertEqual(violations(one_key, "one_key.py"), [])

    def test_an_aliased_spawner_is_still_a_spawner(self):
        """Round 6: ``launch = subprocess.run`` hides the spawn behind a new name."""
        aliased = ("import subprocess\n"
                   "launch = subprocess.run\n"
                   "launch(['git', 'push'])\n")
        self.assertEqual(len(violations(aliased, "alias.py")), 1, violations(aliased))
        imported = ("from subprocess import run as launch\n"
                    "launch(['git', 'push'])\n")
        self.assertEqual(len(violations(imported, "from_import.py")), 1,
                         violations(imported))
        module = ("import subprocess as sp\n"
                  "sp.run(['git', 'push'])\n")
        self.assertEqual(len(violations(module, "module_alias.py")), 1, violations(module))
        # The scrubbed form of each stays acceptable.
        for name, line in (("alias", "launch = subprocess.run\nlaunch(['git', 'push'], "
                                     "env=git_env())\n"),
                           ("from_import", "launch(['git', 'push'], env=git_env())\n")):
            with self.subTest(shape=name):
                head = ("import subprocess\nfrom tests.gitenv import git_env\n"
                        if name == "alias" else
                        "from subprocess import run as launch\n"
                        "from tests.gitenv import git_env\n")
                self.assertEqual(violations(head + line, f"{name}_ok.py"), [])

    def test_a_string_command_is_a_spawn_too(self):
        """Round 6: ``shell=True`` takes a command line, not an argv list."""
        shell = ("import subprocess\n"
                 "subprocess.run('git push origin main', shell=True)\n")
        self.assertEqual(len(violations(shell, "shell.py")), 1, violations(shell))
        fstring = ("import subprocess\n"
                   "branch = 'main'\n"
                   "subprocess.run(f'git push origin {branch}', shell=True)\n")
        self.assertEqual(len(violations(fstring, "fstring.py")), 1, violations(fstring))
        constant = ("import subprocess\n"
                    "PUSH = 'git push origin main'\n"
                    "subprocess.run(PUSH, shell=True)\n")
        self.assertEqual(len(violations(constant, "constant.py")), 1, violations(constant))
        scrubbed = ("import subprocess\nfrom tests.gitenv import git_env\n"
                    "subprocess.run('git push origin main', shell=True, env=git_env())\n")
        self.assertEqual(violations(scrubbed, "shell_ok.py"), [])
        # A different program on a command line is not a git spawn.
        other = ("import subprocess\n"
                 "subprocess.run('echo git push', shell=True)\n"
                 "subprocess.run('gh pr list', shell=True)\n")
        self.assertEqual(violations(other, "other_shell.py"), [])

    def test_os_system_is_a_spawn_too(self):
        """``os.system`` launches a shell as well, and takes no ``env=`` at all."""
        shell = "import os\nos.system('git push origin main')\n"
        self.assertEqual(len(violations(shell, "os_system.py")), 1, violations(shell))
        aliased = "import os as operating_system\noperating_system.system('git status')\n"
        self.assertEqual(len(violations(aliased, "os_alias.py")), 1, violations(aliased))
        harmless = "import os\nos.system('echo hi')\nos.popen('ls')\n"
        self.assertEqual(violations(harmless, "os_harmless.py"), [])


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

    def test_a_per_alias_guard_directory_leaves_path(self):
        """The live transport names it ``guard-<alias>``; that is the one that must go."""
        with TemporaryDirectoryPath() as folder:
            guard = folder / "guard-deepseek"
            guard.mkdir()
            (guard / "gh").write_text("#!/bin/sh\n", encoding="utf-8")
            (guard / "git").write_text("#!/bin/sh\n", encoding="utf-8")
            path = os.pathsep.join([str(guard), "/usr/bin"])
            cleaned = gitenv.git_env({"PATH": path})["PATH"]
            self.assertNotIn(str(guard), cleaned)
            self.assertIn("/usr/bin", cleaned)

    def test_a_directory_named_like_a_guard_without_a_shim_stays(self):
        """Only a directory that actually holds a shim is a guard; a name alone is not."""
        with TemporaryDirectoryPath() as folder:
            lookalike = folder / "guard-fixtures"
            lookalike.mkdir()
            path = os.pathsep.join([str(lookalike), "/usr/bin"])
            self.assertIn(str(lookalike), gitenv.git_env({"PATH": path})["PATH"])


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
