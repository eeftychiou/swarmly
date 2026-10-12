"""Per-test ``os.environ`` isolation for unittest suites (issue #106).

``AgentConfig.from_dict(...)`` calls ``load_env()``, which merges ``Path.cwd()/.env``
(and friends) into the process-global ``os.environ``.  A test module that builds a
config therefore leaks the developer's real ``.env`` into every test that runs after
it; PR #104 immunized one victim (``tests/test_server_client.py``), this helper stops
the leak at the source, per test.

Attach it to a module with a two-line ``load_tests`` wrapper::

    from tests import envguard


    def load_tests(loader, tests, pattern):
        return envguard.guard(loader, tests, pattern)

unittest calls ``load_tests`` while loading a module, so every test in that module then
runs between an ``os.environ`` snapshot and an exact restore registered with
``addCleanup`` -- which also covers ``unittest.IsolatedAsyncioTestCase``.

There are no import-time side effects, and the guard is inert under pytest: pytest
does not call ``load_tests``, so only the (harmless) import happens during collection.
"""

from __future__ import annotations

import os
import types
import unittest

__all__ = ["guard", "wrap"]


def guard(loader, tests, pattern):
    """``load_tests`` hook: run every test in ``tests`` under an env snapshot/restore."""
    return wrap(tests)


def wrap(suite_or_test):
    """Recursively wrap a suite (or a single test) so each contained test restores ``os.environ``."""
    if isinstance(suite_or_test, unittest.TestSuite):
        suite_or_test._tests = [wrap(child) for child in suite_or_test._tests]
        return suite_or_test
    if isinstance(suite_or_test, unittest.TestCase) and not getattr(suite_or_test, "_envguard", False):
        return _guard_case(suite_or_test)
    return suite_or_test


def _guard_case(test):
    """Patch ``test.run`` so the environment the test ran in is restored after it."""
    original_run = test.run

    def guarded_run(self, result=None, *args, **kwargs):
        # Registered before the test machinery starts, so setup, the test body, teardown and
        # the test's own cleanups all run inside the snapshot -- and ours runs last (LIFO).
        self.addCleanup(_restore, dict(os.environ))
        return original_run(result, *args, **kwargs)

    test.run = types.MethodType(guarded_run, test)
    test._envguard = True

    return test


def _restore(snapshot):
    """Bring ``os.environ`` back to exactly ``snapshot``: drop added keys, restore changed ones."""
    for key in list(os.environ):
        if key not in snapshot:
            del os.environ[key]
    for key, value in snapshot.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
