"""pytest entry points for the suite's shared guards (issue #7, brainstorm 2.4/2.5).

``tests/envguard.py`` was written as a ``load_tests`` hook, so it only ever ran under
``python -m unittest``; under ``pytest`` nothing restored ``os.environ`` between tests and a
test that merged a ``.env`` leaked it into every test after it.  The autouse fixture below
gives pytest the same per-test snapshot and exact restore.

The guard environment (``GIT_CONFIG_*``, ``SWARMLY_ROLE``, the guard directory on ``PATH``,
``GIT_AUTHOR_*``) is scrubbed once, when the ``tests`` package is imported -- see
``tests/__init__.py`` and ``tests/gitenv.py``.
"""

from __future__ import annotations

import pytest

from tests import envguard


@pytest.fixture(autouse=True)
def _restore_environment_after_each_test():
    """Run every test between an ``os.environ`` snapshot and an exact restore."""
    before = envguard.snapshot()
    try:
        yield
    finally:
        envguard.restore(before)
