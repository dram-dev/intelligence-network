"""Google Drive publisher — the digest's home, and how people subscribe to it.

The digest is a Google Doc in a Drive folder the app creates on first run:

    <GDRIVE_FOLDER_NAME>/
        Latest — <network> digest        (fixed link; overwritten every day)
        2026-09-15 <network> digest      (one per day, kept)

"Subscribing" to the digest means having access to that folder — either
through the anyone-with-link setting (GDRIVE_PUBLIC_LINK) or by being added
as a reader (`/digest you@example.com` on Telegram → `add_reader`, which
makes Google send the share e-mail). The Telegram `digest` category pings
subscribers with the day's link when it lands.

OAuth is the Desktop-app flow, scope `drive.file` (only files this app
creates). All Drive calls go through `_svc()` so tests inject a fake.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from intelnet import db
from intelnet.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
KV_FOLDER = "gdrive_folder_id"
KV_LATEST = "gdrive_latest_id"


class DriveNotConfigured(RuntimeError):
    """No credentials / token available (or GDRIVE_ENABLED=false)."""


def _get_credentials(interactive: bool = False):
    """Load (and refresh) the saved token. Only `interactive` may open a browser.

    The nightly pipeline and the bot call this non-interactively: a missing or
    dead token raises DriveNotConfigured instead of starting a consent flow that
    would block a headless process — and the shared cross-digest run lock — forever.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(settings.gdrive_token_path)
    creds_path = Path(settings.gdrive_credentials_path)
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except ValueError:
            creds = None          # unreadable token → treat as not authorized
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            if not interactive:
                raise DriveNotConfigured(
                    "Google authorization expired or was revoked — run `uv run intelnet drive init`"
                ) from exc
            creds = None
    if not (creds and creds.valid):
        if not creds_path.exists():
            raise DriveNotConfigured(
                f"Google OAuth client not found at {creds_path} — see secrets/README.md"
            )
        if not interactive:
            raise DriveNotConfigured(
                "Google Drive is not authorized yet — run `uv run intelnet drive init`"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        # prompt=consent guarantees a refresh token even on a re-authorization.
        creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    return creds


class DrivePublisher:
    def __init__(self, service: Any = None) -> None:
        self._service = service

    # ── plumbing ──────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return settings.gdrive_enabled

    def _svc(self, interactive: bool = False):
        if self._service is None:
            if not self.enabled:
                raise DriveNotConfigured("GDRIVE_ENABLED=false")
            from googleapiclient.discovery import build

            creds = _get_credentials(interactive)
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def authorize(self) -> None:
        """Browser consent on first use — only `intelnet drive init` calls this."""
        self._svc(interactive=True)

    def _exists(self, file_id: str) -> bool:
        """Is a stored folder/doc id still usable (not deleted, trashed, or another account's)?"""
        try:
            meta = self._svc().files().get(fileId=file_id, fields="id,trashed").execute()
        except Exception as exc:  # noqa: BLE001 — googleapiclient HttpError carries .resp.status
            if getattr(getattr(exc, "resp", None), "status", None) in (403, 404):
                return False
            raise
        return not meta.get("trashed")

    @property
    def configured(self) -> bool:
        return self._service is not None or (
            self.enabled and (settings.gdrive_token_path.exists() or settings.gdrive_credentials_path.exists())
        )

    @staticmethod
    def folder_url(folder_id: str | None) -> str | None:
        return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else None

    @staticmethod
    def doc_url(file_id: str | None) -> str | None:
        return f"https://docs.google.com/document/d/{file_id}/edit" if file_id else None

    # ── folder + latest doc ───────────────────────────────────────────────
    def ensure_folder(self) -> str:
        fid = db.kv_get(KV_FOLDER)
        if fid and self._exists(fid):
            return fid
        svc = self._svc()
        created = svc.files().create(
            body={"name": settings.gdrive_folder_name, "mimeType": FOLDER_MIME}, fields="id"
        ).execute()
        fid = created["id"]
        db.kv_set(KV_FOLDER, fid)
        if settings.gdrive_public_link:
            self.set_public(fid)
        logger.info("gdrive: created folder %s (%s)", settings.gdrive_folder_name, fid)
        return fid

    def ensure_latest_doc(self, folder_id: str) -> str:
        lid = db.kv_get(KV_LATEST)
        if lid and self._exists(lid):
            return lid
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._svc()
        body = {"name": f"Latest — {settings.network_name} digest", "mimeType": DOC_MIME,
                "parents": [folder_id]}
        media = MediaInMemoryUpload(b"<p>No digest published yet.</p>", mimetype="text/html")
        created = svc.files().create(body=body, media_body=media, fields="id").execute()
        lid = created["id"]
        db.kv_set(KV_LATEST, lid)
        return lid

    def set_public(self, file_id: str) -> None:
        self._svc().permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}, fields="id"
        ).execute()

    # ── publishing ────────────────────────────────────────────────────────
    def publish(self, date: str, html: str) -> dict[str, str | None]:
        """Create the day's doc and refresh the Latest doc. Returns links."""
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._svc()
        folder_id = self.ensure_folder()
        latest_id = self.ensure_latest_doc(folder_id)
        name = f"{date} {settings.network_name} digest"
        media = MediaInMemoryUpload(html.encode("utf-8"), mimetype="text/html")
        existing = svc.files().list(
            q=f"name = '{name}' and '{folder_id}' in parents and trashed = false",
            fields="files(id)", pageSize=1,
        ).execute().get("files") or []
        if existing:
            doc_id = existing[0]["id"]
            svc.files().update(fileId=doc_id, media_body=media).execute()
        else:
            doc_id = svc.files().create(
                body={"name": name, "mimeType": DOC_MIME, "parents": [folder_id]},
                media_body=media, fields="id",
            ).execute()["id"]
        svc.files().update(
            fileId=latest_id,
            media_body=MediaInMemoryUpload(html.encode("utf-8"), mimetype="text/html"),
        ).execute()
        return {
            "doc_id": doc_id, "doc_url": self.doc_url(doc_id),
            "latest_url": self.doc_url(latest_id), "folder_url": self.folder_url(folder_id),
        }

    # ── subscribers ───────────────────────────────────────────────────────
    def add_reader(self, email: str) -> bool:
        """Share the folder with an e-mail (Google sends the invitation)."""
        try:
            folder_id = self.ensure_folder()
            self._svc().permissions().create(
                fileId=folder_id,
                body={"type": "user", "role": "reader", "emailAddress": email},
                sendNotificationEmail=True, fields="id",
            ).execute()
            return True
        except DriveNotConfigured:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdrive: share with %s failed: %s", email, exc)
            return False

    def sync_readers(self) -> int:
        """Share the folder with every recorded e-mail subscriber (idempotent-ish)."""
        n = 0
        for email in db.email_subscribers():
            if self.add_reader(email):
                n += 1
        return n

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "credentials": settings.gdrive_credentials_path.exists(),
            "token": settings.gdrive_token_path.exists(),
            "folder_id": db.kv_get(KV_FOLDER),
            "folder_url": self.folder_url(db.kv_get(KV_FOLDER)),
            "latest_url": self.doc_url(db.kv_get(KV_LATEST)),
            "email_subscribers": len(db.email_subscribers()),
        }


publisher = DrivePublisher()
