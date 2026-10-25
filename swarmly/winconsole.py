"""No console windows flashing up on Windows when swarmly runs without a console.

A bot run by a scheduled task through ``pythonw`` has no console of its own, so Windows gives
every console program it starts -- git, gh, the agent's CLI, and each shell command that CLI
runs for its tools -- a console window of its own, which flashes up and closes (admin, 25 Oct:
"I keep seeing cmd prompts popping up"). Started with ``CREATE_NO_WINDOW``, a child gets a
console with no window instead, and whatever it starts in turn shares that console, so nothing
is ever painted. A process that has a console (a terminal, or a hidden one) is left alone:
its children share it and show nothing either.
"""

from __future__ import annotations

import functools
import os
import subprocess
from typing import Any, Callable

#: Windows process creation flags (``subprocess`` defines them only on Windows).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
_MARK = "_swarmly_no_window"


def no_window(init: Callable[..., None]) -> Callable[..., None]:
    """``Popen.__init__`` that starts a child with no console window, unless the caller
    asked for a console of its own (``CREATE_NEW_CONSOLE``) or none (``DETACHED_PROCESS``)."""
    @functools.wraps(init)
    def wrapped(self: Any, *args: Any, creationflags: int = 0, **kwargs: Any) -> None:
        if not creationflags & (CREATE_NEW_CONSOLE | DETACHED_PROCESS):
            creationflags |= CREATE_NO_WINDOW
        init(self, *args, creationflags=creationflags, **kwargs)
    setattr(wrapped, _MARK, True)
    return wrapped


def _has_console() -> bool:
    import ctypes
    return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]


def hide_child_consoles(system: str = os.name,
                        has_console: Callable[[], bool] = _has_console) -> bool:
    """Start every child process of this one without a console window, when this process
    runs on Windows with no console of its own. Returns whether it did; safe to call twice.
    ``subprocess.Popen`` is what ``asyncio``'s subprocesses run on as well, so this covers
    both."""
    if system != "nt" or has_console():
        return False
    if not getattr(subprocess.Popen.__init__, _MARK, False):
        subprocess.Popen.__init__ = no_window(subprocess.Popen.__init__)  # type: ignore[method-assign]
    return True
