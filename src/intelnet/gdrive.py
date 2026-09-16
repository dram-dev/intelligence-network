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

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from intelnet import db
from intelnet.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
HTML_MIME = "text/html"
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CSV_MIME = "text/csv"
KV_FOLDER = "gdrive_folder_id"
KV_LATEST = "gdrive_latest_id"


class DriveNotConfigured(RuntimeError):
    """No credentials / token available (or GDRIVE_ENABLED=false)."""


class DriveWrongAccount(DriveNotConfigured):
    """Authorized as a different Google account than GDRIVE_ACCOUNT."""

    def __init__(self, actual: str | None, expected: str) -> None:
        self.actual, self.expected = actual, expected
        super().__init__(
            f"Google Drive is authorized as {actual or 'an unknown account'}, not {expected} — "
            f"sign in again and pick {expected}: `uv run intelnet drive init --remote`"
        )


# Show the account chooser (and pre-select GDRIVE_ACCOUNT) instead of silently
# reusing whichever Google account the browser happens to be signed in to.
PROMPT = "select_account consent"


def _account_hint() -> dict[str, str]:
    acct = settings.gdrive_account.strip()
    return {"login_hint": acct} if acct else {}


def verify_account(service: Any) -> str | None:
    """The authorized account's e-mail; raises DriveWrongAccount if GDRIVE_ACCOUNT differs."""
    about = service.about().get(fields="user(emailAddress)").execute()
    actual = ((about.get("user") or {}).get("emailAddress") or "").strip()
    expected = settings.gdrive_account.strip()
    if expected and actual.lower() != expected.lower():
        raise DriveWrongAccount(actual or None, expected)
    return actual or None


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
            detail = str(exc).lower()
            if "disabled_client" in detail or "account" in detail and ("disabled" in detail or "deleted" in detail):
                # Re-authorizing can't fix this: Google shut the client or the account down.
                raise DriveNotConfigured(
                    "Google has disabled this app's OAuth client or its Google account — restore the "
                    "account (or set up a new client) before Drive publishing can resume"
                ) from exc
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
        # prompt includes consent, which guarantees a refresh token on re-authorization.
        creds = flow.run_local_server(port=0, prompt=PROMPT, **_account_hint())
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    return creds


# ── sign-in from another device (phone) ────────────────────────────────────
# The normal `drive init` opens a browser on this machine. On a headless Mac mini
# you can instead open the Google link on your phone: after you approve, Google
# redirects to a localhost address that won't load on the phone — its URL carries
# a single-use code, which `drive init --code '<that URL>'` exchanges here. The
# PKCE verifier never leaves this machine.

REMOTE_REDIRECT = "http://localhost:8765/"
PENDING_NAME = "gdrive_pending.json"


def _pending_path() -> Path:
    return Path(settings.gdrive_token_path).with_name(PENDING_NAME)


def _write_token(creds: Any) -> None:
    token_path = Path(settings.gdrive_token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)


