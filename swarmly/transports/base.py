"""The durable-plane transport contract.

A transport stores envelope files and lists them back. It knows nothing about envelopes,
signatures, addressing or seen-ledgers: those stay in :mod:`swarmly.bus` and the poller, so
every transport gets the same delivery semantics for free.

Envelope files are immutable and uniquely named (``<message_id>_<sender>_<intent>.md``),
which is what lets a plain synced folder or a cloud drive act as a bus: there is nothing
to merge, only new files to notice.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

try:  # Python 3.8+: typing.Protocol
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment,misc]


class TransportError(RuntimeError):
    """A transport could not complete a read or a write."""


class BusTransport(Protocol):
    """Where envelope files live. Implementations: git, folder, gdrive."""

    #: Short name used in config (``bus.transport``) and logs.
    kind: str

    def refresh(self) -> None:
        """Bring the local view up to date (best-effort; must not raise)."""

    def list_messages(self) -> List[Tuple[str, str]]:
        """Return ``(name, text)`` for every envelope file, sorted by name.

        A file that cannot be read yet (still syncing, truncated) is skipped with a
        warning rather than failing the whole listing; it is picked up next time.
        """

    def post(self, filename: str, content: str, *, publish: bool = True,
             summary: Optional[str] = None) -> str:
        """Store one envelope file and return where it landed.

        ``publish`` asks the transport to make the file visible to other nodes now
        (for git: commit and push). ``summary`` is a one-line description of the
        message, used where the transport keeps a log (the git commit message).
        """

    def describe(self) -> str:
        """One line for ``check`` output and logs."""


def checked_envelope_name(name: str) -> str:
    """*name* when it is a bare envelope file name; raises :class:`TransportError` for a
    path (review P0-7: a ``../`` in a message id must not write outside the inbox)."""
    if (not isinstance(name, str) or not is_envelope_name(name) or "/" in name
            or "\\" in name or ".." in name or ":" in name
            or any(ord(c) < 32 for c in name)):
        raise TransportError(f"not an envelope file name: {name!r}")
    return name


def _check_inbox(root: Path, inbox_dir: Path) -> None:
    """Raise unless every directory from *root* down to *inbox_dir* is a real directory
    (or not created yet) inside *root*: a symlinked ``.agents`` or ``inbox`` committed to
    the bus must not redirect a write outside it (review P0-7)."""
    try:
        relative = inbox_dir.relative_to(root)
    except ValueError:
        raise TransportError(f"inbox {inbox_dir} is not inside {root}") from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TransportError(f"refusing to write through the symlink {current}")
    if inbox_dir.exists():
        resolved_root = root.resolve()
        resolved = inbox_dir.resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise TransportError(f"inbox {inbox_dir} resolves outside {root}")


def write_envelope(root: Path, inbox_dir: Path, filename: str, content: str) -> Path:
    """Write one envelope file into *inbox_dir* and return its path.

    The name must be bare (:func:`checked_envelope_name`), no directory on the way may be a
    symlink, and an existing symlink at the target is refused rather than followed. The
    content goes to a temporary file first and is moved into place in one step, so a reader
    never sees half an envelope. An existing envelope is never replaced: the same content
    again is a no-op, different content is a TransportError (a second message must not
    silently erase the first).
    """
    name = checked_envelope_name(filename)
    root, inbox_dir = Path(root), Path(inbox_dir)
    _check_inbox(root, inbox_dir)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    _check_inbox(root, inbox_dir)
    target = inbox_dir / name
    if target.is_symlink():
        raise TransportError(f"refusing to write through the symlink {target}")
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(inbox_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _place_without_clobber(tmp_name, target, content)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _place_without_clobber(tmp_name: str, target: Path, content: str) -> None:
    """Move *tmp_name* to *target* unless *target* already exists."""
    try:
        os.link(tmp_name, target)                 # atomic, and fails if target exists
    except FileExistsError:
        pass
    except OSError:
        # No hard links on this file system: check, then move (a narrow race, still
        # far better than always overwriting).
        if not os.path.lexists(target):
            os.replace(tmp_name, target)
            return
    else:
        os.unlink(tmp_name)
        return
    try:
        same = target.read_text(encoding="utf-8") == content
    except OSError:
        same = False
    if not same:
        raise TransportError(f"an envelope named {target.name} already exists; not replaced")
    os.unlink(tmp_name)


def is_envelope_name(name: str) -> bool:
    """True for an envelope file name; false for temp files, dotfiles and other clutter."""
    base = name.rsplit("/", 1)[-1]
    return base.endswith(".md") and not base.startswith((".", "~"))
