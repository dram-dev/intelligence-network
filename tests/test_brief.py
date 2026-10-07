"""The morning brief: each subscriber's own county, in the chat, once a day."""
from __future__ import annotations

import json
from datetime import timedelta

from intelnet import brief, contrib, db, geo, network, pipeline
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
    assert "· Illinois" in state and "Tap /home" in state and "Ended:" not in state       # no county lines
    # the day's map heads each rich brief, the reader's county outlined; it goes up as a JPEG
    rich = dict(zip([chat for chat, _ in sent], sent.rich, strict=True))
    assert 'tg://photo?id=dm-' in rich["5"] and "your county is outlined" in rich["5"]
    assert 'tg://photo?id=dm-' in rich["6"] and "your county is outlined" not in rich["6"]
    uploads = [m for m in sent.media if m]
    assert len(uploads) == 2 and all(next(iter(m.values()))[:2] == b"\xff\xd8" for m in uploads)
    assert "<b>Rain 1.50 in</b> · s-" in rich["5"]               # across the state: the map's stand-outs
    assert pipeline.notify_digest(force=True)["sent"] == 0                         # once a day


def test_the_briefs_stories_carry_the_maps_numbers(fresh_db, sent, make_sensor):
    from intelnet import brief, daymap, notable
    from intelnet.ingest.base import IngestedItem

    ann = make_sensor("tg:5", zip_code="62704", chat_id="5")
    contrib.contribute(ann, "rain 1.5in", source_id_base="m1", online=False, use_llm=False)
    db.upsert_items([IngestedItem(source="news", source_id="s1", title="Heavy rain soaks Springfield overnight",
                                  url="https://ex.test/1", content="...", metadata={"feed": "Google News"})])
    with db.get_conn() as conn:
        db.update_triage(conn.execute("SELECT id FROM items").fetchone()["id"], "keep", 0.9, "weather", "t")
    day = notable.build()
    [story] = day["news"]
    rich = brief.compose_rich("17167", {}, day=day, picture=daymap.prepare(day, "17167"))
    assert f"<p><b>{story['n']} · Springfield</b><br>" in rich and "↳ Rain 1.50 in, s-" in rich
    plain = brief.compose("17167", {}, day=day)
    assert f"<b>{story['n']} · Springfield</b>\n" in plain and "Heavy rain soaks Springfield" in plain


def test_the_brief_waits_out_quiet_hours_and_goes_without_drive(fresh_db, sent, monkeypatch):
    db.add_subscription("5", "weather.digest", "il")
    monkeypatch.setattr(pipeline, "subscriptions_allowed_now", lambda: False)
    assert pipeline.notify_digest()["reason"] == "quiet hours" and not sent
    monkeypatch.setattr(pipeline, "subscriptions_allowed_now", lambda: True)
    out = pipeline.notify_digest()                                                 # Drive is off in tests
    assert out["sent"] == 1 and not out["links"]
    assert "digest.html\">Full digest" in sent[0][1] and "Google Doc" not in sent[0][1]   # the site's copy still is


def test_full_digest_opens_the_phone_friendly_copy_then_the_doc(fresh_db, monkeypatch):
    """Google Docs gives a phone a 256-pixel copy of any picture: the brief's first link is the
    site's copy of the digest, the Google Doc one tap further."""
    from intelnet import brief

    monkeypatch.setattr(settings, "site_url", "https://example.test/intelnet/")
    links = {"digest": "https://docs.google.com/document/d/D/edit",
             "folder": "https://drive.google.com/drive/folders/F"}
    assert brief._digest_links(links) == [("Full digest", "https://example.test/intelnet/digest.html"),
                                          ("Google Doc", "https://docs.google.com/document/d/D/edit"),
                                          ("All digests", "https://drive.google.com/drive/folders/F")]
    monkeypatch.setattr(settings, "site_url", "")
    monkeypatch.setattr(settings, "github_repo", "")                 # no site at all: the Doc is the digest
    assert brief._digest_links(links)[0] == ("Full digest", "https://docs.google.com/document/d/D/edit")


def test_each_brief_carries_a_button_to_invite_a_neighbor(fresh_db, sent, make_sensor, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    make_sensor("tg:5", zip_code="62704", chat_id="5")                              # Sangamon County
    db.add_subscription("5", "weather.digest", "il")
    db.add_subscription("6", "weather.digest", "il")                                # no home
    monkeypatch.setattr(settings, "telegram_bot_handle", "intelligence_network_bot")
    pipeline.notify_digest(force=True)
    buttons = dict(zip([chat for chat, _ in sent], sent.markups, strict=True))
    [[mine]] = buttons["5"]["inline_keyboard"]
    assert mine["text"] == "📣 Invite a neighbor" and mine["url"].startswith("https://t.me/share/url?")
    shared = parse_qs(urlparse(mine["url"]).query)
    assert shared["url"] == ["https://t.me/intelligence_network_bot?start=sub_weather_warnings_sangamon"]
    assert shared["text"][0].startswith("Free weather warnings for Sangamon County")
    [[state]] = buttons["6"]["inline_keyboard"]
    assert parse_qs(urlparse(state["url"]).query)["url"] == ["https://t.me/intelligence_network_bot?start="]
    monkeypatch.setattr(settings, "telegram_bot_handle", "")
    assert brief.invite_markup(None) is None                                        # no handle, no button
