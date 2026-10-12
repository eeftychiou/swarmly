"""Folder bus: envelope files in a directory that something else keeps in sync.

Point it at a folder synced by Google Drive for desktop, Dropbox, OneDrive, iCloud Drive,
Syncthing or an SMB/NFS share, and every node that sees the folder shares one bus. No git,
no account on any API, no ports.

Writes are atomic (temp file + rename) so a sync client never uploads half an envelope,
and readers skip temp files and anything that does not parse yet; the poller simply sees
it on the next cycle. Sync clients that keep both copies of a conflict ("name (1).md")
are harmless: both carry the same ``message_id`` and the seen-ledger delivers it once.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from swarmly.transports.base import TransportError, is_envelope_name, write_envelope

logger = logging.getLogger("swarmly.transports.folder")


class FolderTransport:
    """Envelope files in ``<path>/inbox``."""

    kind = "folder"

    def __init__(self, path: Path, *, create: bool = False):
        self.path = Path(path).expanduser()
        self.inbox_dir = self.path / "inbox"
        if create:
            self.inbox_dir.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        return f"folder: {self.inbox_dir}"

    def refresh(self) -> None:
        """Nothing to do: the sync client keeps the folder current."""

    def list_messages(self) -> List[Tuple[str, str]]:
        if not self.inbox_dir.is_dir():
            logger.warning("folder bus: %s does not exist (is the sync client running?)",
                           self.inbox_dir)
            return []
        items: List[Tuple[str, str]] = []
        for path in sorted(self.inbox_dir.iterdir()):
            if not path.is_file() or not is_envelope_name(path.name):
                continue
            try:
                items.append((str(path), path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                # Cloud placeholders and half-synced files land here; retry next cycle.
                logger.warning("folder bus: %s is not readable yet; skipping this cycle", path)
        return items

    def post(self, filename: str, content: str, *, publish: bool = True,
             summary: Optional[str] = None) -> str:
        if not self.path.is_dir():
            raise TransportError(f"bus folder {self.path} does not exist")
        target = write_envelope(self.path, self.inbox_dir, filename, content)
        return str(target)
