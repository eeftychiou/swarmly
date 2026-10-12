"""Durable-plane transports: where envelope files are stored and read back.

``git`` (gitbus, the default), ``folder`` (any synced or shared directory: Google Drive
for desktop, Dropbox, OneDrive, Syncthing, a network share) and ``gdrive`` (the Google
Drive API, for nodes without a sync client). All three carry the same envelope files, so
the bus, the poller, signatures and seen-ledgers work identically on each.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from swarmly.transports.base import BusTransport, TransportError, is_envelope_name
from swarmly.transports.folder import FolderTransport
from swarmly.transports.git import BusPushError, GitTransport

TRANSPORT_KINDS = ("git", "folder", "gdrive")

__all__ = [
    "BusPushError",
    "BusTransport",
    "FolderTransport",
    "GitTransport",
    "TRANSPORT_KINDS",
    "TransportError",
    "build_transport",
    "is_envelope_name",
]


def build_transport(kind: Optional[str], *, repo: Path, ref: Optional[str] = None,
                    folder: Optional[str] = None,
                    gdrive: Optional[Mapping[str, Any]] = None,
                    state_dir: Optional[Path] = None,
                    identity: Optional[Tuple[str, str]] = None) -> BusTransport:
    """Construct the transport named by *kind* (``None`` means ``git``).

    *repo* is the git checkout for the ``git`` transport; *state_dir* is where a
    transport may keep a local cache (never inside a synced folder).
    """
    name = (kind or "git").strip().lower()
    if name == "git":
        return GitTransport(Path(repo), ref=ref, identity=identity)
    if name == "folder":
        if not folder:
            raise TransportError("bus.folder is required for the folder transport")
        return FolderTransport(Path(folder))
    if name == "gdrive":
        from swarmly.transports.gdrive import GoogleDriveTransport
        options = dict(gdrive or {})
        folder_id = str(options.get("folder_id") or "").strip()
        cache_dir = None
        if state_dir is not None and folder_id:
            cache_dir = Path(state_dir) / f"gdrive-cache-{folder_id}"
        return GoogleDriveTransport(folder_id, credentials=options.get("credentials") or None,
                                    cache_dir=cache_dir)
    raise TransportError(f"unknown bus transport {kind!r}; known: {', '.join(TRANSPORT_KINDS)}")
