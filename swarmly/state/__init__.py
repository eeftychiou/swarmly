"""The swarm's shared state: the topic registry, the lease and the owed table, in one store.

See ``docs/design/topic-registry.md``. ``records`` holds the data dictionary, ``keys`` who may
write what, ``log`` the fold every bot runs the same way, and ``store`` / ``telegram_store``
the backends (memory, a folder, Telegram itself).
"""

from swarmly.state.log import Lease, Op, StateLog
from swarmly.state.store import DirStore, MemoryLog, MemoryStore, StateStore, StoreError, WriteResult

__all__ = ["DirStore", "Lease", "MemoryLog", "MemoryStore", "Op", "StateLog", "StateStore",
           "StoreError", "WriteResult"]