def begin_remote_authorization() -> str:
    """Start a sign-in to finish on another device. Returns the link to open there."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = Path(settings.gdrive_credentials_path)
    if not creds_path.exists():
        raise DriveNotConfigured(f"Google OAuth client not found at {creds_path} — see secrets/README.md")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES, redirect_uri=REMOTE_REDIRECT)
    url, state = flow.authorization_url(access_type="offline", prompt=PROMPT, **_account_hint())
    pending = _pending_path()
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"state": state, "code_verifier": flow.code_verifier,
                                   "redirect_uri": REMOTE_REDIRECT}))
    pending.chmod(0o600)
    return url


def complete_remote_authorization(response: str) -> Any:
    """Finish a remote sign-in from the redirected URL (or the bare code). Writes the token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    pending_path = _pending_path()
    if not pending_path.exists():
        raise DriveNotConfigured("No sign-in in progress — start one with `uv run intelnet drive init --remote`")
    pending = json.loads(pending_path.read_text())
    text = response.strip().strip("'\"")
    code = text
    if "://" in text or "code=" in text or "error=" in text:
        query = parse_qs(urlparse(text if "://" in text else "http://x/?" + text.split("?", 1)[-1]).query)
        if query.get("error"):
            raise DriveNotConfigured(f"Google declined the sign-in: {query['error'][0]}")
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if state and state != pending["state"]:
            raise DriveNotConfigured("That link belongs to a different sign-in attempt — start again with --remote")
    if not code:
        raise DriveNotConfigured("No authorization code in what was pasted — copy the whole address-bar URL")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.gdrive_credentials_path), SCOPES, redirect_uri=pending["redirect_uri"],
        code_verifier=pending["code_verifier"], autogenerate_code_verifier=False,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    _write_token(creds)
    pending_path.unlink(missing_ok=True)
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
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
            if settings.gdrive_account.strip():
                verify_account(service)       # never publish from the wrong Google account
            self._service = service
        return self._service

    def account(self) -> str | None:
        """E-mail of the Google account Drive is authorized as."""
        return verify_account(self._svc())

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

    @staticmethod
    def file_url(file_id: str | None) -> str | None:
        return f"https://drive.google.com/file/d/{file_id}/view" if file_id else None

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

    # ── files ─────────────────────────────────────────────────────────────
    def _find(self, name: str, parent_id: str) -> str | None:
        """Id of a file with this exact name in this folder, if there is one."""
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        found = self._svc().files().list(
            q=f"name = '{safe}' and '{parent_id}' in parents and trashed = false",
            fields="files(id)", pageSize=1,
        ).execute().get("files") or []
        return found[0]["id"] if found else None

    def ensure_day_folder(self, folder_id: str, date: str) -> str:
        """One folder per day, holding every format of that day's digest."""
        fid = self._find(date, folder_id)
        if fid:
            return fid
        return self._svc().files().create(
            body={"name": date, "mimeType": FOLDER_MIME, "parents": [folder_id]}, fields="id",
        ).execute()["id"]

    def _upload(self, name: str, parent_id: str, data: bytes, mimetype: str,
                *, convert_to: str | None = None) -> str:
        """Write a file by name, replacing its contents if it is already there.

        Re-publishing a day therefore refreshes files in place, so links people
        already have keep working.
        """
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._svc()
        media = MediaInMemoryUpload(data, mimetype=mimetype)
        fid = self._find(name, parent_id)
        if fid:
            svc.files().update(fileId=fid, media_body=media).execute()
            return fid
        body: dict[str, Any] = {"name": name, "parents": [parent_id]}
        if convert_to:                      # ask Drive to convert on the way in (HTML → Google Doc)
            body["mimeType"] = convert_to
        return svc.files().create(body=body, media_body=media, fields="id").execute()["id"]

    def _export(self, doc_id: str, mimetype: str) -> bytes:
        """Google's own rendering of a Doc — PDF and Word come from here."""
        return self._svc().files().export_media(fileId=doc_id, mimeType=mimetype).execute()

    def _adopt(self, name: str, from_id: str, to_id: str) -> str | None:
        """Move a digest written before day folders existed into its day folder."""
        fid = self._find(name, from_id)
        if fid:
            self._svc().files().update(
                fileId=fid, addParents=to_id, removeParents=from_id, fields="id",
            ).execute()
        return fid

    # ── publishing ────────────────────────────────────────────────────────
    def publish(self, date: str, html: str, tables: dict[str, str] | None = None,
                rerender: Callable[[dict[str, str]], str] | None = None) -> dict[str, str | None]:
        """Publish the day's digest in every format, and refresh the Latest doc.

        The day gets a folder of its own holding the Google Doc, a PDF, a Word
        file, the page as HTML and one CSV per table: the document is for
        reading, the rest is for keeping, printing or loading into a spreadsheet.

        A file can't link to itself before it exists, so the digest is written
        twice when `rerender` is given: once to create the files, then again with
        a "also available as" bar naming them. The second pass replaces contents
        in place, so every link — including ones already sent out — still works.
        Returns the links the digest record and the Telegram ping use.
        """
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._svc()
        folder_id = self.ensure_folder()
        latest_id = self.ensure_latest_doc(folder_id)
        day_id = self.ensure_day_folder(folder_id, date)
        name = f"{date} {settings.network_name} digest"
        data = html.encode("utf-8")

        doc_id = self._find(name, day_id) or self._adopt(name, folder_id, day_id)
        if doc_id:
            svc.files().update(fileId=doc_id, media_body=MediaInMemoryUpload(data, mimetype=HTML_MIME)).execute()
        else:
            doc_id = svc.files().create(
                body={"name": name, "mimeType": DOC_MIME, "parents": [day_id]},
                media_body=MediaInMemoryUpload(data, mimetype=HTML_MIME), fields="id",
            ).execute()["id"]

        written = {"doc": doc_id}
        for suffix, mime in (("pdf", PDF_MIME), ("docx", DOCX_MIME)):
            try:
                written[suffix] = self._upload(f"{name}.{suffix}", day_id, self._export(doc_id, mime), mime)
            except Exception as exc:  # noqa: BLE001 — a missing format must not lose the digest
                logger.warning("gdrive: %s export failed for %s: %s", suffix, date, exc)
        written["html"] = self._upload(f"{name}.html", day_id, data, HTML_MIME)
        for table, text in (tables or {}).items():
            written[table] = self._upload(f"{date} {table}.csv", day_id, text.encode("utf-8"), CSV_MIME)

        downloads = {label: url for label, url in (
            ("PDF", self.file_url(written.get("pdf"))),
            ("Word", self.file_url(written.get("docx"))),
            ("HTML", self.file_url(written.get("html"))),
            ("CSV tables", self.folder_url(day_id) if tables else None),
        ) if url}
        if rerender is not None:
            data = rerender(downloads).encode("utf-8")
            svc.files().update(fileId=doc_id,
                               media_body=MediaInMemoryUpload(data, mimetype=HTML_MIME)).execute()
            self._upload(f"{name}.html", day_id, data, HTML_MIME)
            for suffix, mime in (("pdf", PDF_MIME), ("docx", DOCX_MIME)):
                if suffix in written:                  # re-export so the PDF carries the bar too
                    try:
                        self._upload(f"{name}.{suffix}", day_id, self._export(doc_id, mime), mime)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("gdrive: %s re-export failed for %s: %s", suffix, date, exc)

        svc.files().update(
            fileId=latest_id, media_body=MediaInMemoryUpload(data, mimetype=HTML_MIME),
        ).execute()
        logger.info("gdrive: published %s in %d files", date, len(written))
        return {
            "doc_id": doc_id, "doc_url": self.doc_url(doc_id),
            "latest_url": self.doc_url(latest_id), "folder_url": self.folder_url(folder_id),
            "day_url": self.folder_url(day_id), "formats": ", ".join(sorted(written)),
            "downloads": downloads,
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

    def remove_reader(self, email: str) -> bool:
        """Take an e-mail address off the digest folder (best-effort)."""
        folder_id = db.kv_get(KV_FOLDER)
        if not folder_id:
            return False
        try:
            svc = self._svc()
            perms = svc.permissions().list(
                fileId=folder_id, fields="permissions(id,emailAddress,role)"
            ).execute().get("permissions", [])
            hits = [p for p in perms if (p.get("emailAddress") or "").lower() == email.strip().lower()
                    and p.get("role") != "owner"]
            for perm in hits:
                svc.permissions().delete(fileId=folder_id, permissionId=perm["id"]).execute()
            return bool(hits)
        except DriveNotConfigured:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdrive: removing %s failed: %s", email, exc)
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
