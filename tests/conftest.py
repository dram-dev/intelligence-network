"""Shared fixtures — the suite is hermetic.

Every test gets a throwaway SQLite file; Telegram, Google Drive, the LLMs and
online geo lookups are all forced off, so nothing reaches the network even
when the developer `.env` is fully configured.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intelnet import db, geo
from intelnet.config import settings
from intelnet.models import KIND_HUMAN, Sensor

FIXTURES = Path(__file__).parent / "fixtures"


class _NoTelegram:
    """Stands in for `requests` inside intelnet.telegram: records, never connects."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, *a, **k):
        self.calls.append(url.rsplit("/", 1)[-1])
        raise ConnectionError("tests are offline")

    get = post


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    monkeypatch.setattr(settings, "geo_online_lookup", False)
    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(settings, "grid_checks_enabled", False)
    monkeypatch.setattr(settings, "backup_dir", tmp_path_factory.mktemp("backups"))
    monkeypatch.setattr(settings, "gdrive_enabled", False)
    monkeypatch.setattr(settings, "notify_enabled", False)
    monkeypatch.setattr(settings, "network_join_code", "")
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "999")
    # Deployment-specific values from the developer .env must not leak into assertions.
    monkeypatch.setattr(settings, "gdrive_account", "")
    monkeypatch.setattr(settings, "site_url", "")
    monkeypatch.setattr(settings, "network_contact_email", "")
    monkeypatch.setattr(settings, "site_auto_push", False)
    from intelnet import telegram

    monkeypatch.setattr(telegram.bot, "enabled", False)
    # Belt and braces: even with the bot enabled, nothing reaches api.telegram.org —
    # and a test that tries fails here rather than being swallowed as a failed send.
    guard = _NoTelegram()
    monkeypatch.setattr(telegram, "requests", guard)
    yield
    assert not guard.calls, f"a test tried to reach the Telegram API: {guard.calls}"


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


class Outbound(list):
    """(chat_id, text) per message sent; `.edits` holds (chat_id, message_id, text)."""

    def __init__(self) -> None:
        super().__init__()
        self.edits: list[tuple[str, int, str]] = []
        self.replies: list[tuple[str, int | None, bool]] = []    # (chat, replied-to id, silent)
        self.markups: list[dict | None] = []                     # keyboards sent, per message
        self.edit_markups: list[dict | None] = []                # keyboards on edits
        self.answers: list[tuple[str, str]] = []                 # (callback id, toast)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Outbound:
    """Capture outbound Telegram messages and edits instead of posting them."""
    from intelnet import delivery, telegram

    log = Outbound()
    ids = iter(range(1000, 1_000_000))

    def _deliver(chat_id, text, *, silent=False, reply_to=None, markup=None):
        log.append((str(chat_id), text))
        log.replies.append((str(chat_id), reply_to, silent))
        log.markups.append(markup)
        return telegram.Sent(True, message_id=next(ids))

    def _edit(chat_id, message_id, text, markup=None):
        log.edits.append((str(chat_id), message_id, text))
        log.edit_markups.append(markup)
        return telegram.Sent(True, message_id=message_id)

    def _answer(query_id, text=""):
        log.answers.append((str(query_id), text))
        return telegram.Sent(True)

    monkeypatch.setattr(telegram.bot, "enabled", True)
    monkeypatch.setattr(telegram.bot, "deliver", _deliver)
    monkeypatch.setattr(telegram.bot, "edit", _edit)
    monkeypatch.setattr(telegram.bot, "answer_callback", _answer)
    monkeypatch.setattr(telegram.bot, "typing", lambda *a, **k: None)
    monkeypatch.setattr(delivery, "PER_CHAT_SECONDS", 0.0)
    monkeypatch.setattr(delivery, "GLOBAL_SECONDS", 0.0)
    return log


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def shift_to_now(text: str, anchor: str = "newest") -> str:
    """Slide a fixture's clock so one of its timestamps is this moment.

    Fixtures hold real timestamps, so anything that asks "is this still active"
    — live alerts, retention windows, a digest's 24-hour recap — goes quiet once
    they age out, and the suite starts failing on a date nobody chose. The gaps
    between stamps are kept exactly as recorded; only the whole set moves.

    `anchor="newest"` reads the payload as just fetched, so its latest element
    is now and the rest is history. `anchor="oldest"` reads it as just issued,
    so the earliest element is now and the rest is still to come — which is what
    a test about alerts currently in force wants.
    """
    stamps = [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in _ISO.findall(text)]
    if not stamps:
        return text
    delta = datetime.now(timezone.utc) - (min(stamps) if anchor == "oldest" else max(stamps))
    return _ISO.sub(
        lambda m: (datetime.fromisoformat(m.group(0).replace("Z", "+00:00")) + delta).isoformat(),
        text,
    )


def load_fixture(name: str, *, fresh: bool = False, anchor: str = "newest") -> dict:
    """Read a recorded payload. `fresh` re-dates it to now (see `shift_to_now`)."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(shift_to_now(text, anchor) if fresh else text)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
