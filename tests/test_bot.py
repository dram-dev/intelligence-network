"""Telegram bot routing — every command through handle_message."""
from __future__ import annotations

import time

from intelnet import bot, db
from intelnet.config import settings


def msg(text: str | None = None, uid: int = 42, chat: int | None = None, mid: int = 1, **extra) -> dict:
    # `date` is now, not a fixed stamp: the bot times readings by it, so a frozen
    # one ages out of the windows these tests query.
    m = {"message_id": mid, "date": int(time.time()), "chat": {"id": chat or uid, "type": "private"},
         "from": {"id": uid, "first_name": "Cy", "last_name": "Q", "username": "cyq"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def test_help_join_home_me(fresh_db):
    assert "Intelligence Network" in bot.handle_message(msg("/start"))
    r = bot.handle_message(msg("/join"))
    assert "Welcome, Cy Q" in r and db.get_sensor("tg:42") is not None
    assert "already a sensor" in bot.handle_message(msg("/join"))
    r = bot.handle_message(msg("/home 60601-2001"))
    assert "60601-2001, Cook County" in r
    assert db.get_sensor("tg:42").location.zip9 == "60601-2001"
    assert "Couldn't place" in bot.handle_message(msg("/home atlantis"))
    r = bot.handle_message(msg("/me"))
    assert "tg:42" in r and "Cook County" in r


def test_join_code_gate(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "network_join_code", "secret")
    assert "join code" in bot.handle_message(msg("/join"))
    assert "Join first" in bot.handle_message(msg("rain 1in"))
    assert "Welcome" in bot.handle_message(msg("/join secret"))


def test_plain_text_auto_joins_and_records(fresh_db):
    r = bot.handle_message(msg("rain 1.2in @62704"))
    assert "Auto-joined" in r and "Recorded:" in r
    assert db.get_sensor("tg:42").n_signals == 1
    r = bot.handle_message(msg("gust 40", mid=2))          # no home, no @ → told how to fix
    assert "no location" in r and "/home" in r


def test_shared_location_sets_home(fresh_db):
    bot.handle_message(msg("/join"))
    r = bot.handle_message(msg(location={"latitude": 39.7817, "longitude": -89.6501}))
    assert "Sangamon County" in r
    assert db.get_sensor("tg:42").location.county_fips == "17167"


def test_photo_caption_is_a_reading_with_evidence(fresh_db):
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704"))
    r = bot.handle_message(msg(caption="hail quarter", photo=[{"file_id": "abc"}], mid=3))
    assert "Recorded:" in r
    sig = db.recent_signals(1, kinds=("human",))[0]
    assert sig.evidence["photo_file_id"] == "abc" and sig.metric == "hail_mm"


def test_json_signal_command(fresh_db):
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704"))
    r = bot.handle_message(msg('/signal {"metric":"rain_mm","value":0.5,"unit":"in"}'))
    assert "Rainfall 0.5 in at 62704" in r


def test_subscribe_flow(fresh_db):
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704-1234"))
    assert "weather.warnings</b> @ Sangamon County" in bot.handle_message(msg("/subscribe warnings"))
    assert "@ 60601" in bot.handle_message(msg("/subscribe events 60601"))
    assert "@ IL" in bot.handle_message(msg("/subscribe digest cook"))     # digest is state-wide
    assert "Already subscribed" in bot.handle_message(msg("/subscribe warnings"))
    subs = bot.handle_message(msg("/subs"))
    assert "weather.warnings" in subs and "weather.events" in subs and "weather.digest" in subs
    assert "Removed 1" in bot.handle_message(msg("/unsubscribe events 60601"))
    assert "Removed 2" in bot.handle_message(msg("/unsubscribe all"))
    assert "No subscriptions" in bot.handle_message(msg("/subs"))
    assert "Unknown category" in bot.handle_message(msg("/subscribe nope"))
    r = bot.handle_message(msg("/subscribe *.events 62704"))
    assert r.count("Subscribed") == 8 and "soil.events" in r and "quake.events" in r
    assert "@ Sangamon County" in bot.handle_message(msg("/subscribe soil.reports"))
    assert "Removed 8" in bot.handle_message(msg("/unsubscribe *.events 62704"))
    assert "Topics" in bot.handle_message(msg("/topics")) and "corn_stage" in bot.handle_message(msg("/topics agriculture"))


def test_near_alerts_latest_network(fresh_db):
    bot.handle_message(msg("/join"))
    assert "Where?" in bot.handle_message(msg("/near"))
    bot.handle_message(msg("/home 62704"))
    bot.handle_message(msg("rain 1in", mid=5))
    r = bot.handle_message(msg("/near 6h"))
    assert "Rainfall" in r and "last 6h" in r
    assert "No active NWS alerts for Sangamon County" in bot.handle_message(msg("/alerts"))
    assert "No active NWS alerts for Cook County" in bot.handle_message(msg("/alerts cook"))
    from intelnet.config import settings

    assert "offline" in bot.handle_message(msg("/latest"))              # Drive off (tests) → no dead links
    settings.gdrive_enabled = True
    try:
        assert "No digest published yet" in bot.handle_message(msg("/latest"))
        db.record_digest("2026-09-15", drive_file_id="d", drive_url="https://docs.google.com/document/d/d/edit",
                         latest_url="https://docs.google.com/document/d/l/edit",
                         folder_url="https://drive.google.com/drive/folders/f", n_events=0, n_signals=0, n_sensors=0)
        r = bot.handle_message(msg("/latest"))
    finally:
        settings.gdrive_enabled = False
    assert "2026-09-15" in r and "docs.google.com/document/d/d" in r
    assert "Sensors: 1 joined" in bot.handle_message(msg("/network"))


def test_digest_email_records_subscriber_even_without_drive(fresh_db):
    assert "Usage" in bot.handle_message(msg("/digest nope"))
    r = bot.handle_message(msg("/digest someone@example.com"))
    assert "someone@example.com" in r and db.email_subscribers() == ["someone@example.com"]


def test_admin_commands_are_gated(fresh_db):
    bot.handle_message(msg("/join"))
    assert bot.handle_message(msg("/admin stats")) == "Admin only."
    admin = msg("/admin ban tg:42", uid=999)
    assert "✅ ban tg:42" in bot.handle_message(admin)
    assert db.get_sensor("tg:42").status == "banned"
    assert bot.handle_message(msg("rain 1in")) is None            # banned sensors are ignored
    assert "✅ unban" in bot.handle_message(msg("/admin unban tg:42", uid=999))
    assert "trust tg:42 = 0.9" in bot.handle_message(msg("/admin trust tg:42 0.9", uid=999))
    assert db.get_sensor("tg:42").trust == 0.9
    assert "Sensors" in bot.handle_message(msg("/admin sensors", uid=999))


def test_unknown_command_and_empty(fresh_db):
    assert "Unknown command /bogus" in bot.handle_message(msg("/bogus"))
    assert bot.handle_message(msg()) is None
    assert bot.handle_message({"chat": {}, "from": {}}) is None


def test_privacy_and_forget(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "network_contact_email", "network@example.org")
    r = bot.handle_message(msg("/privacy"))
    assert "privacy.html" in r and "terms.html" in r and "network@example.org" in r and "/forget confirm" in r
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704"))
    bot.handle_message(msg("rain 1in", mid=7))
    bot.handle_message(msg("/subscribe warnings"))
    bot.handle_message(msg("/digest me@example.org"))
    assert "/forget confirm" in bot.handle_message(msg("/forget"))
    assert db.get_sensor("tg:42") is not None                           # nothing deleted without confirm
    r = bot.handle_message(msg("/forget confirm"))
    assert "Deleted 1 reading(s), 1 subscription(s), 1 e-mail address(es)" in r
    assert db.get_sensor("tg:42") is None and db.recent_signals(1) == [] and db.email_subscribers() == []
    assert db.subscriptions_for(42) == []
    assert "nothing stored" in bot.handle_message(msg("/forget confirm"))


def test_suspended_sensor_can_still_forget(fresh_db):
    bot.handle_message(msg("/join"))
    db.set_sensor_status("tg:42", "banned")
    assert bot.handle_message(msg("rain 1in")) is None
    assert "Deleted" in bot.handle_message(msg("/forget confirm"))


def test_backlog_is_answered_in_order_and_a_late_reading_says_so(fresh_db, sent):
    assert not hasattr(bot, "_drain_backlog")          # nothing sent while the bot was down is skipped
    late = msg("rain 0.5in @62704", uid=7, mid=11, date=int(time.time()) - 20 * 60)
    offset = bot.handle_updates([{"update_id": 501, "message": late},
                                 {"update_id": 502, "message": msg("/help", uid=7, mid=12)}])
    assert offset == 503 and [c for c, _ in sent] == ["7", "7"]
    assert "20 min after you sent it" in sent[0][1] and "<tg-time" in sent[0][1]
    sig = db.recent_signals(1)[0]
    assert sig.evidence["received_late_min"] == 20
    assert 19 * 60 <= (sig.received_at - sig.observed_at).total_seconds() <= 21 * 60


def test_an_edited_reading_replaces_the_original(fresh_db, sent):
    bot.handle_updates([{"update_id": 1, "message": msg("rain 0.5in @62704", uid=8, mid=21)}])
    bot.handle_updates([{"update_id": 2, "edited_message": msg("rain 1.5in @62704", uid=8, mid=21)}])
    rows = db.recent_signals(1, kinds=("human",), metric="rain_mm")
    assert [round(s.value, 1) for s in rows] == [38.1]
    assert "Corrected" in sent[-1][1]


def test_live_location_is_where_you_are_not_a_new_home(fresh_db, sent):
    bot.handle_message(msg("/join", uid=9))
    bot.handle_message(msg("/home 62704", uid=9))
    here = {"latitude": 39.80, "longitude": -89.64, "live_period": 3600}
    assert "Following your live location" in bot.handle_message(msg(uid=9, mid=30, location=here))
    tick = {"latitude": 39.85, "longitude": -89.60, "live_period": 3600}
    assert bot.handle_updates([{"update_id": 3, "edited_message": msg(uid=9, mid=30, location=tick)}]) == 4
    assert not sent                                                   # ticks are silent
    assert db.get_sensor("tg:9").location.zip5 == "62704"            # home unchanged
    assert round(db.live_location("9").lat, 2) == 39.85
    bot.handle_message(msg("hail quarter", uid=9, mid=31))
    hail = db.recent_signals(1, kinds=("human",), metric="hail_mm")[0]
    assert round(hail.location.lat, 2) == 39.85                      # the reading lands where they are


def test_a_site_link_joins_subscribes_and_offers_the_location_button(fresh_db, sent):
    bot.handle_updates([{"update_id": 7, "message": msg("/start sub_weather_warnings_cook", uid=11)}])
    assert db.get_sensor("tg:11") is not None
    assert [(r["category"], r["area"]) for r in db.subscriptions_for("11")] == [("weather.warnings", "il.cook")]
    assert "Subscribed" in sent[0][1] and sent.markups[0]["keyboard"][0][0]["request_location"] is True
    r = bot.handle_message(msg(uid=11, location={"latitude": 41.88, "longitude": -87.63}))
    assert "Home set" in r and r.markup["keyboard"][0][0]["text"] == "🌧 Rain"   # location button → report buttons
    assert "Intelligence Network" in bot.handle_message(msg("/start sub_bogus", uid=11))


def test_sharing_a_location_first_joins_you(fresh_db):
    r = bot.handle_message(msg(uid=12, location={"latitude": 39.7817, "longitude": -89.6501}))
    assert "Home set" in r and db.get_sensor("tg:12").location.county_fips == "17167"


def _tap(uid: int, chat: int, message_id: int, data: str, cid: str) -> dict:
    return {"update_id": 900 + message_id, "callback_query": {
        "id": cid, "from": {"id": uid, "first_name": "Q"}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}}


def test_the_report_keyboard_records_hail_in_two_taps_and_undoes_it(fresh_db, sent):
    bot.handle_message(msg("/join", uid=13))
    home = bot.handle_message(msg("/home 62704", uid=13))
    kb = home.markup
    assert [b["text"] for b in kb["keyboard"][0]] == ["🌧 Rain", "🧊 Hail", "💨 Wind"] and kb["is_persistent"]
    assert bot.handle_message(msg("/report", uid=13)).markup == kb
    picker = bot.handle_message(msg("🧊 Hail", uid=13, mid=40))
    quarter = next(b for row in picker.markup["inline_keyboard"] for b in row if b["text"].startswith("Quarter"))
    bot.handle_updates([_tap(13, 13, 41, quarter["callback_data"], "cb1")])
    assert [round(s.value) for s in db.recent_signals(1, kinds=("human",), metric="hail_mm")] == [25]
    assert sent.answers == [("cb1", "Quarter 1″")] and "Hail size" in sent.edits[-1][2]
    undo = sent.edit_markups[-1]["inline_keyboard"][0][0]["callback_data"]
    assert undo == "u:41"
    bot.handle_updates([_tap(13, 13, 41, undo, "cb2")])
    assert db.recent_signals(1, kinds=("human",), metric="hail_mm") == []
    assert sent.answers[-1] == ("cb2", "Removed") and "Removed" in sent.edits[-1][2]


def test_nothing_here_is_kept_and_counted_but_never_judged_or_pushed(fresh_db, sent):
    db.add_subscription("77", "weather.reports", "il.sangamon")
    bot.handle_message(msg("/join", uid=14))
    bot.handle_message(msg("/home 62704", uid=14))
    r = bot.handle_message(msg("✅ Nothing here", uid=14, mid=50))
    assert "All quiet" in r and "quiet reports show where" in r
    assert r.markup["inline_keyboard"][0][0]["callback_data"] == "u:50"
    s = db.get_sensor("tg:14")
    assert (s.trust, s.n_signals) == (0.5, 1)
    assert not sent                                        # not pushed to report subscribers


_TELEGRAM_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a",
                  "tg-emoji", "code", "pre", "blockquote", "tg-time"}


def _telegram_safe(html: str) -> bool:
    """Would Telegram's HTML parser accept this? Only its tags, and no bare '&'."""
    import re

    tags = {t.lower() for t in re.findall(r"</?\s*([A-Za-z][\w+-]*)", html)}
    bare_amp = re.search(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)", html)
    return tags <= _TELEGRAM_TAGS and bare_amp is None


def test_every_usage_and_error_reply_is_valid_telegram_html(fresh_db, monkeypatch):
    """A reply Telegram can't parse never arrives: a bare /subscribe once went unanswered
    because its usage line held a literal <category>."""
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "42")
    bot.handle_message(msg("/join"))
    for text in ("/subscribe", "/subscribe nonsense", "/subscribe warnings atlantis", "/unsubscribe nonsense",
                 "/home", "/home atlantis", "/near atlantis", "/alerts atlantis", "/admin", "/admin trust",
                 "/admin nope", "/followups", "/mute", "/unmute", "/topics", "/help", "/me", "/subs",
                 "/report", "/latest", "/network", "/privacy", "/digest nope", "/bogus"):
        r = bot.handle_message(msg(text))
        assert r is None or _telegram_safe(str(r)), (text, str(r)[:200])
