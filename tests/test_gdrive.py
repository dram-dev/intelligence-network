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

    def update(self, fileId=None, media_body=None, addParents=None, removeParents=None, fields=None):
        self.svc.updates.append((fileId, _content(media_body)))
        hit = next((f for f in self.svc.files_created if f["id"] == fileId), None)
        if hit is not None:
            if media_body is not None:
                hit["content"] = _content(media_body)
            if addParents:
                hit["parents"] = [p for p in hit.get("parents", []) if p != removeParents] + [addParents]
        return _Call({"id": fileId})

    def list(self, q=None, fields=None, pageSize=None):
        name = q.split("'")[1]
        parent = q.split("'")[3] if q.count("'") >= 4 else None
        hits = [{"id": f["id"]} for f in self.svc.files_created
                if f.get("name") == name and (parent is None or parent in (f.get("parents") or []))]
        return _Call({"files": hits})

    def export_media(self, fileId=None, mimeType=None):
        self.svc.exports.append((fileId, mimeType))
        return _Call(f"{mimeType} bytes for {fileId}".encode())


class FakePerms:
    def __init__(self, svc):
        self.svc = svc

    def create(self, fileId=None, body=None, sendNotificationEmail=None, fields=None):
        self.svc.perms.append((fileId, body, sendNotificationEmail))
        return _Call({"id": f"p{len(self.svc.perms)}"})

    def list(self, fileId=None, fields=None):
        return _Call({"permissions": [
            {"id": f"p{i + 1}", "emailAddress": body.get("emailAddress"), "role": body.get("role")}
            for i, (fid, body, _n) in enumerate(self.svc.perms) if fid == fileId and body is not None]})

    def delete(self, fileId=None, permissionId=None):
        idx = int(permissionId[1:]) - 1
        self.svc.perms[idx] = (self.svc.perms[idx][0], None, None)
        return _Call({})


class FakeService:
    def __init__(self):
        self.files_created, self.updates, self.perms, self.exports = [], [], [], []

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
    by_name = {f["name"]: f for f in svc.files_created}
    folder = by_name[settings.gdrive_folder_name]
    latest = next(f for f in svc.files_created if f["name"].startswith("Latest"))
    daily = by_name["2026-09-15 Intelligence Network digest"]
    assert folder["mimeType"] == FOLDER_MIME
    assert latest["mimeType"] == DOC_MIME and latest["parents"] == [folder["id"]]
    assert daily["content"] == "<h1>hi</h1>" and daily["parents"] == [by_name["2026-09-15"]["id"]]
    assert db.kv_get(KV_FOLDER) == folder["id"] and db.kv_get(KV_LATEST) == latest["id"]
    assert svc.perms[0][1] == {"type": "anyone", "role": "reader"}          # public link
    assert (latest["id"], "<h1>hi</h1>") in svc.updates                      # Latest refreshed
    assert links["doc_url"].endswith(f"/{daily['id']}/edit") and links["folder_url"].endswith(folder["id"])

    # re-publishing the same day updates the files in place
    count = len(svc.files_created)
    pub.publish("2026-09-15", "<h1>v2</h1>")
    assert len(svc.files_created) == count and (daily["id"], "<h1>v2</h1>") in svc.updates

    assert pub.add_reader("a@b.co") and svc.perms[-1][1]["emailAddress"] == "a@b.co"
    assert pub.remove_reader("A@B.co") and svc.perms[-1][1] is None           # case-insensitive
    assert pub.remove_reader("nobody@b.co") is False
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
    assert seen == {"port": 0, "prompt": "select_account consent"}


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
    assert url.startswith("https://accounts.google.com/") and made[0].auth_kw["prompt"] == "select_account consent"
    assert "login_hint" not in made[0].auth_kw
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


