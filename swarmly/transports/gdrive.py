"""Google Drive bus: envelope files in one Drive folder, through the Drive v3 API.

Use this when a node cannot run a sync client (a headless server, a container, CI). Nodes
that already have Google Drive for desktop can point a :class:`~swarmly.transports.folder.
FolderTransport` at the synced folder instead; both see the same files.

Install the extra: ``pip install "swarmly[gdrive]"``.

Credentials (``bus.gdrive.credentials``, else Application Default Credentials):

* an **authorized-user** JSON, e.g. from
  ``gcloud auth application-default login --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform``;
* a **service-account** key JSON. Service accounts have no storage quota of their own, so
  the folder must live on a **shared drive** the account is a member of.

Envelope files never change after they are written, so downloaded bodies are cached by
Drive file id (in memory, and on disk when ``cache_dir`` is set) and each poll only lists
the folder and fetches what is new.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarmly.transports.base import TransportError, checked_envelope_name, is_envelope_name

logger = logging.getLogger("swarmly.transports.gdrive")

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
#: Drive ids are URL-safe base64-ish; anything else is a typo (and must not reach the query).
_FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _missing_extra(exc: Exception) -> TransportError:
    return TransportError(
        "the Google Drive transport needs the gdrive extra: "
        f'pip install "swarmly[gdrive]" ({exc})')


def load_credentials(path: Optional[str] = None) -> Any:
    """Load Drive credentials from *path* (authorized-user or service-account JSON),
    or from Application Default Credentials when *path* is empty."""
    try:
        import google.auth
        from google.oauth2 import credentials as user_credentials
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise _missing_extra(exc)

    if not path:
        creds, _project = google.auth.default(scopes=SCOPES)
        return creds
    resolved = Path(os.path.expandvars(os.path.expanduser(path)))
    try:
        info = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransportError(f"cannot read Google credentials {resolved}: {exc}")
    kind = info.get("type")
    if kind == "service_account":
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if kind == "authorized_user":
        return user_credentials.Credentials.from_authorized_user_info(info, scopes=SCOPES)
    raise TransportError(f"{resolved}: unsupported credential type {kind!r} "
                         "(expected service_account or authorized_user)")


def build_drive_service(credentials_path: Optional[str] = None) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise _missing_extra(exc)
    creds = load_credentials(credentials_path)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _default_media(content: bytes) -> Any:
    try:
        from googleapiclient.http import MediaInMemoryUpload
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise _missing_extra(exc)
    return MediaInMemoryUpload(content, mimetype="text/markdown", resumable=False)


class GoogleDriveTransport:
    """Envelope files directly inside one Google Drive folder."""

    kind = "gdrive"

    def __init__(self, folder_id: str, *, credentials: Optional[str] = None,
                 cache_dir: Optional[Path] = None, service: Any = None,
                 media_factory: Optional[Callable[[bytes], Any]] = None):
        if not folder_id:
            raise TransportError("bus.gdrive.folder_id is required for the gdrive transport")
        if not _FOLDER_ID.match(folder_id):
            raise TransportError(f"bus.gdrive.folder_id {folder_id!r} is not a Drive folder id "
                                 "(copy the last part of the folder's URL)")
        self.folder_id = folder_id
        self.credentials = credentials
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._service = service
        self._media = media_factory or _default_media
        self._bodies: Dict[str, str] = {}

    def describe(self) -> str:
        return f"gdrive: folder {self.folder_id}"

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = build_drive_service(self.credentials)
        return self._service

    def refresh(self) -> None:
        """Nothing to do: every listing asks Drive directly."""

    # -- reading ---------------------------------------------------------------------
    def _list_files(self) -> List[Dict[str, Any]]:
        query = (f"'{self.folder_id}' in parents and trashed = false "
                 f"and mimeType != '{FOLDER_MIME}'")
        files: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            response = self.service.files().list(
                q=query, pageSize=1000, pageToken=token, orderBy="name",
                fields="nextPageToken, files(id, name)",
                supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            files.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return files

    def _cache_path(self, file_id: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.md"

    def _body(self, file_id: str, name: str) -> Optional[str]:
        if file_id in self._bodies:
            return self._bodies[file_id]
        cached = self._cache_path(file_id)
        if cached is not None and cached.is_file():
            try:
                text = cached.read_text(encoding="utf-8")
                self._bodies[file_id] = text
                return text
            except OSError:
                pass
        try:
            raw = self.service.files().get_media(fileId=file_id,
                                                 supportsAllDrives=True).execute()
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception:  # noqa: BLE001 - one bad file must not stop the others
            logger.warning("gdrive bus: could not download %s (%s); retrying next cycle",
                           name, file_id, exc_info=True)
            return None
        self._bodies[file_id] = text
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text, encoding="utf-8")
            except OSError:
                logger.warning("gdrive bus: cache write failed for %s", cached)
        return text

    def list_messages(self) -> List[Tuple[str, str]]:
        try:
            files = self._list_files()
        except TransportError:
            raise
        except Exception:  # noqa: BLE001 - offline or quota: an empty cycle, not a crash
            logger.warning("gdrive bus: listing folder %s failed", self.folder_id,
                           exc_info=True)
            return []
        items: List[Tuple[str, str]] = []
        for entry in sorted(files, key=lambda f: (f.get("name", ""), f.get("id", ""))):
            name = entry.get("name", "")
            if not is_envelope_name(name):
                continue
            body = self._body(entry["id"], name)
            if body is not None:
                items.append((f"gdrive:{self.folder_id}/{name}", body))
        return items

    # -- writing ---------------------------------------------------------------------
    def post(self, filename: str, content: str, *, publish: bool = True,
             summary: Optional[str] = None) -> str:
        checked_envelope_name(filename)
        media = self._media(content.encode("utf-8"))
        try:
            created = self.service.files().create(
                body={"name": filename, "parents": [self.folder_id]},
                media_body=media, fields="id", supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001 - surface as one error type
            raise TransportError(f"Google Drive upload of {filename} failed: {exc}") from exc
        file_id = created.get("id", "")
        if file_id:
            self._bodies[file_id] = content
        return f"gdrive:{self.folder_id}/{filename}"
