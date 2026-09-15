"""Drive publisher against a fake Drive v3 service (no network, no OAuth)."""
from __future__ import annotations

import pytest

from intelnet import db
from intelnet.gdrive import DOC_MIME, FOLDER_MIME, KV_FOLDER, KV_LATEST, DriveNotConfigured, DrivePublisher


class _NotFound(Exception):
    class resp:              # mimics googleapiclient.errors.HttpError.resp
        status = 404


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeFiles:
    def __init__(self, svc):
        self.svc = svc

    def create(self, body=None, media_body=None, fields=None):
        fid = f"id{len(self.svc.files_created) + 1}"
        self.svc.files_created.append({"id": fid, **body, "content": _content(media_body)})
        return _Call({"id": fid})

    def get(self, fileId=None, fields=None):
        hit = next((f for f in self.svc.files_created if f["id"] == fileId), None)
        if hit is None:
            raise _NotFound()
        return _Call({"id": fileId, "trashed": hit.get("trashed", False)})

    def update(self, fileId=None, media_body=None):
        self.svc.updates.append((fileId, _content(media_body)))
        return _Call({"id": fileId})

    def list(self, q=None, fields=None, pageSize=None):
        name = q.split("'")[1]
        hits = [{"id": f["id"]} for f in self.svc.files_created if f.get("name") == name]
        return _Call({"files": hits})


class FakePerms:
    def __init__(self, svc):
        self.svc = svc

    def create(self, fileId=None, body=None, sendNotificationEmail=None, fields=None):
        self.svc.perms.append((fileId, body, sendNotificationEmail))
        return _Call({"id": "p"})


class FakeService:
    def __init__(self):
        self.files_created, self.updates, self.perms = [], [], []

    def files(self):
        return FakeFiles(self)

    def permissions(self):
        return FakePerms(self)


def _content(media):
    if media is None:
        return None
    return media.getbytes(0, media.size()).decode()      # MediaIoBaseUpload public API


def test_publish_creates_folder_latest_and_daily_doc(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "gdrive_enabled", True)
    monkeypatch.setattr(settings, "gdrive_public_link", True)
    svc = FakeService()
    pub = DrivePublisher(service=svc)
    links = pub.publish("2026-09-15", "<h1>hi</h1>")
    folder, latest, daily = svc.files_created
    assert folder["mimeType"] == FOLDER_MIME and folder["name"] == settings.gdrive_folder_name
    assert latest["mimeType"] == DOC_MIME and latest["parents"] == [folder["id"]]
    assert daily["name"] == "2026-09-15 Intelligence Network digest" and daily["content"] == "<h1>hi</h1>"
    assert db.kv_get(KV_FOLDER) == folder["id"] and db.kv_get(KV_LATEST) == latest["id"]
    assert svc.perms[0][1] == {"type": "anyone", "role": "reader"}          # public link
    assert svc.updates == [(latest["id"], "<h1>hi</h1>")]                    # Latest refreshed
    assert links["doc_url"].endswith(f"/{daily['id']}/edit") and links["folder_url"].endswith(folder["id"])

    # re-publishing the same day updates the doc in place
    pub.publish("2026-09-15", "<h1>v2</h1>")
    assert len(svc.files_created) == 3 and (daily["id"], "<h1>v2</h1>") in svc.updates

    assert pub.add_reader("a@b.co") and svc.perms[-1][1]["emailAddress"] == "a@b.co"
    db.add_email_subscriber("c@d.co", None)
    assert pub.sync_readers() == 1
    st = pub.status()
    assert st["folder_id"] == folder["id"] and st["email_subscribers"] == 1


def test_disabled_publisher_raises_not_configured(fresh_db):
    pub = DrivePublisher()
    with pytest.raises(DriveNotConfigured):
        pub.ensure_folder()
    assert pub.add_reader("x@y.z") is False
    assert pub.configured is False


def test_missing_or_trashed_folder_is_recreated(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "gdrive_enabled", True)
    svc = FakeService()
    pub = DrivePublisher(service=svc)
    pub.publish("2026-09-15", "<p>a</p>")
    first = db.kv_get(KV_FOLDER)
    svc.files_created[0]["trashed"] = True        # someone deleted the folder in Drive
    svc.files_created[1]["trashed"] = True        # …and the Latest doc with it
    pub.publish("2026-09-16", "<p>b</p>")
    assert db.kv_get(KV_FOLDER) != first and db.kv_get(KV_LATEST) != svc.files_created[1]["id"]
    db.kv_set(KV_FOLDER, "id-from-another-account")
    assert pub.ensure_folder() != "id-from-another-account"


