"""Shared fixtures — the suite is hermetic.

Every test gets a throwaway SQLite file; Telegram, Google Drive, the LLMs and
online geo lookups are all forced off, so nothing reaches the network even
when the developer `.env` is fully configured.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intelnet import db, geo
from intelnet.config import settings
from intelnet.models import KIND_HUMAN, Sensor

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "geo_online_lookup", False)
    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(settings, "gdrive_enabled", False)
    monkeypatch.setattr(settings, "notify_enabled", False)
    monkeypatch.setattr(settings, "network_join_code", "")
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "999")
    from intelnet import telegram

    monkeypatch.setattr(telegram.bot, "enabled", False)


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "network.db"
    monkeypatch.setattr(settings, "db_path", path)
    db.init_db(path)
    return path


@pytest.fixture
def make_sensor(fresh_db):
    def _make(sensor_id: str = "tg:1", *, name: str = "Ann", zip_code: str = "62704",
              chat_id: str | None = None, kind: str = KIND_HUMAN, trust: float | None = None) -> Sensor:
        loc = geo.location_from_zip(zip_code) or geo.Location()
        s = db.upsert_sensor(Sensor(id=sensor_id, kind=kind, name=name,
                                    chat_id=chat_id or sensor_id.split(":")[-1], location=loc))
        if trust is not None:
            db.set_sensor_trust(sensor_id, trust)
            s = db.get_sensor(sensor_id) or s
        return s

    return _make


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture outbound Telegram sends as (chat_id, text) instead of posting."""
    from intelnet import telegram

    log: list[tuple[str, str]] = []

    def _send_to(chat_id, text, **_kw):
        log.append((str(chat_id), text))
        return True

    monkeypatch.setattr(telegram.bot, "enabled", True)
    monkeypatch.setattr(telegram.bot, "send_to", _send_to)
    return log


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
