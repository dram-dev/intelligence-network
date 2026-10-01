"""The Telegram Mini App: its page, the state it's opened with, and what it sends back."""
from __future__ import annotations

import base64
import json
import time

from intelnet import bot, db, export
from intelnet.config import settings


def _msg(uid: int, **extra) -> dict:
    return {"message_id": 7, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "first_name": "Ann"}, **extra}


def _app(data: dict, uid: int = 31) -> dict:
    return _msg(uid, web_app_data={"data": json.dumps(data), "button_text": bot.APP_BUTTON})


def _state(markup: dict) -> dict:
    url = markup["keyboard"][-1][0]["web_app"]["url"]
    blob = url.split("#s=", 1)[1]
    return json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))


def test_the_keyboard_opens_the_app_with_the_chats_own_state(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "site_url", "https://example.test/intelnet/")
    bot.handle_message(_msg(31, text="/join"))
    bot.handle_message(_msg(31, text="/subscribe warnings"))
    bot.handle_message(_msg(31, text="rain 0.5in @62704"))
    r = bot.handle_message(_msg(31, text="/home 62704"))
    button = r.markup["keyboard"][-1][0]
    assert button["text"] == bot.APP_BUTTON and button["web_app"]["url"].startswith("https://example.test/intelnet/app/#s=")
    s = _state(r.markup)
    assert s["home"]["zip"] == "62704" and s["home"]["slug"] == "sangamon"
    assert s["reports"][0]["m"] == "Rainfall" and s["reports"][0]["v"] == "0.50 in" and s["reports"][0]["q"] == "raw"
    assert s["followups"] is True and s["trust"] == []
    monkeypatch.setattr(settings, "site_url", "http://localhost:8000/")               # Telegram needs https
    assert all("web_app" not in b for row in bot.report_keyboard(31)["keyboard"] for b in row)


def test_a_report_from_the_app_lands_where_it_says(fresh_db):
    bot.handle_message(_msg(31, text="/join"))
    r = bot.handle_message(_app({"a": "report", "text": "hail quarter", "lat": 39.7817, "lon": -89.6501}))
    assert "Recorded:" in r
    [hail] = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    assert hail.location.precision == "point" and round(hail.location.lat, 3) == 39.782
    assert "didn't come through" in bot.handle_message(_msg(31, web_app_data={"data": "not json"}))


def test_subscriptions_home_and_followups_from_the_app(fresh_db):
    bot.handle_message(_msg(31, text="/join"))
    bot.handle_message(_msg(31, text="/home 62704"))
    r = bot.handle_message(_app({"a": "subs", "add": [["weather.warnings", "il.sangamon"], ["weather.digest", "62704"],
                                                     ["bogus.category", "il"]], "remove": []}))
    assert [(x["category"], x["area"]) for x in db.subscriptions_for("31")] == [
        ("weather.digest", "il"), ("weather.warnings", "il.sangamon")]           # digest is state-wide; bogus ignored
    assert "Subscribed" in r
    bot.handle_message(_app({"a": "subs", "add": [], "remove": [["weather.warnings", "il.sangamon"]]}))
    assert [x["category"] for x in db.subscriptions_for("31")] == ["weather.digest"]
    assert "Home set" in bot.handle_message(_app({"a": "home", "place": "60601"}))
    assert db.get_sensor("tg:31").location.county_fips == "17031"
    assert "Follow-ups off" in bot.handle_message(_app({"a": "followups", "on": False}))


def test_the_app_page_is_built_and_topics_carry_the_report_buttons(fresh_db, tmp_path):
    snap = export.snapshot(days=1)
    weather = next(t for t in snap["topics"] if t["name"] == "weather")
    assert weather["quick_reports"][1]["id"] == "hail" and weather["quick_reports"][1]["choices"]
    page = export.render_app_page(snap, tmp_path).read_text(encoding="utf-8")
    assert "{{" not in page and "telegram-web-app.js" in page and "tg.sendData" in page
    assert "--accent:" in page                                                  # the site's tokens
