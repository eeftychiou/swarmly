"""One event loop for the synchronous tests' ``run(coro)``.

``asyncio.run`` builds and closes a new event loop on every call. On Windows each loop opens
a loopback TCP pair for its self-pipe, and every closed pair leaves a port in TIME_WAIT for up
to two minutes. A full suite made about 1,200 loops, so one run on a bot's machine held about
1,200 ephemeral ports, and a few runs close together pushed the machine toward its limit.

``run`` keeps one loop for the whole test process and, like ``asyncio.run``, cancels whatever
the coroutine left running before it returns, so no task leaks from one test into the next.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")

_loop: Optional[asyncio.AbstractEventLoop] = None


def shared_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def run(coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run(coro)`` on the shared loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError("run() cannot be called from a running event loop")
    loop = shared_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        _cancel_leftovers(loop)
        asyncio.set_event_loop(None)        # as asyncio.run leaves it: no current loop


def _cancel_leftovers(loop: asyncio.AbstractEventLoop) -> None:
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    for task in tasks:
        if not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler({
                "message": "unhandled exception in a task left running by a test",
                "exception": task.exception(),
                "task": task,
            })
