"""The morning brief: each subscriber's own county, in the chat, once a day."""
from __future__ import annotations

import json
from datetime import timedelta

from intelnet import contrib, db, geo, network, pipeline
from intelnet.config import settings
from intelnet.models import KIND_STATION, Signal, iso, utcnow


def test_a_subscriber_gets_their_countys_night_and_the_state_in_brief(fresh_db, sent, make_sensor, monkeypatch):
    ann = make_sensor("tg:5", zip_code="62704", chat_id="5")                     # Sangamon County
    db.add_subscription("5", "weather.digest", "il")
    db.add_subscription("6", "weather.digest", "il")                             # no home: the state
    here = geo.location_from_zip("62704")
    network.process(Signal(source="iem_asos", source_id="SPI|1|rain_mm", sensor_id="station:spi",
                           sensor_kind=KIND_STATION, topic="weather", metric="rain_mm", value=30.0, unit="mm",
                           location=here, quality="reference", evidence={"period": "1h"}))
    contrib.contribute(ann, "rain 1.5in", source_id_base="m1", online=False, use_llm=False)
    ended = iso(utcnow() - timedelta(hours=3))
    db.save_alert_thread("T1", current_id="T1", event="Severe Thunderstorm Warning", severity="Severe",
                         counties_json=json.dumps(["17167"]), status="ended", opened_at=ended,
                         updated_at=ended, ended_at=ended, ended_reason="expired")
    today = utcnow().strftime("%Y-%m-%d")
    monkeypatch.setattr(settings, "gdrive_enabled", True)
    db.record_digest(today, drive_file_id="d", drive_url="https://docs.google.com/document/d/d/edit",
                     latest_url=None, folder_url=None, n_events=0, n_signals=1, n_sensors=1)
    out = pipeline.notify_digest(force=True)
    assert out["sent"] == 2 and out["links"]
    texts = dict(sent)
    mine = texts["5"]
    assert "Morning brief" in mine and "Sangamon County" in mine
    assert "Rainfall: up to" in mine and "1 from people" in mine
    assert "Ended: Severe Thunderstorm Warning" in mine
    assert "<blockquote expandable>" in mine and "Full digest" in mine and "county/sangamon.html" in mine
    state = texts["6"]
    assert "· Illinois" in state and "Set your home" in state and "Ended:" not in state   # no county lines
    assert pipeline.notify_digest(force=True)["sent"] == 0                         # once a day


def test_the_brief_waits_out_quiet_hours_and_goes_without_drive(fresh_db, sent, monkeypatch):
    db.add_subscription("5", "weather.digest", "il")
    monkeypatch.setattr(pipeline, "subscriptions_allowed_now", lambda: False)
    assert pipeline.notify_digest()["reason"] == "quiet hours" and not sent
    monkeypatch.setattr(pipeline, "subscriptions_allowed_now", lambda: True)
    out = pipeline.notify_digest()                                                 # Drive is off in tests
    assert out["sent"] == 1 and not out["links"] and "Full digest" not in sent[0][1]
