"""The ``a2a`` compatibility shim maps every old module path onto ``swarmly``.

The shim lives only in the private development checkout (it is left out of the package and
of the public tree), so these tests skip where it is absent.
"""

import subprocess
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless((ROOT / "a2a" / "__init__.py").exists(), "no a2a shim in this tree")
class TestRenameShim(unittest.TestCase):
    def test_old_paths_are_the_same_modules(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            import a2a.swarm.cli as old_cli
            import a2a.telegram.channel as old_channel
        import swarmly.swarm.cli
        import swarmly.telegram.channel
        self.assertIs(old_channel, swarmly.telegram.channel)
        self.assertIs(old_cli.main, swarmly.swarm.cli.main)

    def test_python_dash_m_runs_the_old_entry_point(self):
        proc = subprocess.run([sys.executable, "-m", "a2a.telegram.cli", "--help"], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bus-poll", proc.stdout)
        # Shown by default, so a launcher's log says it is using the old name.
        self.assertIn("renamed to swarmly", proc.stderr)

    def test_the_real_module_keeps_its_own_spec(self):
        # Its own spec is what importlib.reload() needs. (No reload here: it would replace
        # the module's classes under the other tests.)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            import a2a.transports.base  # noqa: F401
        import swarmly.transports.base as real
        self.assertEqual(real.__spec__.name, "swarmly.transports.base")


class TestOldCommandNames(unittest.TestCase):
    """The a2a-* console scripts say they were renamed, then run the new command."""

    def _run(self, fn_name):
        import contextlib
        import io
        from unittest import mock

        from swarmly import _aliases
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(sys, "argv", ["x", "--help"]):
            with self.assertRaises(SystemExit) as ctx:
                getattr(_aliases, fn_name)()
        return ctx.exception.code, err.getvalue()

    def test_each_alias_names_its_replacement_and_runs_it(self):
        for fn_name, new in (("a2a_telegram", "swarmly-telegram"), ("a2a", "swarmly"),
                             ("a2a_keygen", "swarmly-keygen")):
            with self.subTest(fn_name):
                code, err = self._run(fn_name)
                self.assertEqual(code, 0)
                self.assertIn(f"renamed to {new};", err)

    def test_the_package_declares_them(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for line in ('a2a-telegram = "swarmly._aliases:a2a_telegram"',
                     'a2a = "swarmly._aliases:a2a"',
                     'a2a-keygen = "swarmly._aliases:a2a_keygen"'):
            self.assertIn(line, text)