def _oauth_paths(tmp_path, monkeypatch, *, client=True):
    from intelnet.config import settings

    creds, token = tmp_path / "client.json", tmp_path / "token.json"
    if client:
        creds.write_text('{"installed": {"client_id": "c", "client_secret": "s", '
                         '"auth_uri": "https://accounts.google.com/o/oauth2/auth", '
                         '"token_uri": "https://oauth2.googleapis.com/token", '
                         '"redirect_uris": ["http://localhost"]}}')
    monkeypatch.setattr(settings, "gdrive_credentials_path", creds)
    monkeypatch.setattr(settings, "gdrive_token_path", token)
    return creds, token


def test_headless_calls_never_start_a_browser_flow(tmp_path, monkeypatch):
    import google_auth_oauthlib.flow as oauth_flow

    from intelnet import gdrive

    def _boom(*a, **k):
        raise AssertionError("a browser consent flow was started from a headless call")

    monkeypatch.setattr(oauth_flow.InstalledAppFlow, "from_client_secrets_file", classmethod(_boom))
    _oauth_paths(tmp_path, monkeypatch, client=False)
    with pytest.raises(gdrive.DriveNotConfigured, match="secrets/README.md"):
        gdrive._get_credentials()
    _oauth_paths(tmp_path, monkeypatch)
    with pytest.raises(gdrive.DriveNotConfigured, match="drive init"):
        gdrive._get_credentials()


def test_expired_token_is_reported_not_prompted(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2 import credentials as gcreds

    from intelnet import gdrive

    _, token = _oauth_paths(tmp_path, monkeypatch)
    token.write_text('{"token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s", '
                     '"token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"}')

    def _dead(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(gcreds.Credentials, "refresh", _dead)
    with pytest.raises(gdrive.DriveNotConfigured, match="expired or was revoked"):
        gdrive._get_credentials()


def test_interactive_authorization_writes_a_private_token(tmp_path, monkeypatch):
    import google_auth_oauthlib.flow as oauth_flow

    from intelnet import gdrive

    _, token = _oauth_paths(tmp_path, monkeypatch)
    seen = {}

    class FakeCreds:
        valid = True

        def to_json(self):
            return '{"token": "t", "refresh_token": "r"}'

    class FakeFlow:
        def run_local_server(self, **kw):
            seen.update(kw)
            return FakeCreds()

    monkeypatch.setattr(oauth_flow.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, path, scopes: FakeFlow()))
    gdrive._get_credentials(interactive=True)
    assert token.exists() and oct(token.stat().st_mode)[-3:] == "600"
    assert seen == {"port": 0, "prompt": "consent"}


def test_remote_sign_in_round_trip(tmp_path, monkeypatch):
    import json as _json

    import google_auth_oauthlib.flow as oauth_flow

    from intelnet import gdrive

    _, token = _oauth_paths(tmp_path, monkeypatch)
    made = []

    class FakeCreds:
        def to_json(self):
            return '{"token": "t", "refresh_token": "r"}'

    class FakeFlow:
        def __init__(self, **kw):
            self.kw, self.code_verifier, self.fetched = kw, kw.get("code_verifier") or "verifier-123", None

        def authorization_url(self, **kw):
            self.auth_kw = kw
            return "https://accounts.google.com/o/oauth2/auth?client_id=c&state=st8", "st8"

        def fetch_token(self, **kw):
            self.fetched = kw

        @property
        def credentials(self):
            return FakeCreds()

    def _factory(cls, path, scopes, **kw):
        made.append(FakeFlow(**kw))
        return made[-1]

    monkeypatch.setattr(oauth_flow.InstalledAppFlow, "from_client_secrets_file", classmethod(_factory))
    url = gdrive.begin_remote_authorization()
    assert url.startswith("https://accounts.google.com/") and made[0].auth_kw["prompt"] == "consent"
    pending = gdrive._pending_path()
    assert _json.loads(pending.read_text()) == {"state": "st8", "code_verifier": "verifier-123",
                                                "redirect_uri": gdrive.REMOTE_REDIRECT}
    assert oct(pending.stat().st_mode)[-3:] == "600"

    with pytest.raises(gdrive.DriveNotConfigured, match="different sign-in"):
        gdrive.complete_remote_authorization("http://localhost:8765/?state=other&code=abc")
    with pytest.raises(gdrive.DriveNotConfigured, match="declined"):
        gdrive.complete_remote_authorization("http://localhost:8765/?error=access_denied&state=st8")

    gdrive.complete_remote_authorization("'http://localhost:8765/?state=st8&code=4/0AbC&scope=x'")
    assert made[-1].fetched == {"code": "4/0AbC"} and made[-1].kw["code_verifier"] == "verifier-123"
    assert token.exists() and oct(token.stat().st_mode)[-3:] == "600" and not pending.exists()
    with pytest.raises(gdrive.DriveNotConfigured, match="No sign-in in progress"):
        gdrive.complete_remote_authorization("4/0AbC")
