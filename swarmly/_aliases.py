"""The command names from before the rename to Swarmly, kept for one release.

Each prints one line to stderr naming its replacement, then runs it unchanged, so a
launcher or scheduled task still using an old name keeps working and its log says so.
"""

import sys


def _note(old: str, new: str) -> None:
    print(f"{old}: renamed to {new}; this alias goes away in the next release",
          file=sys.stderr)


def a2a_telegram() -> None:
    _note("a2a-telegram", "swarmly-telegram")
    from swarmly.telegram.cli import main
    sys.exit(main())


def a2a() -> None:
    _note("a2a", "swarmly")
    from swarmly.swarm.cli import main
    sys.exit(main())


def a2a_keygen() -> None:
    _note("a2a-keygen", "swarmly-keygen")
    from swarmly.keygen import main
    sys.exit(main())
