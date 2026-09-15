"""Daily pipeline: stage wiring under the cross-digest lock, and the digest ping."""
from __future__ import annotations

import os
import threading

import pytest
from digest_core import runlock

from intelnet import db, pipeline


@pytest.fixture
def stubbed(monkeypatch, tmp_path, fresh_db):
    monkeypatch.setenv("PIPELINE_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(runlock, "_POLL_SEC", 0.05)
    calls: list[str] = []

    from intelnet import watch

    monkeypatch.setattr(watch, "run_once", lambda run_type="watch", only=None: calls.append("watch") or {"feeds": {}, "events_closed": 0})
    monkeypatch.setattr(pipeline, "discover_ingestors", lambda pkg: {"news": object})
    monkeypatch.setattr(pipeline, "run_ingest", lambda *a, **k: calls.append("news") or (3, 2))
    from intelnet import export

    monkeypatch.setattr(export, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(export, "FRAGMENT", tmp_path / "no-fragment.html")
    return calls


def test_pipeline_runs_stages_and_records_digest(stubbed, monkeypatch, tmp_path):
    from intelnet import llm

    monkeypatch.setattr(llm, "narrative", lambda payload: "The network was quiet.")
    summary = pipeline.run("daily", skip_publish=True)
    assert stubbed == ["watch", "news"]
    assert summary["news"] == {"fetched": 3, "new": 2} and summary["digest"]["date"]
    assert summary["links"]["doc_url"] is None
    assert summary["export"] == {"json": 10, "site": False} and (tmp_path / "docs" / "data" / "network.json").exists()
    row = db.latest_digest()
    assert row["date"] == summary["digest"]["date"] and row["drive_url"] is None
    with db.get_conn() as conn:
        assert conn.execute("SELECT run_type FROM run_log WHERE source='pipeline'").fetchone()["run_type"] == "daily"


def test_pipeline_publishes_when_drive_enabled(stubbed, monkeypatch):
    from intelnet import gdrive
    from intelnet.config import settings

    monkeypatch.setattr(settings, "gdrive_enabled", True)
    published = {}

    class Pub:
        def publish(self, date, html):
            published["date"], published["html"] = date, html
            return {"doc_id": "d", "doc_url": "https://docs.google.com/document/d/d/edit",
                    "latest_url": "https://docs.google.com/document/d/l/edit",
                    "folder_url": "https://drive.google.com/drive/folders/f"}

        def sync_readers(self):
            return 0

    monkeypatch.setattr(gdrive, "publisher", Pub())
    summary = pipeline.run("daily")
    assert published["date"] == summary["digest"]["date"] and "<h1>" in published["html"]
    assert db.latest_digest()["drive_url"].endswith("/d/edit")


def test_pipeline_waits_for_the_lock_and_gives_up_when_wedged(stubbed, monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_LOCK_TIMEOUT_SEC", "0.3")
    hold = threading.Event()
    release = threading.Event()

    def holder():
        with runlock.pipeline_serialize("macro-ai-digest"):
            hold.set()
            release.wait(5)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    hold.wait(2)
    waits: list[str] = []
    with pytest.raises(runlock.PipelineLockTimeout) as exc:
        pipeline.run("daily", skip_publish=True, on_wait=waits.append)
    assert "macro-ai-digest" in str(exc.value) and waits and "macro-ai-digest" in waits[0]
    assert stubbed == []                      # never ran the stages
    release.set()
    t.join(2)
    assert os.path.exists(str(tmp_path / "lock"))


def test_notify_digest_respects_quiet_hours(fresh_db, sent, monkeypatch):
    assert pipeline.notify_digest()["reason"] == "no digest"
    db.record_digest("2026-09-15", drive_file_id="d", drive_url="https://docs.google.com/document/d/d/edit",
                     latest_url=None, folder_url=None, n_events=1, n_signals=2, n_sensors=1)
    db.add_subscription("5", "weather.digest", "il")
    monkeypatch.setattr(pipeline, "subscriptions_allowed_now", lambda: False)
    assert pipeline.notify_digest()["reason"] == "quiet hours"
    assert pipeline.notify_digest(force=True)["sent"] == 1 and sent[0][0] == "5"
    monkeypatch.setattr(pipeline, "subscriptions_allowed_now", lambda: True)
    assert pipeline.notify_digest()["sent"] == 0             # already delivered (notify_log)


def test_triage_without_llm_keeps_items_unranked(fresh_db):
    from intelnet.ingest.base import IngestedItem

    db.upsert_items([IngestedItem(source="news", source_id="a", title="T", url=None, content="c")])
    counts = pipeline.triage_news(24, pipeline._Quiet())
    assert counts == {"pending": 1, "kept": 0, "dropped": 0, "untriaged": 1}
    assert db.kept_items_since(1)[0]["triage_reason"].startswith("untriaged")
