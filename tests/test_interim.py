"""The interim design wave: rich alert cards with buttons, mute, report-what-I-see, the
report reply, photos after app reports, chat sections, the rich morning brief, the app."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from test_alert_threads import BOX, feature, run_feed

from intelnet import bot, brief, contrib, db, delivery, export, geo, grids, telegram
from intelnet.config import settings
from intelnet.feeds import nws_alerts


def _home(make_sensor, sid="tg:21", lat=39.7817, lon=-89.6501):
    s = make_sensor(sid, zip_code="62704")
    s.location = geo.location_from_point(lat, lon, online=False)
    return db.upsert_sensor(s)


def _svr(alert_id="R1", **kw):
    kw.setdefault("hail", "1.75")
    kw.setdefault("wind", "70 MPH")
    f = feature(alert_id, polygon=BOX,
                motion=(datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
                + "-00:00...storm...262DEG...43KT...39.78,-89.95", **kw)
    f["properties"]["parameters"].update({"thunderstormDamageThreat": ["CONSIDERABLE"],
                                          "tornadoDetection": ["RADAR INDICATED"]})
    return f


def _tap(uid: int, message_id: int, data: str, cid: str, markup: dict | None = None) -> dict:
    m = {"message_id": message_id, "chat": {"id": uid, "type": "private"}}
    if markup:
        m["reply_markup"] = markup
    return {"update_id": 800 + len(cid), "callback_query": {"id": cid, "from": {"id": uid, "first_name": "Q"},
                                                            "data": data, "message": m}}


def test_the_alert_card_is_a_rich_message_with_buttons_and_a_plain_fallback(fresh_db, sent, monkeypatch, make_sensor):
    monkeypatch.setattr(settings, "site_url", "https://example.test/net/")
    _home(make_sensor)
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, _svr(sent_ago=5))
    rich, markup = sent.rich[0], sent.markups[0]
    # one modest heading; the reader's situation in plain bold; tags as one line, never
    # adjacent <mark>s (Telegram runs them together into one highlighter band)
    assert rich.startswith("<h4>⚠️ Severe Thunderstorm Warning</h4><p><b>Your home is inside the warned area.</b>")
    # the picture comes before the details, so the reader's own line never runs into the tags
    assert re.search(r'</p><figure><img src="tg://photo\?id=wm-[0-9a-f]+"/></figure><p><b>Considerable damage '
                     r'threat</b><br>Hail <b>1\.75 in</b> \(golf ball\) · Wind <b>70 mph</b> · Tornado '
                     r'radar-indicated</p><blockquote>Move to an interior room', rich)
    assert not any(tag in rich for tag in ("<h2>", "<mark>", "<code>", "<tg-map", 'format="r"'))
    assert "<footer>NWS Lincoln · until" in rich
    # the picture of the warned area, uploaded with the card
    [name] = re.findall(r'<figure><img src="tg://photo\?id=(wm-[0-9a-f]+)"/>', rich)
    assert sent.media[0][name][:2] == b"\xff\xd8"                        # a JPEG
    report, second = markup["inline_keyboard"]
    assert [b["text"] for b in report] == ["📍 Report what I see"] and report[0]["style"] == "primary"
    assert [b["text"] for b in second] == ["🗺 Live map", "🔕 Mute 1 hour"]
    assert "focus=R1" in second[0]["web_app"]["url"] and report[0]["callback_data"].startswith("rw:")
    assert "Your home is inside the warned area" in sent[0][1]           # the plain version stands by
    run_feed(monkeypatch, _svr("R2", refs=("R1",), message_type="Update", sent_ago=1))
    assert "updated" in sent.edit_rich[-1] and sent.edit_markups[-1]["inline_keyboard"]
    run_feed(monkeypatch)
    monkeypatch.setattr(nws_alerts, "ABSENT_CONFIRM", timedelta(0))
    run_feed(monkeypatch)
    assert "Severe Thunderstorm Warning ended early</h4>" in sent.edit_rich[-1]
    assert sent.edit_markups[-1] is None                                  # buttons go when it ends


def test_a_refused_rich_message_goes_out_plain_at_once(monkeypatch):
    calls = []

    def call(method, payload):
        calls.append(method)
        if method == "sendRichMessage":
            return telegram.Sent(False, permanent=True, error="Bad Request: can't parse rich message")
        return telegram.Sent(True, message_id=7)

    monkeypatch.setattr(telegram.bot, "enabled", True)
    monkeypatch.setattr(telegram.bot, "_call", call)
    assert telegram.bot.deliver("1", "plain", rich="<h2>rich</h2>").ok and calls == ["sendRichMessage", "sendMessage"]
    calls.clear()
    monkeypatch.setattr(telegram.bot, "_call", lambda m, p: calls.append(m) or telegram.Sent(False, retry_after=3))
    assert not telegram.bot.deliver("1", "plain", rich="<h2>rich</h2>").ok and calls == ["sendRichMessage"]  # retried later
    monkeypatch.setattr(settings, "telegram_rich_messages", False)
    calls.clear()
    telegram.bot.deliver("1", "plain", rich="<h2>rich</h2>")
    assert calls == ["sendMessage"]


def test_mute_keeps_alerts_coming_silently_until_unmuted(fresh_db, sent, monkeypatch, make_sensor):
    _home(make_sensor)
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    bot.handle_updates([_tap(21, 5, "mute:60", "m1", {"inline_keyboard": [[{"text": "🔕 Mute 1 hr", "callback_data": "mute:60"}]]})])
    assert delivery.muted("21") and "Muted until" in sent.answers[-1][1]
    assert sent.markup_edits[-1][2]["inline_keyboard"][0][0]["callback_data"] == "unmute"
    run_feed(monkeypatch, _svr(sent_ago=5))
    assert sent.replies[-1][2] is True                                   # the card arrived, silently
    bot.handle_updates([_tap(21, 5, "unmute", "m2")])
    assert not delivery.muted("21")
    bot.handle_updates([_tap(21, 6, "sample", "m3")])
    assert sent.answers[-1][1].startswith("Sample card") and not delivery.muted("21")
    assert "Muted until" in bot.handle_message({"message_id": 9, "date": int(time.time()), "chat": {"id": 21},
                                                "from": {"id": 21}, "text": "/mute 30"})


def test_report_what_i_see_asks_under_the_card_and_records_a_tap(fresh_db, sent, monkeypatch, make_sensor):
    _home(make_sensor)
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, _svr(sent_ago=5))
    card_mid = db.card("alert:R1", "21")["message_id"]
    rw = sent.markups[0]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_updates([_tap(21, card_mid, rw, "r1")])
    assert sent.replies[-1][1] == card_mid and "What are you seeing right now?" in sent[-1][1]
    buttons = [b for row in sent.markups[-1]["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons][:2] == ["🧊 Hail", "💨 Wind"]
    nothing = next(b for b in buttons if "Nothing" in b["text"])["callback_data"]
    bot.handle_updates([_tap(21, 777, nothing, "r2")])
    assert db.message_signals("telegram", "tg:21", "21:777")[0].metric == "nothing_here"


def test_pickers_offer_type_it(fresh_db, sent):
    bot.handle_message({"message_id": 1, "date": int(time.time()), "chat": {"id": 31}, "from": {"id": 31}, "text": "/join"})
    picker = bot.handle_message({"message_id": 2, "date": int(time.time()), "chat": {"id": 31}, "from": {"id": 31},
                                 "text": "🧊 Hail"})
    last = picker.markup["inline_keyboard"][-1][-1]
    assert last["text"] == "⌨️ Type it"
    bot.handle_updates([_tap(31, 2, last["callback_data"], "t1")])
    assert sent.markups[-1]["force_reply"] is True and "hail 1.25in" in sent[-1][1]


def test_a_photo_after_an_app_report_joins_it_as_evidence(fresh_db, sent):
    base = {"date": int(time.time()), "chat": {"id": 41, "type": "private"}, "from": {"id": 41, "first_name": "P"}}
    bot.handle_message({**base, "message_id": 1, "text": "/join"})
    bot.handle_message({**base, "message_id": 2, "text": "/home 62704"})
    r = bot.handle_message({**base, "message_id": 3, "web_app_data": {"data": json.dumps(
        {"a": "report", "text": "hail quarter", "photo": True})}})
    assert "Now send the photo" in r
    r = bot.handle_message({**base, "message_id": 4, "photo": [{"file_id": "small"}, {"file_id": "BIG"}]})
    assert "Attached to your report" in r
    [hail] = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    assert hail.evidence["photo_file_id"] == "BIG" and hail.quality == "raw"     # evidence, never proof


def test_chat_sections_when_topic_mode_is_on(fresh_db, sent, monkeypatch, make_sensor):
    monkeypatch.setattr(settings, "telegram_topics", "on")
    made = []
    monkeypatch.setattr(telegram.bot, "create_topic", lambda chat, name, color=None: made.append(name) or 100 + len(made))
    _home(make_sensor)
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, _svr(sent_ago=5))
    run_feed(monkeypatch, _svr("R2", refs=("R1",), message_type="Update", sent_ago=1, hail="2.75"))
    assert made == ["⚠️ Alerts"] and set(sent.threads) == {101}                # card and its reply: one section
    delivery.send_now([db.enqueue("helped:9", "21", "note", silent=True)])
    assert made[-1] == "📍 My reports" and sent.threads[-1] == 102
    monkeypatch.setattr(settings, "telegram_topics", "off")
    assert delivery.thread_for("21", "alerts") is None


def test_the_report_reply_says_why_and_what_radar_shows(fresh_db, sent, monkeypatch, make_sensor):
    ann = _home(make_sensor)
    run_feed(monkeypatch, _svr(sent_ago=5))
    monkeypatch.setattr(grids, "quick_look", lambda sig, timeout=6.0: "radar estimates 1.57 in here (last hour)")
    r = bot.handle_message({"message_id": 5, "date": int(time.time()), "chat": {"id": 21}, "from": {"id": 21},
                            "text": "hail 1.75in"})
    assert "✅ <b>Recorded:</b> Hail size 1.75 in at" in r
    assert "<b>Corroborated</b>: Severe Thunderstorm Warning in effect; radar estimates 1.57 in here (last hour)" in r
    assert ann is not None


def test_quick_look_reads_the_radar_grid(fresh_db, monkeypatch, make_sensor):
    from intelnet.models import Signal

    monkeypatch.setattr(settings, "grid_checks_enabled", True)
    monkeypatch.setitem(grids.READERS, "mrms_grib2", lambda g, p, lat, lon: grids.Sample(40.0, datetime.now(timezone.utc), lat, lon))
    sig = Signal(source="t", source_id="x", sensor_id="s", sensor_kind="human", topic="weather", metric="hail_mm",
                 value=44, location=geo.location_from_point(39.78, -89.65, online=False))
    assert grids.quick_look(sig) == "radar estimates 1.57 in here (last hour)"
    monkeypatch.setitem(grids.READERS, "mrms_grib2", lambda g, p, lat, lon: grids.Sample(0.0, datetime.now(timezone.utc), lat, lon))
    assert grids.quick_look(sig) is None


def test_the_morning_brief_is_rich_with_a_readings_table(fresh_db, sent, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    for s, text, base in ((ann, "rain 1.2in", "m1"), (bob, "rain 1.1in", "m2")):
        contrib.contribute(s, text, source_id_base=base, online=False, use_llm=False)
    db.add_subscription("1", "weather.digest", "il")
    brief.fanout_brief("2026-10-01", {})
    rich = sent.rich[-1]
    assert rich.startswith("<h4>☀️ Morning brief: Sangamon County</h4>") and "<details><summary>The network</summary>" in rich
    assert "<p><b>Across Illinois</b></p><ul>" in rich
    assert rich.endswith("</footer>") and "<h3>" not in rich
    assert "<table compact><caption>Rainfall, last 24 hours</caption>" in rich and "corroborated" in rich
    assert "Morning brief" in sent[-1][1]                                # plain fallback


def test_the_app_page_and_its_map_data(fresh_db, tmp_path, make_sensor):
    contrib.contribute(make_sensor("tg:1", zip_code="62704"), "hail quarter", source_id_base="m1", online=False,
                       use_llm=False)
    snap = export.snapshot(days=1)
    assert snap["reports"][0]["who"].startswith("s-") and snap["reports"][0]["zip5"] == "62704"
    assert "gauges" in snap
    page = export.render_app_page(snap, tmp_path).read_text(encoding="utf-8")
    for bit in ('data-tab="alerts"', "il-reference.json", 'data-layer="gauges"', "Mute alerts for 1 hour", ".tiles {"):
        assert bit in page, bit
    ref = json.loads((export.SITE_DIR / "assets" / "il-reference.json").read_text())
    assert any(p[0] == "Springfield" for p in ref["places"]) and ref["rivers"] and ref["roads"] and ref["water"]
