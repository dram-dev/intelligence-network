"""Drive publisher against a fake Drive v3 service (no network, no OAuth)."""
from __future__ import annotations

import pytest

from intelnet import db
from intelnet.gdrive import DOC_MIME, FOLDER_MIME, KV_FOLDER, KV_LATEST, DriveNotConfigured, DrivePublisher


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