def test_account_pinning(tmp_path, monkeypatch):
    import google_auth_oauthlib.flow as oauth_flow

    from intelnet import gdrive
    from intelnet.config import settings

    class About:
        def __init__(self, email):
            self.email = email

        def get(self, fields=None):
            return _Call({"user": {"emailAddress": self.email}})

    class Svc:
        def __init__(self, email):
            self._about = About(email)

        def about(self):
            return self._about

    monkeypatch.setattr(settings, "gdrive_account", "")
    assert gdrive.verify_account(Svc("someone@gmail.com")) == "someone@gmail.com"     # not pinned: anything goes
    monkeypatch.setattr(settings, "gdrive_account", "ilintelligencenetwork@gmail.com")
    assert gdrive.verify_account(Svc("ILIntelligenceNetwork@gmail.com")) == "ILIntelligenceNetwork@gmail.com"
    with pytest.raises(gdrive.DriveWrongAccount) as exc:
        gdrive.verify_account(Svc("someone.else@gmail.com"))
    assert exc.value.actual == "someone.else@gmail.com" and "pick ilintelligencenetwork@gmail.com" in str(exc.value)
    assert isinstance(exc.value, gdrive.DriveNotConfigured)                         # pipeline treats it as not configured

    # sign-in links pre-select the pinned account
    _oauth_paths(tmp_path, monkeypatch)
    captured = {}

    class Flow:
        code_verifier = "v"

        def authorization_url(self, **kw):
            captured.update(kw)
            return "https://accounts.google.com/o/oauth2/auth?x", "s"

    monkeypatch.setattr(oauth_flow.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, path, scopes, **kw: Flow()))
    gdrive.begin_remote_authorization()
    assert captured["login_hint"] == "ilintelligencenetwork@gmail.com" and captured["prompt"] == "select_account consent"


def test_disabled_client_is_reported_as_such(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2 import credentials as gcreds

    from intelnet import gdrive

    _, token = _oauth_paths(tmp_path, monkeypatch)
    token.write_text('{"token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s", '
                     '"token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"}')

    def _disabled(self, request):
        raise RefreshError("disabled_client: The OAuth client was disabled.",
                           {"error": "disabled_client", "error_description": "The OAuth client was disabled."})

    monkeypatch.setattr(gcreds.Credentials, "refresh", _disabled)
    for interactive in (False, True):
        with pytest.raises(gdrive.DriveNotConfigured, match="has disabled this app"):
            gdrive._get_credentials(interactive=interactive)


def _published(svc):
    return {f["name"]: f for f in svc.files_created}


def test_publish_writes_every_format_into_a_folder_for_the_day(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "gdrive_enabled", True)
    monkeypatch.setattr(settings, "network_name", "Intelligence Network")
    svc = FakeService()
    pub = DrivePublisher(service=svc)
    links = pub.publish("2026-09-16", "<h1>digest</h1>",
                        tables={"events": "a,b\n1,2\n", "network-vitals": "metric,value\n"})

    files = _published(svc)
    day = files["2026-09-16"]
    assert day["mimeType"] == FOLDER_MIME                       # a folder per day
    name = "2026-09-16 Intelligence Network digest"
    for expected, mime in ((name, DOC_MIME), (f"{name}.pdf", None), (f"{name}.docx", None),
                           (f"{name}.html", None), ("2026-09-16 events.csv", None),
                           ("2026-09-16 network-vitals.csv", None)):
        assert expected in files, expected
        assert files[expected]["parents"] == [day["id"]]
        if mime:
            assert files[expected]["mimeType"] == mime
    assert files[f"{name}.pdf"]["content"].startswith("application/pdf bytes")
    assert [m for _f, m in svc.exports] == ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    assert links["day_url"].endswith(day["id"])
    assert "pdf" in links["formats"] and "events" in links["formats"]


def test_republishing_a_day_updates_the_same_files(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "gdrive_enabled", True)
    svc = FakeService()
    pub = DrivePublisher(service=svc)
    pub.publish("2026-09-16", "<h1>one</h1>", tables={"events": "a\n"})
    before = len(svc.files_created)
    pub.publish("2026-09-16", "<h1>two</h1>", tables={"events": "b\n"})
    assert len(svc.files_created) == before                      # nothing duplicated
    assert _published(svc)[f"2026-09-16 {settings.network_name} digest.html"]["content"] == "<h1>two</h1>"


def test_a_digest_written_before_day_folders_is_moved_into_one(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "gdrive_enabled", True)
    svc = FakeService()
    pub = DrivePublisher(service=svc)
    folder_id = pub.ensure_folder()
    name = f"2026-09-15 {settings.network_name} digest"
    svc.files_created.append({"id": "old-doc", "name": name, "mimeType": DOC_MIME,
                              "parents": [folder_id], "content": "<h1>old</h1>"})
    links = pub.publish("2026-09-15", "<h1>new</h1>")
    assert links["doc_id"] == "old-doc"                           # same file, same link
    day_id = _published(svc)["2026-09-15"]["id"]
    assert _published(svc)[name]["parents"] == [day_id]
